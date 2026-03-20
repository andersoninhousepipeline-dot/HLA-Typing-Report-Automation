@echo off
REM HLA Typing Report Generator — Launcher (Windows)
SETLOCAL EnableDelayedExpansion

cd /d "%~dp0"

REM Check if virtual environment exists
if not exist venv (
    echo Virtual environment not found. Running setup first...
    echo.
    call setup.bat
    if errorlevel 1 (
        echo.
        echo [ERROR] Setup failed. Please fix the errors above and try again.
        pause
        exit /b 1
    )
)

REM Check if venv activation script exists
if not exist venv\Scripts\activate.bat (
    echo [ERROR] Virtual environment is corrupted
    echo Please delete the 'venv' folder and run setup.bat again
    pause
    exit /b 1
)

REM Activate virtual environment
call venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] Failed to activate virtual environment
    pause
    exit /b 1
)

REM Quick dependency check/update (silent)
pip install -r requirements.txt --quiet 2>nul

REM Check if main script exists
if not exist hla_report_generator.py (
    echo [ERROR] hla_report_generator.py not found
    echo Please ensure you are running this script from the correct directory
    pause
    exit /b 1
)

REM Launch the application
echo ================================================
echo   Starting HLA Typing Report Generator...
echo ================================================
echo.
python hla_report_generator.py

REM Check if the application exited with an error
if errorlevel 1 (
    echo.
    echo [ERROR] Application exited with an error
    echo.
    pause
)
