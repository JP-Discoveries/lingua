@echo off
title Lingua — Install
cd /d "%~dp0"

echo.
echo  =========================================
echo   Lingua — First-time setup
echo  =========================================
echo.

:: ── Create virtual environment ──────────────────────────────────────────────
if not exist "venv" (
    echo [1/3] Creating virtual environment...
    py -3.11 -m venv venv
    if errorlevel 1 (
        echo ERROR: Could not create venv. Is Python installed and on PATH?
        pause & exit /b 1
    )
    echo       Done.
    echo.
) else (
    echo [1/3] Virtual environment already exists, skipping.
    echo.
)

:: ── Install base requirements ────────────────────────────────────────────────
echo [2/3] Installing requirements...
call venv\Scripts\activate.bat
pip install --upgrade pip --quiet
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: pip install failed. Check requirements.txt.
    pause & exit /b 1
)
echo       Done.
echo.

:: ── llama-cpp-python (GPU build) ────────────────────────────────────────────
echo [3/3] Installing llama-cpp-python with CUDA 12.4 GPU support...
pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124 --quiet
if errorlevel 1 (
    echo.
    echo   WARNING: GPU build failed. Falling back to CPU-only build.
    echo   The LLM will be slower but still functional.
    echo.
    pip install llama-cpp-python --quiet
)
echo       Done.
echo.

:: ── Download offline word data ───────────────────────────────────────────────
echo Downloading offline word data (WordNet + CMU dict)...
python setup.py
echo.

echo  =========================================
echo   Installation complete!
echo   Run Lingua with:  run.cmd
echo  =========================================
echo.
pause
