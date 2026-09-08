#!/bin/bash
set -e
echo "=== Razorpay Headless API - One-Click Isolated Setup ==="

# Detect python
if command -v python3 &> /dev/null; then
    PY=python3
elif command -v python &> /dev/null; then
    PY=python
else
    echo "❌ Python not found! Install Python 3.9+ from https://python.org"
    exit 1
fi
echo "✓ Found: $($PY --version) ($PY)"

# Create isolated venv if not exists
if [ ! -d "venv" ]; then
    echo "Creating isolated venv..."
    $PY -m venv venv
    echo "✓ venv created"
else
    echo "✓ venv already exists"
fi

# Activate venv
echo "Activating venv..."
source venv/bin/activate
echo "✓ venv activated: $(which python)"

# Upgrade pip
echo "Upgrading pip..."
pip install --upgrade pip -q

# Install dependencies in venv
echo "Installing dependencies in venv..."
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
else
    pip install fastapi "uvicorn[standard]" playwright pydantic python-dotenv httpx
fi
echo "✓ Dependencies installed"

# Install playwright browser in venv
echo "Checking playwright chromium..."
if ! python -c "from playwright.sync_api import sync_playwright" &> /dev/null; then
    pip install playwright -q
fi
# Check if chromium already installed
if [ ! -d "$HOME/Library/Caches/ms-playwright" ] && [ ! -d "$HOME/.cache/ms-playwright" ]; then
    echo "Installing chromium browser..."
    python -m playwright install chromium
else
    echo "✓ Chromium already installed, ensuring..."
    python -m playwright install chromium 2>&1 | tail -2
fi

# Verify everything
echo "Verifying installation..."
python -c "import fastapi, uvicorn, playwright; print('✓ fastapi', fastapi.__version__)"
python -c "from playwright.sync_api import sync_playwright; print('✓ playwright OK')"
python -m py_compile app.py && echo "✓ app.py syntax OK"

# Check port
PORT=${PORT:-8000}
if lsof -Pi :$PORT -sTCP:LISTEN -t &> /dev/null; then
    echo "⚠ Port $PORT already in use, trying 8001..."
    PORT=8001
fi

# Print URLs
LOCAL_IP=$(python3 -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8',80)); print(s.getsockname()[0]); s.close()" 2>/dev/null || echo "127.0.0.1")
echo ""
echo "=========================================="
echo " Starting Razorpay Headless API (Isolated venv)"
echo "  Local:    http://127.0.0.1:$PORT"
echo "  Network:  http://$LOCAL_IP:$PORT"
echo "  External: http://0.0.0.0:$PORT"
echo "  Docs:     http://127.0.0.1:$PORT/docs"
echo "  Env:      $(pwd)/venv (isolated, no global install)"
echo "=========================================="
echo ""

# Run in venv
PORT=$PORT python -m uvicorn app:app --host 0.0.0.0 --port $PORT --reload
