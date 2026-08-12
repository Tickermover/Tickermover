"""
Tests for the Pro gate — the plan resolver and the subscription lookup.

WHY THESE EXIST. Two symptoms were reported in production on 12 Aug 2026: free
sessions rendering Pro chrome, and Pro users dropping to Free. Both traced to
the same thing — a failed lookup being indistinguishable from a genuine "free"
answer. Specifically:

  * auth.get_subscription returned {'plan':'free'} for ANY non-list response, so
    an expired token (401), a Supabase 5xx, a timeout and a missing table all
    read as "this user is on the free plan";
  * _is_pro_user then CACHED that False for 300s, turning one transient error
    into five minutes of paywall for a paying customer;
  * /api/user/me omitted the allow-list check that /api/user/account had, so a
    comped account was told plan='free' and the client persisted it.

Every assertion below pins one of those behaviours. A failure here means a
paying customer can be shown a paywall, or a free one shown Pro — treat it as
release-blocking.

Run: python tests/run_all.py   (no pytest needed)  — or  pytest tests/

NOTE ON THE ast LIFT. _comped_email and _resolve_plan live in app.py, which
constructs the DataCoordinator and the intelligence singletons at import time.
Importing it here would make a fast unit suite depend on all of that, so the two
pure functions are sliced out of the real source and executed against stubbed
globals. The test therefore exercises the SHIPPED text, not a copy that can
drift — and if either function grows a new dependency, this raises rather than
quietly passing.
"""
import ast
import asyncio
import pathlib
import types

import auth as authmod

ROOT = pathlib.Path(__file__).resolve().parent.parent


