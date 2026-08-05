-- TickerMover — create the subscriptions/watchlists tables
-- Run in Supabase: SQL Editor → New query → paste → Run.
--
-- WHY THIS EXISTS
--   Stripe checkout worked, the webhook was signed and delivered, and the plan
--   write came back HTTP 404 PGRST205: "Could not find the table
--   'public.subscriptions' in the schema cache". docs/prod/supabase_schema.sql
--   had never been applied to this project. Nothing showed it, because
--   get_subscription() falls back to {plan:'free'} whenever the read isn't a
--   list — so a missing table reads exactly like "everyone is on the free plan".
--
--   This is docs/prod/supabase_schema.sql made safely re-runnable (the original
--   errors on a second run, because `create policy` has no IF NOT EXISTS), plus
--   a backfill for accounts that signed up before the table existed.

-- ── subscriptions ────────────────────────────────────────────────────────────
create table if not exists public.subscriptions (
  user_id           uuid references auth.users on delete cascade primary key,
  plan              text        not null default 'free',    -- 'free' | 'pro'
  status            text        not null default 'active',  -- 'active' | 'cancelled' | 'past_due'
  razorpay_sub_id   text,
  razorpay_order_id text,
  valid_until       timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

-- ── watchlists ───────────────────────────────────────────────────────────────
create table if not exists public.watchlists (
  user_id  uuid references auth.users on delete cascade,
  ticker   text not null,
  added_at timestamptz not null default now(),
  primary key (user_id, ticker)
);

-- ── Row Level Security ───────────────────────────────────────────────────────
-- Users reach their own row only. The Stripe webhook has no user token and
-- writes with the service_role key, which bypasses RLS — so SUPABASE_SERVICE_KEY
-- must be set in the app environment or paid upgrades cannot be recorded.
alter table public.subscriptions enable row level security;
alter table public.watchlists    enable row level security;

drop policy if exists "subscriptions_self" on public.subscriptions;
create policy "subscriptions_self" on public.subscriptions
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

drop policy if exists "watchlists_self" on public.watchlists;
create policy "watchlists_self" on public.watchlists
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- ── Auto-create a free row on signup ─────────────────────────────────────────
create or replace function public.handle_new_user()
returns trigger language plpgsql security definer as $$
begin
  insert into public.subscriptions (user_id, plan, status)
  values (new.id, 'free', 'active')
  on conflict (user_id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute procedure public.handle_new_user();

-- ── Backfill ─────────────────────────────────────────────────────────────────
-- The trigger only fires for NEW signups; everyone who registered while the
-- table was missing has no row at all.
insert into public.subscriptions (user_id, plan, status)
  select id, 'free', 'active' from auth.users
  on conflict (user_id) do nothing;

-- ── Indexes ──────────────────────────────────────────────────────────────────
create index if not exists idx_watchlists_user_id on public.watchlists (user_id);
create index if not exists idx_subscriptions_razorpay on public.subscriptions (razorpay_sub_id)
  where razorpay_sub_id is not null;

-- Sanity check — expect one row per registered user.
select count(*) as subscription_rows from public.subscriptions;
