import os
import unittest

from pydantic import BaseModel

os.environ.setdefault('WEBUI_SECRET_KEY', 'test-secret-key')

from open_webui.models.memories import normalize_memory_hash  # noqa: E402
from open_webui.utils.memory import validate_memory_operations  # noqa: E402


class Operation(BaseModel):
    action: str
    id: str | None = None
    content: str | None = None
    type: str | None = None
    path: str | None = None
    expected_version: int | None = None


class Form:
    def __init__(self, operations):
        self.operations = operations


class MemoryValidationTests(unittest.TestCase):
    def test_replace_preserves_omitted_path(self):
        operations = validate_memory_operations(Form([Operation(action='replace', id='memory-1', content='updated')]))
        self.assertNotIn('path', operations[0])

    def test_replace_can_explicitly_clear_path(self):
        operation = Operation(action='replace', id='memory-1', content='updated')
        operation.path = None
        operation.__pydantic_fields_set__.add('path')
        operations = validate_memory_operations(Form([operation]))
        self.assertIn('path', operations[0])
        self.assertIsNone(operations[0]['path'])

    def test_normalized_hash_is_stable_for_cosmetic_changes(self):
        first = normalize_memory_hash('Uses   Vim', 'user', '/Preferences/Editor/')
        second = normalize_memory_hash(' uses vim ', 'USER', 'preferences/editor')
        self.assertEqual(first, second)


if __name__ == '__main__':
    unittest.main()
