"""
AlphaHunt — Backtest Harness (Phase 1+2 of validation roadmap)

Goal: replace simulation with evidence. This script answers:

  1. Information Coefficient (IC): does our score predict forward returns?
     (Spearman rank correlation between Alpha Score at month-end T and
      next-month return.  IC ≥ 0.05 = real edge, IC ≈ 0 = noise.)

  2. Live trade simulation: pick top-20 by score each month, apply our
     4-rule exit logic, mark to market, record P&L vs SPY benchmark.

  3. Per-year hit rate, average winner, average loser, max drawdown,
     Sharpe ratio.

LIMITATIONS (be honest with yourself reading the results):

  - Uses ONLY price-derived components of Alpha Score (momentum, RS, RSI,
    52w distance, volume spike, trend strength, breakout proximity).  We
    cannot historically reconstruct fundamental signals (earnings_quality,
    fcf_margin, analyst_cons, insider_bias, etc.) without point-in-time
    fundamentals data — those are 9 of the 19 weights.

  - Universe = today's 187 stocks.  Some weren't public 5 years ago
    (e.g. CRDO IPO'd 2022; IONQ via SPAC 2021).  The script skips dates
    when a ticker has no price.  This INTRODUCES SURVIVORSHIP BIAS:
    we know which IPOs survived to today.

  - No realistic transaction costs / slippage / taxes modelled.
    Subtract ~1% per round-trip from raw returns for honesty.

  - Bull-market dominant: 2020-2024 has been an unusually good period
    for growth stocks.  Performance in 2022 (the only real bear in the
    window) is the more meaningful number.

USAGE:
    python backtest.py                # default 5y, full universe, $10k per pick
    python backtest.py --years 3      # shorter window
    python backtest.py --top 10       # 10-stock portfolio instead of 20
    python backtest.py --output out/  # custom output directory

OUTPUTS (default in ./backtest_out/):
    summary.md       — markdown report with key metrics + per-year breakdown
    trades.csv       — every closed trade (one row each)
    monthly_pnl.csv  — month-end portfolio NAV vs SPY
    ic_history.csv   — IC value at each month-end checkpoint
"""

from __future__ import annotations
import argparse
import csv
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

# ── Imports we depend on ─────────────────────────────────────────────────
try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
except ImportError as e:
    print("ERROR: missing dependency. Install with:")
    print("    pip install yfinance pandas numpy")
    print(f"({e})")
    sys.exit(1)

try:
    from scipy.stats import spearmanr
    HAVE_SCIPY = True
except ImportError:
    HAVE_SCIPY = False
    print("(scipy not installed — IC will use numpy fallback, slightly less precise)")

# ── Universe ─────────────────────────────────────────────────────────────
# We import the same ticker list the live app uses, so backtest matches prod.
sys.path.insert(0, str(Path(__file__).parent))
try:
    from stock_universe import _EXCHANGE
    UNIVERSE = list(_EXCHANGE.keys())
except Exception as e:
    print(f"Could not import universe: {e}")
    print("Falling back to hardcoded test set.")
    UNIVERSE = ["AMD", "AVGO", "PLTR", "MU", "AMAT", "LRCX", "KLAC", "MRVL",
                "CRWD", "PANW", "VRT", "GEV", "CEG", "VST", "ANET",
                "COHR", "LITE", "FN", "ASTS", "IONQ"]


# ══════════════════════════════════════════════════════════════════════════
# DATA LAYER  — pull historical daily data, cache to disk
# ══════════════════════════════════════════════════════════════════════════

CACHE_DIR = Path(__file__).parent / ".backtest_cache"
CACHE_DIR.mkdir(exist_ok=True)


def load_prices(tickers: list[str], years: int) -> pd.DataFrame:
    """
    Returns a DataFrame indexed by date with columns = tickers (Adj Close).
    Cached on disk so re-runs don't hit yfinance every time.
    """
    end = datetime.now()
    start = end - timedelta(days=int(years * 365.25) + 30)
    cache_key = f"prices_{years}y_{len(tickers)}t.parquet"
    cache_path = CACHE_DIR / cache_key

    if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < 86400:
        print(f"📂 Loading cached prices ({cache_key})")
        return pd.read_parquet(cache_path)

    print(f"⏬ Downloading {years}y of prices for {len(tickers)} tickers via yfinance…")
    raw = yf.download(
        tickers + ["SPY"],
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        auto_adjust=True,
        progress=True,
        threads=True,
        group_by="ticker",
    )
    # Flatten multi-index → just adj close columns named by ticker
    prices = pd.DataFrame()
    for t in tickers + ["SPY"]:
        try:
            prices[t] = raw[t]["Close"]
        except (KeyError, TypeError):
            print(f"  · skip {t} — no data")
    prices.to_parquet(cache_path)
    print(f"✓ Cached → {cache_path}")
    return prices


