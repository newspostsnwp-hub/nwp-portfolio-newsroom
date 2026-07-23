"""Email the Next Wave Partners portfolio briefing.

Reads site/data/news.json and sends a newspaper-style HTML briefing.

Structure:
  * A short "Good morning" intro describing the edition.
  * One block per portfolio company that has stories in scope.
  * Inside each block, an optional SECTOR subheading carrying industry news
    for that company.

Nothing is padded. A company with no stories is omitted entirely, and the
sector subheading only appears when there is sector news to show.

The briefing goes out on every run. When no portfolio stories are in scope,
the edition falls back to a sector-only round-up rather than being skipped.
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
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
NEWS_FILE = ROOT / "site" / "data" / "news.json"

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)
EMAIL_TO = [a.strip() for a in os.getenv("EMAIL_TO", "").split(",") if a.strip()]
DASHBOARD_URL = os.getenv(
    "DASHBOARD_URL", "https://nwp-portfolio-newsroom.newspostsnwp.workers.dev/"
).strip()
MAX_PER_COMPANY = max(1, int(os.getenv("DIGEST_MAX", "4")))
MAX_SECTOR_PER_COMPANY = max(0, int(os.getenv("DIGEST_SECTOR_MAX", "3")))
# Sector items per company on a sector-only edition, where sector news is the
# whole briefing rather than a subheading.
MAX_SECTOR_FALLBACK = max(1, int(os.getenv("DIGEST_SECTOR_FALLBACK_MAX", "4")))
# "today" = only stories first seen in this refresh; "all" = everything in file.
EMAIL_SCOPE = os.getenv("EMAIL_SCOPE", "today").strip().lower()
# The briefing now sends on every run. Set SEND_IF_EMPTY=false to restore the
# old behaviour of skipping when there is genuinely nothing to carry.
SEND_IF_EMPTY = os.getenv("SEND_IF_EMPTY", "true").strip().lower() not in {"0", "false", "no"}

NAVY = "#0f2547"
BLUE = "#2563c9"
INK = "#16202e"
BODY = "#3d4757"
MUTED = "#7b8494"
RULE = "#e2e6ec"
HAIR = "#eef1f5"
PAGE_BG = "#e9edf2"
CARD = "#ffffff"
READY = "#1a7f37"
REVIEW = "#a25a00"
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


def day_key(value: str) -> str:
    parsed = parse_date(value)
    return parsed.date().isoformat() if parsed else ""


def sort_key(story: dict[str, Any]) -> tuple[int, float]:
    parsed = parse_date(str(story.get("published_at", "")))
    return (int(story.get("score", 0)), parsed.timestamp() if parsed else 0.0)


def load_edition() -> tuple[list[tuple[str, list[dict], list[dict]]], str, int, int, bool]:
    """Return ([(company, stories, sector_items)], generated_at, story_total,
    sector_total, sector_only).

    Normally only companies with at least one story appear, with their sector
    items riding along beneath. When no portfolio stories are in scope, the
    edition falls back to a sector-only round-up: every company with sector
    news gets a block, so the briefing still has something to carry. If the
    day's scope is empty on both streams, the sector round-up widens to
    whatever sector news the file still holds.
    """
    data = json.loads(NEWS_FILE.read_text(encoding="utf-8"))
    generated_at = str(data.get("generated_at", ""))
    today = day_key(generated_at)

    all_stories = [s for s in data.get("stories", []) if isinstance(s, dict) and s.get("title")]
    all_sector = [s for s in data.get("sector_stories", []) if isinstance(s, dict) and s.get("title")]

    def in_scope(items: list[dict]) -> list[dict]:
        if EMAIL_SCOPE != "today":
            return items
        return [s for s in items
                if day_key(str(s.get("first_seen") or s.get("published_at", ""))) == today]

    def by_company(items: list[dict]) -> dict[str, list[dict]]:
        grouped: dict[str, list[dict]] = {}
        for item in items:
            grouped.setdefault(str(item.get("company", "")).strip(), []).append(item)
        return grouped

    grouped = by_company(in_scope(all_stories))
    sector_by_company = by_company(in_scope(all_sector))

    sector_only = not grouped
    if sector_only and not sector_by_company:
        sector_by_company = by_company(all_sector)
    sector_only = sector_only and bool(sector_by_company)

    sector_cap = MAX_SECTOR_FALLBACK if sector_only else MAX_SECTOR_PER_COMPANY
    names = sorted(grouped) if grouped else sorted(sector_by_company)

    blocks: list[tuple[str, list[dict], list[dict]]] = []
    story_total = sector_total = 0
    for company in names:
        company_stories = sorted(grouped.get(company, []), key=sort_key,
                                 reverse=True)[:MAX_PER_COMPANY]
        company_sector = sorted(sector_by_company.get(company, []), key=sort_key,
                                reverse=True)[:sector_cap]
        if not company_stories and not company_sector:
            continue
        blocks.append((company, company_stories, company_sector))
        story_total += len(company_stories)
        sector_total += len(company_sector)
    return blocks, generated_at, story_total, sector_total, sector_only


def build_intro(blocks, story_total: int, sector_total: int, sector_only: bool) -> str:
    if not blocks:
        return ("Good morning. Nothing cleared the filters on either the portfolio or its "
                "sectors this morning, so today&rsquo;s edition is a quiet one.")
    names = escape(", ".join(name for name, _, _ in blocks))
    company_word = "company" if len(blocks) == 1 else "companies"
    if sector_only:
        item_word = "item" if sector_total == 1 else "items"
        return (f"Good morning. No new portfolio coverage today, so this edition runs as a "
                f"sector round-up &mdash; {sector_total} {item_word} from the industries of "
                f"{len(blocks)} portfolio {company_word}: {names}.")
    lead = max((s for _, stories, _ in blocks for s in stories),
               key=lambda s: int(s.get("score", 0)), default=None)
    word = "story" if story_total == 1 else "stories"
    intro = (f"Good morning. Today&rsquo;s briefing carries {story_total} new {word} "
             f"across {len(blocks)} portfolio {company_word} &mdash; {names}.")
    if lead:
        intro += (f" Leading the edition: {escape(str(lead.get('company','')))}, "
                  f"&lsquo;{escape(str(lead.get('title','')))}&rsquo;.")
    return intro


def render_story(story: dict[str, Any], first: bool) -> str:
    ready = story.get("status") == "ready"
    colour = READY if ready else REVIEW
    label = "Draft ready" if ready else "Needs review"
    url = escape(str(story.get("url", "#")), quote=True)
    divider = "" if first else f"border-top:1px solid {HAIR};"
    return f"""
    <tr><td style="padding:15px 22px;{divider}">
      <a href="{url}" style="font-family:{SANS};font-size:16px;font-weight:bold;
         line-height:1.34;color:{INK};text-decoration:none;">{escape(str(story.get('title','')))}</a>
      <div style="font-family:{SANS};font-size:13px;color:{BODY};line-height:1.55;
                  margin-top:6px;">{escape(str(story.get('summary','')))}</div>
      <div style="font-family:{SANS};font-size:11px;color:{MUTED};margin-top:9px;line-height:1.5;">
        <span style="font-weight:bold;color:{BODY};">{escape(str(story.get('source','')))}</span>
        &nbsp;&middot;&nbsp; {escape(str(story.get('story_type','Update')))}
        &nbsp;&middot;&nbsp; {escape(format_date(str(story.get('published_at',''))))}
        &nbsp;&middot;&nbsp; <span style="color:{colour};font-weight:bold;">{label}</span>
        &nbsp;&middot;&nbsp; <a href="{url}" style="color:{BLUE};text-decoration:none;">Read source &rsaquo;</a>
      </div>
    </td></tr>"""


def render_sector(items: list[dict[str, Any]], accent: str, divider: bool = True) -> str:
    if not items:
        return ""
    industry = escape(str(items[0].get("industry", "")) or "Sector")
    rows = []
    for item in items:
        url = escape(str(item.get("url", "#")), quote=True)
        angle = escape(str(item.get("angle", "")))
        rows.append(f"""
        <tr><td style="padding:9px 22px;">
          <a href="{url}" style="font-family:{SANS};font-size:13.5px;font-weight:bold;
             color:{INK};text-decoration:none;line-height:1.35;">{escape(str(item.get('title','')))}</a>
          <div style="font-family:{SANS};font-size:12px;color:{BODY};line-height:1.5;
                      margin-top:4px;">{escape(str(item.get('summary','')))}</div>
          {f'<div style="font-family:{SANS};font-size:11.5px;color:{MUTED};margin-top:4px;">{angle}</div>' if angle else ''}
          <div style="font-family:{SANS};font-size:10.5px;color:{MUTED};margin-top:5px;">
            {escape(str(item.get('source','')))} &nbsp;&middot;&nbsp;
            {escape(format_date(str(item.get('published_at',''))))}</div>
        </td></tr>""")
    return f"""
    <tr><td style="padding:12px 22px 6px 22px;background:{HAIR};{'border-top:1px solid ' + RULE + ';' if divider else ''}">
      <span style="font-family:{SANS};font-size:10.5px;font-weight:bold;letter-spacing:1.2px;
                   text-transform:uppercase;color:{accent};">Sector &mdash; {industry}</span>
    </td></tr>
    <tr><td style="padding:0;background:{HAIR};"><table role="presentation" width="100%"
        cellpadding="0" cellspacing="0">{''.join(rows)}</table></td></tr>"""


def render_block(company: str, stories: list[dict], sector: list[dict], accent: str) -> str:
    rows = "".join(render_story(s, i == 0) for i, s in enumerate(stories))
    count = len(stories)
    if count:
        tag = "1 story" if count == 1 else f"{count} stories"
    else:
        tag = "1 sector item" if len(sector) == 1 else f"{len(sector)} sector items"
    return f"""
    <tr><td style="padding:0 0 16px 0;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="background:{CARD};border:1px solid {RULE};border-top:4px solid {accent};">
        <tr><td style="padding:14px 22px 12px 22px;border-bottom:1px solid {RULE};">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
            <td style="font-family:{SANS};font-size:19px;font-weight:bold;color:{accent};
                       line-height:1.2;">{escape(company)}</td>
            <td align="right" style="font-family:{SANS};font-size:11px;color:{MUTED};
                       white-space:nowrap;padding-left:10px;">{tag}</td>
          </tr></table>
        </td></tr>
        {rows}
        {render_sector(sector, accent, bool(stories))}
      </table>
    </td></tr>"""


def build_html(blocks, generated_at: str, story_total: int, sector_total: int,
               sector_only: bool) -> str:
    when = format_date(generated_at)
    dash = escape(DASHBOARD_URL, quote=True)
    cards = "".join(render_block(name, stories, sector, ACCENTS[i % len(ACCENTS)])
                    for i, (name, stories, sector) in enumerate(blocks))
    masthead = "Sector Briefing" if sector_only else "Portfolio Briefing"
    cta = "Open the dashboard &rarr;" if sector_only else "Open the dashboard for drafts &rarr;"
    if sector_only:
        preheader = (f"Sector briefing for {when}: no new portfolio coverage, "
                     f"{sector_total} sector items across {len(blocks)} companies.")
        counts = f"{sector_total} sector items &nbsp;&middot;&nbsp; {len(blocks)} companies"
    else:
        preheader = (f"Portfolio briefing for {when}: {story_total} stories "
                     f"across {len(blocks)} companies.")
        counts = (f"{story_total} stories &nbsp;&middot;&nbsp; {len(blocks)} companies"
                  + (f" &nbsp;&middot;&nbsp; {sector_total} sector items" if sector_total else ""))

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:{PAGE_BG};">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;font-size:1px;
       line-height:1px;color:{PAGE_BG};">{escape(preheader)}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background:{PAGE_BG};padding:26px 12px;">
    <tr><td align="center">
      <table role="presentation" width="640" cellpadding="0" cellspacing="0"
             style="max-width:640px;width:100%;">

        <tr><td style="background:{NAVY};padding:9px 22px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
            <td style="font-family:{SANS};font-size:11px;font-weight:bold;color:#c7d3e6;
                       letter-spacing:1px;">PORTFOLIO NEWSROOM</td>
            <td align="right" style="font-family:{SANS};font-size:11px;color:#c7d3e6;">{when}</td>
          </tr></table>
        </td></tr>

        <tr><td style="background:{CARD};border-left:1px solid {RULE};border-right:1px solid {RULE};
                       padding:26px 22px 0 22px;" align="center">
          <div style="font-family:{SANS};font-weight:bold;font-size:33px;letter-spacing:1px;
               line-height:1.1;"><span style="color:{NAVY};">NEXT WAVE</span>
            <span style="color:{BLUE};">PARTNERS</span></div>
          <div style="border-top:3px solid {NAVY};margin:14px 0 0 0;"></div>
        </td></tr>

        <tr><td style="background:{CARD};border-left:1px solid {RULE};border-right:1px solid {RULE};
                       padding:16px 22px 6px 22px;" align="center">
          <div style="font-family:{SERIF};font-size:40px;color:{INK};line-height:1.05;">
            {masthead}</div>
          <div style="font-family:{SANS};font-size:10.5px;color:{MUTED};letter-spacing:1.5px;
               text-transform:uppercase;margin-top:6px;">{counts}</div>
        </td></tr>

        <tr><td style="background:{CARD};border-left:1px solid {RULE};border-right:1px solid {RULE};
                       padding:14px 26px 4px 26px;">
          <div style="font-family:{SANS};font-size:14px;font-weight:bold;color:{INK};
               line-height:1.5;">{build_intro(blocks, story_total, sector_total, sector_only)}</div>
        </td></tr>
        <tr><td style="background:{CARD};border-left:1px solid {RULE};border-right:1px solid {RULE};
                       border-bottom:1px solid {RULE};padding:16px 26px 22px 26px;">
          <a href="{dash}" style="display:inline-block;background:{NAVY};color:#ffffff;
             text-decoration:none;font-family:{SANS};font-size:13px;font-weight:bold;
             padding:11px 22px;">{cta}</a>
        </td></tr>

        <tr><td style="height:20px;line-height:20px;font-size:0;">&nbsp;</td></tr>

        <tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          {cards}
        </table></td></tr>

        <tr><td style="border-top:3px solid {NAVY};padding:18px 22px 8px 22px;" align="center">
          <a href="{dash}" style="display:inline-block;border:1px solid {NAVY};color:{NAVY};
             text-decoration:none;font-family:{SANS};font-size:13px;font-weight:bold;
             padding:10px 22px;">Go to the dashboard &rarr;</a>
        </td></tr>
        <tr><td style="padding:12px 22px 26px 22px;" align="center">
          <div style="font-family:{SANS};font-size:11px;color:{MUTED};line-height:1.7;">
            An internal briefing for the Next Wave Partners team, compiled automatically
            by the portfolio newsroom.<br>
            An overview only &mdash; open each draft on the dashboard and review it before posting.
          </div>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body></html>"""


