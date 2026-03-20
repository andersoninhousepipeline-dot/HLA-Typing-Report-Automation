#!/bin/bash
# HLA Typing Report Generator — Launcher (Linux/macOS)

# Exit on error
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Auto-setup if venv missing
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}Virtual environment not found. Running setup first...${NC}"
    echo ""
    bash setup.sh
    if [ $? -ne 0 ]; then
        echo ""
        echo -e "${RED}[ERROR]${NC} Setup failed. Please fix the errors above and try again."
        exit 1
    fi
fi

# Check if venv activation script exists
if [ ! -f "venv/bin/activate" ]; then
    echo -e "${RED}[ERROR]${NC} Virtual environment is corrupted"
    echo "Please delete the 'venv' folder and run ./setup.sh again"
    echo ""
    echo "  rm -rf venv && ./setup.sh"
    exit 1
fi

# Activate venv
source venv/bin/activate
if [ $? -ne 0 ]; then
    echo -e "${RED}[ERROR]${NC} Failed to activate virtual environment"
    exit 1
fi

# Quick dependency check/update (silent)
pip install -r requirements.txt --quiet 2>/dev/null || true

# Check if main script exists
if [ ! -f "hla_report_generator.py" ]; then
    echo -e "${RED}[ERROR]${NC} hla_report_generator.py not found"
    echo "Please ensure you are running this script from the correct directory"
    exit 1
fi

# Launch app
echo "================================================"
echo "  Starting HLA Typing Report Generator..."
echo "================================================"
echo ""

python3 hla_report_generator.py

# Check exit status
if [ $? -ne 0 ]; then
    echo ""
    echo -e "${RED}[ERROR]${NC} Application exited with an error"
    echo ""
    read -p "Press Enter to continue..."
fi
