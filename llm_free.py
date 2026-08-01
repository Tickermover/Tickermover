"""llm_free.py — free-tier LLM chain (no Anthropic cost).

One helper, `chat_json()`, that tries each configured FREE provider in order
and returns the first parsed JSON object. Built so the site's AI features keep
working without an Anthropic balance.

Provider order (first configured wins, then falls through on failure):
  1. GROQ_API_KEY    — console.groq.com. Fast, generous free tier.
                       Default model: llama-3.3-70b-versatile (~24k char input).
  2. GEMINI_API_KEY  — aistudio.google.com. Free tier, ~1M token context, so it
                       can read a whole 10-K without truncation. Best for long
                       filings. Default model: gemini-2.0-flash.
  3. CEREBRAS_API_KEY— cloud.cerebras.ai. Free tier, very fast Llama models.
  4. OPENROUTER_API_KEY — openrouter.ai. Routes to assorted free models.

All are OpenAI-compatible chat endpoints except Gemini, which has its own
REST shape — handled below.

Every provider failure is recorded in LAST_ERRORS so /api/event-intel-status
can show why a call fell through instead of failing silently.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

LAST_ERRORS: list[dict] = []
_MAX_ERRORS = 8


def _note(provider: str, detail: str) -> None:
    LAST_ERRORS.insert(0, {
        "provider": provider, "detail": str(detail)[:200],
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    del LAST_ERRORS[_MAX_ERRORS:]


_ALIASES = {
    "GEMINI_API_KEY":     ["GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY",
                            "GOOGLE_AI_API_KEY", "GEMINI_KEY", "GOOGLE_GEMINI_API_KEY"],
    "GROQ_API_KEY":       ["GROQ_API_KEY", "GROQ_KEY"],
    "CEREBRAS_API_KEY":   ["CEREBRAS_API_KEY", "CEREBRAS_KEY"],
    "OPENROUTER_API_KEY": ["OPENROUTER_API_KEY", "OPENROUTER_KEY", "OPEN_ROUTER_API_KEY"],
}


def _key(name: str) -> str:
    """Read a provider key, tolerating the common alias names people actually
    set in their host's dashboard (GOOGLE_API_KEY vs GEMINI_API_KEY, etc.)."""
    for n in _ALIASES.get(name, [name]):
        v = (os.environ.get(n, "") or "").strip()
        if v:
            return v
    return ""


# (name, env key, model env, default model, endpoint, max input chars)
_PROVIDERS = [
    ("groq", "GROQ_API_KEY", "GROQ_MODEL", "llama-3.3-70b-versatile",
     "https://api.groq.com/openai/v1/chat/completions", 24000),
    ("gemini", "GEMINI_API_KEY", "GEMINI_MODEL", "gemini-2.0-flash",
     "https://generativelanguage.googleapis.com/v1beta/models", 700000),
    ("cerebras", "CEREBRAS_API_KEY", "CEREBRAS_MODEL", "llama-3.3-70b",
     "https://api.cerebras.ai/v1/chat/completions", 20000),
    ("openrouter", "OPENROUTER_API_KEY", "OPENROUTER_MODEL",
     "meta-llama/llama-3.3-70b-instruct:free",
     "https://openrouter.ai/api/v1/chat/completions", 24000),
]


def available() -> list[str]:
    """Names of providers that have a key configured, in try order."""
    return [n for (n, envk, _m, _d, _u, _c) in _PROVIDERS if _key(envk)]


def status() -> dict:
    return {
        "providers": [
            {"name": n, "configured": bool(_key(envk)),
             "model": _key(menv) or dflt, "max_input_chars": cap}
            for (n, envk, menv, dflt, _u, cap) in _PROVIDERS
        ],
        "order": available(),
        "recent_errors": LAST_ERRORS[:5] or None,
    }


