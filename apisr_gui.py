#!/usr/bin/env python3
"""APISR Upscaler launcher.  Run with the system Python (needs python3-gobject, libadwaita)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from apisr_ui.ui import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(__file__))
