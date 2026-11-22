-- DEVMASTER AI reference schema (PostgreSQL 15+). Design only: not yet applied or tested.
-- Once Module 1 exists, Alembic migrations become the source of truth.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TYPE project_role   AS ENUM ('owner','admin','editor','viewer');
CREATE TYPE studio_type    AS ENUM ('web','api','database','data_ml','game','mobile','desktop','generic');
CREATE TYPE run_kind       AS ENUM ('execute','test','build','lint','deploy');
CREATE TYPE run_status     AS ENUM ('queued','running','succeeded','failed','timeout','cancelled');
CREATE TYPE deploy_status  AS ENUM ('queued','building','releasing','live','failed','rolled_back');
CREATE TYPE db_engine      AS ENUM ('postgres','sqlite');
CREATE TYPE ai_role        AS ENUM ('system','user','assistant','tool');

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END; $$ LANGUAGE plpgsql;

-- ───────── Auth ─────────
CREATE TABLE users (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email         citext NOT NULL UNIQUE,
  password_hash text,                       -- argon2id; NULL for SSO-only users
  display_name  text NOT NULL,
  is_active     boolean NOT NULL DEFAULT true,
  is_superuser  boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  last_login_at timestamptz
);
CREATE TRIGGER trg_users_upd BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE refresh_tokens (               -- rotating; reuse of a revoked token revokes the whole family
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  family_id   uuid NOT NULL,
  token_hash  bytea NOT NULL UNIQUE,
  user_agent  text, ip inet,
  expires_at  timestamptz NOT NULL,
  revoked_at  timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON refresh_tokens(user_id);
CREATE INDEX ON refresh_tokens(family_id);

CREATE TABLE api_tokens (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name         text NOT NULL,
  token_hash   bytea NOT NULL UNIQUE,
  scopes       text[] NOT NULL DEFAULT '{}',
  expires_at   timestamptz,
  last_used_at timestamptz,
  revoked_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- ───────── Projects & permissions ─────────
CREATE TABLE projects (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id    uuid NOT NULL REFERENCES users(id),
  name        text NOT NULL,
  slug        text NOT NULL,
  studio      studio_type NOT NULL DEFAULT 'generic',
  description text,
  settings    jsonb NOT NULL DEFAULT '{}',
  quota_bytes bigint NOT NULL DEFAULT 1073741824,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  archived_at timestamptz,
  UNIQUE (owner_id, slug)
);
CREATE TRIGGER trg_projects_upd BEFORE UPDATE ON projects FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE project_members (
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role       project_role NOT NULL,
  added_by   uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, user_id)
);
CREATE INDEX ON project_members(user_id);

CREATE TABLE project_secrets (              -- env vars; value encrypted with envelope encryption (KMS)
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  key        text NOT NULL,
  ciphertext bytea NOT NULL,
  key_id     text NOT NULL,
  created_by uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, key)
);

-- ───────── Files (content-addressed) ─────────
CREATE TABLE blobs (
  hash           bytea PRIMARY KEY,         -- sha256
  size_bytes     bigint NOT NULL,
  storage_key    text,                      -- S3/MinIO key when large
  inline_content bytea,                     -- small files stored inline (<= 64 KB)
  created_at     timestamptz NOT NULL DEFAULT now(),
  CHECK (storage_key IS NOT NULL OR inline_content IS NOT NULL)
);

CREATE TABLE files (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id  uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  path        text NOT NULL,                -- normalized, no '..', no leading '/'
  is_dir      boolean NOT NULL DEFAULT false,
  blob_hash   bytea REFERENCES blobs(hash),
  mime        text,
  updated_by  uuid REFERENCES users(id),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  deleted_at  timestamptz,
  CHECK (path !~ '(^|/)\.\.(/|$)' AND path !~ '^/'),
  CHECK (is_dir OR blob_hash IS NOT NULL)
);
CREATE UNIQUE INDEX files_live_path ON files(project_id, path) WHERE deleted_at IS NULL;

