"""B13 smoke subset selection contract."""
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
PY = sys.executable

PREFLIGHT = "tests/test_control.py::test_preflight_is_pure_no_model"
CANDIDATE = "tests/test_candidate_contract.py::test_candidate_blocked_is_never_ready"
EVIDENCE = (
    "tests/test_evidence_environment.py::"
    "test_real_green_project_test_runs_and_evidence_is_private"
)
GROUPING = (
    "tests/test_command_contract.py::"
    "test_full_command_groups_preflight_permission_prompt_and_final_only"
)
REWORK = (
    "tests/test_rework_budget.py::"
    "test_three_quality_failures_block_before_cli"
)
SLOW_BOOTSTRAP = (
    "tests/test_bootstrap.py::"
    "test_real_isolated_preflight_is_wired_through_bootstrap"
)


def _pytest(*args, timeout=120):
    return subprocess.run(
        [PY, "-m", "pytest", "tests/", *args, "-q", "-p", "no:cacheprovider"],
        cwd=PROJECT,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def smoke_nodeids():
    proc = _pytest("-m", "smoke", "--collect-only")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return [
        line.strip()
        for line in proc.stdout.splitlines()
        if "::" in line and not line.lstrip().startswith("=")
    ]


def test_smoke_selection_size_and_required_categories():
    selected = smoke_nodeids()
    assert 8 <= len(selected) <= 30, selected
    for nodeid in (PREFLIGHT, CANDIDATE, EVIDENCE, GROUPING, REWORK):
        assert nodeid in selected, nodeid
    assert SLOW_BOOTSTRAP not in selected


def test_smoke_subset_is_registered_green_and_fast():
    started = time.monotonic()
    proc = _pytest("-m", "smoke", "-W", "error::pytest.PytestUnknownMarkWarning")
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert elapsed < 60, elapsed
