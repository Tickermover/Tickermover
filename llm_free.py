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

import time

import httpx

logger = logging.getLogger(__name__)

LAST_ERRORS: list[dict] = []
# Providers that just told us they cannot serve us — skip them for a while
# rather than paying the round-trip on every request. 429 = quota (retry
# soon), 402/410 = billing required / retired (retry rarely).
_COOLDOWN: dict[str, float] = {}


def _cool(name: str, detail: str) -> None:
    d = (detail or "")
    secs = 0
    if "429" in d or "quota" in d.lower() or "rate limit" in d.lower():
        secs = 900          # 15 min — free quotas usually reset hourly/daily
    elif "413" in d:
        secs = 300          # payload/TPM ceiling — try again shortly
    elif "402" in d or "410" in d:
        secs = 21600        # 6 h — needs billing, or the service is retiring
    if secs:
        _COOLDOWN[name] = time.time() + secs


def _cooling(name: str) -> bool:
    return _COOLDOWN.get(name, 0) > time.time()
_MAX_ERRORS = 8


def _note(provider: str, detail: str) -> None:
    _cool(provider, detail)
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


def _model_for(name: str, menv: str, default: str) -> str:
    """Configured model override, ignored when it is obviously malformed for
    that provider — a bad override otherwise silently disables the provider."""
    m = (_key(menv) or "").strip()
    if not m:
        return default
    if name == "nvidia" and "/" not in m:
        logger.warning("llm_free: ignoring NVIDIA_MODEL=%r (needs 'org/model'), using %s", m, default)
        return default
    return m


def _url_for(name: str, default: str) -> str:
    """Endpoint override from `<NAME>_URL`.

    Providers move and retire endpoints on their own schedule — NVIDIA is
    currently answering a bare "404 page not found", GitHub Models is mid
    retirement, and OpenRouter has already renamed a slug under us. Each of
    those needed a code change and a deploy to correct. With this, a moved
    endpoint is a dashboard edit.

    It also lets account-scoped providers work at all: Cloudflare Workers AI
    embeds the account id in its path, so its URL cannot be a constant."""
    return (_key(f"{name.upper()}_URL") or "").strip() or default


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
    # Default was gemini-2.0-flash, which Google shut down on 2026-06-01, so an
    # install without GEMINI_MODEL set had a dead primary AND dead fallbacks.
    ("gemini", "GEMINI_API_KEY", "GEMINI_MODEL", "gemini-3.6-flash",
     "https://generativelanguage.googleapis.com/v1beta/models", 700000),
    # deepseek-v4-pro hit END OF LIFE on 2026-08-07 and now returns HTTP 410
    # "Gone" for every request. Set NVIDIA_MODEL in the environment to override
    # without a deploy when this one is retired in turn — the 410 body names the
    # retirement date, which is what made this diagnosable at all.
    ("nvidia", "NVIDIA_API_KEY", "NVIDIA_MODEL", "deepseek-ai/deepseek-v3.1",
     "https://integrate.api.nvidia.com/v1/chat/completions", 45000),
    ("groq", "GROQ_API_KEY", "GROQ_MODEL", "openai/gpt-oss-120b",
     "https://api.groq.com/openai/v1/chat/completions", 11000),
    ("cerebras", "CEREBRAS_API_KEY", "CEREBRAS_MODEL", "gpt-oss-120b",
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
    # The ":free" suffix was retired — openrouter now 404s it with
    # "This model is unavailable for free ... use this slug instead:
    # meta-llama/llama-3.3-70b-instruct", so the backstop was dead.
    ("openrouter", "OPENROUTER_API_KEY", "OPENROUTER_MODEL",
     "meta-llama/llama-3.3-70b-instruct",
     "https://openrouter.ai/api/v1/chat/completions", 24000),

    # ── Added 2026-08-13, after a day when the whole chain was down at once ──
    # Both are OpenAI-compatible, so they need no new call path — a tuple and a
    # key is the entire integration. Both are INERT until their key is set, so
    # adding them cannot break the existing chain.
    #
    # SambaNova: permanent free tier (no card), reported ~20 req/min and 200k
    # tokens/day per model. Small daily budget, but it is a genuinely separate
    # quota pool from Groq and Cerebras, which is the point — the failure we
    # keep hitting is every provider rate-limiting at once.
    ("sambanova", "SAMBANOVA_API_KEY", "SAMBANOVA_MODEL",
     "Meta-Llama-3.3-70B-Instruct",
     "https://api.sambanova.ai/v1/chat/completions", 30000),
    # Cloudflare Workers AI: ~10k free "neurons"/day. Its path embeds the
    # account id, so the URL MUST come from CLOUDFLARE_URL — set it to
    # https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/ai/v1/chat/completions
    # The default below is deliberately unusable so a missing account id fails
    # loudly at the first call rather than looking configured.
    ("cloudflare", "CLOUDFLARE_API_KEY", "CLOUDFLARE_MODEL",
     "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
     "https://api.cloudflare.com/client/v4/accounts/SET_CLOUDFLARE_URL/ai/v1/chat/completions",
     20000),
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
             "model": _model_for(n, menv, dflt), "max_input_chars": cap}
            for (n, envk, menv, dflt, _u, cap) in _PROVIDERS
        ],
        "order": available(),
        "cooling_off": {k: int(v - time.time()) for k, v in _COOLDOWN.items()
                        if v > time.time()} or None,
        "recent_errors": LAST_ERRORS[:5] or None,
    }


