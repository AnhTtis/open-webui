@echo off
setlocal
title Open WebUI - Backend

set "ROOT_DIR=%~dp0"
cd /d "%ROOT_DIR%" || exit /b 1

if not exist ".venv\Scripts\activate.bat" (
    echo Open WebUI virtual environment not found at "%ROOT_DIR%.venv". 1>&2
    echo Create the environment and install backend dependencies before starting. 1>&2
    exit /b 1
)

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo Failed to activate the Open WebUI virtual environment. 1>&2
    exit /b 1
)

pushd "backend" || exit /b 1
call "start_windows.bat"
set "EXIT_CODE=%ERRORLEVEL%"
popd

if not "%EXIT_CODE%" == "0" echo Backend launcher exited with code %EXIT_CODE%. 1>&2
exit /b %EXIT_CODE%
