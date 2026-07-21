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

# Palette.
NAVY = "#0f2547"
BLUE = "#2563c9"
INK = "#16202e"
BODY = "#3d4757"
MUTED = "#7b8494"
RULE = "#e2e6ec"
HAIR = "#eef1f5"
PAGE_BG = "#e9edf2"
CARD_BG = "#ffffff"
READY = "#1a7f37"
REVIEW = "#a25a00"

# Deep, harmonious accent colours cycled across company cards for rhythm.
ACCENTS = ["#0f2547", "#2563c9", "#0e7490", "#3f3d76", "#4a5568"]

SERIF = "Georgia,'Times New Roman',serif"
SANS = "Arial,Helvetica,sans-serif"


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

def render_story_html(story: dict, first: bool) -> str:
    status = story.get("status", "needs_review")
    status_colour = READY if status == "ready" else REVIEW
    status_label = "Draft ready" if status == "ready" else "Needs review"
    title = escape(story.get("title", "Untitled story"))
    url = escape(story.get("url", "#"), quote=True)
    source = escape(story.get("source", "Unknown source"))
    story_type = escape(story.get("story_type", "Update"))
    when = escape(format_date(story.get("published_at", "")))
    summary = escape(story.get("summary", "")) or "Summary available on the dashboard."
    divider = "" if first else f"border-top:1px solid {HAIR};"

    return f"""
    <tr><td style="padding:15px 22px;{divider}">
      <a href="{url}" style="font-family:{SANS};font-size:16px;font-weight:bold;
         line-height:1.34;color:{INK};text-decoration:none;">{title}</a>
      <div style="font-family:{SANS};font-size:13px;color:{BODY};
                  line-height:1.55;margin-top:6px;">{summary}</div>
      <div style="font-family:{SANS};font-size:11px;color:{MUTED};
                  margin-top:9px;line-height:1.5;">
        <span style="font-weight:bold;color:{BODY};">{source}</span>
        &nbsp;&middot;&nbsp; {story_type} &nbsp;&middot;&nbsp; {when}
        &nbsp;&middot;&nbsp;
        <span style="color:{status_colour};font-weight:bold;">{status_label}</span>
        &nbsp;&middot;&nbsp;
        <a href="{url}" style="color:{BLUE};text-decoration:none;">Read source &rsaquo;</a>
      </div>
    </td></tr>"""


def render_company_card(name: str, stories: list[dict], accent: str) -> str:
    rows = "".join(
        render_story_html(story, first=(i == 0)) for i, story in enumerate(stories)
    )
    count = len(stories)
    tag = "1 story" if count == 1 else f"{count} stories"
    return f"""
    <tr><td style="padding:0 0 16px 0;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="background:{CARD_BG};border:1px solid {RULE};
                    border-top:4px solid {accent};border-radius:5px;">
        <tr><td style="padding:14px 22px 12px 22px;border-bottom:1px solid {RULE};">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
            <td style="font-family:{SANS};font-size:19px;font-weight:bold;
                       color:{accent};line-height:1.2;">{escape(name)}</td>
            <td align="right" style="font-family:{SANS};font-size:11px;
                       color:{MUTED};white-space:nowrap;padding-left:10px;">{tag}</td>
          </tr></table>
        </td></tr>
        {rows}
      </table>
    </td></tr>"""


def build_intro(groups: list[tuple[str, list[dict]]], total: int) -> str:
    covered = [(name, items) for name, items in groups if items]
    if not covered:
        return "Good morning. There is no new portfolio coverage in today&rsquo;s edition."

    top = max(
        (s for _, items in covered for s in items),
        key=lambda s: int(s.get("score", 0)),
    )
    lead_company = escape(str(top.get("company", "")))
    lead_title = escape(str(top.get("title", "")))
    names = escape(", ".join(name for name, _ in covered))
    stories_word = "story" if total == 1 else "stories"
    company_word = "company" if len(covered) == 1 else "companies"

    return (
        f"Good morning. Today&rsquo;s briefing carries {total} new {stories_word} "
        f"across {len(covered)} portfolio {company_word} &mdash; {names}. "
        f"Leading the edition: {lead_company}, &lsquo;{lead_title}&rsquo;."
    )


def build_quiet_strip(groups: list[tuple[str, list[dict]]]) -> str:
    quiet = [name for name, items in groups if not items]
    if not quiet or not SHOW_EMPTY_COMPANIES:
        return ""
    listed = escape(" &nbsp;&bull;&nbsp; ".join(quiet)).replace("&amp;", "&")
    return f"""
    <tr><td style="padding:6px 0 20px 0;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="background:{HAIR};border:1px solid {RULE};border-radius:5px;">
        <tr><td style="padding:14px 22px;">
          <div style="font-family:{SANS};font-size:11px;font-weight:bold;
                      color:{MUTED};margin-bottom:5px;">
            Also monitored &mdash; no new coverage today</div>
          <div style="font-family:{SANS};font-size:12px;color:{BODY};
                      line-height:1.6;">{listed}</div>
        </td></tr>
      </table>
    </td></tr>"""


