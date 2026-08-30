"""
stock_rag.py — per-stock AI assistant with a real document RAG.

Pipeline:  EDGAR/transcript text (reused from event_intel)
           → chunk → Voyage embeddings → in-memory vector index per ticker
           → cosine retrieve top-k → Claude answers, grounded in the chunks.

Everything degrades gracefully: if VOYAGE_API_KEY or ANTHROPIC_API_KEY are
missing, ask() returns a clear "not enabled" message instead of erroring.

Env:
  VOYAGE_API_KEY      — embeddings (https://www.voyageai.com)
  VOYAGE_MODEL        — default "voyage-3-lite"
  ANTHROPIC_API_KEY   — answer generation (shared with the rest of the app)
  ANTHROPIC_MODEL     — cheap default for misc raw calls, "claude-haiku-4-5-20251001"
  ASK_MODEL           — interactive assistant, default "claude-sonnet-5"
  CONCALL_MODEL       — deep earnings-call summary, default "claude-sonnet-5"
"""
from __future__ import annotations

import json
import os
import re
import time
import logging

import httpx
import anthropic_shim

logger = logging.getLogger(__name__)

VOYAGE_KEY      = os.environ.get("VOYAGE_API_KEY", "").strip()
VOYAGE_MODEL    = os.environ.get("VOYAGE_MODEL", "voyage-3-lite")
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
# Both the interactive assistant (ASK) and the deep, narrative concall summary run
# on Sonnet 5 for best-in-class prose — a deliberate quality upgrade from Haiku.
# Ask is per-user capped (12/day) and concall is 30-day durably cached, so the
# stronger tier stays inside the monthly AI budget. Both env-overridable;
# ANTHROPIC_MODEL stays the cheap default for any other raw call.
ASK_MODEL = os.environ.get("ASK_MODEL", "claude-sonnet-5")
CONCALL_MODEL = os.environ.get("CONCALL_MODEL", "claude-sonnet-5")

_INDEX: dict[str, dict] = {}          # ticker -> {ts, chunks:[str], vecs: np.ndarray}
_INDEX_TTL = 24 * 3600               # rebuild a ticker's index once a day
_TOP_K = 6
_CHUNK = 1200
_OVERLAP = 200
_MAX_CHUNKS = 160                    # safety cap on embeddings per ticker


def _gen_ok() -> bool:
    """Whether a generation request can actually be answered.

    ANTHROPIC_KEY is NOT the answer to that. Every call below goes through
    anthropic_shim.post(), which ignores the headers it is handed and routes to
    the free provider chain, so this module's four `if not ANTHROPIC_KEY` gates
    were testing something the request path never consults. On this deployment
    the variable was present-but-empty, so /api/ask-status reported
    generation:false and the Concall pane rendered "AI summaries are paused"
    while five healthy free providers sat idle.
    """
    try:
        import anthropic_shim
        return anthropic_shim.generation_available()
    except Exception:
        return False


def is_enabled() -> bool:
    """True when generation can run AND embeddings are configured."""
    return bool(VOYAGE_KEY) and _gen_ok()


def status() -> dict:
    return {
        "enabled": is_enabled(),
        "embeddings": bool(VOYAGE_KEY),
        "generation": _gen_ok(),
    }


