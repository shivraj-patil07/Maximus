"""
MAXIMUS Backend — extends AIDA with Pro Tools API
===================================================
This module imports AIDA's FastAPI app and mounts the
MAXIMUS Pro Tools router (/api/v1/pro/*) on top of it.
"""

import sys
import os

# Ensure AIDA modules are importable
sys.path.insert(0, "/app")

from bootstrap_secrets import ensure_secret_key
ensure_secret_key()

# Import AIDA's fully configured FastAPI app (this also runs lifespan + MCP setup)
from main import app  # noqa: F401  — re-exported as the WSGI entrypoint

from fastapi import Depends
from auth import get_current_user
from api.pro_tools import router as pro_tools_router

# Mount Pro Tools under the same /api/v1 prefix, protected by JWT auth
app.include_router(
    pro_tools_router,
    prefix="/api/v1",
    dependencies=[Depends(get_current_user)],
)
