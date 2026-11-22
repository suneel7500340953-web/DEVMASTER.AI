# DEVMASTER AI — Architecture (Phase 0)

Status: **design only. No module is implemented or tested yet.** See `ROADMAP.md`.

## 1. Principles
1. **Modular monolith first.** One FastAPI app with strict module boundaries (`modules/*`), shared auth/project services. A module may only call another through its `service.py` interface, never its tables. This allows later extraction into services without a rewrite.
2. **User code never runs on the API server.** All execution happens in the separate **Runner** service inside sandboxed containers.
3. **Long work is a job.** Anything > ~2 s (runs, builds, deploys, git clone, AI streaming bookkeeping) goes through Celery; clients follow progress over WebSocket/SSE.
4. **Everything is audited.** State-changing actions write to an append-only `audit_log`.
5. **Studios are plugins.** Web, API, Data/ML, Game, Mobile and Desktop studios are data + code behind one `StudioPlugin` interface.
6. **Provider-independent AI.** One `LLMProvider` interface; OpenAI-compatible (Groq, OpenAI, vLLM, Ollama), Anthropic, and others are adapters.

## 2. System overview
```
Browser (React+TS, Monaco, xterm.js)
   │ HTTPS REST + WebSocket
   ▼
Reverse proxy (Caddy/Nginx, TLS, rate limit)
   ▼
API (FastAPI) ───── PostgreSQL (platform data)
   │  │              Redis (cache, pub/sub, Celery broker, rate limits)
   │  │              Object storage: S3/MinIO (file blobs, run logs, artifacts)
   │  └─ enqueue ──▶ Celery workers (runs, git, deploy, AI post-processing)
   │                      │ gRPC/HTTPS (mTLS, signed job tokens)
   ▼                      ▼
Preview gateway      RUNNER service (separate host/VPC, no DB access)
(signed subdomains)       └─ container runtime: Docker + gVisor (runsc), or Firecracker
                              ephemeral sandbox per run / per terminal session
```
The API **never** mounts `/var/run/docker.sock`. Only the Runner talks to the container runtime, and it only accepts jobs signed by the API.

## 3. Sandbox security model (non-negotiable)
- Separate machine/VPC subnet from API and databases; Runner has no credentials to platform DB.
- Per-run container: non-root user, read-only root FS, `tmpfs` scratch, writable workspace copy only, `--cap-drop=ALL`, `no-new-privileges`, seccomp profile, PID/CPU/memory/disk limits, wall-clock timeout, output size cap.
- **Network off by default.** Opt-in per run profile (e.g. package install step) through an allow-list egress proxy; block cloud metadata IPs (169.254.169.254) and private ranges.
- Strong isolation runtime (gVisor `runsc` or Firecracker microVMs) for untrusted multi-tenant use. Plain Docker alone is **not** sufficient for hostile users.
- Images from a curated registry only (`devmaster/runtime-*`), pinned by digest.
- Secrets injected as env vars only into runs that request them; masked in logs.
- Interactive terminal = PTY inside the project's sandbox container via Runner WebSocket, idle-timeout and per-user concurrency limits.
- Previews are served from separate origins (`<id>.preview.example.com`) so user JS cannot touch the platform's cookies.

## 4. Backend modules
| Module | Responsibility |
|---|---|
| `auth` | Registration, login, argon2id hashes, JWT access (15 min) + rotating refresh tokens (httpOnly cookie), API tokens, optional OAuth/OIDC |
| `projects` | Projects, templates, members, RBAC (`owner/admin/editor/viewer`) |
| `files` | Virtual file tree, content-addressed blobs, versions, search |
| `git` | Init/commit/branch/diff/merge/push/pull (git CLI executed in Runner/worker, never in API process) |
| `runs` | Run/Test/Build/Lint jobs, log streaming, artifacts, terminal sessions |
| `tests` | Parsers for pytest/jest/vitest/JUnit XML → `test_results` |
| `databases` | Managed per-project DBs on a **separate** cluster, SQL console, schema browser, query history |
| `ai` | Provider registry, conversations, context builder, streaming, diff-based file edits, usage accounting |
| `deployments` | Targets (registry/SSH/static host), build → push → release, status, rollback |
| `plugins` | Manifest validation, install/enable per project, extension points |
| `audit` | Append-only audit log + query API |

Each module folder: `router.py` (HTTP), `schemas.py` (Pydantic), `models.py` (SQLAlchemy), `service.py` (business logic), `repo.py` (queries), `tasks.py` (Celery).

