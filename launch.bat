@echo off
REM HLA Typing Report Generator — Launcher (Windows)

cd /d "%~dp0"

if not exist venv (
    echo Virtual environment not found. Running setup...
    call setup.bat
)

call venv\Scripts\activate.bat
pip install -r requirements.txt -q 2>nul

echo Starting HLA Typing Report Generator...
python hla_report_generator.py
