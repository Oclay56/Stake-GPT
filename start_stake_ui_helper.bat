@echo off
setlocal

cd /d "%~dp0"
title AZP Stake UI Helper

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Could not find .venv\Scripts\python.exe
  echo Run the project setup first, then try this launcher again.
  echo.
  pause
  exit /b 1
)

if not exist ".env" (
  echo ERROR: Could not find .env
  echo The helper needs local Supabase settings in C:\Users\farne\Desktop\AZP\.env
  echo.
  pause
  exit /b 1
)

if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" -m app.local_helper_gui
) else (
  start "" ".venv\Scripts\python.exe" -m app.local_helper_gui
)

exit /b 0
