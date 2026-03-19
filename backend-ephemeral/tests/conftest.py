"""Shared fixtures for backend-ephemeral tests."""

import sys
from pathlib import Path

# Add backend-ephemeral/ to path so `src` is importable as a package
backend_dir = str(Path(__file__).parent.parent)
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)
