@echo off
setlocal
cd /d "%~dp0"
set AZP_BRIDGE_UI_MODE=click
set AZP_BRIDGE_HEADLESS=0
echo Starting AZP Local Bridge in guarded click mode...
echo It may click exact matched Stake legs only. It will not enter wager amounts or submit bets.
echo Close this window to stop watching for slip jobs.
python -m app.local_slip_bridge watch --ui-mode click
pause
