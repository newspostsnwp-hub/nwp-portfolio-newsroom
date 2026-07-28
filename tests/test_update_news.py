"""Offline unit tests for scripts/update_news.py's pure functions.

No network/API calls anywhere in this file. Run with:
    python -m pytest tests/ -q
Requires pytest.ini's `pythonpath = scripts` so `import update_news` resolves
without turning scripts/ into a package.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from bs4 import BeautifulSoup

import update_news as u


# --------------------------------------------------------------- parse_datetime

class TestParseDatetime:
    def test_rfc822_with_named_zone(self):
        d = u.parse_datetime("Wed, 02 Oct 2024 15:00:00 GMT")
        assert d is not None
        assert (d.year, d.month, d.day, d.hour) == (2024, 10, 2, 15)

    def test_rfc822_with_numeric_offset(self):
        d = u.parse_datetime("Wed, 02 Oct 2024 15:00:00 +0000")
        assert d.hour == 15

    def test_iso_with_z_suffix(self):
        d = u.parse_datetime("2024-10-02T15:00:00Z")
        assert d.hour == 15
        assert d.utcoffset().total_seconds() == 0

    def test_iso_with_offset_converts_to_utc(self):
        d = u.parse_datetime("2024-10-02T15:00:00+01:00")
        assert d.hour == 14  # normalised to UTC

    def test_date_only(self):
        d = u.parse_datetime("2024-10-02")
        assert (d.year, d.month, d.day, d.hour) == (2024, 10, 2, 0)

    def test_long_month_name(self):
        d = u.parse_datetime("02 October 2024")
        assert (d.day, d.month) == (2, 10)

    def test_us_style_month_name(self):
        d = u.parse_datetime("October 02, 2024")
        assert (d.day, d.month) == (2, 10)

    def test_compact_gdelt_style(self):
        d = u.parse_datetime("20241002T150000Z")
        assert d.hour == 15

    def test_garbage_returns_none(self):
        assert u.parse_datetime("not a date") is None

    def test_empty_and_none_return_none(self):
        assert u.parse_datetime("") is None
        assert u.parse_datetime(None) is None


# --------------------------------------------------------------- normalise_url

class TestNormaliseUrl:
    def test_strips_tracking_query_params(self):
        got = u.normalise_url(
            "https://Example.com:443/News/Story-One/?utm_source=x&ref=y&id=5"
        )
        assert got == "https://example.com/News/Story-One?id=5"

    def test_trailing_slash_removed(self):
        assert u.normalise_url("https://example.com/News/Story-One/") == \
            u.normalise_url("https://example.com/News/Story-One")

    def test_default_port_stripped(self):
        assert u.normalise_url("http://example.com:80/a/b/") == \
            "http://example.com/a/b"

    def test_empty_input(self):
        assert u.normalise_url("") == ""


# --------------------------------------------------------------- titles_similar

class TestTitlesSimilar:
    def test_near_identical_by_ratio(self):
        assert u.titles_similar(
            "Company launches new widget in UK",
            "Company launches new widget in the UK",
        )

    def test_unrelated_titles_not_similar(self):
        assert not u.titles_similar(
            "Company launches new widget in UK",
            "Totally unrelated headline about weather",
        )

    def test_near_dup_by_token_overlap(self):
        # Ratio alone won't catch this; token-set jaccard should.
        assert u.titles_similar(
            "Acme Corp announces major expansion plans today",
            "Acme Corp announces major UK expansion plans",
        )

    def test_empty_title_never_similar(self):
        assert not u.titles_similar("", "Acme Corp announces something")


# ---------------------------------------------------------- looks_like_article_url

class TestLooksLikeArticleUrl:
    @pytest.mark.parametrize("url", [
        "https://example.com/about-us",
        "https://example.com/news/",
        "https://example.com/blog/category/updates",
        "https://example.com/news/page/2",
        "https://example.com/insights",
    ])
    def test_rejects_nav_and_section_pages(self, url):
        assert u.looks_like_article_url(url) is False

    @pytest.mark.parametrize("url", [
        "https://example.com/news/company-launches-new-widget-uk",
        "https://example.com/2026/07/keg-launch",
    ])
    def test_accepts_real_article_slugs(self, url):
        assert u.looks_like_article_url(url) is True


# --------------------------------------------------------- deduplicate_candidates

class TestDeduplicateCandidates:
    def test_provider_priority_wins_on_same_story(self):
        items = [
            {"title": "Acme wins big contract",
             "url": "https://news.example.com/acme-wins-big-contract",
             "discovered_via": "GDELT", "title_match": True,
             "published_at": "2026-07-20"},
            {"title": "Acme wins big contract",
             "url": "https://acme.com/press/acme-wins-big-contract",
             "discovered_via": "Official RSS", "title_match": True,
             "published_at": "2026-07-20"},
        ]
        kept = u.deduplicate_candidates(items)
        assert len(kept) == 1
        assert kept[0]["discovered_via"] == "Official RSS"

    def test_distinct_stories_both_kept(self):
        items = [
            {"title": "Acme wins big contract",
             "url": "https://news.example.com/a", "discovered_via": "GDELT",
             "title_match": True, "published_at": "2026-07-20"},
            {"title": "Totally different story",
             "url": "https://news.example.com/other-story-x",
             "discovered_via": "GDELT", "title_match": False,
             "published_at": "2026-07-19"},
        ]
        assert len(u.deduplicate_candidates(items)) == 2


# ------------------------------------------------------------ deduplicate_stories

class TestDeduplicateStories:
    def test_dedup_only_within_same_company(self):
        stories = [
            {"company": "Acme", "title": "Acme wins big contract",
             "url": "https://news.example.com/a", "score": 70,
             "discovered_via": "GDELT"},
            {"company": "Acme", "title": "Acme wins big contract!",
             "url": "https://acme.com/press/a", "score": 90,
             "discovered_via": "Official RSS"},
            {"company": "OtherCo", "title": "Acme wins big contract",
             "url": "https://news.example.com/b", "score": 60,
             "discovered_via": "GDELT"},
        ]
        kept = u.deduplicate_stories(stories)
        acme_kept = [s for s in kept if s["company"] == "Acme"]
        assert len(acme_kept) == 1
        assert acme_kept[0]["url"] == "https://acme.com/press/a"
        # Same headline, different company - not collapsed.
        assert any(s["company"] == "OtherCo" for s in kept)

    def test_higher_score_preferred_when_tied_on_provider(self):
        stories = [
            {"company": "Acme", "title": "Acme wins big contract",
             "url": "https://a.example.com/x", "score": 90,
             "discovered_via": "GDELT"},
            {"company": "Acme", "title": "Acme wins big contract",
             "url": "https://b.example.com/y", "score": 40,
             "discovered_via": "GDELT"},
        ]
        kept = u.deduplicate_stories(stories)
        assert len(kept) == 1 and kept[0]["score"] == 90


# ------------------------------------------------------- validate_company_analysis

class TestValidateCompanyAnalysis:
    def test_score_clamped_above_100(self):
        assert u.validate_company_analysis({"is_relevant": True, "score": 150})["score"] == 100

    def test_score_clamped_below_0(self):
        assert u.validate_company_analysis({"is_relevant": True, "score": -20})["score"] == 0

    def test_non_numeric_score_defaults_to_0(self):
        result = u.validate_company_analysis({"is_relevant": True, "score": "not-a-number"})
        assert result["score"] == 0

    def test_relevance_string_coercion_true(self):
        for value in ("yes", "true", "1", "TRUE"):
            assert u.validate_company_analysis({"is_relevant": value})["is_relevant"] is True

    def test_relevance_string_coercion_false(self):
        for value in ("no", "false", "0", "maybe"):
            assert u.validate_company_analysis({"is_relevant": value})["is_relevant"] is False

    def test_warning_injected_when_relevant_but_no_drafts(self):
        result = u.validate_company_analysis(
            {"is_relevant": True, "score": 80, "drafts": {}}
        )
        assert any("draft" in w.casefold() for w in result["warnings"])

    def test_no_warning_when_not_relevant_and_no_drafts(self):
        result = u.validate_company_analysis(
            {"is_relevant": False, "score": 10, "drafts": {}}
        )
        assert result["warnings"] == []

    def test_no_warning_when_relevant_and_drafts_present(self):
        result = u.validate_company_analysis(
            {"is_relevant": True, "score": 80,
             "drafts": {"concise": "Some real detail here."}}
        )
        assert result["warnings"] == []


# -------------------------------------------------------- validate_sector_analysis

class TestValidateSectorAnalysis:
    def test_score_clamped_and_rounded(self):
        assert u.validate_sector_analysis({"is_relevant": "true", "score": "70.6"})["score"] == 71

    def test_relevance_string_coercion(self):
        assert u.validate_sector_analysis({"is_relevant": "yes", "score": 10})["is_relevant"] is True
        assert u.validate_sector_analysis({"is_relevant": "no", "score": 10})["is_relevant"] is False

    def test_summary_and_angle_are_cleaned_text(self):
        result = u.validate_sector_analysis(
            {"is_relevant": True, "score": 50, "summary": "  x   y  "}
        )
        assert result["summary"] == "x y"
        assert result["angle"] == ""


# ------------------------------------------------------------- website_url

class TestWebsiteUrl:
    def test_bare_host_gets_scheme_and_root_path(self):
        assert u.website_url("Revvi.co.uk") == "https://revvi.co.uk/"

    def test_existing_scheme_and_path_kept(self):
        assert u.website_url("HTTPS://WWW.Example.com/uk/") == "https://www.example.com/uk/"

    def test_query_and_fragment_dropped(self):
        assert u.website_url("example.com/x?utm_source=a#top") == "https://example.com/x"

    def test_blank_and_nonsense_return_empty(self):
        assert u.website_url("") == ""
        assert u.website_url("not a website") == ""

    def test_host_strips_www(self):
        assert u.website_host("https://www.revvi.co.uk/") == "revvi.co.uk"


# ------------------------------------------------- company_needs_enrichment

class TestCompanyNeedsEnrichment:
    def test_dashboard_shaped_entry_needs_enrichment(self):
        assert u.company_needs_enrichment(
            {"name": "Revvi", "search_terms": [], "domain": ""}) is True

    def test_curated_entry_is_left_alone(self):
        assert u.company_needs_enrichment(
            {"name": "Swytch", "search_terms": ["Swytch e-bike"],
             "domain": "swytchbike.com", "industry_terms": []}) is False

    def test_already_researched_entry_is_not_repeated(self):
        assert u.company_needs_enrichment(
            {"name": "Revvi", "search_terms": [], "domain": "",
             "enriched_at": "2026-07-27T00:00:00+00:00"}) is False


# ---------------------------------------------------------- parse_json_object

class TestParseJsonObject:
    def test_plain_object(self):
        assert u.parse_json_object('{"a": 1}') == {"a": 1}

    def test_fenced_object(self):
        assert u.parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}

    def test_object_wrapped_in_prose(self):
        text = 'Here is what I found:\n{"a": 1}\nSources: example.com'
        assert u.parse_json_object(text) == {"a": 1}

    def test_non_object_rejected(self):
        with pytest.raises(ValueError):
            u.parse_json_object("[1, 2, 3]")


# ------------------------------------------------------- validate_enrichment

class TestValidateEnrichment:
    def test_lists_are_cleaned_deduped_and_capped(self):
        result = u.validate_enrichment(
            {"search_terms": ["Revvi  bike", "revvi bike", "", "a", "b", "c", "d", "e"]})
        assert result["search_terms"][:2] == ["Revvi bike", "a"]
        assert len(result["search_terms"]) == 5

    def test_non_http_urls_are_dropped(self):
        result = u.validate_enrichment(
            {"rss_feeds": ["not a url", "revvi.co.uk/feed", "https://revvi.co.uk/feed"]})
        assert result["rss_feeds"] == ["https://revvi.co.uk/feed"]

    def test_missing_fields_default_empty(self):
        result = u.validate_enrichment({})
        assert result["aliases"] == [] and result["description"] == ""
        assert result["confidence"] == "unknown"

    def test_overlong_description_is_truncated(self):
        result = u.validate_enrichment({"description": "x" * 5000})
        assert len(result["description"]) == u.DESCRIPTION_LIMIT


# ------------------------------------------------------------- matches_company

class TestMatchesCompany:
    COMPANY = {
        "name": "Future Maintenance Technologies",
        "aliases": ["FMT Robotics", "thinkFMT"],
        "search_terms": ["ARIIS rail inspection robot", "TRES train inspection robot"],
        "people": ["Jane Doe"],
    }

    def test_matches_via_search_terms_only(self):
        assert u.matches_company(
            self.COMPANY, "ARIIS rail inspection robot completes trial on the ECML")

    def test_matches_via_people_only(self):
        assert u.matches_company(self.COMPANY, "Jane Doe joins the board of a rail supplier")

    def test_rejects_unrelated_text(self):
        assert not u.matches_company(self.COMPANY, "Totally unrelated story about biscuits")


# --------------------------------------------------------------- fetch_article

class _FakeResponse:
    def __init__(self, text: str):
        self.text = text
        self.headers = {"content-type": "text/html; charset=utf-8"}


class TestFetchArticle:
    def test_extracts_text_from_div_and_li_layout(self, monkeypatch):
        # No <p> tags at all - the shape that used to yield text == "" and
        # get dropped as "thin" before the LLM ever saw an appointment post.
        html = """
        <html><body><main>
          <div>
            <div>Jane Doe has been appointed Head of Post-Production at Acme Studios.</div>
            <ul><li>She joins from Rival Studios where she led the grading team for six years.</li></ul>
          </div>
        </main></body></html>
        """
        monkeypatch.setattr(u, "request_with_backoff", lambda *a, **k: _FakeResponse(html))
        result = u.fetch_article("https://example.com/news/jane-doe-appointed-head")
        assert "Jane Doe has been appointed" in result["text"]
        assert "Rival Studios" in result["text"]


# ------------------------------------------------------------ resolve_scraped_date

class TestResolveScrapedDate:
    NOW = "2026-07-27T00:00:00+00:00"

    @staticmethod
    def _page(published=""):
        return {"published": published, "text": "x", "description": "", "title": ""}

    def test_recent_page_date_is_used(self):
        recent = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        item = {"discovered_via": "Company newsroom", "title": "t",
                "url": "https://example.com/news/jane-doe-appointed-head"}
        assert u.resolve_scraped_date(item, self._page(recent), self.NOW) is True
        assert item["published_at"] == recent

    def test_old_page_date_is_kept_verbatim_and_the_item_dropped(self):
        """The regression that put months-old pages in the feed dated today: a
        real date outside the window must never be replaced with now."""
        old = (datetime.now(timezone.utc) - timedelta(days=160)).isoformat()
        item = {"discovered_via": "Company newsroom", "title": "t",
                "url": "https://example.com/news/pet-lightweighting"}
        assert u.resolve_scraped_date(item, self._page(old), self.NOW) is False
        assert item["published_at"] == old
        assert item["published_at"] != self.NOW

    def test_date_recovered_from_scraped_card_text(self):
        recent = datetime.now(timezone.utc) - timedelta(days=5)
        title = f"Packaging Technology Something Happened {recent.strftime('%d %b, %Y')}"
        item = {"discovered_via": "Company newsroom", "title": title,
                "url": "https://example.com/news/something-happened"}
        assert u.resolve_scraped_date(item, self._page(), self.NOW) is True
        assert u.parse_datetime(item["published_at"]).date() == recent.date()

    def test_date_recovered_from_url_path(self):
        recent = datetime.now(timezone.utc) - timedelta(days=4)
        url = f"https://example.com/{recent.year}/{recent.month:02d}/{recent.day:02d}/a-story-here"
        item = {"discovered_via": "Company newsroom", "title": "No date here at all", "url": url}
        assert u.resolve_scraped_date(item, self._page(), self.NOW) is True
        assert u.parse_datetime(item["published_at"]).date() == recent.date()

    def test_undated_with_nothing_recoverable_is_dropped(self):
        item = {"discovered_via": "Company newsroom", "title": "No date anywhere here",
                "url": "https://example.com/news/jane-doe-appointed-head"}
        assert u.resolve_scraped_date(item, self._page(), self.NOW) is False
        assert "published_at" not in item

    def test_never_stamps_now(self):
        item = {"discovered_via": "Company newsroom", "title": "Undated evergreen guide",
                "url": "https://example.com/insights/how-to-do-a-thing"}
        u.resolve_scraped_date(item, self._page(), self.NOW)
        assert item.get("published_at") != self.NOW
        assert item.get("date_estimated") is not True


# --------------------------------------------------------- title extraction

class TestCleanPageTitle:
    def test_strips_site_suffix(self):
        assert u.clean_page_title(
            "Engineering Behind PET Lightweighting | Petainer News"
        ) == "Engineering Behind PET Lightweighting"

    def test_strips_hyphen_site_suffix(self):
        assert u.clean_page_title(
            "Clearway appoint Christian Baumbach as Managing Director - Clearway"
        ) == "Clearway appoint Christian Baumbach as Managing Director"

    def test_strips_trailing_read_time(self):
        assert u.clean_page_title(
            "Decision Optimisation for Strategic Command 3 min read"
        ) == "Decision Optimisation for Strategic Command"

    def test_keeps_title_when_trimming_would_gut_it(self):
        assert u.clean_page_title("Whitespace | Insights and news updates") \
            == "Whitespace | Insights and news updates"

    def test_falls_back_when_unusable(self):
        assert u.clean_page_title("Home", "The scraped headline goes here") \
            == "The scraped headline goes here"


class TestAnchorHeadline:
    def test_prefers_heading_over_whole_card(self):
        soup = BeautifulSoup(
            '<a href="/x"><span>Packaging Technology</span>'
            '<h3>The Engineering Behind PET Lightweighting</h3>'
            '<time>20 Feb, 2026</time></a>', "html.parser")
        assert u.anchor_headline(soup.a) == "The Engineering Behind PET Lightweighting"

    def test_falls_back_to_full_text_without_a_heading(self):
        soup = BeautifulSoup('<a href="/x">Molinare hires Des Carey for Dublin</a>',
                             "html.parser")
        assert u.anchor_headline(soup.a) == "Molinare hires Des Carey for Dublin"


# ------------------------------------------------------------- suppression

class TestSuppression:
    def test_suppressed_url_makes_no_candidate(self, monkeypatch):
        url = "https://example.com/insights/evergreen-guide"
        monkeypatch.setattr(u, "SUPPRESSED_URLS", {u.normalise_url(url)})
        assert u.make_candidate(
            company={"name": "Acme"}, title="A perfectly good headline here",
            url=url, source="example.com",
            published_at=datetime.now(timezone.utc).isoformat(),
            discovered_via="Company newsroom") is None

    def test_tracking_params_do_not_defeat_suppression(self, monkeypatch):
        monkeypatch.setattr(
            u, "SUPPRESSED_URLS", {u.normalise_url("https://example.com/a/story")})
        assert u.make_candidate(
            company={"name": "Acme"}, title="A perfectly good headline here",
            url="https://example.com/a/story?utm_source=x", source="example.com",
            published_at=datetime.now(timezone.utc).isoformat(),
            discovered_via="Company newsroom") is None

    def test_unsuppressed_url_still_makes_a_candidate(self, monkeypatch):
        monkeypatch.setattr(u, "SUPPRESSED_URLS", {"https://example.com/other"})
        assert u.make_candidate(
            company={"name": "Acme"}, title="A perfectly good headline here",
            url="https://example.com/a/story", source="example.com",
            published_at=datetime.now(timezone.utc).isoformat(),
            discovered_via="Company newsroom") is not None

    def test_missing_file_suppresses_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(u, "SUPPRESSED_FILE", tmp_path / "nope.json")
        assert u.load_suppressed() == set()

    def test_corrupt_file_suppresses_nothing(self, tmp_path, monkeypatch):
        path = tmp_path / "suppressed.json"
        path.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(u, "SUPPRESSED_FILE", path)
        assert u.load_suppressed() == set()

    def test_reads_urls_key(self, tmp_path, monkeypatch):
        path = tmp_path / "suppressed.json"
        path.write_text(json.dumps({"urls": ["https://example.com/a/"]}), encoding="utf-8")
        monkeypatch.setattr(u, "SUPPRESSED_FILE", path)
        assert u.load_suppressed() == {u.normalise_url("https://example.com/a/")}


# ----------------------------------------------------- seen-cache retry-once

class TestSeenCacheRetry:
    def test_rejection_is_retried_once_then_suppressed(self):
        cutoff = u.cutoff(7)
        now_iso = datetime.now(timezone.utc).isoformat()

        first = u._reject_record(None, now_iso)
        assert first["n"] == 1
        assert u.seen_cache_should_skip(first, cutoff) is False  # first reject: retry allowed

        second = u._reject_record(first, now_iso)
        assert second["n"] == 2
        assert u.seen_cache_should_skip(second, cutoff) is True  # second reject: suppressed

    def test_kept_record_always_suppressed(self):
        cutoff = u.cutoff(7)
        now_iso = datetime.now(timezone.utc).isoformat()
        record = {"t": now_iso, "kept": True}
        assert u.seen_cache_should_skip(record, cutoff) is True

    def test_legacy_record_without_n_counts_as_one_prior_attempt(self):
        cutoff = u.cutoff(7)
        now_iso = datetime.now(timezone.utc).isoformat()
        legacy = {"t": now_iso, "kept": False}
        assert u.seen_cache_should_skip(legacy, cutoff) is False  # one retry still owed
        bumped = u._reject_record(legacy, now_iso)
        assert bumped["n"] == 2
        assert u.seen_cache_should_skip(bumped, cutoff) is True


# -------------------------------------------------------------- bing_news_url

class TestBingNewsUrl:
    def test_builds_quoted_rss_query_scoped_to_gb(self):
        url = u.bing_news_url(["Molinare", "Notorious DIT"])
        assert url.startswith("https://www.bing.com/news/search?q=")
        assert "format=RSS" in url
        assert "cc=GB" in url
        assert "Molinare" in url and "Notorious" in url


# ------------------------------------------------------- search_companies_house

class TestSearchCompaniesHouse:
    def test_returns_empty_with_no_key_set(self, monkeypatch):
        monkeypatch.delenv("COMPANIES_HOUSE_KEY", raising=False)
        items, ok = u.search_companies_house({"name": "Acme", "company_number": "12345678"})
        assert items == [] and ok == 0

    def test_returns_empty_with_no_company_number(self, monkeypatch):
        monkeypatch.setenv("COMPANIES_HOUSE_KEY", "fake-key")
        items, ok = u.search_companies_house({"name": "Acme"})
        assert items == [] and ok == 0


# ------------------------------------------------------------ story_tier/recency

class TestStoryTier:
    def test_boundary_at_80_is_lead(self):
        assert u.story_tier(80) == "lead"
        assert u.story_tier(79) == "reported"

    def test_boundary_at_60_is_reported(self):
        assert u.story_tier(60) == "reported"
        assert u.story_tier(59) == "low"

    def test_below_45_is_still_low(self):
        assert u.story_tier(0) == "low"


class TestStoryRecency:
    def test_exactly_fresh_days_old_is_fresh(self):
        edge = (datetime.now(timezone.utc) - timedelta(days=u.FRESH_DAYS) + timedelta(minutes=5)).isoformat()
        assert u.story_recency(edge) == "fresh"

    def test_older_than_fresh_days_is_catchup(self):
        old = (datetime.now(timezone.utc) - timedelta(days=u.FRESH_DAYS + 1)).isoformat()
        assert u.story_recency(old) == "catchup"


# ------------------------------------------------------------------ assemble_story

def _story_item(**overrides):
    item = {"url": "https://acme.com/a", "title": "Acme news", "company": "Acme",
            "company_domain": "acme.com", "industry": "Media", "source": "Acme Press",
            "published_at": datetime.now(timezone.utc).isoformat(), "discovered_via": "Official RSS"}
    item.update(overrides)
    return item


def _story_analysis(**overrides):
    analysis = {"score": 90, "story_type": "Update", "summary": "s", "why_it_matters": "w",
                "verified_facts": [], "warnings": [],
                "drafts": {"concise": "x", "investor": "", "people": ""}}
    analysis.update(overrides)
    return analysis


class TestAssembleStoryDateEstimated:
    def test_flag_survives_assembly(self):
        story = u.assemble_story(_story_item(date_estimated=True), _story_analysis(),
                                 datetime.now(timezone.utc).isoformat())
        assert story["date_estimated"] is True

    def test_flag_defaults_false_not_missing(self):
        story = u.assemble_story(_story_item(), _story_analysis(),
                                 datetime.now(timezone.utc).isoformat())
        assert story["date_estimated"] is False


class TestAssembleStory:
    def test_high_score_no_warnings_is_ready(self):
        story = u.assemble_story(_story_item(), _story_analysis(score=90), "2026-07-27T00:00:00Z")
        assert story["status"] == "ready"

    def test_trivial_warning_does_not_downgrade_status(self):
        story = u.assemble_story(_story_item(),
                                 _story_analysis(score=90, warnings=["A stylistic note."]),
                                 "2026-07-27T00:00:00Z")
        assert story["status"] == "ready"

    def test_missing_draft_warning_downgrades_status(self):
        story = u.assemble_story(_story_item(),
                                 _story_analysis(score=90, warnings=[u.SUBSTANTIVE_WARNING]),
                                 "2026-07-27T00:00:00Z")
        assert story["status"] == "needs_review"

    def test_below_lead_score_is_needs_review_even_without_warnings(self):
        story = u.assemble_story(_story_item(), _story_analysis(score=70), "2026-07-27T00:00:00Z")
        assert story["status"] == "needs_review"

    def test_tier_and_recency_fields_present(self):
        story = u.assemble_story(_story_item(), _story_analysis(score=90), "2026-07-27T00:00:00Z")
        assert story["tier"] == "lead"
        assert story["recency"] == "fresh"


# ---------------------------------------------------------- skip_analysis assembly

class TestAssembleSkipAnalysisStory:
    def test_bypasses_llm_and_assembles_directly(self):
        item = {"company": "Acme", "company_domain": "acme.com", "industry": "Media",
                "title": "Jane Doe appointed Head of Ops at Acme",
                "url": "https://find-and-update.company-information.service.gov.uk/company/1/officers",
                "source": "Companies House",
                "published_at": datetime.now(timezone.utc).isoformat(),
                "feed_summary": "Companies House records show Jane Doe appointed Head of Ops.",
                "discovered_via": "Companies House", "skip_analysis": True}
        story = u.assemble_skip_analysis_story(item, "2026-07-27T00:00:00Z")
        assert story["tier"] == "lead"
        assert story["story_type"] == "Appointment"
        assert story["drafts"] == {"concise": "", "investor": "", "people": ""}
        assert story["status"] == "needs_review"

    def test_malformed_item_raises_so_caller_can_guard(self):
        with pytest.raises(Exception):
            u.assemble_skip_analysis_story(None, "2026-07-27T00:00:00Z")


# ---------------------------------------------------------------- archive merge

def _aged(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class TestMergeArchive:
    def test_new_items_append(self):
        existing = [{"url": "https://a.com/1", "published_at": _aged(u.ARCHIVE_DAYS + 5)}]
        fresh = [{"url": "https://a.com/2", "published_at": _aged(u.ARCHIVE_DAYS + 1)}]
        assert len(u.merge_archive(existing, fresh)) == 2

    def test_duplicate_by_normalised_url_not_appended(self):
        aged = _aged(u.ARCHIVE_DAYS + 5)
        existing = [{"url": "https://a.com/1?utm_source=x", "published_at": aged}]
        fresh = [{"url": "https://a.com/1", "published_at": aged}]
        assert len(u.merge_archive(existing, fresh)) == 1

    def test_story_still_in_the_live_feed_is_not_archived_yet(self):
        fresh = [{"url": "https://a.com/1", "published_at": _aged(u.ARCHIVE_DAYS - 5)}]
        assert u.merge_archive([], fresh) == []

    def test_story_is_archived_once_it_passes_the_boundary(self):
        fresh = [{"url": "https://a.com/1", "published_at": _aged(u.ARCHIVE_DAYS + 1)}]
        assert len(u.merge_archive([], fresh)) == 1

    def test_entry_for_a_now_removed_company_survives(self):
        existing = [{"url": "https://old.com/1", "company": "GoneCo",
                     "published_at": _aged(u.ARCHIVE_DAYS + 20)}]
        merged = u.merge_archive(existing, [])
        assert len(merged) == 1 and merged[0]["company"] == "GoneCo"

    def test_one_year_cap_drops_provably_old_items(self):
        existing = [{"url": "https://a.com/1", "published_at": _aged(u.ARCHIVE_MAX_DAYS + 30)}]
        assert u.merge_archive(existing, []) == []

    def test_item_without_a_parseable_date_is_kept_not_dropped(self):
        # It cannot satisfy the live feed's is_within check either, so archiving
        # it is the only way it survives at all.
        existing = [{"url": "https://a.com/1", "published_at": ""}]
        assert len(u.merge_archive(existing, [])) == 1


class TestLoadArchive:
    def test_missing_file_returns_empty_list(self, tmp_path, monkeypatch):
        monkeypatch.setattr(u, "ARCHIVE_FILE", tmp_path / "archive.json")
        assert u.load_archive() == []

    def test_corrupt_file_logs_and_starts_fresh(self, tmp_path, monkeypatch):
        path = tmp_path / "archive.json"
        path.write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr(u, "ARCHIVE_FILE", path)
        assert u.load_archive() == []

    def test_valid_file_is_read_not_discarded(self, tmp_path, monkeypatch):
        path = tmp_path / "archive.json"
        path.write_text(json.dumps({"stories": [{"url": "https://a.com/1"}]}), encoding="utf-8")
        monkeypatch.setattr(u, "ARCHIVE_FILE", path)
        assert len(u.load_archive()) == 1
