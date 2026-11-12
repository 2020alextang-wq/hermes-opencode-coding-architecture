"""B15-T: executable behavior matrix for the frozen control scenarios."""
import os
import signal
import subprocess
import time
from pathlib import Path

from test_control import CONTROL, PY, change, e, invoke, ledger, mode, wait_for

PROJECT = Path(__file__).resolve().parents[1]

MATRIX = {
    "timeout_not_ready": (
        "tests/test_control.py::test_silent_process_times_out"
    ),
    "cancel_not_ready": (
        "tests/test_control.py::test_cancel_kills_grandchild_even_when_leader_exited"
    ),
    "no_candidate_exit0_not_ready": (
        "tests/test_control.py::test_no_new_assistant_message_is_unknown"
    ),
    "removed_assertion_failed": (
        "tests/test_control.py::test_content_scan_deleted_assertion_fails_run"
    ),
    "command_groups_final_gate_only": (
        "tests/test_command_contract.py::"
        "test_full_command_groups_preflight_permission_prompt_and_final_only"
    ),
    "token_equal_limit_budget_stop": (
        "tests/test_rework_budget.py::"
        "test_token_equal_to_limit_stops_as_budget_stop_with_complete_budget"
    ),
    "unauthorized_delete_failed": (
        "tests/test_delete_contract.py::"
        "test_deleted_unauthorized_file_is_attributed_to_deleted_file"
    ),
    "unknown_skill_blocked": (
        "tests/test_skill_manifest.py::test_unknown_skill_blocks_before_cli"
    ),
}


def test_matrix_has_required_scenarios():
    required = {
        "timeout_not_ready",
        "cancel_not_ready",
        "no_candidate_exit0_not_ready",
        "removed_assertion_failed",
        "command_groups_final_gate_only",
        "token_equal_limit_budget_stop",
        "unauthorized_delete_failed",
        "unknown_skill_blocked",
    }
    assert len(MATRIX) >= 8
    assert required <= set(MATRIX)


def test_matrix_nodeids_exist():
    proc = subprocess.run(
        [PY, "-m", "pytest", "tests/", "--collect-only", "-q",
         "-p", "no:cacheprovider"],
        cwd=PROJECT,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    collected = {
        line.strip()
        for line in proc.stdout.splitlines()
        if "::" in line and not line.lstrip().startswith("=")
    }
    missing = sorted(nodeid for nodeid in MATRIX.values() if nodeid not in collected)
    assert not missing, "missing nodeids: " + ", ".join(missing)


def test_cancel_kills_spawn_child_before_marker(e):
    mode(e, "spawn_child")
    change(e, timeout_seconds=60)
    runner = subprocess.Popen(
        [PY, str(CONTROL), "run", str(e["task"])],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "CODING_CONTROL_SETTINGS": str(e["settings"])},
    )
    try:
        wait_for(e["tmp"] / "record.json")
        rid = next((e["tmp"] / "state/runs").iterdir()).name
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if ledger(e, rid)["status"] == "RUNNING":
                break
            time.sleep(.05)
        else:
            raise AssertionError("run did not reach RUNNING before cancel")
        cancelled, _ = invoke(e, "cancel", rid, timeout=6)
        assert cancelled["status"] == "CANCELLED"
        runner.communicate(timeout=8)
        time.sleep(6)
        assert not (e["tmp"] / "child_marker").exists()
        assert ledger(e, rid)["status"] == "CANCELLED"
    finally:
        if runner.poll() is None:
            runner.terminate()
            runner.communicate(timeout=3)
        state = e["tmp"] / "state/runs"
        if state.exists():
            for folder in state.iterdir():
                info = ledger(e, folder.name)
                if info.get("child_pgid"):
                    try:
                        os.killpg(info["child_pgid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass


def test_silent_exit0_without_candidate_is_failed(e):
    mode(e, "silent_exit0")
    data, code = invoke(e, "run", e["task"])
    info = ledger(e, data["run_id"])
    assert data["status"] == "FAILED" and code != 0
    assert "missing candidate envelope" in (info.get("error") or "")
