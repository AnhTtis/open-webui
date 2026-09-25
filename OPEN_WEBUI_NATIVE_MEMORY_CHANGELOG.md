# Native memory changelog versus original Open WebUI

> Updated: 2026-09-25  
> Working branch: `claude/native-memory-stability`  
> Base comparison: `origin/main` at `0a7c15832` (`ci: run the external regression suite on release pull requests (#29313)`)  
> Branch starting point: `34791a339` (`Cập nhật cấu trúc native memory và UI`)  
> Working tree: uncommitted hardening on top of `34791a339`. No additional commit or push has been requested.

This document inventories what this checkout changed relative to upstream `origin/main` at `0a7c15832`, then what the post-`34791a339` hardening added. It is the memory-specific companion to [OPEN_WEBUI_NATIVE_ARCHITECTURE_AND_STATUS.md](./OPEN_WEBUI_NATIVE_ARCHITECTURE_AND_STATUS.md) and [OPEN_WEBUI_RELEASE_GATES.md](./OPEN_WEBUI_RELEASE_GATES.md).

## 1. Comparison identity

| Marker | Value |
| ------ | ----- |
| Upstream baseline | `origin/main` `0a7c15832` |
| Native-memory starting commit on this branch | `34791a339` |
| Hardening state | dirty working tree after `34791a339`; not a new commit |
| Product shape | one Open WebUI, many users, `pending` / `user` / `admin`; not shared-database multi-org tenancy |
| Admin contract | metadata and aggregate health only; no raw memory browsing |
| Persistence contract | canonical data on HDD/SSD (SQL + persistent vector). Process RAM is a bounded working set, not zero RAM |

`git diff --stat origin/main` at the time of this writing reported **65 files changed, 10598 insertions, 1388 deletions** for the committed branch, plus the uncommitted hardening files listed in section 3.

## 2. Invariants versus original

Original Open WebUI memory on `origin/main` is a thin per-user content list plus a vector collection. This branch makes SQL the canonical authority and treats the vector index as a rebuildable derivative.

| Topic | Original (`origin/main` `0a7c15832`) | This branch |
| ----- | ------------------------------------ | ----------- |
| Authority | Vector store is effectively the retrieval source; SQL stores content | SQL is canonical for owner, content, status, revision, quota; vector is ranking-only |
| Lifecycle | Insert / update / delete current row | `candidate`, `active`, `superseded`, `archived`, `deleted` plus revisions, evidence, proposals, jobs |
| Isolation | Collection name `user-memory-{user_id}` | SQL owner checks on every read/write; vector hits reconstructed from SQL; generation-scoped vector IDs |
| RAM | Unbounded `.all()` / whole-user loads on several paths | Page / cursor / top-K / batch / byte caps; no routine whole-user or whole-install load |
| Quota | None | Per-user item and UTF-8 byte quota with a `memory_usage` ledger |
| Account deletion | User delete does not leave durable vector cleanup | Independent `memory_account_cleanup` outlives the user row |
| Admin | No memory health surface | Aggregate counts/buckets only; no content, path, IDs, payloads, or raw errors |
| Import/export | Not a first-class backup path | JSON v1 (atomic, bounded) + NDJSON v2 (staging + publish) |
| Jobs | Best-effort async vector upsert | Durable jobs, claim tokens, lease generation, heartbeat, process semaphore |

Canonical data lives on disk in PostgreSQL/SQLite and in a persistent vector backend. The process may hold a page of rows, a top-K of hits, one import/export batch, and embedding/prompt buffers. RAM must not grow with total stored memories.

## 3. File-level inventory

### 3.1 Committed on `34791a339` versus `origin/main`

Native pipeline and Windows/docs work that is not memory-only, but is part of this branch delta:

