@echo off
setlocal

cd /d "%~dp0"
title Stake-GPT CLI

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Could not find .venv\Scripts\python.exe
  echo Run the project setup first, then try this launcher again.
  echo.
  pause
  exit /b 1
)

if not exist ".env" (
  echo ERROR: Could not find .env
  echo Stake-GPT needs local Supabase settings in C:\Users\farne\Desktop\AZP\.env
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
