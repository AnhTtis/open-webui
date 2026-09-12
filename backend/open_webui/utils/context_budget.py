"""Final request budget guard after tools, memory, plugins, and source injection."""

from __future__ import annotations

import logging
from typing import Any

from open_webui.utils.misc import get_content_from_message

log = logging.getLogger(__name__)


def estimate_text_tokens(value: Any) -> int:
    if value is None:
        return 0
    if not isinstance(value, str):
        value = str(value)
    # Conservative mixed-language fallback. Provider usage/tokenizers remain the
    # source of truth when available, but this prevents obviously oversized calls.
    return max(1, (len(value) + 2) // 3)


def estimate_request_tokens(form_data: dict) -> int:
    tokens = 0
    for message in form_data.get('messages') or []:
        tokens += 6 + estimate_text_tokens(message.get('role'))
        tokens += estimate_text_tokens(get_content_from_message(message))
        tokens += estimate_text_tokens(message.get('tool_calls'))
    tokens += estimate_text_tokens(form_data.get('tools'))
    tokens += estimate_text_tokens(form_data.get('response_format'))
    return tokens


def resolve_context_limit(form_data: dict, model: dict | None, metadata: dict) -> int | None:
    params = metadata.get('params') or {}
    candidates = [
        params.get('num_ctx'),
        params.get('context_length'),
        form_data.get('num_ctx'),
        ((model or {}).get('info', {}).get('params') or {}).get('num_ctx'),
        ((model or {}).get('info', {}).get('meta') or {}).get('context_length'),
        ((model or {}).get('details') or {}).get('context_length'),
    ]
    for value in candidates:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def _completion_reserve(form_data: dict, limit: int) -> int:
    for key in ('max_completion_tokens', 'max_tokens'):
        try:
            value = int(form_data.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return min(value, max(512, limit // 2))
    return min(max(512, limit // 8), max(1, limit // 2))


def enforce_final_context_budget(form_data: dict, model: dict | None, metadata: dict) -> dict:
    """Drop oldest complete message records until the final provider payload fits.

    The system message and latest user turn are never removed. This is an
    emergency guard, not a substitute for normal context compaction.
    """
    limit = resolve_context_limit(form_data, model, metadata)
    if not limit:
        return form_data

    reserve = _completion_reserve(form_data, limit)
    budget = max(1, limit - reserve)
    before = estimate_request_tokens(form_data)
    if before <= budget:
        metadata['context_budget'] = {
            'estimated_tokens': before,
            'budget': budget,
            'context_limit': limit,
            'completion_reserve': reserve,
            'dropped_messages': 0,
            'source': 'heuristic',
        }
        return form_data

    messages = list(form_data.get('messages') or [])
    system_messages = [message for message in messages if message.get('role') == 'system']
    non_system = [message for message in messages if message.get('role') != 'system']
    latest_user_index = next(
        (idx for idx in range(len(non_system) - 1, -1, -1) if non_system[idx].get('role') == 'user'),
        len(non_system) - 1,
    )

    dropped = 0
    while (
        len(non_system) > 1
        and estimate_request_tokens({**form_data, 'messages': [*system_messages, *non_system]}) > budget
    ):
        removable = next(
            (idx for idx in range(len(non_system)) if idx != latest_user_index),
            None,
        )
        if removable is None:
            break
        non_system.pop(removable)
        dropped += 1
        if removable < latest_user_index:
            latest_user_index -= 1

    form_data['messages'] = [*system_messages, *non_system]
    after = estimate_request_tokens(form_data)
    metadata['context_budget'] = {
        'estimated_tokens': after,
        'estimated_tokens_before': before,
        'budget': budget,
        'context_limit': limit,
        'completion_reserve': reserve,
        'dropped_messages': dropped,
        'source': 'heuristic',
        'over_budget': after > budget,
    }
    if dropped:
        log.warning(
            'Final context guard dropped %s complete message(s): estimated=%s budget=%s limit=%s',
            dropped,
            after,
            budget,
            limit,
        )
    return form_data
