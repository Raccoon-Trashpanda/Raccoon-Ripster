#!/usr/bin/env python
"""Ripster — autonomous launcher (from source). Double-click / `python ripster_launcher.py`:
starts the server and opens the UI in its own native window (pywebview, falls back
to the browser). Lightweight replacement for the 35 MB RipsterLauncher.exe.

All logic lives in ripster/launcher.py (so it's covered by the import test-net).
"""
import os, sys
# Bundled-Python (embeddable ._pth) doesn't add the script dir to sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ripster.launcher import main

if __name__ == "__main__":
    main()
