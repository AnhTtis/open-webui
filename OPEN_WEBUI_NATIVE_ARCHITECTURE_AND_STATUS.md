# Open WebUI native architecture and implementation status

> Updated: 2026-09-25  
> Working branch: `claude/native-memory-stability`  
> Scope: native Open WebUI plus durable per-user memory hardening

This is the canonical architecture and implementation-status document for the native Open WebUI work. For the file-level delta versus `origin/main` at `0a7c15832`, see [OPEN_WEBUI_NATIVE_MEMORY_CHANGELOG.md](./OPEN_WEBUI_NATIVE_MEMORY_CHANGELOG.md). For local data protection, see [OPEN_WEBUI_LOCAL_DATA_AND_PROTECTION.md](./OPEN_WEBUI_LOCAL_DATA_AND_PROTECTION.md). For unfinished production validation, see [OPEN_WEBUI_RELEASE_GATES.md](./OPEN_WEBUI_RELEASE_GATES.md).

## 1. Architecture decision

Open WebUI uses one native request pipeline:

```text
Browser
  -> Open WebUI authentication and permissions
  -> native request/query preprocessing
  -> native Skills, MCP, memory, files and RAG
  -> context assembly and compaction
  -> final provider-context budget guard
  -> configured model provider
  -> native streaming, persistence and UI
```

Embedded Hermes Harness is retired from Open WebUI. Open WebUI does not delegate its system prompt, model loop, tool loop, SSE parsing, session database or persistence to Hermes.

The following retired paths have been removed from the checkout:

- `backend/open_webui/hermes/`
- `backend/open_webui/integrations/`
- `backend/.svelte-kit/`

`D:\ClaudeExperience\Memory\hermes-agent` is a separate read-only reference repository. It must not be modified, deleted, imported as an Open WebUI runtime dependency or used as a rollback path.

## 2. Runtime ownership

| Capability or data                                          | Authority                                         |
| ----------------------------------------------------------- | ------------------------------------------------- |
| Authentication, users, groups and permissions               | Open WebUI                                        |
| Chat messages, streaming and assistant output               | Native Open WebUI pipeline                        |
| Skills, functions, tools and MCP                            | Native Open WebUI                                 |
| Canonical memory and lifecycle history                      | Open WebUI SQL database                           |
| Embeddings, vector index and generated search/profile views | Derivative; rebuildable from canonical SQL        |
| Uploaded Office binary                                      | Canonical object in configured Open WebUI storage |
| Office PDF preview                                          | Temporary derivative generated on demand          |

## 3. Startup and dependency behavior

Daily startup no longer downloads or installs optional assets:

- `npm run dev` starts Vite directly.
- `npm run pyodide:prepare` is an explicit idempotent preparation step.
- `npm run build` prepares Pyodide before the Vite production build.
- `backend/start_windows.bat` starts Uvicorn without installing Playwright or NLTK.
- `backend/setup_windows.bat` installs optional Playwright Chromium and NLTK `punkt_tab` only when explicitly run for the Playwright web-loader configuration.
- Root Windows launchers validate prerequisites, propagate exit codes and use readiness checks.

Canonical Windows setup and run commands are in [docs/WINDOWS_DEVELOPMENT.md](./docs/WINDOWS_DEVELOPMENT.md).

## 4. Query, context and reasoning

### Query/context pipeline

The native middleware normalizes messages and applies a final budget checkpoint after plugins and retrieval have assembled the request:

```python
form_data = normalize_messages_for_model(form_data)
form_data = enforce_final_context_budget(form_data, model, metadata)
```

`backend/open_webui/utils/context_budget.py` estimates request size, resolves the model context limit, reserves completion capacity, preserves system/latest-user content and drops older complete records when necessary. It writes context-budget metadata for diagnostics. Provider tokenization remains authoritative; the local estimator is a conservative guard.

Native compaction remains in `backend/open_webui/utils/context_compaction.py`. Provider-private reasoning is not accepted as a canonical conversation summary or durable user memory.

### Reasoning normalization

