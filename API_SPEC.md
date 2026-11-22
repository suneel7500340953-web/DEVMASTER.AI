# DEVMASTER AI — API Specification (v1, draft)

Status: **design only.** The OpenAPI document will be generated from the FastAPI code and replace this file as the contract.

## Conventions
- Base path `/api/v1`, JSON, UTF-8. Timestamps ISO-8601 UTC. IDs are UUIDs.
- Auth: `Authorization: Bearer <access_jwt>` or `Bearer dm_pat_<api_token>`. Refresh token is an httpOnly, SameSite=Strict cookie.
- Permissions: `V` viewer, `E` editor, `A` admin, `O` owner of the project; `—` = any authenticated user; `public` = no auth.
- Errors: RFC 7807 `application/problem+json` (`type`, `title`, `status`, `detail`, `request_id`).
- Pagination: cursor-based (`?limit=50&cursor=...`) → `{items, next_cursor}`.
- Idempotency: `Idempotency-Key` header on POSTs that start runs/deploys.
- Rate limits: `429` with `Retry-After`; per-user and per-IP.
- Realtime: WebSocket `/api/v1/ws/...` (auth via short-lived ticket from `POST /auth/ws-ticket`).

## Auth
| Method & path | Perm | Purpose |
|---|---|---|
| POST `/auth/register` | public | Create account |
| POST `/auth/login` | public | Access token + refresh cookie |
| POST `/auth/refresh` | cookie | Rotate refresh token |
| POST `/auth/logout` | — | Revoke token family |
| GET `/auth/me` · PATCH `/auth/me` | — | Profile |
| POST `/auth/ws-ticket` | — | One-time WebSocket ticket |
| GET/POST/DELETE `/auth/api-tokens` | — | Personal access tokens |

## Projects & members
| Method & path | Perm | Purpose |
|---|---|---|
| GET `/projects` · POST `/projects` | — | List / create (optional `template_key`) |
| GET/PATCH/DELETE `/projects/{id}` | V / A / O | Read, update, archive |
| GET `/templates?studio=` | — | Templates from enabled studio plugins |
| GET/POST `/projects/{id}/members` | V / A | List, invite |
| PATCH/DELETE `/projects/{id}/members/{user_id}` | A | Change role, remove |
| GET/PUT/DELETE `/projects/{id}/secrets/{key}` | A | Write-only secrets (values never returned) |

## Files
| Method & path | Perm | Purpose |
|---|---|---|
| GET `/projects/{id}/files/tree` | V | Tree listing |
| GET `/projects/{id}/files?path=` | V | Read file (ETag) |
| PUT `/projects/{id}/files?path=` | E | Write file (`If-Match` for conflict detection) |
| POST `/projects/{id}/files/move` · DELETE `?path=` | E | Rename/move, delete |
| GET `/projects/{id}/files/{file_id}/versions` | V | History |
| GET `/projects/{id}/search?q=` | V | Content search |
| GET `/projects/{id}/export.zip` · POST `/projects/{id}/import` | V / E | ZIP export, import |

## Git
| Method & path | Perm | Purpose |
|---|---|---|
| GET `/projects/{id}/git/status` · `/log` · `/branches` · `/diff` | V | Read state |
| POST `/projects/{id}/git/init` · `/commit` · `/checkout` · `/branch` · `/merge` | E | Local operations (job) |
| POST `/projects/{id}/git/push` · `/pull` | E | Remote sync (job, uses secret) |

## Runs, tests, terminal
| Method & path | Perm | Purpose |
|---|---|---|
| GET `/projects/{id}/run-profiles` | V | Profiles from studio plugins |
| POST `/projects/{id}/runs` | E | Start `{kind, profile, args}` → `202 {run_id}` |
| GET `/projects/{id}/runs` · `/runs/{run_id}` | V | History / details |
| POST `/runs/{run_id}/cancel` | E | Cancel |
| WS `/ws/runs/{run_id}/logs` | V | Live log stream (replays from start) |
| GET `/runs/{run_id}/test-results` | V | Parsed test results |
| GET `/runs/{run_id}/artifacts` | V | Signed download URLs |
| POST `/projects/{id}/terminal` | E | Open session → `{session_id}` |
| WS `/ws/terminal/{session_id}` | E | PTY stream (stdin/stdout/resize) |
| GET `/projects/{id}/preview` | V | Signed preview URL |

## Database studio
| Method & path | Perm | Purpose |
|---|---|---|
| GET/POST `/projects/{id}/databases` | V / A | List, provision/connect |
| GET `/databases/{db_id}/schema` | V | Tables, columns, FKs |
| POST `/databases/{db_id}/query` | E | Execute SQL (timeout, row cap, read-only toggle) |
| GET `/databases/{db_id}/history` | V | Query history |
| POST `/databases/{db_id}/backup` · `/restore` | A | Jobs |

## AI assistant
| Method & path | Perm | Purpose |
|---|---|---|
| GET `/ai/providers` | — | Enabled providers & models |
| PUT `/ai/credentials/{provider}` | — | Store own API key (encrypted) |
| GET/POST `/projects/{id}/ai/conversations` | V / E | List, create |
| GET `/ai/conversations/{cid}/messages` | V | History |
| POST `/ai/conversations/{cid}/messages` | E | Send; response is SSE stream; may include `file_changes` proposals |
| POST `/ai/conversations/{cid}/apply` | E | Apply accepted diffs (writes files, creates versions with `source=ai`) |
| GET `/ai/usage` | — | Token usage |

## Deployments
| Method & path | Perm | Purpose |
|---|---|---|
| GET/POST `/projects/{id}/deploy-targets` | A | Manage targets |
| POST `/projects/{id}/deployments` | A | Start deployment (job) |
| GET `/projects/{id}/deployments` · `/deployments/{id}` | V | Status |
| POST `/deployments/{id}/rollback` | A | Roll back |

## Plugins
| Method & path | Perm | Purpose |
|---|---|---|
| GET `/plugins` | — | Installed plugins |
| POST `/plugins` · PATCH/DELETE `/plugins/{key}` | superuser | Install, enable, remove |
| PUT/DELETE `/projects/{id}/plugins/{key}` | A | Enable per project |

## Audit & system
| Method & path | Perm | Purpose |
|---|---|---|
| GET `/projects/{id}/audit` | A | Project audit trail |
| GET `/admin/audit` | superuser | Global audit |
| GET `/healthz` · `/readyz` · `/metrics` | public / internal | Liveness, readiness, Prometheus |

## Runner (internal, not exposed publicly)
`POST /jobs` (signed job token, mTLS) · `DELETE /jobs/{id}` · `GET /jobs/{id}/logs` · `WS /sessions/{id}`. Accepts only jobs signed by the API; has no DB access.