def build_html(groups: list[tuple[str, list[dict]]], generated_at: str, total: int) -> str:
    when = format_date(generated_at)
    dash = escape(DASHBOARD_URL, quote=True)
    covered = sum(1 for _, items in groups if items)

    cards = ""
    accent_index = 0
    for name, stories in groups:
        if not stories:
            continue
        cards += render_company_card(name, stories, ACCENTS[accent_index % len(ACCENTS)])
        accent_index += 1

    preheader = f"Portfolio briefing for {when}: {total} stories across {covered} companies."

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:{PAGE_BG};">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;
       font-size:1px;line-height:1px;color:{PAGE_BG};">{escape(preheader)}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background:{PAGE_BG};padding:26px 12px;">
    <tr><td align="center">
      <table role="presentation" width="640" cellpadding="0" cellspacing="0"
             style="max-width:640px;width:100%;">

        <!-- Masthead banner -->
        <tr><td style="background:{NAVY};border-radius:6px 6px 0 0;padding:9px 22px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
            <td style="font-family:{SANS};font-size:11px;font-weight:bold;
                       color:#c7d3e6;letter-spacing:1px;">PORTFOLIO NEWSROOM</td>
            <td align="right" style="font-family:{SANS};font-size:11px;
                       color:#c7d3e6;">{when}</td>
          </tr></table>
        </td></tr>

        <!-- Wordmark -->
        <tr><td style="background:{CARD_BG};border-left:1px solid {RULE};
                       border-right:1px solid {RULE};padding:26px 22px 0 22px;"
                align="center">
          <div style="font-family:{SANS};font-weight:bold;font-size:33px;
               letter-spacing:1px;line-height:1.1;">
            <span style="color:{NAVY};">NEXT WAVE</span>
            <span style="color:{BLUE};">PARTNERS</span>
          </div>
          <div style="border-top:3px solid {NAVY};margin:14px 0 0 0;"></div>
        </td></tr>

        <!-- Serif hero title -->
        <tr><td style="background:{CARD_BG};border-left:1px solid {RULE};
                       border-right:1px solid {RULE};padding:16px 22px 6px 22px;"
                align="center">
          <div style="font-family:{SERIF};font-size:40px;color:{INK};
               line-height:1.05;font-weight:normal;">Portfolio Briefing</div>
        </td></tr>

        <!-- Intro + dashboard button -->
        <tr><td style="background:{CARD_BG};border-left:1px solid {RULE};
                       border-right:1px solid {RULE};padding:14px 26px 4px 26px;">
          <div style="font-family:{SANS};font-size:14px;font-weight:bold;
               color:{INK};line-height:1.5;">{build_intro(groups, total)}</div>
        </td></tr>
        <tr><td style="background:{CARD_BG};border-left:1px solid {RULE};
                       border-right:1px solid {RULE};border-bottom:1px solid {RULE};
                       border-radius:0 0 6px 6px;padding:16px 26px 22px 26px;">
          <a href="{dash}" style="display:inline-block;background:{NAVY};color:#ffffff;
             text-decoration:none;font-family:{SANS};font-size:13px;font-weight:bold;
             padding:11px 22px;border-radius:4px;">
             Open the dashboard for full stories and drafts &rarr;</a>
        </td></tr>

        <!-- Spacer -->
        <tr><td style="height:20px;line-height:20px;font-size:0;">&nbsp;</td></tr>

        <!-- Company cards -->
        <tr><td>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
            {cards}
            {build_quiet_strip(groups)}
          </table>
        </td></tr>

        <!-- Footer -->
        <tr><td style="border-top:3px solid {NAVY};padding:18px 22px 8px 22px;"
                align="center">
          <a href="{dash}" style="display:inline-block;border:1px solid {NAVY};
             color:{NAVY};text-decoration:none;font-family:{SANS};font-size:13px;
             font-weight:bold;padding:10px 22px;border-radius:4px;">
             Go to the dashboard &rarr;</a>
        </td></tr>
        <tr><td style="padding:12px 22px 26px 22px;" align="center">
          <div style="font-family:{SANS};font-size:11px;color:{MUTED};line-height:1.7;">
            An internal briefing for the Next Wave Partners team, compiled
            automatically by the portfolio newsroom.<br>
            This is an overview only &mdash; open each draft on the dashboard
            and review it before posting.
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
        "NEXT WAVE PARTNERS",
        "PORTFOLIO BRIEFING",
        f"{when}  |  {total} stories across {covered} companies",
        "",
        f"Dashboard (full stories and drafts): {DASHBOARD_URL}",
        "",
        "-" * 60,
    ]
    quiet = []
    for name, stories in groups:
        if not stories:
            quiet.append(name)
            continue
        lines.append("")
        lines.append(name)
        lines.append("-" * len(name))
        for story in stories:
            lines.append(f"  {story.get('title', '')}")
            if story.get("summary"):
                lines.append(f"    {story['summary']}")
            status = story.get("status", "needs_review")
            status_label = "Draft ready" if status == "ready" else "Needs review"
            lines.append(
                f"    {story.get('source', '')} | "
                f"{story.get('story_type', 'Update')} | "
                f"{format_date(story.get('published_at', ''))} | {status_label}"
            )
            lines.append(f"    {story.get('url', '')}")
    if quiet and SHOW_EMPTY_COMPANIES:
        lines += ["", "Also monitored, no new coverage today:", "  " + ", ".join(quiet)]
    lines += ["", "-" * 60, "", f"Dashboard: {DASHBOARD_URL}",
              "Internal briefing compiled automatically by the NWP newsroom."]
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
