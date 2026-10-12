#!/usr/bin/env python3
"""
PentraAI — AI-Driven Autonomous Penetration Testing Platform
============================================================
Unified launcher merging the best of AIDA and AstraMitra.

Supports:
  - Claude Code, OpenAI Codex, Kimi Code, Qwen Code
  - Claude Desktop (via HTTP MCP or WSL stdio)
  - Cursor IDE (via SSE/HTTP MCP snippet)
  - Automatic workspace resolution and assessment loading
  - JWT authentication with long-lived API keys

Transport modes:
  stdio  — spawns backend/mcp/aida_mcp_server.py as local subprocess (default when local backend found)
  http   — connects to running backend HTTP MCP endpoint at port 8000/mcp (default otherwise)

Usage:
  python3 pentraai.py [OPTIONS] [PROMPT]
  python3 pentraai.py setup-desktop       # Generate Claude Desktop config
"""

import os
import re
import sys
import json
import subprocess
from pathlib import Path
from typing import Optional, Literal

# ─── Dependency bootstrap ─────────────────────────────────────────────────────

def _ensure_deps():
    VENV = Path(__file__).parent / ".venv"
    try:
        import click, httpx
        from rich.console import Console
        return
    except ImportError:
        pass

    python = str(VENV / "bin" / "python") if (VENV / "bin" / "python").exists() else sys.executable
    print("Installing CLI dependencies...", file=sys.stderr)
    if not VENV.exists():
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True, capture_output=True)
        python = str(VENV / "bin" / "python")
    req = Path(__file__).parent / "requirements.txt"
    if req.exists():
        subprocess.run([python, "-m", "pip", "install", "--quiet", "-r", str(req)], check=True)
    if python != sys.executable:
        os.execv(python, [python] + sys.argv)

_ensure_deps()

import click
import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

# ─── Configuration ─────────────────────────────────────────────────────────────

console = Console()

ROOT          = Path(__file__).parent.absolute()
CONFIG_DIR    = ROOT / ".pentraai"
PREPROMPT     = ROOT / "Docs" / "PrePrompt.txt"
PREPROMPT_ASVS = ROOT / "Docs" / "PrePrompt-ASVS.txt"
MCP_CFG_FILE  = CONFIG_DIR / "mcp-config.json"
SESSION_FILE  = CONFIG_DIR / "session"
API_KEY_FILE  = CONFIG_DIR / "api-key"
CONTAINER_PREFS = CONFIG_DIR / "container-preference"

# Local AIDA backend MCP server paths to try (in order)
_MCP_SERVER_CANDIDATES = [
    ROOT / "backend" / "mcp" / "aida_mcp_server.py",
    ROOT.parent / "AIDA" / "backend" / "mcp" / "aida_mcp_server.py",
]
MCP_SERVER_PATH: Optional[Path] = next((p for p in _MCP_SERVER_CANDIDATES if p.exists()), None)

DEFAULT_BACKEND    = "http://localhost:8000/api"
DEFAULT_MCP_HTTP   = "http://localhost:8000/mcp"
DEFAULT_PERMISSION = "default"
DEFAULT_MODEL      = "claude-sonnet-4-5"
KIMI_CONFIG_DIR    = ".kimi-code"

CLIType = Literal["claude", "codex", "kimi", "qwen"]

# ─── Token helpers ────────────────────────────────────────────────────────────

def _read_token(path: Path) -> Optional[str]:
    return path.read_text().strip() or None if path.exists() else None

def _write_token(path: Path, token: str):
    CONFIG_DIR.mkdir(exist_ok=True)
    path.write_text(token)
    path.chmod(0o600)

def _validate(backend_url: str, token: str) -> bool:
    try:
        with httpx.Client(timeout=5) as c:
            return c.get(f"{backend_url}/auth/me",
                         headers={"Authorization": f"Bearer {token}"}).status_code == 200
    except httpx.ConnectError:
        console.print("[red]✗ Cannot reach backend — is `./start.sh` running?[/red]\n")
        sys.exit(1)

def _do_login(backend_url: str) -> str:
    import getpass
    console.print("[bold]PentraAI Login[/bold]")
    u = console.input("  Username: ")
    p = getpass.getpass("  Password: ")
    try:
        with httpx.Client(timeout=10) as c:
            r = c.post(f"{backend_url}/auth/login", json={"username": u, "password": p})
        if r.status_code == 200:
            return r.json()["access_token"]
        console.print(f"[red]✗ Login failed ({r.status_code})[/red]\n")
        sys.exit(1)
    except httpx.ConnectError:
        console.print("[red]✗ Backend unreachable. Run: ./start.sh[/red]\n")
        sys.exit(1)

