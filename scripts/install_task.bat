@echo off
setlocal EnableDelayedExpansion
title WA Auto Publisher - Install Startup Task
echo ============================================
echo  WA Channel Auto Publisher
echo  Installing Windows Startup Task
echo ============================================
echo.

REM Get project root (one level up from scripts/)
set "SCRIPT_DIR=%~dp0"
set "PROJECT_DIR=%SCRIPT_DIR%.."
for %%i in ("%PROJECT_DIR%") do set "PROJECT_DIR=%%~fi"

echo Project directory: %PROJECT_DIR%

REM Find Python executable - prefer venv, fall back to system
set "PYTHON_EXE=pythonw.exe"
set "PYTHON_DIR="

if exist "%PROJECT_DIR%\venv\Scripts\pythonw.exe" (
    set "PYTHON_EXE=%PROJECT_DIR%\venv\Scripts\pythonw.exe"
    echo Found virtual environment Python
) else if exist "%PROJECT_DIR%\.venv\Scripts\pythonw.exe" (
    set "PYTHON_EXE=%PROJECT_DIR%\.venv\Scripts\pythonw.exe"
    echo Found virtual environment Python
) else (
    REM Try to find system pythonw
    for /f "tokens=*" %%p in ('where pythonw 2^>nul') do (
        set "PYTHON_EXE=%%p"
        goto :found_python
    )
    echo Warning: pythonw.exe not found, using python.exe
    set "PYTHON_EXE=python.exe"
    :found_python
)

echo Python: %PYTHON_EXE%
echo Script: %PROJECT_DIR%\main.py
echo.

REM Remove existing task if present
schtasks /delete /tn "WA Auto Publisher" /f >nul 2>&1

REM Create the scheduled task
REM - Trigger: At user logon
REM - Delay:   30 seconds (let desktop settle)  
REM - Run as:  Current user (required for browser + keyring access)
REM - Priority: Normal

schtasks /create ^
    /tn "WA Auto Publisher" ^
    /tr "\"%PYTHON_EXE%\" \"%PROJECT_DIR%\main.py\"" ^
    /sc ONLOGON ^
    /delay 0000:30 ^
    /ru "%USERDOMAIN%\%USERNAME%" ^
    /rl HIGHEST ^
    /f

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ============================================
    echo  SUCCESS! Startup task created.
    echo.
    echo  The app will start automatically on your
    echo  next Windows login.
    echo.
    echo  Dashboard: http://localhost:5000
    echo ============================================
) else (
    echo.
    echo ============================================
    echo  ERROR: Could not create scheduled task.
    echo.
    echo  Try running this script as Administrator:
    echo  Right-click ^> Run as administrator
    echo ============================================
)

echo.
pause
endlocal
