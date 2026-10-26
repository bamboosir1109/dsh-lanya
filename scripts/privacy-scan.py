#!/usr/bin/env python3
"""Pre-publish privacy scan.

Run this before every push. It walks the files git would publish (tracked files
plus anything new that is not gitignored) and looks for two grades of problem:

  BLOCK - identity that must never leave this machine: a real QQ number sitting
          in an identity context, an email address, a bare 32-hex token.
  NOTE  - environment better kept private, but it names nobody: absolute paths
          from the author's disk, IP addresses.

Exit codes: 0 clean, 1 block-level findings (do not push), 2 scan could not run.

ASCII-only source: this runs on the same Windows box that reads these files with
an ANSI codepage, so no non-ASCII literals live in here.

Usage:
  python privacy-scan.py               scan the repo this file lives in
  python privacy-scan.py --all         include gitignored files (noisier)
  python privacy-scan.py --path X      scan a different tree
"""

import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BS = chr(92)  # keeps regex escaping readable in a file that must stay ASCII

SKIP_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".zip", ".7z",
    ".exe", ".dll", ".mp3", ".wav", ".ogg", ".mp4", ".webm", ".ttf", ".woff",
    ".woff2", ".pdf", ".pyc", ".log",
}

SELF = os.path.basename(os.path.abspath(__file__))

# A bare number only matters when the line is about an identity.
IDENTITY_CONTEXT = re.compile(r"(?i)(qq|group|admin|uin|nick|owner|user_?id)")

# Round delays, byte sizes and protocol codes are code, not identity.
NOT_A_NUMBER = {
    1024, 2048, 4096, 8192, 16384, 20000, 30000, 40000, 60000, 86400,
    1048576, 1073741824, 86400000, 15000, 180000, 200000, 240000, 300000,
    400000, 999999, 99999, 123456789, 1413141, 151922, 232938, 342020,
    339933, 141821, 10200, 21600, 22003, 86038, 86090, 86101,
    19387, 19388, 19389, 19400, 8720, 6099, 9880, 3000,
}

SEVERITY_BLOCK = "block"
SEVERITY_NOTE = "note"

PATTERNS = [
    ("qq-number", SEVERITY_BLOCK, re.compile(r"[0-9]{5,11}")),
    ("email", SEVERITY_BLOCK, re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}")),
    ("hex-token", SEVERITY_BLOCK, re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")),
    # Backslashes are normalised to "/" before matching (see scan_line), so the
    # separator classes here never have to escape one.
    ("abs-path-win", SEVERITY_NOTE,
     re.compile("[A-Za-z]:/*(?:DSH|Users|Windows|code)")),
    ("abs-path-unix", SEVERITY_NOTE, re.compile(r"(?<![A-Za-z0-9])/(?:home|Users)/[A-Za-z0-9_.-]+/")),
    ("ipv4", SEVERITY_NOTE, re.compile(r"(?:[0-9]{1,3}[.]){3}[0-9]{1,3}")),
]


def tracked_files(root, include_ignored):
    """What git would actually publish."""
    try:
        args = ["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
        if include_ignored:
            args = ["git", "-C", root, "ls-files", "-z", "--cached", "--others"]
        out = subprocess.run(args, capture_output=True, check=True).stdout
    except Exception as exc:
        print("cannot list files: %s" % exc)
        return None
    names = [n for n in out.decode("utf-8", "replace").split(chr(0)) if n]
    return [os.path.join(root, n) for n in names]


def local_terms(root):
    """Real names / pen names / anything else that must not ship."""
    path = os.path.join(root, "privacy-terms.local.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        return [str(t).strip() for t in doc.get("terms", []) if str(t).strip()]
    except Exception:
        return []


def _qq_ok(line, match):
    """Is this bare number worth a block-level flag?"""
    start, end = match.start(), match.end()
    # #339933 is a colour, 0x1048576 is a constant.
    if start and line[start - 1] in "#xX":
        return False
    # groupLimit / h264profile: the digits belong to an identifier.
    if start and (line[start - 1].isalpha() or line[start - 1] == "_"):
        return False
    if end < len(line) and (line[end].isalpha() or line[end] == "_"):
        return False
    # 100000.0 is a float bound, not a number somebody owns.
    if end + 1 < len(line) and line[end] == "." and line[end + 1].isdigit():
        return False
    try:
        if int(match.group(0)) in NOT_A_NUMBER:
            return False
    except ValueError:
        return False
    return bool(IDENTITY_CONTEXT.search(line))


def scan_line(line):
    hits = []
    # Same length, so match offsets stay usable for the "#" colour guard.
    norm = line.replace(BS, "/")
    for name, severity, rx in PATTERNS:
        for m in rx.finditer(norm):
            hit = m.group(0)
            if name == "qq-number" and not _qq_ok(line, m):
                continue
            if name == "ipv4":
                if hit.startswith("127.") or hit in ("0.0.0.0", "255.255.255.255"):
                    continue
                try:
                    if any(int(p) > 255 for p in hit.split(".")):
                        continue
                except ValueError:
                    continue
            hits.append((name, severity, hit))
    return hits


def scan_file(path, terms):
    findings = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except Exception:
        return findings
    rel = os.path.relpath(path, REPO)
    if SELF in rel:
        return findings
    for i, line in enumerate(lines, 1):
        for name, severity, hit in scan_line(line):
            findings.append((severity, name, hit, rel, i, line.strip()[:120]))
        for t in terms:
            if t and t in line:
                findings.append((SEVERITY_BLOCK, "local-term", t, rel, i, line.strip()[:120]))
    return findings


def main():
    root = REPO
    include_ignored = False
    argv = sys.argv[1:]
    if "--all" in argv:
        include_ignored = True
    if "--path" in argv:
        try:
            root = os.path.abspath(argv[argv.index("--path") + 1])
        except Exception:
            print("--path needs a directory")
            return 2

    files = tracked_files(root, include_ignored)
    if files is None:
        return 2

    terms = local_terms(root)
    findings = []
    scanned = 0
    for f in files:
        if not os.path.isfile(f):
            continue
        if os.path.splitext(f)[1].lower() in SKIP_EXT:
            continue
        scanned += 1
        findings.extend(scan_file(f, terms))

    blocks = [f for f in findings if f[0] == SEVERITY_BLOCK]
    notes = [f for f in findings if f[0] == SEVERITY_NOTE]

    print("scanned %d file(s) under %s" % (scanned, root))
    if not terms:
        print("(no privacy-terms.local.json - only the built-in patterns ran)")

    if notes:
        print("")
        print("%d note(s) - environment, not identity:" % len(notes))
        for severity, name, hit, rel, line_no, ctx in notes:
            print("  %-14s %-44s %s:%d" % (name, hit, rel, line_no))

    if not blocks:
        print("")
        print("clean: no identity leak found - safe to push")
        return 0

    print("")
    print("%d BLOCK-level finding(s) - do NOT push until these are gone:" % len(blocks))
    for severity, name, hit, rel, line_no, ctx in blocks:
        print("  %-14s %-24s %s:%d  %s" % (name, hit, rel, line_no, ctx))
    print("")
    print("Placeholders that are fine: YOUR_ADMIN_QQ / YOUR_GROUP_ID / BOT_QQ.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
