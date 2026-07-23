"""Offline unit tests for scripts/send_digest.py's pure plumbing.

No network/SMTP calls anywhere in this file. Run with:
    python -m pytest tests/ -q
"""
from __future__ import annotations

import json

import send_digest as d


STORY = {"company": "Acme", "title": "Acme wins big contract", "url": "https://acme.com/a",
         "source": "Acme Press", "score": 90, "status": "ready", "summary": "Detail.",
         "story_type": "Partnership", "published_at": "2026-07-23T09:00:00Z"}

BLOCKS = [("Acme", [STORY], [])]


# --------------------------------------------------------------------- build_intro

class TestBuildIntro:
    def test_prefers_stored_digest_intro_when_present(self):
        out = d.build_intro(BLOCKS, 1, 0, False, "Good morning, a busy one for Acme today.")
        assert out == "Good morning, a busy one for Acme today."

    def test_escapes_stored_digest_intro(self):
        out = d.build_intro(BLOCKS, 1, 0, False, "Acme & Co < news >")
        assert "&amp;" in out and "&lt;" in out

    def test_falls_back_to_static_template_when_missing(self):
        out = d.build_intro(BLOCKS, 1, 0, False, "")
        assert "Today&rsquo;s briefing carries 1 new story" in out

    def test_falls_back_when_no_blocks(self):
        out = d.build_intro([], 0, 0, False, "")
        assert "quiet one" in out


class TestBuildIntroPlain:
    def test_prefers_stored_digest_intro_verbatim(self):
        out = d.build_intro_plain(BLOCKS, 1, 0, False, "Good morning, a busy one for Acme today.")
        assert out == "Good morning, a busy one for Acme today."

    def test_falls_back_to_static_template_when_missing(self):
        out = d.build_intro_plain(BLOCKS, 1, 0, False, "")
        assert "Today's briefing carries 1 new story" in out
        # Plain text must not carry HTML entities.
        assert "&rsquo;" not in out and "&mdash;" not in out


# --------------------------------------------------------------------- load_edition

class TestLoadEdition:
    def _write_news(self, tmp_path, monkeypatch, digest_intro=None):
        payload = {
            "generated_at": "2026-07-23T08:00:00Z",
            "companies": [{"name": "Acme"}],
            "stories": [dict(STORY, first_seen="2026-07-23T08:00:00Z")],
            "sector_stories": [],
        }
        if digest_intro is not None:
            payload["digest_intro"] = digest_intro
        news_file = tmp_path / "news.json"
        news_file.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(d, "NEWS_FILE", news_file)

    def test_reads_stored_digest_intro(self, tmp_path, monkeypatch):
        self._write_news(tmp_path, monkeypatch, digest_intro="A calm start for the portfolio.")
        _, _, _, _, _, digest_intro = d.load_edition()
        assert digest_intro == "A calm start for the portfolio."

    def test_missing_digest_intro_is_empty_string(self, tmp_path, monkeypatch):
        self._write_news(tmp_path, monkeypatch, digest_intro=None)
        _, _, _, _, _, digest_intro = d.load_edition()
        assert digest_intro == ""

    def test_blank_digest_intro_is_empty_string(self, tmp_path, monkeypatch):
        self._write_news(tmp_path, monkeypatch, digest_intro="   ")
        _, _, _, _, _, digest_intro = d.load_edition()
        assert digest_intro == ""


# --------------------------------------------------------------------- build_html

class TestBuildHtml:
    def test_masthead_is_title_case_not_all_caps(self):
        html = d.build_html(BLOCKS, "2026-07-23T08:00:00Z", 1, 0, False)
        assert "Portfolio Newsroom" in html
        assert "PORTFOLIO NEWSROOM" not in html

    def test_hover_style_and_class_present(self):
        html = d.build_html(BLOCKS, "2026-07-23T08:00:00Z", 1, 0, False)
        assert "a.hl:hover" in html
        assert 'class="hl"' in html

    def test_stored_digest_intro_rendered_in_html(self):
        html = d.build_html(BLOCKS, "2026-07-23T08:00:00Z", 1, 0, False,
                            "A calm start for the portfolio.")
        assert "A calm start for the portfolio." in html


# --------------------------------------------------------------------- build_text

class TestBuildText:
    def test_stored_digest_intro_rendered_in_text(self):
        text = d.build_text(BLOCKS, "2026-07-23T08:00:00Z", 1, 0, False,
                            "A calm start for the portfolio.")
        assert "A calm start for the portfolio." in text

    def test_fallback_intro_rendered_when_missing(self):
        text = d.build_text(BLOCKS, "2026-07-23T08:00:00Z", 1, 0, False, "")
        assert "Today's briefing carries 1 new story" in text
