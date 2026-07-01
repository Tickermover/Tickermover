"""
Tests for usage_log.py — AI cost estimation (the input to the daily + monthly
spend caps). If the pricing math drifts, the $50/month ceiling is wrong.

Run: python tests/run_all.py   (no pytest needed)
"""
import usage_log
from usage_log import estimate_cost


def test_tier_mapping():
    assert usage_log._tier("claude-opus-4-8") == "opus"
    assert usage_log._tier("claude-sonnet-5") == "sonnet"
    assert usage_log._tier("claude-haiku-4-5-20251001") == "haiku"
    assert usage_log._tier("") == "haiku"          # default to cheapest tier name
    assert usage_log._tier(None) == "haiku"


def test_haiku_pricing_per_million():
    # 1M no-cache input = $1.00, 1M output = $5.00 for Haiku.
    assert round(estimate_cost("claude-haiku-4-5", 1_000_000, 0, 0, 0, 0), 6) == 1.0
    assert round(estimate_cost("claude-haiku-4-5", 0, 0, 0, 1_000_000, 0), 6) == 5.0


def test_sonnet_and_opus_more_expensive_than_haiku():
    args = (1_000_000, 0, 0, 1_000_000, 0)
    h = estimate_cost("claude-haiku-4-5", *args)
    s = estimate_cost("claude-sonnet-5", *args)
    o = estimate_cost("claude-opus-4-8", *args)
    assert h < s < o


def test_web_search_adds_per_request_fee():
    base = estimate_cost("claude-haiku-4-5", 0, 0, 0, 0, 0)
    with_web = estimate_cost("claude-haiku-4-5", 0, 0, 0, 0, 3)
    assert round(with_web - base, 6) == round(3 * usage_log._WEB_SEARCH, 6)


def test_zero_usage_is_zero_cost():
    assert estimate_cost("claude-haiku-4-5", 0, 0, 0, 0, 0) == 0.0
