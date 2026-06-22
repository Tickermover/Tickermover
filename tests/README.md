# tests/

First automated tests for the project — seeded from the 2026-06-22 QA audit so the
fixes can't silently regress. Pure-function / unit level (no network, no running
server), so they're fast and deterministic.

## Run

No pytest required:

```bash
python tests/run_all.py
```

Exit code is non-zero if anything fails (CI-gateable). Once `pytest` is installed,
`pytest tests/` runs the identical tests.

## Coverage

| File | Guards |
|---|---|
| `test_billing.py` | Webhook signature verify **fails closed** (QA B1/B2) + signature math (B3); Pro gating |
| `test_usage_log.py` | AI cost estimation / tier pricing — the input to the daily + $50/month caps |
| `test_scoring.py` | `compute_pop_score` robustness on empty/None/NaN inputs + grade ordering |
| `test_bottom_line.py` | `_sig` cache key is bucket-stable (the fix for the Jun-19 Haiku cost bleed) |

## Adding tests

Drop a `test_<area>.py` in this dir with `test_*` functions using bare `assert`,
then add the module name to `TEST_MODULES` in `run_all.py`. Only import modules
that load without a running server (billing, usage_log, ai_scorer, bottom_line_ai,
selection_store, stock_universe, …) — `app.py` starts the whole app on import.

## Good next targets
- `_safe_redirect` / `_valid_ticker` / `_sanitize_watch_entries` (currently in
  app.py — extract to a small util module to make them importable, then test).
- `auth.verify_token` with crafted tokens (alg:none, expired, wrong key).
- `selection_store.is_stale` TTL boundary.