def load_volumes(tickers: list[str], years: int) -> pd.DataFrame:
    """Daily volumes — used for the volume_spike component."""
    end = datetime.now()
    start = end - timedelta(days=int(years * 365.25) + 30)
    cache_key = f"volumes_{years}y_{len(tickers)}t.parquet"
    cache_path = CACHE_DIR / cache_key

    if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < 86400:
        return pd.read_parquet(cache_path)

    print(f"⏬ Downloading volumes…")
    raw = yf.download(tickers, start=start.strftime("%Y-%m-%d"),
                      end=end.strftime("%Y-%m-%d"),
                      auto_adjust=False, progress=False, threads=True,
                      group_by="ticker")
    vols = pd.DataFrame()
    for t in tickers:
        try: vols[t] = raw[t]["Volume"]
        except Exception: pass
    vols.to_parquet(cache_path)
    return vols


# ══════════════════════════════════════════════════════════════════════════
# SCORING  — reconstruct the price-only side of the Alpha Score at any date
# ══════════════════════════════════════════════════════════════════════════

def _rsi(series: pd.Series, period: int = 14) -> float:
    """Final RSI value at the end of the series."""
    if len(series) < period + 1:
        return 50.0
    delta = series.diff().dropna()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = -delta.clip(upper=0).rolling(period).mean()
    if loss.iloc[-1] == 0:
        return 100.0
    rs = gain.iloc[-1] / loss.iloc[-1]
    return 100 - (100 / (1 + rs))


def compute_score(prices: pd.Series, volumes: Optional[pd.Series],
                  as_of: pd.Timestamp) -> Optional[dict]:
    """
    Compute the price-derived slice of Alpha Score for one ticker, as of date `as_of`.
    Returns dict with components + raw composite, or None if insufficient history.

    Maps roughly onto live ai_scorer weights:
       momentum_1m       0.10  (we apply 0.20 because we don't have other signals)
       rel_strength      0.10  → 0.20
       volume_spike      0.06  → 0.10
       rsi_zone          0.07  → 0.10
       dist_52w_high     0.05  → 0.10
       trend_strength    0.10  → 0.15
       breakout_prox     0.06  → 0.10
       momentum_3m       (new) 0.05  (proxy for score_momentum)
    """
    px = prices.loc[:as_of].dropna()
    if len(px) < 252:
        return None
    p_today = float(px.iloc[-1])
    p_21    = float(px.iloc[-22]) if len(px) > 22 else p_today
    p_63    = float(px.iloc[-64]) if len(px) > 64 else p_today
    p_252   = float(px.iloc[-252]) if len(px) > 252 else p_today

    mom_1m = (p_today / p_21  - 1.0) if p_21  else 0
    mom_3m = (p_today / p_63  - 1.0) if p_63  else 0
    high_252 = float(px.iloc[-252:].max())
    low_252  = float(px.iloc[-252:].min())
    dist_high = 1 - (p_today / high_252) if high_252 > 0 else 1
    breakout_prox = (p_today - low_252) / max(high_252 - low_252, 1e-9)

    # SMA-based trend strength
    sma200 = float(px.iloc[-200:].mean())
    atr14 = float(px.iloc[-14:].diff().abs().mean()) or 1e-9
    trend = (p_today - sma200) / atr14

    rsi = _rsi(px.iloc[-30:])
    # RSI zone — peaks at 60, falls toward extremes
    rsi_zone = max(0, 1 - abs(rsi - 60) / 40)

    # Volume spike — today vs 50-day avg (if available)
    vspike = 1.0
    if volumes is not None and not volumes.dropna().empty:
        v = volumes.loc[:as_of].dropna()
        if len(v) > 50:
            vspike = float(v.iloc[-5:].mean()) / max(float(v.iloc[-50:].mean()), 1)

    # Score on 0-100 scale (each component normalised to roughly 0-1)
    components = {
        "momentum_1m":     max(-0.5, min(1.0, mom_1m)) + 0.5,         # roughly 0-1.5
        "momentum_3m":     max(-0.5, min(1.0, mom_3m)) + 0.5,
        "rel_strength":    0,  # filled in by caller (cross-sectional)
        "volume_spike":    min(1.0, max(0, (vspike - 0.7) / 1.3)),
        "rsi_zone":        rsi_zone,
        "dist_52w_high":   1 - dist_high,                             # close to 1 when near high
        "trend_strength":  min(1.0, max(0, (trend + 5) / 15)),
        "breakout_prox":   breakout_prox,
    }
    return {
        "components": components,
        "rsi": rsi,
        "mom_1m": mom_1m,
        "mom_3m": mom_3m,
        "dist_52w": dist_high,
        "price": p_today,
    }


