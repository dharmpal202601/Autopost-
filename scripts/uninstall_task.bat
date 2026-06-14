@echo off
title WA Auto Publisher - Remove Startup Task
echo Removing WA Auto Publisher from Windows Task Scheduler...
echo.

schtasks /delete /tn "WA Auto Publisher" /f

if %ERRORLEVEL% EQU 0 (
    echo SUCCESS: Startup task removed.
    echo The app will no longer start automatically at login.
) else (
    echo Task not found or could not be removed.
    echo (It may not have been installed, or try running as Administrator.)
)

echo.
pause
