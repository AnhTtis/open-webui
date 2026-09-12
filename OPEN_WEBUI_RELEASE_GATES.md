# Open WebUI release gates

> Updated: 2026-09-11  
> Branch: `claude/native-memory-stability`  
> Scope: production readiness after native-only source implementation

The feature source and focused tests are substantially complete. The retired Harness directories are already removed. This document is the single canonical list of work that remains before production approval for more than 20 non-technical users and 3–4 years of memory retention.

## Current verified baseline

- Embedded Hermes Harness is absent from the native runtime and retired directories are deleted.
- Focused backend pytest: **43 tests passed, 333 subtests passed**.
- Focused frontend Vitest: **2 files, 10 tests passed**.
- Focused ESLint passes for the changed files API, Memory Center and Office preview files.
- IDE diagnostics are empty for those changed frontend files and `backend/tests/test_office_preview.py`.
- Focused Prettier and `git diff --check` pass at the current checkpoint.
- Isolated SQLite backend startup and `/health` HTTP 200 passed.
- Frontend Vite startup HTTP 200 passed without startup-time dependency preparation.
- Mocked Office conversion and file-route security/canonical-byte tests passed.
- `package.json` parses and `uv lock --check` passes.
- pytest, pytest-asyncio and Ruff are installed in `.venv`; direct focused Ruff execution remains blocked because the permission boundary requires explicit approval for the PyPI-installed Ruff executable.

## 1. Focused backend Ruff gate

Run Ruff only against changed memory, context, MCP, middleware, reasoning, Office, migration and test files. Fix findings in that scope; do not auto-fix the repository-wide pre-existing debt.

Required outcomes:

- focused `ruff check` exits 0;
- focused `ruff format --check` exits 0;
- formatter/linter version is pinned consistently (the lock currently resolves Ruff 0.15.18 while the existing environment contains 0.16.7);
- no import with framework-registration or other side effects is removed without verification.

## 2. Focused frontend lint and full frontend baseline

Focused ESLint now exits successfully for:

- `src/lib/apis/files/index.ts`
- `src/lib/apis/memories/index.ts`
- `src/lib/components/chat/Settings/Personalization.svelte`
- `src/lib/components/common/FileItemModal.svelte`

The Excel `{@html}` boundary remains sanitized with DOMPurify immediately before assignment and is documented at the render boundary. Do not remove that sanitation or replace it with an unconditional lint suppression.

Focused Vitest passes **2 files and 10 tests**. The full `npm run check` still fails on broader existing TypeScript/JavaScript diagnostics outside these changed feature files, including RichTextInput JavaScript helpers and `src/lib/utils/index.ts`.

Still required under Node 22:

```powershell
npm run lint:frontend
npm run check
npm run test:frontend
npm run build
```

Record a complete full-check baseline grouped by root cause. Production release requires a successful full check/build or an explicitly approved, time-bounded exception with no diagnostics in changed feature files.

## 3. Supported Node.js build gate

`package.json` supports Node `>=18.13.0 <=22.x.x`. The current machine reported Node 24, which is outside the supported range.

Required outcomes:

- run frontend lint/test/build with Node 22.x;
- use `npm ci --force` for the existing dependency tree;
- do not certify a Node 24-only build as the supported release build.

## 4. Live LibreOffice conversion

The current machine has no approved `soffice` installation. Source and mocked tests are complete; live conversion is not.

Validate DOCX, XLS and XLSX with:

- Unicode filenames;
- multiple sheets/pages;
- merged cells;
- charts and images;
- page breaks and print areas;
- missing/corrupt documents;
- timeout kill/reap;
- cleanup after success and failure;
- disabled/unavailable LibreOffice browser fallback.

Required outcomes:

- server preview opens as a valid PDF with acceptable layout;
- original download retains exact bytes, MIME and filename;
- unauthorized requests return 404 before storage read;
- temporary input/output/profile directories are removed.

## 5. PostgreSQL 16 and pgvector integration

SQLite is not the production authority for the intended retention/load.

Provision an approved PostgreSQL 16 + pgvector environment and test:

1. migration on a clean database;
2. migration/backfill on representative legacy memory rows;
3. per-user count/hash/revision validation;
4. concurrent proposal review (only one apply);
5. optimistic update races;
6. concurrent import/normalized-hash duplicate races;
7. durable job claim, lease expiry, retry, stale recovery and dead letter;
8. strict user isolation across CRUD/history/proposal/export/import/retrieval;
9. pgvector ranking and SQL rejection of stale/deleted/cross-user vector hits.

## 6. Backup-grade memory export/import

Current import validates schema/size, strips tenant IDs, reports duplicates, supports dry-run and restores canonical rows without overwriting existing rows. It does not yet perform full historical restoration.

Still required:

