# Security audit

**App:** Press Pass · **Live:** https://ss11807-battlecardbuilder.onrender.com · **Lane:** Beginner (Ship it)
**Audited:** November 22, 2026, on the live site and locally · **Re-run:** `python3 -m unittest discover tests` (offline) and `python3 tests/live_rls_check.py --site https://ss11807-battlecardbuilder.onrender.com --limits` (live)

## My kind of app

- **People sign in.** Email + password through Supabase Auth ("Confirm email" off, because the free plan can't send confirmation emails to outside addresses).
- **It saves data per user** in Supabase Postgres: saved client setups (`client_profiles`), past newsletters (`newsletters`) and article-removal notes (`feedback`). Every row has a `user_id` the database fills in from the sign-in.
- **It calls paid APIs from the server:** Anthropic Claude (scoring, summaries, PDF reading), Exa (news search) and NYT Article Search (free). Keys live only in Render's environment variables (live) or a git-ignored `.env` (local).
- **Hosting:** a Python standard-library server on Render's free plan, deployed from `main`. No third-party Python packages. The browser loads one library from a CDN (`@supabase/supabase-js`) plus the PostHog analytics snippet.

So the test that fits (slide 31) is **"People sign in": log in as a second account and try to read the first one's data.**

## What I checked

### 1. Row Level Security on every table

- RLS is on for all three tables (`supabase/schema.sql`). Signed-out visitors (`anon`) have no access at all; signed-in users get only what each table needs.
- Policies, in plain English:
  - `client_profiles`, `newsletters`: you can read, add, edit and delete **only rows whose `user_id` is you**; you can't add a row for someone else or hand your row to someone else.
  - `feedback`: you can read, add and delete only your own notes. **Nobody can edit a note** (no update policy or grant), because notes never need changing.
- **Tested on the live project** with throwaway accounts (`tests/live_rls_check.py --site … --limits`): **44/44 checks passed**, including the write limits below. Account A asking for B's rows (whole table or by exact id) got nothing; A could not change, delete, plant or take over B's rows in any table; B's rows were unchanged afterwards; a signed-out visitor saw no rows.
- **In the browser on the live site:** the owner's account shows its past newsletter (Ledger, Nov 22); a second, new account shows **"No newsletters yet"** and no saved clients. Screenshot submitted. Also opened and signed in on a phone.

### 2. Secret keys stay on the server

- Downloaded every file the live site serves (`/`, `/signin.html`, `/auth.js`, `/api/config`) and searched them for `sb_secret_`, `service_role`, `sk-or-`, `sk-ant-`, `sk-proj-`, `sk_live_` **and the actual values of every key in `.env`**: none found.
- `/api/config` sends the browser only the Supabase URL and the **publishable** key (designed to be public; RLS protects the data). The Supabase secret key isn't used anywhere and isn't on Render.
- **Searched the repo too:** all 48 files and every version of every file in the git history, for the same patterns and the actual key values. The only matches are documentation, `.env.example` placeholders (`sk-ant-replace-with-your-key`) and a fake value in a test; **no real key has ever been committed**, so nothing needed rotating. (The PostHog `phc_` project key in the pages is public by design.)
- Tried to download `.env` from the live site (`/.env`, `/../.env`, `/%2e%2e/.env`, `/..%2f.env`): all refused (404/400).
- The live site has its **own Anthropic key with a $5 spend limit**, separate from the local one (slide 34).

### 3. Private storage

- No file storage is used. Uploaded PDFs go to Claude for one summary and are never written to disk or the database.
- Everything the app saves is in Supabase behind RLS (Render's disk is wiped on every restart anyway).

### 4. Production out of the agent's reach

- The coding agent never had the Supabase secret key, the database password or Render access. Database changes (`supabase/schema.sql`) were run by me in the Supabase SQL Editor after reviewing them; Render settings and keys were entered by me.
- `AGENTS.md` has an **Ask me first** list: database changes, keys/secrets/permissions, anything that costs money, and pushing to `main` (which deploys).
- **Manual backup** (Supabase's free plan has no automatic backups): on Nov 22 I exported every table with data as CSV from the dashboard (`client_profiles`, `newsletters`; `feedback` was still empty) plus the older local feedback notes, into a folder outside the repo, since it holds users' data. The table structure and rules are in git (`supabase/schema.sql`).

### 5. Spending caps and rate limits

- **Hard caps at the providers:** a $5 spend limit on the live Anthropic key; Exa's free plan stops at its $10 monthly credit (no card on file); NYT is free.
- **Rate limit on the AI routes:** each account may make 30 news searches and 10 PDF reads per hour (`RATE_LIMITS` in `app/server.py`). **Tested live:** 11 PDF requests from one account → the 11th got `429 Limit reached: 10 PDF reads per hour`.
- **Sign-in required for every paid route:** tested live, requests with no token or a fake token get `401` and cost nothing.
- **Limits on database writes:** the page writes to Supabase directly, so the database itself refuses new rows past a per-account limit: 60 saved clients, 60 newsletters and 300 feedback notes per hour, and 500 / 1,000 / 5,000 in total (a trigger in `supabase/schema.sql`, which also sets `created_at` itself so a backdated time can't dodge the count). **Tested live:** 60 newsletters and 60 saved clients went through, the 61st of each was refused, a backdated row got the real time, and editing an existing row still worked.

### 6. Dependencies

- **Python:** standard library only (`requirements.txt` lists no packages), so `pip-audit` has nothing to scan. There's no `package.json`, so `npm audit` doesn't apply either.
- **Browser:** the one library the pages load, `@supabase/supabase-js`, and its bundled parts, checked against the OSV vulnerability database (the same data `npm audit` uses). Nothing high-severity; one low-severity advisory, fixed below.

### 7. Agent attack review

I asked my coding agent to attack the app. What it tried, and what happened:

| Attack | Result |
|---|---|
| Read, change, delete or plant another user's rows with the public key | Blocked by RLS (39/39 live) |
| Call the paid routes with no token or a forged token | `401`, nothing spent |
| Run up the bill from one account | `429` after the hourly limit |
| Download `.env`, server code or old notes, including `../` path tricks | Refused (after the fix below) |
| Inject HTML/script through article headlines or summaries from Exa/NYT | Shown as text: every field is escaped before it's put on the page |
| Load Press Pass invisibly inside another site to trick clicks (clickjacking) | Blocked (after the fix below) |
| Swap the CDN copy of the sign-in library for a malicious one | Browser refuses it (after the fix below) |
| Hide instructions inside an article for Claude (prompt injection) | Claude can only return scores and summary text in a fixed JSON shape; the worst case is a misleading score or summary, which the user reviews before sending |

## What I found

1. **`/server.py`, `feeds.json` and `outlets.json` were downloadable from the live site.** No secrets were in them (checked: zero key values; identical to the public GitHub repo), but a site shouldn't serve its own code: a key pasted into code by mistake would have leaked.
2. **No rate limit on the AI routes** (before this week's changes): any signed-in account could call Claude and Exa without limit.
3. **Feedback notes were saved to a file**, which Render deletes on every restart, and which wasn't per user.
4. **`@supabase/supabase-js` 2.45.4 bundled `@supabase/auth-js` 2.65.0**, which has a low-severity advisory (GHSA-8r88-6cj9-9fh5, fixed in 2.70.0). It affects admin functions that need the secret key, which Press Pass never uses, so it wasn't exploitable here.
5. **The CDN library had no integrity hash**, so a tampered copy on the CDN would have run on the sign-in page.
6. **No clickjacking protection:** any site could frame Press Pass.
7. **No limit on database writes.** The paid routes had a rate limit, but the page writes to Supabase directly, so one account could add rows without limit and fill the free plan's 500 MB.
8. **No backup.** Supabase's free plan doesn't make automatic backups.

## What I fixed

1. The server now serves **only** `index.html`, `signin.html` and `auth.js`; every other path (GET or HEAD) is a 404. Re-checked live: `/server.py`, `/feeds.json`, `/outlets.json` → 404. (commit `ca1e3d8`, tests in `tests/test_hosting.py`)
2. **Per-user hourly limits** on `/api/news` (30) and `/api/pdf` (10), tested locally and live. (commit `73f7280`)
3. **Feedback moved to a Supabase `feedback` table** with RLS (read/add/delete own, no edits), tested live. (commit `73f7280`)
4. **Upgraded to `@supabase/supabase-js` 2.119.0** (0 known vulnerabilities in it or its bundled `auth-js`), after testing every Supabase call the pages make against the real project.
5. **Pinned it with an SRI integrity hash**, using the package's original `dist/umd/supabase.js` (jsDelivr generates `.min.js` on the fly, which can't be pinned safely). Verified the file is byte-identical on jsDelivr and unpkg.
6. **Security headers** on every response: `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`.
7. **Database write limits** per account (per hour and in total), enforced by the database and tested live.
8. **Manual backup** of every table with data, kept outside the repo.

## What is left

- **Open sign-ups.** With "Confirm email" off, anyone with the link can create accounts, and the rate limit is per account. The spend caps bound the damage. Plan: keep sign-ups on through pod testing (Nov 24), then turn off "Allow new users to sign up" in Supabase.
- **The rate limit lives in server memory**, so it resets when Render restarts or redeploys.
- **One Supabase project serves both local testing and the live site** (no separate dev and prod). Test accounts are throwaway and cleaned up after each check.
- **Backups are manual:** the Nov 22 export is the only one. Export again before any database change (now a step in `AGENTS.md`) and every week or so.
- **The total-row limits (500 / 1,000 / 5,000) weren't tested live**, because reaching them needs hours of inserts under the hourly limit; they use the same code path as the hourly limit, which was tested.
- **Supabase Auth has its own rate limits** on sign-ups and sign-ins (Authentication → Rate Limits). I haven't changed or reviewed them; they're whatever Supabase sets by default.
- **PostHog session replays** record page text such as client names and article headlines (typed inputs and the email in the header are masked). The page tells users it records replays.
- **Third parties see search inputs:** category names, search terms and descriptions go to Exa and NYT; client and article details go to Claude for scoring.
- **The PostHog script loads without an integrity hash** (PostHog updates it continually, so it can't be pinned).
- **Draft HTML is rendered as-is when you reopen your own past newsletter.** RLS means only you can write it, so the only risk is to yourself (self-XSS).
- **No Content-Security-Policy for scripts yet** (only `frame-ancestors`); a full policy would need to allow PostHog and jsDelivr.
