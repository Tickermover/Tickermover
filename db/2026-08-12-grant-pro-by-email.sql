-- TickerMover — grant Pro to a list of emails (test users / comped beta testers)
-- Run in Supabase: SQL Editor → New query → paste → Run. Safe to re-run.
--
-- HOW THE GATE WORKS (app.py:_is_pro_user)
--   Pro = an active row in public.subscriptions (plan='pro', status='active'),
--   OR the email is in the PRO_ALLOW_EMAILS / AI_ALLOW_EMAILS env vars.
--   The beta free-for-all (BETA_PRO_UNTIL) EXPIRED 2026-08-01, so these are
--   now the only two ways in.
--
-- LIMITATION: subscriptions is keyed by user_id (FK → auth.users), so this
--   only grants Pro to emails that ALREADY have an account. Anyone who hasn't
--   signed up yet is reported by step 1 below — either have them sign up first
--   and re-run this, or put them in PRO_ALLOW_EMAILS instead (works pre-signup).
--
-- CACHE: _pro_cache in app.py holds Pro status for 300s per user_id. A user
--   already signed in may keep seeing the paywall for up to 5 minutes after
--   this runs. Signing out and back in clears it immediately.

-- ── EDIT THIS LIST ───────────────────────────────────────────────────────────
create temporary table _wl (email text primary key);
insert into _wl (email) values
  (lower('tester1@example.com')),
  (lower('tester2@example.com')),
  (lower('tester3@example.com')),
  (lower('tester4@example.com')),
  (lower('tester5@example.com')),
  (lower('tester6@example.com')),
  (lower('tester7@example.com')),
  (lower('tester8@example.com')),
  (lower('tester9@example.com')),
  (lower('tester10@example.com'));

-- ── 1. Which of these have no account yet? (these will NOT be granted) ───────
select w.email as "no_account_yet"
from _wl w
left join auth.users u on lower(u.email) = w.email
where u.id is null;

-- ── 2. Grant Pro to everyone who does have an account ────────────────────────
insert into public.subscriptions (user_id, plan, status, valid_until)
select u.id, 'pro', 'active', timestamptz '2099-12-31 00:00:00+00'
from auth.users u
join _wl w on lower(u.email) = w.email
on conflict (user_id) do update
   set plan        = 'pro',
       status      = 'active',
       valid_until = excluded.valid_until,
       updated_at  = now();

-- ── 3. Verify — should show 'pro' / 'active' for each granted email ──────────
select u.email, s.plan, s.status, s.valid_until, s.updated_at
from public.subscriptions s
join auth.users u on u.id = s.user_id
join _wl w on lower(u.email) = w.email
order by u.email;

-- ── REVOKE (when the test window ends) ───────────────────────────────────────
-- update public.subscriptions s
--    set plan = 'free', status = 'active', valid_until = null, updated_at = now()
--   from auth.users u
--  where u.id = s.user_id
--    and lower(u.email) in ('tester1@example.com', 'tester2@example.com');
