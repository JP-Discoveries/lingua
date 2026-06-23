@echo off
title Lingua — Build GPU LLM
cd /d "%~dp0"

echo.
echo ══════════════════════════════════════════════════════════════
echo  Lingua — Build llama-cpp-python with CUDA 13 GPU support
echo  This runs once and takes 5-15 minutes.
echo ══════════════════════════════════════════════════════════════
echo.

:: ── Check for MSVC ────────────────────────────────────────────────────────────
where cl.exe >nul 2>&1
if not errorlevel 1 goto :has_compiler

:: Look for VS Build Tools vcvarsall
set "VCVARS="
for %%P in (
    "C:\Program Files\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat"
    "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvarsall.bat"
    "C:\Program Files\Microsoft Visual Studio\2022\Professional\VC\Auxiliary\Build\vcvarsall.bat"
    "C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\VC\Auxiliary\Build\vcvarsall.bat"
) do (
    if exist "%%~P" set "VCVARS=%%~P"
)

if defined VCVARS (
    echo [build] Found Visual Studio at %VCVARS%
    call "%VCVARS%" x64
    goto :has_compiler
)

echo.
echo [build] Microsoft C++ Build Tools not found.
echo         Download the free installer from:
echo.
echo           https://aka.ms/vs/17/release/vs_BuildTools.exe
echo.
echo         Run it, select "Desktop development with C++", install,
echo         then re-run this script.
echo.
pause
exit /b 1

:has_compiler
echo [build] C++ compiler found.

:: ── Check for CUDA ────────────────────────────────────────────────────────────
where nvcc.exe >nul 2>&1
if errorlevel 1 (
    echo [build] nvcc not found — CUDA toolkit must be on PATH.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('nvcc --version 2^>^&1 ^| findstr "release"') do echo [build] %%v

:: ── Activate venv ─────────────────────────────────────────────────────────────
call venv\Scripts\activate.bat

:: ── Build ─────────────────────────────────────────────────────────────────────
echo.
echo [build] Uninstalling existing llama-cpp-python...
pip uninstall llama-cpp-python -y

echo.
echo [build] Building from source with CUDA 13 (this takes 5-15 minutes)...
echo.

set CMAKE_ARGS=-DGGML_CUDA=on -DCMAKE_CUDA_ARCHITECTURES=all-major
pip install llama-cpp-python --no-binary llama-cpp-python --force-reinstall

if errorlevel 1 (
    echo.
    echo ══════════════════════════════════════════════════════════════
    echo  Build failed. Common fixes:
    echo   - Make sure "Desktop development with C++" is installed in
    echo     Visual Studio / Build Tools
    echo   - Run this script from a normal command prompt (not admin)
    echo ══════════════════════════════════════════════════════════════
    pause
    exit /b 1
)

echo.
echo ══════════════════════════════════════════════════════════════
echo  GPU build complete! Run run.cmd to start Lingua.
echo ══════════════════════════════════════════════════════════════
echo.
pause
