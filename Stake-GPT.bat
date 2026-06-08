@echo off
setlocal

cd /d "%~dp0"
title Stake-GPT TUI

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

set "POWERSHELL_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%POWERSHELL_EXE%" (
  echo ERROR: Could not find Windows PowerShell.
  echo Expected: %POWERSHELL_EXE%
  echo.
  pause
  exit /b 1
)

set "STAKE_GPT_ROOT=%CD%"
start "Stake-GPT TUI" "%POWERSHELL_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$Host.UI.RawUI.WindowTitle='Stake-GPT TUI'; $Host.UI.RawUI.BackgroundColor='Black'; $Host.UI.RawUI.ForegroundColor='DarkGray'; Clear-Host; $root=$env:STAKE_GPT_ROOT; Set-Location -LiteralPath $root; & '.\.venv\Scripts\python.exe' -m app.local_helper_tui; $code=$LASTEXITCODE; if ($code -ne 0) { Write-Host ''; Write-Host ('Stake-GPT TUI exited with code {0}.' -f $code); Read-Host 'Press Enter to close' }; exit $code"
exit /b 0
