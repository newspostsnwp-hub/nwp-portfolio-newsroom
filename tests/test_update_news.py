"""Offline unit tests for scripts/update_news.py's pure functions.

No network/API calls anywhere in this file. Run with:
    python -m pytest tests/ -q
Requires pytest.ini's `pythonpath = scripts` so `import update_news` resolves
without turning scripts/ into a package.
"""
from __future__ import annotations

import pytest

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
