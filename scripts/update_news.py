"""Collect recent public signals around Next Wave Partners portfolio companies.

This v2 collector improves recall by combining:
- company entity graphs (brands, products, execs, suppliers, customers, etc.)
- official RSS/newsroom discovery and sitemap/page probing
- page diffing for career, pricing, leadership, support, investor and insight pages
- GDELT and Google News RSS coverage for market and supply-chain signals
- a richer section taxonomy (company, market, supply_chain, customers_partners,
  leadership, regulatory, funding_mna, other)
- Gemini analysis with provenance and reasoning metadata
"""

from __future__ import annotations

import base64
import dataclasses
import difflib
import hashlib
import html
import json
import logging
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from requests import Response

ROOT = Path(__file__).resolve().parents[1]
COMPANIES_FILE = ROOT / "config" / "companies.json"
SECTIONS_FILE = ROOT / "config" / "sections.json"
OUTPUT_FILE = ROOT / "site" / "data" / "news.json"
CACHE_DIR = ROOT / ".cache"
DISCOVERY_CACHE_FILE = CACHE_DIR / "discovery.json"
SEEN_CACHE_FILE = CACHE_DIR / "seen.json"
SNAPSHOT_CACHE_FILE = CACHE_DIR / "page_snapshots.json"

GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
GOOGLE_NEWS_RSS_ENDPOINT = "https://news.google.com/rss/search"

USER_AGENT = (
    "Mozilla/5.0 (compatible; NextWavePortfolioNewsroom/4.0; "
    "+https://nextwavepartners.co.uk/)"
)

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
LOOKBACK_DAYS = max(1, int(os.getenv("LOOKBACK_DAYS", "7")))
BLOG_FALLBACK_DAYS = max(7, int(os.getenv("BLOG_FALLBACK_DAYS", "120")))
MAX_PER_COMPANY = max(1, int(os.getenv("MAX_PER_COMPANY", "8")))
ANALYZE_PER_COMPANY = max(1, int(os.getenv("ANALYZE_PER_COMPANY", "10")))
MIN_SCORE = max(0, min(100, int(os.getenv("MIN_SCORE", "55"))))
READY_SCORE = max(0, min(100, int(os.getenv("READY_SCORE", "80"))))
ARTICLE_TEXT_LIMIT = max(2000, int(os.getenv("ARTICLE_TEXT_LIMIT", "11000")))
REQUEST_TIMEOUT_SECONDS = max(5, int(os.getenv("REQUEST_TIMEOUT_SECONDS", "25")))
GDELT_MAX_ATTEMPTS = max(1, int(os.getenv("GDELT_MAX_ATTEMPTS", "3")))
GEMINI_MAX_ATTEMPTS = max(1, int(os.getenv("GEMINI_MAX_ATTEMPTS", "4")))
GEMINI_MIN_INTERVAL_SECONDS = max(0.0, float(os.getenv("GEMINI_DELAY_SECONDS", "1.2")))
COLLECTION_WORKERS = max(1, int(os.getenv("COLLECTION_WORKERS", "6")))
RUN_BUDGET_SECONDS = max(60, int(os.getenv("RUN_BUDGET_SECONDS", "1500")))
DISCOVERY_TTL_DAYS = max(1, int(os.getenv("DISCOVERY_TTL_DAYS", "7")))
SEEN_TTL_DAYS = max(1, int(os.getenv("SEEN_TTL_DAYS", str(LOOKBACK_DAYS))))
SNAPSHOT_TTL_DAYS = max(1, int(os.getenv("SNAPSHOT_TTL_DAYS", "7")))
TITLE_RATIO_THRESHOLD = float(os.getenv("TITLE_RATIO_THRESHOLD", "0.86"))
TITLE_JACCARD_THRESHOLD = float(os.getenv("TITLE_JACCARD_THRESHOLD", "0.70"))
MAX_FEEDS_PER_COMPANY = 4
MAX_NEWSROOM_PAGES_PER_COMPANY = 6
MAX_WATCH_PAGES_PER_COMPANY = 12
MAX_BLOG_CANDIDATES = 8

HOST_MIN_INTERVALS = {
    "api.gdeltproject.org": 5.0,
    "news.google.com": 2.0,
}
DEFAULT_HOST_INTERVAL = 1.0

PROVIDER_PRIORITY = {
    "Official RSS": 0,
    "Page diff": 1,
    "Company blog": 2,
    "Official company site": 3,
    "GDELT": 4,
    "Google News RSS": 5,
}

STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "for", "on", "and", "or", "with", "as",
    "at", "by", "from", "is", "are", "be", "after", "over", "its", "new", "amid",
    "into", "up", "out", "how", "why", "what", "this", "that", "their", "our", "we",
}

SECTION_ORDER = [
    "company", "market", "supply_chain", "customers_partners", "leadership",
    "regulatory", "funding_mna", "other",
]
SECTION_LABELS = {
    "company": "Company updates",
    "market": "Market & sector",
    "supply_chain": "Supply chain & logistics",
    "customers_partners": "Customers & partnerships",
    "leadership": "Leadership & people",
    "regulatory": "Regulatory & legal",
    "funding_mna": "Funding & M&A",
    "other": "Other signals",
}

SECTION_KEYWORDS = {
    "supply_chain": [
        "supply chain", "logistics", "supplier", "suppliers", "shipping", "delivery",
        "freight", "warehouse", "inventory", "procurement", "manufacturing", "distribution",
        "materials", "raw material", "ports", "transport", "transportation", "lead time",
        "lead times", "fulfilment", "fulfillment", "import", "export", "factory", "production",
        "operations", "shortage", "backlog", "customs",
    ],
    "market": [
        "market", "sector", "industry", "demand", "pricing", "price", "outlook",
        "forecast", "revenue", "margin", "growth", "competition", "competitor",
        "customer", "clients", "contract", "funding", "capital", "valuation", "regulation",
        "regulatory", "policy", "macro", "economic", "economy", "expansion", "launch",
        "partnership", "earnings", "results", "strategy", "deal", "sales", "trend",
    ],
    "customers_partners": [
        "customer", "customers", "client", "clients", "partner", "partners", "partnership",
        "contract", "contracts", "pilot", "rollout", "reseller", "distributor", "channel",
        "supplier agreement", "renewal", "tender", "rfp", "framework agreement",
    ],
    "leadership": [
        "appoint", "appointed", "joins", "joined", "hire", "hired", "ceo", "cfo", "coo",
        "board", "director", "chair", "chief executive", "chief financial officer",
        "leadership", "executive", "promote", "promotion", "stepping down", "resigns",
        "resigned", "departure",
    ],
    "regulatory": [
        "regulator", "regulatory", "compliance", "court", "lawsuit", "litigation", "sec",
        "fca", "cma", "antitrust", "license", "approval", "audit", "investigation", "settlement",
        "claim", "appeal", "judgment", "fine", "penalty", "privacy", "sanction",
    ],
    "funding_mna": [
        "funding", "raise", "raised", "round", "investment", "invested", "acquisition",
        "acquire", "acquired", "merger", "m&a", "buyout", "ipo", "list", "listing",
        "sale", "sell", "sold", "takeover", "minority stake",
    ],
}