def strip_fence(text: str) -> str:
    """Remove a wrapping markdown code fence.

    The free providers (Llama, DeepSeek, Gemini) routinely answer with
    ```markdown ... ``` even when asked for prose; Anthropic did not, so this
    only started leaking into user-visible copy after the migration.
    """
    t = (text or "").strip()
    if not t.startswith("```"):
        return t
    nl = t.find(chr(10))
    if nl == -1:
        return t.strip("`").strip()
    first = t[3:nl].strip().lower()
    # only treat it as a fence when the info-string is a language tag
    if first and not first.isalpha():
        return t
    t = t[nl + 1:]
    if t.rstrip().endswith("```"):
        t = t.rstrip()[:-3]
    return t.strip()


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


# TWO OF THESE THREE WERE DEAD. gemini-2.0-flash and gemini-2.0-flash-lite were
# shut down by Google on 2026-06-01, so every fallback attempt after the primary
# was a guaranteed HTTP 404 — the chain looked like it had depth and had none.
# That is why /api/event-intel-status kept reporting
# "models/gemini-2.0-flash-lite is no longer available": it is the LAST entry,
# so its error is the one that survives, masking whatever failed first.
#
# Now the live Gemini 3 tiers first, then 2.5 as the proven backstop.
# NOTE 2.5-flash, 2.5-flash-lite and 2.5-pro all shut down on 2026-10-16 —
# after that this list must be Gemini 3 only. Override without a deploy by
# setting GEMINI_MODEL.
_GEMINI_FALLBACKS = [
    "gemini-3.6-flash",        # current balanced tier
    "gemini-3.5-flash-lite",   # fastest / cheapest, good for summarisation
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",        # dies 2026-10-16
    "gemini-2.5-flash-lite",   # dies 2026-10-16
]


async def _call_gemini(base: str, key: str, model: str, prompt: str,
                       max_tokens: int, timeout: float,
                       json_mode: bool = True) -> tuple[bool, str]:
    """Try the configured Gemini model, then cheaper tiers on quota errors.
    gemini-2.5-pro has a very small free allowance, so a single 429 there
    should not cost us the provider with the biggest context window."""
    tries = [model] + [m for m in _GEMINI_FALLBACKS if m != model]
    last = ""
    for i, m in enumerate(tries):
        ok, body = await _call_gemini_once(base, key, m, prompt, max_tokens, timeout, json_mode)
        if ok:
            if i:
                logger.info("llm_free: gemini fell back to %s", m)
            return True, body
        last = body
        if "429" not in body and "404" not in body:
            break
    return False, last


