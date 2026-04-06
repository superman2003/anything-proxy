@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title Anything Proxy PostgreSQL Migration

cd /d "%~dp0"

set "PYTHON_CMD=.venv\Scripts\python.exe"
if not exist "%PYTHON_CMD%" (
    echo [1/4] Creating local virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create .venv
        pause
        exit /b 1
    )
)

echo [2/4] Installing migration dependencies...
call ".venv\Scripts\python.exe" -m pip install --upgrade pip setuptools wheel
call ".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

set "PYTHON_CMD=.venv\Scripts\python.exe"

set "SRC_URL="
set "DST_URL="
set "SRC_SCHEMA=public"
set "DST_SCHEMA=anything_proxy"

set /p SRC_URL=Source PostgreSQL URL: 
set /p DST_URL=Target PostgreSQL URL: 
set /p SRC_SCHEMA=Source schema (default public): 
set /p DST_SCHEMA=Target schema (default anything_proxy): 

if "%SRC_SCHEMA%"=="" set "SRC_SCHEMA=public"
if "%DST_SCHEMA%"=="" set "DST_SCHEMA=anything_proxy"

if "%SRC_URL%"=="" (
    echo [ERROR] Source URL is required.
    pause
    exit /b 1
)

if "%DST_URL%"=="" (
    echo [ERROR] Target URL is required.
    pause
    exit /b 1
)

echo [3/4] Running migration...
call "%PYTHON_CMD%" -X utf8 migrate_postgres_data.py --source-url "%SRC_URL%" --target-url "%DST_URL%" --source-schema "%SRC_SCHEMA%" --target-schema "%DST_SCHEMA%"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%EXIT_CODE%"=="0" (
    echo [ERROR] Migration failed with code %EXIT_CODE%.
) else (
    echo [OK] Migration finished.
)
echo [4/4] Done
pause
exit /b %EXIT_CODE%