def parse_json(text: str) -> dict | None:
    """Tolerant JSON extraction (models like to wrap output in fences)."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:].strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        # Last resort: grab the outermost {...}
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            try:
                return json.loads(t[a:b + 1])
            except json.JSONDecodeError:
                return None
        return None


async def _call_openai_compatible(url: str, key: str, model: str, prompt: str,
                                  max_tokens: int, timeout: float) -> tuple[bool, str]:
    payload = {
        "model": model, "max_tokens": max_tokens, "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": prompt}],
    }
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(url, json=payload,
                         headers={"Authorization": f"Bearer {key}",
                                  "Content-Type": "application/json"})
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:160]}"
    body = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content", "")
    return True, body


async def _call_gemini(base: str, key: str, model: str, prompt: str,
                       max_tokens: int, timeout: float) -> tuple[bool, str]:
    url = f"{base}/{model}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": max_tokens,
                             "responseMimeType": "application/json"},
    }
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(url, json=payload,
                         headers={"Content-Type": "application/json",
                                  "x-goog-api-key": key})
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:160]}"
    data = r.json()
    parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [{}])
    return True, "".join(p.get("text", "") for p in parts)


async def chat_json(prompt: str, *, max_tokens: int = 1500, timeout: float = 30.0,
                    prefer: str | None = None) -> dict | None:
    """Run `prompt` through the free-provider chain; return parsed JSON.

    `prefer` moves one provider to the front (e.g. "gemini" for long filings
    that would be truncated elsewhere). Returns None only when every
    configured provider fails — check LAST_ERRORS for why.
    """
    order = list(_PROVIDERS)
    if prefer:
        order.sort(key=lambda p: 0 if p[0] == prefer else 1)
    tried = 0
    for (name, envk, menv, dflt, url, cap) in order:
        key = _key(envk)
        if not key:
            continue
        tried += 1
        model = _key(menv) or dflt
        try:
            if name == "gemini":
                ok, body = await _call_gemini(url, key, model, prompt[:cap], max_tokens, timeout)
            else:
                ok, body = await _call_openai_compatible(url, key, model, prompt[:cap],
                                                         max_tokens, timeout)
        except Exception as exc:
            _note(name, f"exception: {exc}")
            continue
        if not ok:
            _note(name, body)
            continue
        parsed = parse_json(body)
        if parsed is None:
            _note(name, f"unparseable response: {body[:120]}")
            continue
        logger.info(f"llm_free: answered by {name} ({model})")
        return parsed
    if tried == 0:
        _note("none", "no free provider configured (set GROQ_API_KEY or GEMINI_API_KEY)")
    return None


async def _call_text(name, url, key, model, prompt, max_tokens, timeout, cap):
    """Same providers, but plain-text output (no JSON response_format)."""
    if name == "gemini":
        u = f"{url}/{model}:generateContent"
        payload = {"contents": [{"parts": [{"text": prompt[:cap]}]}],
                   "generationConfig": {"temperature": 0.3, "maxOutputTokens": max_tokens}}
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(u, json=payload,
                             headers={"Content-Type": "application/json",
                                      "x-goog-api-key": key})
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:160]}"
        parts = (((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or [{}])
        return True, "".join(p.get("text", "") for p in parts)
    payload = {"model": model, "max_tokens": max_tokens, "temperature": 0.3,
               "messages": [{"role": "user", "content": prompt[:cap]}]}
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(url, json=payload,
                         headers={"Authorization": f"Bearer {key}",
                                  "Content-Type": "application/json"})
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:160]}"
    body = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content", "")
    return True, body


async def chat_text(prompt: str, *, max_tokens: int = 1500, timeout: float = 45.0,
                    prefer: str | None = None) -> str | None:
    """Plain-text completion through the free chain. Returns None when every
    configured provider fails (see LAST_ERRORS)."""
    order = list(_PROVIDERS)
    if prefer:
        order.sort(key=lambda p: 0 if p[0] == prefer else 1)
    tried = 0
    for (name, envk, menv, dflt, url, cap) in order:
        key = _key(envk)
        if not key:
            continue
        tried += 1
        model = _key(menv) or dflt
        try:
            ok, body = await _call_text(name, url, key, model, prompt, max_tokens, timeout, cap)
        except Exception as exc:
            _note(name, f"text exception: {exc}")
            continue
        if not ok:
            _note(name, body)
            continue
        if body and body.strip():
            logger.info(f"llm_free: text answered by {name} ({model})")
            return body
        _note(name, "empty text response")
    if tried == 0:
        _note("none", "no free provider configured")
    return None
