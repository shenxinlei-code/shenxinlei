#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import os
import re
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET


DEFAULT_FEEDS = [
    "https://news.google.com/rss/search?q=%22AI+tool%22+OR+%22AI+tools%22+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=%22artificial+intelligence%22+tool+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=%22machine+learning%22+tool+when:1d&hl=en-US&gl=US&ceid=US:en",
]

USER_AGENT = "Hermes-News-Digest/1.0"


@dataclass(frozen=True)
class Item:
    source: str
    title: str
    url: str
    published: datetime | None
    summary: str


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _text(el: ET.Element | None) -> str:
    return "" if el is None or el.text is None else el.text.strip()


def _clean_html(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_datetime(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None

    try:
        dt = parsedate_to_datetime(raw)
    except Exception:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _pick_link(entry: ET.Element) -> str:
    for child in entry:
        if _strip_ns(child.tag) != "link":
            continue
        href = child.attrib.get("href") or _text(child)
        if href:
            rel = child.attrib.get("rel", "")
            if rel in {"alternate", ""}:
                return href.strip()
    for child in entry:
        if _strip_ns(child.tag) == "link":
            href = _text(child)
            if href:
                return href
    return ""


def fetch_url(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_feed(xml_bytes: bytes, source_url: str) -> list[Item]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise RuntimeError(f"invalid XML from {source_url}: {e}") from e

    items: list[Item] = []

    for node in root.iter():
        tag = _strip_ns(node.tag)
        if tag not in {"item", "entry"}:
            continue

        children = {}
        for child in node:
            children.setdefault(_strip_ns(child.tag), []).append(child)

        def first(name: str) -> ET.Element | None:
            vals = children.get(name) or []
            return vals[0] if vals else None

        title = _clean_html(_text(first("title")))
        if not title:
            continue

        url = _pick_link(node)
        pub_raw = _text(first("pubDate")) or _text(first("updated")) or _text(first("published"))
        published = _parse_datetime(pub_raw)

        summary = _clean_html(
            _text(first("description"))
            or _text(first("summary"))
            or _text(first("content"))
        )

        items.append(
            Item(
                source=source_url,
                title=title,
                url=url,
                published=published,
                summary=summary,
            )
        )

    return items


def load_feed_urls(cli_urls: list[str]) -> list[str]:
    if cli_urls:
        return [u.strip() for u in cli_urls if u.strip()]

    env = os.environ.get("NEWS_FEEDS", "").strip()
    if env:
        urls = []
        for part in re.split(r"[\n,]+", env):
            part = part.strip()
            if part:
                urls.append(part)
        if urls:
            return urls

    return DEFAULT_FEEDS[:]


def dedupe_and_sort(items: list[Item]) -> list[Item]:
    seen = set()
    uniq: list[Item] = []

    for item in items:
        key = (item.url or item.title).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(item)

    uniq.sort(key=lambda x: x.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return uniq


def filter_recent(items: list[Item], hours: int) -> list[Item]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent = []
    for item in items:
        dt = item.published or datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt >= cutoff:
            recent.append(item)
    return recent


def render_text(items: list[Item]) -> str:
    now = datetime.now(timezone.utc).astimezone()
    lines = [
        "AI 工具新闻日报",
        f"生成时间：{now:%Y-%m-%d %H:%M:%S %Z}",
        "",
    ]

    if not items:
        lines.append("今天没有抓到新的新闻。")
        return "\n".join(lines) + "\n"

    for idx, item in enumerate(items, 1):
        lines.append(f"{idx}. {item.title}")
        if item.published:
            local = item.published.astimezone()
            lines.append(f"   时间：{local:%Y-%m-%d %H:%M}")
        if item.url:
            lines.append(f"   链接：{item.url}")
        if item.summary:
            summary = item.summary
            if len(summary) > 300:
                summary = summary[:300].rstrip() + "…"
            lines.append(f"   摘要：{summary}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_html(items: list[Item]) -> str:
    now = datetime.now(timezone.utc).astimezone()
    parts = [
        "<html><body>",
        "<h2>AI 工具新闻日报</h2>",
        f"<p>生成时间：{html.escape(now.strftime('%Y-%m-%d %H:%M:%S %Z'))}</p>",
    ]

    if not items:
        parts.append("<p>今天没有抓到新的新闻。</p>")
        parts.append("</body></html>")
        return "\n".join(parts)

    parts.append("<ol>")
    for item in items:
        parts.append("<li>")
        parts.append(f"<strong>{html.escape(item.title)}</strong><br>")
        if item.published:
            local = item.published.astimezone()
            parts.append(f"<div>时间：{html.escape(local.strftime('%Y-%m-%d %H:%M'))}</div>")
        if item.url:
            esc_url = html.escape(item.url, quote=True)
            parts.append(f'<div>链接：<a href="{esc_url}">{esc_url}</a></div>')
        if item.summary:
            summary = item.summary
            if len(summary) > 300:
                summary = summary[:300].rstrip() + "…"
            parts.append(f"<div>摘要：{html.escape(summary)}</div>")
        parts.append("</li>")
    parts.append("</ol>")
    parts.append("</body></html>")
    return "\n".join(parts)


def send_email(subject: str, text_body: str, html_body: str | None = None) -> None:
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    mail_to = os.environ["MAIL_TO"]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = mail_to
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    if smtp_port == 465:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.login(smtp_user, smtp_password)
            server.send_message(msg)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch AI-related RSS/Atom items and email a digest.")
    parser.add_argument("--feed", action="append", default=[], help="Feed URL (can be repeated)")
    parser.add_argument("--hours", type=int, default=24, help="Only include items newer than this many hours")
    parser.add_argument("--limit", type=int, default=12, help="Maximum number of items to include")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout in seconds")
    parser.add_argument("--subject", default="AI 工具新闻日报", help="Email subject")
    args = parser.parse_args()

    feeds = load_feed_urls(args.feed)

    all_items: list[Item] = []
    errors: list[str] = []

    for feed in feeds:
        try:
            xml = fetch_url(feed, timeout=args.timeout)
            all_items.extend(parse_feed(xml, feed))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            errors.append(f"{feed}: network error: {e}")
        except RuntimeError as e:
            errors.append(str(e))

    recent = filter_recent(dedupe_and_sort(all_items), args.hours)[: args.limit]

    text_body = render_text(recent)
    html_body = render_html(recent)

    if not recent:
        print("No recent items found; nothing sent.")
        return 0

    send_email(args.subject, text_body, html_body)

    print(f"Sent digest with {len(recent)} item(s).")
    if errors:
        print("\nWarnings:", file=sys.stderr)
        for err in errors:
            print(f"- {err}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