PAGE_HINTS = [
    ("news", "company"), ("newsroom", "company"), ("press", "company"), ("blog", "company"),
    ("insights", "market"), ("articles", "market"), ("updates", "company"), ("stories", "company"),
    ("careers", "leadership"), ("jobs", "leadership"), ("people", "leadership"),
    ("leadership", "leadership"), ("team", "leadership"), ("investor", "funding_mna"),
    ("investors", "funding_mna"), ("pricing", "market"), ("products", "company"),
    ("product", "company"), ("support", "company"), ("help", "company"), ("about", "company"),
    ("customers", "customers_partners"), ("case-studies", "customers_partners"), ("case-studies", "customers_partners"),
    ("partners", "customers_partners"), ("supplier", "supply_chain"), ("suppliers", "supply_chain"),
    ("logistics", "supply_chain"), ("warehouse", "supply_chain"), ("distribution", "supply_chain"),
]

FEED_PROBE_PATHS = (
    "/feed/", "/rss.xml", "/atom.xml", "/blog/feed/", "/news/feed/", "/feed.xml", "/news/rss.xml", "/blog/rss.xml",
)
NEWSROOM_PROBE_PATHS = (
    "/news/", "/newsroom/", "/blog/", "/press/", "/insights/", "/articles/", "/updates/", "/media-centre/",
)
WATCH_PAGE_PATHS = (
    "/news/", "/newsroom/", "/blog/", "/press/", "/insights/", "/updates/", "/stories/",
    "/careers/", "/jobs/", "/leadership/", "/about/", "/investors/", "/investor-relations/",
    "/pricing/", "/products/", "/product/", "/customers/", "/partners/", "/support/",
    "/faq/", "/case-studies/", "/sustainability/",
)
SKIP_LINK_WORDS = (
    "contact", "privacy", "cookie", "terms", "login", "sign in", "careers", "jobs", "about us",
    "our team", "people", "investors", "support", "faq", "subscribe", "newsletter", "search", "menu",
    "home", "shop", "cart",
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/xml;q=0.8,text/plain;q=0.7,*/*;q=0.6",
    "Accept-Language": "en-GB,en;q=0.9",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
LOGGER = logging.getLogger("nwp-news")

GEMINI_CLIENT: genai.Client | None = None


class UpstreamUnavailableError(RuntimeError):
    pass


_THREAD_LOCAL = threading.local()


def get_session() -> requests.Session:
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
        _THREAD_LOCAL.session = session
    return session


class HostRateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_slot: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = urlsplit(url).netloc.lower()
        interval = HOST_MIN_INTERVALS.get(host, DEFAULT_HOST_INTERVAL)
        with self._lock:
            now = time.monotonic()
            ready_at = max(now, self._next_slot.get(host, now))
            self._next_slot[host] = ready_at + interval
        delay = ready_at - now
        if delay > 0:
            time.sleep(delay)


RATE_LIMITER = HostRateLimiter()


def parse_retry_after(response: Response) -> float | None:
    header = response.headers.get("Retry-After")
    if not header:
        return None
    try:
        return max(0.0, float(header))
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds())
        except Exception:
            return None


def request_with_backoff(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    attempts: int = 3,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
    expected: str = "html",
    label: str = "request",
) -> Response:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            RATE_LIMITER.wait(url)
            response = get_session().get(url, params=params, timeout=timeout, allow_redirects=True)
            if response.status_code == 429:
                retry_after = parse_retry_after(response)
                sleep_for = retry_after if retry_after is not None else min(20.0, 2.0 * attempt)
                LOGGER.info("Rate limited for %s (%s), sleeping %.1fs", label, response.url, sleep_for)
                time.sleep(sleep_for)
                continue
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").casefold()
            if expected == "xml" and "xml" not in content_type and not response.content.lstrip().startswith(b"<"):
                raise requests.RequestException(f"Expected XML, got {content_type or 'unknown'}")
            if expected == "json" and "json" not in content_type:
                raise requests.RequestException(f"Expected JSON, got {content_type or 'unknown'}")
            return response
        except requests.RequestException as exc:
            last_exc = exc
            LOGGER.debug("%s attempt %s failed for %s: %s", label, attempt, url, exc)
            if attempt < attempts:
                time.sleep(min(20.0, 1.5 * attempt * attempt))
    assert last_exc is not None
    raise last_exc


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"<[^>]+>", " ", html.unescape(value))


def unique_strings(values: list[Any], limit: int | None = None) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = clean_text(value)
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        output.append(text)
        if limit is not None and len(output) >= limit:
            break
    return output


def normalise_url(url: str) -> str:
    parts = urlsplit(clean_text(url))
    if not parts.scheme or not parts.netloc:
        return clean_text(url)
    path = re.sub(r"/+$", "", parts.path or "/") or "/"
    query = parts.query if parts.query and not parts.query.startswith("utm_") else ""
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def story_id(url: str, title: str = "") -> str:
    basis = f"{normalise_url(url)}|{clean_text(title).casefold()}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean_text(title).casefold()).strip()


def title_tokens(title: str) -> set[str]:
    return {token for token in title_key(title).split() if token and token not in STOPWORDS}


def titles_similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    ratio = difflib.SequenceMatcher(None, title_key(a), title_key(b)).ratio()
    if ratio >= TITLE_RATIO_THRESHOLD:
        return True
    ta = title_tokens(a)
    tb = title_tokens(b)
    if not ta or not tb:
        return False
    jaccard = len(ta & tb) / len(ta | tb)
    return jaccard >= TITLE_JACCARD_THRESHOLD


def parse_datetime(value: str | None) -> datetime | None:
    value = clean_text(value)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except Exception:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def is_within(date_text: str, days: int) -> bool:
    parsed = parse_datetime(date_text)
    return parsed is not None and parsed >= cutoff(days)


def iso_or_original(date_text: str) -> str:
    parsed = parse_datetime(date_text)
    return parsed.isoformat() if parsed else clean_text(date_text)


def sortable_datetime(date_text: str | None) -> datetime:
    return parse_datetime(date_text) or datetime.min.replace(tzinfo=timezone.utc)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_sections() -> dict[str, Any]:
    if SECTIONS_FILE.exists():
        payload = load_json(SECTIONS_FILE, {})
        if isinstance(payload, dict):
            return payload
    return {"section_order": SECTION_ORDER, "section_labels": SECTION_LABELS, "section_keywords": SECTION_KEYWORDS}


def load_companies() -> list[dict[str, Any]]:
    data = load_json(COMPANIES_FILE, None)
    if not isinstance(data, list):
        raise ValueError("config/companies.json must contain a JSON array.")
    companies: list[dict[str, Any]] = []
    for index, raw in enumerate(data, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"Company entry {index} must be a JSON object.")
        name = clean_text(raw.get("name"))
        if not name:
            raise ValueError(f"Company entry {index} has no name.")
        company = dict(raw)
        company["name"] = name
        company["description"] = clean_text(raw.get("description"))
        company["website"] = clean_text(raw.get("website"))
        company["domain"] = clean_text(raw.get("domain"))
        company["blog_url"] = clean_text(raw.get("blog_url"))
        for field in ("aliases", "exclude_terms", "rss_feeds", "newsroom_urls", "search_terms", "brands", "products", "executives", "subsidiaries", "customers", "suppliers", "partners", "competitors", "locations", "watch_pages"):
            if not isinstance(raw.get(field, []), list):
                raise ValueError(f"{name}: {field} must be a JSON list.")
            company[field] = unique_strings(raw.get(field, []))
        companies.append(company)
    return companies


def company_entity_terms(company: dict[str, Any]) -> list[str]:
    parts: list[Any] = [company.get("name"), *company.get("aliases", []), *company.get("brands", []), *company.get("products", []), *company.get("executives", []), *company.get("subsidiaries", []), *company.get("customers", []), *company.get("suppliers", []), *company.get("partners", []), *company.get("competitors", []), *company.get("locations", [])]
    return unique_strings(parts, limit=40)


def company_search_terms(company: dict[str, Any]) -> list[str]:
    configured = company.get("search_terms") or []
    raw = configured if configured else company_entity_terms(company)
    return unique_strings(raw, limit=24)


def exclusion_matches(company: dict[str, Any], *values: str) -> bool:
    haystack = " ".join(clean_text(v) for v in values).casefold()
    return any(clean_text(term).casefold() in haystack for term in company.get("exclude_terms", []) if clean_text(term))


def matches_entity_terms(company: dict[str, Any], *values: str) -> bool:
    haystack = " ".join(clean_text(v) for v in values).casefold()
    for term in company_entity_terms(company):
        needle = term.casefold()
        if len(needle) < 3:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack):
            return True
    return False


def match_details(company: dict[str, Any], *values: str) -> dict[str, Any]:
    haystack = " ".join(clean_text(v) for v in values).casefold()
    details: list[str] = []
    for group_name, terms in (("company", [company["name"], *company.get("aliases", [])]), ("brand", company.get("brands", [])), ("product", company.get("products", [])), ("exec", company.get("executives", [])), ("customer", company.get("customers", [])), ("supplier", company.get("suppliers", [])), ("partner", company.get("partners", [])), ("competitor", company.get("competitors", [])), ("location", company.get("locations", []))):
        for term in unique_strings(list(terms)):
            if len(term) < 3:
                continue
            if re.search(rf"(?<![a-z0-9]){re.escape(term.casefold())}(?![a-z0-9])", haystack):
                details.append(f"{group_name}: {term}")
                break
    return {"matched": bool(details), "details": details}


def classify_section(*values: str, hint: str = "") -> tuple[str, float, list[str]]:
    haystack = " ".join(clean_text(v) for v in values).casefold()
    combined = f"{clean_text(hint).casefold()} {haystack}".strip()
    hits: list[tuple[str, int, list[str]]] = []
    for section, keywords in SECTION_KEYWORDS.items():
        section_hits = [kw for kw in keywords if kw in combined]
        if section_hits:
            hits.append((section, len(section_hits), section_hits[:4]))
    if not hits:
        return "company", 0.2, []
    hits.sort(key=lambda item: item[1], reverse=True)
    top_section, count, keywords = hits[0]
    confidence = min(0.95, 0.45 + 0.12 * count)
    if top_section == "market" and any(k in combined for k in SECTION_KEYWORDS["supply_chain"]):
        if count == 1:
            return "supply_chain", 0.58, keywords
    return top_section, confidence, keywords


def load_cache(path: Path) -> dict[str, Any]:
    data = load_json(path, {})
    return data if isinstance(data, dict) else {}


def save_cache(path: Path, data: dict[str, Any]) -> None:
    try:
        save_json(path, data)
    except OSError as exc:
        LOGGER.warning("Could not write cache %s: %s", path, exc)


def looks_like_feed(response: Response) -> bool:
    content_type = response.headers.get("content-type", "").casefold()
    head = response.content[:512].lstrip().lower()
    return ("xml" in content_type or head.startswith(b"<?xml") or head.startswith(b"<rss") or head.startswith(b"<feed"))


def probe_url(url: str) -> Response | None:
    try:
        RATE_LIMITER.wait(url)
        response = get_session().get(url, timeout=10, allow_redirects=True)
        if response.status_code == 200:
            return response
    except requests.RequestException:
        pass
    return None


def discover_sitemaps(base: str, domain: str) -> list[str]:
    sitemap_urls: list[str] = []
    probes = [urljoin(base, "/robots.txt"), urljoin(base, "/sitemap.xml"), urljoin(base, "/sitemap_index.xml")]
    for url in probes:
        response = probe_url(url)
        if response is None:
            continue
        if url.endswith("robots.txt"):
            for line in response.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    sitemap_urls.append(clean_text(line.split(":", 1)[1]))
        else:
            sitemap_urls.append(response.url)
    results: list[str] = []
    for url in unique_strings(sitemap_urls, limit=6):
        try:
            response = request_with_backoff(url, attempts=2, expected="xml", label=f"sitemap {domain}")
        except requests.RequestException:
            continue
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            continue
        locs = [clean_text(elem.text) for elem in root.iter() if elem.tag.endswith("loc")]
        if root.tag.endswith("sitemapindex"):
            for child in locs[:6]:
                if child:
                    results.append(child)
        else:
            for child in locs[:250]:
                if child and domain in urlsplit(child).netloc.lower():
                    results.append(child)
    return unique_strings(results, limit=60)


def discover_official_sources(company: dict[str, Any], discovery_cache: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    feeds = list(company.get("rss_feeds", []))
    pages = list(company.get("newsroom_urls", []))
    watch_pages = list(company.get("watch_pages", []))
    if company.get("blog_url"):
        pages.append(company["blog_url"])
        watch_pages.append(company["blog_url"])
    domain = clean_text(company.get("domain"))
    base = clean_text(company.get("website")) or (f"https://{domain}/" if domain else "")
    if not base:
        return unique_strings(feeds, MAX_FEEDS_PER_COMPANY), unique_strings(pages, MAX_NEWSROOM_PAGES_PER_COMPANY), unique_strings(watch_pages, MAX_WATCH_PAGES_PER_COMPANY)

    cache_key = domain or urlsplit(base).netloc
    entry = discovery_cache.get(cache_key)
    fresh = isinstance(entry, dict) and (checked := parse_datetime(entry.get("checked_at"))) is not None and checked >= cutoff(DISCOVERY_TTL_DAYS)

    if not fresh:
        found_feeds: list[str] = []
        found_pages: list[str] = []
        found_watch: list[str] = []
        for path in FEED_PROBE_PATHS:
            if len(found_feeds) >= MAX_FEEDS_PER_COMPANY:
                break
            response = probe_url(urljoin(base, path))
            if response is not None and looks_like_feed(response):
                found_feeds.append(response.url)
        for path in NEWSROOM_PROBE_PATHS:
            if len(found_pages) >= MAX_NEWSROOM_PAGES_PER_COMPANY:
                break
            response = probe_url(urljoin(base, path))
            if response is None:
                continue
            content_type = response.headers.get("content-type", "").casefold()
            final_path = urlsplit(response.url).path.casefold()
            if "html" in content_type and any(h in final_path for h in ["news", "blog", "press", "insights", "updates", "stories"]):
                found_pages.append(response.url)
        for path in WATCH_PAGE_PATHS:
            if len(found_watch) >= MAX_WATCH_PAGES_PER_COMPANY:
                break
            response = probe_url(urljoin(base, path))
            if response is not None and "html" in response.headers.get("content-type", "").casefold():
                found_watch.append(response.url)
        sitemap_urls = discover_sitemaps(base, domain)
        for candidate in sitemap_urls:
            path = urlsplit(candidate).path.casefold()
            if any(token in path for token, _ in PAGE_HINTS):
                found_watch.append(candidate)
        entry = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "rss_feeds": found_feeds,
            "newsroom_urls": found_pages,
            "watch_pages": found_watch,
        }
        discovery_cache[cache_key] = entry
        LOGGER.info("Discovery for %s: %s feed(s), %s newsroom page(s), %s watch page(s)", company["name"], len(found_feeds), len(found_pages), len(found_watch))

    if isinstance(entry, dict):
        feeds.extend(entry.get("rss_feeds", []))
        pages.extend(entry.get("newsroom_urls", []))
        watch_pages.extend(entry.get("watch_pages", []))
    watch_pages.extend(sitemap_urls if 'sitemap_urls' in locals() else [])
    return unique_strings(feeds, MAX_FEEDS_PER_COMPANY), unique_strings(pages, MAX_NEWSROOM_PAGES_PER_COMPANY), unique_strings(watch_pages, MAX_WATCH_PAGES_PER_COMPANY)


def make_candidate(
    *,
    company: dict[str, Any],
    title: str,
    url: str,
    source: str,
    published_at: str,
    discovered_via: str,
    feed_summary: str = "",
    verify_date_on_page: bool = False,
    fallback_window: bool = False,
    section_hint: str = "",
    source_type: str = "article",
    signal: str = "news",
) -> dict[str, Any] | None:
    title = clean_text(title)
    url = clean_text(url)
    source = clean_text(source) or urlsplit(url).netloc.replace("www.", "")
    published_at = clean_text(published_at)
    if not title or not url:
        return None
    if exclusion_matches(company, title, source, feed_summary):
        return None

    is_official = discovered_via in {"Official RSS", "Company blog", "Official company site", "Page diff"}
    window = BLOG_FALLBACK_DAYS if fallback_window else LOOKBACK_DAYS
    if verify_date_on_page:
        if published_at and not is_within(published_at, window):
            return None
    elif published_at and not is_within(published_at, window):
        return None

    inferred_section, confidence, section_keywords = classify_section(title, feed_summary, source, section_hint)
    entity_match = match_details(company, title, feed_summary, source, section_hint)
    return {
        "company": clean_text(company["name"]),
        "company_domain": clean_text(company.get("domain")),
        "title": title,
        "url": url,
        "source": source,
        "published_at": published_at,
        "feed_summary": clean_text(feed_summary),
        "discovered_via": discovered_via,
        "verify_date_on_page": verify_date_on_page,
        "section_hint": clean_text(section_hint),
        "section": inferred_section,
        "section_label": SECTION_LABELS.get(inferred_section, SECTION_LABELS["other"]),
        "section_confidence": round(confidence, 2),
        "section_keywords": section_keywords,
        "source_type": source_type,
        "signal": signal,
        "needs_grounding": not is_official,
        "title_match": matches_entity_terms(company, title, feed_summary),
        "entity_match": entity_match,
        "match_reasons": entity_match.get("details", []),
    }


def resolve_google_news_url(url: str) -> str:
    parts = urlsplit(url)
    if "news.google.com" not in parts.netloc.lower():
        return url
    match = re.search(r"/articles/([^/?#]+)", parts.path)
    if not match:
        return url
    token = match.group(1)
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, TypeError):
        return url
    for blob in re.findall(rb"https?://[\x21-\x7e]+", raw):
        candidate = blob.decode("ascii", errors="ignore")
        host = urlsplit(candidate).netloc.lower()
        if host and "google.com" not in host:
            return candidate
    return url


def parse_rss_feed(*, xml_content: bytes, company: dict[str, Any], discovered_via: str, default_source: str = "") -> list[dict[str, Any]]:
    root = ET.fromstring(xml_content)
    output: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        url = resolve_google_news_url(clean_text(item.findtext("link")))
        feed_summary = strip_html(item.findtext("description"))
        title = clean_text(item.findtext("title"))
        record = make_candidate(
            company=company,
            title=title,
            url=url,
            source=clean_text(item.findtext("source")) or default_source,
            published_at=clean_text(item.findtext("pubDate")),
            discovered_via=discovered_via,
            feed_summary=feed_summary,
            source_type="feed",
        )
        if record:
            output.append(record)
    for entry in root.findall(".//entry"):
        link = ""
        for link_tag in entry.findall(".//{*}link"):
            href = clean_text(link_tag.attrib.get("href"))
            if href:
                link = href
                break
        record = make_candidate(
            company=company,
            title=clean_text(entry.findtext("title")),
            url=resolve_google_news_url(link),
            source=default_source,
            published_at=clean_text(entry.findtext("published") or entry.findtext("updated")),
            discovered_via=discovered_via,
            feed_summary=strip_html(entry.findtext("summary") or entry.findtext("content")),
            source_type="feed",
        )
        if record:
            output.append(record)
    return output


def search_official_rss(company: dict[str, Any], feeds: list[str]) -> tuple[list[dict[str, Any]], int]:
    items: list[dict[str, Any]] = []
    successes = 0
    for feed in unique_strings(feeds, MAX_FEEDS_PER_COMPANY):
        try:
            response = request_with_backoff(feed, attempts=2, expected="xml", label=f"RSS {company['name']}")
            parsed = parse_rss_feed(xml_content=response.content, company=company, discovered_via="Official RSS", default_source=urlsplit(response.url).netloc.replace("www.", ""))
            items.extend(parsed)
            successes += 1
        except (requests.RequestException, ET.ParseError) as exc:
            LOGGER.warning("RSS parse failed for %s at %s: %s", company["name"], feed, exc)
    return items, successes


def same_site(url: str, domain: str) -> bool:
    host = urlsplit(url).netloc.lower()
    domain = domain.lower().lstrip("www.")
    return host == domain or host.endswith("." + domain)


def extract_article_links(soup: BeautifulSoup, base_url: str, domain: str, limit: int) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = clean_text(anchor.get("href"))
        text = clean_text(anchor.get_text(" "))
        if not href or not text:
            continue
        if len(text) < 8 or any(word in text.casefold() for word in SKIP_LINK_WORDS):
            continue
        resolved = urljoin(base_url, href)
        if not same_site(resolved, domain):
            continue
        path = urlsplit(resolved).path.casefold()
        if path in {"/", ""}:
            continue
        if not any(token in path for token, _ in PAGE_HINTS) and not re.search(r"\d{4}|news|blog|press|insight|article|update|story|case|career|job|leadership|pricing|customer|partner|supplier|investor|product", path):
            continue
        key = normalise_url(resolved)
        if key in seen:
            continue
        seen.add(key)
        links.append((text, resolved))
        if len(links) >= limit:
            break
    return links


def search_official_web_pages(company: dict[str, Any], pages: list[str]) -> tuple[list[dict[str, Any]], int]:
    output: list[dict[str, Any]] = []
    successes = 0
    domain = clean_text(company.get("domain"))
    for page_url in unique_strings(pages, MAX_NEWSROOM_PAGES_PER_COMPANY):
        try:
            response = request_with_backoff(page_url, attempts=2, label=f"page {company['name']}")
        except requests.RequestException as exc:
            LOGGER.warning("Official page unavailable for %s at %s: %s", company["name"], page_url, exc)
            continue
        successes += 1
        if "html" not in response.headers.get("content-type", "").casefold():
            continue
        soup = BeautifulSoup(response.text, "html.parser")
        for anchor_text, link in extract_article_links(soup, response.url, domain, MAX_BLOG_CANDIDATES):
            output.append(make_candidate(
                company=company,
                title=anchor_text,
                url=link,
                source=urlsplit(response.url).netloc.replace("www.", ""),
                published_at=extract_page_date(soup, response),
                discovered_via="Official company site",
                feed_summary=extract_meta_description(soup),
                verify_date_on_page=True,
                source_type="page",
            ) or {})
    return [item for item in output if item], successes


def fetch_watch_page_snapshot(url: str) -> dict[str, str]:
    empty = {"text": "", "page_date": "", "description": "", "title": "", "hash": "", "path": ""}
    try:
        response = request_with_backoff(url, attempts=2, timeout=20, label=f"watch {url}")
    except requests.RequestException as exc:
        LOGGER.warning("Watch page fetch failed for %s: %s", url, exc)
        return empty
    content_type = response.headers.get("content-type", "").casefold()
    if "html" not in content_type and "xhtml" not in content_type:
        return empty
    soup = BeautifulSoup(response.text, "html.parser")
    page_date = extract_page_date(soup, response)
    description = extract_meta_description(soup)
    title = clean_text((soup.title.get_text(" ") if soup.title else "") or response.url)
    for element in soup(["script", "style", "nav", "footer", "header", "form", "aside", "noscript", "svg"]):
        element.decompose()
    container = soup.find("article") or soup.find("main") or soup.body or soup
    paragraphs: list[str] = []
    seen: set[str] = set()
    total = 0
    for para in container.find_all(["p", "li", "h2", "h3"]):
        text = clean_text(para.get_text(" "))
        key = text.casefold()
        if len(text) < 30 or key in seen:
            continue
        seen.add(key)
        paragraphs.append(text)
        total += len(text)
        if total >= ARTICLE_TEXT_LIMIT:
            break
    text = " ".join(paragraphs)[:ARTICLE_TEXT_LIMIT]
    digest = hashlib.sha1((title + "\n" + text).encode("utf-8")).hexdigest()
    return {"text": text, "page_date": page_date, "description": description, "title": title, "hash": digest, "path": urlsplit(response.url).path}


def changed_watch_candidates(company: dict[str, Any], watch_pages: list[str], snapshot_cache: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for page_url in unique_strings(watch_pages, MAX_WATCH_PAGES_PER_COMPANY):
        snapshot = fetch_watch_page_snapshot(page_url)
        if not snapshot.get("hash"):
            continue
        key = normalise_url(page_url)
        previous = snapshot_cache.get(key) if isinstance(snapshot_cache.get(key), dict) else {}
        prev_hash = clean_text(previous.get("hash"))
        changed = prev_hash and prev_hash != snapshot["hash"]
        snapshot_cache[key] = {
            "checked_at": now.isoformat(),
            "hash": snapshot["hash"],
            "title": snapshot.get("title", ""),
            "path": snapshot.get("path", ""),
            "page_date": snapshot.get("page_date", ""),
        }
        if not changed:
            continue
        path = snapshot.get("path", "").casefold()
        section_hint = next((section for token, section in PAGE_HINTS if token in path), "company")
        candidate = make_candidate(
            company=company,
            title=snapshot.get("title") or page_url,
            url=page_url,
            source=urlsplit(page_url).netloc.replace("www.", ""),
            published_at=snapshot.get("page_date") or now.isoformat(),
            discovered_via="Page diff",
            feed_summary=snapshot.get("description", ""),
            verify_date_on_page=False,
            fallback_window=True,
            section_hint=section_hint,
            source_type="page",
            signal="page_change",
        )
        if candidate:
            candidate["change_detected"] = True
            results.append(candidate)
    return results


def search_gdelt(company: dict[str, Any], focus: str = "") -> tuple[list[dict[str, Any]], bool]:
    terms = company_search_terms(company)
    if focus in SECTION_KEYWORDS:
        terms = unique_strings([*terms, *SECTION_KEYWORDS[focus]], limit=32)
    query = " OR ".join(f'"{t}"' for t in terms)
    params = {
        "query": f"({query}) sourcelang:eng",
        "mode": "artlist", "format": "json",
        "maxrecords": str(min(max(ANALYZE_PER_COMPANY * 3, 20), 40)),
        "sort": "datedesc", "timespan": f"{LOOKBACK_DAYS}d",
    }
    try:
        response = request_with_backoff(GDELT_ENDPOINT, params=params, attempts=GDELT_MAX_ATTEMPTS, expected="json", label=f"GDELT {company['name']} {focus}".strip())
    except requests.RequestException as exc:
        LOGGER.error("GDELT unavailable for %s: %s", company["name"], exc)
        return [], False
    output: list[dict[str, Any]] = []
    for item in response.json().get("articles", []):
        title = clean_text(item.get("title", ""))
        url = clean_text(item.get("url", ""))
        summary = clean_text(item.get("summary", ""))
        section_hint = focus or classify_section(title, summary, item.get("domain", ""))[0]
        record = make_candidate(
            company=company,
            title=title,
            url=url,
            source=item.get("domain") or urlsplit(url).netloc,
            published_at=item.get("seendate", ""),
            discovered_via="GDELT",
            feed_summary=summary,
            section_hint=section_hint,
            source_type="article",
            signal="aggregated_news",
        )
        if record:
            output.append(record)
    return output, True


def search_google_news_rss(company: dict[str, Any], focus: str = "") -> tuple[list[dict[str, Any]], bool]:
    terms = company_search_terms(company)
    if focus in SECTION_KEYWORDS:
        terms = unique_strings([*terms, *SECTION_KEYWORDS[focus]], limit=32)
    query = f"({' OR '.join(chr(34) + t + chr(34) for t in terms)}) when:{LOOKBACK_DAYS}d"
    url = f"{GOOGLE_NEWS_RSS_ENDPOINT}?q={quote_plus(query)}&hl=en-GB&gl=GB&ceid=GB:en"
    try:
        response = request_with_backoff(url, attempts=2, expected="xml", label=f"Google News {company['name']} {focus}".strip())
    except requests.RequestException as exc:
        LOGGER.error("Google News RSS unavailable for %s: %s", company["name"], exc)
        return [], False
    try:
        items = parse_rss_feed(xml_content=response.content, company=company, discovered_via="Google News RSS", default_source="Google News")
        if focus:
            for item in items:
                section, confidence, keywords = classify_section(item.get("title", ""), item.get("feed_summary", ""), item.get("source", ""), hint=focus)
                item["section"] = section
                item["section_label"] = SECTION_LABELS.get(section, SECTION_LABELS["other"])
                item["section_confidence"] = max(item.get("section_confidence", 0), confidence)
                item["section_keywords"] = keywords
        return items, True
    except ET.ParseError as exc:
        LOGGER.error("Google News RSS unparseable for %s: %s", company["name"], exc)
        return [], False


def provider_priority(item: dict[str, Any]) -> int:
    return PROVIDER_PRIORITY.get(item.get("discovered_via", ""), 9)


def deduplicate_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(items, key=lambda i: (0 if i.get("title_match") else 1, provider_priority(i), -sortable_datetime(i.get("published_at")).timestamp()))
    kept: list[dict[str, Any]] = []
    kept_urls: set[str] = set()
    for item in ordered:
        url_key = normalise_url(item.get("url", ""))
        if url_key and url_key in kept_urls:
            continue
        if any(titles_similar(item.get("title", ""), k.get("title", "")) for k in kept):
            continue
        kept.append(item)
        if url_key:
            kept_urls.add(url_key)
    return kept


def collect_candidates(company: dict[str, Any], discovery_cache: dict[str, Any], snapshot_cache: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    combined: list[dict[str, Any]] = []
    successes = 0
    feeds, pages, watch_pages = discover_official_sources(company, discovery_cache)

    rss_items, rss_ok = search_official_rss(company, feeds)
    combined.extend(rss_items); successes += rss_ok
    page_items, page_ok = search_official_web_pages(company, pages)
    combined.extend(page_items); successes += page_ok
    changed_items = changed_watch_candidates(company, watch_pages, snapshot_cache)
    combined.extend(changed_items)

    for focus in ("", "market", "supply_chain", "customers_partners"):
        gdelt_items, gdelt_ok = search_gdelt(company, focus=focus)
        combined.extend(gdelt_items); successes += int(gdelt_ok)
        google_items, google_ok = search_google_news_rss(company, focus=focus)
        combined.extend(google_items); successes += int(google_ok)

    unique = deduplicate_candidates(combined)
    pool = unique[:max(ANALYZE_PER_COMPANY * 4, 24)]
    LOGGER.info("%s: %s raw -> %s unique (kept %s); providers ok: %s", company["name"], len(combined), len(unique), len(pool), successes)
    return pool, successes


def extract_meta_description(soup: BeautifulSoup) -> str:
    for attrs in ({"name": "description"}, {"property": "og:description"}, {"name": "twitter:description"}, {"property": "article:description"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            return clean_text(tag.get("content"))
    return ""


def extract_page_date(soup: BeautifulSoup, response: Response) -> str:
    for attrs in ({"property": "article:published_time"}, {"name": "article:published_time"}, {"property": "article:modified_time"}, {"name": "article:modified_time"}, {"property": "og:updated_time"}, {"name": "pubdate"}, {"name": "publishdate"}, {"name": "date"}, {"itemprop": "datePublished"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            return clean_text(tag.get("content"))
    time_tag = soup.find("time")
    if time_tag:
        return clean_text(time_tag.get("datetime") or time_tag.get_text(" "))
    return clean_text(response.headers.get("Last-Modified"))


def fetch_article_page(url: str) -> dict[str, str]:
    empty = {"text": "", "page_date": "", "description": ""}
    try:
        response = request_with_backoff(url, attempts=2, timeout=20, label=f"article {url}")
    except requests.RequestException as exc:
        LOGGER.warning("Article fetch failed for %s: %s", url, exc)
        return empty
    content_type = response.headers.get("content-type", "").casefold()
    if "html" not in content_type and "xhtml" not in content_type:
        return empty
    soup = BeautifulSoup(response.text, "html.parser")
    page_date = extract_page_date(soup, response)
    description = extract_meta_description(soup)
    for element in soup(["script", "style", "nav", "footer", "header", "form", "aside", "noscript", "svg"]):
        element.decompose()
    container = soup.find("article") or soup.find("main") or soup.body
    if container is None:
        return {"text": "", "page_date": page_date, "description": description}
    paragraphs: list[str] = []
    seen: set[str] = set()
    total = 0
    for para in container.find_all("p"):
        text = clean_text(para.get_text(" "))
        key = text.casefold()
        if len(text) < 40 or key in seen:
            continue
        seen.add(key)
        paragraphs.append(text)
        total += len(text)
        if total >= ARTICLE_TEXT_LIMIT:
            break
    return {"text": " ".join(paragraphs)[:ARTICLE_TEXT_LIMIT], "page_date": page_date, "description": description}


def strip_json_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def validate_analysis(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Gemini response was not a JSON object.")
    relevance = raw.get("is_relevant", False)
    if isinstance(relevance, str):
        relevance = relevance.strip().casefold() in {"true", "yes", "1"}
    relevance = bool(relevance)
    try:
        score = int(round(float(raw.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))
    drafts_raw = raw.get("drafts", {}) if isinstance(raw.get("drafts"), dict) else {}
    drafts = {
        "concise": clean_text(drafts_raw.get("concise")),
        "investor": clean_text(drafts_raw.get("investor")),
        "people": clean_text(drafts_raw.get("people")),
    }
    warnings = unique_strings(raw.get("warnings", []) if isinstance(raw.get("warnings"), list) else [raw.get("warnings")])
    verified = unique_strings(raw.get("verified_facts", []) if isinstance(raw.get("verified_facts"), list) else [raw.get("verified_facts")], limit=8)
    if relevance and not any(drafts.values()):
        warnings.append("The model did not return usable draft text.")
    section = clean_text(raw.get("section")).casefold().strip()
    if section not in SECTION_ORDER:
        section = "company"
    section_label = clean_text(raw.get("section_label")) or SECTION_LABELS.get(section, SECTION_LABELS["company"])
    why = clean_text(raw.get("why_it_matters"))
    return {
        "is_relevant": relevance,
        "score": score,
        "story_type": clean_text(raw.get("story_type")) or "Update",
        "section": section,
        "section_label": section_label,
        "summary": clean_text(raw.get("summary")),
        "why_it_matters": why,
        "drafts": drafts,
        "warnings": warnings,
        "verified_facts": verified,
        "headline": clean_text(raw.get("headline")),
        "confidence": clean_text(raw.get("confidence")) or ("high" if score >= READY_SCORE else "medium" if score >= MIN_SCORE else "low"),
        "source_type": clean_text(raw.get("source_type")),
        "signal": clean_text(raw.get("signal")),
        "match_reasons": unique_strings(raw.get("match_reasons", []) if isinstance(raw.get("match_reasons"), list) else []),
    }


def build_prompt(company: dict[str, Any], article: dict[str, Any], article_text: str, blog_fallback: bool) -> str:
    entity_terms = company_entity_terms(company)
    return f"""
You are analysing a portfolio-company signal for Next Wave Partners.

Return valid JSON only, with keys:
- is_relevant (boolean)
- score (0-100)
- story_type (one of: Update, Market move, Supply chain, Customer / partnership, Leadership, Regulatory, Funding / M&A, Other)
- section (one of: company, market, supply_chain, customers_partners, leadership, regulatory, funding_mna, other)
- section_label (human readable)
- headline (short neutral headline)
- summary (2-4 sentences)
- why_it_matters (1-3 sentences, portfolio lens)
- confidence (low, medium, high)
- source_type
- signal
- match_reasons (array of strings)
- verified_facts (array of short facts grounded in the article)
- warnings (array of caveats)
- drafts: object with concise, investor, people drafts

Company:
- name: {company['name']}
- description: {company.get('description','')}
- domain: {company.get('domain','')}
- entity terms: {', '.join(entity_terms[:35])}

Story context:
- title: {article.get('title','')}
- url: {article.get('url','')}
- source: {article.get('source','')}
- published_at: {article.get('published_at','')}
- discovered_via: {article.get('discovered_via','')}
- section_hint: {article.get('section_hint','')}
- section keywords: {', '.join(article.get('section_keywords', []) or [])}
- match reasons from rules: {', '.join(article.get('match_reasons', []) or [])}
- source_type: {article.get('source_type','')}
- signal: {article.get('signal','')}
- blog_fallback: {str(blog_fallback).lower()}

Article text:
{article_text[:ARTICLE_TEXT_LIMIT]}

Rules:
1. Be strict: if it does not materially matter to the company or portfolio, mark is_relevant false.
2. Prioritize company news, then market, then supply chain, then customers/partners, leadership, regulatory, funding/M&A.
3. If the item is only a weak market mention or a generic aggregator duplicate, lower the score.
4. The concise draft should be plain-English and suitable for a dashboard card.
5. The investor draft should focus on strategic and financial implications.
6. The people draft should only emphasize leadership / talent / team implications when relevant.
7. Keep factual claims grounded in the provided text; do not invent details.
8. If blog_fallback is true, the story should almost always be relevant if the page is genuinely new or updated.
""".strip()


def _respect_gemini_interval() -> None:
    global _LAST_GEMINI_CALL
    now = time.monotonic()
    wait = GEMINI_MIN_INTERVAL_SECONDS - (now - _LAST_GEMINI_CALL)
    if wait > 0:
        time.sleep(wait)
    _LAST_GEMINI_CALL = time.monotonic()


_LAST_GEMINI_CALL = 0.0


def analyse_and_draft(company: dict[str, Any], article: dict[str, Any], article_text: str, blog_fallback: bool = False) -> dict[str, Any]:
    if GEMINI_CLIENT is None:
        raise RuntimeError("Gemini client is not initialised.")
    prompt = build_prompt(company, article, article_text, blog_fallback)
    last_exc: Exception | None = None
    for attempt in range(1, GEMINI_MAX_ATTEMPTS + 1):
        try:
            _respect_gemini_interval()
            response = GEMINI_CLIENT.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    response_mime_type="application/json",
                    max_output_tokens=1400,
                ),
            )
            text = strip_json_fences(getattr(response, "text", "") or "")
            payload = json.loads(text)
            return validate_analysis(payload)
        except Exception as exc:
            last_exc = exc
            LOGGER.warning("Gemini attempt %s failed for %s: %s", attempt, article.get("url", ""), exc)
            if attempt < GEMINI_MAX_ATTEMPTS:
                time.sleep(min(15.0, 1.5 * attempt * attempt))
    assert last_exc is not None
    raise last_exc


def assemble_story(item: dict[str, Any], analysis: dict[str, Any], force_review: bool = False) -> dict[str, Any]:
    title = clean_text(analysis.get("headline")) or item["title"]
    summary = clean_text(analysis.get("summary")) or item.get("feed_summary") or ""
    story = {
        "id": story_id(item["url"], title or item["title"]),
        "company": item["company"],
        "company_domain": item.get("company_domain", ""),
        "title": title,
        "source": item.get("source", ""),
        "source_type": item.get("source_type", "article"),
        "signal": item.get("signal", "news"),
        "url": item["url"],
        "published_at": iso_or_original(item.get("published_at", "")),
        "discovered_via": item.get("discovered_via", ""),
        "section": analysis.get("section") or item.get("section", "company"),
        "section_label": analysis.get("section_label") or item.get("section_label") or SECTION_LABELS.get(item.get("section", "company"), SECTION_LABELS["company"]),
        "section_confidence": item.get("section_confidence", 0),
        "score": int(analysis.get("score", 0)),
        "summary": summary,
        "why_it_matters": clean_text(analysis.get("why_it_matters")),
        "story_type": clean_text(analysis.get("story_type")) or "Update",
        "confidence": clean_text(analysis.get("confidence")) or "medium",
        "verified_facts": analysis.get("verified_facts", []),
        "warnings": analysis.get("warnings", []),
        "match_reasons": unique_strings([*item.get("match_reasons", []), *analysis.get("match_reasons", [])], limit=12),
        "section_keywords": item.get("section_keywords", []),
        "section_hint": item.get("section_hint", ""),
        "entity_match": item.get("entity_match", {}),
        "title_match": item.get("title_match", False),
        "drafts": analysis.get("drafts", {}),
        "draft_status": "review" if force_review or int(analysis.get("score", 0)) < READY_SCORE else "ready",
        "needs_human_review": force_review or int(analysis.get("score", 0)) < READY_SCORE,
        "blog_fallback": force_review,
    }
    return story


def fetch_latest_blog_post(company: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    urls = unique_strings([company.get("blog_url", ""), *company.get("newsroom_urls", []), *company.get("watch_pages", [])], limit=8)
    for url in urls:
        try:
            response = request_with_backoff(url, attempts=2, label=f"blog {company['name']}")
        except requests.RequestException:
            continue
        if "html" not in response.headers.get("content-type", "").casefold():
            continue
        soup = BeautifulSoup(response.text, "html.parser")
        for anchor_text, link in extract_article_links(soup, response.url, clean_text(company.get("domain")), MAX_BLOG_CANDIDATES):
            candidate = make_candidate(
                company=company,
                title=anchor_text,
                url=link,
                source=urlsplit(response.url).netloc.replace("www.", ""),
                published_at=extract_page_date(soup, response),
                discovered_via="Company blog",
                feed_summary=extract_meta_description(soup),
                verify_date_on_page=True,
                fallback_window=True,
                source_type="page",
                signal="blog",
            )
            if candidate:
                page = fetch_article_page(candidate["url"])
                return candidate, page["text"] or candidate.get("feed_summary", "")
    return None, ""


def load_previous_stories() -> list[dict[str, Any]]:
    data = load_json(OUTPUT_FILE, {})
    stories = data.get("stories", []) if isinstance(data, dict) else []
    return [story for story in stories if isinstance(story, dict) and story.get("id")]


def carry_forward_stories(previous: list[dict[str, Any]], companies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    company_names = {c["name"] for c in companies}
    carried: list[dict[str, Any]] = []
    for story in previous:
        if clean_text(story.get("company")) not in company_names:
            continue
        published = parse_datetime(story.get("published_at"))
        if published is not None and published < cutoff(LOOKBACK_DAYS):
            continue
        carried.append(story)
    return carried


def deduplicate_stories(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(stories, key=lambda s: (int(s.get("score", 0)), sortable_datetime(str(s.get("published_at", "")))), reverse=True)
    kept: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for story in ordered:
        url_key = normalise_url(story.get("url", ""))
        if url_key and url_key in seen_urls:
            continue
        if any(story.get("company") == existing.get("company") and titles_similar(story.get("title", ""), existing.get("title", "")) for existing in kept):
            continue
        kept.append(story)
        if url_key:
            seen_urls.add(url_key)
    return kept


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def section_bucket_order(sections: dict[str, Any] | None = None) -> list[str]:
    if sections and isinstance(sections.get("section_order"), list):
        return [s for s in sections["section_order"] if isinstance(s, str)] or SECTION_ORDER
    return SECTION_ORDER


def main() -> None:
    deadline = time.monotonic() + RUN_BUDGET_SECONDS
    companies = load_companies()
    sections = load_sections()
    discovery_cache = load_cache(DISCOVERY_CACHE_FILE)
    seen_cache = load_cache(SEEN_CACHE_FILE)
    snapshot_cache = load_cache(SNAPSHOT_CACHE_FILE)
    seen_cutoff = cutoff(SEEN_TTL_DAYS)
    snapshot_cutoff = cutoff(SNAPSHOT_TTL_DAYS)
    now_iso = datetime.now(timezone.utc).isoformat()

    candidates_by_company: dict[str, list[dict[str, Any]]] = {}
    provider_successes = 0
    total_candidates = 0
    with ThreadPoolExecutor(max_workers=min(COLLECTION_WORKERS, max(1, len(companies)))) as pool:
        futures = {pool.submit(collect_candidates, c, discovery_cache, snapshot_cache): c for c in companies}
        for future in as_completed(futures):
            company = futures[future]
            try:
                shortlist, successes = future.result()
            except Exception as exc:
                LOGGER.error("Collection failed for %s: %s", company["name"], exc)
                shortlist, successes = [], 0
            candidates_by_company[company["name"]] = shortlist
            provider_successes += successes
            total_candidates += len(shortlist)
    save_cache(DISCOVERY_CACHE_FILE, discovery_cache)
    save_cache(SNAPSHOT_CACHE_FILE, snapshot_cache)

    if provider_successes == 0:
        raise UpstreamUnavailableError("Every news provider failed. Existing news.json was left unchanged.")

    previous = load_previous_stories()
    all_stories = carry_forward_stories(previous, companies)
    reused_count = len(all_stories)

    seen_story_urls = {normalise_url(str(s.get("url", ""))) for s in all_stories}
    seen_story_urls.discard("")
    carried_titles_by_company: dict[str, list[str]] = defaultdict(list)
    for story in all_stories:
        carried_titles_by_company[clean_text(story.get("company"))].append(str(story.get("title", "")))

    to_process: list[tuple[dict[str, Any], dict[str, Any]]] = []
    skipped_recent = 0
    for company in companies:
        taken = 0
        carried_titles = carried_titles_by_company.get(company["name"], [])
        for item in candidates_by_company.get(company["name"], []):
            if taken >= ANALYZE_PER_COMPANY:
                break
            url_key = normalise_url(item["url"])
            if url_key and url_key in seen_story_urls:
                continue
            if any(titles_similar(item["title"], t) for t in carried_titles):
                continue
            record = seen_cache.get(url_key)
            if isinstance(record, dict):
                decided = parse_datetime(record.get("t"))
                if decided is not None and decided >= seen_cutoff:
                    skipped_recent += 1
                    continue
            if url_key:
                seen_story_urls.add(url_key)
            carried_titles.append(item["title"])
            to_process.append((company, item))
            taken += 1
    LOGGER.info("Carried %s stories; %s new candidates to analyse; %s skipped recently", reused_count, len(to_process), skipped_recent)

    pages: dict[int, dict[str, str]] = {}
    if to_process:
        with ThreadPoolExecutor(max_workers=min(COLLECTION_WORKERS, len(to_process))) as pool:
            fmap = {pool.submit(fetch_article_page, item["url"]): i for i, (_, item) in enumerate(to_process)}
            for future in as_completed(fmap):
                i = fmap[future]
                try:
                    pages[i] = future.result()
                except Exception as exc:
                    LOGGER.warning("Article prefetch failed: %s", exc)
                    pages[i] = {"text": "", "page_date": "", "description": ""}

    gemini_attempts = gemini_successes = grounding_drops = 0
    for i, (company, item) in enumerate(to_process):
        if time.monotonic() > deadline:
            LOGGER.warning("Run budget exhausted; stopping before remaining candidates.")
            break
        page = pages.get(i, {"text": "", "page_date": "", "description": ""})
        if item.get("verify_date_on_page"):
            page_date = page["page_date"]
            if not page_date or not is_within(page_date, LOOKBACK_DAYS):
                LOGGER.info("Skipping undated/out-of-window page: %s", item["url"])
                continue
            item["published_at"] = page_date
        if not item.get("feed_summary") and page.get("description"):
            item["feed_summary"] = page["description"]
        if item.get("needs_grounding") and not matches_entity_terms(company, item["title"], page["text"], item.get("feed_summary", "")):
            grounding_drops += 1
            url_key = normalise_url(item["url"])
            if url_key:
                seen_cache[url_key] = {"t": now_iso, "kept": False}
            LOGGER.info("Grounding drop: %s", item["url"])
            continue
        LOGGER.info("Analysing: %s | %s | %s", item["title"], item["source"], item["url"])
        gemini_attempts += 1
        try:
            analysis = analyse_and_draft(company, item, page["text"])
            gemini_successes += 1
        except Exception as exc:
            LOGGER.error("Drafting failed for %s: %s", item["url"], exc)
            continue
        url_key = normalise_url(item["url"])
        kept = analysis["is_relevant"] and analysis["score"] >= MIN_SCORE
        if url_key:
            seen_cache[url_key] = {"t": now_iso, "kept": kept}
        if kept:
            all_stories.append(assemble_story(item, analysis))
        else:
            LOGGER.info("Rejected below threshold: %s", item["title"])

    companies_with_stories = {clean_text(s.get("company")) for s in all_stories}
    fallback_added = 0
    for company in companies:
        if time.monotonic() > deadline:
            LOGGER.warning("Run budget exhausted; skipping remaining fallback work.")
            break
        if company["name"] in companies_with_stories or not company.get("blog_url"):
            continue
        LOGGER.info("Blog fallback for %s (%s)", company["name"], company["blog_url"])
        candidate, text = fetch_latest_blog_post(company)
        if candidate is None:
            continue
        gemini_attempts += 1
        try:
            analysis = analyse_and_draft(company, candidate, text, blog_fallback=True)
            gemini_successes += 1
        except Exception as exc:
            LOGGER.error("Blog fallback drafting failed for %s: %s", candidate["url"], exc)
            continue
        if not analysis.get("summary"):
            analysis["summary"] = candidate.get("feed_summary") or "Latest update from the company site."
        all_stories.append(assemble_story(candidate, analysis, force_review=True))
        companies_with_stories.add(company["name"])
        fallback_added += 1

    seen_cache = {
        k: v for k, v in seen_cache.items()
        if isinstance(v, dict) and (p := parse_datetime(v.get("t"))) is not None and p >= seen_cutoff
    }
    snapshot_cache = {
        k: v for k, v in snapshot_cache.items()
        if isinstance(v, dict) and (p := parse_datetime(v.get("checked_at"))) is not None and p >= snapshot_cutoff
    }
    save_cache(SEEN_CACHE_FILE, seen_cache)
    save_cache(SNAPSHOT_CACHE_FILE, snapshot_cache)

    if total_candidates > 0 and gemini_attempts > 0 and gemini_successes == 0 and reused_count == 0:
        raise UpstreamUnavailableError("Candidates were found, but every Gemini request failed. Existing news.json was left unchanged.")

    all_stories = deduplicate_stories(all_stories)
    by_company: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for story in all_stories:
        by_company[clean_text(story.get("company"))].append(story)

    final_stories: list[dict[str, Any]] = []
    for company in companies:
        stories = by_company.get(company["name"], [])
        stories.sort(key=lambda s: (int(s.get("score", 0)), sortable_datetime(str(s.get("published_at", "")))), reverse=True)
        final_stories.extend(stories[:MAX_PER_COMPANY])
    final_stories.sort(key=lambda s: (int(s.get("score", 0)), sortable_datetime(str(s.get("published_at", "")))), reverse=True)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": LOOKBACK_DAYS,
        "section_order": section_bucket_order(sections),
        "section_labels": sections.get("section_labels", SECTION_LABELS),
        "story_count": len(final_stories),
        "stories": final_stories,
        "run_summary": {
            "companies_checked": len(companies),
            "companies_with_coverage": len({clean_text(s.get("company")) for s in final_stories}),
            "providers_succeeded": provider_successes,
            "candidates_found": total_candidates,
            "gemini_requests_attempted": gemini_attempts,
            "gemini_requests_succeeded": gemini_successes,
            "grounding_drops": grounding_drops,
            "blog_fallbacks_added": fallback_added,
            "stories_carried_forward": reused_count,
            "candidates_skipped_recently_evaluated": skipped_recent,
            "minimum_score": MIN_SCORE,
        },
    }
    atomic_write_json(OUTPUT_FILE, output)
    LOGGER.info("Wrote %s stories across %s companies to %s", len(final_stories), output["run_summary"]["companies_with_coverage"], OUTPUT_FILE)


if __name__ == "__main__":
    api_key = clean_text(os.getenv("GEMINI_API_KEY"))
    if not api_key:
        LOGGER.error("GEMINI_API_KEY is not set.")
        sys.exit(1)
    GEMINI_CLIENT = genai.Client(api_key=api_key)
    try:
        main()
    except Exception:
        LOGGER.exception("Portfolio-news refresh failed.")
        sys.exit(1)