CREATE TABLE file_versions (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  file_id    uuid NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  blob_hash  bytea NOT NULL REFERENCES blobs(hash),
  created_by uuid REFERENCES users(id),
  source     text NOT NULL DEFAULT 'user',  -- user | ai | git | plugin
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON file_versions(file_id, created_at DESC);

-- ───────── Git (data lives in real git repos; DB holds metadata only) ─────────
CREATE TABLE git_repos (
  project_id     uuid PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  default_branch text NOT NULL DEFAULT 'main',
  remote_url     text,
  credential_id  uuid,                      -- -> project_secrets.id
  storage_key    text NOT NULL,             -- location of the repo volume/bundle
  last_synced_at timestamptz
);

-- ───────── Execution, tests, terminal ─────────
CREATE TABLE runs (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id   uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  user_id      uuid NOT NULL REFERENCES users(id),
  kind         run_kind NOT NULL,
  profile      text NOT NULL,               -- run profile key from a studio plugin
  image        text NOT NULL,               -- pinned by digest
  command      text[] NOT NULL,
  status       run_status NOT NULL DEFAULT 'queued',
  exit_code    int,
  limits       jsonb NOT NULL,              -- cpu, memory, pids, timeout_s, network
  celery_id    text,
  log_key      text,                        -- object-storage key of full log
  commit_sha   text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  started_at   timestamptz,
  finished_at  timestamptz
);
CREATE INDEX ON runs(project_id, created_at DESC);
CREATE INDEX ON runs(status) WHERE status IN ('queued','running');

CREATE TABLE test_results (
  id          bigserial PRIMARY KEY,
  run_id      uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  suite       text, name text NOT NULL,
  status      text NOT NULL CHECK (status IN ('passed','failed','skipped','error')),
  duration_ms int, message text, file_path text, line int
);
CREATE INDEX ON test_results(run_id);

CREATE TABLE terminal_sessions (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id  uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  user_id     uuid NOT NULL REFERENCES users(id),
  runner_ref  text NOT NULL,
  opened_at   timestamptz NOT NULL DEFAULT now(),
  closed_at   timestamptz
);

-- ───────── Database studio (user DBs live on a SEPARATE cluster) ─────────
CREATE TABLE project_databases (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id   uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name         text NOT NULL,
  engine       db_engine NOT NULL,
  mode         text NOT NULL CHECK (mode IN ('managed','external')),
  secret_id    uuid REFERENCES project_secrets(id),
  status       text NOT NULL DEFAULT 'provisioning',
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, name)
);

CREATE TABLE query_history (
  id          bigserial PRIMARY KEY,
  database_id uuid NOT NULL REFERENCES project_databases(id) ON DELETE CASCADE,
  user_id     uuid NOT NULL REFERENCES users(id),
  sql         text NOT NULL,
  status      text NOT NULL,
  duration_ms int, row_count int, error text,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- ───────── AI ─────────
CREATE TABLE llm_providers (
  id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  key       text NOT NULL UNIQUE,           -- 'groq','openai','anthropic','ollama'
  kind      text NOT NULL,                  -- 'openai_compatible' | 'anthropic' | ...
  base_url  text,
  models    jsonb NOT NULL DEFAULT '[]',
  enabled   boolean NOT NULL DEFAULT true
);

CREATE TABLE llm_credentials (               -- platform-wide (user_id NULL) or bring-your-own-key
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  provider_id uuid NOT NULL REFERENCES llm_providers(id),
  user_id     uuid REFERENCES users(id) ON DELETE CASCADE,
  ciphertext  bytea NOT NULL,
  key_id      text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ai_conversations (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  user_id    uuid NOT NULL REFERENCES users(id),
  title      text NOT NULL DEFAULT 'New chat',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON ai_conversations(project_id, user_id, updated_at DESC);

CREATE TABLE ai_messages (
  id              bigserial PRIMARY KEY,
  conversation_id uuid NOT NULL REFERENCES ai_conversations(id) ON DELETE CASCADE,
  role            ai_role NOT NULL,
  content         text NOT NULL,
  provider_key    text, model text,
  tokens_in       int, tokens_out int,
  file_changes    jsonb,                    -- proposed/applied diffs
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON ai_messages(conversation_id, id);

-- ───────── Deployments ─────────
CREATE TABLE deploy_targets (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name       text NOT NULL,
  kind       text NOT NULL,                 -- 'docker_registry' | 'ssh' | 'static_host' | ...
  config     jsonb NOT NULL DEFAULT '{}',
  secret_id  uuid REFERENCES project_secrets(id),
  UNIQUE (project_id, name)
);

CREATE TABLE deployments (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id  uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  target_id   uuid NOT NULL REFERENCES deploy_targets(id),
  run_id      uuid REFERENCES runs(id),
  commit_sha  text,
  status      deploy_status NOT NULL DEFAULT 'queued',
  url         text,
  created_by  uuid NOT NULL REFERENCES users(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);
CREATE INDEX ON deployments(project_id, created_at DESC);

-- ───────── Plugins ─────────
CREATE TABLE plugins (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  key          text NOT NULL UNIQUE,
  name         text NOT NULL,
  version      text NOT NULL,
  manifest     jsonb NOT NULL,
  permissions  text[] NOT NULL DEFAULT '{}',
  enabled      boolean NOT NULL DEFAULT true,
  installed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE project_plugins (
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  plugin_id  uuid NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
  config     jsonb NOT NULL DEFAULT '{}',
  PRIMARY KEY (project_id, plugin_id)
);

-- ───────── Audit (append-only) ─────────
CREATE TABLE audit_log (
  id            bigserial PRIMARY KEY,
  ts            timestamptz NOT NULL DEFAULT now(),
  actor_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  project_id    uuid REFERENCES projects(id) ON DELETE SET NULL,
  action        text NOT NULL,              -- e.g. 'project.create','file.update','run.start'
  entity_type   text, entity_id text,
  ip            inet, request_id text,
  metadata      jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX ON audit_log(project_id, ts DESC);
CREATE INDEX ON audit_log(actor_user_id, ts DESC);

CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'audit_log is append-only'; END; $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_audit_no_change BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();
