@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title Anything Proxy Windows Deploy

cd /d "%~dp0"

echo ==========================================
echo   Anything Proxy - Full Windows Deploy
echo ==========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python was not found in PATH.
    pause
    exit /b 1
)

if not exist ".venv" (
    echo [1/7] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create .venv
        pause
        exit /b 1
    )
) else (
    echo [1/7] Reusing existing .venv
)

echo [2/7] Installing dependencies...
call ".venv\Scripts\python.exe" -m pip install --upgrade pip setuptools wheel
call ".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies
    pause
    exit /b 1
)

if not exist ".env" (
    echo [3/7] Creating .env from .env.example...
    copy /y ".env.example" ".env" >nul
) else (
    echo [3/7] Reusing existing .env
)

set "DEFAULT_PG_URL=postgresql://postgres:2003.0816zcr@127.0.0.1:5432/postgres"
set "DEFAULT_REDIS_URL=redis://127.0.0.1:6379/0"
set "DEFAULT_PORT=3000"
set "DEFAULT_DB_SCHEMA=anything_proxy"

echo [4/7] Writing defaults into .env...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$path = Join-Path '%~dp0' '.env';" ^
  "$content = Get-Content -LiteralPath $path -Raw -Encoding UTF8;" ^
  "$pairs = @{" ^
  "  'DATABASE_URL' = '%DEFAULT_PG_URL%';" ^
  "  'REDIS_URL' = '%DEFAULT_REDIS_URL%';" ^
  "  'PORT' = '%DEFAULT_PORT%';" ^
  "  'DB_SCHEMA' = '%DEFAULT_DB_SCHEMA%';" ^
  "  'DB_POOL_MIN_SIZE' = '1';" ^
  "  'DB_POOL_MAX_SIZE' = '10';" ^
  "  'AUTO_MIGRATE_SQLITE_TO_POSTGRES' = 'true';" ^
  "};" ^
  "foreach ($entry in $pairs.GetEnumerator()) {" ^
  "  $pattern = '(?m)^' + [regex]::Escape($entry.Key) + '=.*$';" ^
  "  if ($content -match $pattern) {" ^
  "    $content = [regex]::Replace($content, $pattern, ($entry.Key + '=' + $entry.Value))" ^
  "  } else {" ^
  "    if ($content.Length -gt 0 -and -not $content.EndsWith([Environment]::NewLine)) { $content += [Environment]::NewLine }" ^
  "    $content += ($entry.Key + '=' + $entry.Value) + [Environment]::NewLine" ^
  "  }" ^
  "}" ^
  "Set-Content -LiteralPath $path -Value $content -Encoding UTF8"
if errorlevel 1 (
    echo [ERROR] Failed to update .env
    pause
    exit /b 1
)

echo [5/7] Validating environment...
call ".venv\Scripts\python.exe" -X utf8 -c "from config import settings; print('DATABASE_URL=' + str(settings.database_url)); print('REDIS_URL=' + str(settings.redis_url)); print('PORT=' + str(settings.port)); print('DB_SCHEMA=' + str(settings.db_schema))"
if errorlevel 1 (
    echo [ERROR] Environment validation failed
    pause
    exit /b 1
)

echo [6/7] Preparing runtime folders...
if not exist "data" mkdir "data"
if not exist "logs" mkdir "logs"

echo [7/7] Starting server...
echo.
call ".venv\Scripts\python.exe" -X utf8 main.py
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Server exited with code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
