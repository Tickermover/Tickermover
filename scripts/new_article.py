"""
new_article.py  ─  Scaffold a new Zacks-style research article for AlphaHunt.

Usage:
    python scripts/new_article.py

The script prompts you interactively for the headline facts (ticker, title,
thesis bullets, valuation, risks, recommendation) and prints a ready-to-paste
Python dict you drop into the top of `_BLOG_ARTICLES` in `app.py`.

Why a script and not auto-generation?
    Daily articles work best when a human still picks the angle and writes the
    thesis. This script takes the structural boilerplate off your plate so you
    can focus on the analysis itself.

Quick workflow:
    1.  python scripts/new_article.py
    2.  Answer the prompts (most have sensible defaults — just hit Enter).
    3.  Copy the printed block into `_BLOG_ARTICLES` at the top.
    4.  Optionally set "featured": True if it's the daily highlight.
"""
from __future__ import annotations
import datetime
import re
import sys
import textwrap


# ─── Helpers ──────────────────────────────────────────────────────────

def ask(prompt: str, default: str = "") -> str:
    """Prompt the user; return entered value or default if blank."""
    suffix = f"  [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


def ask_list(prompt: str, n: int = 3, label: str = "bullet") -> list[str]:
    """Collect a list of items. Empty input ends collection."""
    print(f"\n{prompt}  (enter a blank line to stop, target ~{n})")
    items: list[str] = []
    while True:
        v = input(f"  {label} {len(items)+1}: ").strip()
        if not v:
            break
        items.append(v)
    return items


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60]


def py_quote(s: str) -> str:
    """Escape a string for safe inclusion in Python source."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ─── Main ─────────────────────────────────────────────────────────────

def main() -> None:
    today = datetime.date.today().isoformat()
    print(f"\n─── New AlphaHunt research article ─── ({today})\n")

    ticker   = ask("Ticker (e.g. NVDA)").upper()
    if not ticker:
        sys.exit("Ticker is required.")
    title    = ask("Headline title")
    summary  = ask("One-paragraph summary (1-2 sentences)")
    category = ask("Category", "AI Infrastructure")

    rating   = ask("Rating", "Strong Buy")
    convict  = ask("Conviction (High/Medium/Low)", "High")
    cur_pr   = ask("Current price (USD)", "0")
    tgt_pr   = ask("Price target (USD)", "0")
    horizon  = ask("Investment horizon", "12-18 months")

    thesis   = ask_list("Investment thesis bullets", 4, "thesis")
    risks    = ask_list("Key risks (bear case)", 3, "risk")
    moat_pts = ask_list("Competitive moat points", 3, "moat")

    val_verdict = ask("Valuation verdict (Cheap/Fair/Full/Premium/Expensive)", "Fair")
    val_summary = ask("Valuation summary (1 sentence)")

    rev_growth  = ask("Recent quarter revenue growth", "+25% YoY")
    guidance    = ask("Latest guidance (1 sentence)", "")

    revisions   = ask("Estimate revisions trend (1 sentence)", "Estimates trending higher.")
    catalysts   = ask("Key upcoming catalysts (comma-separated)", "Next earnings")

    rec_summary = ask("Recommendation summary (1-2 sentences)")

    # Build the dict source
    article_id = ask("Article slug-id", f"{ticker.lower()}-{slug(title)[:40]}-{today[:7]}")

    src = textwrap.dedent(f'''\
      {{
        "id": "{article_id}",
        "ticker": "{ticker}", "date": "{today}",
        "category": "{py_quote(category)}",
        "title": "{py_quote(title)}",
        "summary": "{py_quote(summary)}",
        "featured": False,    # set to True if this is today's featured highlight
        "report": {{
          "rating": "{py_quote(rating)}", "conviction": "{py_quote(convict)}",
          "price_target": {tgt_pr or 0}, "current_price": {cur_pr or 0}, "horizon": "{py_quote(horizon)}",
          "thesis": [
    ''')
    for t in thesis:
        src += f'        "{py_quote(t)}",\n'
    src += textwrap.dedent(f'''\
          ],
          "earnings": {{
            "quarter": "Most recent quarter",
            "revenue_growth": "{py_quote(rev_growth)}",
            "guidance": "{py_quote(guidance)}",
          }},
          "valuation": {{
            "verdict": "{py_quote(val_verdict)}",
            "summary": "{py_quote(val_summary)}",
          }},
          "moat": {{
            "headline": "Competitive position",
            "points": [
    ''')
    for m in moat_pts:
        src += f'          "{py_quote(m)}",\n'
    src += textwrap.dedent('''\
            ],
          },
          "risks": [
    ''')
    for r in risks:
        src += f'        "{py_quote(r)}",\n'
    src += textwrap.dedent(f'''\
          ],
          "forecast": {{
            "estimate_revisions": "{py_quote(revisions)}",
            "key_catalysts":      "{py_quote(catalysts)}",
          }},
          "recommendation": {{
            "verdict": "{py_quote(rating)}",
            "summary": "{py_quote(rec_summary)}",
          }},
        }},
        # Optional: long-form HTML appendix shown below the structured report.
        "content": "<p>Add a deeper-dive narrative here (HTML allowed).</p>",
      }},
    ''')

    print("\n─── Copy the block below into the top of `_BLOG_ARTICLES` in app.py ───\n")
    print(src)
    print("─── Done. Don't forget to set `\"featured\": True` if this is today's highlight. ───\n")


if __name__ == "__main__":
    main()