async def _call_gemini_once(base: str, key: str, model: str, prompt: str,
                            max_tokens: int, timeout: float,
                            json_mode: bool = True) -> tuple[bool, str]:
    url = f"{base}/{model}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": dict({"temperature": 0.1, "maxOutputTokens": max_tokens},
                                 **({"responseMimeType": "application/json"} if json_mode else {})),
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


def _fit_first(usable: list, prompt: str) -> list:
    """Put providers whose context actually FITS the prompt first.

    Every call site sends `prompt[:cap]`, so a provider with a small window
    does not fail on a long prompt — it silently answers a truncated one, and
    because that is an HTTP 200 the chain stops there and never reaches a
    provider that could have read the whole thing. That is not hypothetical:
    the weekly generator's system prompt alone is ~9.3k chars against groq's
    11k cap, so groq answered on a prompt with essentially none of the data
    attached and mistral (100k) was never tried.

    Order is otherwise preserved, and providers that would truncate stay in the
    list as a last resort — a degraded answer still beats no answer."""
    n = len(prompt or "")
    fits = [p for p in usable if p[5] >= n]
    short = [p for p in usable if p[5] < n]
    if short and fits:
        logger.info("llm_free: prompt %d chars — deferring %s (window too small)",
                    n, ",".join(p[0] for p in short))
    # Longest window first among those that would truncate, so the fallback
    # loses as little of the prompt as possible.
    return fits + sorted(short, key=lambda p: -p[5])


