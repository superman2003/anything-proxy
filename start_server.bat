@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title Anything Proxy Start

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python was not found in PATH.
    pause
    exit /b 1
)

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

echo [2/4] Ensuring dependencies...
call ".venv\Scripts\python.exe" -m pip install --upgrade pip setuptools wheel
call ".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies
    pause
    exit /b 1
)

set "PYTHON_CMD=.venv\Scripts\python.exe"

echo [3/4] Preparing local files...
if not exist ".env" copy /y ".env.example" ".env" >nul

if not exist "logs" mkdir "logs"
if not exist "data" mkdir "data"

echo [4/4] Starting Anything Proxy...
echo Starting Anything Proxy...
echo.
call "%PYTHON_CMD%" -X utf8 main.py
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Server exited with code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
