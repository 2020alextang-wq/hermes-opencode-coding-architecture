"""Contract tests for the batch subcommand: cross-project parallel scheduling."""
import json
import os
import subprocess
import time

import pytest

from test_control import CONTROL, PY, e, mode


def make_project(root, name):
    project = root / name
    (project / "src").mkdir(parents=True)
    (project / "src/main.py").write_text("original\n")
    (project / "tests").mkdir()
    (project / "tests/test_control.py").write_text(
        "def test_batch_fixture():\n    assert 1 + 1 == 2\n")
    (project / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n*.pyc\n.fake_state/\n")
    for cmd in (["git", "init"],
                ["git", "add", "src/main.py", "tests/test_control.py", ".gitignore"]):
        subprocess.run(cmd, cwd=project, check=True, capture_output=True, timeout=5)
    return project


def write_task(tmp_path, name, project, **fields):
    data = {"project": str(project), "goal": "Update src/main.py",
            "allowed_files": ["src/main.py"],
            "test_commands": [[PY, "-m", "pytest", "tests/test_control.py", "-q"]],
            "timeout_seconds": 10, "max_tokens": 100, "max_permission_denials": 2}
    data.update(fields)
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(data))
    return path


def invoke_batch(e, payload, timeout=40, batch_name="batch.json"):
    path = e["tmp"] / batch_name
    path.write_text(json.dumps(payload))
    started = time.monotonic()
    r = subprocess.run([PY, str(CONTROL), "batch", str(path)], text=True,
                       capture_output=True, timeout=timeout,
                       env={**os.environ, "CODING_CONTROL_SETTINGS": str(e["settings"])})
    return r, time.monotonic() - started


def parse(r):
    assert r.stdout.strip(), (r.returncode, r.stderr)
    return json.loads(r.stdout)


def ledger_for(e, rid):
    return json.loads((e["tmp"] / "state/runs" / rid / "ledger.json").read_text())


def two_projects(e):
    second = make_project(e["root"], "project2")
    return e["project"], second


def test_two_projects_jobs_two_run_in_parallel(e):
    first, second = two_projects(e)
    mode(e, "slow")
    one = write_task(e["tmp"], "task-one", first)
    two = write_task(e["tmp"], "task-two", second)
    r, elapsed = invoke_batch(e, {"jobs": 2, "tasks": [str(one), str(two)]})
    data = parse(r)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert data["status"] == "ok"
    assert [run["task"] for run in data["runs"]] == [str(one), str(two)]
    assert [run["status"] for run in data["runs"]] == ["READY_FOR_REVIEW"] * 2
    assert data["runs"][0]["run_id"] != data["runs"][1]["run_id"]
    assert ledger_for(e, data["runs"][0]["run_id"])["project"] == str(first.resolve())
    assert ledger_for(e, data["runs"][1]["run_id"])["project"] == str(second.resolve())
    assert elapsed < 4.5, elapsed


def test_same_two_tasks_jobs_one_are_serial(e):
    first, second = two_projects(e)
    mode(e, "slow")
    one = write_task(e["tmp"], "task-one", first)
    two = write_task(e["tmp"], "task-two", second)
    r, elapsed = invoke_batch(e, {"jobs": 1, "tasks": [str(one), str(two)]})
    data = parse(r)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert [run["status"] for run in data["runs"]] == ["READY_FOR_REVIEW"] * 2
    assert data["runs"][0]["run_id"] != data["runs"][1]["run_id"]
    assert elapsed >= 4.0, elapsed


def test_same_project_scheduled_twice_second_is_blocked(e):
    first = e["project"]
    mode(e, "normal")
    one = write_task(e["tmp"], "task-one", first)
    two = write_task(e["tmp"], "task-two", first)
    r, _ = invoke_batch(e, {"jobs": 2, "tasks": [str(one), str(two)]})
    data = parse(r)
    assert r.returncode == 1
    assert data["status"] != "ok"
    assert [run["task"] for run in data["runs"]] == [str(one), str(two)]
    assert data["runs"][0]["status"] == "READY_FOR_REVIEW"
    assert data["runs"][1]["status"] == "BLOCKED"
    assert "project already scheduled" in (data["runs"][1].get("error") or "")


def test_any_blocked_preflight_runs_nothing(e):
    first, second = two_projects(e)
    mode(e, "normal")
    good = write_task(e["tmp"], "task-good", first)
    bad = write_task(e["tmp"], "task-bad", second, allowed_files=[])
    r, _ = invoke_batch(e, {"jobs": 2, "tasks": [str(good), str(bad)]})
    data = parse(r)
    assert r.returncode == 1
    assert data["status"] == "blocked"
    assert data["runs"] == []
    assert not (e["tmp"] / "record.json").exists()


@pytest.mark.parametrize("payload", [
    {"jobs": 0},
    {"jobs": 5},
    {"jobs": "2"},
])
def test_invalid_jobs_exits_two(e, payload):
    payload = dict(payload, tasks=[str(e["task"])])
    r, _ = invoke_batch(e, payload)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "invalid jobs" in (r.stdout + r.stderr)


def test_missing_tasks_exits_two(e):
    r, _ = invoke_batch(e, {"jobs": 1})
    assert r.returncode == 2, (r.stdout, r.stderr)


def test_missing_batch_file_exits_two(e):
    r = subprocess.run([PY, str(CONTROL), "batch", str(e["tmp"] / "missing.json")],
                       text=True, capture_output=True, timeout=10,
                       env={**os.environ, "CODING_CONTROL_SETTINGS": str(e["settings"])})
    assert r.returncode == 2, (r.stdout, r.stderr)


def test_jobs_defaults_to_one(e):
    first, second = two_projects(e)
    mode(e, "normal")
    one = write_task(e["tmp"], "task-one", first)
    two = write_task(e["tmp"], "task-two", second)
    r, _ = invoke_batch(e, {"tasks": [str(one), str(two)]})
    data = parse(r)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert [run["status"] for run in data["runs"]] == ["READY_FOR_REVIEW"] * 2
    assert data["runs"][0]["run_id"] != data["runs"][1]["run_id"]
