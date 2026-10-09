"""Make the repository root importable as the package ``hwiki``.

The plugin lives at the repo root (Hermes installs it as ``$HERMES_HOME/plugins/hwiki``),
so tests load it under that package name regardless of the checkout folder name.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if "hwiki" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "hwiki", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["hwiki"] = _mod
    _spec.loader.exec_module(_mod)
