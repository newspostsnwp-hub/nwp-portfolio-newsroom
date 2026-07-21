"""Email a morning digest of the latest portfolio news.

Reads site/data/news.json (produced by update_news.py) and sends a
newspaper-style HTML briefing, grouped by portfolio company, over SMTP.
Everything is configured through environment variables, so no addresses
or credentials live in the repo.
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
DASHBOARD_URL = os.getenv(
    "DASHBOARD_URL", "https://nwp-portfolio-newsroom.newspostsnwp.workers.dev/"
).strip()
MAX_PER_COMPANY = max(1, int(os.getenv("DIGEST_MAX", "6")))
SEND_IF_EMPTY = os.getenv("SEND_IF_EMPTY", "false").strip().lower() in {"1", "true", "yes"}
SHOW_EMPTY_COMPANIES = os.getenv(
    "SHOW_EMPTY_COMPANIES", "true"
).strip().lower() in {"1", "true", "yes"}

# Fixed running order of the portfolio. The names must match the "company"
# field written into news.json by update_news.py.
COMPANY_ORDER = [
    "The Delivery Group",
    "Clearway Group",
    "Swytch",
    "Molinare",
    "Pip Studios",
    "Petainer",
    "Whitespace",
    "UOE",
    "McGraw Hill",
    "Fertility Associates",
    "Roof-Maker",
]

# Palette (navy / blue wordmark, restrained editorial greys).
NAVY = "#0f2547"
BLUE = "#2563c9"
INK = "#1a2332"
BODY = "#444e5c"
MUTED = "#8a94a6"
RULE = "#e4e8ee"
PAGE_BG = "#eef1f5"
CARD_BG = "#ffffff"
READY = "#1a7f37"
REVIEW = "#9a6700"


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


def sort_key(story: dict) -> tuple[int, float]:
    parsed = parse_date(story.get("published_at", ""))
    stamp = parsed.timestamp() if parsed else 0.0
    return (int(story.get("score", 0)), stamp)


def load_grouped() -> tuple[list[tuple[str, list[dict]]], str, int]:
    """Return ([(company, stories)], generated_at, total_story_count)."""
    data = json.loads(NEWS_FILE.read_text(encoding="utf-8"))
    stories = [s for s in data.get("stories", []) if isinstance(s, dict) and s.get("id")]

    buckets: dict[str, list[dict]] = {name: [] for name in COMPANY_ORDER}
    for story in stories:
        name = (story.get("company") or "").strip()
        match = next((c for c in COMPANY_ORDER if c.lower() == name.lower()), None)
        buckets.setdefault(match or name or "Other", []).append(story)

    ordered: list[tuple[str, list[dict]]] = []
    seen = set()
    for name in COMPANY_ORDER:
        items = sorted(buckets.get(name, []), key=sort_key, reverse=True)[:MAX_PER_COMPANY]
        ordered.append((name, items))
        seen.add(name)
    for name, items in buckets.items():
        if name not in seen and items:
            ordered.append((name, sorted(items, key=sort_key, reverse=True)[:MAX_PER_COMPANY]))

    total = sum(len(items) for _, items in ordered)
    return ordered, data.get("generated_at", ""), total


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------

def render_story_html(story: dict) -> str:
    status = story.get("status", "needs_review")
    badge_colour = READY if status == "ready" else REVIEW
    badge_label = "Draft ready" if status == "ready" else "Needs review"
    title = escape(story.get("title", "Untitled story"))
    url = escape(story.get("url", "#"), quote=True)
    source = escape(story.get("source", "Unknown source"))
    meta = " &nbsp;&middot;&nbsp; ".join(
        escape(str(part))
        for part in (
            story.get("story_type", "Update"),
            format_date(story.get("published_at", "")),
        )
    )
    summary = escape(story.get("summary", "")) or "Summary available on the dashboard."

    return f"""
    <tr><td style="padding:14px 0;border-bottom:1px solid {RULE};">
      <a href="{url}" style="font-family:Arial,Helvetica,sans-serif;font-size:16px;
         font-weight:bold;line-height:1.35;color:{INK};text-decoration:none;">{title}</a>
      <div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;
                  color:{BODY};line-height:1.5;margin-top:6px;">{summary}</div>
      <div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;
                  color:{MUTED};margin-top:8px;">
        Source: <a href="{url}" style="color:{BLUE};text-decoration:none;">{source}</a>
        &nbsp;&middot;&nbsp; {meta}
        &nbsp;&middot;&nbsp; <span style="color:{badge_colour};font-weight:bold;">
          {badge_label}</span>
      </div>
    </td></tr>"""


def render_company_html(name: str, stories: list[dict]) -> str:
    heading = f"""
    <tr><td style="padding:22px 0 6px 0;">
      <span style="font-family:Arial,Helvetica,sans-serif;font-size:11px;
                   font-weight:bold;letter-spacing:1.5px;text-transform:uppercase;
                   color:{NAVY};border-left:3px solid {BLUE};padding-left:10px;">
        {escape(name)}</span>
    </td></tr>"""

    if not stories:
        body = f"""
        <tr><td style="padding:4px 0 10px 12px;font-family:Arial,Helvetica,sans-serif;
            font-size:13px;color:{MUTED};font-style:italic;">
          No new coverage in this edition.</td></tr>"""
    else:
        body = "".join(render_story_html(story) for story in stories)

    return heading + body


def build_html(groups: list[tuple[str, list[dict]]], generated_at: str, total: int) -> str:
    when = format_date(generated_at)
    dash = escape(DASHBOARD_URL, quote=True)
    covered = sum(1 for _, items in groups if items)

    sections = "".join(
        render_company_html(name, stories)
        for name, stories in groups
        if stories or SHOW_EMPTY_COMPANIES
    )

    preheader = (
        f"Portfolio news briefing for {when} - {total} stories across "
        f"{covered} companies."
    )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:{PAGE_BG};">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;
       font-size:1px;line-height:1px;color:{PAGE_BG};">{escape(preheader)}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background:{PAGE_BG};padding:24px 12px;">
    <tr><td align="center">
      <table role="presentation" width="640" cellpadding="0" cellspacing="0"
             style="max-width:640px;width:100%;background:{CARD_BG};
                    border:1px solid {RULE};border-radius:6px;">
        <tr><td style="padding:26px 34px 0 34px;" align="center">
          <a href="{dash}" style="display:inline-block;background:{NAVY};
             color:#ffffff;text-decoration:none;font-family:Arial,Helvetica,sans-serif;
             font-size:13px;font-weight:bold;padding:11px 20px;border-radius:4px;">
             View the full dashboard and post drafts</a>
        </td></tr>

        <tr><td style="padding:22px 34px 0 34px;" align="center">
          <div style="font-family:Arial,Helvetica,sans-serif;font-weight:bold;
               font-size:34px;letter-spacing:1px;line-height:1.1;">
            <span style="color:{NAVY};">NEXT WAVE</span>
            <span style="color:{BLUE};">PARTNERS</span>
          </div>
        </td></tr>

        <tr><td style="padding:10px 34px 0 34px;" align="center">
          <div style="border-top:2px solid {NAVY};border-bottom:1px solid {RULE};
               padding:8px 0;font-family:Arial,Helvetica,sans-serif;font-size:11px;
               letter-spacing:2px;text-transform:uppercase;color:{BODY};">
            Portfolio Newsroom &nbsp;&middot;&nbsp; {when}
            &nbsp;&middot;&nbsp; {total} stories across {covered} companies
          </div>
        </td></tr>

        <tr><td style="padding:14px 34px 4px 34px;">
          <div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;
               color:{BODY};line-height:1.5;">
            The latest public coverage of the portfolio, ordered by company.
            Headlines link to their sources; full stories and ready-to-edit
            LinkedIn drafts are on the dashboard.
          </div>
        </td></tr>

        <tr><td style="padding:0 34px 8px 34px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
            {sections}
          </table>
        </td></tr>

        <tr><td style="padding:18px 34px 30px 34px;" align="center">
          <a href="{dash}" style="display:inline-block;border:1px solid {NAVY};
             color:{NAVY};text-decoration:none;font-family:Arial,Helvetica,sans-serif;
             font-size:13px;font-weight:bold;padding:10px 20px;border-radius:4px;">
             Open the dashboard</a>
          <div style="font-family:Arial,Helvetica,sans-serif;font-size:11px;
               color:{MUTED};line-height:1.6;margin-top:16px;">
            Internal briefing for the Next Wave Partners team, generated
            automatically by the portfolio newsroom.<br>
            An overview only - please review each draft on the dashboard
            before posting.
          </div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


# --------------------------------------------------------------------------
# Plain-text rendering (kept in sync for deliverability)
# --------------------------------------------------------------------------

def build_text(groups: list[tuple[str, list[dict]]], generated_at: str, total: int) -> str:
    when = format_date(generated_at)
    covered = sum(1 for _, items in groups if items)
    lines = [
        "NEXT WAVE PARTNERS - PORTFOLIO NEWSROOM",
        f"{when}  |  {total} stories across {covered} companies",
        "",
        f"Dashboard and post drafts: {DASHBOARD_URL}",
        "",
        "=" * 60,
    ]
    for name, stories in groups:
        if not stories and not SHOW_EMPTY_COMPANIES:
            continue
        lines.append("")
        lines.append(name.upper())
        if not stories:
            lines.append("  No new coverage in this edition.")
            continue
        for story in stories:
            lines.append(f"  - {story.get('title', '')}")
            if story.get("summary"):
                lines.append(f"    {story['summary']}")
            status = story.get("status", "needs_review")
            status_label = "Draft ready" if status == "ready" else "Needs review"
            lines.append(
                f"    Source: {story.get('source', '')} | "
                f"{story.get('story_type', 'Update')} | "
                f"{format_date(story.get('published_at', ''))} | {status_label}"
            )
            lines.append(f"    {story.get('url', '')}")
    lines += ["", "=" * 60, "", f"Open the dashboard: {DASHBOARD_URL}",
              "Internal briefing generated automatically by the NWP newsroom."]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Send
# --------------------------------------------------------------------------

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

    groups, generated_at, total = load_grouped()
    if total == 0 and not SEND_IF_EMPTY:
        print("No stories to send today; skipping email.")
        return

    subject = f"Next Wave Partners portfolio briefing - {format_date(generated_at)}"
    html_body = build_html(groups, generated_at, total)
    text_body = build_text(groups, generated_at, total)
    send_email(subject, html_body, text_body)
    print(f"Sent briefing with {total} stories to {', '.join(EMAIL_TO)}.")


if __name__ == "__main__":
    main()
