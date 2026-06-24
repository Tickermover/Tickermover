-- =============================================================================
-- weekly_editorial — the signed weekly long-form editorial (Opus 4.8).
-- =============================================================================
-- One frozen edition per ISO week. Backs weekly_editorial_store.py. Durable so
-- the edition survives Railway redeploys; without this table the code falls back
-- to ephemeral output/weekly/*.json (wiped each deploy) and Opus re-bills.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS — safe to re-run. Apply once per
-- environment (prod = env_id 1, dev = env_id 2).
-- =============================================================================

CREATE TABLE IF NOT EXISTS weekly_editorial (
    env_id        int          NOT NULL,
    week_start    text         NOT NULL,            -- ISO date of the Monday this edition covers
    generated_at  timestamptz  NOT NULL DEFAULT now(),
    model         text,
    article       jsonb        NOT NULL,            -- {title, standfirst, body_markdown, pull_quote, house_view, tickers, subject, sources, ...}
    status        text         DEFAULT 'ready',
    PRIMARY KEY (env_id, week_start)
);

COMMENT ON TABLE weekly_editorial IS
    'Signed weekly long-form editorial (Opus 4.8). One frozen edition per ISO week, keyed (env_id, week_start). Written with the service_role key by weekly_editorial_store.py.';
