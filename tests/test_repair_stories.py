"""Offline unit tests for scripts/repair_stories.py's routing logic.

No network/API calls anywhere in this file - fetch_article and call_gemini are
monkeypatched. Run with:
    python -m pytest tests/ -q
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import repair_stories as r
import update_news as u


def _aged(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _story(**overrides):
    story = {"id": "abc", "company": "Petainer", "url": "https://petainer.com/insights/a",
             "title": "Packaging Technology The Engineering Behind PET Lightweighting 20 Feb, 2026",
             "published_at": _aged(0), "summary": "s", "score": 80, "tier": "lead"}
    story.update(overrides)
    return story


def _stub(monkeypatch, *, verdict, page=None):
    monkeypatch.setattr(r, "classify", lambda story, material: verdict)
    monkeypatch.setattr(u, "fetch_article", lambda url: page or {
        "text": "body", "published": "", "description": "", "title": ""})


# ------------------------------------------------------------------- routing

class TestDecide:
    def test_evergreen_is_deleted(self, monkeypatch):
        _stub(monkeypatch, verdict={"is_news": False, "headline": "An explainer",
                                    "published_date": "2026-02-20", "reason": "explainer"})
        assert r.decide(_story())["action"] == "delete"

    def test_recent_event_is_kept(self, monkeypatch):
        _stub(monkeypatch, verdict={"is_news": True, "headline": "Acme appoints a new MD",
                                    "published_date": _aged(3)[:10]})
        # No date in the title or URL, so the model's date is what is used.
        assert r.decide(_story(title="Acme appoints a new managing director",
                               url="https://acme.com/news/a"))["action"] == "keep"

    def test_older_event_is_archived(self, monkeypatch):
        _stub(monkeypatch, verdict={"is_news": True, "headline": "Acme appoints a new MD",
                                    "published_date": _aged(u.ARCHIVE_DAYS + 10)[:10]})
        assert r.decide(_story(title="Acme appoints a new managing director",
                               url="https://acme.com/news/a"))["action"] == "archive"

    def test_event_over_a_year_old_is_deleted(self, monkeypatch):
        _stub(monkeypatch, verdict={"is_news": True, "headline": "Acme appoints a new MD",
                                    "published_date": _aged(u.ARCHIVE_MAX_DAYS + 30)[:10]})
        assert r.decide(_story(title="Acme appoints a new managing director",
                               url="https://acme.com/news/a"))["action"] == "delete"

    def test_event_with_no_establishable_date_is_deleted(self, monkeypatch):
        _stub(monkeypatch, verdict={"is_news": True, "headline": "Acme appoints a new MD",
                                    "published_date": ""})
        story = _story(title="No date anywhere in this headline",
                       url="https://acme.com/news/a-story")
        assert r.decide(story)["action"] == "delete"

    def test_classifier_failure_keeps_the_story(self, monkeypatch):
        """A dead API must never be read as 'delete everything'."""
        _stub(monkeypatch, verdict={})
        decision = r.decide(_story())
        assert decision["action"] == "keep"
        assert "unavailable" in decision["why"]


class TestResolveDate:
    def test_page_metadata_wins(self, monkeypatch):
        page = {"published": _aged(5), "text": "", "description": "", "title": ""}
        assert r.resolve_date(_story(), page, "2020-01-01")[:10] == _aged(5)[:10]

    def test_falls_back_to_date_in_scraped_title(self):
        page = {"published": "", "text": "", "description": "", "title": ""}
        got = r.resolve_date(_story(), page, "")
        assert got[:10] == "2026-02-20"

    def test_falls_back_to_url_path(self):
        page = {"published": "", "text": "", "description": "", "title": ""}
        story = _story(title="No date in here at all",
                       url="https://acme.com/2026/03/09/a-story")
        assert r.resolve_date(story, page, "")[:10] == "2026-03-09"

    def test_falls_back_to_the_model(self):
        page = {"published": "", "text": "", "description": "", "title": ""}
        story = _story(title="No date in here at all", url="https://acme.com/news/a")
        assert r.resolve_date(story, page, "2026-05-04")[:10] == "2026-05-04"

    def test_ignores_the_date_already_on_record(self):
        """That value is the one under suspicion - the bug wrote 'now' into it."""
        page = {"published": "", "text": "", "description": "", "title": ""}
        story = _story(title="No date in here at all", url="https://acme.com/news/a",
                       published_at=_aged(0))
        assert r.resolve_date(story, page, "") == ""

    def test_unestablishable_returns_empty(self):
        page = {"published": "", "text": "", "description": "", "title": ""}
        story = _story(title="No date in here at all", url="https://acme.com/news/a")
        assert r.resolve_date(story, page, "") == ""


class TestApplyDecision:
    def test_title_and_date_are_replaced_and_id_rebuilt(self):
        story = _story()
        decision = {"action": "keep", "why": "", "confidence": "high",
                    "title": "The Engineering Behind PET Lightweighting",
                    "date": _aged(3)}
        fixed = r.apply_decision(story, decision)
        assert fixed["title"] == "The Engineering Behind PET Lightweighting"
        assert fixed["published_at"] == decision["date"]
        assert fixed["date_estimated"] is False
        assert fixed["id"] == u.story_id(story["url"], decision["title"])
        assert fixed["id"] != story["id"]

    def test_missing_date_leaves_the_original_alone(self):
        story = _story()
        fixed = r.apply_decision(story, {"action": "keep", "why": "", "confidence": "low",
                                         "title": "A title", "date": ""})
        assert fixed["published_at"] == story["published_at"]


class TestLoadSuppressedPayload:
    def test_missing_file_starts_an_empty_list(self, tmp_path, monkeypatch):
        monkeypatch.setattr(u, "SUPPRESSED_FILE", tmp_path / "nope.json")
        assert r.load_suppressed_payload()["urls"] == []

    def test_existing_urls_are_preserved(self, tmp_path, monkeypatch):
        path = tmp_path / "suppressed.json"
        path.write_text(json.dumps({"urls": ["https://a.com/x"]}), encoding="utf-8")
        monkeypatch.setattr(u, "SUPPRESSED_FILE", path)
        assert r.load_suppressed_payload()["urls"] == ["https://a.com/x"]

    def test_corrupt_file_does_not_raise(self, tmp_path, monkeypatch):
        path = tmp_path / "suppressed.json"
        path.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(u, "SUPPRESSED_FILE", path)
        assert r.load_suppressed_payload()["urls"] == []


class TestDatePrecedence:
    def test_scraped_title_date_beats_the_model(self, monkeypatch):
        """The title carries evidence from the source; the model is a fallback."""
        _stub(monkeypatch, verdict={"is_news": True, "headline": "Clean headline",
                                    "published_date": _aged(1)[:10]})
        decision = r.decide(_story())  # title embeds "20 Feb, 2026"
        assert decision["date"][:10] == "2026-02-20"
