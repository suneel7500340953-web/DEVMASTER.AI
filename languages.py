"""Language registry: what each language *really* needs to execute.

`execution` says what the platform can actually do with a file:
  run      -> compiled/interpreted in a sandbox container
  preview  -> rendered in the browser only (HTML, CSS): never "executed"
  validate -> checked, not run (JSON)
Syntax highlighting in the editor is independent of this table.
"""
from __future__ import annotations

import shutil
import subprocess
import textwrap
from dataclasses import dataclass

SQL_RUNNER = textwrap.dedent("""\
    import sqlite3, sys
    c = sqlite3.connect(':memory:'); buf = ''
    try:
        for line in open(sys.argv[1], encoding='utf-8'):
            buf += line
            if sqlite3.complete_statement(buf):
                cur = c.execute(buf); buf = ''
                if cur.description:
                    print(' | '.join(d[0] for d in cur.description))
                    for r in cur.fetchall():
                        print(' | '.join(map(str, r)))
        if buf.strip():
            c.execute(buf)
    except sqlite3.Error as e:
        print('SQL error:', e, file=sys.stderr); sys.exit(1)
""")


@dataclass(frozen=True)
class Language:
    id: str
    name: str
    extensions: tuple[str, ...]
    execution: str                      # run | preview | validate
    image: str | None = None            # sandbox image
    compile: tuple[str, ...] | None = None   # tokens: {file} {stem} {out}
    run: tuple[str, ...] | None = None
    requires: tuple[str, ...] = ()      # local binaries; "a|b" = either
    version_args: tuple[str, ...] = ("--version",)
    install: str = ""
    env: tuple[tuple[str, str], ...] = ()
    notes: str = ""


def _L(**kw) -> Language:
    return Language(**kw)