def _chunk(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    out, i = [], 0
    while i < len(text) and len(out) < _MAX_CHUNKS:
        out.append(text[i:i + _CHUNK])
        i += _CHUNK - _OVERLAP
    return out


async def _voyage_embed(texts: list[str]):
    """Embed a list of strings via Voyage. Returns np.ndarray or None."""
    if not VOYAGE_KEY or not texts:
        return None
    try:
        import numpy as np
    except Exception:
        logger.warning("stock_rag: numpy not available")
        return None
    vecs: list[list[float]] = []
    try:
        async with httpx.AsyncClient(timeout=40) as c:
            for b in range(0, len(texts), 96):     # Voyage accepts batches
                batch = texts[b:b + 96]
                r = await c.post(
                    "https://api.voyageai.com/v1/embeddings",
                    headers={"Authorization": "Bearer " + VOYAGE_KEY,
                             "content-type": "application/json"},
                    json={"model": VOYAGE_MODEL, "input": batch},
                )
                if r.status_code != 200:
                    logger.warning(f"stock_rag: Voyage HTTP {r.status_code}: {r.text[:200]}")
                    return None
                vecs.extend(d["embedding"] for d in r.json().get("data", []))
    except Exception as exc:
        logger.warning(f"stock_rag: Voyage failed: {exc}")
        return None
    if not vecs:
        return None
    return np.array(vecs, dtype=np.float32)


async def _gather_documents(ticker: str) -> list[tuple[str, str]]:
    """Reuse event_intel's EDGAR + transcript fetchers for real source text."""
    parts: list[tuple[str, str]] = []
    try:
        import event_intel as ei
    except Exception as exc:
        logger.warning(f"stock_rag: event_intel import failed: {exc}")
        return parts
    try:
        ed = await ei._fetch_edgar_recent(ticker)
        if ed and ed.get("text"):
            parts.append((ed.get("source_label", "SEC filing"), ed["text"]))
    except Exception as exc:
        logger.warning(f"stock_rag: EDGAR fetch {ticker}: {exc}")
    try:
        tr = await ei._fetch_av_transcript(ticker)
        if tr and tr.get("text"):
            parts.append(("Earnings call transcript", tr["text"]))
    except Exception as exc:
        logger.warning(f"stock_rag: transcript fetch {ticker}: {exc}")
    return parts


async def _build_index(ticker: str):
    docs = await _gather_documents(ticker)
    chunks: list[str] = []
    for label, text in docs:
        for ch in _chunk(text):
            chunks.append(f"[{label}] {ch}")
    chunks = chunks[:_MAX_CHUNKS]
    if not chunks:
        return None
    vecs = await _voyage_embed(chunks)
    if vecs is None:
        return None
    idx = {"ts": time.time(), "chunks": chunks, "vecs": vecs}
    _INDEX[ticker.upper()] = idx
    return idx


async def _get_index(ticker: str):
    t = ticker.upper()
    cur = _INDEX.get(t)
    if cur and (time.time() - cur["ts"] < _INDEX_TTL):
        return cur
    return await _build_index(t)


async def _retrieve(ticker: str, question: str, k: int = _TOP_K) -> list[str]:
    idx = await _get_index(ticker)
    if not idx:
        return []
    qv = await _voyage_embed([question])
    if qv is None:
        return []
    import numpy as np
    V = idx["vecs"]
    Vn = V / (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
    qn = qv[0] / (np.linalg.norm(qv[0]) + 1e-9)
    sims = Vn @ qn
    top = np.argsort(-sims)[:k]
    return [idx["chunks"][int(i)] for i in top]


async def _claude_answer(ticker, question, context_blocks, profile_data, user_id=None) -> str | None:
    if not _gen_ok():
        return None
    ctx = "\n\n".join(context_blocks[:8])[:60000]
    # Prompt-caching split: everything that's stable for a given ticker (the
    # instructions + our metrics + the retrieved document context) goes in one
    # cached block; only the user's question varies between calls. When a user
    # asks several questions about the SAME ticker within the 5-min cache TTL,
    # the large context (up to ~15K tokens) is served from cache at ~0.1× cost
    # instead of re-sent at full price. The question sits AFTER the breakpoint
    # so it never invalidates the cached prefix.
    prefix = (
        f"You are TickerMover's senior equity-research assistant for the US-listed "
        f"stock {ticker}. Give a best-in-class, investor-grade answer.\n"
        "Ground your answer FIRST in the context below (SEC filings, an earnings "
        "transcript, and our own metrics) and quote concrete figures from it where "
        "relevant. You may ALSO draw on your own broader knowledge of the company, "
        "its industry, competitors, business model, and the macro backdrop to make "
        "the answer genuinely useful and complete — the context is a starting point, "
        "not a hard boundary. When the documents and your general knowledge conflict, "
        "prefer the documents, and don't invent specific figures that aren't either "
        "in the context or well established. It's fine to present reasoning and "
        "industry context as your own analysis. Be concise and specific; use short "
        "bullet points or a small table when it helps.\n"
        "IMPORTANT: You are NOT a financial advisor. Describe and explain; never "
        "tell the user to buy, sell, or hold.\n\n"
        f"=== TICKERMOVER METRICS ===\n{profile_data or '(none provided)'}\n\n"
        f"=== DOCUMENT CONTEXT ===\n{ctx or '(no documents retrieved)'}"
    )
    suffix = f"\n\n=== QUESTION ===\n{question}\n\nAnswer:"
    payload = {"model": ASK_MODEL, "max_tokens": 1500,
               "messages": [{"role": "user", "content": [
                   {"type": "text", "text": prefix,
                    "cache_control": {"type": "ephemeral"}},
                   {"type": "text", "text": suffix},
               ]}]}
    headers = {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=45) as c:
            r = await anthropic_shim.post(
                             json=payload, headers=headers)
            if r.status_code != 200:
                logger.warning(f"stock_rag: Anthropic HTTP {r.status_code}: {r.text[:200]}")
                return None
            data = r.json()
            # Surface prompt-cache effectiveness: cache_read>0 means the per-ticker
            # context prefix was served from cache (~0.1× cost). If this stays 0
            # across repeat questions on one ticker, a silent invalidator is at work.
            u = data.get("usage") or {}
            try:
                import usage_log
                usage_log.record("ask", ASK_MODEL, u, ticker=ticker, user_id=user_id)
            except Exception:
                pass
            logger.info(
                "stock_rag ask %s: in=%s cache_write=%s cache_read=%s",
                ticker, u.get("input_tokens"),
                u.get("cache_creation_input_tokens"), u.get("cache_read_input_tokens"),
            )
            return (data.get("content") or [{}])[0].get("text", "").strip()
    except Exception as exc:
        logger.warning(f"stock_rag: Anthropic {ticker}: {exc}")
        return None


async def _claude_raw(prompt: str, max_tokens: int = 2500, model: str | None = None,
                      feature: str = "concall", ticker: str | None = None,
                      want_json: bool = False) -> str | None:
    """`want_json` tells the shim to use the provider's JSON mode. Set it
    whenever the caller parses the reply with json.loads — do not rely on the
    shim sniffing the prompt, which cannot see an instruction buried above tens
    of thousands of characters of appended source text."""
    if not _gen_ok():
        return None
    model = model or ANTHROPIC_MODEL
    payload = {"model": model, "max_tokens": max_tokens,
               "messages": [{"role": "user", "content": prompt}]}
    headers = {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await anthropic_shim.post(
                             json=payload, headers=headers,
                             force_json=True if want_json else None)
            if r.status_code != 200:
                logger.warning(f"stock_rag: Anthropic HTTP {r.status_code}: {r.text[:200]}")
                return None
            data = r.json()
            # Meter it: concall now runs on the pricier Sonnet tier, so its spend
            # must count against the monthly AI budget/report (was untracked).
            try:
                import usage_log
                usage_log.record(feature, model, data.get("usage"), ticker=ticker)
            except Exception:
                pass
            return (data.get("content") or [{}])[0].get("text", "").strip()
    except Exception as exc:
        logger.warning(f"stock_rag: Anthropic raw: {exc}")
        return None


_CONCALL_PROMPT = (
    "You are an equity analyst writing a DEEP, qualitative earnings-call summary "
    "for {ticker}, grounded strictly in the transcript/filing text below.\n"
    "OBJECTIVE: capture the NARRATIVE management gave on the call — strategy and "
    "positioning, segment-by-segment color, demand commentary, management's framing "
    "and tone, what came out of Q&A, and forward guidance in their own words. This "
    "is the STORY companion to a separate fast 'Earnings Brief' that already lists "
    "the headline reported numbers — so do NOT just restate those figures; explain "
    "the WHY and the context behind them. Cite concrete numbers only where they "
    "support the narrative.\n\n"
    "Generate 5-8 SECTIONS with DYNAMIC headings reflecting what THIS call actually "
    "covered (e.g. 'Strategy and positioning', 'Segment color', 'Demand and pricing', "
    "'Management tone and Q&A', 'Forward guidance', 'Risks management flagged'). End "
    "with a section 'Key investor takeaways' (3-5 bullets).\n\n"
    "Rules: use ONLY facts present in the text; attribute guidance to management; do "
    "NOT give buy/sell advice; omit a section rather than inventing. Be specific, not "
    "generic. Each bullet is self-contained and combines the point WITH its driver.\n\n"
    "Return ONLY a JSON object (no prose, no markdown fences):\n"
    "{{\n"
    '  "event_title": "Short title (e.g. \'Q1 FY26 earnings call\')",\n'
    '  "event_date":  "YYYY-MM-DD or empty if unknown",\n'
    '  "sections": [\n'
    '    {{"heading": "Section heading (4-7 words, sentence case)",\n'
    '      "bullets": ["3-5 bullets, each 15-35 words"]}}\n'
    "  ],\n"
    '  "raw_excerpt": "One verbatim sentence (40-70 words) from management that best '
    'captures the call\'s key message"\n'
    "}}\n\n"
    "=== OUR METRICS ===\n{metrics}\n\n=== SOURCE ({src}) ===\n{text}\n"
)


def _parse_json_block(text: str) -> dict | None:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


_CONCALL_CACHE: dict = {}   # (ticker, quarter) -> result, avoids repeat Claude calls
_CONCALL_NS = "concall"     # stable: the Sonnet 5 upgrade fills in as entries age out (30d TTL) — no forced cache bust


async def concall_summary(ticker: str, profile_data: str = "", quarter: str | None = None,
                          allow_generate: bool = True) -> dict:
    """Deep, narrative earnings-call summary as STRUCTURED sections — the same
    shape /api/event-intel returns, so the premium briefing UI renders both.
    Returns {available, event_title, event_date, sections[], raw_excerpt,
    source, source_url} or {available: False, reason}.

    `quarter` (e.g. '2026Q1') targets a specific call; None = most recent."""
    ck = ((ticker or "").upper(), (quarter or "latest"))
    cached = _CONCALL_CACHE.get(ck)
    if cached is not None:
        return cached
    _kv_key = ck[0] + ":" + ck[1]
    # Durable L2 (survives redeploys) — concall summaries are built from filings
    # and rarely change, so a 30-day durable cache avoids re-paying Claude after
    # every build. Re-warm the in-process cache on a hit.
    try:
        from kv_store import store as _kv
        durable = _kv.get(_CONCALL_NS, _kv_key, max_age_s=30 * 24 * 3600)
        if isinstance(durable, dict):
            _CONCALL_CACHE[ck] = durable
            return durable
    except Exception:
        pass
    # Cost breaker: when over budget the caller passes allow_generate=False, so a
    # cache miss returns a "paused" notice instead of paying for a fresh summary.
    if not allow_generate:
        return {"available": False, "reason": "paused",
                "note": "AI summaries are paused for now — please check back later."}
    if not _gen_ok():
        return {"available": False, "reason": "disabled",
                "note": "The AI summary isn't enabled on this deployment yet."}
    text = src = src_url = None
    src_tag = "sec_edgar"
    av_throttled = False
    try:
        import event_intel as ei
        try:
            tr = await ei._fetch_av_transcript(ticker, quarter)
            # An Alpha Vantage throttle is NOT terminal. It used to return here,
            # which skipped the EDGAR fallback sitting fifteen lines below — a
            # free, unlimited source that answers this same question — so every
            # concall after the daily quota ran out showed "temporarily
            # throttled" even though the filing was one request away. Record it
            # and keep going; it only becomes the answer if EDGAR also has
            # nothing.
            if isinstance(tr, dict) and tr.get("error") == "rate_limited":
                av_throttled = True
                tr = None
            if tr and (tr.get("text") or tr.get("transcript")):
                if tr.get("text"):
                    text = tr["text"]
                else:
                    segs = tr.get("transcript") or []
                    text = "\n".join(
                        ((s.get("speaker", "") + ": ") if s.get("speaker") else "")
                        + (s.get("content", "") or "")
                        for s in segs if isinstance(s, dict) and s.get("content"))
                src = "earnings call transcript"
                src_tag = "alpha_vantage_transcript"
        except Exception:
            pass
        # EDGAR fallback is the *latest* filing — only use it for the default
        # (latest) request, never when a specific quarter was asked for.
        if not text and not quarter:
            ed = await ei._fetch_edgar_recent(ticker)
            if ed and ed.get("text"):
                text, src = ed["text"], ed.get("source_label", "recent SEC filing")
                src_url = ed.get("source_url")
                src_tag = f"sec_edgar:{ed.get('source_label', '')}"
    except Exception as exc:
        logger.warning(f"stock_rag: concall fetch {ticker}: {exc}")
    if not text:
        # Only now is the throttle the real story: no transcript AND no filing.
        # A specific-quarter request has no EDGAR path at all, so it lands here
        # whenever the transcript provider is capped.
        if av_throttled:
            return {"available": False, "reason": "rate_limited"}
        return {"available": False, "reason": "no_coverage"}

    prompt = _CONCALL_PROMPT.format(ticker=ticker, src=src, text=text[:45000],
                                    metrics=profile_data or "(none)")
    ans = await _claude_raw(prompt, max_tokens=2600, model=CONCALL_MODEL, ticker=ticker,
                            want_json=True)
    parsed = _parse_json_block(ans)
    if not parsed or not isinstance(parsed.get("sections"), list):
        # Log WHAT came back. "generation_failed" on its own gave no way to tell
        # an empty reply from prose-instead-of-JSON — which is what it was.
        logger.warning("stock_rag: concall %s unparseable (%s): %s",
                       ticker, "no reply" if not ans else f"{len(ans)} chars",
                       (ans or "")[:180].replace("\n", " "))
        return {"available": False, "reason": "generation_failed"}

    parsed["available"] = True
    parsed["source"]    = src_tag
    if src_url:
        parsed["source_url"] = src_url
    _CONCALL_CACHE[ck] = parsed
    try:
        from kv_store import store as _kv
        _kv.set(_CONCALL_NS, _kv_key, parsed)   # persist so the next deploy reuses it
    except Exception:
        pass
    return parsed


async def ask(ticker: str, question: str, profile_data: str = "", user_id=None) -> dict:
    """Main entry: returns {ok, answer, sources[]}."""
    question = (question or "").strip()
    if not question:
        return {"ok": False, "answer": "Ask something about the stock.", "sources": []}
    # Anthropic is the only hard requirement. Voyage embeddings just add document
    # retrieval (RAG); without them the assistant still answers from our metrics
    # plus Claude's own knowledge rather than being switched off entirely.
    if not _gen_ok():
        return {"ok": False, "sources": [],
                "answer": "The AI assistant is temporarily unavailable. "
                          "It comes back on its own — please try again shortly."}
    blocks = await _retrieve(ticker, question) if VOYAGE_KEY else []
    answer = await _claude_answer(ticker, question, blocks, profile_data, user_id=user_id)
    if not answer:
        return {"ok": False, "sources": [],
                "answer": "The assistant is having trouble responding right now. "
                          "Please try again in a moment."}
    # surface which document types backed the answer
    srcs = []
    for b in blocks:
        if b.startswith("["):
            lbl = b[1:b.find("]")]
            if lbl and lbl not in srcs:
                srcs.append(lbl)
    if not srcs:
        srcs = ["TickerMover metrics + Claude analysis"]
    return {"ok": True, "answer": answer, "sources": srcs[:4]}
