"""Reading a project safely and applying approved changes. The AI never writes directly:
it produces a ChangeSet; only apply_changes() with the matching approval id touches disk."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".devmaster", ".idea", ".next"}
PROTECTED_NAMES = {".env", ".git", ".devmaster", ".ssh", ".aws", ".npmrc", ".pypirc", "id_rsa", "id_ed25519"}
PROTECTED_SUFFIX = (".pem", ".key", ".p12", ".pfx")
MAX_FILE = 200_000


class PathError(ValueError): pass
class ApprovalError(PermissionError): pass
class ConflictError(RuntimeError):
    def __init__(self, conflicts): super().__init__("Files changed since the proposal: " + ", ".join(conflicts)); self.conflicts = conflicts


def safe_rel(path) -> str:
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise PathError(f"Bad path: {path!r}")
    p = path.strip().replace("\\", "/")
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        raise PathError(f"Absolute path not allowed: {path!r}")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts or ".." in parts:
        raise PathError(f"Unsafe path: {path!r}")
    return "/".join(parts)


def is_protected(rel: str) -> bool:
    parts = rel.split("/")
    return any(x in PROTECTED_NAMES for x in parts) or parts[-1].startswith(".env") or parts[-1].lower().endswith(PROTECTED_SUFFIX)


def snapshot(root, max_files=2000) -> dict[str, str]:
    """Read-only scan. Secrets, binaries, symlinks, huge files and build dirs are never read (or sent to an LLM)."""
    root, out = Path(root).resolve(), {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS and not (Path(dirpath) / d).is_symlink())
        for f in sorted(files):
            full = Path(dirpath) / f
            rel = full.relative_to(root).as_posix()
            if full.is_symlink() or is_protected(rel) or len(out) >= max_files:
                continue
            try:
                if full.stat().st_size > MAX_FILE:
                    continue
                raw = full.read_bytes()
                if b"\x00" in raw[:2048]:
                    continue
                out[rel] = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
    return out


@dataclass(frozen=True)
class FileChange:
    path: str
    action: str                 # create | modify | delete
    content: str | None
    original: str | None

    def diff(self) -> str:
        a = (self.original or "").splitlines(keepends=True)
        b = (self.content or "").splitlines(keepends=True)
        name_a = "/dev/null" if self.action == "create" else f"a/{self.path}"
        name_b = "/dev/null" if self.action == "delete" else f"b/{self.path}"
        return "".join(difflib.unified_diff(a, b, name_a, name_b))


@dataclass(frozen=True)
class ChangeSet:
    changes: tuple[FileChange, ...] = ()

    @property
    def id(self) -> str:
        h = lambda s: hashlib.sha256((s or "").encode()).hexdigest()
        raw = json.dumps([(c.path, c.action, h(c.content), h(c.original)) for c in self.changes])
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def diff_text(self) -> str:
        return "\n".join(c.diff() for c in self.changes)

    def __bool__(self): return bool(self.changes)


def diff_changeset(original: dict, proposed: dict) -> ChangeSet:
    out = []
    for p in sorted(set(original) | set(proposed)):
        o, n = original.get(p), proposed.get(p)
        if o is None and n is not None: out.append(FileChange(p, "create", n, None))
        elif o is not None and n is None: out.append(FileChange(p, "delete", None, o))
        elif o != n: out.append(FileChange(p, "modify", n, o))
    return ChangeSet(tuple(out))


_BLOCK = re.compile(r"<<<FILE (create|modify|delete) ([^\s>]+)>>>\n?(.*?)\n?<<<END>>>", re.S)


def parse_file_blocks(text: str) -> list[tuple[str, str, str]]:
    return [(a, p, c) for a, p, c in _BLOCK.findall(text)]


def apply_blocks(proposed: dict, blocks) -> list[str]:
    """Merge model output into the in-memory proposed project. Returns rejected paths (with reason)."""
    rejected = []
    for action, path, content in blocks:
        try:
            rel = safe_rel(path)
        except PathError:
            rejected.append(f"{path} (unsafe path)"); continue
        if is_protected(rel):
            rejected.append(f"{rel} (protected file)"); continue
        if action == "delete":
            proposed.pop(rel, None)
        else:
            proposed[rel] = content if content.endswith("\n") or not content else content + "\n"
    return rejected


def _inside(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def apply_changes(root, cs: ChangeSet, approval_id: str) -> str:
    """Write an approved ChangeSet. All-or-nothing, never overwrites files edited since the proposal,
    keeps backups for undo. Returns the backup id."""
    if approval_id != cs.id:
        raise ApprovalError("Approval id does not match this change set. Nothing was written.")
    root = Path(root).resolve()
    conflicts = []
    for c in cs.changes:
        rel = safe_rel(c.path)
        if is_protected(rel):
            raise PathError(f"Protected file: {rel}")
        t = root / rel
        if not _inside(root, t) or t.is_symlink():
            raise PathError(f"Path escapes project: {rel}")
        if c.action == "create":
            if t.exists(): conflicts.append(rel)
        elif not t.is_file() or t.read_bytes().decode("utf-8", "replace") != c.original:
            conflicts.append(rel)
    if conflicts:
        raise ConflictError(conflicts)

    bid = time.strftime("%Y%m%d-%H%M%S") + "-" + cs.id[:8]
    bdir = root / ".devmaster" / "backups" / bid
    manifest = {"created": [], "restored": []}
    try:
        for c in cs.changes:
            t = root / safe_rel(c.path)
            if c.action != "create":
                (bdir / c.path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(t, bdir / c.path)
                manifest["restored"].append(c.path)
            else:
                manifest["created"].append(c.path)
            if c.action == "delete":
                t.unlink()
            else:
                t.parent.mkdir(parents=True, exist_ok=True)
                tmp = t.with_name(t.name + ".dm-tmp")
                tmp.write_bytes(c.content.encode("utf-8"))
                os.replace(tmp, t)
    except Exception:
        _restore(root, bdir, manifest)
        raise
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "manifest.json").write_text(json.dumps(manifest))
    return bid


def _restore(root: Path, bdir: Path, manifest: dict):
    for p in manifest["created"]:
        (root / p).unlink(missing_ok=True)
    for p in manifest["restored"]:
        src = bdir / p
        if src.exists():
            (root / p).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, root / p)


def revert(root, backup_id: str):
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{8}", backup_id):
        raise ValueError("Bad backup id")
    root = Path(root).resolve()
    bdir = root / ".devmaster" / "backups" / backup_id
    _restore(root, bdir, json.loads((bdir / "manifest.json").read_text()))
