@echo off
title Lingua — Fix LLM
cd /d "%~dp0"
call venv\Scripts\activate.bat
set "PATH=%~dp0llama.cpp\cuda;%~dp0llama.cpp;%PATH%"

echo.
echo Removing existing llama-cpp-python...
pip uninstall llama-cpp-python -y

echo.
echo Trying CUDA 13 build...
pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu130 --force-reinstall --quiet
if not errorlevel 1 goto :done

echo CUDA 13 build not found. Trying CUDA 12.5...
pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu125 --force-reinstall --quiet
if not errorlevel 1 goto :done

echo Trying CUDA 12.4...
pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124 --force-reinstall --quiet
if not errorlevel 1 goto :done

echo.
echo All GPU builds failed. Installing CPU-only build...
echo ^(LLM will still work, just slower^)
pip install llama-cpp-python --force-reinstall

:done
echo.
echo Done. Run run.cmd to start Lingua.
pause