# ── Lift the two pure helpers out of app.py ──────────────────────────────────
def _load_plan_helpers(beta: bool = False):
    """Return (_comped_email, _resolve_plan) bound to stubbed globals."""
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    wanted = {"_comped_email", "_resolve_plan"}
    nodes = [n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name in wanted]
    assert len(nodes) == 2, f"expected both helpers in app.py, found {[n.name for n in nodes]}"
    g = {
        "Optional":         __import__("typing").Optional,
        "_PRO_ALLOW":       {"support@tickermover.com"},
        "_AI_ALLOW":        {"dev@example.com"},
        "_beta_pro_active": lambda: beta,
        "is_pro":           lambda plan, status: plan == "pro" and status == "active",
        "config":           types.SimpleNamespace(BETA_PRO_UNTIL="2026-08-01"),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<app.py lift>", "exec"), g)
    return g["_comped_email"], g["_resolve_plan"]


PAID = {"plan": "pro",  "status": "active",   "valid_until": "2026-09-01"}
FREE = {"plan": "free", "status": "active"}
DEAD = {"plan": "pro",  "status": "canceled"}
FAIL = {"plan": "free", "status": "active", "lookup_failed": True}


# ── _comped_email ─────────────────────────────────────────────────────────────
def test_comped_email_matches_allow_lists():
    comped, _ = _load_plan_helpers()
    assert comped("support@tickermover.com") is True      # PRO_ALLOW_EMAILS
    assert comped("dev@example.com") is True              # AI_ALLOW_EMAILS


def test_comped_email_is_case_and_space_insensitive():
    comped, _ = _load_plan_helpers()
    assert comped("  SUPPORT@TickerMover.com ") is True


def test_comped_email_rejects_strangers_and_empties():
    comped, _ = _load_plan_helpers()
    assert comped("nobody@example.com") is False
    assert comped("") is False
    assert comped(None) is False                          # must not raise


# ── _resolve_plan: the comped bug that started the investigation ─────────────
def test_comped_account_is_pro_even_when_the_lookup_fails():
    """The reported symptom: an allow-listed account showing FREE. It must not
    depend on Supabase at all — that is the point of an allow list."""
    _, resolve = _load_plan_helpers()
    p = resolve("support@tickermover.com", FAIL)
    assert p["pro"] is True
    assert p["unknown"] is False


def test_comped_account_is_pro_over_a_free_row():
    _, resolve = _load_plan_helpers()
    p = resolve("support@tickermover.com", FREE)
    assert p["pro"] is True
    assert p["comped"] is True
    assert p["plan"] == "pro"


# ── _resolve_plan: unknown must never read as free ───────────────────────────
def test_failed_lookup_is_unknown_not_free():
    _, resolve = _load_plan_helpers()
    p = resolve("someone@example.com", FAIL)
    assert p["unknown"] is True      # caller should 503, not report "free"
    assert p["pro"] is False         # and must not grant Pro on a guess


def test_genuine_free_row_is_not_unknown():
    _, resolve = _load_plan_helpers()
    assert resolve("someone@example.com", FREE)["unknown"] is False


def test_active_pro_row_resolves_to_pro():
    _, resolve = _load_plan_helpers()
    p = resolve("someone@example.com", PAID)
    assert p["pro"] is True
    assert p["valid_until"] == "2026-09-01"


def test_cancelled_subscription_is_not_pro():
    _, resolve = _load_plan_helpers()
    assert resolve("someone@example.com", DEAD)["pro"] is False


# ── _resolve_plan: beta window ───────────────────────────────────────────────
def test_beta_window_makes_every_account_pro():
    _, resolve = _load_plan_helpers(beta=True)
    assert resolve("someone@example.com", FREE)["pro"] is True


def test_beta_window_suppresses_unknown():
    """During beta the subscription answer is irrelevant, so a failed lookup
    must not 503 a user who is Pro regardless."""
    _, resolve = _load_plan_helpers(beta=True)
    assert resolve("someone@example.com", FAIL)["unknown"] is False


def test_beta_outranks_the_comped_label():
    _, resolve = _load_plan_helpers(beta=True)
    p = resolve("support@tickermover.com", FREE)
    assert p["pro"] is True
    assert p["comped"] is False      # reported as beta, not as team access


# ── auth.get_subscription: distinguishing failure from free ──────────────────
class _FakeResp:
    def __init__(self, status, payload=None, bad_json=False):
        self.status_code, self._p, self._bad = status, payload, bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._p


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp


def _sub(resp) -> dict:
    """Run get_subscription against a stubbed httpx, then restore the module."""
    real_httpx = authmod.httpx
    try:
        authmod.httpx = types.SimpleNamespace(AsyncClient=lambda **kw: _FakeClient(resp))
        c = authmod.SupabaseClient(url="https://x.supabase.co", anon_key="k", jwt_secret="j")
        return asyncio.run(c.get_subscription("token", "uid"))
    finally:
        authmod.httpx = real_httpx


def test_subscription_row_is_returned():
    assert _sub(_FakeResp(200, [PAID]))["plan"] == "pro"


def test_empty_list_is_a_real_free_answer():
    """200 + [] means the user genuinely has no row. That is free, NOT a
    failure — the signup trigger normally creates one, but its absence is not
    an error and must not 503 the account panel."""
    out = _sub(_FakeResp(200, []))
    assert out.get("lookup_failed") is None
    assert out["plan"] == "free"


def test_401_is_a_failed_lookup():
    assert _sub(_FakeResp(401, {"msg": "invalid jwt"}))["lookup_failed"] is True


def test_500_is_a_failed_lookup():
    assert _sub(_FakeResp(500, {}))["lookup_failed"] is True


def test_missing_table_is_a_failed_lookup():
    """PGRST205 — the table was never created. Read as 'everyone is free' once
    already; it is a failure, not an answer."""
    assert _sub(_FakeResp(404, {"code": "PGRST205"}))["lookup_failed"] is True


def test_non_json_body_is_a_failed_lookup():
    assert _sub(_FakeResp(200, bad_json=True))["lookup_failed"] is True


def test_unexpected_shape_is_a_failed_lookup():
    assert _sub(_FakeResp(200, {"unexpected": "dict"}))["lookup_failed"] is True


def test_network_error_is_a_failed_lookup():
    assert _sub(RuntimeError("connection reset"))["lookup_failed"] is True


def test_failed_lookup_still_exposes_free_defaults():
    """Back-compat: any caller that ignores the flag must behave exactly as it
    did before the flag existed."""
    out = _sub(_FakeResp(401, {}))
    assert out["plan"] == "free" and out["status"] == "active"


def test_disabled_client_reports_free_without_a_request():
    c = authmod.SupabaseClient(url="", anon_key="", jwt_secret="")
    out = asyncio.run(c.get_subscription("t", "u"))
    assert out["plan"] == "free"
    assert out.get("lookup_failed") is None