- preserve or safely remap immutable revision chains;
- restore evidence/provenance, relations and appropriate audit linkage;
- restore agent profiles/profile revisions and session state;
- whole-bundle atomicity or a resumable restore cursor;
- checksums/count verification after restore;
- an explicit destination-conflict policy.

A clean-database restore must reproduce canonical state and expected historical chains without allowing a bundle to select another tenant.

## 7. Retention and lifecycle completion

Complete and test:

- session/working-memory checkpoints and TTL;
- evidence-based promotion to long-term memory;
- profile synthesis/revisions and rebuild;
- archive retention and recycle-bin behavior;
- purge grace period and explicit approval policy;
- per-user quota/storage controls;
- richer provenance display;
- admin health views that cannot browse private memory content by default.

## 8. Safe derivative reindex

Implement and validate:

- shadow vector generation;
- snapshot revision cursor;
- batch rebuild;
- ID/count/fingerprint validation;
- mutation replay after snapshot;
- atomic active-generation switch;
- temporary retention of the previous generation;
- SQL/vector drift metrics and controlled cleanup.

Never delete the live collection before the shadow generation is complete and verified.

## 9. Context, query and compaction hardening

Still required for the long-horizon production profile:

- provider/model tokenizer-aware budgeting;
- a query-intent view dedicated to web/file/RAG retrieval;
- dependency-safe trimming by turn/tool group;
- reduction of tool schemas/source context before important conversation turns;
- checkpoint version, checksum, source-message IDs and prompt/model/provider fingerprint;
- superseded-summary lineage;
- deterministic emergency reduction when summary generation fails.

## 10. Reasoning end-to-end tests

The parser split-boundary suite passes. Add middleware/end-to-end coverage for:

- cancel during reasoning;
- provider error;
- reconnect and replay;
- same-model and cross-model replay;
- provider-native reasoning mixed with legacy tags;
- no private reasoning persisted into durable memory;
- incomplete reasoning never displayed as completed.

## 11. Load and long-horizon simulation

Seed 20–50 users with thousands of memories/revisions per user and simulated timestamps over 3–4 years. Measure p50/p95/p99 for retrieval, context assembly and Memory Center pagination. Exercise duplicate/contradiction evidence, profile evolution, worker backlog/restart, reindex during mutation and quota growth.

Required outcomes:

- no cross-user results;
- no lost canonical mutation or durable job across restart;
- latency/backlog/storage remain within operator-approved SLOs.

## 12. Backup and disaster recovery

Configure and exercise:

- PostgreSQL WAL/PITR;
- nightly logical backups;
- encrypted backup/object storage;
- restore into a clean environment;
- canonical count/hash/revision-chain comparison;
- vector/profile/search derivative rebuild;
- release rollback without restoring Harness;
- measured RPO/RTO and an operator runbook.

## 13. Cloud/object-storage Office validation

For each configured storage provider, verify:

- canonical download byte equality;
- access denial before remote object fetch;
- temporary local cache cleanup;
- cleanup after conversion failure/timeout;
- Unicode filename and MIME consistency.

## 14. Monitoring and redaction

Before release, monitor at least:

- memory job age, attempts, failures and dead-letter count;
- per-user memory growth/quota;
- SQL/vector count drift;
- extraction/proposal rates;
- retrieval miss/truncation and injected-token count;
- compaction failure;
- Office conversion latency/failure;
- backup/restore health.

Logs, metrics and admin APIs must not expose private memory/document content, tokens, passwords or secret values.

## Working-copy cleanup still pending

The following disposable artifacts are still present because the recursive deletion request was denied. They are not product source and may be removed only through an explicitly approved, exact allowlist:

- `.memory-check.log`
- `.pytest_cache/`
- root `.svelte-kit/`
- `.codebase-memory/`
- `backend/**/__pycache__/`
- backend `*.pyc` and `*.pyo`

Any cleanup must continue to exclude `backend/data/**`, uploads, vector state, `.env`, `.webui_secret_key`, `.venv`, `node_modules`, `build`, `static/pyodide`, migrations, source, tests and launchers. Do not use `git clean -fdx`.

## Final checklist

- [ ] Focused Ruff check/format pass with a consistent pinned version.
- [x] Focused frontend ESLint pass.
- [ ] Full frontend check and Node 22 production build pass or approved exception is documented.
- [ ] Live LibreOffice matrix passes.
- [ ] PostgreSQL 16 migration and concurrency tests pass.
- [ ] pgvector retrieval and shadow-reindex tests pass.
- [ ] Backup-grade historical memory restore passes.
- [ ] Retention/session/profile lifecycle passes.
- [ ] 20–50-user long-horizon load/restart simulation passes.
- [ ] Backup/restore drill passes with recorded RPO/RTO.
- [ ] Cloud/object-storage Office tests pass where applicable.
- [ ] Monitoring and redaction are verified.
- [ ] Final security review and `git diff --check` pass.
- [ ] Complete deletion/source inventory is reviewed before commit.

No commit or push has been requested or performed.
