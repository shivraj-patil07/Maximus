#!/usr/bin/env bash
# ==============================================================================
# MAXIMUS — One-Command WSL Setup
# ==============================================================================
# Run this ONCE from WSL Ubuntu to set up the complete MAXIMUS environment.
#
# Prerequisites:
#   - WSL Ubuntu with Docker Desktop integration enabled
#   - Internet connection for pulling Docker images
#
# Usage (from WSL Ubuntu terminal):
#   chmod +x setup.sh && ./setup.sh
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'
log()     { echo -e "${GREEN}[✓]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
error()   { echo -e "${RED}[✗]${NC} $*"; exit 1; }
info()    { echo -e "${CYAN}[→]${NC} $*"; }
section() { echo -e "\n${BLUE}══════════════════════════════════════════════════════${NC}\n${BLUE}  $*${NC}\n${BLUE}══════════════════════════════════════════════════════${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

section "MAXIMUS Setup — The AI brain that thinks like an application security tester"
echo ""
info "This script will:"
echo "   1. Check prerequisites (Docker, Python, Node.js)"
echo "   2. Create Python virtual environment"
echo "   3. Create backend environment files"
echo "   4. Create workspace directories"
echo "   5. Pull Docker images"
echo "   6. Start all services"
echo "   7. Guide you through Claude Desktop configuration"
echo ""

# ── 1. Check Prerequisites ────────────────────────────────────────────────────
section "Checking Prerequisites"

# Docker
if ! command -v docker &>/dev/null; then
    error "Docker not found!\nInstall Docker Desktop (Windows) and enable WSL integration:\nhttps://docs.docker.com/desktop/windows/wsl/"
fi
if ! docker info &>/dev/null; then
    error "Docker daemon not running.\nStart Docker Desktop on Windows, then re-run this script."
fi
log "Docker: $(docker --version | cut -d' ' -f3 | tr -d ',')"

# Docker Compose
if docker compose version &>/dev/null; then
    COMPOSE="docker compose"
    log "Docker Compose: $(docker compose version --short 2>/dev/null || echo 'plugin')"
elif command -v docker-compose &>/dev/null; then
    COMPOSE="docker-compose"
    log "Docker Compose: standalone"
else
    error "Docker Compose not found. Install: sudo apt install docker-compose"
fi

# Python
PYTHON="python3"
for py in python3.12 python3.11 python3.10; do
    command -v $py &>/dev/null && { PYTHON=$py; break; }
done
PYTHON_VER=$($PYTHON --version 2>&1 | cut -d' ' -f2)
PY_MAJOR=$(echo "$PYTHON_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PYTHON_VER" | cut -d. -f2)
if [[ "$PY_MAJOR" -lt 3 || ("$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 10) ]]; then
    warn "Python $PYTHON_VER found. Python 3.10+ recommended."
    warn "Install: sudo apt install python3.11"
else
    log "Python: $PYTHON_VER"
fi

# Node.js (optional, for mcp-remote)
if command -v node &>/dev/null; then
    log "Node.js: $(node --version)"
else
    warn "Node.js not found (optional, needed for mcp-remote bridge)"
    info "Install: curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs"
fi

# Claude Code CLI (optional)
if command -v claude &>/dev/null; then
    log "Claude Code: $(claude --version 2>/dev/null | head -1)"
else
    warn "Claude Code CLI not found (needed for maximus.py)"
    info "Install: npm install -g @anthropic-ai/claude-code"
fi

# ── 2. Python virtual environment ─────────────────────────────────────────────
section "Python Environment"

if [[ ! -f ".venv/bin/python" ]]; then
    log "Creating virtual environment..."
    $PYTHON -m venv .venv
    .venv/bin/pip install -q --upgrade pip
    .venv/bin/pip install -q click httpx rich
    log "Virtual environment created: .venv/"
else
    log "Virtual environment already exists"
    .venv/bin/pip install -q --upgrade click httpx rich 2>/dev/null || true
fi

# ── 3. Environment files ──────────────────────────────────────────────────────
section "Configuration Files"

if [[ ! -f backend/.env ]]; then
    cp backend/.env.docker backend/.env
    log "Created backend/.env"
else
    log "backend/.env already exists"
fi

# Generate a random secret key
if command -v openssl &>/dev/null; then
    SECRET=$(openssl rand -hex 32)
    if grep -q "change-me-in-production" backend/.env 2>/dev/null; then
        sed -i "s/your-super-secret-key-min-32-chars-change-me-in-production/$SECRET/" backend/.env
        log "Generated SECRET_KEY"
    fi
fi

# ── 4. Workspace directories ──────────────────────────────────────────────────
section "Workspace Setup"

mkdir -p "$HOME/.maximus/workspaces"
mkdir -p "$SCRIPT_DIR/.maximus"
mkdir -p "$SCRIPT_DIR/backend/logs"
log "Workspace: $HOME/.maximus/workspaces/"
log "Config dir: $SCRIPT_DIR/.maximus/"

# ── 5. Pull Docker images ─────────────────────────────────────────────────────
section "Docker Images"

info "Pulling MAXIMUS Docker images (AIDA backend + pentest container)..."
info "This may take a few minutes on first run (~2-3 GB total)..."
echo ""

# Pull backend from Hub
$COMPOSE pull backend 2>/dev/null && log "Backend image pulled" \
    || { warn "Hub pull failed — will try building from local source"; }

# Pull pentest container
docker pull vasco0x4/aida-pentest:latest 2>/dev/null && log "Pentest container pulled" \
    || warn "Pentest image pull failed — will pull on first start"

# ── 6. Build frontend ─────────────────────────────────────────────────────────
AIDA_FRONTEND="../AIDA/frontend"
if [[ -d "$AIDA_FRONTEND" ]]; then
    log "Building frontend from local source..."
    $COMPOSE build --quiet frontend 2>/dev/null && log "Frontend built" \
        || warn "Frontend build failed — will use Hub image"
else
    $COMPOSE pull frontend 2>/dev/null && log "Frontend image pulled" \
        || warn "Frontend pull failed"
fi

# ── 7. Start services ─────────────────────────────────────────────────────────
section "Starting Services"

log "Starting MAXIMUS stack..."
./start.sh

# ── 8. Claude Desktop config ──────────────────────────────────────────────────
section "Claude Desktop Configuration"

echo ""
info "Run this to generate your Claude Desktop config:"
echo ""
echo "   source .venv/bin/activate"
echo "   python3 maximus.py setup-desktop"
echo ""
info "Or get your API key manually:"
echo "   1. Open http://localhost:31337"
echo "   2. Create admin account (first-time setup wizard)"
echo "   3. Go to Settings → API Keys → Create Key"
echo "   4. Run: python3 maximus.py setup-desktop"
echo ""

# ── Done ──────────────────────────────────────────────────────────────────────
section "Setup Complete!"
echo ""
log "Dashboard  : http://localhost:31337"
log "Backend    : http://localhost:8000"
log "MCP HTTP   : http://localhost:8000/mcp"
echo ""
echo -e "${CYAN}Quick Start:${NC}"
echo "   source .venv/bin/activate"
echo "   python3 maximus.py -a 'my-target'"
echo ""
echo -e "${CYAN}Claude Desktop:${NC}"
echo "   python3 maximus.py setup-desktop"
echo ""
echo -e "${CYAN}Supported AI CLIs:${NC}"
echo "   Claude Code  : python3 maximus.py --cli claude -a 'target'"
echo "   Auto-detect  : python3 maximus.py --cli auto -a 'target'"
echo "   No prompts   : python3 maximus.py --yes -a 'target'"
echo ""
