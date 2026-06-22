"""
Tests for ai_scorer.py — the scoring core. Focus on robustness (the audit's NaN /
empty-input findings) and the grade thresholds, since a crash here blanks the
whole universe.

Run: python tests/run_all.py   (no pytest needed)
"""
import math

from ai_scorer import compute_pop_score

_VALID_GRADES = {"A", "B", "C", "D", "F"}


def test_empty_dict_does_not_crash_and_grades():
    out = compute_pop_score({}, regime=None)
    assert isinstance(out, dict)
    assert out.get("grade") in _VALID_GRADES
    assert 0 <= float(out.get("smart_score", out.get("pop_score") or 0)) <= 100


def test_nan_inputs_do_not_crash():
    # The audit flagged that NaN numeric pillars route to a wrong bucket — the
    # contract this test pins is the weaker but critical one: never raise.
    t = {"momentum_1m": float("nan"), "rsi_14": float("nan"),
         "rel_strength": float("nan"), "revenue_growth_yoy": float("nan")}
    out = compute_pop_score(t, regime=None)
    assert out.get("grade") in _VALID_GRADES
    sc = float(out.get("smart_score", out.get("pop_score") or 0))
    assert not math.isnan(sc)


def test_none_pillar_values_do_not_crash():
    t = {k: None for k in ("momentum_1m", "rsi_14", "rel_strength",
                           "revenue_growth_yoy", "eps_growth_yoy", "pe_ratio")}
    out = compute_pop_score(t, regime=None)
    assert out.get("grade") in _VALID_GRADES


def test_grade_is_monotonic_with_score():
    # Two synthetic stocks: a clearly strong one should never grade BELOW a
    # clearly weak one. (We don't assert exact grades — only ordering — so the
    # test survives weight tuning.)
    strong = {"momentum_1m": 18, "rel_strength": 80, "revenue_growth_yoy": 35,
              "eps_growth_yoy": 40, "rsi_14": 60, "analyst_strong_buy": 70}
    weak = {"momentum_1m": -20, "rel_strength": 10, "revenue_growth_yoy": -15,
            "eps_growth_yoy": -30, "rsi_14": 25}
    gs = compute_pop_score(strong, regime=None)
    gw = compute_pop_score(weak, regime=None)
    order = {"F": 0, "D": 1, "C": 2, "B": 3, "A": 4}
    assert order[gs["grade"]] >= order[gw["grade"]]
