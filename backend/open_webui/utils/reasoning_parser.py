"""Small helpers for normalizing streamed reasoning output state."""

from __future__ import annotations

import re
import time
from typing import Any, Callable


class ReasoningTagState:
    """State names used by a streaming tag parser."""

    PLAIN = 'plain'
    POSSIBLE_OPEN = 'possible_open'
    REASONING = 'reasoning'
    POSSIBLE_CLOSE = 'possible_close'
    COMPLETED = 'completed'
    INTERRUPTED = 'interrupted'


def finalize_reasoning_items(
    output: list[dict[str, Any]],
    *,
    status: str = 'incomplete',
    now: Callable[[], float] = time.time,
) -> int:
    """Close every still-open reasoning item without exposing provider tags.

    Streaming can end because the client cancelled, the provider failed, or a
    connection was re-established. In all of those cases an open reasoning
    item is more truthful as ``incomplete`` than ``completed``. The operation
    is idempotent and deliberately only touches structured reasoning items.
    """
    changed = 0
    ended_at = now()
    for item in output:
        if item.get('type') != 'reasoning' or item.get('status') != 'in_progress':
            continue
        item['ended_at'] = ended_at
        started_at = item.get('started_at')
        if isinstance(started_at, (int, float)):
            item['duration'] = max(0, int(ended_at - started_at))
        item['status'] = status
        changed += 1
    return changed


def finalize_stream_reasoning_items(
    output: list[dict[str, Any]],
    *,
    now: Callable[[], float] = time.time,
) -> int:
    """Finalize reasoning left open when a provider stream ends normally.

    Provider-native reasoning fields have an explicit stream boundary and can be
    completed at EOF. Legacy tag-backed reasoning requires a closing marker; if
    it never arrives, preserving the item as incomplete avoids presenting a
    truncated thought block as successfully completed.
    """
    changed = 0
    ended_at = now()
    for item in output:
        if item.get('type') != 'reasoning' or item.get('status') != 'in_progress':
            continue
        item['ended_at'] = ended_at
        started_at = item.get('started_at')
        if isinstance(started_at, (int, float)):
            item['duration'] = max(0, int(ended_at - started_at))
        item['status'] = 'completed' if item.get('attributes', {}).get('type') == 'reasoning_content' else 'incomplete'
        changed += 1
    return changed


def _start_tag_pattern(start_tag: str) -> str:
    if start_tag.startswith('<') and start_tag.endswith('>'):
        return rf'<{re.escape(start_tag[1:-1])}(\s.*?)?>'
    return re.escape(start_tag)


