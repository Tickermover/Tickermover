-- ════════════════════════════════════════════════════════════════
-- AlphaHunt Dev Supabase — schema replication from prod
-- ════════════════════════════════════════════════════════════════
-- HOW TO USE:
--   1. Create new Supabase project for dev
--   2. Open dev Supabase → SQL Editor → New query
--   3. Paste this entire file → click Run
--   4. Verify: should see "Success. No rows returned" with no errors
--
-- Replicates exactly what's in prod as of 2026-05-04:
--   • profiles table (linked to auth.users)
--   • watchlist table (linked to auth.users)
--   • RLS policies (user-can-only-touch-own-data)
--   • on_auth_user_created trigger (auto-creates profile row on signup)
-- ════════════════════════════════════════════════════════════════


-- ─── Tables ──────────────────────────────────────────────────────

CREATE TABLE public.profiles (
  id          uuid NOT NULL PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  email       text,
  plan        text DEFAULT 'free',
  created_at  timestamptz DEFAULT now(),
  updated_at  timestamptz DEFAULT now()
);

CREATE TABLE public.watchlist (
  id        bigserial PRIMARY KEY,
  user_id   uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  ticker    text NOT NULL,
  added_at  timestamptz DEFAULT now()
);

-- Index for fast watchlist lookup by user_id
CREATE INDEX idx_watchlist_user_id ON public.watchlist(user_id);

-- Prevent duplicate ticker entries per user (e.g. clicking watch twice)
CREATE UNIQUE INDEX idx_watchlist_user_ticker ON public.watchlist(user_id, ticker);


-- ─── Row Level Security ──────────────────────────────────────────
-- Without these, ANYONE could read/edit ALL users' data via the
-- public anon key. Critical to enable.

ALTER TABLE public.profiles  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.watchlist ENABLE ROW LEVEL SECURITY;

-- Profiles: users can read + update only their own row
CREATE POLICY "Users read own profile"
  ON public.profiles
  FOR SELECT
  TO public
  USING (auth.uid() = id);

CREATE POLICY "Users update own profile"
  ON public.profiles
  FOR UPDATE
  TO public
  USING (auth.uid() = id);

-- Watchlist: users can do anything (SELECT/INSERT/UPDATE/DELETE) only on their own rows
CREATE POLICY "Users manage own watchlist"
  ON public.watchlist
  FOR ALL
  TO public
  USING (auth.uid() = user_id);


-- ─── Auto-create profile row when a user signs up ───────────────
-- Trigger fires on auth.users INSERT (Supabase manages auth.users
-- internally, but we can hook it from public schema). Inserts a
-- corresponding profiles row with the new user's id + email.

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
  INSERT INTO public.profiles (id, email)
  VALUES (new.id, new.email);
  RETURN new;
END;
$$;

CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW
  EXECUTE FUNCTION public.handle_new_user();


-- ════════════════════════════════════════════════════════════════
-- Verification queries (run separately after the above to confirm)
-- ════════════════════════════════════════════════════════════════
--
-- SELECT table_name FROM information_schema.tables
-- WHERE table_schema = 'public';
-- → expect: profiles, watchlist
--
-- SELECT tablename, policyname FROM pg_policies
-- WHERE schemaname = 'public';
-- → expect: 3 rows (Users read own profile, Users update own profile,
--           Users manage own watchlist)
--
-- SELECT trigger_name FROM information_schema.triggers
-- WHERE trigger_schema = 'auth' AND event_object_table = 'users';
-- → expect: on_auth_user_created
-- ════════════════════════════════════════════════════════════════
