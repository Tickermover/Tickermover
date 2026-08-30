#!/usr/bin/env python3
"""
apply_keys.py — load API keys into /etc/tickermover.env on the Oracle box.

    python deploy/oracle/apply_keys.py keys.docx
    python deploy/oracle/apply_keys.py keys.txt --dry-run

Reads KEY=VALUE lines from a .docx or plain-text file and merges them into
the server's env file over SSH. Values travel from your machine to the
server and are never printed: the report shows key names and value LENGTHS
only, so a transcript, a screen-share or a shoulder cannot leak them.

Accepts messy input — blank lines, prose between entries, "KEY: value",
smart quotes from Word, and wrapping quotes are all handled.
"""
from __future__ import annotations
import argparse, pathlib, re, subprocess, sys, zipfile

HOST = "ubuntu@79.72.67.33"
KEYFILE = str(pathlib.Path.home() / ".ssh" / "tickermover.key")
ENVPATH = "/etc/tickermover.env"

# Only names the app actually reads are accepted; a typo is reported, not
# silently written, because a misspelled var looks exactly like a missing one.
ALLOWED = {
    "GEMINI_API_KEY","MISTRAL_API_KEY","TOGETHER_API_KEY","NVIDIA_API_KEY",
    "SAMBANOVA_API_KEY","OPENROUTER_API_KEY","CEREBRAS_API_KEY","GROQ_API_KEY",
    "GITHUB_MODELS_KEY","SUPABASE_URL","SUPABASE_ANON_KEY","SUPABASE_JWT_SECRET",
    "SUPABASE_SERVICE_KEY","ALPACA_KEY_ID","ALPACA_SECRET_KEY","FINNHUB_KEY",
    "ALPHA_VANTAGE_KEY","FMP_API_KEY","SEC_API_KEY","APEWISDOM_KEY",
    "SERPER_API_KEY","TAVILY_API_KEY","BRAVE_API_KEY","RESEND_API_KEY",
    "UNSPLASH_ACCESS_KEY","PEXELS_API_KEY","VOYAGE_API_KEY",
}

def read_text(p: pathlib.Path) -> str:
    if p.suffix.lower() == ".docx":
        with zipfile.ZipFile(p) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        xml = re.sub(r"</w:p>", "\n", xml)          # paragraphs -> newlines
        return re.sub(r"<[^>]+>", "", xml)          # strip all other tags
    return p.read_text(encoding="utf-8", errors="replace")

def parse(text: str) -> tuple[dict, list]:
    found, unknown = {}, []
    # Word turns straight quotes into curly ones; strip both.
    trans = str.maketrans({"\u201c":'"',"\u201d":'"',"\u2018":"'","\u2019":"'"})
    for raw in text.translate(trans).splitlines():
        m = re.match(r"\s*([A-Z][A-Z0-9_]{2,})\s*[=:]\s*(.*?)\s*$", raw)
        if not m:
            continue
        name, val = m.group(1), m.group(2).strip().strip('"').strip("'").strip()
        if not val or val.startswith("<"):        # placeholder, not a value
            continue
        if name in ALLOWED:
            found[name] = val
        else:
            unknown.append(name)
    return found, unknown

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path"); ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    p = pathlib.Path(a.path)
    if not p.exists():
        print(f"no such file: {p}"); return 1

    keys, unknown = parse(read_text(p))
    if not keys:
        print("No KEY=VALUE pairs recognised. Expected lines like:")
        print("  GEMINI_API_KEY=AIza...")
        return 1

    print(f"Parsed {len(keys)} key(s) from {p.name}:")
    for k in sorted(keys):
        v = keys[k]
        flag = ""
        if v != v.strip():            flag = "  <- WHITESPACE, will be trimmed"
        if len(v) < 8:                flag = "  <- SUSPICIOUSLY SHORT"
        print(f"  {k:<24} {len(v):>4} chars{flag}")
    if unknown:
        print("\nIgnored (not read by the app — check for typos):")
        for u in sorted(set(unknown)): print(f"  {u}")
    missing = sorted(ALLOWED - set(keys))
    if missing:
        print(f"\nNot supplied ({len(missing)}) — left blank, features degrade:")
        print("  " + ", ".join(missing))

    if a.dry_run:
        print("\n--dry-run: nothing sent.")
        return 0

    # Build a sed program that rewrites only the lines we have values for.
    # Sent over stdin so no value ever appears in a command line (and so
    # never in `ps`, shell history, or this script's own output).
    def esc(v):
        # "|" is the sed delimiter and "&" means the whole match in a
        # replacement, so both must be escaped or a key containing either
        # would silently corrupt the file.
        return v.replace(chr(92), chr(92)*2).replace("|", chr(92)+"|").replace("&", chr(92)+"&")

    prog = "".join(
        "s|^{}=.*|{}={}|".format(k, k, esc(v.strip())) + chr(10)
        for k, v in sorted(keys.items())
    )
    cmd = ["ssh", "-i", KEYFILE, "-o", "StrictHostKeyChecking=no", HOST,
           f"sudo cp {ENVPATH} {ENVPATH}.bak && "
           f"sudo sed -i -f /dev/stdin {ENVPATH} && "
           f"echo APPLIED && sudo grep -c '=$' {ENVPATH}"]
    r = subprocess.run(cmd, input=prog, text=True, capture_output=True)
    out = (r.stdout + r.stderr).strip()
    if "APPLIED" not in out:
        print("\nFAILED:\n" + out); return 1
    still_blank = out.splitlines()[-1].strip()
    print(f"\nApplied. {still_blank} key(s) still blank on the server.")
    print("Backup at /etc/tickermover.env.bak")
    print("Now run:  ssh -i ~/.ssh/tickermover.key ubuntu@79.72.67.33 'sudo systemctl restart tickermover'")
    return 0

if __name__ == "__main__":
    sys.exit(main())