`backend/open_webui/utils/reasoning_parser.py` provides incremental state for provider-native reasoning and legacy reasoning tags. It preserves partial markers across chunks, emits sanitized deltas and marks interrupted or unclosed reasoning as incomplete rather than completed.

Recognized inputs include Ollama `thinking`, `reasoning_content`, `reasoning`, provider `reasoning_details`, Responses API reasoning events and configured legacy tags.

## 5. Skills and MCP

Native Open WebUI Skills and permissions remain active. No Hermes manifest or Hermes system prompt is injected into ordinary requests.

`backend/open_webui/utils/mcp/client.py` keeps MCP clients request-local and applies bounded AnyIO timeouts to tool/resource discovery and execution. Cleanup occurs in the request task. MCP failures should become explicit errors rather than indefinite chat spinners.

## 6. Durable per-user memory

### Canonical schema

Canonical memory lives on disk in SQL (PostgreSQL production / SQLite single-process compatibility) and a persistent vector backend. Application RAM is a bounded working set (page, top-K, batch, byte caps), not a full-user or full-install load, and not zero RAM.

Migration `backend/open_webui/migrations/versions/e7a9c4d2b611_add_durable_memory_lifecycle.py` extends the existing `memory` table and creates:

- `memory_revision`
- `memory_evidence`
- `memory_relation`
- `agent_profile`
- `agent_profile_revision`
- `session_memory_state`
- `memory_proposal`
- `memory_job`
- `memory_audit_event`

Follow-up migrations (do not rewrite `e7a9c4d2b611`):

- `f8c2a91d4e73_add_memory_integrity_lifecycle.py` — quota ledger, vector generation/manifest, independent account-cleanup, live-hash unique index, job fencing, `content_bytes` / session / chat scope columns
- `a9d3b70e5c14_add_memory_transfer_staging.py` — NDJSON v2 staging tables

The current `memory` row stores scope (`session`, `working`, `long_term`), kind, structured value, normalized hash, lifecycle status, confidence/trust/importance, validity and expiry, recall counters, revision/version state and vector sync state.

### Safety and lifecycle behavior

- Every canonical query/mutation is user-scoped.
- Revisions and evidence are append-only records.
- Ordinary delete is a tombstone and can be restored.
- Updates use optimistic `version` checks to prevent lost updates.
- Proposal review uses a conditional `UPDATE ... WHERE status = 'pending'` claim.
- Automatic extraction creates proposals instead of destructively replacing canonical memory.
- Canonical mutation, audit/revision/evidence and vector-sync jobs share the SQL transaction.
- Durable jobs have idempotency keys, leases, retry/backoff, stale-lease recovery and dead-letter state.
- `asyncio.create_task` may dispatch already-claimed work, but SQL is the durability boundary.
- Vector hits are validated against current SQL ownership/status before prompt injection.
- Injected memory is marked as untrusted data, not instruction text.

### Memory Center

The Personalization UI now supports search/filter/pagination, active/candidate/archived/deleted views, proposal approval/dismissal, revision history, restore, optimistic-conflict feedback, learning pause/resume, export, import dry-run and explicit import confirmation.

Import currently restores validated canonical rows without overwriting duplicates. JSON v1 is atomic and bounded; NDJSON v2 stages then publishes. Full historical backup restore on PostgreSQL with live vector backends remains a release gate.

Administrators see quota config and aggregate health only. They do not browse memory content, paths, or user identifiers.

## 7. Office document preview and download

Supported server-preview inputs are DOCX, XLS and XLSX.

`backend/open_webui/utils/office_preview.py`:

- validates extension, MIME, ZIP/OLE signature and maximum size;
- resolves `soffice` lazily so missing LibreOffice cannot block startup;
- uses `asyncio.create_subprocess_exec` without a shell;
- creates isolated temporary input/output/profile directories;
- enforces timeout, kill/reap and cleanup;
- validates PDF signature and output size.

`GET /api/v1/files/{id}/preview` checks access before reading storage and returns a private, no-store, `nosniff` PDF response when conversion succeeds. The UI falls back to browser DOCX or spreadsheet rendering when server conversion is unavailable.

