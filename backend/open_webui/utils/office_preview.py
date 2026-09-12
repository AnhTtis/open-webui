import asyncio
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from open_webui.env import (
    ENABLE_LIBREOFFICE_PREVIEW,
    LIBREOFFICE_PATH,
    LIBREOFFICE_PREVIEW_MAX_FILE_SIZE,
    LIBREOFFICE_PREVIEW_TIMEOUT,
)

OFFICE_CONTENT_TYPES = {
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.xls': 'application/vnd.ms-excel',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
}
_GENERIC_CONTENT_TYPES = {
    '',
    'application/octet-stream',
    'application/zip',
    'application/x-ole-storage',
}
_OLE_HEADER = bytes.fromhex('D0CF11E0A1B11AE1')


class OfficePreviewError(Exception):
    """Base error for office document preview generation."""


class OfficePreviewDisabledError(OfficePreviewError):
    pass


class OfficePreviewUnavailableError(OfficePreviewError):
    pass


class OfficePreviewTypeError(OfficePreviewError):
    pass


class OfficePreviewTooLargeError(OfficePreviewError):
    pass


class OfficePreviewTimeoutError(OfficePreviewError):
    pass


class OfficePreviewConversionError(OfficePreviewError):
    pass


def _resolve_executable(configured_path: str) -> str:
    executable = configured_path.strip()
    if not executable:
        raise OfficePreviewUnavailableError('LibreOffice executable is not configured')

    if os.path.dirname(executable):
        path = Path(executable).expanduser()
        if path.is_file():
            return str(path)
        raise OfficePreviewUnavailableError('LibreOffice executable is unavailable')

    resolved = shutil.which(executable)
    if not resolved:
        raise OfficePreviewUnavailableError('LibreOffice executable is unavailable')
    return resolved


def _validate_ooxml(path: Path, suffix: str) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        raise OfficePreviewTypeError('The uploaded file is not a valid Office document') from exc

    required_member = 'word/document.xml' if suffix == '.docx' else 'xl/workbook.xml'
    if '[Content_Types].xml' not in names or required_member not in names:
        raise OfficePreviewTypeError('The uploaded file does not match its Office document type')


def validate_office_document(
    path: Path,
    filename: str,
    content_type: str | None,
    max_file_size: int = LIBREOFFICE_PREVIEW_MAX_FILE_SIZE,
) -> str:
    suffix = Path(filename).suffix.lower()
    expected_content_type = OFFICE_CONTENT_TYPES.get(suffix)
    if not expected_content_type:
        raise OfficePreviewTypeError('Only DOCX, XLS, and XLSX files can be previewed')

    normalized_content_type = (content_type or '').split(';', 1)[0].strip().lower()
    if normalized_content_type not in _GENERIC_CONTENT_TYPES and normalized_content_type != expected_content_type:
        raise OfficePreviewTypeError('The uploaded file content type does not match its extension')

    try:
        stat = path.stat()
    except OSError as exc:
        raise OfficePreviewTypeError('The uploaded file is unavailable') from exc

    if not path.is_file():
        raise OfficePreviewTypeError('The uploaded file is unavailable')
    if stat.st_size <= 0:
        raise OfficePreviewTypeError('The uploaded file is empty')
    if stat.st_size > max_file_size:
        raise OfficePreviewTooLargeError('The uploaded file is too large to preview')

    if suffix in {'.docx', '.xlsx'}:
        _validate_ooxml(path, suffix)
    else:
        try:
            with path.open('rb') as source:
                header = source.read(len(_OLE_HEADER))
        except OSError as exc:
            raise OfficePreviewTypeError('The uploaded file is unavailable') from exc
        if header != _OLE_HEADER:
            raise OfficePreviewTypeError('The uploaded file is not a valid XLS document')

    return suffix


async def _terminate_process(
    process: asyncio.subprocess.Process,
    communicate_task: asyncio.Task[tuple[bytes, bytes]] | None = None,
) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        if communicate_task is not None:
            await communicate_task
        else:
            await process.communicate()
    except Exception:
        # communicate() is still awaited so the child is reaped after kill.
        pass


async def convert_office_document_to_pdf(
    path: Path,
    filename: str,
    content_type: str | None,
    *,
    enabled: bool = ENABLE_LIBREOFFICE_PREVIEW,
    libreoffice_path: str = LIBREOFFICE_PATH,
    timeout: float = LIBREOFFICE_PREVIEW_TIMEOUT,
    max_file_size: int = LIBREOFFICE_PREVIEW_MAX_FILE_SIZE,
) -> bytes:
    if not enabled:
        raise OfficePreviewDisabledError('Office document previews are disabled')

    suffix = await asyncio.to_thread(validate_office_document, path, filename, content_type, max_file_size)
    executable = await asyncio.to_thread(_resolve_executable, libreoffice_path)

    with tempfile.TemporaryDirectory(prefix='open-webui-office-preview-') as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        profile_dir = temp_dir / 'profile'
        output_dir = temp_dir / 'output'
        profile_dir.mkdir()
        output_dir.mkdir()

        input_path = temp_dir / f'document{suffix}'
        await asyncio.to_thread(shutil.copyfile, path, input_path)

        process = await asyncio.create_subprocess_exec(
            executable,
            '--headless',
            '--nologo',
            '--nodefault',
            '--nolockcheck',
            '--nofirststartwizard',
            f'-env:UserInstallation={profile_dir.resolve().as_uri()}',
            '--convert-to',
            'pdf',
            '--outdir',
            str(output_dir),
            str(input_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        communicate_task = asyncio.create_task(process.communicate())
        try:
            stdout, stderr = await asyncio.wait_for(asyncio.shield(communicate_task), timeout=timeout)
        except TimeoutError as exc:
            await _terminate_process(process, communicate_task)
            raise OfficePreviewTimeoutError('LibreOffice preview conversion timed out') from exc
        except BaseException:
            await _terminate_process(process, communicate_task)
            raise

        output_path = output_dir / 'document.pdf'
        if process.returncode != 0 or not output_path.is_file():
            detail = (stderr or stdout or b'').decode('utf-8', errors='replace').strip()
            raise OfficePreviewConversionError(detail[:500] or 'LibreOffice could not convert the document')

        output_size = output_path.stat().st_size
        if output_size <= 0 or output_size > max_file_size:
            raise OfficePreviewConversionError('The generated PDF has an invalid size')

        pdf = await asyncio.to_thread(output_path.read_bytes)
        if not pdf.startswith(b'%PDF-'):
            raise OfficePreviewConversionError('LibreOffice produced an invalid PDF')
        return pdf