class TaggedOutputState:
    """Incremental parser for tag-backed Open WebUI output items.

    The native stream accumulates deltas in output items before emitting them.
    This state object scans those items incrementally, so split tags are held
    until the next delta and never leak into the user-visible message. It also
    handles the three native tag families: reasoning, solution and code
    interpreter. ``output_id`` is injected to keep this module independent of
    middleware and persistence details.
    """

    def __init__(
        self,
        *,
        output_id: Callable[[str], str],
        now: Callable[[], float] = time.time,
    ) -> None:
        self.output_id = output_id
        self.now = now
        self.scan_positions: dict[tuple[str, str], int] = {}
        self.boundary_positions: dict[tuple[str, str], tuple[int, int, int]] = {}
        self.tags_by_type: dict[str, tuple[tuple[str, str], ...]] = {}
        self.emitted_text: dict[str, str] = {}

    def process(
        self,
        content_type: str,
        tags: list[tuple[str, str]],
        output: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        """Normalize one accumulated output item and report block completion."""
        end_flag = False
        self.tags_by_type[content_type] = tuple(tags)

        def extract_attributes(tag_content: str | None) -> dict[str, str]:
            attributes: dict[str, str] = {}
            if not tag_content:
                return attributes
            for key, value in re.findall(r'(\w+)\s*=\s*"([^"]+)"', tag_content):
                attributes[key] = value
            return attributes

        def get_last_text() -> str:
            if output and output[-1].get('type') == 'message':
                parts = output[-1].get('content', [])
                if parts and parts[-1].get('type') == 'output_text':
                    return parts[-1].get('text', '')
            return ''

        def set_last_text(text: str) -> None:
            if output and output[-1].get('type') == 'message':
                parts = output[-1].get('content', [])
                if parts and parts[-1].get('type') == 'output_text':
                    parts[-1]['text'] = text

        def get_scanned_length(item: dict[str, Any], text: str) -> int:
            item_id = item.get('id')
            if not item_id:
                return 0
            scanned_length = self.scan_positions.get((item_id, content_type), 0)
            return scanned_length if scanned_length <= len(text) else 0

        def save_scanned_length(item: dict[str, Any], text: str) -> None:
            item_id = item.get('id')
            if item_id:
                self.scan_positions[(item_id, content_type)] = len(text)

        def clear_scanned_length(item: dict[str, Any]) -> None:
            item_id = item.get('id')
            if item_id:
                self.scan_positions.pop((item_id, content_type), None)
                self.boundary_positions.pop((item_id, content_type), None)

        def get_tag_boundaries(item: dict[str, Any], text: str, scanned_length: int) -> tuple[int, int]:
            """Return the latest open marker and tag boundary before scan position."""
            key = (item.get('id'), content_type)
            scanned, last_open, last_boundary = self.boundary_positions.get(key, (0, -1, -1))
            if scanned > scanned_length:
                scanned, last_open, last_boundary = 0, -1, -1

            if scanned < scanned_length:
                open_tag = text.rfind('<', scanned, scanned_length)
                if open_tag != -1:
                    last_open = open_tag
                boundary = max(
                    text.rfind('>', scanned, scanned_length),
                    text.rfind('\n', scanned, scanned_length),
                )
                if boundary != -1:
                    last_boundary = boundary
                self.boundary_positions[key] = (scanned_length, last_open, last_boundary)

            return last_open, last_boundary

        output_type_map = {
            'reasoning': 'reasoning',
            'solution': 'message',
            'code_interpreter': 'open_webui:code_interpreter',
        }
        output_item_type = output_type_map.get(content_type, content_type)
        last_type = output[-1].get('type', '') if output else ''

        if last_type == 'message' and output[-1].get('_tag_type') != content_type:
            item = output[-1]
            item_text = get_last_text()
            scanned_length = get_scanned_length(item, item_text)
            max_start_tag_length = max((len(start_tag) for start_tag, _ in tags), default=1)
            search_start = max(0, scanned_length - max_start_tag_length + 1)

            if scanned_length and any(start_tag.startswith('<') and start_tag.endswith('>') for start_tag, _ in tags):
                open_tag_start, last_tag_boundary = get_tag_boundaries(item, item_text, scanned_length)
                if open_tag_start > last_tag_boundary:
                    search_start = min(search_start, open_tag_start)

            for start_tag, end_tag in tags:
                match = re.compile(_start_tag_pattern(start_tag)).search(item_text, search_start)
                if not match:
                    continue

                clear_scanned_length(item)
                attr_content = match.group(1) if match.lastindex else ''
                attributes = extract_attributes(attr_content)
                before_tag = item_text[: match.start()]
                after_tag = item_text[match.end() :]
                set_last_text(before_tag)

                if not before_tag.strip() and output and output[-1].get('type') == 'message':
                    output.pop()

                if output_item_type == 'reasoning':
                    output.append(
                        {
                            'type': 'reasoning',
                            'id': self.output_id('r'),
                            'status': 'in_progress',
                            'start_tag': start_tag,
                            'end_tag': end_tag,
                            'attributes': attributes,
                            'content': [],
                            'summary': None,
                            'started_at': self.now(),
                        }
                    )
                elif output_item_type == 'open_webui:code_interpreter':
                    output.append(
                        {
                            'type': 'open_webui:code_interpreter',
                            'id': self.output_id('ci'),
                            'status': 'in_progress',
                            'start_tag': start_tag,
                            'end_tag': end_tag,
                            'attributes': attributes,
                            'lang': attributes.get('lang', 'python'),
                            'code': '',
                            'output': None,
                            'started_at': self.now(),
                        }
                    )
                else:
                    output.append(
                        {
                            'type': 'message',
                            'id': self.output_id('msg'),
                            'status': 'in_progress',
                            'role': 'assistant',
                            'content': [{'type': 'output_text', 'text': ''}],
                            '_tag_type': content_type,
                            'start_tag': start_tag,
                            'end_tag': end_tag,
                            'attributes': attributes,
                            'started_at': self.now(),
                        }
                    )

                if after_tag:
                    if output_item_type == 'reasoning':
                        output[-1]['content'] = [{'type': 'output_text', 'text': after_tag}]
                    elif output_item_type == 'open_webui:code_interpreter':
                        output[-1]['code'] = after_tag
                    else:
                        set_last_text(after_tag)
                    _, recursive_end = self.process(content_type, tags, output)
                    if recursive_end:
                        end_flag = True
                break
            else:
                save_scanned_length(item, item_text)

        elif (
            (last_type == 'reasoning' and content_type == 'reasoning')
            or (last_type == 'open_webui:code_interpreter' and content_type == 'code_interpreter')
            or (last_type == 'message' and output[-1].get('_tag_type') == content_type)
        ):
            item = output[-1]
            start_tag = item.get('start_tag', '')
            end_tag = item.get('end_tag', '')
            if last_type == 'reasoning':
                parts = item.get('content', [])
                block_content = parts[-1].get('text', '') if parts and parts[-1].get('type') == 'output_text' else ''
            elif last_type == 'open_webui:code_interpreter':
                block_content = item.get('code', '')
            else:
                block_content = get_last_text()

            scanned_length = get_scanned_length(item, block_content)
            end_tag_search_start = max(0, scanned_length - max(len(end_tag), 1) + 1)
            if block_content.find(end_tag, end_tag_search_start) != -1:
                clear_scanned_length(item)
                end_flag = True
                block_content = re.sub(_start_tag_pattern(start_tag), '', block_content).strip()
                split_content = re.split(rf'{re.escape(end_tag)}', block_content, maxsplit=1)
                block_content = split_content[0].strip() if split_content else ''
                leftover_content = split_content[1].strip() if len(split_content) > 1 else ''

                if block_content:
                    if last_type == 'reasoning':
                        item['content'] = [{'type': 'output_text', 'text': block_content}]
                        item['ended_at'] = self.now()
                        item['duration'] = int(item['ended_at'] - item['started_at'])
                        item['status'] = 'completed'
                    elif last_type == 'open_webui:code_interpreter':
                        item['code'] = block_content
                        item['ended_at'] = self.now()
                        item['duration'] = int(item['ended_at'] - item['started_at'])
                    else:
                        set_last_text(block_content)
                        item['ended_at'] = self.now()
                    output.append(self._new_message(leftover_content))
                else:
                    output.pop()
                    output.append(self._new_message(leftover_content))
            else:
                save_scanned_length(item, block_content)

        return output, end_flag

    @staticmethod
    def _item_text(item: dict[str, Any]) -> tuple[str, int]:
        item_type = item.get('type')
        if item_type in {'message', 'reasoning'}:
            parts = item.get('content', [])
            if parts and parts[-1].get('type') == 'output_text':
                return parts[-1].get('text', ''), len(parts) - 1
        elif item_type == 'open_webui:code_interpreter':
            return item.get('code', ''), 0
        return '', 0

    @staticmethod
    def _without_pending_marker(
        text: str,
        markers: list[str],
        *,
        allow_attributes: bool = False,
    ) -> str:
        cut = len(text)
        for marker in markers:
            if not marker:
                continue
            for size in range(1, min(len(marker), len(text)) + 1):
                if text.endswith(marker[:size]):
                    cut = min(cut, len(text) - size)

            if allow_attributes and marker.startswith('<') and marker.endswith('>'):
                marker_stem = marker[:-1]
                open_index = text.rfind('<')
                if open_index != -1:
                    suffix = text[open_index:]
                    is_partial_name = marker_stem.startswith(suffix)
                    is_attribute_form = suffix.startswith(marker_stem) and (
                        len(suffix) == len(marker_stem) or suffix[len(marker_stem)].isspace()
                    )
                    if '>' not in suffix and (is_partial_name or is_attribute_form):
                        cut = min(cut, open_index)

        return text[:cut]

    def _safe_item_text(self, item: dict[str, Any]) -> tuple[str, int]:
        text, content_index = self._item_text(item)
        item_type = item.get('type')
        tag_type = item.get('_tag_type')

        if item_type == 'message' and not tag_type:
            start_tags = [start_tag for tags in self.tags_by_type.values() for start_tag, _ in tags]
            return (
                self._without_pending_marker(
                    text,
                    start_tags,
                    allow_attributes=True,
                ),
                content_index,
            )

        if item_type == 'reasoning' and item.get('attributes', {}).get('type') == 'reasoning_content':
            return text, content_index

        active_type = (
            tag_type
            or ('reasoning' if item_type == 'reasoning' else None)
            or ('code_interpreter' if item_type == 'open_webui:code_interpreter' else None)
        )
        end_tags = [end_tag for _, end_tag in self.tags_by_type.get(active_type, ())]
        if item.get('end_tag'):
            end_tags.append(item['end_tag'])
        return self._without_pending_marker(text, end_tags), content_index

    def prime_emitted_text(self, output: list[dict[str, Any]]) -> None:
        """Record output that existed before this stream so it is not replayed."""
        for item in output:
            item_id = item.get('id')
            if item_id:
                self.emitted_text[item_id] = self._item_text(item)[0]

    def mark_emitted_text(self, item: dict[str, Any]) -> None:
        """Record text emitted through a provider-native event path."""
        item_id = item.get('id')
        if item_id:
            self.emitted_text[item_id] = self._item_text(item)[0]

    def take_safe_deltas(self, output: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return newly visible deltas while retaining split tag markers.

        The accumulated output may temporarily end in a prefix such as ``<thi``
        or ``</thi``. Those bytes are held until the next provider chunk proves
        whether they are markup. This prevents transient tag fragments from
        reaching response delta events while preserving ordinary text that only
        happened to resemble a tag prefix.
        """
        deltas = []
        for output_index, item in enumerate(output):
            item_id = item.get('id')
            if not item_id:
                continue
            safe_text, content_index = self._safe_item_text(item)
            emitted = self.emitted_text.get(item_id, '')
            if safe_text.startswith(emitted):
                delta = safe_text[len(emitted) :]
                self.emitted_text[item_id] = safe_text
                if delta:
                    deltas.append(
                        {
                            'type': (
                                'response.reasoning_text.delta'
                                if item.get('type') == 'reasoning'
                                else 'response.output_text.delta'
                            ),
                            'item_id': item_id,
                            'output_index': output_index,
                            'content_index': content_index,
                            'delta': delta,
                            'item_type': item.get('type'),
                        }
                    )
            elif emitted != safe_text:
                # A completed tag block can trim markup/whitespace from its
                # accumulated item. The withheld marker was never emitted, so
                # resynchronize without attempting an impossible client retract.
                self.emitted_text[item_id] = safe_text

        return deltas

    def _new_message(self, text: str) -> dict[str, Any]:
        return {
            'type': 'message',
            'id': self.output_id('msg'),
            'status': 'in_progress',
            'role': 'assistant',
            'content': [{'type': 'output_text', 'text': text}],
        }


class ReasoningTagParser:
    """Incremental plain-text tag scanner for isolated streaming tests/tools.

    The parser buffers only enough text to resolve a tag split across chunks;
    it never returns tag markup as ordinary answer text. It is intentionally
    independent from the chat output schema so middleware can adapt events
    without coupling this state machine to persistence.
    """

    def __init__(self, tags: list[tuple[str, str]]) -> None:
        self.tags = tuple((start, end) for start, end in tags if start and end)
        self.state = ReasoningTagState.PLAIN
        self._buffer = ''
        self._active: tuple[str, str] | None = None

    @property
    def active_tag(self) -> tuple[str, str] | None:
        return self._active

    def feed(self, chunk: str) -> list[dict[str, str]]:
        if not chunk:
            return []
        self._buffer += chunk
        events: list[dict[str, str]] = []

        while self._buffer:
            if self._active is None:
                match = self._find_open_tag()
                if match is None:
                    safe = self._safe_prefix()
                    if safe:
                        events.append({'type': 'text', 'text': safe})
                        self._buffer = self._buffer[len(safe) :]
                    break

                index, start, end = match
                if index:
                    events.append({'type': 'text', 'text': self._buffer[:index]})
                self._buffer = self._buffer[index + len(start) :]
                self._active = (start, end)
                self.state = ReasoningTagState.REASONING
                events.append({'type': 'reasoning_start', 'start_tag': start, 'end_tag': end})
                continue

            end_tag = self._active[1]
            end_index = self._buffer.find(end_tag)
            if end_index < 0:
                safe = self._safe_prefix(end_tag)
                if safe:
                    events.append({'type': 'reasoning', 'text': safe})
                    self._buffer = self._buffer[len(safe) :]
                break

            if end_index:
                events.append({'type': 'reasoning', 'text': self._buffer[:end_index]})
            self._buffer = self._buffer[end_index + len(end_tag) :]
            events.append({'type': 'reasoning_end', 'end_tag': end_tag})
            self._active = None
            self.state = ReasoningTagState.PLAIN

        return events

    def finish(self) -> list[dict[str, str]]:
        events: list[dict[str, str]] = []
        if self._active is not None:
            safe = self._safe_prefix(self._active[1])
            if safe:
                events.append({'type': 'reasoning', 'text': safe})
            events.append({'type': 'reasoning_incomplete'})
        elif self._buffer:
            safe = self._safe_prefix()
            if safe:
                events.append({'type': 'text', 'text': safe})
            if safe != self._buffer:
                events.append({'type': 'tag_incomplete'})
        self._buffer = ''
        self._active = None
        self.state = (
            ReasoningTagState.INTERRUPTED
            if events and events[-1]['type'] in {'reasoning_incomplete', 'tag_incomplete'}
            else ReasoningTagState.COMPLETED
        )
        return events

    def _find_open_tag(self) -> tuple[int, str, str] | None:
        matches = [(self._buffer.find(start), start, end) for start, end in self.tags if self._buffer.find(start) >= 0]
        if not matches:
            return None
        return min(matches, key=lambda item: item[0])

    def _safe_prefix(self, boundary: str | None = None) -> str:
        limit = len(self._buffer)
        candidates = [start for start, _ in self.tags]
        if boundary:
            candidates.append(boundary)
        for candidate in candidates:
            for size in range(1, min(len(candidate), len(self._buffer)) + 1):
                if self._buffer.endswith(candidate[:size]):
                    limit = min(limit, len(self._buffer) - size)
        return self._buffer[:limit]
