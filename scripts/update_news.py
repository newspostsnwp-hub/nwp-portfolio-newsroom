"""Collect recent public news about Next Wave Partners portfolio companies.

Pipeline per run:
  1. Load companies from config/companies.json.
  2. Discover official RSS feeds and newsroom pages per company (cached).
  3. Collect candidates in parallel from: official RSS, official newsroom
     pages, GDELT, and Google News RSS.
  4. Gate candidates lexically against company aliases, enforce the lookback
     window, and deduplicate aggressively (URL + normalised title, with
     official sources preferred).
  5. Carry forward still-valid stories from the previous news.json and skip
     URLs that were already evaluated recently, so Gemini is only called for
     genuinely new candidates.
  6. Fetch each shortlisted article once (text + on-page date), verify dates
     for scraped items, then analyse and draft with Gemini.
  7. Write site/data/news.json atomically in the existing schema.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import os
import random
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
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

# --------------------------------------------------------------------------
# Paths and endpoints
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
COMPANIES_FILE = ROOT / "config" / "companies.json"
OUTPUT_FILE = ROOT / "site" / "data" / "news.json"
CACHE_DIR = ROOT / ".cache"
DISCOVERY_CACHE_FILE = CACHE_DIR / "discovery.json"
SEEN_CACHE_FILE = CACHE_DIR / "seen.json"

GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
GOOGLE_NEWS_RSS_ENDPOINT = "https://news.google.com/rss/search"

USER_AGENT = (
    "Mozilla/5.0 (compatible; NextWavePortfolioNewsroom/2.0; "
    "+https://nextwavepartners.co.uk/)"
)

# --------------------------------------------------------------------------
# Tunables (all overridable via environment variables)
# --------------------------------------------------------------------------

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
LOOKBACK_DAYS = max(1, int(os.getenv("LOOKBACK_DAYS", "7")))
MAX_PER_COMPANY = max(1, int(os.getenv("MAX_PER_COMPANY", "4")))
MIN_SCORE = max(0, min(100, int(os.getenv("MIN_SCORE", "60"))))
READY_SCORE = max(0, min(100, int(os.getenv("READY_SCORE", "80"))))
ARTICLE_TEXT_LIMIT = max(2000, int(os.getenv("ARTICLE_TEXT_LIMIT", "9000")))
REQUEST_TIMEOUT_SECONDS = max(5, int(os.getenv("REQUEST_TIMEOUT_SECONDS", "25")))
GDELT_MAX_ATTEMPTS = max(1, int(os.getenv("GDELT_MAX_ATTEMPTS", "3")))
GEMINI_MAX_ATTEMPTS = max(1, int(os.getenv("GEMINI_MAX_ATTEMPTS", "4")))
GEMINI_MIN_INTERVAL_SECONDS = max(0.0, float(os.getenv("GEMINI_DELAY_SECONDS", "1.5")))
COLLECTION_WORKERS = max(1, int(os.getenv("COLLECTION_WORKERS", "6")))
RUN_BUDGET_SECONDS = max(60, int(os.getenv("RUN_BUDGET_SECONDS", "1020")))
DISCOVERY_TTL_DAYS = max(1, int(os.getenv("DISCOVERY_TTL_DAYS", "7")))
SEEN_TTL_DAYS = max(1, int(os.getenv("SEEN_TTL_DAYS", str(LOOKBACK_DAYS))))

MAX_FEEDS_PER_COMPANY = 3
MAX_NEWSROOM_PAGES_PER_COMPANY = 3
MAX_LINKS_PER_NEWSROOM_PAGE = 10

# Minimum polite spacing between requests to the same host, in seconds.
HOST_MIN_INTERVALS = {
    "api.gdeltproject.org": 5.0,
    "news.google.com": 2.0,
}
DEFAULT_HOST_INTERVAL = 1.0

PROVIDER_PRIORITY = {
    "Official RSS": 0,
    "Official company site": 1,
    "GDELT": 2,
    "Google News RSS": 3,
}

OFFICIAL_PAGE_HINTS = (
    "news",
    "newsroom",
    "blog",
    "press",
    "press-release",
    "press-releases",
    "updates",
    "stories",
    "insights",
    "articles",
    "media",
)

FEED_PROBE_PATHS = (
    "/feed/",
    "/rss.xml",
    "/atom.xml",
    "/blog/feed/",
    "/news/feed/",
    "/feed.xml",
)

NEWSROOM_PROBE_PATHS = (
    "/news/",
    "/newsroom/",
    "/blog/",
    "/press/",
    "/media-centre/",
    "/insights/",
)

SKIP_LINK_WORDS = (
    "contact",
    "privacy",
    "cookie",
    "terms",
    "login",
    "sign in",
    "careers",
    "jobs",
    "about",
    "team",
    "people",
    "investors",
    "support",
    "faq",
    "subscribe",
    "newsletter",
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "application/json;q=0.8,*/*;q=0.7"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOGGER = logging.getLogger("nwp-news")

GEMINI_CLIENT: genai.Client | None = None


class UpstreamUnavailableError(RuntimeError):
    """Raised when every upstream source is unavailable."""


# --------------------------------------------------------------------------
# HTTP layer: thread-local sessions, per-host politeness, retries
# --------------------------------------------------------------------------

_THREAD_LOCAL = threading.local()


def get_session() -> requests.Session:
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
        _THREAD_LOCAL.session = session
    return session


class HostRateLimiter:
    """Serialises requests per host with a minimum interval between them."""

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
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def request_with_backoff(
    url: str,
    *,
    params: dict[str, str] | None = None,
    attempts: int = 3,
    timeout: int | None = None,
    expected: str = "text",
    label: str = "request",
) -> Response:
    timeout = timeout or REQUEST_TIMEOUT_SECONDS
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        RATE_LIMITER.wait(url)
        try:
            response = get_session().get(
                url,
                params=params,
                timeout=timeout,
                allow_redirects=True,
            )

            if response.status_code == 429:
                retry_after = parse_retry_after(response)
                wait_seconds = (
                    retry_after
                    if retry_after is not None
                    else min(8 * (2 ** (attempt - 1)), 45)
                ) + random.uniform(0.5, 2.0)

                if attempt == attempts:
                    response.raise_for_status()

                LOGGER.warning(
                    "%s rate-limited on attempt %s/%s; waiting %.1fs",
                    label, attempt, attempts, wait_seconds,
                )
                time.sleep(wait_seconds)
                continue

            if response.status_code in {500, 502, 503, 504}:
                if attempt == attempts:
                    response.raise_for_status()
                wait_seconds = min(4 * (2 ** (attempt - 1)), 30) + random.uniform(0.5, 2.0)
                LOGGER.warning(
                    "%s returned HTTP %s on attempt %s/%s; waiting %.1fs",
                    label, response.status_code, attempt, attempts, wait_seconds,
                )
                time.sleep(wait_seconds)
                continue

            response.raise_for_status()

            if expected == "json":
                response.json()
            elif expected == "xml":
                ET.fromstring(response.content)

            return response

        except (requests.RequestException, json.JSONDecodeError, ET.ParseError) as exc:
            last_error = exc
            if attempt == attempts:
                break
            wait_seconds = min(3 * (2 ** (attempt - 1)), 20) + random.uniform(0.5, 2.0)
            LOGGER.warning(
                "%s failed on attempt %s/%s: %s; waiting %.1fs",
                label, attempt, attempts, exc, wait_seconds,
            )
            time.sleep(wait_seconds)

    raise requests.RequestException(f"{label} failed after {attempts} attempts: {last_error}")


# --------------------------------------------------------------------------
# Text, URL, and date helpers
# --------------------------------------------------------------------------

def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", html.unescape(str(value))).strip()


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    return clean_text(soup.get_text(" "))


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
    url = clean_text(url)
    if not url:
        return ""

    parts = urlsplit(url)
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower().replace(":80", "").replace(":443", "")
    path = re.sub(r"/+$", "", parts.path) or "/"

    ignored_prefixes = ("utm_", "fbclid", "gclid", "mc_")
    kept_query_parts: list[str] = []
    for piece in parts.query.split("&"):
        if not piece:
            continue
        key = piece.split("=", 1)[0].casefold()
        if key.startswith(ignored_prefixes):
            continue
        kept_query_parts.append(piece)

    return urlunsplit((scheme, netloc, path, "&".join(kept_query_parts), ""))


def story_id(url: str, title: str = "") -> str:
    source = normalise_url(url) or clean_text(title).casefold()
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean_text(title).casefold()).strip()


def parse_datetime(value: str | None) -> datetime | None:
    text = clean_text(value)
    if not text:
        return None

    for candidate_text in (text, text.replace("Z", "+00:00"), text.replace("/", "-")):
        try:
            parsed = datetime.fromisoformat(candidate_text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass

    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y%m%dT%H%M%SZ",
        "%Y%m%dT%H%M%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass

    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def lookback_cutoff() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)


def is_within_lookback(date_text: str) -> bool:
    parsed = parse_datetime(date_text)
    return parsed is not None and parsed >= lookback_cutoff()


def iso_or_original(date_text: str) -> str:
    parsed = parse_datetime(date_text)
    return parsed.isoformat() if parsed else clean_text(date_text)


def sortable_datetime(date_text: str | None) -> datetime:
    return parse_datetime(date_text) or datetime.min.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Companies
# --------------------------------------------------------------------------

def load_companies() -> list[dict[str, Any]]:
    if not COMPANIES_FILE.exists():
        raise FileNotFoundError(f"Missing file: {COMPANIES_FILE}")

    data = json.loads(COMPANIES_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("companies.json must contain a non-empty JSON list.")

    companies: list[dict[str, Any]] = []
    for index, raw in enumerate(data, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"Company entry {index} must be a JSON object.")

        name = clean_text(raw.get("name"))
        if not name:
            raise ValueError(f"Company entry {index} has no name.")

        for field_name in ("aliases", "exclude_terms", "rss_feeds", "newsroom_urls", "search_terms"):
            value = raw.get(field_name, [])
            if not isinstance(value, list):
                raise ValueError(f"{name}: {field_name} must be a JSON list.")

        company = dict(raw)
        company["name"] = name
        company["description"] = clean_text(raw.get("description"))
        company["aliases"] = unique_strings(raw.get("aliases", []))
        company["exclude_terms"] = unique_strings(raw.get("exclude_terms", []))
        company["rss_feeds"] = unique_strings(raw.get("rss_feeds", []))
        company["newsroom_urls"] = unique_strings(raw.get("newsroom_urls", []))
        company["search_terms"] = unique_strings(raw.get("search_terms", []))
        companies.append(company)

    return companies


def company_search_terms(company: dict[str, Any]) -> list[str]:
    configured = company.get("search_terms") or []
    raw_terms = configured if configured else [company["name"], *company.get("aliases", [])]
    return unique_strings(raw_terms, limit=5)


def exclusion_matches(company: dict[str, Any], *values: str) -> bool:
    haystack = " ".join(clean_text(value) for value in values).casefold()
    return any(
        clean_text(term).casefold() in haystack
        for term in company.get("exclude_terms", [])
        if clean_text(term)
    )


def matches_company(company: dict[str, Any], *values: str) -> bool:
    """Cheap lexical gate: the text must mention the company name or an alias."""
    haystack = " ".join(clean_text(value) for value in values).casefold()
    for term in (company["name"], *company.get("aliases", [])):
        needle = clean_text(term).casefold()
        if len(needle) < 3:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack):
            return True
    return False


# --------------------------------------------------------------------------
# Cache files (discovery of official sources; recently evaluated URLs)
# --------------------------------------------------------------------------

def load_json_cache(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_json_cache(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        LOGGER.warning("Could not write cache %s: %s", path, exc)


def looks_like_feed(response: Response) -> bool:
    content_type = response.headers.get("content-type", "").casefold()
    head = response.content[:512].lstrip().lower()
    return (
        "xml" in content_type
        or head.startswith(b"<?xml")
        or head.startswith(b"<rss")
        or head.startswith(b"<feed")
    )


def probe_url(url: str) -> Response | None:
    try:
        RATE_LIMITER.wait(url)
        response = get_session().get(url, timeout=10, allow_redirects=True)
        if response.status_code == 200:
            return response
    except requests.RequestException:
        pass
    return None


def discover_official_sources(
    company: dict[str, Any],
    discovery_cache: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Return (rss_feeds, newsroom_urls), combining config with cached probes.

    Config entries always win. Conventional paths on the company's own domain
    are probed at most once every DISCOVERY_TTL_DAYS.
    """
    feeds = list(company.get("rss_feeds", []))
    pages = list(company.get("newsroom_urls", []))
    domain = clean_text(company.get("domain"))
    base = clean_text(company.get("website")) or (f"https://{domain}/" if domain else "")

    if not base:
        return unique_strings(feeds, MAX_FEEDS_PER_COMPANY), unique_strings(pages, MAX_NEWSROOM_PAGES_PER_COMPANY)

    cache_key = domain or urlsplit(base).netloc
    entry = discovery_cache.get(cache_key)
    fresh = False
    if isinstance(entry, dict):
        checked = parse_datetime(entry.get("checked_at"))
        fresh = checked is not None and checked >= datetime.now(timezone.utc) - timedelta(days=DISCOVERY_TTL_DAYS)

    if not fresh:
        found_feeds: list[str] = []
        found_pages: list[str] = []

        for path in FEED_PROBE_PATHS:
            if len(found_feeds) >=
