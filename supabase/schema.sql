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

-- ---------------------------------------------------------------------------
-- Feedback notes: why the user removed an article (added in Phase 3, when the
-- app moved to Render, whose disk is wiped on every restart). Later used to
-- show Claude what this user doesn't want.
-- Notes are never edited, so there is no update policy (and no update grant).
-- ---------------------------------------------------------------------------
create table if not exists public.feedback (
  id              uuid primary key default gen_random_uuid(),
  user_id         uuid not null default auth.uid() references auth.users (id) on delete cascade,
  client_name     text not null default '' check (char_length(client_name) <= 200),
  category        text not null default '' check (char_length(category) <= 200),
  headline        text not null default '' check (char_length(headline) <= 1000),
  source          text not null default '' check (char_length(source) <= 300),
  domain          text not null default '' check (char_length(domain) <= 300),
  url             text not null default '' check (char_length(url) <= 3000),
  priority_source boolean not null default false,
  manual          boolean not null default false,
  filter_reason   text not null default '' check (char_length(filter_reason) <= 300),
  ai_relevance    numeric,
  ai_relevance_parts jsonb,
  reasons         text[] not null default '{}',     -- quick picks such as "Off-topic"
  note            text not null default '' check (char_length(note) <= 2000),
  created_at      timestamptz not null default now()
);

create index if not exists feedback_user_created_idx on public.feedback (user_id, created_at desc);

revoke all on public.feedback from anon;
revoke all on public.feedback from authenticated;
grant select, insert, delete on public.feedback to authenticated;

alter table public.feedback enable row level security;

drop policy if exists "Users read their own feedback"   on public.feedback;
drop policy if exists "Users add their own feedback"    on public.feedback;
drop policy if exists "Users delete their own feedback" on public.feedback;

create policy "Users read their own feedback" on public.feedback
  for select to authenticated
  using ((select auth.uid()) = user_id);

create policy "Users add their own feedback" on public.feedback
  for insert to authenticated
  with check ((select auth.uid()) = user_id);

create policy "Users delete their own feedback" on public.feedback
  for delete to authenticated
  using ((select auth.uid()) = user_id);

-- ---------------------------------------------------------------------------
-- Write limits (security audit: "a per-visitor rate limit on every route that
-- writes to your database"). The page writes to these tables directly, so the
-- database itself refuses new rows past these limits, per account:
--
--   table             new rows per hour   rows in total
--   client_profiles          60                 500
--   newsletters              60               1,000
--   feedback                300               5,000
--
-- Editing an existing row doesn't count (it adds nothing), and every column
-- already has a size limit. created_at is always set by the database here, so
-- a backdated created_at can't be used to slip past the hourly count.
-- ---------------------------------------------------------------------------
create or replace function public.enforce_write_limits()
returns trigger
language plpgsql
security invoker            -- runs as the signed-in user, so RLS still applies
set search_path = ''
as $$
declare
  per_hour  integer;
  total_max integer;
  recent    integer;
  total     integer;
begin
  if tg_table_name = 'client_profiles' then per_hour := 60;  total_max := 500;
  elsif tg_table_name = 'newsletters'  then per_hour := 60;  total_max := 1000;
  elsif tg_table_name = 'feedback'     then per_hour := 300; total_max := 5000;
  else return new;
  end if;

  new.created_at := now();  -- never trust a time sent by the browser

  execute format(
    'select count(*) filter (where created_at > now() - interval ''1 hour''), count(*)
       from public.%I where user_id = $1', tg_table_name)
    into recent, total
    using new.user_id;

  if recent >= per_hour then
    raise exception 'Press Pass limit reached: at most % new % per hour. Try again later.',
      per_hour, replace(tg_table_name, '_', ' ') using errcode = 'P0001';
  end if;
  if total >= total_max then
    raise exception 'Press Pass limit reached: at most % % per account. Delete some old ones first.',
      total_max, replace(tg_table_name, '_', ' ') using errcode = 'P0001';
  end if;
  return new;
end;
$$;

revoke all on function public.enforce_write_limits() from public, anon, authenticated;

drop trigger if exists enforce_write_limits on public.client_profiles;
drop trigger if exists enforce_write_limits on public.newsletters;
drop trigger if exists enforce_write_limits on public.feedback;

create trigger enforce_write_limits before insert on public.client_profiles
  for each row execute function public.enforce_write_limits();
create trigger enforce_write_limits before insert on public.newsletters
  for each row execute function public.enforce_write_limits();
create trigger enforce_write_limits before insert on public.feedback
  for each row execute function public.enforce_write_limits();
