-- Press Pass: tables for signed-in users.
-- Run this once in the Supabase dashboard: SQL Editor → New query → paste → Run.
-- Safe to re-run: it drops and recreates the policies, and only creates
-- tables that don't exist yet (it never deletes data).
--
-- Every row belongs to one user (user_id). The database fills user_id in with
-- auth.uid(), the id of whoever is signed in, so the browser never sends it.
-- Row Level Security (RLS) then makes every read and write check that the row's
-- user_id matches the signed-in user.

-- ---------------------------------------------------------------------------
-- Saved client setups: everything in step 1, so it can be loaded again later.
-- ---------------------------------------------------------------------------
create table if not exists public.client_profiles (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null default auth.uid() references auth.users (id) on delete cascade,
  client_name      text not null check (char_length(client_name) between 1 and 200),
  companies        text[] not null default '{}',   -- competitors / companies to watch
  priority_sources text[] not null default '{}',   -- domains such as reuters.com
  categories       jsonb  not null default '[]',   -- [{ "name", "terms", "exclude" }]
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (user_id, client_name)                    -- one saved setup per client per user
);

-- ---------------------------------------------------------------------------
-- Past newsletters: saved when a draft is copied in step 3.
-- ---------------------------------------------------------------------------
create table if not exists public.newsletters (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null default auth.uid() references auth.users (id) on delete cascade,
  client_name text not null check (char_length(client_name) between 1 and 200),
  categories  jsonb not null default '[]',         -- category names, in newsletter order
  articles    jsonb not null default '[]',         -- included articles: headline, url, source, date, summary, relevance
  draft_html  text  not null check (char_length(draft_html) <= 500000),  -- the edited draft
  ai_cost_usd numeric(10, 6) not null default 0,   -- Claude cost of the searches behind it
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create index if not exists newsletters_user_created_idx on public.newsletters (user_id, created_at desc);

-- ---------------------------------------------------------------------------
-- Who may use the tables at all: signed-in users yes, anonymous visitors no.
-- ---------------------------------------------------------------------------
revoke all on public.client_profiles, public.newsletters from anon;
grant select, insert, update, delete on public.client_profiles, public.newsletters to authenticated;

-- ---------------------------------------------------------------------------
-- Row Level Security: each user can only see and change their own rows.
-- (select auth.uid()) is the signed-in user's id; the extra "select" lets
-- Postgres work it out once per query instead of once per row.
-- ---------------------------------------------------------------------------
alter table public.client_profiles enable row level security;
alter table public.newsletters     enable row level security;

drop policy if exists "Users read their own client setups"   on public.client_profiles;
drop policy if exists "Users add their own client setups"    on public.client_profiles;
drop policy if exists "Users edit their own client setups"   on public.client_profiles;
drop policy if exists "Users delete their own client setups" on public.client_profiles;

create policy "Users read their own client setups" on public.client_profiles
  for select to authenticated
  using ((select auth.uid()) = user_id);

create policy "Users add their own client setups" on public.client_profiles
  for insert to authenticated
  with check ((select auth.uid()) = user_id);

create policy "Users edit their own client setups" on public.client_profiles
  for update to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

create policy "Users delete their own client setups" on public.client_profiles
  for delete to authenticated
  using ((select auth.uid()) = user_id);

drop policy if exists "Users read their own newsletters"   on public.newsletters;
drop policy if exists "Users add their own newsletters"    on public.newsletters;
drop policy if exists "Users edit their own newsletters"   on public.newsletters;
drop policy if exists "Users delete their own newsletters" on public.newsletters;

create policy "Users read their own newsletters" on public.newsletters
  for select to authenticated
  using ((select auth.uid()) = user_id);

create policy "Users add their own newsletters" on public.newsletters
  for insert to authenticated
  with check ((select auth.uid()) = user_id);

create policy "Users edit their own newsletters" on public.newsletters
  for update to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

create policy "Users delete their own newsletters" on public.newsletters
  for delete to authenticated
  using ((select auth.uid()) = user_id);
