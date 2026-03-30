#!/usr/bin/env bash
set -e

echo "=== AuraScribe Setup ==="

# 1. Python virtual environment
if [ ! -d ".venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate

echo "Installing Python dependencies..."
pip install --upgrade pip
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
pip install whisperx
pip install -r requirements.txt

# 2. Frontend
echo "Building frontend..."
cd frontend
npm install
npm run build
cd ..

# 3. Obsidian vault directories
echo "Creating Obsidian vault directories..."
source .env 2>/dev/null || true
VAULT="${OBSIDIAN_VAULT:-/home/aria/obsidian-vault}"
mkdir -p "$VAULT/AuraScribe/Meetings"
mkdir -p "$VAULT/AuraScribe/People"
mkdir -p "$VAULT/AuraScribe/Daily"

echo ""
echo "=== Setup complete! ==="
echo "Run AuraScribe with:"
echo "  source .venv/bin/activate && python tray.py     # system tray"
echo "  source .venv/bin/activate && python main.py     # terminal"
echo ""
echo "First time: open http://localhost:8000 and click the People icon"
echo "to enroll your voice so AuraScribe can identify you."
