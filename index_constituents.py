"""
TickerMover — Index constituent registry.

Static snapshot (Nov 2024) of S&P 500, NASDAQ-100, and Dow Jones
Industrial Average constituents. Used by:
- profile_rules.assign_profile() to tag each ticker with eligibility
  for Conservative / Balanced / Aggressive universes
- /api/universe payload to expose index membership per ticker
- Frontend coverage stats ("Conservative: 12 of 30 Dow tracked")

MAINTENANCE:
- Memberships change ~5-15 times per index per year (quarterly rebalance).
- When the index publishes an add/drop, edit the lists below directly.
- The lists below are intentionally compact (symbol only); names + sectors
  come from FMP/Polygon at ingestion time.

COVERAGE NOTE:
- DOW_30: complete (30/30).
- NASDAQ_100: complete (~100/100).
- SP500: covers the top ~280 by market cap (>90% of index weight).
  The long tail of small / regional names is intentionally omitted from
  this snapshot — expand as needed when you care about that bucket.
"""
from __future__ import annotations

# ── Dow Jones Industrial Average (30) ───────────────────────────
DOW_30: list[str] = [
    "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX",
    "DIS", "GS", "HD", "HON", "IBM", "JNJ", "JPM", "KO", "MCD",
    "MMM", "MRK", "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH",
    "V", "VZ", "WMT",
]

# ── NASDAQ-100 (Dec 2024) ───────────────────────────────────────
NASDAQ_100: list[str] = [
    "AAPL", "MSFT", "AMZN", "NVDA", "META", "GOOGL", "GOOG", "TSLA",
    "AVGO", "COST", "NFLX", "ADBE", "AMD", "PEP", "CSCO", "TMUS",
    "INTC", "CMCSA", "INTU", "QCOM", "TXN", "AMGN", "ISRG", "AMAT",
    "MU", "BKNG", "HON", "VRTX", "ADI", "LRCX", "PANW", "REGN",
    "GILD", "ADP", "MELI", "PYPL", "SBUX", "KLAC", "MDLZ", "SNPS",
    "ABNB", "CDNS", "MAR", "ASML", "ORLY", "CTAS", "CRWD", "MRVL",
    "CSX", "ROP", "NXPI", "FTNT", "FANG", "ADSK", "AEP", "MNST",
    "DASH", "CHTR", "KDP", "MCHP", "PCAR", "IDXX", "KHC", "ROST",
    "PAYX", "ODFL", "EXC", "GEHC", "AZN", "CTSH", "FAST", "EA",
    "VRSK", "BKR", "DDOG", "XEL", "BIIB", "DXCM", "CSGP", "ANSS",
    "CCEP", "CPRT", "ON", "LULU", "ZS", "TEAM", "GFS", "DLTR",
    "WDAY", "TTWO", "TTD", "ARM", "MDB", "WBD", "ILMN", "PDD",
    "SIRI", "WBA", "CDW", "CEG", "SMCI", "MRNA",
]

