@echo off
setlocal enabledelayedexpansion
title PLDT H153-381 Modem Analytics System
cd /d "%~dp0"

echo ======================================================================
echo    PLDT H153-381 Modem WAN Traffic ^& Throttling Analytics System
echo ======================================================================
echo.

:: 1. Check Python installation
set "PY_CMD="
python --version >nul 2>&1
if %errorlevel% equ 0 (
    set "PY_CMD=python"
) else (
    py --version >nul 2>&1
    if %errorlevel% equ 0 (
        set "PY_CMD=py"
    )
)

if "%PY_CMD%"=="" (
    echo [ERROR] Python was not found on this computer.
    echo.
    echo Please install Python 3.9 or higher from https://www.python.org/downloads/
    echo IMPORTANT: Make sure to check the box "Add Python to PATH" during setup!
    echo.
    set /p "OPEN_PY=Would you like to open the Python download page now? [Y/n]: "
    if /i "!OPEN_PY!" neq "n" (
        start https://www.python.org/downloads/
    )
    echo.
    pause
    exit /b 1
)

:: 2. Set up Python virtual environment (.venv)
if not exist ".venv\Scripts\activate.bat" (
    echo [*] Creating Python virtual environment in .venv ...
    %PY_CMD% -m venv .venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment. Trying to continue with global Python...
    )
)

if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
    set "RUN_PY=python"
) else (
    set "RUN_PY=%PY_CMD%"
)

:: 3. Install / Verify dependencies
echo [*] Checking required dependencies...
%RUN_PY% -c "import huawei_lte_api" >nul 2>&1
if %errorlevel% neq 0 (
    echo [*] Installing required packages from requirements.txt ...
    %RUN_PY% -m pip install --quiet --upgrade pip
    %RUN_PY% -m pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
)

:: 4. Check .env configuration
set "NEED_SETUP=0"
if not exist ".env" (
    set "NEED_SETUP=1"
) else (
    findstr /C:"YOUR_MODEM_PASSWORD" .env >nul 2>&1
    if %errorlevel% equ 0 set "NEED_SETUP=1"
    findstr /C:"MODEM_PASS=" .env >nul 2>&1
    if %errorlevel% equ 0 (
        for /f "tokens=2 delims==" %%A in ('findstr "MODEM_PASS=" .env') do (
            if "%%A"=="" set "NEED_SETUP=1"
        )
    )
)

if "!NEED_SETUP!"=="1" (
    echo [*] Initial setup required. Launching setup wizard...
    %RUN_PY% setup_wizard.py
    if not exist ".env" (
        echo [ERROR] Setup was not completed. Exiting...
        pause
        exit /b 1
    )
)

:: 5. Launch Application
echo.
echo [*] Starting PLDT Modem Analytics Engine...
echo.
%RUN_PY% snmp_poller.py

echo.
echo [!] Server stopped.
pause
