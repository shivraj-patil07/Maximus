#!/usr/bin/env bash
# ==============================================================================
# MAXIMUS — Start Services
# The AI brain that thinks like an application security tester
# ==============================================================================
# Usage:
#   ./start.sh                  Local mode  — http://localhost:31337
#   ./start.sh --dev            Dev mode    — http://localhost:5173 (hot reload)
#   ./start.sh --lan            LAN mode    — https://<LAN_IP> (Caddy + self-signed)
#   ./start.sh --localhost      Localhost mode — commands run on host, not container
#   ./start.sh --rebuild        Force rebuild of Docker images
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()     { echo -e "${GREEN}[✓]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
error()   { echo -e "${RED}[✗]${NC} $*"; }
section() { echo -e "\n${BLUE}══════════════════════════════════════════${NC}\n${BLUE}  $*${NC}\n${BLUE}══════════════════════════════════════════${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── Parse arguments ─────────────────────────────────────────────────────────
MODE="prod"
REBUILD=false
DEPLOYMENT_MODE_OVERRIDE=""
TLS_MODE=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --dev|-d)       MODE="dev"; shift ;;
        --lan|-l)       TLS_MODE="lan"; shift ;;
        --localhost)    DEPLOYMENT_MODE_OVERRIDE="localhost"; shift ;;
        --container)    DEPLOYMENT_MODE_OVERRIDE="container"; shift ;;
        --rebuild|-r)   REBUILD=true; shift ;;
        --help|-h)
            cat <<EOF
Usage: ./start.sh [OPTIONS]

  (default)     Local mode  — http://localhost:31337
  --dev, -d     Dev mode    — http://localhost:5173  (Vite hot reload)
  --lan, -l     LAN mode    — https://<IP>           (self-signed TLS)
  --localhost   Execute commands on host (no pentest container)
  --container   Execute commands in maximus-pentest container (default)
  --rebuild     Force rebuild of Docker images
  --help        Show this help
EOF
            exit 0 ;;
        *)
            error "Unknown argument: $1"; echo "Run ./start.sh --help"; exit 1 ;;
    esac
done

# ── Deployment mode ──────────────────────────────────────────────────────────
DEPLOYMENT_MODE_FILE="$SCRIPT_DIR/.maximus/deployment-mode"
mkdir -p "$SCRIPT_DIR/.maximus"

CURRENT_MODE=$(cat "$DEPLOYMENT_MODE_FILE" 2>/dev/null || echo "container")
DEPLOYMENT_MODE="${DEPLOYMENT_MODE_OVERRIDE:-$CURRENT_MODE}"
[[ "$DEPLOYMENT_MODE" != "container" && "$DEPLOYMENT_MODE" != "localhost" ]] && DEPLOYMENT_MODE="container"
echo "$DEPLOYMENT_MODE" > "$DEPLOYMENT_MODE_FILE"
export DEPLOYMENT_MODE

# ── Docker checks ─────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    error "Docker not installed. Get it: https://docs.docker.com/desktop/"
    exit 1
fi
if ! docker info &>/dev/null; then
    error "Docker daemon not running. Start Docker Desktop first."
    exit 1
fi

if docker compose version &>/dev/null; then
    COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
    COMPOSE="docker-compose"
else
    error "Docker Compose not found. Install: sudo apt install docker-compose"
    exit 1
fi

# ── Mode config ───────────────────────────────────────────────────────────────
AIDA_PORT=31337
FRONTEND_URL="http://localhost:${AIDA_PORT}"
MODE_LABEL="Local"

if [[ "$MODE" == "dev" ]]; then
    COMPOSE_FILES=""
    FRONTEND_URL="http://localhost:5173"
    MODE_LABEL="Development"
elif [[ "$TLS_MODE" == "lan" ]]; then
    COMPOSE_FILES="-f docker-compose.yml -f docker-compose.prod.yml"
    HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "")
    if [[ -z "$HOST_IP" ]]; then
        read -rp "Enter your LAN IP (e.g. 192.168.1.10): " HOST_IP
    fi
    export BACKEND_CORS_ORIGINS="https://${HOST_IP},https://localhost"
    FRONTEND_URL="https://${HOST_IP}"
    MODE_LABEL="LAN"
else
    COMPOSE_FILES="-f docker-compose.yml -f docker-compose.prod.yml"
fi

COMPOSE_CMD="$COMPOSE ${COMPOSE_FILES:-}"

section "MAXIMUS — ${MODE_LABEL} Mode (${DEPLOYMENT_MODE})"

# ── Port conflict check ───────────────────────────────────────────────────────
check_port() {
    local port=$1 svc=$2
    if command -v ss &>/dev/null; then
        local p; p=$(ss -tlnp 2>/dev/null | grep ":$port " | sed 's/.*users:(("\([^"]*\)".*/\1/')
        if [[ -n "$p" ]] && ! echo "$p" | grep -qiE "docker|orbstack"; then
            warn "Port $port ($svc) in use by: $p"; return 1
        fi
    fi
    return 0
}

PORT_CONFLICT=false
check_port 5432 "PostgreSQL" || PORT_CONFLICT=true
check_port 8000 "Backend"    || PORT_CONFLICT=true
check_port "$AIDA_PORT" "Frontend" || PORT_CONFLICT=true