def _fetch_api_key(backend_url: str, session_token: str) -> Optional[str]:
    try:
        with httpx.Client(timeout=10) as c:
            r = c.post(f"{backend_url}/auth/api-token",
                       headers={"Authorization": f"Bearer {session_token}"})
        return r.json().get("api_token") if r.status_code == 200 else None
    except Exception:
        return None

def authenticate(backend_url: str) -> str:
    """Return a valid bearer token. Priority: env → api-key file → session file → login."""
    token = os.getenv("PENTRAAI_TOKEN") or os.getenv("AIDA_TOKEN")
    if token:
        return token
    for path in (API_KEY_FILE, SESSION_FILE):
        token = _read_token(path)
        if token and _validate(backend_url, token):
            if path == SESSION_FILE:
                ak = _fetch_api_key(backend_url, token)
                if ak:
                    _write_token(API_KEY_FILE, ak)
                    SESSION_FILE.unlink(missing_ok=True)
                    return ak
            return token
        if token:
            path.unlink(missing_ok=True)
    session = _do_login(backend_url)
    ak = _fetch_api_key(backend_url, session)
    if ak:
        _write_token(API_KEY_FILE, ak)
        console.print("[green]✓ Authenticated[/green]\n")
        return ak
    _write_token(SESSION_FILE, session)
    console.print("[green]✓ Authenticated[/green]\n")
    return session

# ─── MCP config generators ────────────────────────────────────────────────────

def _backend_venv_python(quiet=False) -> str:
    """Return path to Python in the local AIDA backend venv, or 'python3'."""
    for candidate in _MCP_SERVER_CANDIDATES:
        venv = candidate.parent.parent.parent / "venv"
        py = venv / "bin" / "python"
        if py.exists():
            return str(py)
    return "python3"

def gen_stdio_config(db_url: str, token: str, quiet=False):
    """Generate stdio MCP config (requires local backend MCP server)."""
    CONFIG_DIR.mkdir(exist_ok=True)
    if not MCP_SERVER_PATH:
        console.print("[red]✗ Local MCP server not found. Use HTTP mode instead.[/red]\n")
        sys.exit(1)

    # Find the backend root (parent of mcp/)
    backend_root = MCP_SERVER_PATH.parent.parent
    python = _backend_venv_python(quiet)

    config = {
        "mcpServers": {
            "pentraai-mcp": {
                "command": python,
                "args": [str(MCP_SERVER_PATH.absolute())],
                "env": {
                    "PYTHONPATH": str(backend_root.absolute()),
                    "DATABASE_URL": db_url,
                    "AIDA_TOKEN": token,
                }
            }
        }
    }
    MCP_CFG_FILE.write_text(json.dumps(config, indent=2))
    MCP_CFG_FILE.chmod(0o600)
    if not quiet:
        console.print(f"[dim]✓ MCP config (stdio): {MCP_CFG_FILE.name}[/dim]")

def gen_http_config(url: str, api_key: str, quiet=False):
    """Generate HTTP MCP config (connects to running backend)."""
    CONFIG_DIR.mkdir(exist_ok=True)
    config = {
        "mcpServers": {
            "pentraai-mcp": {
                "url": url,
                "headers": {"Authorization": f"Bearer {api_key}"},
            }
        }
    }
    MCP_CFG_FILE.write_text(json.dumps(config, indent=2))
    MCP_CFG_FILE.chmod(0o600)
    if not quiet:
        console.print(f"[dim]✓ MCP config (HTTP): {url}[/dim]")

# ─── WSL path helpers ─────────────────────────────────────────────────────────

_WIN_RE = re.compile(r"^([A-Za-z]):[\\/]")

