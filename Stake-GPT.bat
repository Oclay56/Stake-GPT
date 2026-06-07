@echo off
setlocal

cd /d "%~dp0"
title Stake-GPT CLI

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Could not find .venv\Scripts\python.exe
  echo Run setup first, then try this launcher again.
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
  echo ERROR: Could not find .env
  if exist "env" (
    echo Found a file named "env". Rename it to ".env" if it contains your local settings.
  ) else (
    echo Stake-GPT needs local Supabase settings in %CD%\.env
    echo Use .env.example as the template, then fill in your local values.
  )
  echo.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m app.local_helper_cli
set EXIT_CODE=%ERRORLEVEL%

echo.
if not "%EXIT_CODE%"=="0" (
  echo Stake-GPT CLI exited with code %EXIT_CODE%.
)
pause
exit /b %EXIT_CODE%
