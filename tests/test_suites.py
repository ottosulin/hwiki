"""Run the script-style suites under pytest so `pytest tests/` covers everything.

Each suite is also runnable on its own (`python tests/test_provider.py`) and needs
no Hermes install. Here each runs in a fresh subprocess, because the provider and
CLI suites deliberately manipulate module registration and HERMES_HOME.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent


@pytest.mark.parametrize("script", ["test_store_search.py", "test_provider.py", "test_cli_load.py"])
def test_suite_passes(script: str) -> None:
    proc = subprocess.run(
        [sys.executable, script], cwd=TESTS, capture_output=True, text=True, timeout=300,
    )
    tail = "\n".join(proc.stdout.strip().splitlines()[-25:])
    assert proc.returncode == 0, f"{script} failed:\n{tail}\n{proc.stderr[-2000:]}"
    assert " 0 failed" in proc.stdout, tail
