"""
TickerMover — shared Alpha Vantage daily-call budget.

Alpha Vantage's free tier is ~25 calls/day across the WHOLE key. Three code
paths spend from that one pool: fundamentals (data_coordinator.get_fundamentals),
earnings-call transcripts (event_intel._fetch_av_transcript) and the PDF price
fallback (app.api_pdf). They used to count independently (only fundamentals had a
guard), so transcripts/PDF could silently exhaust the quota. This module is the
single source of truth — every AV consumer checks/spends here.
"""
from __future__ import annotations

import datetime
import logging

import config

logger = logging.getLogger(__name__)

_calls_today = 0
_reset_day: datetime.date | None = None
# Set when Alpha Vantage itself answers with an "Information" notice (daily cap
# reached, or a premium-only endpoint on a free key). Our own counter cannot see
# either: the cap is per-key across every process, and a premium endpoint burns
# a request and returns nothing useful no matter how much budget is left. Until
# this trips, one concall load spends up to FOUR calls probing quarters that
# were never going to be served, which is what drained the pool for the paths
# that do work (fundamentals, the PDF fallback). Day-scoped — clears on _roll().
_blocked_reason: str | None = None


def _roll() -> None:
    global _calls_today, _reset_day, _blocked_reason
    today = datetime.date.today()
    if today != _reset_day:
        _calls_today = 0
        _reset_day = today
        _blocked_reason = None


def mark_blocked(reason: str = "") -> None:
    """Alpha Vantage refused on its own terms — stop spending for today."""
    global _blocked_reason
    _roll()
    _blocked_reason = (reason or "rate limited")[:200]
    logger.warning(f"av_budget: Alpha Vantage blocked for today — {_blocked_reason}")


def blocked() -> str | None:
    _roll()
    return _blocked_reason


def remaining() -> int:
    _roll()
    return max(0, int(config.AV_CALLS_PER_DAY) - _calls_today)


def spend(n: int = 1) -> None:
    global _calls_today
    _roll()
    _calls_today += n


def try_spend(n: int = 1) -> bool:
    """Reserve n calls if the budget allows; return False (and spend nothing)
    when exhausted, or when Alpha Vantage has already refused today, so the
    caller can skip the AV request entirely."""
    if blocked():
        return False
    if remaining() < n:
        return False
    spend(n)
    return True
