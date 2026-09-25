"""Hard caps and admin-configurable quotas for native memory.

Canonical rows live on disk (SQLite/PostgreSQL). These limits keep the
application working set bounded: page size, top-K, batch size, and UTF-8
byte caps. They are not a claim of zero RAM.
"""

from __future__ import annotations

import os


def _int_env(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


# Admin-configurable per-user quotas (overridable via config table).
MEMORIES_MAX_ITEMS_PER_USER = _int_env('MEMORIES_MAX_ITEMS_PER_USER', 5000, minimum=1, maximum=1_000_000)
MEMORIES_MAX_CONTENT_BYTES_PER_USER = _int_env(
    'MEMORIES_MAX_CONTENT_BYTES_PER_USER', 5 * 1024 * 1024, minimum=1024, maximum=1024 * 1024 * 1024
)

# Per-item / per-request hard caps (UTF-8 bytes unless noted).
MEMORY_MAX_CONTENT_BYTES = _int_env('MEMORY_MAX_CONTENT_BYTES', 16 * 1024, minimum=256, maximum=1024 * 1024)
MEMORY_MAX_PATH_BYTES = _int_env('MEMORY_MAX_PATH_BYTES', 1024, minimum=64, maximum=16 * 1024)
MEMORY_MAX_QUERY_BYTES = _int_env('MEMORY_MAX_QUERY_BYTES', 2048, minimum=64, maximum=16 * 1024)
MEMORY_MAX_STRUCTURED_BYTES = _int_env('MEMORY_MAX_STRUCTURED_BYTES', 8 * 1024, minimum=256, maximum=256 * 1024)
MEMORY_MAX_OPERATIONS_PER_REQUEST = _int_env('MEMORY_MAX_OPERATIONS_PER_REQUEST', 50, minimum=1, maximum=200)

MEMORY_DEFAULT_PAGE_SIZE = _int_env('MEMORY_DEFAULT_PAGE_SIZE', 20, minimum=1, maximum=100)
MEMORY_MAX_PAGE_SIZE = _int_env('MEMORY_MAX_PAGE_SIZE', 100, minimum=1, maximum=200)

MEMORY_PROMPT_ROW_CAP = _int_env('MEMORY_PROMPT_ROW_CAP', 20, minimum=1, maximum=100)
MEMORY_PROMPT_BYTE_CAP = _int_env('MEMORY_PROMPT_BYTE_CAP', 16 * 1024, minimum=1024, maximum=256 * 1024)
MEMORY_EXTRACTION_ROW_CAP = _int_env('MEMORY_EXTRACTION_ROW_CAP', 50, minimum=1, maximum=250)
MEMORY_EXTRACTION_BYTE_CAP = _int_env('MEMORY_EXTRACTION_BYTE_CAP', 32 * 1024, minimum=1024, maximum=256 * 1024)

MEMORY_VECTOR_TOP_K = _int_env('MEMORY_VECTOR_TOP_K', 8, minimum=1, maximum=50)
MEMORY_VECTOR_OVERSAMPLE = _int_env('MEMORY_VECTOR_OVERSAMPLE', 24, minimum=1, maximum=100)

MEMORY_REINDEX_BATCH = _int_env('MEMORY_REINDEX_BATCH', 200, minimum=10, maximum=1000)
MEMORY_DELETE_BATCH = _int_env('MEMORY_DELETE_BATCH', 200, minimum=10, maximum=1000)
MEMORY_EXPORT_BATCH = _int_env('MEMORY_EXPORT_BATCH', 100, minimum=10, maximum=500)

MEMORY_JSON_IMPORT_MAX_BYTES = _int_env('MEMORY_JSON_IMPORT_MAX_BYTES', 10 * 1024 * 1024, minimum=1024, maximum=50 * 1024 * 1024)
MEMORY_JSON_IMPORT_MAX_MEMORIES = _int_env('MEMORY_JSON_IMPORT_MAX_MEMORIES', 5000, minimum=1, maximum=20_000)
MEMORY_NDJSON_IMPORT_MAX_BYTES = _int_env('MEMORY_NDJSON_IMPORT_MAX_BYTES', 50 * 1024 * 1024, minimum=1024, maximum=512 * 1024 * 1024)
MEMORY_NDJSON_LINE_MAX_BYTES = _int_env('MEMORY_NDJSON_LINE_MAX_BYTES', 64 * 1024, minimum=1024, maximum=1024 * 1024)
MEMORY_NDJSON_MAX_RECORDS = _int_env('MEMORY_NDJSON_MAX_RECORDS', 50_000, minimum=1, maximum=500_000)

MEMORY_TOOL_LIST_DEFAULT = _int_env('MEMORY_TOOL_LIST_DEFAULT', 20, minimum=1, maximum=50)
MEMORY_TOOL_LIST_MAX = _int_env('MEMORY_TOOL_LIST_MAX', 50, minimum=1, maximum=50)
MEMORY_TOOL_RESULT_MAX_BYTES = _int_env('MEMORY_TOOL_RESULT_MAX_BYTES', 32 * 1024, minimum=1024, maximum=256 * 1024)

MEMORY_JOB_CONCURRENCY = _int_env('MEMORY_JOB_CONCURRENCY', 4, minimum=1, maximum=32)
MEMORY_JOB_BATCH_SIZE = _int_env('MEMORY_JOB_BATCH_SIZE', 4, minimum=1, maximum=20)
MEMORY_JOB_LEASE_SECONDS = _int_env('MEMORY_JOB_LEASE_SECONDS', 180, minimum=30, maximum=3600)
MEMORY_JOB_HEARTBEAT_SECONDS = _int_env('MEMORY_JOB_HEARTBEAT_SECONDS', 30, minimum=5, maximum=300)
MEMORY_JOB_TIMEOUT_SECONDS = _int_env('MEMORY_JOB_TIMEOUT_SECONDS', 300, minimum=30, maximum=3600)

MEMORY_TRANSFER_RETENTION_SECONDS = _int_env('MEMORY_TRANSFER_RETENTION_SECONDS', 24 * 60 * 60, minimum=60, maximum=7 * 24 * 60 * 60)

RECALLABLE_MEMORY_STATUSES = ('active',)
LIVE_MEMORY_STATUSES = ('active', 'candidate')
NON_DELETED_MEMORY_STATUSES = ('candidate', 'active', 'superseded', 'archived')
NON_RECALLABLE_MEMORY_STATUSES = ('candidate', 'superseded', 'archived', 'deleted')


def utf8_bytes(value: str | None) -> int:
    return len((value or '').encode('utf-8'))


def clamp_page_size(limit: int | None, default: int = MEMORY_DEFAULT_PAGE_SIZE) -> int:
    if limit is None:
        return default
    return max(1, min(int(limit), MEMORY_MAX_PAGE_SIZE))


async def get_memory_quota_limits() -> tuple[int, int]:
    """Return (max_items, max_content_bytes) with config overlay when available."""
    max_items = MEMORIES_MAX_ITEMS_PER_USER
    max_bytes = MEMORIES_MAX_CONTENT_BYTES_PER_USER
    try:
        from open_webui.models.config import Config

        values = await Config.get_many('memories.max_items_per_user', 'memories.max_content_bytes_per_user')
        configured_items = values.get('memories.max_items_per_user')
        configured_bytes = values.get('memories.max_content_bytes_per_user')
        if isinstance(configured_items, int) and configured_items > 0:
            max_items = configured_items
        if isinstance(configured_bytes, int) and configured_bytes > 0:
            max_bytes = configured_bytes
    except Exception:
        pass
    return max_items, max_bytes