if [[ "$PORT_CONFLICT" == "true" ]]; then
    read -p "Port conflict detected. Continue anyway? (y/N): " -n 1 -r; echo
    [[ ! $REPLY =~ ^[Yy]$ ]] && { error "Aborted."; exit 1; }
fi

# ── Environment files ─────────────────────────────────────────────────────────
if [[ ! -f backend/.env ]]; then
    cp backend/.env.docker backend/.env
    log "Created backend/.env from template"
fi

mkdir -p backend/logs
mkdir -p "$HOME/.maximus/workspaces"

# ── Python environments ───────────────────────────────────────────────────────
PYTHON="python3"
for py in python3.12 python3.11 python3.10; do
    command -v $py &>/dev/null && { PYTHON=$py; break; }
done

if [[ ! -f ".venv/bin/python" ]]; then
    log "Creating CLI virtual environment..."
    $PYTHON -m venv .venv
    .venv/bin/pip install -q --upgrade pip
    [[ -f requirements.txt ]] && .venv/bin/pip install -q -r requirements.txt
    log "CLI venv ready"
fi

# ── Docker images ─────────────────────────────────────────────────────────────
section "Docker Setup"

# Try to use local AIDA backend/frontend source if available
AIDA_BACKEND="../AIDA/backend"
AIDA_FRONTEND="../AIDA/frontend"

if [[ "$REBUILD" == "true" ]]; then
    log "Force rebuilding Docker images..."
    if [[ -d "$AIDA_BACKEND" && -d "$AIDA_FRONTEND" ]]; then
        log "Building from local AIDA source..."
        $COMPOSE_CMD build
    else
        log "Pulling from Docker Hub..."
        $COMPOSE_CMD pull backend frontend
    fi
elif [[ -d "$AIDA_BACKEND" && -d "$AIDA_FRONTEND" ]]; then
    log "Local AIDA source found — checking if build needed..."
    existing=$($COMPOSE_CMD ps -q 2>/dev/null | wc -l | tr -d ' ')
    if [[ "$existing" -eq 0 ]]; then
        log "Building from local AIDA source..."
        $COMPOSE_CMD build --quiet backend frontend 2>/dev/null || {
            warn "Local build failed — pulling from Docker Hub..."
            $COMPOSE_CMD pull --quiet backend frontend 2>/dev/null || true
        }
    fi
else
    log "Pulling backend image from Docker Hub..."
    $COMPOSE_CMD pull --quiet backend 2>/dev/null || {
        warn "Hub pull failed — using cached image (if available)"
    }
    log "Building frontend..."
    $COMPOSE_CMD build --quiet frontend 2>/dev/null || true
fi

# ── Start containers ──────────────────────────────────────────────────────────
section "Starting Containers"

if [[ "$MODE" == "dev" ]]; then
    ENVIRONMENT=development $COMPOSE_CMD up -d --remove-orphans 2>&1 | grep -v "already exists" || true
else
    $COMPOSE_CMD up -d --remove-orphans 2>&1 | grep -v "already exists" || true
fi

# ── Wait for services ─────────────────────────────────────────────────────────
section "Waiting for Services"

wait_for() {
    local name=$1 cmd=$2 max=${3:-60}
    printf "  %-15s " "$name..."
    for ((i=0; i<max; i++)); do
        eval "$cmd" &>/dev/null && { echo -e "${GREEN}Ready${NC}"; return 0; }
        sleep 1
    done
    echo -e "${RED}TIMEOUT${NC}"
    return 1
}

wait_for "PostgreSQL" "$COMPOSE_CMD exec -T postgres pg_isready -U pentraai" 30 \
    || { error "PostgreSQL timeout. Check: $COMPOSE_CMD logs postgres"; exit 1; }

wait_for "Backend API" "curl -sf http://localhost:8000/health" 60 \
    || { error "Backend timeout. Check: $COMPOSE_CMD logs maximus_backend"; exit 1; }

if [[ "$MODE" == "dev" ]]; then
    wait_for "Frontend" "curl -sf http://localhost:5173" 90 \
        || { error "Frontend timeout. Check: $COMPOSE_CMD logs maximus_frontend"; exit 1; }
else
    wait_for "Frontend" "curl -sf http://localhost:${AIDA_PORT}" 90 \
        || { error "Frontend timeout. Check: $COMPOSE_CMD logs maximus_frontend"; exit 1; }
fi

# ── Pentest container ─────────────────────────────────────────────────────────
if [[ "$DEPLOYMENT_MODE" == "container" ]]; then
    RUNNING=$(docker ps --format "{{.Names}}" 2>/dev/null | grep "^maximus-pentest$" || true)
    if [[ -z "$RUNNING" ]]; then
        warn "Pentest container not running — starting..."
        $COMPOSE_CMD up -d maximus-pentest 2>&1 | grep -v "already" || true
    fi
fi

# ── Success ───────────────────────────────────────────────────────────────────
section "MAXIMUS Ready"
echo ""
log "Dashboard : $FRONTEND_URL"
log "Backend   : http://localhost:8000"
log "API Docs  : http://localhost:8000/docs"
log "MCP HTTP  : http://localhost:8000/mcp"
echo ""
log "First time? Open the dashboard and create an admin account."
log "Claude Desktop: python3 maximus.py setup-desktop"
echo ""
$COMPOSE_CMD ps --format "table {{.Name}}\t{{.Status}}" 2>/dev/null || true
echo ""
