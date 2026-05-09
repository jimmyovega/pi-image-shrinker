@echo off
:: ============================================================
::  build.bat - Build PiImageShrinker.exe with PyInstaller
::
::  Requirements:
::    - Python 3.8+ on PATH  (https://python.org)
::    - pip (comes with Python)
::
::  Output: dist\PiImageShrinker.exe  (single portable .exe)
:: ============================================================
setlocal EnableDelayedExpansion

echo.
echo ============================================================
echo   Pi Image Shrinker - Build Script
echo ============================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found on PATH.
    echo         Download and install Python 3.8+ from https://python.org
    echo         Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [1/3] Python found: %PYVER%

echo.
echo [2/3] Checking for PyInstaller...
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo       PyInstaller not found. Installing via pip...
    pip install pyinstaller
    if errorlevel 1 (
        echo [ERROR] Failed to install PyInstaller.
        pause
        exit /b 1
    )
    echo       PyInstaller installed successfully.
) else (
    for /f "tokens=*" %%v in ('python -m PyInstaller --version 2^>^&1') do set PIVER=%%v
    echo       PyInstaller is already installed: v%PIVER%
)

echo.
echo [3/3] Building PiImageShrinker.exe...
echo.

python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name PiImageShrinker ^
    --clean ^
    pi_shrinker.py

if errorlevel 1 (
    echo.
    echo [ERROR] PyInstaller build failed.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Build complete!
echo ============================================================
echo.
echo   Executable: dist\PiImageShrinker.exe
echo.
pause
