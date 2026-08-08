"""anthropic_shim.py — drop-in replacement for Anthropic Messages calls.

Every AI feature in this codebase posts the same way:

    r = await c.post("https://api.anthropic.com/v1/messages",
                     json=payload, headers=headers)
    ... r.status_code ... r.json()["content"][0]["text"] ...

This module provides `post(json=..., headers=...)` returning an object with the
same `.status_code`, `.text` and `.json()` surface, but answered by the FREE
provider chain in `llm_free` (Groq / Gemini / Cerebras / OpenRouter). That lets
every call site switch over without touching its prompts or its parsing.

Behaviour:
  * Anthropic-style payload (system + messages + max_tokens) is flattened into
    one prompt.
  * Returns an Anthropic-shaped body: {"content":[{"type":"text","text":...}]}
  * If a call site asked for JSON output (its prompt says so), the chain's JSON
    mode is used and the object is re-serialised as text — parsing downstream
    is unchanged.
  * ANTHROPIC_FALLBACK=1 re-enables the real Anthropic API as a last resort
    (off by default: an exhausted balance just wastes a round-trip).

Known limitation: server-side tools (Anthropic's `web_search`) have no
equivalent on the free providers. Those requests still run, but without live
web grounding — the caller gets model-knowledge output instead. `used_tools`
is logged so this is visible rather than silent.
"""
from __future__ import annotations

import json as _json
import logging
import os

logger = logging.getLogger(__name__)

_ALLOW_ANTHROPIC = (os.environ.get("ANTHROPIC_FALLBACK", "") or "").strip() in ("1", "true", "yes")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


class ShimResponse:
    """Minimal httpx.Response stand-in: status_code / text / json()."""

    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = _json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"shim HTTP {self.status_code}: {self.text[:200]}")


def _flatten(payload: dict) -> tuple[str, int, bool]:
    """Anthropic payload -> (prompt, max_tokens, wants_json)."""
    parts: list[str] = []
    system = payload.get("system")
    if isinstance(system, list):     # content-block form
        system = " ".join(b.get("text", "") for b in system if isinstance(b, dict))
    if system:
        parts.append(str(system).strip())
    for m in (payload.get("messages") or []):
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") in (None, "text")
            )
        if content:
            role = m.get("role", "user")
            parts.append(str(content) if role == "user" else f"[{role}] {content}")
    prompt = "\n\n".join(p for p in parts if p)
    # Sniff BOTH ends for the JSON instruction.
    #
    # This used to read prompt[-4000:] only, on the assumption that "return only
    # JSON" sits near the end. Most prompts in this codebase are built the other
    # way round — schema first, then tens of thousands of characters of filing or
    # transcript appended — so the tail is raw source prose, the sniff said "no
    # JSON", the provider answered in free text, and the caller's strict
    # json.loads failed. Every time, for every ticker. That is exactly why the
    # Concall summary returned generation_failed permanently while the Earnings
    # Brief (which calls llm_free.chat_json directly) was fine.
    #
    # Widening the window is safe: a false positive costs nothing, because
    # post() already falls back to chat_text when JSON mode yields nothing.
    head = prompt[:4000].lower()
    tail = prompt[-4000:].lower()

    def _asks_json(s: str) -> bool:
        return "json" in s and ("return only" in s or "respond with" in s
                                or "json object" in s or "output json" in s
                                or "only json" in s or "as json" in s)

    wants_json = _asks_json(head) or _asks_json(tail) or (
        "json" in tail and '"' in tail[-500:] and "{" in tail)
    return prompt, int(payload.get("max_tokens") or 1500), wants_json


async def post(url: str | None = None, *, json: dict | None = None,
               headers: dict | None = None, timeout: float = 60.0,
               force_json: bool | None = None, **_kw):
    """Drop-in for `client.post(ANTHROPIC_URL, json=..., headers=...)`.

    `force_json` overrides the sniff. A caller that is going to parse the reply
    with json.loads knows that for certain — it should say so rather than hope
    a keyword survives in the last 4000 characters of a 46k-char prompt."""
    payload = json or {}
    prompt, max_tokens, wants_json = _flatten(payload)
    if force_json is not None:
        wants_json = bool(force_json)
    if payload.get("tools"):
        logger.info("anthropic_shim: request used tools (%s) — free providers "
                    "have no server-side tool support, answering without them",
                    [t.get("name") or t.get("type") for t in payload["tools"]][:3])
    if not prompt.strip():
        return ShimResponse(400, {"error": {"message": "empty prompt after flattening"}})

    import llm_free
    text = None
    if llm_free.available():
        # Long inputs go to the big-context provider first.
        prefer = "gemini" if len(prompt) > 20000 else None
        if wants_json:
            obj = await llm_free.chat_json(prompt, max_tokens=max_tokens,
                                           timeout=timeout, prefer=prefer)
            if obj is not None:
                text = _json.dumps(obj)
        if text is None:
            text = await llm_free.chat_text(prompt, max_tokens=max_tokens,
                                            timeout=timeout, prefer=prefer)

    if text:
        try:
            text = llm_free.strip_fence(text)
        except Exception:
            pass
        return ShimResponse(200, {
            "id": "shim", "type": "message", "role": "assistant",
            "model": "llm_free", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        })

    if _ALLOW_ANTHROPIC and (headers or {}).get("x-api-key"):
        import httpx
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                return await c.post(ANTHROPIC_URL, json=payload, headers=headers)
        except Exception as exc:
            return ShimResponse(502, {"error": {"message": f"anthropic fallback failed: {exc}"}})

    errs = getattr(__import__("llm_free"), "LAST_ERRORS", [])
    detail = errs[0]["detail"] if errs else "no free provider configured"
    return ShimResponse(503, {"error": {"message": f"free providers unavailable: {detail}"}})
