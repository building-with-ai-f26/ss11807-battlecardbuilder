# AGENTS.md — Coding Agent Operating Manual

> Give this file to your coding agent and check that it is using these instructions. Keep the stack, commands, and ground rules current.

## 1. Project Overview
- **What this repo builds:** Press Pass, a newsletter builder for PR agencies. It gathers this week's news for each of a client's categories, has Claude score each article's relevance and write a short summary, and turns the articles a person approves into an email-ready draft.
- **Primary user & core workflow:** An account person at a PR agency who writes a weekly client news roundup. They enter the client, priority sources and categories, review the AI-ranked articles (reorder, remove with feedback, add paywalled ones by hand), then copy the draft into email. See the living PRD in `docs/index.html`.
- **Tech stack:** Plain HTML/CSS/JS page (`app/index.html`) + Python 3.9+ standard-library server (`app/server.py`). News comes from Google News RSS, publisher RSS feeds and the GNews API; scoring and summaries come from Claude Haiku 5.5 (`claude-haiku-5-5`) through a direct HTTPS call. No packages to install. Analytics: PostHog on both pages.

## 2. Project Structure
```
app/
  index.html        The app: 3 steps (set up → review articles → copy draft). Includes PostHog events.
  server.py         Local server. GET /api/news searches all sources, drops articles older
                    than 7 days, removes duplicates, then asks Claude to score and summarize.
                    POST /api/feedback saves removal notes.
  feeds.json        Publisher RSS feeds checked on every search (edit freely).
  data/             Local feedback notes (git-ignored).
docs/
  index.html        Product Page (GitHub Pages): pitch, screenshots, living PRD, build log.
  prototype-1/, prototype-2/   Screenshots for the Product Page.
tests/
  test_sources.py   Offline tests: feed parsing, search-term matching, 7-day cutoff,
                    duplicate removal, Claude step (with a fake response), fallbacks.
.env.example        Names of the API keys the app reads. Copy to .env; never commit .env.
```

## 3. Setup & Commands
- **Install dependencies:** None (Python standard library only).
- **API keys (one-time):**
  1. `cp .env.example .env` (only the first time; running it again erases your keys)
  2. Open `.env` and set `ANTHROPIC_API_KEY` (console.anthropic.com; set a spend limit) and, optionally, `GNEWS_API_KEY` (gnews.io free tier).
  3. Without `ANTHROPIC_API_KEY` the app still runs and ranks articles by source and date. Without `GNEWS_API_KEY` it skips GNews.
- **Run locally:** `python3 app/server.py` from the repo root, then open http://localhost:8000 (Ctrl+C to stop). The first lines printed say whether GNews and Claude scoring are on.
- **Run deterministic tests:** `python3 -m unittest discover tests` (no internet or keys needed; costs nothing).
- **Run model evals:** Not built yet (planned for Build 4).
- **Cost:** about $0.0008 of Claude usage per category searched (20 articles scored), shown on the page under each category.

## 4. Non-Negotiable Engineering Rules
1. **Plan before coding:** Before touching multiple files, propose a brief plan and wait for approval.
2. **Small batches:** Make small, focused changes that can be reviewed and explained by a human in a single Pull Request.
3. **Explainability:** Avoid unnecessary abstractions or dependencies. Help the human builder understand the main components, what changed, why it matters, what could break, and how it was checked.
4. **Verify changes:** Run relevant checks. For a reproducible logic bug, use a failing test to demonstrate the fix when practical. Record manual checks and known limitations; follow the selected Build 4 evaluation track.
5. **Zero secrets in code:** Never hardcode API keys, tokens, or credentials. Always read from environment variables (`.env`) and update `.env.example` when new variables are needed.
6. **Stop when looping:** If a fix fails twice in a row, stop, summarize what failed, and ask the human before trying a third approach.
