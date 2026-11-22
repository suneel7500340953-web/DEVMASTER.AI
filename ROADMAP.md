# DEVMASTER AI — Implementation Roadmap

Rule: a module is **"done" only when** its code exists, automated tests pass in CI, and docs are updated. Anything not meeting that is listed as "in progress". Existing modules are never removed; each stage adds on top.

| # | Module | Deliverables | Definition of done (tests required) |
|---|---|---|---|
| M0 | Foundations | Monorepo, `docker-compose` (postgres, redis, minio, api, worker), CI, Alembic, structured logging, config, `/healthz` | CI green; `docker compose up` boots; health/readiness tests |
| M1 | Core platform | Auth (JWT + refresh rotation), projects, members/RBAC, audit log, files API, React shell + Monaco editor, file tree, responsive layout | Unit + integration (Testcontainers); RBAC matrix tests; Playwright: login → create project → edit/save file on desktop and mobile viewport |
| M2 | Sandbox & execution | Runner service, run profiles, Celery jobs, log streaming (WS), cancel/timeouts, interactive terminal | Integration tests with real containers; **sandbox-escape suite** (network blocked, FS read-only, fork-bomb, memory/CPU limits, metadata IP blocked) |
| M3 | AI assistant | Provider abstraction (OpenAI-compatible + Anthropic), streaming chat, context builder, diff proposals + apply, usage tracking, encrypted keys | Provider adapter tests with recorded fixtures; diff-apply tests; prompt-injection handling tests (files are untrusted input) |
| M4 | Git | init/commit/branch/diff/merge/push/pull, history UI | Tests against local bare repos; conflict handling; credential isolation tests |
| M5 | Testing & debugging | Test run profiles (pytest/jest/vitest), result parsers, test UI, structured log viewer; DAP debugging as stretch | Parser tests with real report fixtures; UI e2e |
| M6 | Database studio | Managed DBs on separate cluster, schema browser, SQL console, history, backup/restore | Isolation tests (cannot reach platform DB); query limit tests |
| M7 | Web studio plugin | Templates (HTML/Tailwind, React/Vite), preview gateway on separate origin | Preview origin isolation test; template build tests |
| M8 | API studio plugin | FastAPI/Express templates, proxied port preview, OpenAPI viewer, HTTP client | Template run tests |
| M9 | Deployments | Targets, build → release pipeline, rollback, status | End-to-end deploy to a local registry/SSH test target |
| M10 | Plugin system hardening | Manifest validation, permissions, SDKs (py/ts), lazy UI panels, third-party plugin example | Permission-boundary tests; sample plugin e2e |
| M11 | Data/ML studio | Jupyter kernels in sandbox, notebook UI, dataset upload | Kernel isolation + resource tests |
| M12 | Game studio | Phaser/Godot-web templates, iframe preview | Template build tests |
| M13 | Mobile studio | React Native/Expo templates, Expo web preview, Android APK build artifacts | Android build in runner; **iOS builds out of scope** (needs macOS runners) |
| M14 | Desktop studio | Electron/Tauri templates, UI preview, Linux packaging (Win/macOS need matching runners) | Packaging tests per supported OS |
| M15 | Production readiness | Helm/Terraform, backups/PITR, observability dashboards, security review, load tests, `DEPLOYMENT.md`, `SECURITY.md` | Load test report; restore drill; external pen-test checklist |

## Suggested order of work
M0 → M1 → M2 → M3 give a usable product (edit, run, and AI-assist in a secure sandbox). Everything after is an additive studio or capability built on those four.

## Known constraints
- Plain Docker is not strong isolation for hostile users; gVisor/Firecracker is required before opening sign-ups publicly.
- Monaco has limited touch/mobile support; mobile layout uses a simplified editing mode.
- GPU workloads, iOS builds, and Windows/macOS packaging need dedicated runner hardware.
