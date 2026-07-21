"""Send a grouped HTML digest for the latest portfolio newsroom output."""

from __future__ import annotations

import html
import json
import os
import smtplib
import sys
from collections import defaultdict
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
NEWS_FILE = ROOT / "site" / "data" / "news.json"

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)
EMAIL_TO = [addr.strip() for addr in os.getenv("EMAIL_TO", "").split(",") if addr.strip()]
SUBJECT = os.getenv("EMAIL_SUBJECT", "Next Wave Partners portfolio briefing")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "").strip()
MAX_PER_SECTION = max(1, int(os.getenv("DIGEST_MAX_PER_SECTION", "4")))
SEND_IF_EMPTY = os.getenv("SEND_IF_EMPTY", "false").strip().lower() in {"1", "true", "yes"}

DEFAULT_SECTION_ORDER = ["company", "market", "supply_chain", "customers_partners", "leadership", "regulatory", "funding_mna", "other"]
DEFAULT_SECTION_LABELS = {
    "company": "Company updates",
    "market": "Market & sector",
    "supply_chain": "Supply chain & logistics",
    "customers_partners": "Customers & partnerships",
    "leadership": "Leadership & people",
    "regulatory": "Regulatory & legal",
    "funding_mna": "Funding & M&A",
    "other": "Other signals",
}

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


def load_news() -> dict[str, Any]:
    if not NEWS_FILE.exists():
        return {"stories": [], "generated_at": None, "section_order": DEFAULT_SECTION_ORDER, "section_labels": DEFAULT_SECTION_LABELS, "run_summary": {}}
    try:
        data = json.loads(NEWS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"stories": [], "generated_at": None, "section_order": DEFAULT_SECTION_ORDER, "section_labels": DEFAULT_SECTION_LABELS, "run_summary": {}}
    if not isinstance(data, dict):
        return {"stories": [], "generated_at": None, "section_order": DEFAULT_SECTION_ORDER, "section_labels": DEFAULT_SECTION_LABELS, "run_summary": {}}
    return data


