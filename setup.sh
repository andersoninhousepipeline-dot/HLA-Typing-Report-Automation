#!/bin/bash
# HLA Typing Report Generator — First-time setup script (Linux/macOS)

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=================================================="
echo "  HLA Typing Report Generator — Setup"
echo "=================================================="

# Check Python 3.8+
if ! command -v python3 &>/dev/null; then
    echo "ERROR: Python 3 not found. Please install Python 3.8 or higher."
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "Python version: $PY_VERSION"

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate venv and install dependencies
echo "Installing dependencies..."
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

echo ""
echo "✓ Setup complete. Run ./launch.sh to start the application."
