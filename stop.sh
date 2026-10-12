#!/usr/bin/env bash
# MAXIMUS — Stop all services (data preserved)

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()     { echo -e "${GREEN}[✓]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
section() { echo -e "\n${BLUE}══════════════════════════════════════════${NC}\n${BLUE}  $*${NC}\n${BLUE}══════════════════════════════════════════${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if docker compose version &>/dev/null; then
    COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
    COMPOSE="docker-compose"
else
    COMPOSE="docker compose"
fi

section "MAXIMUS — Stopping Services"

RUNNING=$($COMPOSE ps --status running -q 2>/dev/null | wc -l | tr -d ' ')
if [[ "$RUNNING" -eq 0 ]]; then
    warn "No running containers"
    echo "To start: ./start.sh"
    exit 0
fi

log "Stopping $RUNNING container(s)..."
$COMPOSE stop
log "All containers stopped — data preserved"
echo ""
log "Restart:  ./start.sh"
echo ""
