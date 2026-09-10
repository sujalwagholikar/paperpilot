"""Vercel entrypoint for PaperPilot Studio.

This adapter intentionally leaves the original frontend and processing files
untouched. It imports the existing FastAPI application and redirects its
runtime job workspace to /tmp, which is the writable filesystem available to
Vercel Functions.

Important: /tmp is ephemeral and not shared as durable application storage.
For production-grade long-running jobs, move job metadata/files to external
storage + a queue/worker. The existing API contract remains unchanged here.
"""
from __future__ import annotations

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import server as _server  # noqa: E402

# Vercel Functions expose a writable /tmp directory. The original server.py
# keeps JOBS as a module-level path, so replacing the global preserves the
# existing code and API without modifying the core file.
VERCEL_JOB_ROOT = Path("/tmp/paperpilot/jobs")
VERCEL_JOB_ROOT.mkdir(parents=True, exist_ok=True)
_server.JOBS = VERCEL_JOB_ROOT
_server.STORAGE = VERCEL_JOB_ROOT.parent

app = _server.app
