-- ════════════════════════════════════════════════════════════════════
-- 2026-06-20 — factor snapshot at entry, for closed-trade attribution
-- ════════════════════════════════════════════════════════════════════
-- Adds a JSONB column that freezes the ranking-relevant factors at the
-- moment a pick is added (momentum, estimate-revision direction, growth,
-- margin trend, FCF, score velocity, etc.). When the trade later closes we
-- can correlate each factor at entry with the realised return — i.e. answer
-- "which signals actually predicted the winners?" — and re-weight the
-- conviction rank by evidence instead of judgment.
--
-- Forward-looking by nature: only trades opened from 2026-06-20 onward carry
-- this; older closed_trades rows keep NULL (cannot be reconstructed
-- point-in-time). Until enough new picks close, the closed-trades view falls
-- back to the conviction-bucket attribution that already exists.
--
-- The app's PortfolioStore.append_trades retries WITHOUT this column if it's
-- missing, so the audit ledger never breaks on a pending migration — but
-- apply this to BOTH prod (env_id 1) and dev (env_id 2) Supabase projects so
-- the snapshots actually persist.
-- ════════════════════════════════════════════════════════════════════

alter table closed_trades
  add column if not exists factors_at_entry jsonb;

comment on column closed_trades.factors_at_entry is
  'Ranking factors frozen at entry (smart_score, momentum_1m/3m, score_velocity, dist_52w_high, rsi_14, rev_growth_yoy/qyoy, eps_rev_net, gross_margin_trend, fcf_margin, target_upside_pct). Added 2026-06-20 for factor attribution. NULL for pre-2026-06-20 trades.';
