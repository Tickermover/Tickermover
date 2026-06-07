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
  ANTHROPIC_MODEL     — default "claude-haiku-4-5-20251001"
"""
from __future__ import annotations

import os
import re
import time
import logging

import httpx

logger = logging.getLogger(__name__)

VOYAGE_KEY      = os.environ.get("VOYAGE_API_KEY", "").strip()
VOYAGE_MODEL    = os.environ.get("VOYAGE_MODEL", "voyage-3-lite")
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

_INDEX: dict[str, dict] = {}          # ticker -> {ts, chunks:[str], vecs: np.ndarray}
_INDEX_TTL = 24 * 3600               # rebuild a ticker's index once a day
_TOP_K = 6
_CHUNK = 1200
_OVERLAP = 200
_MAX_CHUNKS = 160                    # safety cap on embeddings per ticker


def is_enabled() -> bool:
    """True only when both providers are configured."""
    return bool(VOYAGE_KEY and ANTHROPIC_KEY)


def status() -> dict:
    return {
        "enabled": is_enabled(),
        "embeddings": bool(VOYAGE_KEY),
        "generation": bool(ANTHROPIC_KEY),
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


async def _claude_answer(ticker, question, context_blocks, profile_data) -> str | None:
    if not ANTHROPIC_KEY:
        return None
    ctx = "\n\n".join(context_blocks[:8])[:60000]
    prompt = (
        f"You are AlphaHunt's research assistant for the US-listed stock {ticker}. "
        "Answer the user's question using ONLY the context below (SEC filings, an "
        "earnings transcript, and our own metrics). Be concise and specific; use "
        "short bullet points or a small table when it helps. If the context does "
        "not cover the question, say so plainly rather than guessing.\n"
        "IMPORTANT: You are NOT a financial advisor. Describe and explain; never "
        "tell the user to buy, sell, or hold.\n\n"
        f"=== ALPHAHUNT METRICS ===\n{profile_data or '(none provided)'}\n\n"
        f"=== DOCUMENT CONTEXT ===\n{ctx or '(no documents retrieved)'}\n\n"
        f"=== QUESTION ===\n{question}\n\nAnswer:"
    )
    payload = {"model": ANTHROPIC_MODEL, "max_tokens": 1200,
               "messages": [{"role": "user", "content": prompt}]}
    headers = {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=45) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                             json=payload, headers=headers)
            if r.status_code != 200:
                logger.warning(f"stock_rag: Anthropic HTTP {r.status_code}: {r.text[:200]}")
                return None
            return (r.json().get("content") or [{}])[0].get("text", "").strip()
    except Exception as exc:
        logger.warning(f"stock_rag: Anthropic {ticker}: {exc}")
        return None


async def ask(ticker: str, question: str, profile_data: str = "") -> dict:
    """Main entry: returns {ok, answer, sources[]}."""
    question = (question or "").strip()
    if not question:
        return {"ok": False, "answer": "Ask something about the stock.", "sources": []}
    if not is_enabled():
        return {"ok": False, "sources": [],
                "answer": "The AI assistant isn't enabled yet. Set VOYAGE_API_KEY "
                          "and ANTHROPIC_API_KEY to turn it on."}
    blocks = await _retrieve(ticker, question)
    answer = await _claude_answer(ticker, question, blocks, profile_data)
    if not answer:
        return {"ok": False, "sources": [],
                "answer": "I couldn't pull enough source documents to answer that "
                          "reliably right now. Try a different question or check back "
                          "after the next filing."}
    # surface which document types backed the answer
    srcs = []
    for b in blocks:
        if b.startswith("["):
            lbl = b[1:b.find("]")]
            if lbl and lbl not in srcs:
                srcs.append(lbl)
    return {"ok": True, "answer": answer, "sources": srcs[:4]}
