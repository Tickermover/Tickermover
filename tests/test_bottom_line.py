"""
Tests for bottom_line_ai._sig — the cache signature whose drift caused the
Jun-19 Haiku cost bleed. The fix keys on a COARSE material signature so a tiny
daily number wiggle no longer invalidates the cache. These tests pin that.

Run: python tests/run_all.py   (no pytest needed)
"""
import bottom_line_ai
from bottom_line_ai import _sig


def _stock(grade="A", score=77.0, growth="hyper", cautions=None):
    return {"grade": grade, "smart_score": score, "growth_tier": growth,
            "caution_reasons": cautions or []}


def test_identical_material_inputs_same_sig():
    assert _sig(_stock()) == _sig(_stock())


def test_small_score_wiggle_within_bucket_is_stable():
    # 76 and 79 are in the same 5-point bucket (//5 == 15) -> same signature,
    # so an intraday score wiggle must NOT trigger a paid regeneration.
    assert _sig(_stock(score=76.0)) == _sig(_stock(score=79.0))


def test_crossing_a_bucket_changes_sig():
    # 79 (bucket 15) vs 81 (bucket 16) -> different signature (real move).
    assert _sig(_stock(score=79.0)) != _sig(_stock(score=81.0))


def test_grade_change_changes_sig():
    assert _sig(_stock(grade="A")) != _sig(_stock(grade="B"))


def test_caution_set_changes_sig():
    assert _sig(_stock(cautions=[])) != _sig(_stock(cautions=[("rsi", "overbought")]))


def test_missing_fields_do_not_crash():
    assert isinstance(_sig({}), str)
