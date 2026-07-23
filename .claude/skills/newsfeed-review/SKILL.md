---
name: newsfeed-review
description: Run the full News Feed Team review (sourcing + scoring + ops + frontend) end to end, or a scoped subset if arguments are given.
---

This skill runs the News Feed Team: four specialists, no separate coordinator
subagent - subagents in this harness cannot spawn further subagents, so a
"coordinator" that tries to delegate would do nothing real. You (the main
assistant, running this skill) do the orchestration directly using your own
Agent tool.

The team:
- news-sourcing-agent - scripts/update_news.py's collection layer +
  config/companies.json. Commits and pushes.
- news-scoring-agent - scripts/update_news.py's Gemini/scoring layer +
  scripts/send_digest.py. Commits and pushes.
- news-ops-agent - Cloudflare Workers + refresh-news.yml health. Read-only/
  diagnostic; never deploys, never commits.
- news-frontend-agent - site/index.html. Edits the working tree; never
  commits.

Hard rule: news-sourcing-agent and news-scoring-agent edit the SAME file
(scripts/update_news.py, different sections) and must never run in the same
turn/batch. When both are in scope, invoke news-sourcing-agent first, wait
for it to fully return, confirm what it did (`git log -1 --stat`), and only
then invoke news-scoring-agent in a separate message/turn. news-ops-agent
and news-frontend-agent touch entirely different files/systems and can run
in parallel with each other and alongside the sourcing step (you may invoke
news-sourcing-agent + news-ops-agent + news-frontend-agent together in one
message) - but never alongside news-scoring-agent's turn, to keep the
"sourcing fully done before scoring starts" guarantee simple.

Steps:
1. Read any arguments supplied after /newsfeed-review (e.g. "sourcing
   only", "scoring only", "ops only", "frontend only", "focus on Petainer's
   scoring", or none at all).
2. Decide scope by keyword:
   - No scope / "full review"/"whole team": all four specialists.
   - Sourcing-only (mentions sources, RSS feeds, newsroom URLs, search/
     industry terms, coverage, GDELT, Google News, companies.json, "we're
     missing a story", candidate-level duplicates): news-sourcing-agent
     only.
   - Scoring-only (mentions scoring, thresholds, MIN_SCORE/READY_SCORE,
     Gemini, prompts, LinkedIn drafts, draft quality, relevance, "why was
     this dropped/kept", the digest email): news-scoring-agent only.
   - Both sourcing + scoring (generic "backend", "the pipeline",
     "update_news.py" with no more specific keyword): both, sequential per
     the hard rule above.
   - Ops-only (mentions the scheduler, cron, Cloudflare Worker, wrangler,
     the GitHub Actions workflow, workflow_dispatch, "this morning's run
     didn't fire", "the digest didn't send"): news-ops-agent only.
   - Frontend-only (mentions the dashboard, UI, interface, Archive tab,
     auto-refresh, graphs): news-frontend-agent only.
   - Ambiguous: default to all four.
3. Invoke the relevant specialist(s) via your Agent tool, passing the
   user's request through, run in the foreground (you need their result to
   report back). Respect the hard rule above on batching. Do not run
   scripts/update_news.py, scripts/send_digest.py, wrangler, or any git
   command yourself - that's each specialist's job.
4. After they return, verify what actually happened on disk with read-only
   commands (`git log -1 --stat`, `git status`, `git diff --stat`) - never
   run write commands yourself. For news-ops-agent, also confirm by
   inspection that nothing was deployed and nothing was committed (it
   should say so itself, but check `git status` too).
5. Report back tersely, one line per change, with an explicit split:
   "Sourcing - pushed to main (commit <hash>)" / "Scoring - pushed to main
   (commit <hash>)" for anything committed, "Ops - diagnosed, drafted a fix
   for <Cloudflare dashboard|this repo's working tree>, not deployed/not
   committed" for news-ops-agent, and "Frontend - staged in your working
   tree, NOT committed" for news-frontend-agent. If a specialist made no
   changes, say so plainly rather than inventing activity. If a specialist
   flagged something outside its scope, relay that as a brief one-line flag
   - don't act on it or expand on it.
6. This skill only runs when the user explicitly types /newsfeed-review (or
   asks for it by name, e.g. "review the news feed"). Never trigger it
   automatically, on a schedule, or in the background - and this applies
   just as much when the user asks in plain language outside the skill.
