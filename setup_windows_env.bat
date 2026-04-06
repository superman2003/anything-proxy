@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title Anything Proxy Setup

cd /d "%~dp0"

echo ==========================================
echo   Anything Proxy - Windows Setup
echo ==========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python was not found in PATH.
    pause
    exit /b 1
)

if not exist ".venv" (
    echo [1/5] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create .venv
        pause
        exit /b 1
    )
) else (
    echo [1/5] Reusing existing .venv
)

echo [2/5] Upgrading pip...
call ".venv\Scripts\python.exe" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 (
    echo [ERROR] Failed to upgrade pip tooling
    pause
    exit /b 1
)

echo [3/5] Installing requirements...
call ".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies
    pause
    exit /b 1
)

echo [4/5] Preparing local files...
if not exist ".env" copy /y ".env.example" ".env" >nul
if not exist "data" mkdir "data"
if not exist "logs" mkdir "logs"

echo [5/5] Verifying startup files...
call ".venv\Scripts\python.exe" -X utf8 -m py_compile main.py
if errorlevel 1 (
    echo [ERROR] main.py validation failed
    pause
    exit /b 1
)

echo.
echo [OK] Setup complete.
echo Next step:
echo   start_server.bat
echo.
pause
exit /b 0
