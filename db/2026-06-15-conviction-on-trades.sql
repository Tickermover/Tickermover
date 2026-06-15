-- =============================================================================
-- closed_trades.conviction_at_entry — AI analyst-judge conviction at entry.
-- =============================================================================
-- Freezes the Opus 4.8 judge's conviction (0-100) at the moment a pick entered
-- the Top Hunts tracker, so the closed-trades view can measure whether
-- high-conviction picks actually outperformed (forward evidence — the only
-- legitimate way to validate the AI re-rank, since the judge can't be
-- reconstructed historically).
--
-- Idempotent: ADD COLUMN IF NOT EXISTS — safe to re-run. Apply once per env
-- (prod = env_id 1, dev = env_id 2). Until applied, persistence.append_trades
-- retries the insert WITHOUT this field so the audit ledger keeps writing.
-- =============================================================================

ALTER TABLE closed_trades
    ADD COLUMN IF NOT EXISTS conviction_at_entry int;

COMMENT ON COLUMN closed_trades.conviction_at_entry IS
    'AI analyst-judge conviction 0-100 captured at entry (ai_selector.py). Null for picks added before the AI layer existed.';
