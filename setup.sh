#!/bin/bash
# HLA Typing Report Generator — First-time setup script (Linux/macOS)

set -e  # Exit on error
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo "=================================================="
echo "  HLA Typing Report Generator — Setup"
echo "=================================================="
echo ""

# Check if Python 3 is installed
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}[ERROR]${NC} Python 3 not found"
    echo ""
    echo "Please install Python 3.8 or higher:"
    echo "  Ubuntu/Debian: sudo apt-get install python3 python3-venv python3-pip"
    echo "  macOS: brew install python3"
    echo "  Or download from: https://www.python.org/downloads/"
    exit 1
fi

# Check Python version
PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")

echo -e "Found Python version: ${GREEN}$PY_VERSION${NC}"

# Check if Python version is 3.8 or higher
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 8 ]; }; then
    echo -e "${RED}[ERROR]${NC} Python 3.8 or higher is required (found $PY_VERSION)"
    exit 1
fi
echo ""

# Check if venv module is available
if ! python3 -m venv --help &>/dev/null; then
    echo -e "${RED}[ERROR]${NC} Python venv module not available"
    echo "Install it with: sudo apt-get install python3-venv"
    exit 1
fi

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo -e "${BLUE}[1/3]${NC} Creating virtual environment..."
    python3 -m venv venv
    echo -e "      ${GREEN}✓${NC} Virtual environment created successfully"
else
    echo -e "${BLUE}[1/3]${NC} Virtual environment already exists"
fi
echo ""

# Activate virtual environment
echo -e "${BLUE}[2/3]${NC} Activating virtual environment..."
source venv/bin/activate
echo -e "      ${GREEN}✓${NC} Virtual environment activated"
echo ""

# Upgrade pip
echo -e "${BLUE}[3/3]${NC} Installing dependencies..."
echo "      Upgrading pip..."
pip install --upgrade pip --quiet || {
    echo -e "      ${YELLOW}[WARNING]${NC} Failed to upgrade pip, continuing anyway..."
}

# Check if requirements.txt exists
if [ ! -f "requirements.txt" ]; then
    echo -e "${RED}[ERROR]${NC} requirements.txt not found"
    echo "Please ensure requirements.txt is in the same directory as this script"
    exit 1
fi

# Install dependencies
echo "      Installing packages from requirements.txt..."
if ! pip install -r requirements.txt --quiet; then
    echo ""
    echo -e "${RED}[ERROR]${NC} Failed to install dependencies"
    echo ""
    echo "Trying again with verbose output..."
    pip install -r requirements.txt
    exit 1
fi

echo ""
echo "=================================================="
echo -e "  ${GREEN}Setup Complete!${NC}"
echo "=================================================="
echo ""
echo "Next steps:"
echo "  1. Run: ./launch.sh"
echo "  2. Or: chmod +x launch.sh && ./launch.sh"
echo ""
