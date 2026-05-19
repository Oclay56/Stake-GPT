@echo off
setlocal
cd /d "%~dp0"
echo Starting AZP Local Bridge...
echo Close this window to stop watching for slip jobs.
python -m app.local_slip_bridge watch --open-browser
pause
