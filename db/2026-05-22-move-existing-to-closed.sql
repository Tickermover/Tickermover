-- =============================================================================
-- Move every currently-active pick (both env_id=1 prod and env_id=2 dev) into
-- closed_trades with exit_reason='rebuild', then reset both portfolio rows so
-- the next API hit rebuilds fresh under the restored backdate-on-first-build
-- behaviour.
-- =============================================================================
--
-- Why: the user prefers the 30-day simulated lookback (more visual interest in
-- the chart), but wanted current "stale" picks moved out of the active list
-- and into the closed-trades audit log so the new fresh build starts clean.
--
-- Each moved row gets:
--   exit_reason = 'rebuild'
--   exit_label  = 'REBUILD'
--   exit_price  = entry_price (administrative close, not an exit by rule)
--   final_pct   = 0 (no claim of performance — they didn't actually hit a rule)
--   exit_date   = today
--
-- Closed_trades log = previous LSCC/AAOI wins + these admin closes.
--
-- Safe to re-run only because the UPDATE wipes the source. If you re-run BEFORE
-- the new portfolio is populated, you'll get zero rows (idempotent end state).
-- =============================================================================

-- Step 1: expand picks → closed_trades
INSERT INTO closed_trades (
    env_id, ticker, name, entry_date, entry_price,
    pop_at_entry, grade_at_entry, rationale, sub_sector,
    exit_date, exit_price, exit_reason, exit_label, exit_detail,
    final_pct, days_held, won
)
SELECT
    s.id                                         AS env_id,
    p->>'ticker'                                 AS ticker,
    p->>'name'                                   AS name,
    NULLIF(p->>'added_date','')::date            AS entry_date,
    NULLIF(p->>'entry_price','')::numeric        AS entry_price,
    NULLIF(p->>'pop_at_entry','')::numeric       AS pop_at_entry,
    p->>'grade_at_entry'                         AS grade_at_entry,
    LEFT(COALESCE(p->>'rationale',''), 1000)     AS rationale,
    p->>'sub_sector'                             AS sub_sector,
    CURRENT_DATE                                 AS exit_date,
    NULLIF(p->>'entry_price','')::numeric        AS exit_price,   -- admin close, no real exit price
    'rebuild'                                    AS exit_reason,
    'REBUILD'                                    AS exit_label,
    'Administrative close — moved out of active tracker during May 22 rebuild' AS exit_detail,
    0::numeric                                   AS final_pct,
    GREATEST(0, CURRENT_DATE - NULLIF(p->>'added_date','')::date) AS days_held,
    false                                        AS won
FROM model_portfolio_state s,
     jsonb_array_elements(s.payload->'picks') p
WHERE jsonb_array_length(s.payload->'picks') > 0;

-- Step 2: reset both portfolio rows so the next API hit rebuilds fresh
UPDATE model_portfolio_state
SET payload    = '{"created_at":null,"version":2,"picks":[]}'::jsonb,
    updated_at = now()
WHERE id IN (1, 2);

-- Step 3: verify
SELECT 'portfolio_state' AS table_name, id AS env_id,
       jsonb_array_length(payload->'picks') AS pick_count, updated_at
FROM model_portfolio_state
UNION ALL
SELECT 'closed_trades (rebuild today)', env_id, COUNT(*)::int, MAX(created_at)
FROM closed_trades
WHERE exit_reason = 'rebuild' AND exit_date = CURRENT_DATE
GROUP BY env_id
ORDER BY table_name, env_id;