The original Office object is always canonical. Download preserves original bytes, MIME and Unicode filename using an ASCII fallback plus RFC 5987 `filename*`. The preview PDF never replaces the original binary.

## 8. Source and tests that must remain

Required feature files include:

- `backend/open_webui/migrations/versions/e7a9c4d2b611_add_durable_memory_lifecycle.py`
- `backend/open_webui/migrations/versions/f8c2a91d4e73_add_memory_integrity_lifecycle.py`
- `backend/open_webui/migrations/versions/a9d3b70e5c14_add_memory_transfer_staging.py`
- `backend/open_webui/services/account_lifecycle.py`
- `backend/open_webui/utils/memory_limits.py`
- `backend/open_webui/utils/context_budget.py`
- `backend/open_webui/utils/memory_jobs.py`
- `backend/open_webui/utils/office_preview.py`
- `backend/open_webui/utils/reasoning_parser.py`
- memory/file routers, models and middleware changes
- `backend/tests/test_memory_*.py`
- `src/lib/components/admin/Settings/Memory.svelte`
- `backend/tests/test_context_budget.py`
- `backend/tests/test_reasoning_parser.py`
- `backend/tests/test_mcp_client.py`
- `backend/tests/test_office_preview.py`
- `backend/tests/test_file_office_routes.py`
- `src/lib/apis/files/index.test.ts`
- `src/lib/apis/memories/index.test.ts`
- `backend/setup_windows.bat`
- `start-backend.bat`, `start-frontend.bat`, `start-dev.bat`

Do not treat untracked migration, utility, test or launcher files as disposable cleanup artifacts.

## 9. Verification completed

Focused verification completed before this document was updated:

- SQLite Alembic upgrade/downgrade/upgrade and legacy-memory backfill checks.
- Memory lifecycle, optimistic concurrency, proposal claim, export/import and route tests.
- Context-budget, reasoning parser, MCP timeout and Office conversion/route tests.
- Combined backend pytest (2026-09-11 native-pipeline checkpoint): **43 tests passed, 333 subtests passed**.
- Focused frontend Vitest (2026-09-11): **2 files, 10 tests passed**.
- 2026-09-25 hardening tests (`backend/tests/test_memory_hardening.py` and extended `src/lib/apis/memories/index.test.ts`) were **not run** in this environment (no pytest/aiosqlite/Node). They are a release gate, not a pass.
- Focused ESLint passes for the changed files API, Memory Center and Office preview files; IDE diagnostics are empty in that focused scope.
- Office route tests verify denied users receive 404 before storage read and canonical XLSX bytes retain the same SHA-256.
- Backend compile, focused Black/Prettier and `git diff --check` passed at the recorded checkpoint.
- `package.json` parses and `uv lock --check` passes.
- Windows launchers reject the currently installed unsupported Node 24 before starting services; Node 22 build certification remains a release gate.
- Backend isolated SQLite startup reached `/health` with HTTP 200 after using the synchronous `sqlite:///...` migration URL.
- Frontend Vite startup reached HTTP 200 without dependency preparation in `npm run dev`.
- Targeted native-source search found no remaining `hermes_harness`, `open_webui.hermes`, `open_webui.integrations` or Harness-specific `execution_mode` references.

This is source/focused-test completion, not production certification. The remaining external and full-repository gates are tracked only in [OPEN_WEBUI_RELEASE_GATES.md](./OPEN_WEBUI_RELEASE_GATES.md).

## 10. Deployment authority

For more than 20 non-technical users and 3–4 years of memory retention, the production target is PostgreSQL 16 with pgvector, connection pooling, encrypted file/object storage, WAL/PITR, logical backups and periodic restore drills.

SQLite remains a local/development compatibility target. Canonical SQL data must survive vector/profile/search-generation rebuilds.

## 11. Repository status

Branch starting commit `34791a339` is already on `claude/native-memory-stability`. Hardening after that commit remains in the working tree. No further commit or push has been requested. Before any release, review the complete diff, [native memory changelog](./OPEN_WEBUI_NATIVE_MEMORY_CHANGELOG.md), deletion inventory and release gates.