## 5. Plugin / Studio contract
```python
class StudioPlugin(Protocol):
    key: str                      # "studio-web"
    def templates(self) -> list[ProjectTemplate]: ...
    def run_profiles(self) -> list[RunProfile]: ...      # image, command, limits, network policy
    def editor_config(self) -> EditorConfig: ...         # languages, LSP, default panels
    def preview(self) -> PreviewSpec | None: ...         # static | port-proxy | notebook | emulator-web
    def ai_context_hints(self) -> str: ...               # extra system-prompt guidance
```
Plugins ship a `plugin.json` manifest (name, version, permissions requested, run profiles, UI panels). Frontend panels are loaded from a registry (lazy-loaded React components). Plugins cannot access the DB directly — only SDK services (`files`, `runs`, `ai`).

## 6. Studios (build order and honest limits)
| Studio | Stack in sandbox | Preview | Limits |
|---|---|---|---|
| Web | Node/Vite, static HTML | live iframe via preview gateway | — |
| API | FastAPI/Express/Flask | proxied port + OpenAPI viewer + HTTP client | — |
| Database | Postgres/SQLite | SQL console, ER view | managed DBs on separate cluster |
| Data/ML | Python + Jupyter kernel | notebook UI | GPU needs GPU runners (later) |
| Game | Phaser/Godot web export | iframe | no native console builds |
| Mobile | React Native/Expo, Android Gradle | Expo web preview, Android APK artifact | **iOS builds need macOS runners** — out of scope initially |
| Desktop | Electron/Tauri | web preview of UI, packaged artifacts per OS | Windows/macOS packaging needs matching runners |

## 7. Frontend
React + TypeScript + Vite, Tailwind, TanStack Query, Zustand, Monaco (`@monaco-editor/react`), xterm.js, react-router. Layout: collapsible activity bar + side panel + editor tabs + bottom panel (terminal/logs/tests). Mobile/tablet: single-pane stacked navigation with bottom tab bar, Monaco in simplified mode (touch support is limited; documented).
API client generated from OpenAPI (`packages/api-client`).

## 8. Cross-cutting
- **Config:** 12-factor env vars, validated by `pydantic-settings`.
- **Logging:** structured JSON (`structlog`), request IDs, user/project IDs; secrets scrubbed.
- **Errors:** RFC 7807 problem+json; no stack traces to clients.
- **Observability:** Prometheus metrics, OpenTelemetry traces, health/readiness endpoints.
- **Migrations:** Alembic. **Testing:** pytest + httpx, Testcontainers (Postgres/Redis), Playwright e2e, dedicated sandbox-escape test suite.
- **Rate limiting:** Redis token bucket per user/IP; per-user run quotas.
- **Backups:** Postgres PITR, object-storage versioning.

## 9. Repository layout
```
devmaster/
├─ apps/
│  ├─ web/                    # React + TS + Tailwind + Monaco
│  │  └─ src/{app,components,lib,hooks,features/
│  │         {auth,projects,editor,terminal,ai,git,database,tests,deploy,plugins,studios}}
│  ├─ api/                    # FastAPI
│  │  ├─ app/{main.py,core/{config,security,logging,errors,deps},db/{base,session},
│  │  │       modules/{auth,projects,files,git,runs,tests,databases,ai,deployments,plugins,audit},
│  │  │       workers/{celery_app.py}}
│  │  ├─ migrations/          # Alembic
│  │  └─ tests/{unit,integration,security}
│  └─ runner/                 # sandbox service (own Dockerfile, own deploy target)
├─ packages/{plugin-sdk-py,plugin-sdk-ts,api-client}
├─ plugins/{studio-web,studio-api,studio-data,studio-game,studio-mobile,studio-desktop}
├─ runtimes/                  # curated sandbox images
├─ infra/{docker-compose.yml,k8s/,terraform/}
├─ docs/{ARCHITECTURE.md,API_SPEC.md,ROADMAP.md,DEPLOYMENT.md,SECURITY.md}
├─ db/schema.sql              # reference schema (Alembic is source of truth once built)
└─ .github/workflows/ci.yml
```

## 10. Key decisions & trade-offs
- **Modular monolith over microservices:** far less operational load for a small team; boundaries keep extraction possible.
- **Content-addressed file blobs:** dedupe, cheap versioning/snapshots, easy S3 storage.
- **Real `git` for version control** (not a custom reimplementation), run only inside isolated workers.
- **Celery + Redis** now; swappable for Arq/Temporal if workflows grow complex.
- **Managed user DBs separate from the platform DB** so a user SQL console can never reach platform tables.
