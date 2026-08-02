"""scans.py — the Stock Scans catalogue.

A scan is a named, pre-built screen over the live universe: a predicate, a sort
and a column set. Everything here reads rows the coordinator has ALREADY built
(123 fields per ticker), so a scan costs no provider call and no AI spend — it
is arithmetic over data the process is holding anyway.

Six categories, mirroring how an investor actually asks the question:

  stock          quality / value / momentum screens over fundamentals
  result         what the latest earnings print actually said
  market         the market-wide picture (breadth, sectors, regime)
  announcement   corporate actions and filings worth knowing about
  shareholding   who owns it, who is buying it, who is short it
  concall        what management said on the call (from cached AI summaries)

Design rules, learned from the scorecard bugs earlier in this project:
  * A missing field must NEVER read as a failing value. Every helper returns
    None for "unknown", and predicates require a real number before judging.
  * Thresholds are OUR research conventions, not standards. They are stated on
    each scan so a reader can disagree with the line rather than guess it.
  * A scan describes what it found. It never says buy or sell.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ── field helpers ───────────────────────────────────────────────────────
def num(row: dict, *keys):
    """First key that holds a real number, else None. Never raises."""
    for k in keys:
        v = row.get(k)
        if v is None or v == "":
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f != f:                     # NaN
            continue
        return f
    return None


def pct(row: dict, *keys):
    """A ratio field normalised to percent.

    The universe mixes conventions — profit_margin arrives as 0.17 while
    revenue_growth_yoy can arrive as 0.169 OR 16.9 depending on provider. Treat
    anything within +/-2 as a fraction, which is the same guard the fact-check
    card uses."""
    v = num(row, *keys)
    if v is None:
        return None
    return v * 100.0 if -2.0 <= v <= 2.0 else v


def ge(v, t):
    return v is not None and v >= t


def le(v, t):
    return v is not None and v <= t


def gt(v, t):
    return v is not None and v > t



def net_revisions(row: dict):
    """Net analyst revisions from eps_revisions_30d.

    The field is a DICT — {"ups":5,"downs":0,"total_analysts":21,"period":"+1q"} —
    not a number, so reading it with num() silently returned None and the
    "estimates rising" scan matched zero rows out of 517 that carry the field."""
    v = row.get("eps_revisions_30d")
    if not isinstance(v, dict):
        return None
    try:
        return int(v.get("ups") or 0) - int(v.get("downs") or 0)
    except (TypeError, ValueError):
        return None


# ── column presets ──────────────────────────────────────────────────────
C_ALPHA = {"key": "pop_score", "label": "Alpha", "fmt": "int"}
C_GRADE = {"key": "grade", "label": "Grade", "fmt": "text"}
C_PRICE = {"key": "price", "label": "Price", "fmt": "usd"}
C_CHG = {"key": "change_pct", "label": "Today", "fmt": "pct_signed"}


def _cols(*extra):
    return [{"key": "ticker", "label": "Ticker", "fmt": "ticker"},
            {"key": "name", "label": "Company", "fmt": "text"}] + list(extra)


# ── the catalogue ───────────────────────────────────────────────────────
# where(row) -> bool ; sort = (field, "desc"|"asc")
SCANS: list[dict] = [

    # ── STOCK ───────────────────────────────────────────────────────────
    {
        "id": "quality-compounders", "cat": "stock",
        "name": "Quality compounders",
        "blurb": "Profitable, growing and generating cash — the businesses that fund themselves.",
        "note": "Our lines: gross margin ≥ 40%, revenue growth ≥ 15%, positive free cash flow.",
        "where": lambda r: (ge(pct(r, "gross_margin"), 40)
                            and ge(pct(r, "revenue_growth_yoy"), 15)
                            and gt(num(r, "free_cashflow"), 0)),
        "sort": ("pop_score", "desc"),
        "cols": _cols(C_ALPHA, C_GRADE,
                      {"key": "gross_margin", "label": "Gross margin", "fmt": "pct"},
                      {"key": "revenue_growth_yoy", "label": "Rev growth", "fmt": "pct"},
                      {"key": "fcf_margin", "label": "FCF margin", "fmt": "pct"}),
    },
    {
        "id": "value-with-growth", "cat": "stock",
        "name": "Growth at a reasonable price",
        "blurb": "Growing double digits without a premium multiple attached.",
        "note": "Our lines: PEG ≤ 1.5, forward P/E ≤ 25, revenue growth ≥ 10%.",
        "where": lambda r: (le(num(r, "peg_ratio"), 1.5) and gt(num(r, "peg_ratio"), 0)
                            and le(num(r, "forward_pe", "pe_ratio"), 25)
                            and ge(pct(r, "revenue_growth_yoy"), 10)),
        "sort": ("peg_ratio", "asc"),
        "cols": _cols(C_ALPHA,
                      {"key": "peg_ratio", "label": "PEG", "fmt": "x2"},
                      {"key": "forward_pe", "label": "Fwd P/E", "fmt": "x1"},
                      {"key": "revenue_growth_yoy", "label": "Rev growth", "fmt": "pct"}),
    },
    {
        "id": "momentum-leaders", "cat": "stock",
        "name": "Momentum leaders",
        "blurb": "Trending above both moving averages, with three-month strength behind them.",
        "note": "Our lines: price above the 50- and 200-day SMA, 3-month momentum ≥ 15%.",
        "where": lambda r: (gt(num(r, "price"), num(r, "sma_50") or 1e18)
                            and gt(num(r, "price"), num(r, "sma_200") or 1e18)
                            and ge(pct(r, "momentum_3m"), 15)),
        "sort": ("momentum_3m", "desc"),
        "cols": _cols(C_ALPHA, C_PRICE,
                      {"key": "momentum_3m", "label": "3-month", "fmt": "pct_signed"},
                      {"key": "rsi_14", "label": "RSI", "fmt": "int"},
                      {"key": "dist_52w_high", "label": "From 52w high", "fmt": "pct_signed"}),
    },
    {
        "id": "oversold-quality", "cat": "stock",
        "name": "Oversold, still profitable",
        "blurb": "Beaten down on price while the P&L is still working.",
        "note": "Our lines: RSI ≤ 40, positive profit margin, more than 15% below the 52-week high.",
        "where": lambda r: (le(num(r, "rsi_14"), 40)
                            and gt(pct(r, "profit_margin"), 0)
                            and le(pct(r, "dist_52w_high"), -15)),
        "sort": ("rsi_14", "asc"),
        "cols": _cols(C_ALPHA,
                      {"key": "rsi_14", "label": "RSI", "fmt": "int"},
                      {"key": "dist_52w_high", "label": "From 52w high", "fmt": "pct_signed"},
                      {"key": "profit_margin", "label": "Net margin", "fmt": "pct"},
                      C_PRICE),
    },
    {
        "id": "debt-light", "cat": "stock",
        "name": "Debt-light balance sheets",
        "blurb": "Little or no leverage, and the liquidity to sit out a bad quarter.",
        "note": "Our lines: debt/equity ≤ 0.3, current ratio ≥ 1.5.",
        "where": lambda r: (le(num(r, "debt_to_equity"), 0.3)
                            and ge(num(r, "current_ratio"), 1.5)),
        "sort": ("debt_to_equity", "asc"),
        "cols": _cols(C_ALPHA,
                      {"key": "debt_to_equity", "label": "Debt/equity", "fmt": "x2"},
                      {"key": "current_ratio", "label": "Current ratio", "fmt": "x2"},
                      {"key": "roe", "label": "ROE", "fmt": "pct"}),
    },

    # ── RESULT ──────────────────────────────────────────────────────────
    {
        "id": "beat-streak", "cat": "result",
        "name": "Serial beaters",
        "blurb": "Companies that have cleared the estimate every quarter we track.",
        "note": "Our line: beat in at least 3 of the last 4 reported quarters.",
        "where": lambda r: ge(num(r, "eps_beat_streak"), 3),
        "sort": ("eps_beat_streak", "desc"),
        "cols": _cols({"key": "eps_beat_streak", "label": "Beat streak", "fmt": "int"},
                      {"key": "avg_eps_surprise_pct", "label": "Avg surprise", "fmt": "pct_signed"},
                      {"key": "eps_growth_yoy", "label": "EPS growth", "fmt": "pct_signed"},
                      C_ALPHA),
    },
    {
        "id": "estimates-rising", "cat": "result",
        "name": "Estimates being raised",
        "blurb": "More analysts moving numbers up than down over the last 30 days.",
        "note": "Our line: net upward revisions in the last 30 days.",
        "where": lambda r: gt(net_revisions(r), 0),
        "sort": ("_net_rev", "desc"),
        "cols": _cols({"key": "_net_rev", "label": "Net revisions", "fmt": "signed"},
                      {"key": "revenue_growth_yoy", "label": "Rev growth", "fmt": "pct"},
                      C_ALPHA, C_GRADE),
    },
    {
        "id": "just-reported", "cat": "result",
        "name": "Just reported",
        "blurb": "Printed within the last two weeks — the numbers are fresh.",
        "note": "Our line: last earnings date within 14 days.",
        "where": lambda r: le(num(r, "days_since_earnings"), 14) and num(r, "days_since_earnings") is not None,
        "sort": ("days_since_earnings", "asc"),
        "cols": _cols({"key": "days_since_earnings", "label": "Days ago", "fmt": "int"},
                      {"key": "eps_last_beat", "label": "Beat?", "fmt": "bool"},
                      {"key": "rev_growth_qyoy", "label": "Rev growth QoQ", "fmt": "pct_signed"},
                      C_CHG),
    },
    {
        "id": "margin-expanding", "cat": "result",
        "name": "Margins expanding",
        "blurb": "Gross margin trending up — pricing power showing in the P&L.",
        "note": "Our line: our gross-margin trend reads as expanding.",
        "where": lambda r: str(r.get("gross_margin_trend") or "").lower() in ("expanding", "up", "rising"),
        "sort": ("gross_margin", "desc"),
        "cols": _cols({"key": "gross_margin", "label": "Gross margin", "fmt": "pct"},
                      {"key": "operating_margin", "label": "Op margin", "fmt": "pct"},
                      {"key": "profit_margin", "label": "Net margin", "fmt": "pct"},
                      C_ALPHA),
    },
    {
        "id": "reporting-soon", "cat": "result",
        "name": "Reporting in the next fortnight",
        "blurb": "The thesis gets tested shortly — worth knowing before it does.",
        "note": "Our line: earnings date within the next 14 days.",
        "where": lambda r: (num(r, "days_to_earnings") is not None
                            and 0 <= num(r, "days_to_earnings") <= 14),
        "sort": ("days_to_earnings", "asc"),
        "cols": _cols({"key": "days_to_earnings", "label": "Days away", "fmt": "int"},
                      {"key": "earnings_date", "label": "Date", "fmt": "text"},
                      {"key": "eps_beat_streak", "label": "Beat streak", "fmt": "int"},
                      C_ALPHA),
    },

    # ── SHAREHOLDING ────────────────────────────────────────────────────
    {
        "id": "insider-buying", "cat": "shareholding",
        "name": "Insiders buying",
        "blurb": "More insider purchases than sales over the last 90 days.",
        "note": "Our line: insider buys exceed sells in the last 90 days.",
        "where": lambda r: (gt(num(r, "insider_buys_90d"), 0)
                            and gt(num(r, "insider_buys_90d"), num(r, "insider_sells_90d") or 0)),
        "sort": ("insider_buys_90d", "desc"),
        "cols": _cols({"key": "insider_buys_90d", "label": "Buys (90d)", "fmt": "int"},
                      {"key": "insider_sells_90d", "label": "Sells (90d)", "fmt": "int"},
                      {"key": "held_pct_insiders", "label": "Insider held", "fmt": "pct"},
                      C_ALPHA),
    },
    {
        "id": "institutional-heavy", "cat": "shareholding",
        "name": "Institutionally owned",
        "blurb": "Where the professional money already sits.",
        "note": "Our line: institutions hold at least 70% of the float.",
        "where": lambda r: ge(pct(r, "held_pct_institutions"), 70),
        "sort": ("held_pct_institutions", "desc"),
        "cols": _cols({"key": "held_pct_institutions", "label": "Institutional", "fmt": "pct"},
                      {"key": "held_pct_insiders", "label": "Insider", "fmt": "pct"},
                      {"key": "market_cap", "label": "Market cap", "fmt": "money"},
                      C_ALPHA),
    },
    {
        "id": "heavily-shorted", "cat": "shareholding",
        "name": "Heavily shorted",
        "blurb": "A meaningful slice of the float is positioned against these.",
        "note": "Our line: short interest at least 10% of float.",
        "where": lambda r: ge(pct(r, "short_percent_float"), 10),
        "sort": ("short_percent_float", "desc"),
        "cols": _cols({"key": "short_percent_float", "label": "Short % float", "fmt": "pct"},
                      {"key": "momentum_1m", "label": "1-month", "fmt": "pct_signed"},
                      {"key": "profit_margin", "label": "Net margin", "fmt": "pct"},
                      C_ALPHA),
    },
    {
        "id": "founder-held", "cat": "shareholding",
        "name": "Founder / insider heavy",
        "blurb": "Insiders still hold a large personal stake in the outcome.",
        "note": "Our line: insiders hold at least 10%.",
        "where": lambda r: ge(pct(r, "held_pct_insiders"), 10),
        "sort": ("held_pct_insiders", "desc"),
        "cols": _cols({"key": "held_pct_insiders", "label": "Insider held", "fmt": "pct"},
                      {"key": "held_pct_institutions", "label": "Institutional", "fmt": "pct"},
                      {"key": "revenue_growth_yoy", "label": "Rev growth", "fmt": "pct"},
                      C_ALPHA),
    },

    # ── MARKET ──────────────────────────────────────────────────────────
    # Source: the sector table from market_analysis, not universe rows.
    {
        "id": "sector-leaders", "cat": "market", "src": "sectors",
        "name": "Sectors leading this week",
        "blurb": "Where money moved over the last five sessions.",
        "note": "Sorted by 5-day change. Sector moves, not single stocks.",
        "where": lambda r: num(r, "chg_5d") is not None,
        "sort": ("chg_5d", "desc"),
        "cols": [{"key": "name", "label": "Sector", "fmt": "text"},
                 {"key": "chg_1d", "label": "1 day", "fmt": "pct_signed"},
                 {"key": "chg_5d", "label": "5 day", "fmt": "pct_signed"}],
    },
    {
        "id": "sector-laggards", "cat": "market", "src": "sectors",
        "name": "Sectors under pressure",
        "blurb": "The other side of the rotation.",
        "note": "Sectors negative over five sessions, weakest first.",
        "where": lambda r: le(num(r, "chg_5d"), 0),
        "sort": ("chg_5d", "asc"),
        "cols": [{"key": "name", "label": "Sector", "fmt": "text"},
                 {"key": "chg_1d", "label": "1 day", "fmt": "pct_signed"},
                 {"key": "chg_5d", "label": "5 day", "fmt": "pct_signed"}],
    },
    {
        "id": "near-highs", "cat": "market",
        "name": "Breadth — names near their highs",
        "blurb": "How much of the universe is actually participating.",
        "note": "Our line: within 5% of the 52-week high.",
        "where": lambda r: ge(pct(r, "dist_52w_high"), -5),
        "sort": ("dist_52w_high", "desc"),
        "cols": _cols({"key": "dist_52w_high", "label": "From 52w high", "fmt": "pct_signed"},
                      {"key": "momentum_1m", "label": "1-month", "fmt": "pct_signed"},
                      C_ALPHA, {"key": "sector", "label": "Sector", "fmt": "text"}),
    },
    {
        "id": "near-lows", "cat": "market",
        "name": "Breadth — names near their lows",
        "blurb": "The damage, and where it is concentrated.",
        "note": "Our line: more than 30% below the 52-week high.",
        "where": lambda r: le(pct(r, "dist_52w_high"), -30),
        "sort": ("dist_52w_high", "asc"),
        "cols": _cols({"key": "dist_52w_high", "label": "From 52w high", "fmt": "pct_signed"},
                      {"key": "rsi_14", "label": "RSI", "fmt": "int"},
                      C_ALPHA, {"key": "sector", "label": "Sector", "fmt": "text"}),
    },

    # ── ANNOUNCEMENT ────────────────────────────────────────────────────
    # Source: the corporate-actions feed (dividends, splits, ex-dates).
    {
        "id": "upcoming-actions", "cat": "announcement", "src": "actions",
        "name": "Upcoming corporate actions",
        "blurb": "Dividends, splits and ex-dates still ahead.",
        "note": "Straight from the filings calendar — nothing inferred.",
        "where": lambda r: bool(r.get("upcoming")),
        "sort": ("_date_ord", "asc"),
        "cols": [{"key": "date", "label": "Date", "fmt": "text"},
                 {"key": "symbol", "label": "Ticker", "fmt": "ticker"},
                 {"key": "name", "label": "Company", "fmt": "text"},
                 {"key": "type", "label": "Type", "fmt": "text"},
                 {"key": "action", "label": "Action", "fmt": "text"}],
    },
    {
        "id": "dividend-actions", "cat": "announcement", "src": "actions",
        "name": "Dividend announcements",
        "blurb": "Declared and ex-dividend dates across the universe.",
        "note": "Dividend-type actions only.",
        "where": lambda r: "dividend" in str(r.get("type") or "").lower(),
        "sort": ("_date_ord", "asc"),
        "cols": [{"key": "date", "label": "Date", "fmt": "text"},
                 {"key": "symbol", "label": "Ticker", "fmt": "ticker"},
                 {"key": "name", "label": "Company", "fmt": "text"},
                 {"key": "action", "label": "Action", "fmt": "text"}],
    },
    {
        "id": "split-actions", "cat": "announcement", "src": "actions",
        "name": "Splits and other actions",
        "blurb": "Everything that is not a dividend.",
        "note": "Splits, spin-offs and other filed actions.",
        "where": lambda r: "dividend" not in str(r.get("type") or "").lower(),
        "sort": ("_date_ord", "asc"),
        "cols": [{"key": "date", "label": "Date", "fmt": "text"},
                 {"key": "symbol", "label": "Ticker", "fmt": "ticker"},
                 {"key": "name", "label": "Company", "fmt": "text"},
                 {"key": "type", "label": "Type", "fmt": "text"},
                 {"key": "action", "label": "Action", "fmt": "text"}],
    },

    # ── CONCALL ─────────────────────────────────────────────────────────
    # These identify WHERE call notes are worth reading. The notes themselves
    # come from the cached event-intel summaries, opened per ticker — scanning
    # 545 transcripts on demand would be an AI bill, not a scan.
    {
        "id": "fresh-calls", "cat": "concall",
        "name": "Calls worth reading now",
        "blurb": "Reported in the last three weeks — the call is still the newest information.",
        "note": "Our line: reported within 21 days. Open a name for the call notes.",
        "where": lambda r: (num(r, "days_since_earnings") is not None
                            and num(r, "days_since_earnings") <= 21),
        "sort": ("days_since_earnings", "asc"),
        "cols": _cols({"key": "days_since_earnings", "label": "Days ago", "fmt": "int"},
                      {"key": "eps_last_beat", "label": "Beat?", "fmt": "bool"},
                      {"key": "rev_growth_qyoy", "label": "Rev growth QoQ", "fmt": "pct_signed"},
                      C_ALPHA),
    },
    {
        "id": "beat-but-fell", "cat": "concall",
        "name": "Beat the number, fell anyway",
        "blurb": "The print was fine and the stock still sold off — the reason is on the call.",
        "note": "Our lines: last quarter beat, reported within 30 days, 1-month return negative.",
        "where": lambda r: (bool(r.get("eps_last_beat"))
                            and num(r, "days_since_earnings") is not None
                            and num(r, "days_since_earnings") <= 30
                            and le(pct(r, "momentum_1m"), 0)),
        "sort": ("momentum_1m", "asc"),
        "cols": _cols({"key": "momentum_1m", "label": "1-month", "fmt": "pct_signed"},
                      {"key": "days_since_earnings", "label": "Days ago", "fmt": "int"},
                      {"key": "avg_eps_surprise_pct", "label": "Avg surprise", "fmt": "pct_signed"},
                      C_ALPHA),
    },
    {
        "id": "guidance-risk", "cat": "concall",
        "name": "Estimates cut after the print",
        "blurb": "Analysts moved numbers down post-results — what did management say?",
        "note": "Our lines: net downward revisions, reported within 45 days.",
        "where": lambda r: (net_revisions(r) is not None and net_revisions(r) < 0
                            and num(r, "days_since_earnings") is not None
                            and num(r, "days_since_earnings") <= 45),
        "sort": ("_net_rev", "asc"),
        "cols": _cols({"key": "_net_rev", "label": "Net revisions", "fmt": "signed"},
                      {"key": "days_since_earnings", "label": "Days ago", "fmt": "int"},
                      {"key": "momentum_1m", "label": "1-month", "fmt": "pct_signed"},
                      C_ALPHA),
    },
]


_BY_ID = {s["id"]: s for s in SCANS}

CATEGORIES = [
    {"id": "stock", "name": "Stock Scans", "icon": "\U0001F50D",
     "blurb": "Find great companies effortlessly."},
    {"id": "result", "name": "Result Scans", "icon": "\U0001F4C8",
     "blurb": "Scan results, compare growth."},
    {"id": "market", "name": "Market Scans", "icon": "\U0001F30D",
     "blurb": "The full picture of the market."},
    {"id": "announcement", "name": "Announcement Scans", "icon": "\U0001F4E2",
     "blurb": "Scan company announcements."},
    {"id": "shareholding", "name": "Shareholding Scans", "icon": "\U0001F465",
     "blurb": "Track shareholdings, get notified."},
    {"id": "concall", "name": "Concall Scans", "icon": "\U0001F3A7",
     "blurb": "Earnings-call notes and sentiment.", "tag": "New"},
]


def catalogue() -> list[dict]:
    """Categories with their scans (no rows) — powers the menu."""
    out = []
    for c in CATEGORIES:
        items = [{"id": s["id"], "name": s["name"], "blurb": s["blurb"], "note": s.get("note", "")}
                 for s in SCANS if s["cat"] == c["id"]]
        out.append({**c, "scans": items, "count": len(items)})
    return out


def _cell(row: dict, col: dict):
    k = col["key"]
    if col["fmt"] in ("text", "ticker", "bool"):
        return row.get(k)
    if col["fmt"] == "pct":
        return pct(row, k)
    if col["fmt"] == "pct_signed":
        return pct(row, k)
    return num(row, k)


def source_for(scan_id: str) -> str:
    """Which dataset a scan reads: universe (default), sectors, or actions.
    The route uses this to hand run() the right list."""
    s = _BY_ID.get(scan_id)
    return (s or {}).get("src", "universe")


def _augment(r: dict) -> dict:
    """Derived fields the predicates and sorts reference but the row lacks."""
    nr = net_revisions(r)
    d = str(r.get("date") or "")
    return {**r, "_net_rev": nr, "_date_ord": d}


def run(scan_id: str, universe: list, limit: int = 50) -> dict:
    """Run one scan over the supplied rows. Always returns a dict (never raises)."""
    s = _BY_ID.get(scan_id)
    if not s:
        return {"available": False, "reason": "unknown_scan", "id": scan_id, "rows": []}
    universe = [_augment(r) for r in (universe or []) if isinstance(r, dict)]
    rows = []
    for r in (universe or []):
        try:
            if s["where"](r):
                rows.append(r)
        except Exception:
            # A malformed row must never take the whole scan down.
            continue
    field, direction = s["sort"]
    if field == "_date_ord":                      # ISO dates sort lexically
        rows.sort(key=lambda r: str(r.get("_date_ord") or "9999"),
                  reverse=(direction == "desc"))
    else:
        rows.sort(key=lambda r: (num(r, field) if num(r, field) is not None else float("-inf")),
                  reverse=(direction == "desc"))
    out = []
    for r in rows[:limit]:
        cells = {c["key"]: _cell(r, c) for c in s["cols"]}
        cells["ticker"] = str(r.get("ticker") or r.get("symbol") or "").upper()
        cells["name"] = r.get("name") or ""
        cells["sector"] = r.get("sector") or ""
        out.append(cells)
    return {"available": True, "id": s["id"], "cat": s["cat"], "name": s["name"],
            "blurb": s["blurb"], "note": s.get("note", ""), "cols": s["cols"],
            "matched": len(rows), "shown": len(out), "rows": out}