async def chat_json(prompt: str, *, max_tokens: int = 1500, timeout: float = 75.0,
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
    usable = [p for p in order if _key(p[1]) and not _cooling(p[0])]
    if not usable:                      # everything cooling — try anyway
        usable = [p for p in order if _key(p[1])]
    usable = _fit_first(usable, prompt)
    for (name, envk, menv, dflt, url, cap) in usable:
        key = _key(envk)
        if not key:
            continue
        tried += 1
        model = _model_for(name, menv, dflt)
        url = _url_for(name, url)     # endpoint may be overridden per deploy
        try:
            if name == "gemini":
                ok, body = await _call_gemini(url, key, model, prompt[:cap], max_tokens, timeout)
            else:
                ok, body = await _call_openai_compatible(url, key, model, prompt[:cap],
                                                         max_tokens, timeout)
        except Exception as exc:
            _note(name, f"exception: {type(exc).__name__}: {exc}".rstrip(": "))
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
    url = _url_for(name, url)         # endpoint may be overridden per deploy
    """Same providers, but plain-text output (no JSON response_format)."""
    if name == "gemini":
        return await _call_gemini(url, key, model, prompt[:cap], max_tokens, timeout,
                                  json_mode=False)
    if False:
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


async def chat_text(prompt: str, *, max_tokens: int = 1500, timeout: float = 75.0,
                    prefer: str | None = None) -> str | None:
    """Plain-text completion through the free chain. Returns None when every
    configured provider fails (see LAST_ERRORS)."""
    order = list(_PROVIDERS)
    if prefer:
        order.sort(key=lambda p: 0 if p[0] == prefer else 1)
    tried = 0
    usable = [p for p in order if _key(p[1]) and not _cooling(p[0])]
    if not usable:
        usable = [p for p in order if _key(p[1])]
    usable = _fit_first(usable, prompt)
    for (name, envk, menv, dflt, url, cap) in usable:
        key = _key(envk)
        if not key:
            continue
        tried += 1
        model = _model_for(name, menv, dflt)
        try:
            ok, body = await _call_text(name, url, key, model, prompt, max_tokens, timeout, cap)
        except Exception as exc:
            _note(name, f"text exception: {type(exc).__name__}: {exc}".rstrip(": "))
            continue
        if not ok:
            _note(name, body)
            continue
        body = strip_fence(body)
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
        model = _model_for(name, menv, dflt)
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


_CHAT_HINT = ("instruct", "chat", "gpt", "llama", "gemma", "qwen", "mistral",
              "deepseek", "glm", "nemotron", "phi", "compound")
_SKIP_HINT = ("whisper", "embed", "bge", "guard", "tts", "orpheus", "rerank",
              "vision-ocr", "diffusion", "image", "safeguard")


async def discover_working_models(max_try: int = 6, timeout: float = 20.0) -> list[dict]:
    """For each configured provider, find a catalogue model that actually
    answers. Free tiers differ per account (a model can exist but be 402
    payment-required), so the only reliable way is to try them."""
    out = []
    cats = {c["provider"]: c for c in await list_models(timeout=timeout)}
    for (name, envk, menv, dflt, url, cap) in _PROVIDERS:
        key = _key(envk)
        if not key:
            continue
        row = {"provider": name, "configured": _model_for(name, menv, dflt)}
        cands = [row["configured"]]
        for m in (cats.get(name, {}).get("models") or []):
            ml = (m or "").lower()
            if any(b in ml for b in _SKIP_HINT):
                continue
            if any(h in ml for h in _CHAT_HINT) and m not in cands:
                cands.append(m)
        row["tried"] = []
        for m in cands[:max_try]:
            try:
                ok, body = await _call_text(name, url, key, m, "Reply: ok", 16, timeout, cap)
            except Exception as exc:
                ok, body = False, f"exception: {exc}"
            row["tried"].append({"model": m, "ok": bool(ok), "msg": (body or "")[:90]})
            if ok:
                row["working"] = m
                break
        out.append(row)
    return out


async def probe(name: str) -> dict:
    """Make one tiny REAL call to a single provider and report what came back.

    Exists because "no error logged" and "healthy" are indistinguishable from
    the outside: a provider late in the order is only called when everything
    ahead of it fails, so a newly-added key can sit unexercised for days and
    then turn out to be wrong at the worst moment. This calls it directly,
    bypassing order, cooldowns and the `prefer` hint.

    Cheap on purpose — 32 tokens — so it is safe to run against every provider
    on a schedule. Returns the HTTP status and a truncated body; never the key.
    """
    row = next((p for p in _PROVIDERS if p[0] == name), None)
    if row is None:
        return {"provider": name, "ok": False, "reason": "unknown provider",
                "known": [p[0] for p in _PROVIDERS]}
    _n, envk, menv, dflt, url, _cap = row
    key = _key(envk)
    if not key:
        return {"provider": name, "ok": False, "reason": f"{envk} not set"}
    model = _model_for(name, menv, dflt)
    url = _url_for(name, url)
    prompt = 'Reply with exactly this JSON and nothing else: {"ok": true}'
    started = time.time()
    try:
        if name == "gemini":
            ok, body = await _call_gemini(url, key, model, prompt, 32, 20.0)
        else:
            ok, body = await _call_openai_compatible(url, key, model, prompt, 32, 20.0)
    except Exception as exc:
        return {"provider": name, "ok": False, "model": model,
                "url": url, "exception": f"{type(exc).__name__}: {exc}"[:200],
                "ms": int((time.time() - started) * 1000)}
    return {"provider": name, "ok": bool(ok), "model": model, "url": url,
            "body": str(body)[:200], "ms": int((time.time() - started) * 1000)}


async def probe_all() -> list[dict]:
    """Probe every provider that has a key. Ordered worst-first so a failure is
    the first thing you read."""
    import asyncio
    names = [p[0] for p in _PROVIDERS if _key(p[1])]
    res = await asyncio.gather(*[probe(n) for n in names], return_exceptions=True)
    out = []
    for n, r in zip(names, res):
        out.append(r if isinstance(r, dict)
                   else {"provider": n, "ok": False, "exception": str(r)[:200]})
    out.sort(key=lambda d: (d.get("ok") is True, d.get("provider")))
    return out
