import asyncio
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from open_webui.utils import office_preview


def _write_docx(path: Path) -> None:
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types />')
        archive.writestr('word/document.xml', '<document />')


class OfficePreviewValidationTests(unittest.TestCase):
    def test_validate_office_document_checks_package_type_and_size(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            document = Path(temp_dir) / 'document.docx'
            _write_docx(document)

            self.assertEqual(
                office_preview.validate_office_document(
                    document,
                    'résumé.docx',
                    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                    max_file_size=1024,
                ),
                '.docx',
            )

            with self.assertRaises(office_preview.OfficePreviewTypeError):
                office_preview.validate_office_document(
                    document,
                    'document.xlsx',
                    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    max_file_size=1024,
                )

            with self.assertRaises(office_preview.OfficePreviewTooLargeError):
                office_preview.validate_office_document(
                    document,
                    'document.docx',
                    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                    max_file_size=1,
                )


class OfficePreviewConversionTests(unittest.IsolatedAsyncioTestCase):
    async def test_conversion_uses_isolated_profile_and_cleans_temp_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            document = Path(temp_dir) / 'document.docx'
            _write_docx(document)
            captured: dict[str, object] = {}

            class FakeProcess:
                returncode = 0

                async def communicate(self):
                    args = captured['args']
                    output_dir = Path(args[args.index('--outdir') + 1])
                    (output_dir / 'document.pdf').write_bytes(b'%PDF-1.4\n%%EOF')
                    return b'converted', b''

                def kill(self):
                    raise AssertionError('successful conversion should not be killed')

            async def fake_create_subprocess_exec(*args, **kwargs):
                captured['args'] = list(args)
                captured['kwargs'] = kwargs
                captured['temp_dir'] = Path(args[-1]).parent
                return FakeProcess()

            with patch.object(asyncio, 'create_subprocess_exec', new=fake_create_subprocess_exec):
                result = await office_preview.convert_office_document_to_pdf(
                    document,
                    'document.docx',
                    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                    enabled=True,
                    libreoffice_path=sys.executable,
                    timeout=2,
                    max_file_size=1024,
                )

            self.assertTrue(result.startswith(b'%PDF-'))
            self.assertTrue(any(str(arg).startswith('-env:UserInstallation=file:') for arg in captured['args']))
            self.assertIs(captured['kwargs']['stdout'], asyncio.subprocess.PIPE)
            self.assertIs(captured['kwargs']['stderr'], asyncio.subprocess.PIPE)
            self.assertFalse(captured['temp_dir'].exists())

    async def test_conversion_kills_and_reaps_timed_out_process(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            document = Path(temp_dir) / 'document.docx'
            _write_docx(document)

            class FakeProcess:
                returncode = None
                calls = 0
                killed = False

                def __init__(self):
                    self.terminated = asyncio.Event()

                async def communicate(self):
                    self.calls += 1
                    await self.terminated.wait()
                    return b'', b''

                def kill(self):
                    self.killed = True
                    self.returncode = -9
                    self.terminated.set()

            process = FakeProcess()

            async def fake_create_subprocess_exec(*args, **kwargs):
                return process

            with patch.object(asyncio, 'create_subprocess_exec', new=fake_create_subprocess_exec):
                with self.assertRaises(office_preview.OfficePreviewTimeoutError):
                    await office_preview.convert_office_document_to_pdf(
                        document,
                        'document.docx',
                        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                        enabled=True,
                        libreoffice_path=sys.executable,
                        timeout=0.01,
                        max_file_size=1024,
                    )

            self.assertTrue(process.killed)
            self.assertEqual(process.calls, 1)
            self.assertTrue(process.terminated.is_set())


if __name__ == '__main__':
    unittest.main()