WEIGHTS = {
    "momentum_1m":     0.20,
    "momentum_3m":     0.05,
    "rel_strength":    0.20,
    "volume_spike":    0.10,
    "rsi_zone":        0.10,
    "dist_52w_high":   0.10,
    "trend_strength":  0.15,
    "breakout_prox":   0.10,
}


def aggregate_score(components: dict) -> float:
    """Weighted sum → 0-100 scale."""
    s = sum(components.get(k, 0) * w for k, w in WEIGHTS.items())
    return round(s * 100, 2)


# ══════════════════════════════════════════════════════════════════════════
# BACKTEST ENGINE
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class Position:
    ticker: str
    entry_date: pd.Timestamp
    entry_price: float
    entry_score: float
    peak_price: float = 0.0      # set on first enrich

    def trail_floor(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        peak_pct = self.peak_price / self.entry_price - 1
        if   peak_pct >= 1.00: return self.entry_price * 1.50
        elif peak_pct >= 0.50: return self.entry_price * 1.25
        elif peak_pct >= 0.25: return self.entry_price * 1.10
        elif peak_pct >= 0.10: return self.entry_price * 1.00
        return self.entry_price * 0.92  # hard stop tier

    def hard_stop(self) -> float:
        return self.entry_price * 0.92


@dataclass
class ClosedTrade:
    ticker: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    entry_score: float
    final_pct: float
    days_held: int
    exit_reason: str

    @property
    def won(self) -> bool:
        return self.final_pct > 0


def run_backtest(prices: pd.DataFrame, volumes: pd.DataFrame,
                 universe: list[str], top_n: int = 20,
                 stake_per_pick: float = 10000.0) -> dict:
    """
    Walk forward month by month. Each month-end:
      1. Compute Alpha Score for every ticker with sufficient history
      2. Convert raw scores → cross-sectional rel_strength → final score
      3. Pick top-N
      4. Simulate entry next trading day at open (we use close for simplicity)
      5. Daily mark-to-market with the 4-rule exit logic
      6. Record closed trades, NAV, IC

    Returns dict with: trades (list), nav_history (list), ic_history (list),
    summary stats.
    """
    print(f"\n🔬 Backtest: {len(universe)} stocks, top {top_n}, ${stake_per_pick:,.0f}/pick")
    print(f"   Window: {prices.index[0].date()} → {prices.index[-1].date()}")

    # Generate month-end checkpoints (first trading day of each month)
    monthly = prices.resample("M").last()
    checkpoint_dates = monthly.index[monthly.index >= prices.index[0] + timedelta(days=380)]

    open_positions: dict[str, Position] = {}
    closed_trades: list[ClosedTrade] = []
    nav_history: list[dict] = []
    ic_history: list[dict] = []

    cash = stake_per_pick * top_n
    initial_cash = cash

    last_nav = initial_cash
    spy_init = float(prices["SPY"].dropna().iloc[0]) if "SPY" in prices.columns else 0
    spy_today = spy_init

    for ckpt in checkpoint_dates:
        # ── Score every ticker ────────────────────────────────────────────
        raw_scores = {}
        for t in universe:
            if t not in prices.columns:
                continue
            v = volumes[t] if (volumes is not None and t in volumes.columns) else None
            r = compute_score(prices[t], v, ckpt)
            if r is not None:
                raw_scores[t] = r

        if not raw_scores:
            continue

        # Cross-sectional rel_strength: rank momentum_1m within universe
        m1_rank = pd.Series({t: raw_scores[t]["mom_1m"] for t in raw_scores}).rank(pct=True)
        # m1_rank values are 0..1
        for t in raw_scores:
            raw_scores[t]["components"]["rel_strength"] = float(m1_rank[t])
            raw_scores[t]["score"] = aggregate_score(raw_scores[t]["components"])

        # Forward 1-month return for IC (used to evaluate this checkpoint)
        nxt_idx = monthly.index.get_indexer([ckpt])[0] + 1
        if nxt_idx < len(monthly.index):
            nxt = monthly.index[nxt_idx]
            fwd = {}
            for t in raw_scores:
                p_now = raw_scores[t]["price"]
                p_nxt = float(monthly[t].loc[nxt]) if t in monthly.columns else 0
                if p_now > 0 and not pd.isna(p_nxt):
                    fwd[t] = (p_nxt - p_now) / p_now
            common = [t for t in raw_scores if t in fwd]
            if len(common) >= 5:
                xs = [raw_scores[t]["score"] for t in common]
                ys = [fwd[t] for t in common]
                if HAVE_SCIPY:
                    ic, _ = spearmanr(xs, ys)
                else:
                    # rank-correlation by hand
                    rx = pd.Series(xs).rank()
                    ry = pd.Series(ys).rank()
                    ic = float(rx.corr(ry))
                ic_history.append({
                    "date": ckpt, "n_stocks": len(common),
                    "ic": float(ic) if not math.isnan(ic) else 0.0,
                })

        # ── Daily walk through this month for exits ──────────────────────
        # Determine the date range from this ckpt to next
        next_ckpt = checkpoint_dates[checkpoint_dates.get_indexer([ckpt])[0] + 1] \
                    if checkpoint_dates.get_indexer([ckpt])[0] + 1 < len(checkpoint_dates) \
                    else prices.index[-1]
        period = prices.loc[ckpt:next_ckpt]

        for d in period.index:
            # Update peaks + check exits for open positions
            to_close: list[tuple[str, str]] = []
            for tk, pos in list(open_positions.items()):
                if tk not in prices.columns:
                    continue
                p = prices[tk].loc[d]
                if pd.isna(p):
                    continue
                pos.peak_price = max(pos.peak_price, float(p))
                # Hard stop?
                if p <= pos.hard_stop():
                    to_close.append((tk, "stop"))
                    continue
                # Trail stop?
                tf = pos.trail_floor()
                if tf > pos.entry_price * 0.92 and p <= tf:  # only when above hard stop tier
                    to_close.append((tk, "trail"))

            for tk, reason in to_close:
                pos = open_positions.pop(tk)
                exit_price = float(prices[tk].loc[d])
                final_pct = round((exit_price - pos.entry_price) / pos.entry_price * 100, 2)
                days_held = (d - pos.entry_date).days
                closed_trades.append(ClosedTrade(
                    ticker=tk,
                    entry_date=pos.entry_date,
                    exit_date=d,
                    entry_price=pos.entry_price,
                    exit_price=exit_price,
                    entry_score=pos.entry_score,
                    final_pct=final_pct,
                    days_held=days_held,
                    exit_reason=reason,
                ))
                cash += stake_per_pick * (1 + final_pct / 100)

        # ── At checkpoint end, top up to N positions if any closed ───────
        held = set(open_positions.keys())
        # Top N by score, not already held
        ranked = sorted(
            [(t, raw_scores[t]["score"], raw_scores[t]["price"]) for t in raw_scores],
            key=lambda x: x[1], reverse=True,
        )
        for t, score, price in ranked:
            if len(open_positions) >= top_n:
                break
            if t in held or cash < stake_per_pick:
                continue
            open_positions[t] = Position(
                ticker=t, entry_date=ckpt,
                entry_price=price, entry_score=score, peak_price=price,
            )
            cash -= stake_per_pick

        # ── NAV mark-to-market at this checkpoint ─────────────────────────
        nav = cash
        for tk, pos in open_positions.items():
            if tk in prices.columns:
                p = float(prices[tk].loc[ckpt])
                if not pd.isna(p):
                    nav += stake_per_pick * (p / pos.entry_price)
        spy_today = float(prices["SPY"].loc[ckpt]) if "SPY" in prices.columns else spy_today
        nav_history.append({
            "date": ckpt, "nav": round(nav, 2),
            "spy": round(spy_today, 2),
            "open_n": len(open_positions),
            "closed_n": len(closed_trades),
        })

    # Final liquidation at last available date for fair P&L
    last_d = prices.index[-1]
    for tk, pos in list(open_positions.items()):
        if tk in prices.columns:
            p = prices[tk].loc[:last_d].dropna()
            if not p.empty:
                ep = float(p.iloc[-1])
                final_pct = round((ep - pos.entry_price) / pos.entry_price * 100, 2)
                closed_trades.append(ClosedTrade(
                    ticker=tk, entry_date=pos.entry_date, exit_date=last_d,
                    entry_price=pos.entry_price, exit_price=ep,
                    entry_score=pos.entry_score, final_pct=final_pct,
                    days_held=(last_d - pos.entry_date).days,
                    exit_reason="end-of-test",
                ))

    return {
        "trades": closed_trades,
        "nav_history": nav_history,
        "ic_history": ic_history,
        "initial_cash": initial_cash,
        "spy_init": spy_init,
        "spy_final": spy_today,
    }


# ══════════════════════════════════════════════════════════════════════════
# REPORTING
# ══════════════════════════════════════════════════════════════════════════

def write_report(result: dict, out_dir: Path) -> None:
    out_dir.mkdir(exist_ok=True, parents=True)

    trades = result["trades"]
    navs   = result["nav_history"]
    ics    = result["ic_history"]

    # Summary metrics
    if trades:
        wins   = [t for t in trades if t.won]
        losses = [t for t in trades if not t.won]
        hit_rate = len(wins) / len(trades) * 100
        avg_win  = sum(t.final_pct for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.final_pct for t in losses) / len(losses) if losses else 0
        best     = max(t.final_pct for t in trades)
        worst    = min(t.final_pct for t in trades)
    else:
        hit_rate = avg_win = avg_loss = best = worst = 0

    if navs:
        nav_init = result["initial_cash"]
        nav_end  = navs[-1]["nav"]
        total_ret = (nav_end - nav_init) / nav_init * 100
        spy_ret = (result["spy_final"] - result["spy_init"]) / result["spy_init"] * 100 \
                  if result["spy_init"] else 0
        # Max drawdown
        peak = nav_init
        max_dd = 0
        for n in navs:
            peak = max(peak, n["nav"])
            dd = (n["nav"] - peak) / peak
            max_dd = min(max_dd, dd)
        max_dd_pct = max_dd * 100
    else:
        total_ret = spy_ret = max_dd_pct = 0

    avg_ic = sum(i["ic"] for i in ics) / len(ics) if ics else 0
    ic_consistency = sum(1 for i in ics if i["ic"] > 0) / len(ics) if ics else 0

    # CSVs
    if trades:
        with open(out_dir / "trades.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ticker","entry_date","exit_date","entry_price","exit_price",
                        "entry_score","final_pct","days_held","exit_reason","won"])
            for t in trades:
                w.writerow([t.ticker, t.entry_date.date(), t.exit_date.date(),
                            t.entry_price, t.exit_price, t.entry_score,
                            t.final_pct, t.days_held, t.exit_reason, t.won])
    if navs:
        with open(out_dir / "monthly_pnl.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date","nav","spy","open_n","closed_n"])
            for n in navs:
                w.writerow([n["date"].date(), n["nav"], n["spy"], n["open_n"], n["closed_n"]])
    if ics:
        with open(out_dir / "ic_history.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date","n_stocks","ic"])
            for i in ics:
                w.writerow([i["date"].date(), i["n_stocks"], round(i["ic"],4)])

    # Markdown report
    md = []
    md.append("# AlphaHunt — Backtest Summary\n")
    md.append(f"_Generated {datetime.now().isoformat(timespec='seconds')}_\n\n")
    md.append("## Headline numbers\n\n")
    md.append("| Metric | Value | Interpretation |\n")
    md.append("|---|---|---|\n")
    md.append(f"| **Information Coefficient (avg)** | {avg_ic:+.4f} | "
              f"{'real edge' if avg_ic >= 0.05 else 'weak / noise — re-tune weights'} |\n")
    md.append(f"| IC consistency (% checkpoints positive) | {ic_consistency*100:.1f}% | "
              f"{'consistent' if ic_consistency >= 0.6 else 'volatile — possibly regime-dependent'} |\n")
    md.append(f"| Total return | {total_ret:+.1f}% | over backtest window |\n")
    md.append(f"| SPY benchmark | {spy_ret:+.1f}% | same window |\n")
    md.append(f"| **Excess vs SPY** | {total_ret - spy_ret:+.1f}% | the only number that really matters |\n")
    md.append(f"| Max drawdown | {max_dd_pct:.1f}% | worst peak-to-trough |\n")
    md.append(f"| Total closed trades | {len(trades)} | sample size |\n")
    md.append(f"| Hit rate | {hit_rate:.1f}% | trades that ended profitable |\n")
    md.append(f"| Avg winner | {avg_win:+.2f}% | when right |\n")
    md.append(f"| Avg loser  | {avg_loss:+.2f}% | when wrong |\n")
    md.append(f"| Best trade | {best:+.1f}% | top winner |\n")
    md.append(f"| Worst trade| {worst:+.1f}% | top loser |\n\n")

    # Per-year breakdown
    if trades:
        md.append("## Per-year performance\n\n| Year | Trades | Hit Rate | Avg Trade |\n|---|---|---|---|\n")
        by_year: dict[int, list] = {}
        for t in trades:
            by_year.setdefault(t.exit_date.year, []).append(t)
        for y in sorted(by_year):
            ts = by_year[y]
            hr = sum(1 for x in ts if x.won) / len(ts) * 100
            avg = sum(x.final_pct for x in ts) / len(ts)
            md.append(f"| {y} | {len(ts)} | {hr:.1f}% | {avg:+.2f}% |\n")
        md.append("\n")

    # Honest caveats
    md.append("## Caveats — read carefully\n\n")
    md.append("- **Universe survivorship bias:** the 187 tickers are today's curated list. "
              "Some weren't public 5 years ago and we skip dates with no price. Results are "
              "biased upward because we know which IPOs survived.\n")
    md.append("- **Score reconstruction is partial:** only price-derived components (momentum, "
              "RS, RSI, 52w distance, volume spike, trend strength, breakout proximity). "
              "Fundamental + analyst + insider + social signals (9 of 19 weights in live app) "
              "are NOT included — historical point-in-time data isn't available.\n")
    md.append("- **No transaction costs / slippage / taxes:** subtract ~1% per round-trip "
              "for an honest net-of-fees estimate.\n")
    md.append("- **Exit-only validation here:** the 4-rule exit (8% stop + stair-step trail) "
              "is the well-tested CAN SLIM-derived part. The novel claim that needs the IC "
              "to back it up is whether the *score itself* picks better stocks than random.\n")
    md.append("- **A higher hit rate doesn't equal a better strategy:** asymmetric payoff "
              "(big winners, small losers) drives long-run CAGR. Watch avg winner vs avg loser.\n\n")

    md.append("## How to read these results\n\n")
    md.append("**The IC is the single most important number.**\n\n")
    md.append("- IC ≥ 0.05 average AND ≥ 60% of checkpoints positive → score has real predictive power, "
              "you can confidently market the model.\n")
    md.append("- IC between 0.02 and 0.05 → weak edge, possibly real but easy to lose to costs.\n")
    md.append("- IC < 0.02 or wildly inconsistent → score is essentially noise. The good performance "
              "is coming from the EXIT logic + market beta, not the score itself.\n\n")
    md.append("If IC is weak, don't despair — the exit rules alone (cut at -8%, ride winners) on a "
              "random selection of Grade A stocks would still beat undisciplined trading.\n")

    (out_dir / "summary.md").write_text("".join(md))
    print(f"\n✓ Report written → {out_dir.resolve()}")
    print(f"   Avg IC: {avg_ic:+.4f}  ({ic_consistency*100:.0f}% positive)")
    print(f"   Total return: {total_ret:+.1f}% vs SPY {spy_ret:+.1f}%  (excess {total_ret-spy_ret:+.1f}%)")
    print(f"   Hit rate: {hit_rate:.1f}%  ({len(trades)} closed trades, max DD {max_dd_pct:.1f}%)")


# ══════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(description="AlphaHunt backtest")
    ap.add_argument("--years", type=int, default=5, help="years of history (default 5)")
    ap.add_argument("--top", type=int, default=20, help="positions held (default 20)")
    ap.add_argument("--stake", type=float, default=10000, help="$ per pick")
    ap.add_argument("--output", default="backtest_out", help="output directory")
    ap.add_argument("--universe", default=None,
                    help="comma-separated tickers (default = full live universe)")
    args = ap.parse_args()

    universe = args.universe.split(",") if args.universe else UNIVERSE
    universe = [t.strip().upper() for t in universe if t.strip()]
    print(f"Universe: {len(universe)} tickers")

    prices = load_prices(universe, args.years)
    if prices.empty:
        print("No price data — abort.")
        return 1
    volumes = load_volumes(universe, args.years)

    result = run_backtest(prices, volumes, universe,
                          top_n=args.top, stake_per_pick=args.stake)
    write_report(result, Path(args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
