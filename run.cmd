@echo off
title Lingua
cd /d "%~dp0"

:: ── Ensure venv exists ───────────────────────────────────────────────────────
if not exist "venv\Scripts\activate.bat" (
    echo [Lingua] No virtual environment found.
    echo          Run install.cmd first to set up Lingua.
    echo.
    pause
    exit /b 1
)

:: ── Activate venv ────────────────────────────────────────────────────────────
call venv\Scripts\activate.bat

:: ── Add local CUDA 13 DLLs to PATH so llama.dll can find its dependencies ───
set "PATH=%~dp0llama.cpp\cuda;%~dp0llama.cpp;%PATH%"

echo [Lingua] Python: %VIRTUAL_ENV%
echo [Lingua] Starting...
echo.

:: ── Download word data on first run ─────────────────────────────────────────
if not exist "data\nltk_data" (
    echo [Lingua] Downloading offline word data...
    python setup.py
    if errorlevel 1 (
        echo.
        echo [Lingua] Setup failed. Try running install.cmd again.
        pause
        exit /b 1
    )
    echo.
)

:: ── Launch ───────────────────────────────────────────────────────────────────
python main.py
if errorlevel 1 (
    echo.
    echo ══════════════════════════════════════════
    echo  Lingua crashed or failed to start.
    echo  Check the error above for details.
    echo ══════════════════════════════════════════
    echo.
    pause
)
