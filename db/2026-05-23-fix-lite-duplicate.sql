-- =============================================================================
-- Fix LITE duplication — it shows in BOTH the active portfolio AND the
-- closed-trades log after the May 23 backfill.
-- =============================================================================
--
-- Background: the bar_failed rule on May 21 wrote close records for 7 picks
-- (MU, MPWR, BE, RKLB, CRDO, MRVL, LITE). The rule was reverted hours later
-- but the disk-based close records persisted until the next deploy wiped them.
--
-- During the May 22 Supabase consolidation rebuild, the new top-12 selection
-- happened to re-admit LITE (its smart_score was still in the top tier). The
-- other 6 were not re-admitted and stayed out.
--
-- My May 23 backfill (db/2026-05-23-backfill-bar-failed-closures.sql) inserted
-- all 7 into closed_trades, which double-counts LITE — it's now active AND
-- shown as a +12.8% closed booking, which the audit drill-down surfaces as a
-- duplicate row.
--
-- Honest resolution: delete the LITE backfill row. The active record already
-- covers LITE's real continuous journey. The other 6 backfill rows are
-- correct and stay.
--
-- Safe to re-run.
-- =============================================================================

DELETE FROM closed_trades
WHERE env_id      = 1
  AND ticker      = 'LITE'
  AND exit_reason = 'target'
  AND exit_date   = '2026-05-21';

-- Verify: should show only 6 May-21 closures (MU, MPWR, BE, RKLB, CRDO, MRVL)
-- and the 2 May-22 closures (LSCC, AAOI).
SELECT exit_date, ticker, final_pct
FROM closed_trades
WHERE env_id = 1 AND exit_reason = 'target'
ORDER BY exit_date DESC, final_pct DESC;
