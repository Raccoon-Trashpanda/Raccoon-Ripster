"""Shared pytest setup: put the project root on sys.path so `import ripster`
and `import app` resolve regardless of where pytest is invoked from."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
