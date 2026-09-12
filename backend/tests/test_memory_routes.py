import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('WEBUI_SECRET_KEY', 'test-secret-key')

from fastapi import HTTPException, UploadFile  # noqa: E402

from open_webui.models.memories import Memories  # noqa: E402
from open_webui.routers import memories as memories_router  # noqa: E402


class MemoryRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_route_returns_private_json_attachment(self):
        bundle = {
            'schema_version': 1,
            'exported_at': 1_700_000_000,
            'memories': [{'id': 'memory-id', 'content': 'private'}],
            'revisions': [],
            'evidence': [],
            'profiles': [],
            'profile_revisions': [],
            'sessions': [],
        }
        user = SimpleNamespace(id='user-1')

        with (
            patch.object(memories_router, 'check_memories_permission', new=AsyncMock()),
            patch.object(Memories, 'export_memory_bundle', new=AsyncMock(return_value=bundle)) as export,
        ):
            response = await memories_router.export_memories(user=user)

        export.assert_awaited_once_with('user-1')
        self.assertEqual(response.media_type, 'application/json')
        self.assertEqual(json.loads(response.body), bundle)
        self.assertIn('attachment', response.headers['content-disposition'])
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.headers['x-content-type-options'], 'nosniff')

    async def test_import_route_passes_json_and_dry_run_to_service(self):
        payload = json.dumps({'schema_version': 1, 'memories': []}).encode()
        upload = UploadFile(file=io.BytesIO(payload), filename='memory.json')
        expected = {
            'schema_version': 1,
            'dry_run': True,
            'total': 0,
            'imported': 0,
            'skipped': 0,
        }
        user = SimpleNamespace(id='user-1')

        with (
            patch.object(memories_router, 'check_memories_permission', new=AsyncMock()),
            patch.object(
                Memories,
                'import_memory_bundle',
                new=AsyncMock(return_value=expected),
            ) as importer,
        ):
            result = await memories_router.import_memories(file=upload, dry_run=True, user=user)

        self.assertEqual(result, expected)
        importer.assert_awaited_once_with('user-1', {'schema_version': 1, 'memories': []}, dry_run=True)

    async def test_import_route_rejects_invalid_json(self):
        upload = UploadFile(file=io.BytesIO(b'not-json'), filename='memory.json')
        user = SimpleNamespace(id='user-1')

        with patch.object(memories_router, 'check_memories_permission', new=AsyncMock()):
            with self.assertRaisesRegex(HTTPException, 'Expecting value') as raised:
                await memories_router.import_memories(file=upload, user=user)

        self.assertEqual(raised.exception.status_code, 422)

    async def test_import_route_maps_validation_errors_to_unprocessable_entity(self):
        payload = json.dumps({'schema_version': 1, 'memories': [{'content': ''}]}).encode()
        upload = UploadFile(file=io.BytesIO(payload), filename='memory.json')
        user = SimpleNamespace(id='user-1')

        with (
            patch.object(memories_router, 'check_memories_permission', new=AsyncMock()),
            patch.object(
                Memories,
                'import_memory_bundle',
                new=AsyncMock(side_effect=ValueError('Memory entry 1 has no content')),
            ),
        ):
            with self.assertRaisesRegex(HTTPException, 'Memory entry 1 has no content') as raised:
                await memories_router.import_memories(file=upload, user=user)

        self.assertEqual(raised.exception.status_code, 422)

    async def test_import_route_rejects_payloads_over_10_mib(self):
        class OversizedUpload:
            async def read(self, limit):
                return b'x' * limit

        user = SimpleNamespace(id='user-1')
        with patch.object(memories_router, 'check_memories_permission', new=AsyncMock()):
            with self.assertRaises(HTTPException) as raised:
                await memories_router.import_memories(file=OversizedUpload(), user=user)

        self.assertEqual(raised.exception.status_code, 413)


if __name__ == '__main__':
    unittest.main()
