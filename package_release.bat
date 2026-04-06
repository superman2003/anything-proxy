@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title Anything Proxy Release Packager

cd /d "%~dp0"

set "DIST_DIR=%CD%\dist"
set "STAGING_DIR=%DIST_DIR%\anything-proxy-release"
set "ZIP_PATH=%DIST_DIR%\anything-proxy-release.zip"

if not exist "%DIST_DIR%" mkdir "%DIST_DIR%"
if exist "%STAGING_DIR%" rmdir /s /q "%STAGING_DIR%"
mkdir "%STAGING_DIR%"

echo [1/4] Copying project files...
robocopy "%CD%" "%STAGING_DIR%" /E /NFL /NDL /NJH /NJS /NP ^
  /XD ".git" ".venv" "__pycache__" "logs" "data" "dist" ".claude" ^
  /XF "*.pyc" "*.pyo" "*.log" "tmp_*.py"
if errorlevel 8 (
    echo [ERROR] Failed to stage release files.
    pause
    exit /b 1
)

echo [2/4] Cleaning old zip...
if exist "%ZIP_PATH%" del /f /q "%ZIP_PATH%"

echo [3/4] Creating zip...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Compress-Archive -Path '%STAGING_DIR%\*' -DestinationPath '%ZIP_PATH%' -Force"
if errorlevel 1 (
    echo [ERROR] Failed to create zip package.
    pause
    exit /b 1
)

echo [4/4] Done
echo Release package created:
echo   %ZIP_PATH%
echo.
pause
exit /b 0