def build_text(blocks, generated_at: str, story_total: int, sector_total: int,
               sector_only: bool) -> str:
    if sector_only:
        headline = (f"{format_date(generated_at)}  |  no new portfolio coverage  |  "
                    f"{sector_total} sector items across {len(blocks)} companies")
    else:
        headline = (f"{format_date(generated_at)}  |  {story_total} stories "
                    f"across {len(blocks)} companies")
    lines = ["NEXT WAVE PARTNERS",
             "SECTOR BRIEFING" if sector_only else "PORTFOLIO BRIEFING",
             headline, "", f"Dashboard: {DASHBOARD_URL}", "", "-" * 60]
    for company, stories, sector in blocks:
        lines += ["", company.upper(), "-" * len(company)]
        for story in stories:
            lines.append(f"  {story.get('title','')}")
            if story.get("summary"):
                lines.append(f"    {story['summary']}")
            status = "Draft ready" if story.get("status") == "ready" else "Needs review"
            lines.append(f"    {story.get('source','')} | {story.get('story_type','Update')} | "
                         f"{format_date(str(story.get('published_at','')))} | {status}")
            lines.append(f"    {story.get('url','')}")
        if sector:
            lines.append(f"  SECTOR - {sector[0].get('industry','')}")
            for item in sector:
                lines.append(f"    {item.get('title','')}")
                if item.get("summary"):
                    lines.append(f"      {item['summary']}")
                lines.append(f"      {item.get('source','')} | "
                             f"{format_date(str(item.get('published_at','')))}")
                lines.append(f"      {item.get('url','')}")
    lines += ["", "-" * 60, "", f"Dashboard: {DASHBOARD_URL}",
              "Internal briefing compiled automatically by the NWP newsroom."]
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
    missing = [n for n, v in (("SMTP_USER", SMTP_USER), ("SMTP_PASSWORD", SMTP_PASSWORD),
                              ("EMAIL_TO", EMAIL_TO)) if not v]
    if missing:
        print(f"Missing required settings: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    if not NEWS_FILE.exists():
        print(f"No news file found at {NEWS_FILE}", file=sys.stderr)
        sys.exit(1)

    blocks, generated_at, story_total, sector_total, sector_only = load_edition()
    if not blocks and not SEND_IF_EMPTY:
        print("Nothing in scope today; skipping email.")
        return

    label = "sector briefing" if sector_only else "portfolio briefing"
    subject = f"Next Wave Partners {label} - {format_date(generated_at)}"
    send_email(subject,
               build_html(blocks, generated_at, story_total, sector_total, sector_only),
               build_text(blocks, generated_at, story_total, sector_total, sector_only))
    print(f"Sent {label}: {story_total} stories, {sector_total} sector items, "
          f"{len(blocks)} companies -> {', '.join(EMAIL_TO)}")


if __name__ == "__main__":
    main()
