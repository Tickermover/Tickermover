-- =============================================================================
-- Purge the May 22 "rebuild" closures from closed_trades.
-- =============================================================================
-- Why these need to go:
--   Yesterday's move-existing-to-closed.sql expanded every pick from the
--   active portfolio into closed_trades with exit_reason='rebuild'. But those
--   picks were SIMULATED entries from the launch-day backdate path — we never
--   actually held those positions. Recording them as exits creates a fake
--   audit trail (rows showing 'entry=May22, exit=May22, 0d held, 0% final').
--
--   The user flagged 'RGTI/QUBT were never part since Apr 11' — exactly
--   because those tickers never had a real position. Easiest fix is to
--   delete those rows. The audit log starts clean from May 23 onwards.
--
-- What stays:
--   - Real target hits backfilled from the launch-day +100% wins (LSCC, AAOI)
--   - Any future exits that fire under real exit rules (hard stop, +100%
--     target, trail stop, signal exit, valuation stretched)
--
-- Safe to re-run.
-- =============================================================================

-- Show what we're about to delete
SELECT 'BEFORE DELETE' AS phase, env_id, ticker, exit_date, exit_reason
FROM closed_trades
WHERE exit_reason = 'rebuild' AND exit_date = '2026-05-22'
ORDER BY env_id, ticker;

-- Delete
DELETE FROM closed_trades
WHERE exit_reason = 'rebuild' AND exit_date = '2026-05-22';

-- Verify
SELECT 'AFTER DELETE' AS phase, env_id, COUNT(*) AS remaining_trades
FROM closed_trades
GROUP BY env_id
ORDER BY env_id;
