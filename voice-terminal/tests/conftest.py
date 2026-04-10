"""Shared fixtures for voice-terminal tests."""

import sys
from pathlib import Path

# Add voice-terminal/ to path so `src` is importable as a package
voice_dir = str(Path(__file__).parent.parent)
if voice_dir not in sys.path:
    sys.path.insert(0, voice_dir)
