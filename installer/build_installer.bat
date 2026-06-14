@echo off
title WA Auto Publisher - Build Installer
cd /d "%~dp0"
echo ============================================
echo  WA Channel Auto Publisher
echo  Building Windows Installer
echo ============================================
echo.
echo Requirements:
echo   - Inno Setup 6 installed
echo   - All project files present
echo.

REM Find Inno Setup ISCC compiler
set "ISCC="
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" (
    set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
)
if exist "C:\Program Files\Inno Setup 6\ISCC.exe" (
    set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
)

if not defined ISCC (
    echo ERROR: Inno Setup 6 not found.
    echo Download free from: https://jrsoftware.org/isinfo.php
    echo Install it, then run this script again.
    pause
    exit /b 1
)

echo Found Inno Setup: %ISCC%
echo.

REM Create output directory
if not exist ".\output" mkdir ".\output"

REM Build the installer
echo Building installer...
"%ISCC%" setup.iss

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ============================================
    echo  SUCCESS!
    echo  Installer created in: installer\output\
    echo ============================================
    explorer ".\output"
) else (
    echo.
    echo ============================================
    echo  BUILD FAILED. Check errors above.
    echo ============================================
)

pause
