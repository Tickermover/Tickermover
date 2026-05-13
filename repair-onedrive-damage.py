#!/usr/bin/env python3
"""
repair-onedrive-damage.py

OneDrive's Files-On-Demand feature keeps corrupting our HTML templates by
truncating them and appending null-byte padding. This script auto-detects and
repairs every template by:

  1. Stripping trailing null bytes (cheap, always safe).
  2. If the file no longer ends with `</html>`, splicing the missing tail
     from the safety backup using a longest-common-substring anchor.

Usage:
    python repair-onedrive-damage.py

Exit code 0 = all good, 1 = manual intervention needed.
"""
import os, sys, re

REPO = os.path.dirname(os.path.abspath(__file__))
# The safety backup created May 13, 2026. Adjust path if you've made a newer one.
BACKUP_DIRS = sorted([
    os.path.join(os.path.dirname(REPO), d)
    for d in os.listdir(os.path.dirname(REPO))
    if d.startswith('PopDetector_v5_safety_backup_')
], reverse=True)
BACKUP = BACKUP_DIRS[0] if BACKUP_DIRS else None

TEMPLATES = [
    ('templates/landing.html', '</html>'),
    ('templates/dashboard.html', '</html>'),
    ('templates/infographics.html', '</html>'),
    ('templates/earnings_infographic.html', '</html>'),
    ('templates/earnings_tearsheet.html', '</html>'),
]

def splice_tail(cur: bytes, bak: bytes) -> bytes | None:
    """Find longest match between end of `cur` and somewhere in `bak`,
    then return cur + bak[match_end:]. Returns None if no anchor found."""
    for length in (500, 400, 300, 200, 150, 100, 80, 60, 40):
        for start in range(len(cur) - length, max(0, len(cur) - 12000) - 1, -1):
            chunk = cur[start:start+length]
            bi = bak.find(chunk)
            if bi != -1:
                return cur + bak[bi+length:]
    return None

def repair_one(fp: str, must_end: str) -> tuple[bool, str]:
    full = os.path.join(REPO, fp)
    if not os.path.exists(full):
        return False, 'missing'
    d = open(full, 'rb').read()
    nul = d.count(b'\x00')
    clean = d.rstrip(b'\x00')
    txt = clean.decode('utf-8', errors='replace')
    if txt.rstrip().endswith(must_end):
        if nul > 0:
            with open(full, 'wb') as f:
                f.write(clean)
            return True, f'stripped {nul} trailing nulls'
        return True, 'already clean'
    # Need a splice from backup
    if not BACKUP:
        return False, 'no backup folder found'
    bak_path = os.path.join(BACKUP, fp)
    if not os.path.exists(bak_path):
        return False, f'no backup at {bak_path}'
    bak = open(bak_path, 'rb').read().rstrip(b'\x00')
    bak_txt = bak.decode('utf-8', errors='replace')
    if not bak_txt.rstrip().endswith(must_end):
        return False, 'backup itself is truncated'
    # Special case for landing.html which always truncates at the same spot
    if fp.endswith('landing.html'):
        sentinel = 'threshold:0.05});'
        if sentinel in txt:
            cut = txt.rfind(sentinel) + len(sentinel)
            fixed = txt[:cut] + "\n  document.querySelectorAll('.reveal,.reveal-stagger').forEach(el => io.observe(el));\n  })();\n</script>\n</body>\n</html>\n"
            with open(full, 'w', encoding='utf-8', newline='\n') as f:
                f.write(fixed)
            return True, f'restored landing.html tail ({len(fixed.encode("utf-8"))} bytes)'
    # General case: longest anchor splice
    fixed = splice_tail(clean, bak)
    if fixed is None:
        return False, 'no anchor found'
    if not fixed.decode('utf-8', errors='replace').rstrip().endswith(must_end):
        return False, 'splice did not close </html>'
    with open(full, 'wb') as f:
        f.write(fixed)
    return True, f'spliced from backup ({len(fixed)} bytes)'

def main() -> int:
    print('=== OneDrive damage repair ===')
    print(f'Repo:   {REPO}')
    print(f'Backup: {BACKUP or "(none found)"}')
    print()
    fails = 0
    for fp, must_end in TEMPLATES:
        ok, msg = repair_one(fp, must_end)
        mark = '✓' if ok else '✗'
        print(f'  {mark} {fp:48s} - {msg}')
        if not ok:
            fails += 1
    # Final verification
    print()
    print('=== Final state ===')
    for fp, must_end in TEMPLATES:
        full = os.path.join(REPO, fp)
        d = open(full, 'rb').read()
        nul = d.count(b'\x00')
        txt = d.decode('utf-8', errors='replace')
        ends = txt.rstrip().endswith(must_end)
        ok = nul == 0 and ends
        print(f'  {fp:48s}  {len(d):>7d}b  nuls={nul:<3d}  ends-OK={ends}  {"OK" if ok else "FAIL"}')
    if fails == 0:
        print('\nVERDICT: All templates healthy. Safe to deploy.')
        return 0
    else:
        print(f'\nVERDICT: {fails} file(s) need manual repair. Inspect the backup folder.')
        return 1

if __name__ == '__main__':
    sys.exit(main())