LANGUAGES: dict[str, Language] = {l.id: l for l in [
    _L(id="python", name="Python", extensions=(".py",), execution="run",
       image="python:3.12-slim", run=("python", "{file}"), requires=("python3|python",),
       install="Install Python 3.10+: https://www.python.org/downloads/ or `sudo apt install python3`."),
    _L(id="javascript", name="JavaScript", extensions=(".js", ".mjs"), execution="run",
       image="node:22-slim", run=("node", "{file}"), requires=("node",),
       install="Install Node.js LTS: https://nodejs.org/ or `sudo apt install nodejs`."),
    _L(id="typescript", name="TypeScript", extensions=(".ts",), execution="run",
       image="node:24-slim", run=("node", "{file}"), requires=("node",),
       install="Install Node.js 24+ (runs .ts by type stripping): https://nodejs.org/",
       notes="Type stripping only: no type checking, and no enums/namespaces. Use tsc for full checks."),
    _L(id="html", name="HTML", extensions=(".html", ".htm"), execution="preview",
       notes="Rendered in a sandboxed browser preview; nothing is executed server-side."),
    _L(id="css", name="CSS", extensions=(".css",), execution="preview",
       notes="Applied in the browser preview only."),
    _L(id="java", name="Java", extensions=(".java",), execution="run",
       image="eclipse-temurin:21-jdk", run=("java", "{file}"), requires=("javac", "java"),
       version_args=("-version",),
       install="Install a JDK 17+ (not just a JRE): https://adoptium.net/ or `sudo apt install default-jdk`.",
       notes="Single-file source launch (`java File.java`)."),
    _L(id="c", name="C", extensions=(".c",), execution="run",
       image="gcc:14", compile=("gcc", "-O2", "-Wall", "{file}", "-o", "{out}"), run=("{out}",),
       requires=("gcc",),
       install="Install GCC: `sudo apt install build-essential` (Linux), Xcode CLT (macOS), MinGW/MSYS2 (Windows)."),
    _L(id="cpp", name="C++", extensions=(".cpp", ".cc", ".cxx"), execution="run",
       image="gcc:14", compile=("g++", "-std=c++20", "-O2", "-Wall", "{file}", "-o", "{out}"), run=("{out}",),
       requires=("g++",),
       install="Install g++: `sudo apt install build-essential` (Linux), Xcode CLT (macOS), MinGW/MSYS2 (Windows)."),
    _L(id="csharp", name="C#", extensions=(".cs",), execution="run",
       image="mcr.microsoft.com/dotnet/sdk:10.0", run=("dotnet", "run", "{file}"), requires=("dotnet",),
       env=(("DOTNET_CLI_HOME", "/tmp"), ("DOTNET_NOLOGO", "1")),
       install="Install the .NET SDK 10+: https://dotnet.microsoft.com/download",
       notes="Single-file `dotnet run file.cs` needs .NET 10+. Older SDKs need a .csproj project."),
    _L(id="go", name="Go", extensions=(".go",), execution="run",
       image="golang:1.23", run=("go", "run", "{file}"), requires=("go",), version_args=("version",),
       env=(("GOCACHE", "/tmp/gocache"), ("GOPATH", "/tmp/gopath")),
       install="Install Go: https://go.dev/dl/ or `sudo apt install golang`."),
    _L(id="rust", name="Rust", extensions=(".rs",), execution="run",
       image="rust:1-slim", compile=("rustc", "-O", "{file}", "-o", "{out}"), run=("{out}",),
       requires=("rustc",),
       install="Install Rust via rustup: https://rustup.rs/"),
    _L(id="php", name="PHP", extensions=(".php",), execution="run",
       image="php:8.3-cli", run=("php", "{file}"), requires=("php",),
       install="Install PHP CLI: `sudo apt install php-cli` or https://www.php.net/downloads"),
    _L(id="sql", name="SQL (SQLite)", extensions=(".sql",), execution="run",
       image="python:3.12-slim", run=("python", "-c", SQL_RUNNER, "{file}"), requires=("python3|python",),
       install="Needs Python 3 (uses its built-in sqlite3). See the Python install note.",
       notes="Executes against an in-memory SQLite database only. PostgreSQL/MySQL dialects belong to the Database studio."),
    _L(id="kotlin", name="Kotlin", extensions=(".kt",), execution="run",
       image="devmaster/runtime-kotlin:latest",
       compile=("kotlinc", "{file}", "-include-runtime", "-d", "/tmp/{stem}.jar"),
       run=("java", "-jar", "/tmp/{stem}.jar"), requires=("kotlinc", "java"), version_args=("-version",),
       install="Install Kotlin compiler + JDK: https://kotlinlang.org/docs/command-line.html or `sdk install kotlin`.",
       notes="Uses a curated image that is NOT built yet (runtimes/ in the repo)."),
    _L(id="dart", name="Dart", extensions=(".dart",), execution="run",
       image="dart:stable", run=("dart", "run", "{file}"), requires=("dart",),
       env=(("PUB_CACHE", "/tmp/pub-cache"),),
       install="Install the Dart SDK: https://dart.dev/get-dart"),
    _L(id="json", name="JSON", extensions=(".json",), execution="validate",
       image="python:3.12-slim", run=("python", "-m", "json.tool", "{file}"), requires=("python3|python",),
       install="Needs Python 3 for validation. See the Python install note.",
       notes="Validated and pretty-printed, not executed."),
]}

_BY_EXT = {ext: l for l in LANGUAGES.values() for ext in l.extensions}


def language_for_filename(name: str) -> Language | None:
    dot = name.rfind(".")
    return _BY_EXT.get(name[dot:].lower()) if dot != -1 else None


@dataclass(frozen=True)
class RuntimeStatus:
    language: str
    available: bool
    missing: tuple[str, ...]
    version: str | None
    install: str
    mode: str


def detect_runtime(lang: Language, which=shutil.which, runner=subprocess.run) -> RuntimeStatus:
    """Check the LOCAL machine for the tools a language needs."""
    if lang.execution == "preview":
        return RuntimeStatus(lang.id, True, (), None, "", lang.execution)
    missing, first_bin = [], None
    for req in lang.requires:
        found = next((b for b in req.split("|") if which(b)), None)
        if found is None:
            missing.append(req)
        elif first_bin is None:
            first_bin = found
    version = None
    if not missing and first_bin:
        try:
            r = runner([first_bin, *lang.version_args], capture_output=True, text=True, timeout=5)
            text = (r.stdout or r.stderr or "").strip()
            version = text.splitlines()[0] if text else None
        except Exception:
            version = None
    return RuntimeStatus(lang.id, not missing, tuple(missing), version,
                         "" if not missing else lang.install, lang.execution)


def docker_available(which=shutil.which) -> bool:
    return which("docker") is not None


DOCKER_INSTALL = "Install Docker: https://docs.docker.com/get-docker/ (sandboxed execution needs it)."
