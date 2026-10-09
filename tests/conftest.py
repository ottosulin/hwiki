import sys
from pathlib import Path

# Let tests import the `_hwiki_path` helper that registers the repo root as package `hwiki`.
sys.path.insert(0, str(Path(__file__).resolve().parent))
