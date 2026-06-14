-- =============================================================================
-- AI durable-cache tables — app_kv + the result-cache stores.
-- =============================================================================
-- These back every billable Anthropic generation so each one is paid for ONCE
-- and reused across Railway redeploys. If a table is missing, the code silently
-- falls back to ephemeral output/*.json (wiped on every deploy) and the same
-- stock RE-BILLS. The app does NOT auto-create them — see app.py
-- `_CACHE_TABLE_SQL` / `_check_research_caches()` (it only probes + warns).
-- Apply this once per environment.
--
--   app_kv         — why_today (Opus narratives), dependencies, sector_graph,
--                    operating_kpis, pdf_narrative, feedback, insider …
--   stock_overview — AI Overview snapshots
--   stock_research — Deep-Dive research notes
--   stock_compare  — head-to-head compare cards
--   desk_report    — market desk pre/post reports
--
-- Idempotent: CREATE TABLE IF NOT EXISTS — safe to re-run, a no-op on tables
-- that already exist (it will NOT touch their data, columns, or RLS).
--
-- Schemas mirror app.py `_CACHE_TABLE_SQL` exactly so the startup health check
-- (`_check_research_caches`) reports every table as reachable.
-- =============================================================================

CREATE TABLE IF NOT EXISTS app_kv (
    env_id      int          NOT NULL,
    ns          text         NOT NULL,              -- namespace, e.g. 'why_today_v4'
    k           text         NOT NULL,              -- entry key, e.g. the ticker
    v           jsonb        DEFAULT '{}'::jsonb,
    updated_at  timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (env_id, ns, k)
);

CREATE TABLE IF NOT EXISTS stock_overview (
    env_id        int          NOT NULL,
    ticker        text         NOT NULL,
    generated_at  timestamptz  NOT NULL DEFAULT now(),
    model         text,
    markdown      text,
    sources       jsonb        DEFAULT '[]'::jsonb,
    status        text         DEFAULT 'ready',
    PRIMARY KEY (env_id, ticker)
);

CREATE TABLE IF NOT EXISTS stock_research (
    env_id        int          NOT NULL,
    ticker        text         NOT NULL,
    generated_at  timestamptz  NOT NULL DEFAULT now(),
    model         text,
    markdown      text,
    sources       jsonb        DEFAULT '[]'::jsonb,
    status        text         DEFAULT 'ready',
    PRIMARY KEY (env_id, ticker)
);

CREATE TABLE IF NOT EXISTS stock_compare (
    env_id        int          NOT NULL,
    ticker        text         NOT NULL,
    generated_at  timestamptz  NOT NULL DEFAULT now(),
    model         text,
    card          jsonb        DEFAULT '{}'::jsonb,
    sources       jsonb        DEFAULT '[]'::jsonb,
    status        text         DEFAULT 'ready',
    PRIMARY KEY (env_id, ticker)
);

CREATE TABLE IF NOT EXISTS desk_report (
    env_id        int          NOT NULL,
    kind          text         NOT NULL,
    edition_date  text,
    report        jsonb        DEFAULT '{}'::jsonb,
    updated_at    timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (env_id, kind)
);

COMMENT ON TABLE app_kv IS
    'Durable KV cache for AI generations (why_today/Opus, dependencies, sector_graph, operating_kpis, pdf_narrative). Keyed (env_id, ns, k). Written with the service_role key.';

-- ── Row level security (optional hardening — apply deliberately) ─────────────
-- The server reads/writes these with SUPABASE_SERVICE_KEY, which bypasses RLS,
-- so the app works with or without RLS. Left OFF here to guarantee this file is
-- a pure no-op on an already-working prod and never breaks a read path.
--
-- To harden a FRESH environment (stop the public anon key reading/poisoning the
-- caches), enable RLS with NO write policy. Only enable this if NOTHING reads
-- these tables via the anon key from the browser (this app is server-mediated,
-- so that holds). Mirror db/2026-05-23-event-summaries.sql if you DO need a
-- public read policy.
--
--   ALTER TABLE app_kv         ENABLE ROW LEVEL SECURITY;
--   ALTER TABLE stock_overview ENABLE ROW LEVEL SECURITY;
--   ALTER TABLE stock_research ENABLE ROW LEVEL SECURITY;
--   ALTER TABLE stock_compare  ENABLE ROW LEVEL SECURITY;
--   ALTER TABLE desk_report    ENABLE ROW LEVEL SECURITY;
