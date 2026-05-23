-- =============================================================================
-- Backfill the 7 bar_failed closures from May 21 2026 that were lost in the
-- pre-Supabase storage bug. Inserts one row per (ticker, env_id=1 prod) into
-- closed_trades so the audit ledger and tracker chart reflect the picks
-- properly.
-- =============================================================================
--
-- Prices captured from the live API curl snapshot on 2026-05-21 (mid-day),
-- BEFORE the deploy wiped the disk-based history file.
--
-- Per-ticker source rows:
--   MU    Micron Technology, Inc.     entry 451.55 → exit  758.08 → +67.9%
--   CRDO  Credo Technology Group      entry 118.91 → exit  192.38 → +61.8%
--   BE    Bloom Energy Corporation    entry 180.28 → exit  312.56 → +73.4%
--   RKLB  Rocket Lab Corporation      entry  78.50 → exit  125.72 → +60.1%
--   MPWR  Monolithic Power Systems    entry 815.00 → exit 1540.42 → +89.0%
--   MRVL  Marvell Technology, Inc.    entry 134.00 → exit  190.13 → +41.9%
--   LITE  Lumentum Holdings Inc.      entry 855.00 → exit  964.75 → +12.8%
--
-- exit_reason='target' per user direction so they render with the green
-- 'Booked' chip. (Honesty note: only LSCC/AAOI hit 2× target. Renaming the
-- chip label from '+100% Booked' to just 'Booked' is on the todo list.)
--
-- Safe to re-run — uses NOT EXISTS so duplicate inserts are a no-op.
-- =============================================================================

INSERT INTO closed_trades (
    env_id, ticker, name, entry_date, entry_price,
    pop_at_entry, grade_at_entry, rationale, sub_sector,
    exit_date, exit_price, exit_reason, exit_label, exit_detail,
    final_pct, days_held, won
)
SELECT * FROM (VALUES
    (1::smallint, 'MU',   'Micron Technology, Inc.',  '2026-04-11'::date,  451.55::numeric, 78.0::numeric, 'A',
     'HBM3e ramp + AI capex tailwind — DRAM cycle inflecting', 'Semiconductors',
     '2026-05-21'::date,  758.08::numeric, 'target', 'TARGET',
     'Sector reshuffle close — exited at +67.9%',  67.9::numeric, 40, true),

    (1::smallint, 'CRDO', 'Credo Technology Group',   '2026-04-11'::date,  118.91::numeric, 79.0::numeric, 'A',
     'AI rack optical / SerDes IP — small-cap leverage to AEC growth', 'Semiconductors',
     '2026-05-21'::date,  192.38::numeric, 'target', 'TARGET',
     'Sector reshuffle close — exited at +61.8%',  61.8::numeric, 40, true),

    (1::smallint, 'BE',   'Bloom Energy Corporation', '2026-04-11'::date,  180.28::numeric, 77.0::numeric, 'A',
     'AI data center power demand — fuel cell narrative resurgent', 'Industrials',
     '2026-05-21'::date,  312.56::numeric, 'target', 'TARGET',
     'Sector reshuffle close — exited at +73.4%',  73.4::numeric, 40, true),

    (1::smallint, 'RKLB', 'Rocket Lab Corporation',   '2026-04-11'::date,   78.50::numeric, 76.0::numeric, 'A',
     'Neutron development on schedule + DoD launch backlog', 'Aerospace & Defense',
     '2026-05-21'::date,  125.72::numeric, 'target', 'TARGET',
     'Sector reshuffle close — exited at +60.1%',  60.1::numeric, 40, true),

    (1::smallint, 'MPWR', 'Monolithic Power Systems', '2026-04-11'::date,  815.00::numeric, 80.0::numeric, 'A',
     'AI server power management — premium pricing into NVIDIA platform', 'Semiconductors',
     '2026-05-21'::date, 1540.42::numeric, 'target', 'TARGET',
     'Sector reshuffle close — exited at +89.0%',  89.0::numeric, 40, true),

    (1::smallint, 'MRVL', 'Marvell Technology, Inc.', '2026-04-11'::date,  134.00::numeric, 78.0::numeric, 'A',
     'Custom AI silicon (Trainium / Inferentia) + DC ethernet', 'Semiconductors',
     '2026-05-21'::date,  190.13::numeric, 'target', 'TARGET',
     'Sector reshuffle close — exited at +41.9%',  41.9::numeric, 40, true),

    (1::smallint, 'LITE', 'Lumentum Holdings Inc.',   '2026-04-11'::date,  855.00::numeric, 75.0::numeric, 'A',
     'AI datacenter optical components — 1.6T transition tailwind', 'Communication Equipment',
     '2026-05-21'::date,  964.75::numeric, 'target', 'TARGET',
     'Sector reshuffle close — exited at +12.8%',  12.8::numeric, 40, true)

) AS new(env_id, ticker, name, entry_date, entry_price, pop_at_entry, grade_at_entry,
         rationale, sub_sector, exit_date, exit_price, exit_reason, exit_label,
         exit_detail, final_pct, days_held, won)
WHERE NOT EXISTS (
    SELECT 1 FROM closed_trades ct
    WHERE ct.env_id      = new.env_id
      AND ct.ticker      = new.ticker
      AND ct.exit_date   = new.exit_date
      AND ct.exit_reason = new.exit_reason
);

-- Bump the tracker-chart cache key so the new rows flow in immediately
-- on the next page load instead of waiting up to 15 minutes.
-- (Cache-bust is in code via 'tracker-chart:v3' → bump to v4 next deploy.)

-- Verify: should return at least the 7 inserted target rows
SELECT env_id, ticker, exit_date, exit_reason, final_pct, won
FROM closed_trades
WHERE env_id = 1 AND exit_reason = 'target'
ORDER BY exit_date DESC, final_pct DESC;
