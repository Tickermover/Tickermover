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


def _roll() -> None:
    global _calls_today, _reset_day
    today = datetime.date.today()
    if today != _reset_day:
        _calls_today = 0
        _reset_day = today


def remaining() -> int:
    _roll()
    return max(0, int(config.AV_CALLS_PER_DAY) - _calls_today)


def spend(n: int = 1) -> None:
    global _calls_today
    _roll()
    _calls_today += n


def try_spend(n: int = 1) -> bool:
    """Reserve n calls if the budget allows; return False (and spend nothing)
    when exhausted so the caller can skip the AV request entirely."""
    if remaining() < n:
        return False
    spend(n)
    return True