def _is_wsl() -> bool:
    if os.getenv("WSL_DISTRO_NAME") or os.getenv("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in open("/proc/version").read().lower()
    except OSError:
        return False

def translate_path(path: str) -> str:
    if not path or not _WIN_RE.match(path) or not _is_wsl():
        return path
    try:
        r = subprocess.run(["wslpath", "-u", path], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    drive = path[0].lower()
    rest = path[2:].lstrip("/\\").replace("\\", "/")
    return f"/mnt/{drive}/{rest}"

# ─── Workspace resolution ─────────────────────────────────────────────────────

def resolve_workspace(name: str, backend_url: str, token: str) -> Optional[dict]:
    import time
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(3):
        try:
            with httpx.Client(timeout=10) as c:
                r = c.get(f"{backend_url}/workspace/resolve",
                          params={"assessment_name": name}, headers=headers)
            return r.json() if r.status_code == 200 else None
        except httpx.ConnectError:
            console.print("[red]✗ Backend unreachable — run ./start.sh[/red]\n")
            sys.exit(1)
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
    return None

def fetch_methodology(assessment_id: int, backend_url: str, token: str) -> str:
    try:
        with httpx.Client(timeout=10) as c:
            r = c.get(f"{backend_url}/assessments/{assessment_id}",
                      headers={"Authorization": f"Bearer {token}"})
        return r.json().get("methodology", "standard") if r.status_code == 200 else "standard"
    except Exception:
        return "standard"

# ─── CLI detection ────────────────────────────────────────────────────────────

def _cli_exists(name: str) -> bool:
    """Cross-platform CLI detection (works on Windows + Linux/WSL)."""
    import shutil
    if shutil.which(name):
        return True
    # Windows: also check .ps1/.cmd/.bat extensions
    if os.name == "nt":
        for ext in (".ps1", ".cmd", ".bat", ".exe"):
            if shutil.which(name + ext):
                return True
    return False

def detect_cli() -> Optional[CLIType]:
    for cli in ("claude", "codex", "kimi", "qwen"):
        if _cli_exists(cli):
            return cli
    return None

# ─── Codex MCP helpers ───────────────────────────────────────────────────────

def _toml_val(v) -> str:
    if isinstance(v, bool): return "true" if v else "false"
    if isinstance(v, (int, float)): return str(v)
    if isinstance(v, str): return json.dumps(v)
    if isinstance(v, list): return "[" + ", ".join(_toml_val(i) for i in v) + "]"
    raise TypeError(type(v))

def codex_mcp_args(python_bin=None, http_url=None) -> list:
    p = "mcp_servers.pentraai-mcp"
    args = []
    if http_url:
        args += ["--config", f"{p}.url={_toml_val(http_url)}"]
        args += ["--config", f"{p}.bearer_token_env_var={_toml_val('PENTRAAI_MCP_API_KEY')}"]
    else:
        args += ["--config", f"{p}.command={_toml_val(python_bin or 'python3')}"]
        args += ["--config", f"{p}.args={_toml_val([str(MCP_SERVER_PATH.absolute())])}"]
        args += ["--config", f"{p}.env_vars={_toml_val(['PENTRAAI_TOKEN','AIDA_TOKEN','BACKEND_API_URL','DATABASE_URL','PYTHONPATH'])}"]
    args += ["--config", f"{p}.required={_toml_val(True)}"]
    args += ["--config", f"{p}.startup_timeout_sec={_toml_val(30)}"]
    args += ["--config", f"{p}.tool_timeout_sec={_toml_val(900)}"]
    return args

# ─── Setup Desktop subcommand ─────────────────────────────────────────────────

def cmd_setup_desktop(backend_url: str):
    """Generate claude_desktop_config.json for Claude Desktop integration."""
    console.print(Panel(
        "[bold cyan]PentraAI — Claude Desktop Setup[/bold cyan]\n\n"
        "[dim]Generating MCP configuration for Claude Desktop...[/dim]",
        border_style="cyan"
    ))

    # Try to authenticate to get a real API key
    api_key = "<YOUR_API_KEY>"
    try:
        token = authenticate(backend_url)
        api_key = token
        console.print("[green]✓ Retrieved API key from backend[/green]")
    except SystemExit:
        console.print("[yellow]⚠ Backend not running — using placeholder token[/yellow]")
        console.print("  Start the stack: [cyan]./start.sh[/cyan]\n")

    project_path = str(ROOT.absolute())

    # Option 1: HTTP Streamable-HTTP (Claude Desktop v0.9+)
    http_config = {
        "mcpServers": {
            "pentraai": {
                "url": f"{DEFAULT_MCP_HTTP}",
                "headers": {"Authorization": f"Bearer {api_key}"}
            }
        }
    }

    # Option 2: WSL stdio (works with all Claude Desktop versions)
    wsl_distro = os.getenv("WSL_DISTRO_NAME", "Ubuntu")
    wsl_project = project_path.replace("\\", "/")
    if _WIN_RE.match(project_path):
        drive = project_path[0].lower()
        rest = project_path[2:].lstrip("/\\").replace("\\", "/")
        wsl_project = f"/mnt/{drive}/{rest}"

    mcp_server = None
    for candidate in _MCP_SERVER_CANDIDATES:
        if candidate.exists():
            mcp_server = candidate
            break

    if mcp_server:
        backend_root = mcp_server.parent.parent
        wsl_script = str(mcp_server.absolute()).replace("\\", "/")
        if _WIN_RE.match(wsl_script):
            drive = wsl_script[0].lower()
            rest = wsl_script[2:].lstrip("/\\").replace("\\", "/")
            wsl_script = f"/mnt/{drive}/{rest}"

        wsl_backend_root = str(backend_root.absolute()).replace("\\", "/")
        if _WIN_RE.match(wsl_backend_root):
            drive = wsl_backend_root[0].lower()
            rest = wsl_backend_root[2:].lstrip("/\\").replace("\\", "/")
            wsl_backend_root = f"/mnt/{drive}/{rest}"

        wsl_stdio_config = {
            "mcpServers": {
                "pentraai": {
                    "command": "wsl",
                    "args": [
                        "-d", wsl_distro,
                        "-e", "bash", "-c",
                        f"PYTHONPATH={wsl_backend_root} BACKEND_API_URL={backend_url} "
                        f"DATABASE_URL=postgresql://pentraai:pentraai@localhost:5432/pentraai_assessments "
                        f"AIDA_TOKEN={api_key} "
                        f"{wsl_project}/.venv/bin/python {wsl_script} 2>/dev/null"
                    ]
                }
            }
        }
    else:
        wsl_stdio_config = None

    # Option 3: mcp-remote bridge (SSE/HTTP, works with older Claude Desktop)
    mcp_remote_config = {
        "mcpServers": {
            "pentraai": {
                "command": "wsl",
                "args": [
                    "-d", wsl_distro,
                    "-e", "bash", "-c",
                    f"npx -y mcp-remote {DEFAULT_MCP_HTTP} "
                    f"--header 'Authorization: Bearer {api_key}'"
                ]
            }
        }
    }

    # Save combined config (HTTP method recommended)
    config_path = ROOT / "claude_desktop_config.json"
    config_path.write_text(json.dumps(http_config, indent=2))

    console.print(f"\n[green]✓ Saved:[/green] [cyan]{config_path}[/cyan]")
    console.print()

    console.print("[bold]Claude Desktop Configuration Options:[/bold]\n")
    console.print("[bold]Option 1 — HTTP MCP (Claude Desktop v0.9+, Recommended):[/bold]")
    console.print("[dim]Add to %APPDATA%\\Claude\\claude_desktop_config.json:[/dim]\n")
    console.print(json.dumps(http_config, indent=2))

    if wsl_stdio_config:
        console.print("\n[bold]Option 2 — WSL stdio (All Claude Desktop versions):[/bold]\n")
        console.print(json.dumps(wsl_stdio_config, indent=2))

    console.print("\n[bold]Option 3 — mcp-remote bridge (SSE fallback):[/bold]\n")
    console.print(json.dumps(mcp_remote_config, indent=2))

    console.print("\n[bold yellow]Steps:[/bold yellow]")
    console.print("  1. Start PentraAI: [cyan]./start.sh[/cyan]")
    console.print("  2. Open Claude Desktop → Settings → Developer → Edit Config")
    console.print(f"  3. Paste Option 1 config (copy from: [cyan]{config_path}[/cyan])")
    console.print("  4. Restart Claude Desktop")
    console.print("  5. You'll see [bold]pentraai[/bold] in Claude Desktop's tools list\n")

# ─── Main CLI ─────────────────────────────────────────────────────────────────

@click.command(context_settings={"ignore_unknown_options": True})
@click.argument("subcommand_or_prompt", nargs=-1)
@click.option("-a", "--assessment", help="Load specific assessment")
@click.option("-m", "--model", default=None, help="Model override")
@click.option("--permission-mode", default=None, help="Claude permission mode (default/auto/bypassPermissions)")
@click.option("--preprompt", type=click.Path(), help="Custom preprompt file")
@click.option("--base-url", help="Custom API base URL")
@click.option("--api-key", help="API authentication token")
@click.option("--no-mcp", is_flag=True, help="Disable MCP")
@click.option("--http", "http_url", default=None, help="Use HTTP MCP at this URL")
@click.option("--mcp-api-key", default=None, help="Bearer key for HTTP MCP")
@click.option("--cli", "cli_choice", type=click.Choice(["claude","codex","kimi","qwen","auto"]),
              default="claude", show_default=True, help="AI CLI to use")
@click.option("-y", "--yes", is_flag=True, help="Auto-approve all actions")
@click.option("-q", "--quiet", is_flag=True, help="Minimal output")
@click.option("--debug", is_flag=True, help="Debug mode")
@click.option("--scope", "scope_def", default=None, metavar="TARGETS",
              help="Scope definition: comma-separated domains/IPs/CIDRs, or path to scope.json. "
                   "Written to workspace/scope.json before launching AI. "
                   "Example: --scope 'target.com,*.target.com,10.0.0.0/24'")
@click.option("--out-of-scope", "out_of_scope_def", default=None, metavar="TARGETS",
              help="Explicitly excluded targets (comma-separated). Used with --scope.")
def main(subcommand_or_prompt, assessment, model, permission_mode, preprompt,
         base_url, api_key, no_mcp, http_url, mcp_api_key, cli_choice, yes, quiet, debug,
         scope_def, out_of_scope_def):
    """PentraAI — AI-Driven Autonomous Penetration Testing Platform

    Unified launcher combining AIDA and AstraMitra capabilities.

    \b
    Special subcommands:
      setup-desktop    Generate Claude Desktop MCP configuration

    \b
    Examples:
      python3 pentraai.py -a "target-corp"
      python3 pentraai.py setup-desktop
      python3 pentraai.py -a "target-corp" --yes
      python3 pentraai.py -a "target-corp" --cli auto
      python3 pentraai.py -a "target-corp" --scope "target.com,*.target.com,10.0.0.0/24"
      python3 pentraai.py -a "target-corp" --scope scope.json
      python3 pentraai.py -a "target-corp" --scope "target.com" --out-of-scope "mail.target.com"
    """
    os.system("cls" if os.name == "nt" else "clear")

    # Handle special subcommands
    args_list = list(subcommand_or_prompt)
    if args_list and args_list[0] == "setup-desktop":
        backend_url = os.getenv("BACKEND_API_URL", DEFAULT_BACKEND)
        cmd_setup_desktop(backend_url)
        return

    prompt = tuple(args_list)

    # ── Resolve CLI ──────────────────────────────────────────────────────────
    if cli_choice == "auto":
        cli_type = detect_cli()
        if not cli_type:
            console.print("[red]✗ No supported AI CLI found[/red]\n")
            console.print("Install one:\n  [cyan]npm install -g @anthropic-ai/claude-code[/cyan]")
            sys.exit(1)
    else:
        if not _cli_exists(cli_choice):
            console.print(f"[red]✗ {cli_choice} not found in PATH[/red]")
            sys.exit(1)
        cli_type = cli_choice

    # ── Config ───────────────────────────────────────────────────────────────
    explicit_model   = model or os.getenv("PENTRAAI_MODEL")
    base_url         = base_url or os.getenv("ANTHROPIC_BASE_URL")
    api_key          = api_key or os.getenv("ANTHROPIC_AUTH_TOKEN")
    permission_mode  = permission_mode or os.getenv("PENTRAAI_PERMISSION_MODE", DEFAULT_PERMISSION)
    backend_url      = os.getenv("BACKEND_API_URL", DEFAULT_BACKEND)
    db_url           = os.getenv("DATABASE_URL", "postgresql://pentraai:pentraai@localhost:5432/pentraai_assessments")

    if cli_type == "claude" and (base_url or api_key) and not explicit_model:
        explicit_model = DEFAULT_MODEL

    # ── Authenticate ─────────────────────────────────────────────────────────
    token = authenticate(backend_url)

    # ── Assessment selection ──────────────────────────────────────────────────
    if not assessment:
        console.print()
        console.print("[bold cyan]PentraAI — Security Assessment Assistant[/bold cyan]")
        console.print(f"[dim]CLI: {cli_type.title()}[/dim]\n")
        try:
            with httpx.Client(timeout=5) as c:
                r = c.get(f"{backend_url}/assessments",
                          headers={"Authorization": f"Bearer {token}"})
            if r.status_code != 200:
                console.print("[red]Failed to fetch assessments[/red]\n")
                sys.exit(1)
            assessments = r.json()
            console.print("[bold]Select an assessment:[/bold]\n")
            table = Table(show_header=False, box=None, padding=(0, 2))
            table.add_column(style="cyan bold", justify="right", width=4)
            table.add_column()
            table.add_column(style="dim", no_wrap=True)
            for i, a in enumerate(assessments, 1):
                table.add_row(f"{i}.", a["name"], f"({a.get('container_name','N/A')})")
            if not assessments:
                console.print("[dim]No assessments yet — AI will create one.[/dim]\n")
            else:
                console.print(table)
                console.print()
            choice = console.input("[bold]Enter number (or Enter to skip): [/bold]")
            if choice.lower() == "q":
                sys.exit(0)
            if choice and choice != "0":
                idx = int(choice) - 1
                if 0 <= idx < len(assessments):
                    assessment = assessments[idx]["name"]
                    console.print(f"\n[green]✓[/green] Selected: [cyan]{assessment}[/cyan]\n")
        except (httpx.ConnectError, httpx.ReadTimeout):
            console.print("[red]✗ Backend unreachable — run ./start.sh[/red]\n")
            sys.exit(1)
        except (ValueError, KeyboardInterrupt):
            console.print("\nCancelled.\n")
            sys.exit(0)

    # ── Preprompt ─────────────────────────────────────────────────────────────
    if preprompt:
        pp = Path(preprompt).expanduser().resolve()
        if not pp.exists():
            console.print(f"[red]✗ Preprompt not found: {pp}[/red]\n")
            sys.exit(1)
        preprompt_content = pp.read_text()
    else:
        if not PREPROMPT.exists():
            console.print(f"[red]✗ Missing: {PREPROMPT}[/red]\n")
            sys.exit(1)
        preprompt_content = PREPROMPT.read_text()

    # ── MCP Config ────────────────────────────────────────────────────────────
    codex_mcp_python = None
    if not no_mcp:
        effective_http = http_url or os.getenv("PENTRAAI_MCP_HTTP_URL")
        if effective_http:
            key = mcp_api_key or os.getenv("PENTRAAI_MCP_API_KEY", token)
            if cli_type != "codex":
                gen_http_config(effective_http, key, quiet)
        else:
            # Auto-detect: prefer HTTP (backend running), fall back to stdio
            try:
                with httpx.Client(timeout=2) as c:
                    health = c.get(f"{backend_url.replace('/api', '')}/health")
                if health.status_code == 200:
                    gen_http_config(DEFAULT_MCP_HTTP, token, quiet)
                    if not quiet:
                        console.print(f"[dim]↳ Auto-selected HTTP MCP (backend running)[/dim]")
                else:
                    raise httpx.ConnectError("not healthy")
            except Exception:
                if MCP_SERVER_PATH:
                    gen_stdio_config(db_url, token, quiet)
                    if not quiet:
                        console.print(f"[dim]↳ Auto-selected stdio MCP (local backend)[/dim]")
                else:
                    console.print("[yellow]⚠ Backend not running and no local MCP server found[/yellow]")
                    console.print("  Start the stack: [cyan]./start.sh[/cyan]\n")
                    if not yes:
                        ans = console.input("Continue without MCP? (y/N): ")
                        if ans.lower() != "y":
                            sys.exit(1)
                    no_mcp = True

    # ── Workspace ─────────────────────────────────────────────────────────────
    workspace_path = str(ROOT)
    assessment_id  = None
    container_name = None

    if assessment:
        result = resolve_workspace(assessment, backend_url, token)
        if not result or not result.get("success"):
            console.print(f"[red]✗ Assessment '{assessment}' not found.[/red]")
            console.print("  The AI will create it for you.\n")
            preprompt_content += f"\n\n## Task\nCreate a new assessment named '{assessment}'."
        else:
            raw = result["host_path"]
            workspace_path = translate_path(raw)
            assessment_id  = result["assessment_id"]
            container_name = result["container_name"]

            # ASVS methodology uses a specialized preprompt
            if not preprompt:
                if fetch_methodology(assessment_id, backend_url, token) == "asvs" and PREPROMPT_ASVS.exists():
                    preprompt_content = PREPROMPT_ASVS.read_text()
                    if not quiet:
                        console.print("[green]✓ ASVS assessment — using ASVS preprompt[/green]\n")

            preprompt_content += (
                f"\n\n## Active Assessment\n"
                f"**{assessment}** (ID: {assessment_id}) — Container: {container_name}\n\n"
                f"Before using assessment-scoped tools, call `load_assessment(name=\"{assessment}\")`."
            )
            if not quiet:
                console.print(f"[green]✓ Assessment:[/green] {assessment} (ID: {assessment_id})")

    # ── Scope enforcement ─────────────────────────────────────────────────────
    if scope_def:
        scope_path = Path(workspace_path) / "scope.json"
        src = Path(scope_def).expanduser()
        if src.is_file():
            # User passed a path to an existing scope.json
            import shutil
            shutil.copy2(str(src), str(scope_path))
            if not quiet:
                console.print(f"[green]✓ Scope loaded from:[/green] {src}")
        else:
            # Parse comma-separated targets
            in_scope = [t.strip() for t in scope_def.split(",") if t.strip()]
            out_scope = [t.strip() for t in (out_of_scope_def or "").split(",") if t.strip()]
            scope_data = {"in_scope": in_scope, "out_of_scope": out_scope, "notes": ""}
            scope_path.parent.mkdir(parents=True, exist_ok=True)
            scope_path.write_text(json.dumps(scope_data, indent=2))
            if not quiet:
                console.print(f"[green]✓ Scope defined:[/green] {', '.join(in_scope)}")
                if out_scope:
                    console.print(f"[yellow]  Excluded:[/yellow] {', '.join(out_scope)}")

        # Copy tools/ to workspace so the AI can use them
        tools_src = ROOT / "tools"
        tools_dst = Path(workspace_path) / "tools"
        if tools_src.exists() and not tools_dst.exists():
            import shutil
            shutil.copytree(str(tools_src), str(tools_dst))
            if not quiet:
                console.print(f"[dim]✓ Pro tools copied to workspace[/dim]")
        elif tools_src.exists() and tools_dst.exists():
            # Sync updated tool files
            import shutil
            for tool_file in tools_src.glob("*.py"):
                dst_file = tools_dst / tool_file.name
                shutil.copy2(str(tool_file), str(dst_file))

        preprompt_content += (
            f"\n\n## Scope\n"
            f"Scope is defined in `scope.json` at the workspace root.\n"
            f"**MANDATORY**: Run `execute(\"python3 /workspace/tools/scope_check.py <target>\")` "
            f"before testing any new target. BLOCKED results must be respected immediately."
        )

    elif not quiet and not (scope_def):
        # Warn if no scope defined
        console.print("[yellow]⚠ No --scope defined. AI will remind you to set scope before testing.[/yellow]")

    # ── Build CLI command ─────────────────────────────────────────────────────
    env = os.environ.copy()
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
        env["DISABLE_PROMPT_CACHING"] = "0"
    if api_key:
        env["ANTHROPIC_AUTH_TOKEN"] = api_key
    env["BACKEND_API_URL"] = backend_url
    env["PENTRAAI_TOKEN"] = token
    env["AIDA_TOKEN"] = token

    if cli_type == "claude":
        perm = permission_mode
        if yes and perm == DEFAULT_PERMISSION:
            perm = "auto"
        cli_args = ["claude", "--system-prompt", preprompt_content, "--permission-mode", perm]
        if explicit_model:
            cli_args += ["--model", explicit_model]
        if not no_mcp:
            cli_args += ["--mcp-config", str(MCP_CFG_FILE)]
        if debug:
            cli_args.append("--debug")
        if workspace_path != str(ROOT):
            cli_args += ["--add-dir", str(ROOT)]
        if prompt:
            cli_args.extend(prompt)

    elif cli_type == "codex":
        cli_args = ["codex", "--cd", workspace_path,
                    "--sandbox", "workspace-write", "--ask-for-approval", "on-request"]
        cli_args += ["--config", f"developer_instructions={_toml_val(preprompt_content)}"]
        if explicit_model:
            cli_args += ["--model", explicit_model]
        if not no_mcp:
            effective_http = http_url or os.getenv("PENTRAAI_MCP_HTTP_URL")
            if effective_http:
                key = mcp_api_key or os.getenv("PENTRAAI_MCP_API_KEY", token)
                env["PENTRAAI_MCP_API_KEY"] = key
                cli_args += codex_mcp_args(http_url=effective_http)
            else:
                env.update({
                    "AIDA_TOKEN": token, "PENTRAAI_TOKEN": token,
                    "BACKEND_API_URL": backend_url, "DATABASE_URL": db_url,
                    "PYTHONPATH": str(MCP_SERVER_PATH.parent.parent.absolute()) if MCP_SERVER_PATH else "",
                })
                cli_args += codex_mcp_args(python_bin=_backend_venv_python())
        if workspace_path != str(ROOT):
            cli_args += ["--add-dir", str(ROOT)]
        if yes:
            for flag in ("--ask-for-approval", "--sandbox"):
                idx = next((i for i, x in enumerate(cli_args) if x == flag), None)
                if idx is not None:
                    del cli_args[idx:idx+2]
            cli_args.append("--dangerously-bypass-approvals-and-sandbox")
        if prompt:
            cli_args.append(" ".join(prompt))

    elif cli_type == "kimi":
        kimi_dir = Path(workspace_path) / KIMI_CONFIG_DIR
        kimi_dir.mkdir(parents=True, exist_ok=True)
        (kimi_dir / "AGENTS.md").write_text(preprompt_content)
        if not no_mcp and MCP_CFG_FILE.exists():
            kimi_mcp = kimi_dir / "mcp.json"
            kimi_mcp.write_text(MCP_CFG_FILE.read_text())
            kimi_mcp.chmod(0o600)
        cli_args = ["kimi"]
        if explicit_model:
            cli_args += ["--model", explicit_model]
        if prompt:
            cli_args += ["--prompt", " ".join(prompt)]
        elif yes:
            cli_args.append("--yolo")

    else:  # qwen
        qwen_dir = Path(workspace_path) / ".qwen"
        qwen_dir.mkdir(parents=True, exist_ok=True)
        settings_data = {"modelProviders": {"openai": [{"id": explicit_model or "coder"}]}}
        if not no_mcp and MCP_SERVER_PATH:
            settings_data["mcpServers"] = {
                "pentraai-mcp": {
                    "command": _backend_venv_python(),
                    "args": [str(MCP_SERVER_PATH.absolute())],
                    "env": {"PYTHONPATH": str(MCP_SERVER_PATH.parent.parent.absolute()), "DATABASE_URL": db_url}
                }
            }
        (qwen_dir / "settings.json").write_text(json.dumps(settings_data, indent=2))
        (Path(workspace_path) / "QWEN.md").write_text(preprompt_content)
        cli_args = ["qwen"]
        if prompt:
            cli_args += ["-p", " ".join(prompt)]

    # ── Banner ────────────────────────────────────────────────────────────────
    if not quiet:
        cli_names = {"claude": "Claude Code", "codex": "OpenAI Codex", "kimi": "Kimi Code", "qwen": "Qwen Code"}
        perm_disp = "auto" if yes else permission_mode
        panel_lines = [
            "[bold cyan]PentraAI — Autonomous Pentesting[/bold cyan]",
            "",
            f"[dim]CLI:[/dim]        {cli_names.get(cli_type, cli_type)}",
        ]
        if explicit_model:
            panel_lines.append(f"[dim]Model:[/dim]      {explicit_model}")
        panel_lines += [
            f"[dim]MCP:[/dim]        {'[green]Enabled[/green]' if not no_mcp else '[yellow]Disabled[/yellow]'}",
            f"[dim]Permission:[/dim] {perm_disp}",
            f"[dim]Workspace:[/dim]  {workspace_path}",
        ]
        if assessment:
            panel_lines.append(f"[dim]Assessment:[/dim] [cyan]{assessment}[/cyan] (ID: {assessment_id})")
        if scope_def:
            scope_display = scope_def if len(scope_def) < 50 else scope_def[:47] + "..."
            panel_lines.append(f"[dim]Scope:[/dim]      [green]{scope_display}[/green]")
        else:
            panel_lines.append(f"[dim]Scope:[/dim]      [yellow]Not defined (set via --scope)[/yellow]")
        console.print(Panel("\n".join(panel_lines), border_style="blue", box=box.DOUBLE, padding=(1,2)))
        console.print()
        console.print(f"[dim]Launching {cli_names.get(cli_type, cli_type)}...[/dim]\n")

    # ── Launch ────────────────────────────────────────────────────────────────
    try:
        if cli_type in ("claude", "kimi", "qwen"):
            os.chdir(workspace_path)
        os.execvpe(cli_args[0], cli_args, env)
    except FileNotFoundError:
        console.print(f"[red]✗ {cli_args[0]} not found[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
