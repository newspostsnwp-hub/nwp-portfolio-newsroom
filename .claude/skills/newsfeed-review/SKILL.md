---
name: newsfeed-review
description: Run the full News Feed Team review (backend + frontend) end to end, or a scoped subset if arguments are given.
---

This skill runs the News Feed Team. It is a thin trigger only - do not review
or edit any code yourself in this skill; delegate everything to the
news-feed-lead subagent.

Steps:
1. Read any arguments the user supplied after /newsfeed-review (e.g.
   "backend only", "frontend only", "focus on Petainer's scoring", or none
   at all). This is the scope to pass through.
2. Invoke the news-feed-lead subagent, passing the user's original request/
   arguments verbatim. If no arguments were given, tell it this is a full
   review (both specialists).
3. Do not run scripts/update_news.py, scripts/send_digest.py, or any git
   command yourself in this skill - news-feed-lead and its specialists own
   all of that.
4. When news-feed-lead returns its final report, relay it to the user with
   its distinctions intact: which changes are already committed and pushed
   to main (with commit hash) versus which changes are only staged in the
   working tree awaiting explicit approval. Do not compress this distinction
   away - it's the most important thing the user needs to see.
5. This skill only runs when the user explicitly types /newsfeed-review (or
   asks for it by name). It is never triggered automatically, on a schedule,
   or in the background.