- `backend/open_webui/utils/context_budget.py`, `context_compaction.py`, `mcp/client.py`, `middleware.py`, `office_preview.py`, `reasoning_parser.py`
- `backend/open_webui/routers/files.py`, `src/lib/apis/files/index.ts`, `src/lib/components/common/FileItemModal.svelte`
- Windows launchers: `backend/setup_windows.bat`, `backend/start_windows.bat`, `start-backend.bat`, `start-dev.bat`, `start-frontend.bat`, `docs/WINDOWS_DEVELOPMENT.md`
- Docs: `OPEN_WEBUI_NATIVE_ARCHITECTURE_AND_STATUS.md`, `OPEN_WEBUI_RELEASE_GATES.md`, `OPEN_WEBUI_LOCAL_DATA_AND_PROTECTION.md`, `OPEN_WEBUI_MODELS_CURRENT.md`, historical Hermes notes
- Tests: `backend/tests/test_context_budget.py`, `test_file_office_routes.py`, `test_mcp_client.py`, `test_office_preview.py`, `test_reasoning_parser.py`, `src/lib/apis/files/index.test.ts`

Memory-specific committed files:

- `backend/open_webui/migrations/versions/e7a9c4d2b611_add_durable_memory_lifecycle.py`
- `backend/open_webui/models/memories.py`
- `backend/open_webui/routers/memories.py`
- `backend/open_webui/utils/memory.py`, `utils/memory_jobs.py`
- `backend/open_webui/tools/builtin.py`
- `src/lib/apis/memories/index.ts`, `src/lib/apis/memories/index.test.ts`
- `src/lib/components/chat/Settings/Personalization.svelte`
- `src/lib/components/chat/Settings/Personalization/MemoryModal.svelte`
- `backend/tests/test_memory_lifecycle.py`, `test_memory_routes.py`, `test_memory_validation.py`

### 3.2 Uncommitted hardening after `34791a339`

New:

- `backend/open_webui/migrations/versions/f8c2a91d4e73_add_memory_integrity_lifecycle.py`
- `backend/open_webui/migrations/versions/a9d3b70e5c14_add_memory_transfer_staging.py`
- `backend/open_webui/services/__init__.py`
- `backend/open_webui/services/account_lifecycle.py`
- `backend/open_webui/utils/memory_limits.py`
- `backend/tests/test_memory_hardening.py`
- `src/lib/components/admin/Settings/Memory.svelte`
- `OPEN_WEBUI_NATIVE_MEMORY_CHANGELOG.md` (this file)

Modified:

- `.env.example`
- `backend/open_webui/config.py`, `env.py`, `internal/db.py`
- `backend/open_webui/models/{memories,users,auths,groups,chats,oauth_sessions}.py`
- `backend/open_webui/routers/{memories,users,scim,auths}.py`
- `backend/open_webui/utils/{memory,memory_jobs}.py`
- `backend/open_webui/tools/builtin.py`
- `src/lib/apis/memories/index.ts`, `src/lib/apis/memories/index.test.ts`
- `src/lib/components/chat/Settings/Personalization.svelte`
- `src/lib/components/chat/SettingsModal.svelte`
- `README.md`, `OPEN_WEBUI_NATIVE_ARCHITECTURE_AND_STATUS.md`, `OPEN_WEBUI_RELEASE_GATES.md`

## 4. Schema and migrations

Do not rewrite `e7a9c4d2b611`. Follow-up order:

1. `e7a9c4d2b611_add_durable_memory_lifecycle.py` — lifecycle tables (already on the branch).
2. `f8c2a91d4e73_add_memory_integrity_lifecycle.py` — `memory_usage`, `memory_vector_generation`, `memory_vector_manifest`, `memory_account_cleanup` (no FK to `user`); `memory.content_bytes`, `session_id`, `chat_id`; proposal `idempotency_key`; job fencing columns; partial unique live hash; duplicate preflight fails with counts only.
3. `a9d3b70e5c14_add_memory_transfer_staging.py` — `memory_transfer`, `memory_transfer_record`, `memory_transfer_id_map`.

Preflight: duplicate live `(user_id, normalized_hash)` in `active`/`candidate` fails the integrity migration with counts and a remediation query. Private content is not printed.

