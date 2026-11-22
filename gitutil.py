"""Read-only git helpers for change summaries. Run on a trusted host/runner only.
Hardened flags stop repo config from executing external programs."""
from __future__ import annotations

import subprocess

_SAFE = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "--no-pager"]


def _git(root, *args, limit=60_000) -> str:
    try:
        r = subprocess.run([*_SAFE, *args], cwd=root, capture_output=True, text=True, errors="replace", timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout[:limit] if r.returncode == 0 else ""


def git_context(root) -> str:
    log = _git(root, "log", "-n", "10", "--oneline")
    diff = _git(root, "diff", "--no-ext-diff", "--no-textconv", "HEAD") or _git(root, "diff", "--no-ext-diff", "--no-textconv")
    if not (log or diff):
        return ""
    return f"RECENT COMMITS:\n{log}\nUNCOMMITTED CHANGES (diff):\n{diff}"
