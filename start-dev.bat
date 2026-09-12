@echo off
setlocal EnableDelayedExpansion
title Open WebUI Launcher

set "ROOT_DIR=%~dp0"
cd /d "%ROOT_DIR%" || exit /b 1

if "%PORT%" == "" set "PORT=8080"
if "%FRONTEND_PORT%" == "" set "FRONTEND_PORT=5173"
if "%STARTUP_TIMEOUT_SECONDS%" == "" set "STARTUP_TIMEOUT_SECONDS=120"
set /a STARTUP_TIMEOUT_SECONDS=STARTUP_TIMEOUT_SECONDS+0 >nul 2>&1
if !STARTUP_TIMEOUT_SECONDS! LEQ 0 set "STARTUP_TIMEOUT_SECONDS=120"

if not exist ".venv\Scripts\activate.bat" (
    echo Open WebUI virtual environment not found. Run uv sync --frozen --python 3.11 first. 1>&2
    exit /b 1
)

where node >nul 2>&1
if errorlevel 1 (
    echo Node.js was not found on PATH. Install Node.js 22.x first. 1>&2
    exit /b 1
)

where npm >nul 2>&1
if errorlevel 1 (
    echo npm was not found on PATH. Install Node.js 22.x first. 1>&2
    exit /b 1
)

set "NODE_MAJOR="
for /f "tokens=1 delims=." %%V in ('node -p "process.versions.node" 2^>nul') do set "NODE_MAJOR=%%V"
if not "!NODE_MAJOR!" == "22" (
    echo Unsupported Node.js version. Open WebUI development requires Node.js 22.x; current version: 1>&2
    node --version 1>&2
    exit /b 1
)

if not exist "node_modules\.bin\vite.cmd" (
    echo Frontend dependencies are not installed. Run npm ci --force before starting. 1>&2
    exit /b 1
)

where curl.exe >nul 2>&1
if errorlevel 1 (
    echo curl.exe is required for startup readiness checks. 1>&2
    exit /b 1
)

set "BACKEND_URL=http://127.0.0.1:%PORT%/health"
set "FRONTEND_URL=http://127.0.0.1:%FRONTEND_PORT%/"

curl.exe --silent --fail --connect-timeout 1 --max-time 2 "%BACKEND_URL%" >nul 2>&1
if not errorlevel 1 (
    echo A server is already responding at %BACKEND_URL%. Stop it before using this launcher. 1>&2
    exit /b 1
)

curl.exe --silent --fail --connect-timeout 1 --max-time 2 "%FRONTEND_URL%" >nul 2>&1
if not errorlevel 1 (
    echo A server is already responding at %FRONTEND_URL%. Stop it before using this launcher. 1>&2
    exit /b 1
)

echo ====================================================
echo       Open WebUI - Starting Application...
echo ====================================================
echo Launching backend on port %PORT%...
start "Open WebUI Backend" /D "%ROOT_DIR%" cmd.exe /c "call start-backend.bat || (echo. & echo Backend startup failed. & pause)"
if errorlevel 1 (
    echo Failed to launch the backend window. 1>&2
    exit /b 1
)

echo Launching frontend on port %FRONTEND_PORT%...
start "Open WebUI Frontend" /D "%ROOT_DIR%" cmd.exe /c "call start-frontend.bat || (echo. & echo Frontend startup failed. & pause)"
if errorlevel 1 (
    echo Failed to launch the frontend window. 1>&2
    exit /b 1
)

set "BACKEND_READY=0"
set "FRONTEND_READY=0"
set "ELAPSED=0"

:wait_for_servers
if "!BACKEND_READY!" == "0" (
    curl.exe --silent --fail --connect-timeout 1 --max-time 2 "%BACKEND_URL%" >nul 2>&1
    if not errorlevel 1 (
        set "BACKEND_READY=1"
        echo Backend is ready:  %BACKEND_URL%
    )
)

if "!FRONTEND_READY!" == "0" (
    curl.exe --silent --fail --connect-timeout 1 --max-time 2 "%FRONTEND_URL%" >nul 2>&1
    if not errorlevel 1 (
        set "FRONTEND_READY=1"
        echo Frontend is ready: %FRONTEND_URL%
    )
)

if "!BACKEND_READY!!FRONTEND_READY!" == "11" goto servers_ready
if !ELAPSED! GEQ !STARTUP_TIMEOUT_SECONDS! goto startup_timeout

timeout /t 1 /nobreak >nul
set /a ELAPSED+=1
goto wait_for_servers

:startup_timeout
echo Startup did not complete within !STARTUP_TIMEOUT_SECONDS! seconds. 1>&2
if "!BACKEND_READY!" == "0" echo Backend is not ready at %BACKEND_URL%. 1>&2
if "!FRONTEND_READY!" == "0" echo Frontend is not ready at %FRONTEND_URL%. 1>&2
echo Check the backend and frontend windows for the underlying error. 1>&2
exit /b 1

:servers_ready
echo.
echo Open WebUI is ready.
echo - Backend API:  http://localhost:%PORT%
echo - Frontend Web: http://localhost:%FRONTEND_PORT%
exit /b 0