Partial unique index:

```sql
UNIQUE (user_id, normalized_hash)
WHERE status IN ('active','candidate') AND normalized_hash IS NOT NULL
```

`memory.user_id -> user.id ON DELETE CASCADE`. Ordinary `memory_job.user_id` keeps its FK. Account cleanup uses a separate table so vector collection deletion can run after the user row is gone.

Rollback: reverse `a9d3b70e5c14` then `f8c2a91d4e73` then `e7a9c4d2b611` only on a database that can lose native-memory state. Production roll-forward is the supported path.

## 5. Runtime database and RAM

SQLite:

- `PRAGMA foreign_keys=ON` on every sync, async, and SQLCipher connection (`DATABASE_SQLITE_PRAGMA_FOREIGN_KEYS`, default `ON`).
- Disk-first defaults: smaller async pool (`DATABASE_SQLITE_ASYNC_POOL_SIZE`, default 32), `temp_store=FILE`, `mmap_size=0`, reduced cache (`cache_size=-8192`). Operators may override.
- SQLite is a **single-process** compatibility target. Multi-process / multi-node memory workers require PostgreSQL. The job worker warns or refuses an unsafe SQLite multi-worker layout.

Production target: PostgreSQL 16 plus persistent pgvector or on-disk Qdrant. SQLite is not the authority for multi-user retention.

Working-set caps (see `backend/open_webui/utils/memory_limits.py`):

- Page default 20, max 100.
- Prompt injection: row and UTF-8 byte caps.
- Extraction snapshot: row and cumulative byte caps.
- JSON v1 import: 10 MiB and 5,000 memories.
- Tool `list_memories`: default 20, max 50.
- Job concurrency: process semaphore `MEMORY_JOB_CONCURRENCY` (default 4).

This is not zero RAM. DB page cache, vector-engine cache, embedding buffers, and one page/batch always exist.

## 6. Quota, transactions, and API

Admin-configurable (env + admin config):

- `MEMORIES_MAX_ITEMS_PER_USER` → `memories.max_items_per_user` (default 5000)
- `MEMORIES_MAX_CONTENT_BYTES_PER_USER` → `memories.max_content_bytes_per_user` (default 5 MiB)

Service layer enforces quota on add, tools, proposal approval, restore, and both import formats. `memory_usage` is reserved under a row lock / conditional update. Soft delete releases quota; restore reserves again; replace uses a byte delta. Duplicate live hashes are a no-op.

Transaction rule: public methods commit only when they own the session; `*_tx` helpers flush. Nested savepoints catch `IntegrityError` for live-hash races.

HTTP mapping:

- `MemoryRequestTooLarge` → 413
- `MemoryQuotaExceeded` → 409 `{code: memory_quota_exceeded, counted_items, max_items, ...}`
- `MemoryConflictError` → 409

Static routes (`/page`, `/summary`, `/admin/health`, `/export/ndjson`) are declared before `/{memory_id}`.

`GET /memories/` remains a list for compatibility but is now paginated with a default/max cap. `GET /memories/page` returns `{items,total,next_cursor,skip,limit}`. `GET /memories/summary` is the per-user quota + sync counts used by Memory Center. `GET /memories/admin/health` is admin-only and metadata-only.

`/query` preserves rank/score but reconstructs documents from SQL. Tools no longer side-effect vector deletes.

Access: `user_can_use_memories(user)` requires `memories.enable` and (admin or `features.memories`). Client `features.memory` is opt-in only, not authorization.

Prompt memory is untrusted data, not instructions.

## 7. Vector fencing and workers

