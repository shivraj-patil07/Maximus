#!/usr/bin/env bash
# MAXIMUS — Restart all services

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

./stop.sh
sleep 2
./start.sh "$@"
