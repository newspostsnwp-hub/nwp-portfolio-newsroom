"""Email a morning digest of the latest portfolio news.

Reads site/data/news.json (produced by update_news.py) and sends an HTML
summary of the top stories over SMTP. Everything is configured through
environment variables, so no addresses or credentials live in the repo.
"""

from __future__ import annotations

import json
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NEWS_FILE = ROOT / "site" / "data" / "news.json"

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)
EMAIL_TO = [addr.strip() for addr in os.getenv("EMAIL_TO", "").split(",") if addr.strip()]
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "").strip()
DIGEST_MAX = max(1, int(os.getenv("DIGEST_MAX", "12")))
SEND_IF_EMPTY = os.getenv("SEND_IF_EMPTY", "false").strip().lower() in {"1", "true", "yes"}


def parse_date(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        parsed = parsedate_to_datetime(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def format_date(value: str) -> str:
    parsed = parse_date(value)
    return parsed.strftime("%d %b %Y") if parsed else "Date unavailable"


def load_stories() -> tuple[list[dict], str]:
    data = json.loads(NEWS_FILE.read_text(encoding="utf-8"))
    stories = [s for s in data.get("stories", []) if isinstance(s, dict) and s.get("id")]
    stories.sort(key=lambda s: int(s.get("score", 0)), reverse=True)
    return stories[:DIGEST_MAX], data.get("generated_at", "")


def build_html(stories: list[dict], generated_at: str) -> str:
    when = format_date(generated_at)
    rows = []
    for story in stories:
        status = story.get("status", "needs_review")
        badge_colour = "#1a7f37" if status == "ready" else "#9a6700"
        badge_label = "Ready to post" if status == "ready" else "Needs review"
        title = escape(story.get("title", "Untitled story"))
        url = escape(story.get("url", "#"), quote=True)
        meta = " &middot; ".join(
            escape(str(part))
            for part in (
                story.get("story_type", "Other"),
                story.get("source", "Unknown source"),
                format_date(story.get("published_at", "")),
            )
        )
        rows.append(f"""
        <tr><td style="padding:16px 0;border-bottom:1px solid #e5e7eb;">
          <div style="font-size:12px;color:#6b7280;text-transform:uppercase;
                      letter-spacing:.04em;">{escape(story.get('company', ''))}</div>
          <a href="{url}" style="font-size:17px;font-weight:600;color:#111827;
             text-decoration:none;">{title}</a>
          <div style="margin:6px 0;font-size:13px;color:#6b7280;">{meta}</div>
          <div style="font-size:14px;color:#374151;line-height:1.5;">
            {escape(story.get('summary', ''))}</div>
          <div style="margin-top:8px;font-size:12px;">
            <span style="color:{badge_colour};font-weight:600;">{badge_label}</span>
            <span style="color:#9ca3af;"> &nbsp;|&nbsp; Newsworthiness
              {int(story.get('score', 0))}/100</span>
          </div>
        </td></tr>""")

    dashboard_link = (
        f'<p style="margin-top:24px;"><a href="{escape(DASHBOARD_URL, quote=True)}" '
        f'style="color:#2563eb;">Open the full dashboard &rarr;</a></p>'
        if DASHBOARD_URL else ""
    )

    return f"""<!DOCTYPE html><html><body style="margin:0;background:#f3f4f6;
      font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
      <div style="max-width:640px;margin:0 auto;padding:24px;">
        <div style="background:#ffffff;border-radius:12px;padding:28px 32px;">
          <h1 style="margin:0;font-size:20px;color:#111827;">
            Portfolio news &mdash; {when}</h1>
          <p style="margin:4px 0 0;font-size:13px;color:#6b7280;">
            {len(stories)} stories from your Next Wave Partners newsroom.</p>
          <table style="width:100%;border-collapse:collapse;margin-top:8px;">
            {''.join(rows)}
          </table>
          {dashboard_link}
        </div>
        <p style="text-align:center;font-size:11px;color:#9ca3af;margin-top:16px;">
          Generated automatically by the NWP newsroom pipeline.</p>
      </div></body></html>"""


def build_text(stories: list[dict], generated_at: str) -> str:
    lines = [f"Portfolio news - {format_date(generated_at)}", ""]
    for story in stories:
        lines.append(f"* {story.get('company', '')}: {story.get('title', '')}")
        lines.append(
            f"  {story.get('story_type', 'Other')} | {story.get('source', '')} | "
            f"{format_date(story.get('published_at', ''))} | "
            f"score {int(story.get('score', 0))}/100"
        )
        if story.get("summary"):
            lines.append(f"  {story['summary']}")
        lines.append(f"  {story.get('url', '')}")
        lines.append("")
    if DASHBOARD_URL:
        lines.append(f"Full dashboard: {DASHBOARD_URL}")
    return "\n".join(lines)


def send_email(subject: str, html_body: str, text_body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = EMAIL_FROM
    message["To"] = ", ".join(EMAIL_TO)
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(message)


def main() -> None:
    missing = [
        name
        for name, value in (
            ("SMTP_USER", SMTP_USER),
            ("SMTP_PASSWORD", SMTP_PASSWORD),
            ("EMAIL_TO", EMAIL_TO),
        )
        if not value
    ]
    if missing:
        print(f"Missing required settings: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    if not NEWS_FILE.exists():
        print(f"No news file found at {NEWS_FILE}", file=sys.stderr)
        sys.exit(1)

    stories, generated_at = load_stories()
    if not stories and not SEND_IF_EMPTY:
        print("No stories to send today; skipping email.")
        return

    subject = f"Portfolio news - {format_date(generated_at)} ({len(stories)} stories)"
    html_body = build_html(stories, generated_at)
    text_body = build_text(stories, generated_at)
    send_email(subject, html_body, text_body)
    print(f"Sent digest with {len(stories)} stories to {', '.join(EMAIL_TO)}.")


if __name__ == "__main__":
    main()