- Embedding fingerprint = SHA-256 of `engine|model|prefix|schema=mem-v2` (32 hex chars). API keys are not included.
- Vector document IDs: `mem:{generation}:{memory_id}:r{revision}`.
- Upsert/delete jobs re-read SQL by `(job.user_id, job.memory_id)` and check claim token, generation, revision, and recallable status before and after the external vector call.
- Reindex creates a building generation, dual-writes, verifies, then atomically activates and retires the previous generation.
- Claims: PostgreSQL `FOR UPDATE SKIP LOCKED`; SQLite uses a process-local lock. `complete` / `fail` / heartbeat require matching `claim_token` and `lease_generation`.
- Process-wide semaphore + task registry; overall `MEMORY_JOB_TIMEOUT_SECONDS`.

## 8. Account deletion (admin and SCIM)

`backend/open_webui/services/account_lifecycle.py` is the single path for admin, SCIM, and compatibility wrappers in `models/users.py` / `models/auths.py`.

Covered in the deletion transaction:

- primary-admin protection and optional self-delete forbid
- SCIM ownership check when required
- revoke JWT / API keys / OAuth sessions **before** SQL delete
- insert `memory_account_cleanup` (no FK to user)
- dead-letter ordinary memory jobs
- remove group membership and chats via no-commit TX helpers
- delete `auth` and `user`; FK cascade purges canonical memory rows
- publish `USER_DELETED` only after commit

Cleanup worker deletes legacy `user-memory-{user_id}` and generation collections even after the user row is gone.

Not claimed as complete account erasure. Other user-owned resources (files, knowledge, prompts, notes, channels, and similar) remain outside this inventory until they are explicitly wired through the same service.

## 9. Import / export

JSON v1: bounded read, validate entire bundle, strip source tenant IDs, remap IDs, apply in one canonical transaction. Late failure rolls back. Importer restores the canonical arrays the v1 exporter emits (memories plus related revision/evidence/profile/session records). Jobs and vector derivatives are not imported; new vector jobs are enqueued from imported current state.

NDJSON v2: typed manifest / memory / footer records, SHA-256 of prior lines, `StreamingResponse` export, chunked `UploadFile` import into staging tables, validate, then `publish_transfer`. Dry-run parses and validates without publish.

## 10. Frontend

- `MemoryApiError` carries `status`, optional `code`, optional `context`. `parseMemoryApiError` accepts string or object `detail`.
- Page / summary / admin health / NDJSON export-import / `importMemoryFile` routing by `.ndjson`.
- Memory Center uses server `/page` + `/summary`, shared `Pagination.svelte` (1-based), quota display, NDJSON export, `.json`/`.ndjson` import, operation labels for rebuild/import/clear.
- Admin Settings → Memory: quota config and aggregate health only. No user or memory drill-down.

## 11. Tests actually run

This environment has system Python 3.11 without pip, pytest, SQLAlchemy, aiosqlite, Node, or npm. Focused hardening tests were **written but not executed** here:

```text
# intended, not run
cd backend && python -m pytest tests/test_memory_hardening.py tests/test_memory_lifecycle.py tests/test_memory_routes.py tests/test_memory_validation.py
npx vitest run src/lib/apis/memories/index.test.ts
```

Historical focused results from 2026-09-11 (pre-hardening) remain in [OPEN_WEBUI_NATIVE_ARCHITECTURE_AND_STATUS.md](./OPEN_WEBUI_NATIVE_ARCHITECTURE_AND_STATUS.md) and are not re-claimed for this working tree.

See [OPEN_WEBUI_RELEASE_GATES.md](./OPEN_WEBUI_RELEASE_GATES.md) for operational checks that cannot be produced here: live pgvector/Qdrant, multi-worker PostgreSQL, crash during vector/import, long-horizon load, backup/PITR, real SCIM provider deletion, Alembic upgrade through the follow-up migrations.

PostgreSQL 16 concurrency tests are opt-in and were **not** run.

## 12. Compatibility notes

- Existing `/memories/` list shape remains but is capped.
- JSON v1 export/import remains; NDJSON v2 is the scalable path.
- `features.memory` client flag still does not grant access.
- SQLite deployments must stay single-process for durable memory jobs.
- Embedding fingerprint mismatch falls back to bounded SQL until a new generation is active.