def parse_date(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_date(value: str) -> str:
    dt = parse_date(value)
    if not dt:
        return value or "Unknown date"
    return dt.strftime("%d %b %Y")


def sort_key(story: dict[str, Any]) -> tuple[int, float]:
    return (int(story.get("score", 0)), parse_date(str(story.get("published_at", ""))) or datetime.min.replace(tzinfo=timezone.utc))


def section_for_story(story: dict[str, Any]) -> str:
    return (story.get("section") or "company").strip() or "company"


def load_grouped() -> tuple[list[tuple[str, dict[str, list[dict[str, Any]]]]], dict[str, Any]]:
    data = load_news()
    stories = [s for s in data.get("stories", []) if isinstance(s, dict) and s.get("id")]
    section_order = data.get("section_order") if isinstance(data.get("section_order"), list) else DEFAULT_SECTION_ORDER
    section_labels = data.get("section_labels") if isinstance(data.get("section_labels"), dict) else DEFAULT_SECTION_LABELS

    by_company: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for story in stories:
        by_company[story.get("company", "Unknown company")].append(story)

    ordered_companies = [name for name in COMPANY_ORDER if name in by_company]
    ordered_companies.extend(sorted(name for name in by_company if name not in ordered_companies))

    grouped: list[tuple[str, dict[str, list[dict[str, Any]]]]] = []
    for company in ordered_companies:
        sections: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for story in sorted(by_company[company], key=sort_key, reverse=True):
            sections[section_for_story(story)].append(story)
        grouped.append((company, sections))
    return grouped, {"generated_at": data.get("generated_at"), "story_count": len(stories), "section_order": section_order, "section_labels": section_labels, "run_summary": data.get("run_summary", {})}


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def story_chip(story: dict[str, Any]) -> str:
    score = int(story.get("score", 0))
    published = format_date(str(story.get("published_at", "")))
    source = esc(story.get("source") or story.get("discovered_via") or "")
    section = esc(story.get("section_label") or DEFAULT_SECTION_LABELS.get(section_for_story(story), "Other signals"))
    warnings = story.get("warnings") or []
    warn = f"<span class='warn'>{' '.join(esc(w) for w in warnings[:1])}</span>" if warnings else ""
    draft_state = "Needs review" if story.get("needs_human_review") else "Ready"
    return f"""
      <article class="story">
        <div class="story-top">
          <span class="badge">{section}</span>
          <span class="badge score">{score}/100</span>
        </div>
        <h4><a href="{esc(story.get('url'))}" target="_blank" rel="noreferrer">{esc(story.get('title') or 'Untitled')}</a></h4>
        <p class="meta">{source} · {published} · {esc(story.get('story_type') or 'Update')}</p>
        <p class="summary">{esc(story.get('summary') or '')}</p>
        <p class="why">{esc(story.get('why_it_matters') or '')}</p>
        <div class="foot">{warn}<span>{esc(draft_state)}</span></div>
      </article>
    """


def render_company(company: str, sections: dict[str, list[dict[str, Any]]], section_order: list[str], section_labels: dict[str, str]) -> str:
    total = sum(len(v) for v in sections.values())
    chunks: list[str] = []
    for section in section_order:
        items = sections.get(section, [])[:MAX_PER_SECTION]
        if not items:
            continue
        cards = "".join(story_chip(story) for story in items)
        chunks.append(f"""
          <section class="section-block">
            <h3>{esc(section_labels.get(section, DEFAULT_SECTION_LABELS.get(section, section.replace('_', ' ').title())))}</h3>
            <div class="grid">{cards}</div>
          </section>
        """)
    extras = sorted(k for k in sections if k not in section_order)
    for section in extras:
        items = sections.get(section, [])[:MAX_PER_SECTION]
        if not items:
            continue
        cards = "".join(story_chip(story) for story in items)
        chunks.append(f"""
          <section class="section-block">
            <h3>{esc(section_labels.get(section, section.replace('_', ' ').title()))}</h3>
            <div class="grid">{cards}</div>
          </section>
        """)
    return f"""
    <section class="company-card">
      <div class="company-head">
        <h2>{esc(company)}</h2>
        <span>{total} story{'ies' if total != 1 else ''}</span>
      </div>
      {''.join(chunks)}
    </section>
    """


def build_intro(grouped: list[tuple[str, dict[str, list[dict[str, Any]]]]], meta: dict[str, Any]) -> str:
    total = meta.get("story_count", 0)
    generated_at = format_date(str(meta.get("generated_at", ""))) if meta.get("generated_at") else "Unknown time"
    companies_with_news = len(grouped)
    summary = meta.get("run_summary", {}) if isinstance(meta.get("run_summary", {}), dict) else {}
    blurb = f"{total} stories across {companies_with_news} companies"
    if summary:
        bits = []
        if summary.get("providers_succeeded") is not None:
            bits.append(f"{summary['providers_succeeded']} provider hits")
        if summary.get("grounding_drops") is not None:
            bits.append(f"{summary['grounding_drops']} grounding drops")
        if bits:
            blurb += " · " + " · ".join(bits)
    dash = f"<a href='{esc(DASHBOARD_URL)}'>Open dashboard</a>" if DASHBOARD_URL else ""
    return f"""
      <div class="intro">
        <div>
          <div class="kicker">Next Wave Partners portfolio briefing</div>
          <h1>{esc(blurb)}</h1>
          <p>Generated {esc(generated_at)}. {dash}</p>
        </div>
      </div>
    """


def build_html(grouped: list[tuple[str, dict[str, list[dict[str, Any]]]]], meta: dict[str, Any]) -> str:
    order = meta.get("section_order") if isinstance(meta.get("section_order"), list) else DEFAULT_SECTION_ORDER
    labels = meta.get("section_labels") if isinstance(meta.get("section_labels"), dict) else DEFAULT_SECTION_LABELS
    body = "".join(render_company(company, sections, order, labels) for company, sections in grouped)
    if not body:
        body = "<p class='empty'>No stories were available for this digest.</p>"
    return f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width,initial-scale=1">
      <title>{esc(SUBJECT)}</title>
      <style>
        body {{ margin:0; padding:0; font-family: Arial, Helvetica, sans-serif; background:#f4f7fb; color:#102033; }}
        .wrap {{ max-width: 1120px; margin: 0 auto; padding: 24px; }}
        .intro {{ background: linear-gradient(135deg, #ffffff, #eef4ff); border: 1px solid #dfe8f6; border-radius: 20px; padding: 24px; margin-bottom: 20px; }}
        .kicker {{ text-transform: uppercase; letter-spacing: .08em; font-size: 12px; color: #4e6a86; font-weight: 700; }}
        h1 {{ margin: 8px 0 6px; font-size: 28px; }}
        .intro p {{ margin: 0; color: #5a7087; }}
        .company-card {{ background: #fff; border: 1px solid #e3eaf4; border-radius: 20px; overflow: hidden; margin-bottom: 18px; box-shadow: 0 8px 24px rgba(20, 42, 67, .06); }}
        .company-head {{ display:flex; justify-content:space-between; align-items:center; gap:12px; padding: 18px 20px; border-bottom: 1px solid #edf2f8; background: #fbfcfe; }}
        .company-head h2 {{ margin:0; font-size: 22px; }}
        .company-head span {{ color: #6a7f96; font-size: 13px; }}
        .section-block {{ padding: 16px 20px 4px; }}
        .section-block h3 {{ margin: 0 0 12px; font-size: 13px; letter-spacing: .08em; text-transform: uppercase; color:#6a7f96; }}
        .grid {{ display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
        .story {{ border: 1px solid #e6edf6; border-radius: 16px; padding: 16px; background: #fff; }}
        .story h4 {{ margin: 10px 0 8px; font-size: 16px; line-height: 1.35; }}
        .story h4 a {{ color:#14324f; text-decoration:none; }}
        .story h4 a:hover {{ text-decoration:underline; }}
        .story-top {{ display:flex; justify-content:space-between; gap:8px; }}
        .badge {{ display:inline-block; font-size: 11px; padding: 5px 8px; border-radius: 999px; background:#edf4ff; color:#365c8d; font-weight:700; }}
        .score {{ background:#eff8f1; color:#27633a; }}
        .meta {{ margin:0 0 10px; color:#6a7f96; font-size: 12px; }}
        .summary, .why {{ margin: 0 0 10px; font-size: 14px; line-height: 1.55; color:#23364b; }}
        .why {{ color:#39536d; }}
        .foot {{ display:flex; justify-content:space-between; gap:10px; align-items:center; color:#6a7f96; font-size: 12px; }}
        .warn {{ color:#9a5b00; }}
        .empty {{ color:#6a7f96; font-style: italic; }}
        @media (max-width: 760px) {{ .grid {{ grid-template-columns: 1fr; }} .wrap {{ padding: 14px; }} }}
      </style>
    </head>
    <body>
      <div class="wrap">
        {build_intro(grouped, meta)}
        {body}
      </div>
    </body>
    </html>
    """


def send_email(html_body: str) -> None:
    if not EMAIL_TO and not SEND_IF_EMPTY:
        raise SystemExit("EMAIL_TO is empty. Set EMAIL_TO or set SEND_IF_EMPTY=true to skip sending.")
    if not EMAIL_TO:
        return
    msg = EmailMessage()
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(EMAIL_TO)
    msg["Subject"] = SUBJECT
    msg.set_content("HTML digest attached. Open this email in an HTML-capable client.")
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.starttls()
        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)


def main() -> None:
    grouped, meta = load_grouped()
    html_body = build_html(grouped, meta)
    if not grouped and not SEND_IF_EMPTY:
        print("No stories to send.")
        return
    send_email(html_body)
    print(f"Sent digest with {meta.get('story_count', 0)} stories.")


if __name__ == "__main__":
    main()
