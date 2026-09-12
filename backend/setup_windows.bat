@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%" || exit /b 1

if /I not "%WEB_LOADER_ENGINE%" == "playwright" (
    echo WEB_LOADER_ENGINE is not set to playwright; no optional browser setup is required.
    exit /b 0
)

where python >nul 2>&1
if errorlevel 1 (
    echo Python was not found. Activate the Open WebUI virtual environment first. 1>&2
    exit /b 1
)

if "%PLAYWRIGHT_WS_URL%" == "" (
    echo Installing the Playwright Chromium browser...
    python -m playwright install chromium
    if errorlevel 1 (
        echo Playwright Chromium installation failed. 1>&2
        exit /b 1
    )
) else (
    echo PLAYWRIGHT_WS_URL is set; skipping the local Chromium installation.
)

echo Downloading the NLTK punkt_tab data package...
python -c "import nltk; success = nltk.download('punkt_tab'); raise SystemExit(0 if success else 1)"
if errorlevel 1 (
    echo NLTK punkt_tab setup failed. 1>&2
    exit /b 1
)

echo Optional Windows web-loader setup completed successfully.
exit /b 0
