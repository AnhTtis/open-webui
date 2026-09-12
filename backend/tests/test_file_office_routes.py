import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault('WEBUI_SECRET_KEY', 'test-secret-key')

from fastapi import HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

from open_webui.models.files import Files  # noqa: E402
from open_webui.routers import files as files_router  # noqa: E402
from open_webui.storage.provider import Storage  # noqa: E402


class OfficeFileRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_denies_unrelated_users_without_reading_storage(self):
        file = SimpleNamespace(id='file-id', user_id='owner', path='private/document.docx')
        user = SimpleNamespace(id='other-user', role='user')

        with (
            patch.object(Files, 'get_file_by_id', new=AsyncMock(return_value=file)),
            patch.object(files_router, 'has_access_to_file', new=AsyncMock(return_value=False)),
            patch.object(Storage, 'get_file', new=Mock()) as get_file,
        ):
            with self.assertRaises(HTTPException) as raised:
                await files_router.get_file_preview_by_id('file-id', user=user, db=object())

        self.assertEqual(raised.exception.status_code, 404)
        get_file.assert_not_called()

    async def test_preview_returns_private_sanitized_pdf_response(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / 'source.docx'
            source.write_bytes(b'canonical-office-bytes')
            file = SimpleNamespace(
                id='file-id',
                user_id='owner',
                path='private/source.docx',
                filename='résumé kế hoạch.docx',
                meta={
                    'name': 'résumé kế hoạch.docx',
                    'content_type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                },
            )
            user = SimpleNamespace(id='owner', role='user')
            pdf = b'%PDF-1.4\npreview\n%%EOF'

            with (
                patch.object(Files, 'get_file_by_id', new=AsyncMock(return_value=file)),
                patch.object(Storage, 'get_file', new=Mock(return_value=str(source))),
                patch.object(
                    files_router,
                    'convert_office_document_to_pdf',
                    new=AsyncMock(return_value=pdf),
                ) as convert,
                patch.object(files_router, '_cleanup_local_cache', new=Mock()) as cleanup,
            ):
                response = await files_router.get_file_preview_by_id('file-id', user=user, db=object())

            self.assertEqual(response.body, pdf)
            self.assertEqual(response.media_type, 'application/pdf')
            self.assertEqual(response.headers['cache-control'], 'private, no-store')
            self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
            self.assertIn(
                "filename*=UTF-8''r%C3%A9sum%C3%A9%20k%E1%BA%BF%20ho%E1%BA%A1ch.pdf",
                response.headers['content-disposition'],
            )
            convert.assert_awaited_once()
            cleanup.assert_called_once_with(file.path)

    async def test_canonical_download_keeps_binary_bytes_mime_and_unicode_filename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / 'source.xlsx'
            original = b'PK\x03\x04\x00\x00canonical-xlsx-bytes\xff\x00'
            source.write_bytes(original)
            file = SimpleNamespace(
                id='file-id',
                user_id='owner',
                path='private/source.xlsx',
                filename='báo cáo quý.xlsx',
                meta={
                    'name': 'báo cáo quý.xlsx',
                    'content_type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                },
                data={},
            )
            user = SimpleNamespace(id='owner', role='user')

            with (
                patch.object(Files, 'get_file_by_id', new=AsyncMock(return_value=file)),
                patch.object(Storage, 'get_file', new=Mock(return_value=str(source))),
            ):
                response = await files_router.get_file_content_by_id('file-id', user=user, db=object())

            self.assertIsInstance(response, FileResponse)
            self.assertEqual(
                response.media_type,
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            )
            self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
            self.assertIn('attachment', response.headers['content-disposition'])
            self.assertIn(
                "filename*=UTF-8''b%C3%A1o%20c%C3%A1o%20qu%C3%BD.xlsx", response.headers['content-disposition']
            )
            returned = Path(response.path).read_bytes()
            self.assertEqual(returned, original)
            self.assertEqual(hashlib.sha256(returned).digest(), hashlib.sha256(original).digest())


if __name__ == '__main__':
    unittest.main()
