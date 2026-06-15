-- =============================================================================
-- selection_judgments — AI analyst-judge conviction scores for Top Hunts.
-- =============================================================================
-- Backs the advisory re-rank layer (ai_selector.py → selection_store.py). The
-- Opus 4.8 judge scores conviction for each quant-qualified candidate ~nightly;
-- selection uses it as a tiebreaker within the shortlist, and the cards surface
-- the one-line thesis + red flags. Durable so judgments survive Railway
-- redeploys; without this table the code falls back to ephemeral
-- output/selection/*.json (wiped each deploy) and the judge re-bills daily.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS — safe to re-run. Apply once per
-- environment (prod = env_id 1, dev = env_id 2).
-- =============================================================================

CREATE TABLE IF NOT EXISTS selection_judgments (
    env_id        int          NOT NULL,
    ticker        text         NOT NULL,
    generated_at  timestamptz  NOT NULL DEFAULT now(),
    model         text,
    conviction    int,                                 -- 0-100 Outperform→Avoid
    thesis        text,                                -- one-sentence rationale
    red_flags     jsonb        DEFAULT '[]'::jsonb,
    lean          text,                                -- dominant pillar
    PRIMARY KEY (env_id, ticker)
);

COMMENT ON TABLE selection_judgments IS
    'AI analyst-judge conviction scores for Top Hunts selection (advisory re-rank). Keyed (env_id, ticker). Written with the service_role key by selection_store.py.';

-- RLS left OFF to match the other cache tables — the server reads/writes with
-- SUPABASE_SERVICE_KEY (bypasses RLS). To harden a fresh environment:
--   ALTER TABLE selection_judgments ENABLE ROW LEVEL SECURITY;
