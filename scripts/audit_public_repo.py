#!/usr/bin/env python3
"""Fail CI when public Git history contains private Loom data or known secrets."""
from __future__ import annotations

import re
import subprocess
import sys


FORBIDDEN_PATH = re.compile(
    r"(^|/)(?:\.loom-local|data|cards|sources)(?:/|$)|"
    r"(?:^|/)\.env$|\.(?:pem|key|p12|pfx|sqlite|sqlite3|db)$",
    re.IGNORECASE,
)
SECRET_PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "GitHub token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "OpenAI key": re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    "AWS access key": re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "Google API key": re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b"),
    "Slack token": re.compile(rb"\bxox[baprs]-[0-9A-Za-z-]{20,}\b"),
    "Stripe live key": re.compile(rb"\b[rs]k_live_[0-9A-Za-z]{16,}\b"),
    "credential URL": re.compile(rb"https?://[^\s/:@]{1,128}:[^\s/@]{1,128}@", re.I),
}


def git(*args: str, text: bool = True) -> str | bytes:
    return subprocess.run(
        ["git", *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=text,
    ).stdout


def main() -> int:
    tracked = str(git("ls-files")).splitlines()
    history_paths = str(git("log", "--all", "--format=", "--name-only")).splitlines()
    bad_paths = sorted({path for path in history_paths if FORBIDDEN_PATH.search(path)})
    if bad_paths:
        print("ERROR: private runtime paths exist in reachable history:", file=sys.stderr)
        for path in bad_paths:
            print(f"  {path}", file=sys.stderr)
        return 1

    object_lines = str(git("rev-list", "--objects", "--all")).splitlines()
    object_ids = [line.split(" ", 1)[0] for line in object_lines]
    findings: dict[str, int] = {name: 0 for name in SECRET_PATTERNS}
    scanned = 0
    for oid in object_ids:
        if str(git("cat-file", "-t", oid)).strip() != "blob":
            continue
        body = bytes(git("cat-file", "blob", oid, text=False))
        scanned += 1
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(body):
                findings[name] += 1

    hits = {name: count for name, count in findings.items() if count}
    if hits:
        print(f"ERROR: secret patterns found in {sum(hits.values())} reachable blobs", file=sys.stderr)
        for name, count in hits.items():
            print(f"  {name}: {count}", file=sys.stderr)
        return 1

    print(f"privacy audit passed: {len(tracked)} tracked paths, {scanned} reachable blobs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
