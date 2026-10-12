# ==============================================================================
# MAXIMUS - Windows Startup Script
# Usage: .\start.ps1
# ==============================================================================

$ErrorActionPreference = "SilentlyContinue"

Write-Host ""
Write-Host "  ============================================" -ForegroundColor Green
Write-Host "   MAXIMUS" -ForegroundColor Green
Write-Host "   The AI brain that thinks like an" -ForegroundColor DarkGray
Write-Host "   application security tester" -ForegroundColor DarkGray
Write-Host "  ============================================" -ForegroundColor Green
Write-Host ""

# Check Docker
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "  [X] Docker not found. Install Docker Desktop first." -ForegroundColor Red
    exit 1
}

# Start containers
Write-Host "  [*] Starting containers..." -ForegroundColor Yellow
docker compose up -d 2>&1 | Where-Object { $_ -notmatch "^(WARN|#)" } | ForEach-Object { Write-Host "      $_" }

# Wait for backend
Write-Host ""
Write-Host "  [*] Waiting for services to be ready..." -ForegroundColor Yellow

$maxWait = 60
$ready = $false
for ($i = 0; $i -lt $maxWait; $i++) {
    $status = docker inspect --format "{{.State.Health.Status}}" maximus_backend 2>$null
    if ($status -eq "healthy") { $ready = $true; break }
    Start-Sleep -Seconds 1
    if ($i % 10 -eq 9) { Write-Host "      Still waiting... ($($i+1)s)" -ForegroundColor DarkGray }
}

Write-Host ""
if ($ready) {
    Write-Host "  ============================================" -ForegroundColor Cyan
    Write-Host "   MAXIMUS is READY!" -ForegroundColor Green
    Write-Host "  ============================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "   [UI]  Main Dashboard  ->  http://localhost:31337" -ForegroundColor Cyan
    Write-Host "   [API] Backend         ->  http://localhost:8000" -ForegroundColor DarkGray
    Write-Host "   [DOC] Swagger Docs    ->  http://localhost:8000/docs" -ForegroundColor DarkGray
    Write-Host "   [MCP] Claude Desktop  ->  http://localhost:8000/mcp" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  ============================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Open your browser: http://localhost:31337" -ForegroundColor Green
    Write-Host ""
} else {
    Write-Host "  [!] Backend took too long. Check logs:" -ForegroundColor Yellow
    Write-Host "      docker compose logs maximus_backend" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "   [UI]  http://localhost:31337" -ForegroundColor Cyan
    Write-Host "   [API] http://localhost:8000" -ForegroundColor Cyan
    Write-Host ""
}

# Show container status
Write-Host "  Container Status:" -ForegroundColor DarkGray
docker compose ps --format "table {{.Name}}`t{{.Status}}" 2>&1
Write-Host ""
