# AGENTS.md — Coding Agent Operating Manual

> Give this file to your coding agent and check that it is using these instructions. Keep the stack, commands, and ground rules current.

## 1. Project Overview
- **What this repo builds:** Press Pass, a newsletter builder for PR agencies. It gathers this week's news for each of a client's categories, has Claude score each article's relevance and write a short summary, and turns the articles a person approves into an email-ready draft.
- **Primary user & core workflow:** An account person at a PR agency who writes a weekly client news roundup. They enter the client, priority sources and categories, review the AI-ranked articles (reorder, remove with feedback, add paywalled ones by hand), then copy the draft into email. See the living PRD in `docs/index.html`.
- **Tech stack:** Plain HTML/CSS/JS page (`app/index.html`) + Python 3.9+ standard-library server (`app/server.py`). News comes from the Exa search API (main source when `EXA_API_KEY` is set: one search limited to the client's priority sources, one open search, both last 7 days with highlights), the NYT Article Search API and publisher RSS feeds; without an Exa key it falls back to Google News RSS and the GNews API; scoring and summaries come from Claude Haiku 5.5 (`claude-haiku-5-5`) through a direct HTTPS call. No packages to install. Sign-in and saved data: Supabase (Auth + Postgres with Row Level Security); the browser loads `supabase-js` 2.119.0 from the jsDelivr CDN, pinned with an SRI integrity hash (use `dist/umd/supabase.js`, never the generated `.min.js`; recompute the hash when upgrading). Analytics: PostHog on the app, the sign-in page and the Product Page; signed-in users are identified by Supabase user id, never email.

## 2. Project Structure
```
app/
  index.html        The app: 3 steps (set up → review articles → copy draft), saved clients,
                    past newsletters, sign-out. Needs a login. Includes PostHog events.
  signin.html       Sign in / create account (email + password).
  auth.js           Shared sign-in helpers: Supabase client, login redirect, API calls with the
                    user's token, PostHog identify/reset.
  server.py         Local server. GET /api/config gives the browser the Supabase URL and
                    publishable key. Every /api route except config needs a valid sign-in token.
                    GET /api/news searches all sources, drops articles older
                    than 7 days, removes duplicates, then asks Claude to score and summarize.
                    POST /api/pdf sends an uploaded PDF of a paywalled article to Claude
                    and returns its headline, source and summary (the PDF is not saved).
                    /api/news and /api/pdf have per-user hourly limits (RATE_LIMITS: 30 and 10).
                    Listens on 127.0.0.1:8000 locally, 0.0.0.0:$PORT on a host (listen_address).
  feeds.json        Publisher RSS feeds checked on every search (edit freely).
  outlets.json      Readable outlet names by domain (Exa returns only a web address). Edit freely.
  data/             Old local feedback notes from before Supabase (git-ignored, never served).
                    New notes go to the Supabase feedback table, saved by the page.
supabase/
  schema.sql        Tables (client_profiles, newsletters, feedback) and Row Level Security
                    policies (feedback: read/add/delete own only; no edits).
                    Write limits per account (trigger enforce_write_limits): 60/60/300 new rows
                    an hour and 500/1,000/5,000 in total; created_at is set by the database.
                    Run it in the Supabase SQL Editor; safe to re-run.
docs/
  index.html        Product Page (GitHub Pages): pitch, screenshots, living PRD, build log.
  prototype-1/, prototype-2/   Screenshots for the Product Page.
tests/
  test_sources.py   Offline tests: feed parsing, search-term matching, 7-day cutoff,
                    duplicate removal, Claude step and PDF reading (with fake
                    responses), fallbacks.
  test_exa_nyt.py   Offline tests for Exa and NYT (faked answers): request settings, highlights,
                    excluded words, dates, which sources run with which keys.
  test_hosting.py   Offline tests for hosting: listen address with/without PORT; .env and
                    feedback notes can't be downloaded from the site.
  test_auth.py      Offline tests for sign-in: token check (faked Supabase), every paid route
                    refuses requests without a valid token, /api/config never sends a secret.
SECURITY.md         Security audit (Build 3): what was checked on the live site, found, fixed, and what is left.
.env.example        Names of the API keys the app reads. Copy to .env; never commit .env.
```

## 3. Setup & Commands
- **Install dependencies:** None (Python standard library only).
- **API keys (one-time):**
  1. `cp .env.example .env` (only the first time; running it again erases your keys)
  2. Open `.env` and set `ANTHROPIC_API_KEY` (console.anthropic.com; set a spend limit) and, optionally, `GNEWS_API_KEY` (gnews.io free tier).
  3. Set `EXA_API_KEY` (dashboard.exa.ai; free plan gives $10 of credits a month) and `NYT_API_KEY` (developer.nytimes.com → Apps → enable Article Search API). With Exa set, Google News and GNews are skipped.
  4. Without `ANTHROPIC_API_KEY` the app still runs and ranks articles by source and date. Without `EXA_API_KEY` it uses Google News and GNews (`GNEWS_API_KEY`). Without `NYT_API_KEY` it skips NYT.
  5. Set `SUPABASE_URL` and `SUPABASE_PUBLISHABLE_KEY` (required: without them the sign-in page says sign-in isn't set up and the API refuses requests). One-time Supabase setup is under Shipping below. Never add the Supabase secret key; nothing uses it.
- **Run locally:** `python3 app/server.py` from the repo root, then open http://localhost:8000 (Ctrl+C to stop). The first lines printed say which sources (Exa, NYT, or Google News/GNews), Claude scoring and sign-in are on.
- **Run deterministic tests:** `python3 -m unittest discover tests` (no internet or keys needed; costs nothing).
- **Run model evals:** Not built yet (planned for Build 4).
- **Cost:** Exa search about $0.03 per category (two searches of 10 results with highlights; Exa's own estimate is shown on the page), so about $0.10 per 3-category newsletter. NYT is free. Claude: about $0.001–$0.0024 per category searched (20 articles scored with the four-part rubric), about $0.003–$0.007 per 3-category newsletter. Shown on the page under each category.

## 4. Ask me first
Stop and ask the human before any of these, even mid-task:
- Deleting data or changing the database (anything in `supabase/schema.sql`, or running SQL).
- Keys, secrets and permissions (adding or changing keys, Supabase auth settings, RLS policies, who can sign up).
- Anything that costs money (new paid APIs, raising rate limits, bigger result counts, paid hosting plans).
- Pushing to `main` (it deploys the live site).

## 5. Shipping
**The loop, every change:**
1. Run the tests before every commit: `python3 -m unittest discover tests`.
2. Commit with a clear message and push to `main` (after the human's OK). Pushing to `main` deploys the live site on Render.
3. Open the live URL and check what changed (Render needs 1–2 minutes; check Events/Logs there if it fails).
4. Tell the human what is live and what you checked.

**Supabase (one-time, in the dashboard at supabase.com):**
1. New project (free plan). Save the database password somewhere safe; the app never uses it.
2. SQL Editor → New query → paste all of `supabase/schema.sql` → Run. Re-run it whenever the file changes.
3. Authentication → Sign In / Providers → Email: keep Email on, turn **Confirm email off**. The free plan's built-in email only sends to your own Supabase team and about 2 emails an hour, so other people's confirmation emails wouldn't arrive. To turn it back on later, connect your own SMTP service first (Authentication → Emails → SMTP).
4. Authentication → URL Configuration: Site URL = the app's URL (`http://localhost:8000` until it's hosted, then the live URL). Add the redirect URLs `http://localhost:8000/**` and, once hosted, `https://<live-host>/**`.
5. Project Settings → API Keys: copy the Project URL and the **publishable** key into `.env` as `SUPABASE_URL` and `SUPABASE_PUBLISHABLE_KEY`.
6. Free projects pause after about a week with no activity; un-pause from the dashboard before a demo.

**Before every push:**
1. `python3 -m unittest discover tests` passes.
2. `git status` shows no `.env` and nothing under `app/data/`.
3. Manual check: sign in with two accounts in two browsers (or a private window); each sees only its own saved clients and past newsletters.
4. Before a schema change: back up first (Supabase → Table Editor → each table → Export → CSV, saved outside the repo; the free plan has no automatic backups). After it: re-run `supabase/schema.sql` in the SQL Editor (ask first), then `python3 tests/live_rls_check.py --server` (add `--site https://<live-host>` for the live site and `--limits` for the write limits).

**Hosting (Render, free plan):**
- Render runs `python3 app/server.py`. Render sets a `PORT` environment variable; when it's present the server listens on `0.0.0.0` on that port (`listen_address()` in `server.py`). Locally nothing changes: `127.0.0.1:8000`.
- Web Service settings: Language **Python 3**, Branch **main**, Root Directory blank, Build Command `pip install -r requirements.txt` (the file lists no packages; it's there for Render's standard build step), Start Command `python3 app/server.py`, Instance Type **Free**, Health Check Path `/api/config` (Settings → Health & Alerts).
- Environment variables on Render (names only; never from git). Use a **separate Anthropic key just for the live site, with a low spend limit**, so a problem there can't drain your main key: `ANTHROPIC_API_KEY`, `EXA_API_KEY`, `NYT_API_KEY`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`. (`GNEWS_API_KEY` only matters without Exa.) Never add the Supabase secret key.
- After the first deploy: add the live URL to Supabase → Authentication → URL Configuration (Site URL = live URL; redirect URLs keep `http://localhost:8000/**` and add `https://<live-host>/**`), then sign in on the live URL.
- Connect the repo through GitHub (Render asks to install its GitHub app; if the repo is missing, request access for the building-with-ai-f26 organization). Every push to `main` then redeploys automatically.
- Free plan limits: the service sleeps after about 15 minutes idle (the next visit takes 30–60 s to wake), and its disk is wiped on every restart or deploy. That's why everything the app saves lives in Supabase.
- Cost protection: with "Confirm email" off, anyone with the URL could create an account and spend API credits. Once your own and your testers' accounts exist, turn off Supabase → Authentication → Sign In / Providers → **Allow new users to sign up**. Keep spend limits on Anthropic (console.anthropic.com → Limits); Exa's free plan stops at its $10 monthly credit without a card on file.

## 6. Non-Negotiable Engineering Rules
1. **Plan before coding:** Before touching multiple files, propose a brief plan and wait for approval.
2. **Small batches:** Make small, focused changes that can be reviewed and explained by a human in a single Pull Request.
3. **Explainability:** Avoid unnecessary abstractions or dependencies. Help the human builder understand the main components, what changed, why it matters, what could break, and how it was checked.
4. **Verify changes:** Run relevant checks. For a reproducible logic bug, use a failing test to demonstrate the fix when practical. Record manual checks and known limitations; follow the selected Build 4 evaluation track.
5. **Zero secrets in code:** Never hardcode API keys, tokens, or credentials. Always read from environment variables (`.env`) and update `.env.example` when new variables are needed.
6. **Stop when looping:** If a fix fails twice in a row, stop, summarize what failed, and ask the human before trying a third approach.
