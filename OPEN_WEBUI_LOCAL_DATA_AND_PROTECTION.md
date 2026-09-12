# Open WebUI local data and protection

> Updated: 2026-09-11  
> Scope: native Open WebUI only

This is the canonical local-data map for this checkout. It replaces the former Hermes/Open WebUI combined storage document.

## 1. Default data root

For this source checkout, `backend/open_webui/env.py` resolves the default data root as:

```text
D:\ClaudeExperience\Memory\open-webui\backend\data
```

Equivalent repository-relative path:

```text
backend/data
```

`DATA_DIR` may override this location. `DATABASE_URL` may separately point canonical SQL state to PostgreSQL or another configured database.

The old path below is not the active source-checkout default and must not be used for backup decisions:

```text
backend/open_webui/data
```

If a zero-byte or abandoned `backend/open_webui/data/webui.db` exists, treat it as non-authoritative unless an operator has explicitly configured `DATA_DIR` to that directory and verified the running process uses it.

## 2. Local SQLite authority

Without `DATABASE_URL` or database component overrides, canonical SQL state is:

```text
backend/data/webui.db
```

This database includes users, permissions, chats, files and the durable memory tables. It is protected data. Never delete, truncate or replace it as part of cache cleanup.

For production with more than 20 users and 3–4 years retention, use PostgreSQL 16 rather than treating this SQLite file as the production authority.

## 3. File storage

Default local uploaded-file storage is:

```text
backend/data/uploads/
```

These objects are canonical originals. For DOCX/XLS/XLSX, preserve the exact uploaded bytes, original MIME metadata and filename. Server-generated PDF previews are derivatives and must never overwrite the original object.

When S3, GCS or Azure Blob storage is configured, the remote object is canonical according to that provider configuration; any local downloaded conversion copy is temporary cache.

## 4. Vector and generated data

Common local vector/cache locations are under `DATA_DIR`, including configured Chroma/vector state and cache directories such as:

```text
backend/data/vector_db/
backend/data/cache/
```

Embeddings, vector indexes, search generations and synthesized profile/search views are derivative in the architecture. They must be rebuildable from canonical SQL/revision/evidence data.

“Derivative” does not mean “safe to delete at any time.” Never remove the live vector collection during an active deployment. Build and validate a shadow generation, atomically switch the active generation and retain the old generation temporarily for rollback.

## 5. Durable memory data

Canonical memory lives in SQL, not in a Hermes workspace or Hermes state database.

Tables:

| Table                    | Purpose                                                        |
| ------------------------ | -------------------------------------------------------------- |
| `memory`                 | Current canonical per-user memory state                        |
| `memory_revision`        | Append-only revision history                                   |
| `memory_evidence`        | Source/provenance evidence for revisions                       |
| `memory_relation`        | Duplicate/supersedes/contradicts/supports relationships        |
| `agent_profile`          | Per-user/per-agent profile root and learning state             |
| `agent_profile_revision` | Versioned generated profile content                            |
| `session_memory_state`   | Working/session summary, goals, open loops and expiry          |
| `memory_proposal`        | Approval-gated automatic memory changes                        |
| `memory_job`             | Durable extraction/vector/profile jobs with leases and retries |
| `memory_audit_event`     | Metadata-oriented lifecycle audit events                       |

Every memory operation must enforce `user_id` isolation. General audit and health views must not expose raw private memory content by default.

There is no active Open WebUI Hermes session database, `DATA_DIR/hermes/state.db`, or Hermes task workspace lifecycle in the native architecture.

## 6. Secrets and configuration

Protected configuration includes:

```text
.env
backend/.env                 # if locally used
backend/.webui_secret_key
```

These files may be checked for existence during diagnostics, but their contents must not be printed, copied into documentation or committed.

Changing or losing `WEBUI_SECRET_KEY` can invalidate sessions and may affect encrypted or signed state. Back it up using the deployment secret-management process, not ordinary source-control or cache-cleanup procedures.

## 7. Office preview lifecycle

```text
canonical DOCX/XLS/XLSX in storage
  -> authenticated access check
  -> temporary local input copy when required
  -> isolated temporary LibreOffice profile/output
  -> validated PDF bytes returned with private/no-store/nosniff
  -> temporary input/output/profile cleanup
```

The browser fallback reads the original binary through the authorized file endpoint and renders DOCX/spreadsheet content client-side. Download always targets the original Office object.

## 8. Path classification

### Protected: never remove during code cleanup

- `backend/data/**`
- configured external database data/volumes
- configured S3/GCS/Azure canonical objects
- `.env`, `backend/.env`
- `backend/.webui_secret_key`
- `.venv`
- `node_modules`
- intentional `build/` output when being used for production-like serving
- `static/pyodide/` unless explicitly rebuilding it
- migrations, source files, tests and launcher scripts

### Rebuildable, but operationally controlled

- vector indexes and embedding collections
- generated profile/search generations
- frontend production build
- Pyodide distribution assets
- downloaded model/browser/NLTK caches

Rebuildable data may still be required for uptime or rollback. Use a documented rebuild/switch procedure rather than ad-hoc deletion.

### Disposable development artifacts

Only after confirming no process depends on them:

- root `.svelte-kit/`
- `.pytest_cache/`
- `.codebase-memory/`
- `__pycache__/`
- `*.pyc`, `*.pyo`
- local diagnostic logs such as `.memory-check.log`

Do not use `git clean -fdx`; it can remove ignored databases, secrets, environments and generated assets.

## 9. Backup minimums

### Local/development

Back up at least:

- `backend/data/webui.db` while using a database-consistent method;
- `backend/data/uploads/`;
- `.webui_secret_key` through protected secret storage;
- any external vector state needed to avoid a long rebuild.

### Production target

- PostgreSQL 16 WAL/PITR;
- nightly logical backup;
- encrypted object/file backup;
- periodic clean-environment restore drill;
- per-user count/hash/revision-chain validation;
- derivative vector/profile rebuild verification;
- recorded RPO and RTO.

A backup is not accepted until restore has been tested.

## 10. Pre-cleanup and post-cleanup checks

Before cleanup, record without exposing content:

- database file checksum and size;
- upload count/size;
- vector state count/size;
- existence, not value, of secret/config files.

After cleanup, verify those values are unchanged. The recorded baseline for the current cleanup had SHA-256 `7571cdc15e4528b5b807a0d557927a53b6a24f89c79a665ffd35bc3ac47bc056` for `backend/data/webui.db`; use it only for this working-copy check, not as a permanent production invariant.
