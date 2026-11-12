"""Shared pytest configuration for the coding-control suite."""
import os
from pathlib import Path

import pytest

# Point the controller's optional content scanner at the bundled fixture so
# secret/downgrade scanning is exercised in tests. Subprocess invocations of
# coding_control.py / tools/git_gate.py inherit this from the test process.
_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("CODING_CONTROL_SECRET_SCAN", str(_ROOT / "tools" / "secret_scan.py"))


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "smoke: fast, high-signal contract subset for quick feedback",
    )
