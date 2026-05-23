-- =============================================================================
-- event_summaries — cached Quartr-style summaries of earnings call transcripts.
-- =============================================================================
-- Produced by event_intel.py: pulls transcript from Alpha Vantage, summarizes
-- via Anthropic Haiku, persists here so subsequent reads are free + instant.
-- TTL is enforced in Python (14 days) — a row past that gets re-summarized
-- on next request.
-- =============================================================================

CREATE TABLE IF NOT EXISTS event_summaries (
    id              bigserial PRIMARY KEY,
    ticker          text        NOT NULL,
    event_title     text,
    event_date      date,
    source          text,                            -- 'alpha_vantage_transcript' etc.
    key_updates     jsonb,                           -- string[]
    operations      jsonb,                           -- string[]
    outlook         jsonb,                           -- string[]
    risks           jsonb,                           -- string[]
    raw_excerpt     text,
    summarized_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (ticker, event_date)
);

CREATE INDEX IF NOT EXISTS idx_event_summaries_ticker_date
    ON event_summaries (ticker, event_date DESC);

CREATE INDEX IF NOT EXISTS idx_event_summaries_ticker_latest
    ON event_summaries (ticker, summarized_at DESC);

COMMENT ON TABLE event_summaries IS
    'Cached LLM summaries of earnings call transcripts. One row per (ticker, event_date). 14-day TTL enforced client-side.';

-- ── Row level security ───────────────────────────────────────────────────
-- Same pattern as model_portfolio_state: writes via service_role only,
-- public reads open so the tear sheet can render without auth.
ALTER TABLE event_summaries ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "event_summaries public read" ON event_summaries;
CREATE POLICY "event_summaries public read"
    ON event_summaries FOR SELECT
    USING (true);
