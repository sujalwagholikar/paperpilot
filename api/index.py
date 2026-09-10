"""Vercel entrypoint for PaperPilot Studio.

The original index.html, server.py, and doc.py remain unchanged on disk.
This adapter executes server.py from memory with its runtime workspace redirected
to /tmp before the module is initialized. This is necessary because server.py
creates its storage directory during import, while Vercel deployment files are
read-only at runtime outside /tmp.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

SERVER_SOURCE = APP_DIR / "server.py"
source = SERVER_SOURCE.read_text(encoding="utf-8")

# Preserve the core server.py byte-for-byte while changing only its in-memory
# initialization so its storage path is writable on Vercel.
needle = 'STORAGE = BASE_DIR / "storage"'
replacement = 'STORAGE = Path("/tmp/paperpilot/storage")'
if needle not in source:
    raise RuntimeError("PaperPilot server.py format changed; Vercel adapter needs review.")
source = source.replace(needle, replacement, 1)

module = types.ModuleType("paperpilot_server_runtime")
module.__file__ = str(SERVER_SOURCE)
module.__package__ = ""
sys.modules[module.__name__] = module
exec(compile(source, str(SERVER_SOURCE), "exec"), module.__dict__)

app = module.app
