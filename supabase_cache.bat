@echo off
setlocal

cd /d "%~dp0"
title Stake-GPT Cache Cleanup

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Could not find .venv\Scripts\python.exe
  echo Run setup first, then try this cleanup again.
  echo.
  if exist ".tools\uv\uv.exe" (
    echo Suggested setup command:
    echo   .\.tools\uv\uv.exe venv .venv --python 3.13
    echo   .\.tools\uv\uv.exe pip install -r requirements-local.txt
  ) else (
    echo Install Python 3.13 or uv, then create .venv and install requirements-local.txt.
  )
  echo.
  pause
  exit /b 1
)

if not exist ".env" (
  echo WARNING: Could not find .env
  if exist "env" (
    echo Found a file named "env". Rename it to ".env" if it contains your local settings.
  ) else (
    echo Supabase cleanup needs local settings in %CD%\.env
    echo Use .env.example as the template, then fill in your local values.
  )
  echo.
  echo Running local-only cleanup for rebuildable cache/temp files.
  ".venv\Scripts\python.exe" -m app.supabase_cache --local-only --root-dir "%CD%" %*
  set EXIT_CODE=%ERRORLEVEL%
  echo.
  if not "%EXIT_CODE%"=="0" (
    echo Local cleanup failed with code %EXIT_CODE%.
  )
  pause
  exit /b %EXIT_CODE%
)

".venv\Scripts\python.exe" -m app.supabase_cache --root-dir "%CD%" %*
set EXIT_CODE=%ERRORLEVEL%

echo.
if not "%EXIT_CODE%"=="0" (
  echo Cleanup failed with code %EXIT_CODE%.
)
pause
exit /b %EXIT_CODE%
