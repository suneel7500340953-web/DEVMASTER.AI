"""Runs a project's tests against PROPOSED files (a temp copy), never against the user's directory."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..runs.sandbox import hardened_argv, stop_container
from .workspace import safe_rel

MAX_OUT = 20_000


@dataclass
class TestResult:
    ran: bool
    passed: bool | None
    output: str
    command: str = ""
    reason: str = ""


def detect_test_command(files: dict) -> tuple[str, list[str], tuple, str] | None:
    """(image, argv, env, label) for the first supported test setup, else None."""
    names = list(files)
    if any(n.endswith(("_test.go",)) for n in names) and "go.mod" in files:
        return ("golang:1.23", ["go", "test", "./..."], (("GOCACHE", "/tmp/gocache"), ("GOPATH", "/tmp/gopath")), "go test")
    if any(n.split("/")[-1].startswith("test_") and n.endswith(".py") or n.endswith("_test.py") for n in names):
        return ("python:3.12-slim", ["python", "-m", "unittest", "discover", "-v"], (("PYTHONDONTWRITEBYTECODE", "1"),), "python unittest")
    if any(n.endswith((".test.js", ".test.mjs")) for n in names):
        return ("node:22-slim", ["node", "--test"], (), "node --test")
    return None


def _stage(files: dict, target: str):
    for rel, content in files.items():
        p = Path(target) / safe_rel(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _truncate(s: str) -> str:
    return s if len(s) <= MAX_OUT else s[:MAX_OUT // 2] + "\n...[truncated]...\n" + s[-MAX_OUT // 2:]


class DockerTestRunner:
    """Safe option: tests run in the hardened, network-less container (needs Docker on a runner host)."""

    def __init__(self, timeout_s=90):
        self.timeout_s = timeout_s

    def run(self, files: dict) -> TestResult:
        det = detect_test_command(files)
        if not det:
            return TestResult(False, None, "", reason="No supported tests found (Python unittest, node --test, go test).")
        image, argv, env, label = det
        import shlex
        with tempfile.TemporaryDirectory() as d:
            _stage(files, d)
            os.chmod(d, 0o755)
            name = f"dm-test-{uuid.uuid4().hex[:10]}"
            cmd = hardened_argv(image, shlex.join(argv), d, container_name=name, env=env)
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                stop_container(name)
                return TestResult(True, False, f"Tests timed out after {self.timeout_s}s.", label)
            except OSError as e:
                return TestResult(False, None, "", label, f"Docker not available: {e}")
            return TestResult(True, p.returncode == 0, _truncate(p.stdout + p.stderr), label)


class LocalTestRunner:
    """UNSAFE: runs the tests directly on THIS machine with no isolation. Only for your own machine and
    your own code (e.g. Termux/laptop). Never use on a shared server."""

    def __init__(self, timeout_s=90):
        self.timeout_s = timeout_s

    def run(self, files: dict) -> TestResult:
        det = detect_test_command(files)
        if not det:
            return TestResult(False, None, "", reason="No supported tests found (Python unittest, node --test, go test).")
        _, argv, env, label = det
        if argv[0] == "python":
            argv = [sys.executable, *argv[1:]]
        with tempfile.TemporaryDirectory() as d:
            _stage(files, d)
            e = {"PATH": os.environ.get("PATH", ""), "HOME": d, "PYTHONDONTWRITEBYTECODE": "1", **dict(env)}
            try:
                p = subprocess.run(argv, cwd=d, env=e, capture_output=True, text=True, errors="replace", timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                return TestResult(True, False, f"Tests timed out after {self.timeout_s}s.", label)
            except OSError as ex:
                return TestResult(False, None, "", label, f"Runtime missing: {ex}")
            return TestResult(True, p.returncode == 0, _truncate(p.stdout + p.stderr), label)
