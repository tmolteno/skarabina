#!/usr/bin/env python3
"""Reject AI-assistant attribution in this repository.

The owner does not want an AI assistant credited as a contributor here, so no
commit message, tracked file or tracked file name may name one.  Two tiers:

  * attribution -- co-author trailers, "generated with ..." credits and
    assistant e-mail addresses: rejected in every tracked file and every
    commit message, including this file's own source (the patterns are
    written so they cannot match their own text);
  * bare mention -- the assistant's name and its model names: rejected
    everywhere except this file (it must hold the patterns it enforces) and
    .gitignore (its ignore rule names the file it has to exclude).

Modes:
  (default)          scan the tracked files (the index) and their names
  --message FILE     scan one commit message (the commit-msg hook passes the
                     message file git handed it)
  --commits RANGE    scan the commit messages in RANGE (CI, the pushed range)
  --history          scan every commit message reachable from any ref

Exit 0 when clean, 1 on a violation, 2 on misuse.  Standard library only.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# --- the patterns ---------------------------------------------------------
# Each is written so that its own source text cannot satisfy it (bracketed
# alternations, escaped dots); the bare-mention tier is exempted for this file
# below anyway, because it has to spell out what it forbids.
TIER1 = (
    (
        "co-author trailer",
        re.compile(
            r"\bco[- ]?authored[- ]?by\b[^\n]{0,160}\b(?:claude|anthropic|opu[s4]|sonnet|haiku)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "assistant credit",
        re.compile(
            r"\b(?:generated|created|written|assisted|helped)\s+(?:with|using|by)\s+"
            r"(?:claude|anthropic)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "assistant address",
        re.compile(r"\b[\w.+-]*@(?:anthropic|claude)\.(?:com|ai|org)\b", re.IGNORECASE),
    ),
)
TIER2 = re.compile(r"\b(?:claude|anthropic|opu[s4]|sonnet|haiku)\b", re.IGNORECASE)
BAD_NAME = re.compile(r"(?:^|/)(?:claude[^/]*|\.claude)(?:/|$)", re.IGNORECASE)

# Tier-2 exemptions: the files that must be able to name what they forbid.
# Tier 1 still applies to them, so an actual credit cannot hide there.
TIER2_EXEMPT = ("tools/check-no-ai-attribution.py", ".gitignore")


def _git(*args: str, cwd: str | None = None, input_bytes: bytes | None = None):
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _repo_root() -> str:
    code, out, err = _git("rev-parse", "--show-toplevel")
    if code != 0:
        sys.stderr.write(err.decode("utf-8", "replace") or "not inside a git repository\n")
        raise SystemExit(2)
    return out.decode("utf-8", "replace").strip()


def _grep_index(pattern: str, excludes: tuple[str, ...], root: str) -> list[str]:
    """Search the index (the staged content -- what a commit would carry)."""
    cmd = ["grep", "--cached", "-P", "-I", "-n", "-i", "-e", pattern, "--", "."]
    cmd += [":(exclude)" + e for e in excludes]
    code, out, err = _git(*cmd, cwd=root)
    if code not in (0, 1):  # 1 = no match; anything else is a real failure
        sys.stderr.write("git grep failed (is git built with PCRE support?):\n")
        sys.stderr.write(err.decode("utf-8", "replace"))
        raise SystemExit(2)
    return out.decode("utf-8", "replace").splitlines()


def scan_files(root: str) -> list[str]:
    hits: list[str] = []
    code, out, err = _git("ls-files", "-z", cwd=root)
    if code != 0:
        sys.stderr.write(err.decode("utf-8", "replace"))
        raise SystemExit(2)
    for rel in out.decode("utf-8", "replace").split("\0"):
        if rel and BAD_NAME.search(rel):
            hits.append(f"{rel}: forbidden file name (an assistant instruction file)")
    for why, pattern in TIER1:
        hits += [f"index: {line}: [{why}]" for line in _grep_index(pattern.pattern, (), root)]
    hits += [
        f"index: {line}: [bare mention]"
        for line in _grep_index(TIER2.pattern, TIER2_EXEMPT, root)
    ]
    return hits


def scan_message(label: str, text: str) -> list[str]:
    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for why, pattern in TIER1:
            if pattern.search(line):
                hits.append(f"{label}:{lineno}: [{why}] {line.strip()[:200]}")
        if TIER2.search(line):
            hits.append(f"{label}:{lineno}: [bare mention] {line.strip()[:200]}")
    return hits


def _log_messages(root: str, args: list[str]) -> list[tuple[str, str]]:
    code, out, err = _git("log", "--format=%x1e%H%x1f%B", *args, cwd=root)
    if code != 0:
        sys.stderr.write(err.decode("utf-8", "replace"))
        raise SystemExit(2)
    messages = []
    for record in out.decode("utf-8", "replace").split("\x1e"):
        if not record.strip():
            continue
        sha, _, body = record.partition("\x1f")
        messages.append((sha.strip(), body))
    return messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="reject AI-assistant attribution")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--message", metavar="FILE", help="scan one commit message file")
    mode.add_argument("--commits", metavar="RANGE", help="scan commit messages in RANGE")
    mode.add_argument(
        "--history", action="store_true", help="scan every commit message reachable from any ref"
    )
    args = parser.parse_args(argv)

    if args.message:
        try:
            text = Path(args.message).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            sys.stderr.write(f"{exc}\n")
            return 2
        hits = scan_message(args.message, text)
    elif args.commits or args.history:
        root = _repo_root()
        revs = ["--all"] if args.history else [args.commits]
        hits = []
        for sha, body in _log_messages(root, revs):
            hits += scan_message(sha[:12], body)
    else:
        hits = scan_files(_repo_root())

    if hits:
        for hit in hits:
            print(hit)
        print(f"FAIL: {len(hits)} AI-attribution violation(s)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
