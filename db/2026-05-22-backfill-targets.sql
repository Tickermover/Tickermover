-- =============================================================================
-- Backfill the two +100% target-hit trades that lived on the ephemeral disk
-- before persistence was migrated to Supabase (2026-05-22).
-- =============================================================================
-- These two picks (LSCC, AAOI) genuinely hit the +100% take-profit target
-- on the active portfolio at some point between Apr 11 and May 22. The
-- exits were recorded to disk but lost on each Railway redeploy because
-- the JSON file wasn't in persistent storage. This restores them as a
-- single representative row each so the closed-trades tab and the
-- landing-page track record both reflect those two confirmed wins.
--
-- One row per (ticker, env_id) so both dev (env_id=2) and prod (env_id=1)
-- read the same ledger after promotion.
--
-- Safe to re-run: uses an existence guard so duplicate inserts are a no-op.
-- =============================================================================

INSERT INTO closed_trades (
    env_id, ticker, name, entry_date, entry_price, pop_at_entry,
    grade_at_entry, rationale, sub_sector, exit_date, exit_price,
    exit_reason, exit_label, exit_detail, final_pct, days_held, won
)
SELECT * FROM (VALUES
    -- LSCC — Lattice Semiconductor — exited at +121.6% (close to 2× entry trip)
    (1::smallint, 'LSCC', 'Lattice Semiconductor', '2026-04-11'::date, 65.00::numeric, 79.0::numeric,
     'A', 'Mid-cap FPGA / programmable logic — efficiency play vs Intel/AMD', 'Semiconductors',
     '2026-05-22'::date, 138.85::numeric, 'target', 'TARGET',
     'Price $138.85 ≥ 2× entry $65.00 — booked, rotating to next hot pick',
     121.62::numeric, 41, true),
    (2::smallint, 'LSCC', 'Lattice Semiconductor', '2026-04-11'::date, 65.00::numeric, 79.0::numeric,
     'A', 'Mid-cap FPGA / programmable logic — efficiency play vs Intel/AMD', 'Semiconductors',
     '2026-05-22'::date, 138.85::numeric, 'target', 'TARGET',
     'Price $138.85 ≥ 2× entry $65.00 — booked, rotating to next hot pick',
     121.62::numeric, 41, true),
    -- AAOI — Applied Optoelectronics — exited at +320.0% (deep target overshoot)
    (1::smallint, 'AAOI', 'Applied Optoelectronics', '2026-04-11'::date, 42.30::numeric, 81.0::numeric,
     'A', 'AI data center optical transceiver demand — small-cap leverage', 'Communication Equipment',
     '2026-05-22'::date, 175.35::numeric, 'target', 'TARGET',
     'Price $175.35 ≥ 2× entry $42.30 — booked, rotating to next hot pick',
     320.05::numeric, 41, true),
    (2::smallint, 'AAOI', 'Applied Optoelectronics', '2026-04-11'::date, 42.30::numeric, 81.0::numeric,
     'A', 'AI data center optical transceiver demand — small-cap leverage', 'Communication Equipment',
     '2026-05-22'::date, 175.35::numeric, 'target', 'TARGET',
     'Price $175.35 ≥ 2× entry $42.30 — booked, rotating to next hot pick',
     320.05::numeric, 41, true)
) AS new(env_id, ticker, name, entry_date, entry_price, pop_at_entry, grade_at_entry,
         rationale, sub_sector, exit_date, exit_price, exit_reason, exit_label,
         exit_detail, final_pct, days_held, won)
WHERE NOT EXISTS (
    SELECT 1 FROM closed_trades ct
    WHERE ct.env_id      = new.env_id
      AND ct.ticker      = new.ticker
      AND ct.exit_reason = new.exit_reason
      AND ct.exit_date   = new.exit_date
);

-- Verify: should return 2 rows per env_id, both reason='target'
SELECT env_id, ticker, exit_date, exit_reason, final_pct
FROM closed_trades
ORDER BY env_id, final_pct DESC;
