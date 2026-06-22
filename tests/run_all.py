"""
Zero-dependency test runner for the tests/ suite.

The repo has no pytest installed, so this discovers every `test_*` function in the
`tests/test_*.py` modules, runs them, and reports pass/fail. It's also fully
pytest-compatible — once pytest is available, `pytest tests/` runs the same tests.

Usage:
    python tests/run_all.py
Exit code is non-zero if any test fails (so it can gate CI).
"""
import importlib
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))          # import project modules (billing, ai_scorer, …)
sys.path.insert(0, str(ROOT / "tests"))

TEST_MODULES = [
    "test_billing",
    "test_usage_log",
    "test_scoring",
    "test_bottom_line",
]


def main() -> int:
    passed = failed = 0
    failures = []
    for modname in TEST_MODULES:
        mod = importlib.import_module(modname)
        tests = [(n, getattr(mod, n)) for n in dir(mod)
                 if n.startswith("test_") and callable(getattr(mod, n))]
        for name, fn in tests:
            try:
                fn()
                passed += 1
                print(f"  PASS  {modname}.{name}")
            except Exception as e:  # noqa: BLE001 — a test failure is any exception
                failed += 1
                failures.append((f"{modname}.{name}", e, traceback.format_exc()))
                print(f"  FAIL  {modname}.{name}  -> {type(e).__name__}: {e}")

    print(f"\n{'='*52}\n{passed} passed, {failed} failed")
    if failures:
        print("\nFAILURE DETAIL:")
        for ref, _e, tb in failures:
            print(f"\n--- {ref} ---\n{tb}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
