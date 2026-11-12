"""Command group authorization contract for the controller (B6/F8)."""
import json
import shlex
import sys

import pytest

from test_control import change, e, invoke, ledger

PY = sys.executable
FINAL = [PY, "-m", "pytest", "tests/test_control.py", "-q"]
ITERATION = [PY, "-m", "pytest", "tests/test_iteration_gate.py", "-q"]
BLOCK = [PY, "-m", "pytest", "tests/test_block_gate.py", "-q"]
FINAL_ONLY = {"iteration": [], "block_gate": [], "final_gate": [FINAL]}
ALL_GROUPS = {"iteration": [ITERATION], "block_gate": [BLOCK], "final_gate": [FINAL]}


def set_groups(e, groups):
    task = json.loads(e["task"].read_text())
    task.pop("test_commands", None)
    task["command_groups"] = groups
    e["task"].write_text(json.dumps(task))


def marker_test(e, name, marker):
    (e["project"] / "tests" / name).write_text(
        "import pathlib\n\n\n"
        "def test_marker():\n"
        f"    target = pathlib.Path({str(marker)!r})\n"
        "    target.write_text('ran')\n"
        "    assert target.read_text() == 'ran'\n")


def evidence(e, rid):
    path = e["tmp"] / "state" / "runs" / rid / "test_evidence.json"
    return json.loads(path.read_text())


@pytest.mark.smoke
def test_full_command_groups_preflight_permission_prompt_and_final_only(e):
    iteration_marker = e["project"] / "iteration-ran.marker"
    block_marker = e["project"] / "block-ran.marker"
    marker_test(e, "test_iteration_gate.py", iteration_marker)
    marker_test(e, "test_block_gate.py", block_marker)
    set_groups(e, ALL_GROUPS)

    checked, code = invoke(e, "preflight", e["task"])
    assert checked["status"] == "ok" and code == 0
    assert checked["command_groups"] == ALL_GROUPS
    assert set(checked["command_groups"]) == {"iteration", "block_gate", "final_gate"}

    result, code = invoke(e, "run", e["task"])
    assert result["status"] == "READY_FOR_REVIEW" and code == 0
    record = json.loads((e["tmp"] / "record.json").read_text())
    bash = json.loads(record["config"])["permission"]["bash"]
    assert bash["*"] == "deny"
    for argv in (ITERATION, BLOCK, FINAL):
        assert bash[shlex.join(argv)] == "allow"
    prompt = record["prompt"]
    assert "Iteration commands" in prompt
    assert "Block gate" in prompt
    assert "Final gate" in prompt
    assert "Final gate is executed independently by the controller; all other commands are merely authorized" in prompt
    assert not iteration_marker.exists()
    assert not block_marker.exists()

    record = evidence(e, result["run_id"])
    assert record["status"] == "passed"
    assert len(record["commands"]) == 1
    assert record["commands"][0]["argv"] == FINAL
    assert record["commands"][0]["group"] == "final_gate"
    info = ledger(e, result["run_id"])
    assert info["effective_groups"] == ALL_GROUPS
    assert info["executed_groups"] == ["final_gate"]


@pytest.mark.smoke
def test_omitted_iteration_and_block_gate_are_empty(e):
    set_groups(e, {"final_gate": [FINAL]})
    checked, code = invoke(e, "preflight", e["task"])
    assert checked["status"] == "ok" and code == 0
    assert checked["command_groups"] == FINAL_ONLY
    result, code = invoke(e, "run", e["task"])
    assert result["status"] == "READY_FOR_REVIEW" and code == 0
    info = ledger(e, result["run_id"])
    assert info["effective_groups"] == FINAL_ONLY
    assert info["executed_groups"] == ["final_gate"]


@pytest.mark.parametrize("op", ["preflight", "run"])
def test_command_groups_and_test_commands_are_mutually_exclusive(e, op):
    change(e, command_groups=ALL_GROUPS)
    data, code = invoke(e, op, e["task"])
    assert data["status"] == "BLOCKED" and code != 0
    assert "command_groups and test_commands are mutually exclusive" in json.dumps(
        data, ensure_ascii=False)
    assert not (e["tmp"] / "record.json").exists()


@pytest.mark.parametrize("groups", [
    {"iteration": [ITERATION]},
    {"iteration": [ITERATION], "block_gate": []},
    {"iteration": [], "block_gate": [], "final_gate": []},
    {"final_gate": []},
])
def test_final_gate_missing_or_empty_blocks(e, groups):
    set_groups(e, groups)
    for op in ("preflight", "run"):
        data, code = invoke(e, op, e["task"])
        assert data["status"] == "BLOCKED" and code != 0
        assert "final_gate" in json.dumps(data, ensure_ascii=False)
    assert not (e["tmp"] / "record.json").exists()


@pytest.mark.parametrize("group,bad", [
    ("iteration", [PY, "-m", "pytest", "tests/bad;touch marker"]),
    ("iteration", [PY, "-c", "print(1)"]),
    ("block_gate", [PY, "-m", "pytest", "tests/*.py"]),
    ("block_gate", [PY, "-m", "pytest", "/tmp/absolute_test.py"]),
])
def test_invalid_argv_in_any_group_blocks_and_names_group(e, group, bad):
    groups = {"iteration": [], "block_gate": [], "final_gate": [FINAL]}
    groups[group] = [bad]
    set_groups(e, groups)
    for op in ("preflight", "run"):
        data, code = invoke(e, op, e["task"])
        assert data["status"] == "BLOCKED" and code != 0
        assert group in json.dumps(data, ensure_ascii=False)
    assert not (e["tmp"] / "record.json").exists()


@pytest.mark.smoke
def test_legacy_test_commands_are_final_gate_only(e):
    original = json.loads(e["task"].read_text())["test_commands"]
    checked, code = invoke(e, "preflight", e["task"])
    assert checked["status"] == "ok" and code == 0
    assert checked["command_groups"] == {
        "iteration": [], "block_gate": [], "final_gate": original}
    result, code = invoke(e, "run", e["task"])
    assert result["status"] == "READY_FOR_REVIEW" and code == 0
    record = json.loads((e["tmp"] / "record.json").read_text())
    bash = json.loads(record["config"])["permission"]["bash"]
    assert bash["*"] == "deny"
    for argv in original:
        assert bash[shlex.join(argv)] == "allow"
    evidence_record = evidence(e, result["run_id"])
    assert [command["argv"] for command in evidence_record["commands"]] == original
    assert [command["group"] for command in evidence_record["commands"]] == ["final_gate"]
    info = ledger(e, result["run_id"])
    assert info["effective_groups"] == {
        "iteration": [], "block_gate": [], "final_gate": original}
    assert info["executed_groups"] == ["final_gate"]


def test_real_final_gate_failure_is_failed_with_real_exit_code(e):
    (e["project"] / "tests/test_final_gate_fail.py").write_text(
        "def test_final_gate_failure():\n    assert 1 == 2\n")
    failing = [PY, "-m", "pytest", "tests/test_final_gate_fail.py", "-q"]
    set_groups(e, {"iteration": [], "block_gate": [], "final_gate": [failing]})
    result, code = invoke(e, "run", e["task"])
    assert result["status"] == "FAILED" and code != 0
    record = evidence(e, result["run_id"])
    assert record["status"] == "failed"
    assert len(record["commands"]) == 1
    command = record["commands"][0]
    assert command["argv"] == failing
    assert command["group"] == "final_gate"
    assert isinstance(command["exit_code"], int) and command["exit_code"] != 0
    assert ledger(e, result["run_id"])["status"] == "FAILED"
