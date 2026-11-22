"""Builds and runs the hardened `docker run` command for one file.

SECURITY: only ever run this on the dedicated Runner host, never on the API server.
Plain Docker is not enough for hostile multi-tenant use; add gVisor (runsc) or Firecracker.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from .languages import Language

MAX_OUTPUT = 1_000_000
_FILENAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


def validate_filename(filename: str) -> str:
    if not filename or not _FILENAME.match(filename) or ".." in filename.split("/") or "//" in filename:
        raise ValueError(f"Unsafe filename: {filename!r}")
    return filename


def build_command(lang: Language, filename: str) -> tuple[list[str] | None, list[str]]:
    """Return (compile_argv or None, run_argv) with tokens substituted. No shell involved."""
    if lang.execution not in ("run", "validate"):
        raise ValueError(f"{lang.name} is '{lang.execution}' only: it has no execution support.")
    validate_filename(filename)
    stem = Path(filename).stem
    sub = lambda t: t.replace("{file}", filename).replace("{stem}", stem).replace("{out}", f"/tmp/{stem}")
    comp = [sub(t) for t in lang.compile] if lang.compile else None
    return comp, [sub(t) for t in (lang.run or ())]


def docker_run_argv(lang: Language, filename: str, workspace: str, *, container_name: str,
                    memory: str = "256m", cpus: str = "1", pids: int = 128,
                    network: bool = False) -> list[str]:
    if not _NAME.match(container_name):
        raise ValueError("Bad container name")
    ws = Path(workspace)
    if not ws.is_absolute() or "," in workspace:
        raise ValueError("Workspace must be an absolute path without commas")
    comp, run = build_command(lang, filename)
    script = shlex.join(run) if not comp else f"{shlex.join(comp)} && {shlex.join(run)}"

    argv = ["docker", "run", "--rm", "--name", container_name]
    if not network:
        argv += ["--network", "none"]
    argv += [
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", str(pids), "--memory", memory, "--memory-swap", memory, "--cpus", cpus,
        "--user", "65534:65534",
        "--tmpfs", "/tmp:rw,exec,nosuid,size=128m",
        "-e", "HOME=/tmp",
    ]
    for k, v in lang.env:
        argv += ["-e", f"{k}={v}"]
    argv += ["--mount", f"type=bind,source={workspace},target=/work,readonly", "-w", "/work",
             lang.image or "", "sh", "-c", script]
    return argv


@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    container: str


def stop_container(name: str) -> None:
    if _NAME.match(name):
        subprocess.run(["docker", "kill", name], capture_output=True, timeout=10)


def run_sandboxed(lang: Language, filename: str, workspace: str, *, timeout_s: int = 10,
                  network: bool = False) -> RunResult:
    name = f"dm-run-{uuid.uuid4().hex[:12]}"
    argv = docker_run_argv(lang, filename, workspace, container_name=name, network=network)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout_s)
        out, err, code, timed = p.stdout, p.stderr, p.returncode, False
    except subprocess.TimeoutExpired:
        stop_container(name)
        out, err, code, timed = "", f"Timed out after {timeout_s}s and was stopped.", -1, True
    trunc = len(out) > MAX_OUTPUT or len(err) > MAX_OUTPUT
    return RunResult(code, out[:MAX_OUTPUT], err[:MAX_OUTPUT], timed, trunc, name)
