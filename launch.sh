#!/bin/bash
# HLA Typing Report Generator — Launcher (Linux/macOS)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Auto-setup if venv missing
if [ ! -d "venv" ]; then
    echo "Virtual environment not found. Running setup..."
    bash setup.sh
fi

# Activate venv
source venv/bin/activate

# Install/update dependencies if requirements changed
pip install -r requirements.txt -q 2>/dev/null

# Launch app
echo "Starting HLA Typing Report Generator..."
python3 hla_report_generator.py
