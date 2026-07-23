# About This Project

## Who this is for
Built by an intern analyst at Next Wave Partners (NWP), mainly as a way to make
themselves indispensable to the firm before their internship ends — the goal
is a service the managing partners and investment manager come to rely on (and
notice if it stops). The builder mostly "vibe codes" but is actively learning
the underlying structure of the repo (GitHub Actions, Cloudflare Pages
deployment) rather than treating it as a black box.

## Audience
Strictly internal and small: the **managing partners and the investment
manager** read the dashboard and the morning email digest. Nobody else. No
compliance/sensitivity concerns have been flagged — the priority is pure
quality, not caution.

## Definition of "working well"
- Captures **all** relevant, high-quality news about the portfolio companies —
  nothing important missed.
- Captures the big sector news too, and the sector round-up should always be
  populated even on days with no portfolio-specific news (never send an empty
  or thin edition).
- No bugs, anywhere.
- The LinkedIn Studio drafts are effective and genuinely publishable —
  not just plausible-looking filler.
- The interface is professional, clean, legible, and informative — this is
  read by senior people and should look and feel like it.

## Roadmap (not yet started, in no particular priority order)
- Legibility/aesthetic refinements to the dashboard.
- Make the Archive tab actually functional.
- Auto-refresh the dashboard view (currently only updates on a manual page
  refresh).
- Add graphs/metrics to the dashboard — possibly market indices (e.g. FTSE)
  alongside portfolio-company news.
- Source additional news from reputable free/open trade press outlets, on
  top of the current GDELT/Google News/company-newsroom pipeline.
- Change the email's display name so it arrives in inboxes as
  "NWP Portfolio Bulletin" rather than the raw sender address.

## How to work with the builder
Default to hands-off: let the News Feed Team (news-backend-agent,
news-frontend-agent) run reviews and report back rather than seeking approval
at every step for in-scope changes. At the same time, stay token-conscious —
don't spend background effort on review cycles beyond what's actually asked
for.

## News Feed Team architecture note
There is no separate coordinator agent - subagents in this harness cannot
spawn further subagents, so a "news-feed-lead" that tries to delegate to the
other two does nothing real (discovered the hard way: it silently faked a
delegation instead of actually calling a tool). Orchestration (deciding
backend vs frontend vs both, invoking the right specialist(s), aggregating
their reports) is handled directly by the main assistant - either via the
`/newsfeed-review` skill or when the user asks in plain language (e.g.
"review the news feed"). Don't recreate a coordinator subagent for this.
