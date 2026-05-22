-- =============================================================================
-- Reset BOTH env_id portfolios so they rebuild fresh with honest today-dated
-- picks. Run this AFTER deploying the code change that removes the simulated-
-- entry backdating (commit dropping the `is_first_build` backdate path).
-- =============================================================================
--
-- Background: the launch-day "first build" path was backdating 30 days and
-- inventing simulated entry prices via momentum_1m. That made the chart show
-- a fake 30-day track record. Removed on 2026-05-22.
--
-- This SQL wipes the picks arrays on rows id=1 (prod) and id=2 (dev), then
-- the next API hit on each environment triggers a true first build with the
-- new code: today's date, today's prices, no simulated flag.
--
-- IMPORTANT: closed_trades is untouched — your backfilled LSCC + AAOI wins
-- stay in the ledger. Only the active portfolio resets.
--
-- Safe to re-run.
-- =============================================================================

UPDATE model_portfolio_state
SET payload    = '{"created_at":null,"version":2,"picks":[]}'::jsonb,
    updated_at = now()
WHERE id IN (1, 2);

-- Verify: should show both rows with picks=[] and updated_at just now
SELECT id,
       payload->>'created_at' AS created_at,
       jsonb_array_length(payload->'picks') AS pick_count,
       updated_at
FROM model_portfolio_state
ORDER BY id;
