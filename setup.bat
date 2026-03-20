@echo off
REM HLA Typing Report Generator — First-time setup (Windows)

cd /d "%~dp0"
echo ==================================================
echo   HLA Typing Report Generator - Setup
echo ==================================================

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Please install Python 3.8 or higher.
    pause
    exit /b 1
)

if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
)

echo Installing dependencies...
call venv\Scripts\activate.bat
pip install --upgrade pip -q
pip install -r requirements.txt -q

echo.
echo Setup complete. Run launch.bat to start the application.
pause
