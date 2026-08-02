"""llm_free.py — free-tier LLM chain (no Anthropic cost).

`chat_json()` / `chat_text()` try each configured FREE provider in order and
return the first good answer, so every AI feature keeps working on £0 spend.

Provider order is QUALITY FIRST (first configured wins, falls through on
failure or rate limit). Set any subset — the chain adapts automatically:

  1. GEMINI_API_KEY      aistudio.google.com   ~1M-token context. Best quality
                         and the only one that reads a whole 10-K uncut.
                         Upgrade with GEMINI_MODEL=gemini-2.5-pro.
  2. NVIDIA_API_KEY      build.nvidia.com      NVIDIA NIM, OpenAI-compatible.
                         Free credits, large open models. Try
                         NVIDIA_MODEL=deepseek-ai/deepseek-r1 or
                         nvidia/llama-3.1-nemotron-70b-instruct for reasoning.
  3. GROQ_API_KEY        console.groq.com      Fastest; small context; the
                         free tier hits token 429s under load.
  4. CEREBRAS_API_KEY    cloud.cerebras.ai     Very fast, generous daily tokens.
  5. MISTRAL_API_KEY     console.mistral.ai    Free tier, strong European model.
  6. GITHUB_MODELS_TOKEN github.com/marketplace/models  Free with a GitHub
                         account; reaches GPT-4o class models.
  7. TOGETHER_API_KEY    api.together.xyz      Free Llama endpoints.
  8. OPENROUTER_API_KEY  openrouter.ai         Free model pool; good backstop.

Key lookup is forgiving: aliases (GOOGLE_API_KEY for Gemini) AND loose names
("Gemini API Key" with spaces) both resolve — see `_key`/`_norm`.

All providers are OpenAI-compatible except Gemini, which has its own REST
shape. Failures are recorded in LAST_ERRORS and surfaced by
/api/event-intel-status so a broken provider is visible, never silent.
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
    "NVIDIA_API_KEY":     ["NVIDIA_API_KEY", "NVIDIA_NIM_API_KEY", "NIM_API_KEY", "NVIDIA_KEY"],
    "MISTRAL_API_KEY":    ["MISTRAL_API_KEY", "MISTRAL_KEY"],
    "GITHUB_MODELS_TOKEN": ["GITHUB_MODELS_TOKEN", "GITHUB_MODELS_KEY", "GH_MODELS_TOKEN"],
    "TOGETHER_API_KEY":   ["TOGETHER_API_KEY", "TOGETHERAI_API_KEY", "TOGETHER_KEY"],
}


def _norm(s: str) -> str:
    """Normalise an env var name for fuzzy matching: upper-case, and treat
    spaces/dashes as underscores. Host dashboards happily accept names like
    'Gemini API Key', which no exact os.environ lookup would ever find."""
    return "".join(ch if ch.isalnum() else "_" for ch in (s or "").upper())


def _key(name: str) -> str:
    """Read a provider key, tolerating (a) common alias names and (b) loose
    naming such as 'Gemini API Key' or 'gemini-api-key'."""
    for n in _ALIASES.get(name, [name]):
        v = (os.environ.get(n, "") or "").strip()
        if v:
            return v
    wanted = {_norm(n) for n in _ALIASES.get(name, [name])}
    for k, v in os.environ.items():
        if _norm(k) in wanted and (v or "").strip():
            return v.strip()
    return ""


# (name, env key, model env, default model, endpoint, max input chars)
# ORDER = quality first. Gemini leads: biggest context and the closest
# free-tier answer quality to Claude. NVIDIA NIM follows (large open models,
# free credits), then the fast small-context providers, then backstops.
_PROVIDERS = [
    ("gemini", "GEMINI_API_KEY", "GEMINI_MODEL", "gemini-2.0-flash",
     "https://generativelanguage.googleapis.com/v1beta/models", 700000),
    ("nvidia", "NVIDIA_API_KEY", "NVIDIA_MODEL", "meta/llama-3.3-70b-instruct",
     "https://integrate.api.nvidia.com/v1/chat/completions", 60000),
    ("groq", "GROQ_API_KEY", "GROQ_MODEL", "llama-3.3-70b-versatile",
     "https://api.groq.com/openai/v1/chat/completions", 24000),
    ("cerebras", "CEREBRAS_API_KEY", "CEREBRAS_MODEL", "llama3.3-70b",
     "https://api.cerebras.ai/v1/chat/completions", 20000),
    ("mistral", "MISTRAL_API_KEY", "MISTRAL_MODEL", "mistral-large-latest",
     "https://api.mistral.ai/v1/chat/completions", 100000),
    ("together", "TOGETHER_API_KEY", "TOGETHER_MODEL",
     "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
     "https://api.together.xyz/v1/chat/completions", 100000),
    # GitHub Models is mid-retirement (returns HTTP 410 brownout) — kept last
    # so an existing token is still tried, but it is no longer worth adding.
    ("github", "GITHUB_MODELS_TOKEN", "GITHUB_MODEL", "openai/gpt-4o",
     "https://models.github.ai/inference/chat/completions", 100000),
    ("openrouter", "OPENROUTER_API_KEY", "OPENROUTER_MODEL",
     "meta-llama/llama-3.3-70b-instruct:free",
     "https://openrouter.ai/api/v1/chat/completions", 24000),
]


def available() -> list[str]:
    """Names of providers that have a key configured, in try order."""
    return [n for (n, envk, _m, _d, _u, _c) in _PROVIDERS if _key(envk)]


def env_key_names() -> list[str]:
    """NAMES ONLY (never values) of env vars that look like an AI provider key.
    Lets you see what a key was actually named in the host dashboard when a
    provider reports 'not configured' despite having been added."""
    pat = ("API_KEY", "_KEY", "TOKEN")
    skip = ("SUPABASE", "SERVICE", "ANON", "SECRET", "PASSWORD", "WEBHOOK",
            "STRIPE", "RESEND", "POLYGON", "FMP", "ALPHA", "FINNHUB", "ALPACA",
            "UNSPLASH", "PEXELS", "GH_", "GITHUB", "JWT", "SESSION")
    out = []
    for k in os.environ:
        ku = k.upper()
        if any(p in ku for p in pat) and not any(s in ku for s in skip):
            out.append(k)
    return sorted(out)


def status() -> dict:
    return {
        "env_key_names_present": env_key_names(),
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


async def probe_all(timeout: float = 20.0) -> list[dict]:
    """Call every CONFIGURED provider with a tiny prompt and report the raw
    result. Turns 'the chain failed' into 'this provider, this status, this
    message' without needing server logs."""
    out = []
    for (name, envk, menv, dflt, url, cap) in _PROVIDERS:
        key = _key(envk)
        if not key:
            continue
        model = _key(menv) or dflt
        row = {"provider": name, "model": model}
        try:
            ok, body = await _call_text(name, url, key, model,
                                        "Reply with the single word: ok",
                                        16, timeout, cap)
            row["ok"] = bool(ok)
            row["result"] = (body or "")[:180]
        except Exception as exc:
            row["ok"] = False
            row["result"] = f"exception: {exc}"[:180]
        out.append(row)
    return out


async def list_models(timeout: float = 15.0) -> list[dict]:
    """Ask each configured OpenAI-compatible provider which models it exposes.
    Removes the guesswork when a provider returns model_not_found — the model
    catalogues differ per account and change over time."""
    out = []
    for (name, envk, menv, dflt, url, cap) in _PROVIDERS:
        key = _key(envk)
        if not key or name == "gemini":
            continue
        base = url.rsplit("/chat/completions", 1)[0]
        row = {"provider": name, "endpoint": base + "/models"}
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.get(base + "/models",
                                headers={"Authorization": f"Bearer {key}"})
            if r.status_code != 200:
                row["error"] = f"HTTP {r.status_code}: {r.text[:120]}"
            else:
                data = r.json()
                ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
                row["count"] = len(ids)
                row["models"] = ids[:40]
        except Exception as exc:
            row["error"] = f"exception: {exc}"[:140]
        out.append(row)
    return out
