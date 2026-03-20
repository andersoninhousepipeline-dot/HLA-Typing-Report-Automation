@echo off
REM HLA Typing Report Generator — First-time setup (Windows)
SETLOCAL EnableDelayedExpansion

cd /d "%~dp0"
echo ==================================================
echo   HLA Typing Report Generator - Setup
echo ==================================================
echo.

REM Check if Python is installed
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH
    echo.
    echo Please install Python 3.8 or higher from:
    echo https://www.python.org/downloads/
    echo.
    echo Make sure to check "Add Python to PATH" during installation
    pause
    exit /b 1
)

REM Check Python version
for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PY_VERSION=%%i
echo Found Python version: %PY_VERSION%
echo.

REM Check if venv module is available
python -m venv --help >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python venv module not available
    echo Please reinstall Python with the venv module enabled
    pause
    exit /b 1
)

REM Create virtual environment if it doesn't exist
if not exist venv (
    echo [1/3] Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment
        pause
        exit /b 1
    )
    echo       Virtual environment created successfully
) else (
    echo [1/3] Virtual environment already exists
)
echo.

REM Activate virtual environment
echo [2/3] Activating virtual environment...
call venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] Failed to activate virtual environment
    pause
    exit /b 1
)
echo       Virtual environment activated
echo.

REM Upgrade pip
echo [3/3] Installing dependencies...
echo       Upgrading pip...
python -m pip install --upgrade pip --quiet
if errorlevel 1 (
    echo [WARNING] Failed to upgrade pip, continuing anyway...
)

REM Check if requirements.txt exists
if not exist requirements.txt (
    echo [ERROR] requirements.txt not found
    echo Please ensure requirements.txt is in the same directory as this script
    pause
    exit /b 1
)

REM Install dependencies
echo       Installing packages from requirements.txt...
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to install dependencies
    echo.
    echo Trying again with verbose output...
    pip install -r requirements.txt
    pause
    exit /b 1
)

echo.
echo ==================================================
echo   Setup Complete!
echo ==================================================
echo.
echo Next steps:
echo   1. Double-click launch.bat to start the application
echo   2. Or run: launch.bat from this directory
echo.
pause
