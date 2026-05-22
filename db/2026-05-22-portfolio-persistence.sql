-- =============================================================================
-- AlphaHunt: Top Hunts portfolio + closed-trades persistence
-- Created: 2026-05-22
-- =============================================================================
-- Problem this fixes:
--   Railway's filesystem is ephemeral. Every deploy was rolling
--   data/model_portfolio.json back to its committed state and wiping
--   data/model_portfolio_history.json entirely. Result: picks vanished
--   without trade records, breaking the "every pick goes through closed
--   trades" audit promise.
--
-- This migration moves both files to Supabase. Tables are accessed only
-- by the server using SUPABASE_SERVICE_KEY (RLS-bypass), so no public
-- write access. Read access is open (anonymous users see the ledger on
-- the landing page).
--
-- To apply: paste this whole file into the Supabase SQL Editor and click
-- Run. It's idempotent — safe to re-run.
-- =============================================================================

-- ── model_portfolio_state ─────────────────────────────────────────────────
-- Single-row table holding the active Top Hunts portfolio as a JSON blob.
-- One row per environment (id=1 = prod, id=2 = dev) so prod and dev never
-- collide on the same Supabase project.
CREATE TABLE IF NOT EXISTS model_portfolio_state (
    id           smallint PRIMARY KEY,
    payload      jsonb    NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE model_portfolio_state IS
    'Top Hunts active portfolio. JSON blob shape matches data/model_portfolio.json (created_at, version, picks[]).';

-- Seed empty rows so app upserts always have a target. Safe to re-run.
INSERT INTO model_portfolio_state (id, payload) VALUES
    (1, '{"created_at":null,"version":2,"picks":[]}'::jsonb),
    (2, '{"created_at":null,"version":2,"picks":[]}'::jsonb)
ON CONFLICT (id) DO NOTHING;

-- Trigger: bump updated_at on every UPDATE so freshness is queryable
-- (the column DEFAULT only fires on INSERT, not UPDATE via PostgREST).
CREATE OR REPLACE FUNCTION model_portfolio_state_set_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_model_portfolio_state_updated_at ON model_portfolio_state;
CREATE TRIGGER trg_model_portfolio_state_updated_at
    BEFORE UPDATE ON model_portfolio_state
    FOR EACH ROW EXECUTE FUNCTION model_portfolio_state_set_updated_at();


-- ── closed_trades ─────────────────────────────────────────────────────────
-- One row per closed trade. Strict columns (not JSON) so the ledger can
-- be queried and indexed — these are the numbers the closed-trades tab
-- and the landing-page track record read from.
CREATE TABLE IF NOT EXISTS closed_trades (
    id               bigserial PRIMARY KEY,
    env_id           smallint  NOT NULL DEFAULT 1,   -- 1=prod, 2=dev (matches portfolio_state.id)
    ticker           text      NOT NULL,
    name             text,
    entry_date       date,
    entry_price      numeric(14,4),
    pop_at_entry     numeric(6,2),
    grade_at_entry   text,
    rationale        text,
    sub_sector       text,
    exit_date        date      NOT NULL,
    exit_price       numeric(14,4),
    exit_reason      text      NOT NULL,             -- target | stop | trail | stretched | bar_failed | signal | rebuild
    exit_label       text,
    exit_detail      text,
    final_pct        numeric(8,2),
    days_held        integer,
    won              boolean   NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_closed_trades_env_exitdate
    ON closed_trades (env_id, exit_date DESC);

CREATE INDEX IF NOT EXISTS idx_closed_trades_ticker
    ON closed_trades (ticker);

COMMENT ON TABLE closed_trades IS
    'Audit ledger of every Top Hunts pick that has been closed. Append-only from the app; one row per exit. Powers the closed-trades tab and the landing-page track record.';


-- ── Row-level security ────────────────────────────────────────────────────
-- Enable RLS on both tables. Server writes use SUPABASE_SERVICE_KEY which
-- bypasses RLS. Public reads are allowed so the landing page can render
-- the track record without auth.
ALTER TABLE model_portfolio_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE closed_trades         ENABLE ROW LEVEL SECURITY;

-- Read policies: anyone can SELECT
DROP POLICY IF EXISTS "model_portfolio_state public read" ON model_portfolio_state;
CREATE POLICY "model_portfolio_state public read"
    ON model_portfolio_state FOR SELECT
    USING (true);

DROP POLICY IF EXISTS "closed_trades public read" ON closed_trades;
CREATE POLICY "closed_trades public read"
    ON closed_trades FOR SELECT
    USING (true);

-- No INSERT / UPDATE / DELETE policies means anon + authenticated roles
-- can only read. The service_role key bypasses RLS entirely.
