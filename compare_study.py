"""
compare_study — a long-form, primary-source head-to-head between two companies.

WHY IT IS BUILT THIS WAY (read before changing the prompt)

The obvious way to write a "NVDA vs AMD" study is to ask a model for one. That
produces exactly the document a reader cannot check: specific product SKUs,
memory capacities, market-share percentages and analyst forecasts, all recalled
from training data, none of it verifiable and some of it wrong. Market share
and roadmap specifics are among the things language models most reliably
invent, and publishing an invented market-share figure on a financial site is a
worse failure than any wording problem.

This project's free provider chain also has NO server-side web search --
anthropic_shim logs "free providers have no server-side tool support, answering
without them" and answers from model knowledge regardless. So a prompt that
merely ASKS for researched facts gets unresearched ones back, silently.

So the study is grounded in what each company says about ITSELF in its own SEC
filings (fetched via event_intel, the same source the RAG uses), plus the
figures we compute ourselves. The model's job is to organise and contrast that
material, never to supply facts. The rule is absolute and enforced in three
places: the system prompt, a per-section instruction, and a post-check that
drops any section containing a figure absent from the source text.

The result is narrower than a research-house note and considerably more
defensible: every claim traces to a filing or to arithmetic we did.

COMPLIANCE
Descriptive and comparative only. No forecast, no price target, no conclusion
about which company to own, no "outlook". The banned-language sweep from
sector_narrative is reused and extended with forward-looking verbs, because the
template this was modelled on had a "Future Outlook" section and that is the
one part of it we deliberately do not reproduce.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

import anthropic_shim
import usage_log
from kv_store import store as _kv

logger = logging.getLogger(__name__)

_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
_MODEL = (os.environ.get("ANTHROPIC_STUDY_MODEL") or "claude-sonnet-5").strip()
_NS = "compare_study_v1"

# How much filing text to hand the model per company. The free chain routes
# long prompts to the big-context provider (see anthropic_shim._flatten), so
# this is generous but still bounded — an unbounded prompt is how a "free" API
# turns into a rate-limit outage.
_PER_DOC = 24000

SECTIONS = [
    ("positioning", "How each company describes its own business"),
    ("products",    "Product lines and technology each one discloses"),
    ("competition", "What each says about its competitive position"),
    ("strategy",    "Stated priorities and plans"),
    ("risks",       "Risk factors each company discloses"),
]

_SYSTEM = (
    "You write a factual, comparative research note on TWO listed companies for "
    "TickerMover, a UK research site.\n"
    "You are given (a) figures we computed ourselves and (b) extracts from each "
    "company's own SEC filings. These are your ONLY sources.\n"
    "ABSOLUTE RULES:\n"
    "- Use ONLY facts present in the supplied material. If a fact is not there, "
    "omit it. Never supply a figure, market share, product name, customer, date "
    "or capacity from your own knowledge — you will be wrong and it will be "
    "published.\n"
    "- Where a company makes a claim about itself, attribute it: 'AMD states', "
    "'per NVIDIA's filing'. Do not present a company's self-description as "
    "established fact.\n"
    "- Compare and contrast. Say where the two differ and where the filings are "
    "silent. 'Neither filing addresses this' is a valid and useful sentence.\n"
    "- NEVER conclude which company is the better investment, which to own, or "
    "which will perform better. No winner, no ranking, no recommendation.\n"
    "- NEVER forecast. No outlook, no projection, no price target, no 'expected "
    "to', no 'poised to', no timeframe for any result. Describe what IS "
    "disclosed, not what will happen.\n"
    "- Generic research for a UK audience. Not advice, not a personal "
    "recommendation. Never 'you should'.\n"
    "- Plain English. No hype, no markdown headers, no emoji.\n"
    "Return ONLY a JSON object with one key per requested section id, each value "
    "a string of 2-4 sentences."
)

_BANNED = re.compile(
    r"\b(you should|should buy|should sell|we recommend|better (?:buy|investment|choice)|"
    r"the winner|outperform(?:s|ed)? going forward|will (?:rise|fall|soar|surge|beat|win)|"
    r"poised to|set to (?:soar|surge|beat)|expected to (?:reach|hit|grow to)|"
    r"price target|guaranteed|our forecast|we forecast|we expect)\b",
    re.I,
)


def available() -> bool:
    return bool(_KEY)


async def _filing_text(ticker: str) -> tuple[str, str]:
    """(label, text) of the most recent filing we can fetch. Never raises."""
    try:
        import event_intel as ei
        ed = await ei._fetch_edgar_recent(ticker)
        if ed and ed.get("text"):
            return (ed.get("source_label") or "SEC filing", ed["text"][:_PER_DOC])
    except Exception as exc:
        logger.warning("compare_study: EDGAR %s: %s", ticker, exc)
    return ("", "")


def _bucket(cmp_data: dict) -> str:
    """Coarse signature — regenerate only on a real change, not every tick."""
    def r(v, step):
        if v is None:
            return "na"
        try:
            return str(int(round(float(v) / step) * step))
        except (TypeError, ValueError):
            return "na"
    rows = {x["key"]: x for x in (cmp_data.get("rows") or [])}
    return "-".join([
        r((rows.get("alpha") or {}).get("a_raw"), 5),
        r((rows.get("alpha") or {}).get("b_raw"), 5),
        str(cmp_data.get("differing") or 0),
    ])


def _figures_in(text: str) -> set:
    """Numbers a sentence asserts. Used to catch invented statistics."""
    return set(re.findall(r"\d[\d,]*\.?\d*\s?(?:%|percent|billion|million|bn|B\b|M\b|GB|x\b)", text or ""))


def _metrics_block(cmp_data: dict) -> str:
    a = cmp_data["a"]["ticker"]
    b = cmp_data["b"]["ticker"]
    lines = [f"Figures we computed (these ARE verified, you may cite them):",
             f"Comparing {a} ({cmp_data['a']['name']}) and {b} ({cmp_data['b']['name']}).",
             f"{a} sub-sector: {cmp_data['a'].get('sector') or 'n/a'}; "
             f"{b} sub-sector: {cmp_data['b'].get('sector') or 'n/a'}.",
             ("They are in the SAME sub-sector." if cmp_data.get("same_sector")
              else "They are in DIFFERENT sub-sectors, so valuation and margin norms differ.")]
    for r in cmp_data.get("rows") or []:
        lines.append(f"- {r['label']}: {a} {r['a']}, {b} {r['b']}"
                     + (f" (higher: {r['higher']})" if r.get("higher") else " (level)"))
    return "\n".join(lines)


async def _call(cmp_data: dict, docs: dict) -> Optional[dict]:
    a = cmp_data["a"]["ticker"]
    b = cmp_data["b"]["ticker"]
    want = "\n".join(f'  "{k}": "{desc}"' for k, desc in SECTIONS)
    src = []
    for tk in (a, b):
        label, text = docs.get(tk, ("", ""))
        if text:
            src.append(f"=== {tk} — {label} (primary source) ===\n{text}")
        else:
            src.append(f"=== {tk} — no filing text available ===\n"
                       f"(Say so where a section would need it.)")
    prompt = (
        _metrics_block(cmp_data) + "\n\n" + "\n\n".join(src)
        + "\n\nReturn a JSON object with exactly these keys:\n{\n" + want + "\n}\n"
        + "Every factual claim must come from the material above. Where the "
          "filings do not cover a section, say that plainly rather than filling "
          "the gap from memory."
    )
    r = await anthropic_shim.post(
        headers={"x-api-key": _KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": _MODEL, "max_tokens": 2600, "system": _SYSTEM,
              "messages": [{"role": "user", "content": prompt}]},
        force_json=True,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"study {a}/{b}: {r.status_code} {str(r.text)[:160]}")
    data = r.json()
    usage_log.record("compare_study", _MODEL, data.get("usage") or {})
    txt = "".join(blk.get("text", "") for blk in data.get("content", [])
                  if blk.get("type") == "text").strip()
    try:
        obj = json.loads(re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M))
    except Exception:
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            return None
        obj = json.loads(m.group(0))

    # Post-checks. Any section that breaks a rule is DROPPED, not rewritten —
    # a second call to repair a compliance or accuracy miss is the wrong trade,
    # and a study missing one section is fine while a wrong one is not.
    source_blob = " ".join(t for _, t in docs.values()) + " " + _metrics_block(cmp_data)
    source_figs = _figures_in(source_blob)
    out = {}
    for k, _ in SECTIONS:
        v = obj.get(k)
        if not isinstance(v, str) or len(v.strip()) < 30:
            continue
        v = re.sub(r"\s+", " ", v).strip()
        if _BANNED.search(v):
            logger.warning("compare_study %s/%s: section %s rejected (language)", a, b, k)
            continue
        invented = _figures_in(v) - source_figs
        if invented:
            logger.warning("compare_study %s/%s: section %s rejected (figures not in "
                           "source: %s)", a, b, k, sorted(invented)[:4])
            continue
        out[k] = v
    return out or None


async def generate(cmp_data: dict, force: bool = False) -> dict:
    """Return {pair, sections, sources, status}. Durably cached per pair+bucket."""
    a, b = cmp_data["a"]["ticker"], cmp_data["b"]["ticker"]
    pair = f"{a}-vs-{b}"
    key = f"{pair}:{_bucket(cmp_data)}"
    if not force:
        hit = _kv.get(_NS, key)
        if hit and hit.get("sections"):
            return {"pair": pair, "sections": hit["sections"],
                    "sources": hit.get("sources") or [], "status": "ready"}
    if not available():
        return {"pair": pair, "sections": None, "status": "unavailable"}

    docs = {}
    for tk in (a, b):
        docs[tk] = await _filing_text(tk)
    if not any(t for _, t in docs.values()):
        # No primary source at all. Refuse rather than let the model write from
        # memory — an ungrounded study is the failure mode this module exists
        # to prevent.
        return {"pair": pair, "sections": None, "status": "no_source"}

    try:
        sections = await _call(cmp_data, docs)
    except Exception as exc:
        logger.error("compare_study %s failed: %s", pair, exc)
        return {"pair": pair, "sections": None, "status": "error"}
    if not sections:
        return {"pair": pair, "sections": None, "status": "unavailable"}
    sources = [f"{tk}: {docs[tk][0]}" for tk in (a, b) if docs[tk][0]]
    _kv.set(_NS, key, {"sections": sections, "sources": sources, "model": _MODEL})
    return {"pair": pair, "sections": sections, "sources": sources, "status": "ready"}


def cached(cmp_data: dict) -> Optional[dict]:
    """Synchronous cache peek for server-rendered pages — never generates."""
    try:
        a, b = cmp_data["a"]["ticker"], cmp_data["b"]["ticker"]
        return _kv.get(_NS, f"{a}-vs-{b}:{_bucket(cmp_data)}") or None
    except Exception:
        return None
