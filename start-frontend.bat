@echo off
setlocal
title Open WebUI - Frontend

set "ROOT_DIR=%~dp0"
cd /d "%ROOT_DIR%" || exit /b 1

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
if not "%NODE_MAJOR%" == "22" (
    echo Unsupported Node.js version. Open WebUI development requires Node.js 22.x; current version: 1>&2
    node --version 1>&2
    exit /b 1
)

if not exist "node_modules\.bin\vite.cmd" (
    echo Frontend dependencies are not installed. Run npm ci --force before starting. 1>&2
    exit /b 1
)

if "%FRONTEND_PORT%" == "" set "FRONTEND_PORT=5173"
npm run dev -- --port "%FRONTEND_PORT%" --strictPort
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%" == "0" echo Frontend launcher exited with code %EXIT_CODE%. 1>&2
exit /b %EXIT_CODE%
