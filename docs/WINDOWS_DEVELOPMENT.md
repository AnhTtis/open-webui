# Windows development

This runbook is for the source checkout at `D:\ClaudeExperience\Memory\open-webui`.

## Requirements

- Windows 11 or another supported Windows environment.
- Python 3.11 (the project supports Python 3.11 and 3.12; this runbook standardizes on 3.11).
- `uv` on `PATH`.
- Node.js 22.x. Node 24 is outside the range declared in `package.json`.
- npm.
- `curl.exe` for `start-dev.bat` readiness checks.
- LibreOffice only if server-side DOCX/XLS/XLSX PDF preview is required.

## First-time setup

Run from PowerShell:

```powershell
Set-Location 'D:\ClaudeExperience\Memory\open-webui'
py -3.11 --version
node --version  # must report v22.x
uv sync --frozen --python 3.11
npm ci --force
if (-not (Test-Path '.env')) { Copy-Item '.env.example' '.env' }
npm run pyodide:prepare
```

Do not overwrite an existing `.env`. Do not print `.env` or `.webui_secret_key` contents in logs or support reports.

`npm ci --force` is intentional for the current dependency tree. Avoid substituting an unreviewed `npm install`, which can update dependency resolution.

## Daily development startup

```powershell
.\start-dev.bat
```

The launcher opens separate backend and frontend windows and waits for:

- backend health: `http://127.0.0.1:8080/health`
- frontend: `http://127.0.0.1:5173/`

Set `PORT`, `FRONTEND_PORT` or `STARTUP_TIMEOUT_SECONDS` before running the launcher when defaults are unsuitable.

Normal startup does not prepare Pyodide, install Playwright Chromium or download NLTK data.

## Run backend and frontend separately

Use two terminals:

```powershell
.\start-backend.bat
```

```powershell
.\start-frontend.bat
```

The backend launcher activates `.venv`, enters `backend` and starts Uvicorn. The frontend launcher requires the Vite executable under `node_modules`.

## Optional Playwright web-loader setup

Only when `WEB_LOADER_ENGINE=playwright` is intended:

```powershell
$env:WEB_LOADER_ENGINE = 'playwright'
.\.venv\Scripts\Activate.ps1
.\backend\setup_windows.bat
```

If `PLAYWRIGHT_WS_URL` is set, local Chromium installation is skipped. This setup is not part of ordinary backend startup.

## Optional LibreOffice preview

LibreOffice preview is disabled by default in `.env.example`. After an operator installs LibreOffice from an approved source, configure:

```powershell
$env:ENABLE_LIBREOFFICE_PREVIEW = 'true'
$env:LIBREOFFICE_PATH = 'C:\Program Files\LibreOffice\program\soffice.exe'
.\start-backend.bat
```

Equivalent `.env` values may be used. Never install LibreOffice during a request or normal application startup.

When preview is disabled, missing or fails, DOCX and spreadsheets use the browser fallback. Download still returns the canonical original Office binary.

## Production-like local run

Build with Node 22:

```powershell
npm run build
.\start-backend.bat
```

Open `http://127.0.0.1:8080`. The backend serves the built frontend; do not also run the Vite development server unless testing development behavior.

## Verification commands

```powershell
# Backend focused tests
$env:PYTHONPATH = 'backend'
.\.venv\Scripts\python.exe -m pytest backend/tests

# Frontend tests (non-watch)
npm run test:frontend

# Frontend lint and type/Svelte check
npm run lint:frontend
npm run check

# Production build
npm run build

# Diff whitespace
# Run only when the checkout is inside a Git work tree.
git diff --check
```

The focused feature baseline recorded on 2026-09-11 is 43 backend tests with 333 subtests and 10 frontend tests. Broader release gates are documented in [`../OPEN_WEBUI_RELEASE_GATES.md`](../OPEN_WEBUI_RELEASE_GATES.md).

## Database URL note

Open WebUI startup migrations use a synchronous SQLAlchemy URL. For local SQLite use:

```text
sqlite:///absolute/or/resolved/path/to/webui.db
```

Do not use `sqlite+aiosqlite:///...` as the migration/startup URL; Alembic's synchronous connection path will fail with `MissingGreenlet`.

## Protected local data

Before cleanup or reset, read [`../OPEN_WEBUI_LOCAL_DATA_AND_PROTECTION.md`](../OPEN_WEBUI_LOCAL_DATA_AND_PROTECTION.md). In particular, do not delete `backend/data`, `.env`, `.webui_secret_key`, `.venv`, `node_modules` or canonical uploads as a cache-cleanup shortcut.

## Troubleshooting

### Unsupported Node version

Install/use Node 22.x, reopen the terminal and run `node --version` again. Do not treat a Node 24 result as the supported build environment.

### Port already in use

Stop the existing service or set another port before launching:

```powershell
$env:PORT = '8081'
$env:FRONTEND_PORT = '5174'
.\start-dev.bat
```

### Backend environment missing

Recreate dependencies with:

```powershell
uv sync --frozen --python 3.11
```

### Frontend dependencies missing

Run:

```powershell
npm ci --force
```

### Office preview unavailable

Leave `ENABLE_LIBREOFFICE_PREVIEW=false` until `LIBREOFFICE_PATH` points to a valid approved installation. The browser fallback remains available.
