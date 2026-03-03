#!/usr/bin/env bash
# ── Kalshi Arb Engine – Oracle Cloud Setup Script ──────────────────────────
# Run this ON the cloud VM after SSH-ing in:
#   bash setup_server.sh
# ───────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_DIR="$HOME/kalshi-arb-engine"
VENV_DIR="$REPO_DIR/.venv"
SERVICE_NAME="kalshi-arb"

echo "╔══════════════════════════════════════════════╗"
echo "║  Kalshi Arb Engine – Server Setup            ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── 1. System packages ────────────────────────────────────────────────────
echo "[1/6] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-pip python3-venv git > /dev/null 2>&1
echo "  ✓ Python $(python3 --version | cut -d' ' -f2), git installed"

# ── 2. Clone or update repo ──────────────────────────────────────────────
echo "[2/6] Setting up repository..."
if [ -d "$REPO_DIR" ]; then
    echo "  Repo exists, pulling latest..."
    cd "$REPO_DIR" && git pull origin main
else
    echo "  Cloning repo..."
    # If private repo, you'll be prompted for credentials or need a token
    git clone https://github.com/mitchellray-gh/kalshi-arb-engine.git "$REPO_DIR"
fi
cd "$REPO_DIR"
echo "  ✓ Repo ready at $REPO_DIR"

# ── 3. Python virtual environment ────────────────────────────────────────
echo "[3/6] Creating Python virtual environment..."
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "  ✓ Virtual environment ready with $(pip list --format=freeze | wc -l) packages"

# ── 4. Check for credentials ────────────────────────────────────────────
echo "[4/6] Checking credentials..."
if [ ! -f "$REPO_DIR/.env" ]; then
    echo ""
    echo "  ⚠  .env file not found!"
    echo "  Copy it from your PC:"
    echo "    scp C:\\kalshi-arb-engine\\.env  ubuntu@<SERVER_IP>:~/kalshi-arb-engine/.env"
    echo ""
fi
if [ ! -f "$REPO_DIR/kalshi.key" ]; then
    echo ""
    echo "  ⚠  kalshi.key not found!"
    echo "  Copy it from your PC:"
    echo "    scp C:\\kalshi-arb-engine\\kalshi.key  ubuntu@<SERVER_IP>:~/kalshi-arb-engine/kalshi.key"
    echo ""
fi
if [ -f "$REPO_DIR/.env" ] && [ -f "$REPO_DIR/kalshi.key" ]; then
    echo "  ✓ Credentials found"
fi

# ── 5. Create results directory ──────────────────────────────────────────
echo "[5/6] Creating results directory..."
mkdir -p "$REPO_DIR/results"
echo "  ✓ results/ directory ready"

# ── 6. Install systemd service ───────────────────────────────────────────
echo "[6/6] Installing systemd service..."

sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<EOF
[Unit]
Description=Kalshi Arb Engine – Autonomous Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=${REPO_DIR}
ExecStart=${VENV_DIR}/bin/python run_engine.py --live
Restart=always
RestartSec=10
StandardOutput=append:${REPO_DIR}/results/engine_stdout.log
StandardError=append:${REPO_DIR}/results/engine_stderr.log

# Environment
Environment=PYTHONUNBUFFERED=1

# Hardening
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=${REPO_DIR}/results ${REPO_DIR}/.env ${REPO_DIR}/kalshi.key

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}
echo "  ✓ Systemd service installed and enabled (auto-starts on boot)"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  Setup Complete!                             ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "  Commands:"
echo "    sudo systemctl start kalshi-arb     # Start the engine"
echo "    sudo systemctl stop kalshi-arb      # Stop the engine"
echo "    sudo systemctl status kalshi-arb    # Check status"
echo "    sudo systemctl restart kalshi-arb   # Restart"
echo "    journalctl -u kalshi-arb -f         # Live log stream"
echo ""
echo "  Quick checks:"
echo "    cd $REPO_DIR"
echo "    $VENV_DIR/bin/python main.py --balance --env prod"
echo "    $VENV_DIR/bin/python main.py --report"
echo ""
if [ ! -f "$REPO_DIR/.env" ] || [ ! -f "$REPO_DIR/kalshi.key" ]; then
    echo "  ⚠  Upload .env and kalshi.key before starting!"
    echo ""
fi
