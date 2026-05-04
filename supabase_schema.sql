-- AlphaHunt — Supabase Schema
-- Run this in your Supabase project: SQL Editor → New Query → paste + run

-- ── subscriptions ──────────────────────────────────────────────────────────────
create table if not exists public.subscriptions (
  user_id      uuid references auth.users on delete cascade primary key,
  plan         text      not null default 'free',   -- 'free' | 'pro'
  status       text      not null default 'active', -- 'active' | 'cancelled' | 'past_due'
  razorpay_sub_id  text,                            -- Razorpay subscription ID
  razorpay_order_id text,                           -- last order ID
  valid_until  timestamptz,                         -- null = free plan (no expiry)
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

-- ── watchlists ─────────────────────────────────────────────────────────────────
create table if not exists public.watchlists (
  user_id   uuid references auth.users on delete cascade,
  ticker    text not null,
  added_at  timestamptz not null default now(),
  primary key (user_id, ticker)
);

-- ── Row Level Security ──────────────────────────────────────────────────────────
alter table public.subscriptions enable row level security;
alter table public.watchlists    enable row level security;

-- Users can only read/write their own rows
create policy "subscriptions_self" on public.subscriptions
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

create policy "watchlists_self" on public.watchlists
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- ── Trigger: auto-create free subscription row on signup ──────────────────────
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

-- ── Indexes ────────────────────────────────────────────────────────────────────
create index if not exists idx_watchlists_user_id on public.watchlists (user_id);
create index if not exists idx_subscriptions_razorpay on public.subscriptions (razorpay_sub_id)
  where razorpay_sub_id is not null;
