---
name: newsfeed-review
description: Run the full News Feed Team review (backend + frontend) end to end, or a scoped subset if arguments are given.
---

This skill runs the News Feed Team. There is no separate coordinator agent -
subagents in this harness cannot spawn further subagents, so a "coordinator"
that tries to delegate would do nothing real. You (the main assistant, running
this skill) do the orchestration directly using your own Agent tool.

Steps:
1. Read any arguments supplied after /newsfeed-review (e.g. "backend only",
   "frontend only", "focus on Petainer's scoring", or none at all).
2. Decide scope:
   - No scope / "full review"/"whole team": invoke both news-backend-agent
     and news-frontend-agent - send both Agent calls in the same message so
     they run in parallel (they touch disjoint files).
   - Clearly backend-only (mentions scoring, sources, Gemini, dedup, the
     backend, scrapers, companies.json): invoke only news-backend-agent.
   - Clearly frontend-only (mentions the dashboard, UI, interface, Archive
     tab, auto-refresh, graphs): invoke only news-frontend-agent.
   - Ambiguous: default to both.
3. Invoke the relevant specialist(s) via your Agent tool, passing the user's
   request through, run in the foreground (you need their result to report
   back). Do not run scripts/update_news.py, scripts/send_digest.py, or any
   git command yourself - that's each specialist's job.
4. After they return, verify what actually happened on disk with read-only
   git commands (`git log -1 --stat`, `git status`, `git diff --stat`) -
   never run write commands yourself.
5. Report back tersely, one line per change, with an explicit split:
   "Backend - pushed to main (commit <hash>)" for anything news-backend-agent
   committed, vs "Frontend - staged in your working tree, NOT committed" for
   anything news-frontend-agent touched. If a specialist made no changes, say
   so plainly rather than inventing activity. If a specialist flagged
   something outside its scope, relay that as a brief one-line flag - don't
   act on it or expand on it.
6. This skill only runs when the user explicitly types /newsfeed-review (or
   asks for it by name, e.g. "review the news feed"). Never trigger it
   automatically, on a schedule, or in the background - and this applies
   just as much when the user asks in plain language outside the skill.