# ── S&P 500 (top ~280 by market cap, Nov 2024 snapshot) ─────────
# Curated to cover >90% of index weight. Add long-tail names when
# you need broader Conservative-universe coverage.
SP500: list[str] = [
    # Mega-cap tech (already heavily in our tech universe)
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "TSLA",
    "AVGO", "ORCL", "ADBE", "CRM", "AMD", "QCOM", "INTU", "TXN",
    "INTC", "IBM", "NOW", "AMAT", "MU", "LRCX", "KLAC", "ADI",
    "MRVL", "PANW", "CDNS", "SNPS", "FTNT", "CRWD", "WDAY", "ANSS",
    "MCHP", "ON", "ROP", "PAYX", "ADP", "MSI", "FICO", "GLW",
    "ANET", "VRSN", "EPAM", "GEN", "TYL", "PTC", "TER", "NXPI",
    # Communications / Media / Telecom
    "NFLX", "DIS", "CMCSA", "T", "VZ", "TMUS", "WBD", "PARA",
    "EA", "TTWO", "TKO", "FOXA", "FOX", "NWSA", "LYV", "OMC",
    "IPG", "MTCH", "PINS", "TTD",
    # Financials (banks, insurance, asset managers, payments, exchanges)
    "JPM", "BAC", "WFC", "GS", "MS", "C", "AXP", "BLK", "BX",
    "SCHW", "KKR", "SPGI", "MCO", "ICE", "CME", "MMC", "AON",
    "AJG", "PGR", "TRV", "ALL", "CB", "MET", "PRU", "AIG",
    "TFC", "USB", "PNC", "COF", "BK", "STT", "FITB", "NTRS",
    "RF", "HBAN", "MTB", "CFG", "KEY", "SYF", "DFS", "AMP",
    "TROW", "BEN", "RJF", "NDAQ", "CBOE", "V", "MA", "PYPL",
    "FI", "FIS", "JKHY", "BRK.B", "BRO", "ACGL", "WTW", "EG",
    "L", "HIG", "AFL", "CINF", "RGA", "GL", "ERIE",
    # Healthcare (pharma, devices, providers, insurers, biotech)
    "LLY", "UNH", "JNJ", "MRK", "ABBV", "TMO", "ABT", "DHR",
    "PFE", "BMY", "AMGN", "GILD", "VRTX", "ISRG", "SYK", "MDT",
    "BSX", "EW", "IDXX", "ZBH", "BAX", "BDX", "RMD", "DXCM",
    "STE", "WAT", "MTD", "WST", "PODD", "HOLX", "REGN", "BIIB",
    "MRNA", "ILMN", "VTRS", "ZTS", "ELV", "CI", "HUM", "CVS",
    "MCK", "COR", "CAH", "HCA", "UHS", "DGX", "LH", "IQV",
    "A", "GEHC", "CRL", "DVA", "INCY", "BIO",
    # Consumer Discretionary
    "AMZN", "TSLA", "HD", "LOW", "MCD", "BKNG", "SBUX", "NKE",
    "TJX", "ROST", "MAR", "HLT", "LULU", "ABNB", "CMG", "ORLY",
    "AZO", "GM", "F", "RCL", "CCL", "NCLH", "POOL", "EXPE",
    "YUM", "DRI", "DPZ", "TSCO", "ULTA", "BBY", "DHI", "LEN",
    "PHM", "NVR", "TOL", "MHK", "WHR", "GRMN", "EBAY", "ETSY",
    "APTV", "BWA",
    # Consumer Staples (defensive — heart of Conservative universe)
    "WMT", "PG", "COST", "KO", "PEP", "PM", "MO", "MDLZ",
    "CL", "GIS", "HSY", "K", "KHC", "SJM", "CAG", "CPB",
    "TSN", "ADM", "TAP", "STZ", "MNST", "KDP", "TGT", "KR",
    "DG", "DLTR", "SYY", "KMB", "CHD", "CLX", "ELC", "EL",
    # Industrials
    "CAT", "BA", "HON", "GE", "UPS", "RTX", "LMT", "DE",
    "UNP", "ETN", "MMM", "PH", "EMR", "ITW", "GD", "NOC",
    "TT", "CSX", "NSC", "FDX", "PCAR", "WM", "RSG", "WCN",
    "GWW", "FAST", "PNR", "ROK", "DOV", "URI", "AME", "OTIS",
    "JCI", "FTV", "XYL", "IR", "DAL", "UAL", "LUV", "ALK",
    "CHRW", "EXPD", "JBHT", "ODFL", "NDSN", "ROL", "CARR",
    "LDOS", "BAH", "GNRC", "TXT", "LHX", "CW", "AOS",
    # Energy
    "XOM", "CVX", "COP", "EOG", "MPC", "PSX", "PXD", "SLB",
    "OXY", "VLO", "OKE", "WMB", "FANG", "KMI", "BKR", "HAL",
    "HES", "EQT", "TRGP", "DVN", "CTRA",
    # Utilities (heart of Conservative)
    "NEE", "DUK", "SO", "AEP", "SRE", "CEG", "D", "PCG",
    "EXC", "ED", "WEC", "ETR", "ES", "EIX", "XEL", "PEG",
    "AEE", "FE", "CMS", "DTE", "PPL", "AWK", "CNP", "LNT",
    "EVRG", "ATO", "NRG", "PNW",
    # Materials
    "LIN", "SHW", "APD", "ECL", "FCX", "NUE", "CTVA", "DD",
    "DOW", "PPG", "VMC", "MLM", "NEM", "STLD", "PKG", "CF",
    "ALB", "IFF", "MOS", "BALL", "AMCR", "AVY", "EMN",
    # Real Estate / REITs
    "PLD", "AMT", "EQIX", "WELL", "PSA", "SPG", "O", "DLR",
    "CCI", "VTR", "EXR", "AVB", "EQR", "SBAC", "ARE", "INVH",
    "MAA", "ESS", "CPT", "HST", "REG", "FRT", "KIM", "BXP",
    "VICI", "DOC", "UDR", "AIRC", "WPC", "IRM",
]

# Deduped union of all three indices
ALL_INDEX_TICKERS: set[str] = set(DOW_30) | set(NASDAQ_100) | set(SP500)


def indices_for(ticker: str) -> list[str]:
    """Return which indices a ticker belongs to."""
    t = (ticker or "").upper()
    out: list[str] = []
    if t in DOW_30:     out.append("DJI")
    if t in NASDAQ_100: out.append("NDX")
    if t in SP500:      out.append("SPX")
    return out


def coverage_summary(tracked_tickers: list[str]) -> dict:
    """Return how many of each index we currently have ingested.
    Used by /api/profile-coverage for the dashboard coverage line."""
    tracked = {(t or "").upper() for t in tracked_tickers}
    return {
        "DJI": {"tracked": len(tracked & set(DOW_30)),     "total": len(DOW_30)},
        "NDX": {"tracked": len(tracked & set(NASDAQ_100)), "total": len(NASDAQ_100)},
        "SPX": {"tracked": len(tracked & set(SP500)),      "total": len(SP500)},
    }
