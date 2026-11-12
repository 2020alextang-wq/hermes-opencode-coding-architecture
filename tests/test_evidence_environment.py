"""Independent real-project tests for controller test evidence and minimal env (B2/F3+F4)."""
import importlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

PY = sys.executable
CONTROL = Path(__file__).resolve().parents[1] / "coding_control.py"
TOKENS = {"total": 19, "input": 10, "output": 5, "reasoning": 2,
          "cache": {"read": 3, "write": 1}}

FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path(__file__).parent
mode = (root / "mode").read_text().strip()
record = {
    "cwd": os.getcwd(),
    "args": sys.argv[1:],
    "config": os.environ.get("OPENCODE_CONFIG_CONTENT"),
    "prompt": sys.argv[-1],
    "home": os.environ.get("HOME"),
    "env_present": {name: name in os.environ for name in (
        "HOME", "OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT", "OPENCODE_CONFIG_DIR",
        "OPENAI_API_KEY", "VENDOR_API_KEY", "DATABASE_URL", "PGPASSWORD",
        "AWS_SECRET_ACCESS_KEY", "PYTHONPATH", "PYTEST_ADDOPTS",
        "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "CODING_CONTROL_SETTINGS")},
}
if sys.argv[1] == "export":
    print(json.dumps({"info": {"id": "ses_test"},
                      "messages": json.loads((root / "messages.json").read_text())}))
    sys.exit(0)
if sys.argv[1:] == ["--version"]:
    print("1.2.3")
    sys.exit(0)
if sys.argv[1:] == ["run", "--help"]:
    print("--dir" + chr(10) + "--agent" + chr(10) + "--format")
    sys.exit(0)
(root / "record.json").write_text(json.dumps(record))
if mode == "error":
    print(json.dumps({"type": "error", "sessionID": "ses_test", "error": "model failed"}), flush=True)
    sys.exit(0)
print(json.dumps({"type": "step_finish", "sessionID": "ses_test",
                  "part": {"id": "s1", "tokens": {"total": 19, "input": 10, "output": 5,
                                                  "reasoning": 2, "cache": {"read": 3, "write": 1}}}}),
      flush=True)
if mode != "review_clean":
    pathlib.Path("src/main.py").write_text("changed by executor" + chr(10))
operation = "review" if mode == "review_clean" else "implement"
changed = [] if operation == "review" else ["src/main.py"]
candidate = {"operation": operation, "status": "READY_FOR_REVIEW",
             "changed_files": changed, "unverified": []}
messages = json.loads((root / "messages.json").read_text())
messages.append({"info": {"id": "m1", "role": "assistant", "providerID": "test-provider",
                          "modelID": "test-model", "tokens": {"total": 19, "input": 10, "output": 5,
                                                              "reasoning": 2,
                                                              "cache": {"read": 3, "write": 1}},
                          "time": {"completed": 1}},
                 "parts": [{"type": "text", "text": "CODING_CONTROL_CANDIDATE " +
                           json.dumps(candidate, separators=(",", ":"))}]})
(root / "messages.json").write_text(json.dumps(messages))
'''

GREEN = "def test_green():\n    assert 1 + 1 == 2\n"
HANG = '''import os, pathlib, time


def test_hang_with_grandchild():
    home = pathlib.Path(os.environ["HOME"])
    pid = os.fork()
    if pid == 0:
        grand = os.fork()
        if grand == 0:
            (home / "grandpid").write_text(str(os.getpid()))
            while True:
                time.sleep(0.2)
        (home / "childpid").write_text(str(os.getpid()))
        while True:
            time.sleep(0.2)
    while not (home / "grandpid").exists():
        time.sleep(0.02)
    time.sleep(60)
'''

ENV_PROBE = '''import json, os, pathlib


def test_records_environment():
    info = {
        "keys": sorted(os.environ),
        "home": os.environ.get("HOME"),
        "tmpdir": os.environ.get("TMPDIR"),
        "xdg": os.environ.get("XDG_CACHE_HOME"),
        "addopts": os.environ.get("PYTEST_ADDOPTS"),
        "dotenv": os.environ.get("PYTHON_DOTENV_DISABLED"),
        "bytecode": os.environ.get("PYTHONDONTWRITEBYTECODE"),
        "mode": os.environ.get("TEST_CASE_MODE"),
    }
    pathlib.Path(os.environ["HOME"], "env.json").write_text(json.dumps(info))
    assert info["home"] == os.environ["HOME"]
    assert info["dotenv"] == "1" and info["bytecode"] == "1"
    assert info["addopts"] == "-p no:cacheprovider"
'''

FORBIDDEN_KEYS = ("OPENAI_API_KEY", "VENDOR_API_KEY", "DATABASE_URL", "PGPASSWORD",
                  "AWS_SECRET_ACCESS_KEY", "PYTHONPATH", "LD_PRELOAD",
                  "DYLD_INSERT_LIBRARIES", "CODING_CONTROL_SETTINGS",
                  "OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT", "OPENCODE_CONFIG_DIR")


@pytest.fixture
def b(tmp_path):
    root = tmp_path / "workspace"
    project = root / "project"
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "src/main.py").write_text("original\n")
    (project / "tests/project_test.py").write_text(GREEN)
    (project / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.pyc\n")
    for cmd in (["git", "init"],
                ["git", "add", "src/main.py", "tests/project_test.py", ".gitignore"]):
        subprocess.run(cmd, cwd=project, check=True, capture_output=True, timeout=3)
    cli = tmp_path / "fake_cli.py"
    cli.write_text(FAKE)
    cli.chmod(0o755)
    (tmp_path / "mode").write_text("normal")
    (tmp_path / "messages.json").write_text("[]")
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"workspace_root": str(root), "cli": str(cli),
                                    "runner_model": "test-provider/test-model",
                                    "state_dir": str(tmp_path / "state")}))
    task = tmp_path / "task.json"
    task.write_text(json.dumps({
        "project": str(project), "goal": "Change src/main.py",
        "allowed_files": ["src/main.py"],
        "test_commands": [[PY, "-m", "pytest", "tests/project_test.py", "-q"]],
        "timeout_seconds": 5, "test_timeout_seconds": 10,
        "max_tokens": 1000, "max_permission_denials": 2}))
    return {"root": root, "project": project, "tmp": tmp_path,
            "settings": settings, "task": task}


def invoke(b, op, value, timeout=20, extra_env=None):
    env = {**os.environ, "CODING_CONTROL_SETTINGS": str(b["settings"])}
    if extra_env:
        env.update(extra_env)
    r = subprocess.run([PY, str(CONTROL), op, str(value)], text=True, capture_output=True,
                       timeout=timeout, env=env)
    assert r.stdout.strip(), (r.returncode, r.stderr)
    return json.loads(r.stdout), r.returncode


def change_task(b, **fields):
    task = json.loads(b["task"].read_text())
    task.update(fields)
    b["task"].write_text(json.dumps(task))


def set_project_test(b, source):
    (b["project"] / "tests/project_test.py").write_text(source)


def set_mode(b, name):
    (b["tmp"] / "mode").write_text(name)


def run_dir(b, rid):
    return b["tmp"] / "state/runs" / rid


def ledger(b, rid):
    return json.loads((run_dir(b, rid) / "ledger.json").read_text())


def evidence(b, rid):
    return json.loads((run_dir(b, rid) / "test_evidence.json").read_text())


def wait_for(path, timeout=8):
    until = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < until:
        time.sleep(0.02)
    assert path.exists(), path


def live(pid):
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], text=True,
                       capture_output=True, timeout=2)
    return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")


def assert_dead(paths):
    for path in paths:
        assert not live(int(path.read_text())), path


@pytest.mark.smoke
def test_real_green_project_test_runs_and_evidence_is_private(b):
    data, code = invoke(b, "run", b["task"])
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
    record = evidence(b, data["run_id"])
    assert record["status"] == "passed" and record["not_run_reason"] is None
    assert len(record["commands"]) == 1
    command = record["commands"][0]
    assert command["argv"][:4] == [PY, "-m", "pytest", "tests/project_test.py"]
    assert command["cwd"] == str(b["project"])
    assert command["exit_code"] == 0
    assert command["started_at"] <= command["ended_at"]
    log = Path(command["log_path"])
    assert log.is_file() and "1 passed" in log.read_text()
    assert record["candidate_before"] == record["candidate_after"]
    assert record["candidate_changed"] is False
    assert ledger(b, data["run_id"])["tests"]["status"] == "passed"
    folder = run_dir(b, data["run_id"])
    assert stat.S_IMODE((folder / "tests").stat().st_mode) == 0o700
    for path in (folder / "test_evidence.json", log):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert log.is_relative_to(folder)
    assert not (b["project"] / "__pycache__").exists()
    assert not (b["project"] / ".pytest_cache").exists()
    assert not (b["project"] / "tests/__pycache__").exists()


@pytest.mark.smoke
def test_real_red_project_test_prevents_ready_and_log_shows_failure(b):
    set_project_test(b, "def test_red():\n    assert 1 == 2\n")
    data, code = invoke(b, "run", b["task"])
    assert data["status"] == "FAILED" and code != 0
    info = ledger(b, data["run_id"])
    assert info["candidate"]["status"] == "READY_FOR_REVIEW"
    assert "verification tests failed" in info["error"]
    record = evidence(b, data["run_id"])
    assert record["status"] == "failed"
    command = record["commands"][0]
    assert command["exit_code"] != 0
    log = Path(command["log_path"]).read_text()
    assert "1 failed" in log and "test_red" in log


@pytest.mark.parametrize("target", ["src/main.py", "src/extra.py"])
def test_test_side_source_change_blocks_ready(b, target):
    set_project_test(b, "import pathlib\n\n\ndef test_mutates():\n"
                        f"    target = pathlib.Path({target!r})\n"
                        "    target.write_text('mutated\\n')\n"
                        "    assert target.read_text() == 'mutated\\n'\n")
    data, code = invoke(b, "run", b["task"])
    assert data["status"] == "FAILED" and code != 0
    assert "tests-modified-candidate" in ledger(b, data["run_id"])["error"]
    record = evidence(b, data["run_id"])
    assert record["status"] == "passed"
    assert record["candidate_changed"] is True
    assert record["candidate_before"] != record["candidate_after"]


def test_test_timeout_kills_process_group_including_grandchild(b):
    set_project_test(b, HANG)
    change_task(b, test_timeout_seconds=2)
    data, code = invoke(b, "run", b["task"], timeout=15)
    assert data["status"] == "TIMEOUT" and code != 0
    record = evidence(b, data["run_id"])
    assert record["status"] == "timeout"
    assert record["commands"][0]["status"] == "timeout"
    home = Path(record["runtime_dir"]) / "home"
    assert_dead([home / "childpid", home / "grandpid"])


def test_shared_test_deadline_is_not_reset_per_command(b):
    (b["project"] / "tests/first_test.py").write_text(
        "import time\n\n\ndef test_slow():\n    time.sleep(2.5)\n")
    (b["project"] / "tests/second_test.py").write_text(
        "import time\n\n\ndef test_slower():\n    time.sleep(5)\n")
    change_task(b, test_timeout_seconds=4, test_commands=[
        [PY, "-m", "pytest", "tests/first_test.py", "-q"],
        [PY, "-m", "pytest", "tests/second_test.py", "-q"],
    ])
    data, code = invoke(b, "run", b["task"], timeout=20)
    assert data["status"] == "TIMEOUT" and code != 0
    record = evidence(b, data["run_id"])
    assert record["status"] == "timeout"
    assert [command["status"] for command in record["commands"]] == ["passed", "timeout"]


def test_cancel_during_tests_kills_grandchildren_and_keeps_running_ledger(b):
    set_project_test(b, HANG)
    change_task(b, test_timeout_seconds=30)
    env = {**os.environ, "CODING_CONTROL_SETTINGS": str(b["settings"])}
    runner = subprocess.Popen([PY, str(CONTROL), "run", str(b["task"])],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        runs = b["tmp"] / "state/runs"
        wait_for(runs)
        rid = next(runs.iterdir()).name
        home = run_dir(b, rid) / "env/home"
        wait_for(home / "grandpid")
        assert ledger(b, rid)["status"] == "RUNNING"
        locked, lock_code = invoke(b, "run", b["task"])
        assert locked["status"] == "BLOCKED" and lock_code != 0
        assert "project lock held" in json.dumps(locked)
        cancelled, _ = invoke(b, "cancel", rid, timeout=10)
        assert cancelled["status"] in ("CANCELLED", "CANCEL_REQUESTED")
        stdout, stderr = runner.communicate(timeout=10)
        final = json.loads(stdout)
        assert final["status"] == "CANCELLED", (final, stderr)
        assert evidence(b, rid)["status"] == "cancelled"
        assert_dead([home / "childpid", home / "grandpid"])
    finally:
        if runner.poll() is None:
            runner.terminate()
            runner.communicate(timeout=5)
        info = ledger(b, rid) if (runs / rid / "ledger.json").exists() else {}
        if info.get("child_pgid"):
            try:
                os.killpg(info["child_pgid"], signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_test_environment_is_whitelisted_and_task_test_env_is_literal(b):
    set_project_test(b, ENV_PROBE)
    change_task(b, test_env={"TEST_CASE_MODE": "synthetic"})
    sentinels = {
        "OPENAI_API_KEY": "sk-B2-SENTINEL", "VENDOR_API_KEY": "vendor-B2-SENTINEL",
        "DATABASE_URL": "postgres://B2-SENTINEL", "PGPASSWORD": "pg-B2-SENTINEL",
        "AWS_SECRET_ACCESS_KEY": "aws-B2-SENTINEL", "PYTHONPATH": "/B2/SENTINEL",
        "PYTEST_ADDOPTS": "-x B2-SENTINEL", "LD_PRELOAD": "",
        "DYLD_INSERT_LIBRARIES": "",
        "OPENCODE_CONFIG": "/B2/SENTINEL.json", "OPENCODE_CONFIG_CONTENT": "{}",
        "OPENCODE_CONFIG_DIR": "/B2/SENTINEL", "HTTP_PROXY": "http://B2-SENTINEL",
    }
    data, code = invoke(b, "run", b["task"], extra_env=sentinels)
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
    record = evidence(b, data["run_id"])
    probe = json.loads((Path(record["runtime_dir"]) / "home/env.json").read_text())
    for key in FORBIDDEN_KEYS:
        assert key not in probe["keys"], key
    assert probe["mode"] == "synthetic"
    assert probe["addopts"] == "-p no:cacheprovider"
    assert probe["dotenv"] == "1" and probe["bytecode"] == "1"
    assert probe["home"] == str(Path(record["runtime_dir"]) / "home")
    assert probe["tmpdir"] == str(Path(record["runtime_dir"]) / "tmp")
    assert probe["xdg"] == str(Path(record["runtime_dir"]) / "xdg/cache")
    text = (run_dir(b, data["run_id"]) / "test_evidence.json").read_text()
    assert "B2-SENTINEL" not in text
    executor = json.loads((b["tmp"] / "record.json").read_text())
    present = executor["env_present"]
    for key in ("OPENAI_API_KEY", "DATABASE_URL", "PGPASSWORD", "PYTHONPATH",
                "PYTEST_ADDOPTS", "CODING_CONTROL_SETTINGS", "OPENCODE_CONFIG",
                "OPENCODE_CONFIG_DIR"):
        assert present[key] is False, key
    assert present["HOME"] is True and present["OPENCODE_CONFIG_CONTENT"] is True
    assert executor["home"] == os.environ.get("HOME")
    assert json.loads(executor["config"])["permission"]["edit"]["src/main.py"] == "allow"


@pytest.mark.parametrize("test_env", [
    {"OPENAI_API_KEY": "x"}, {"SERVICE_KEY": "synthetic-service-key"},
    {"ENCRYPTION_KEY": "synthetic-encryption-key"},
    {"DATABASE_URL": "x"}, {"PGPASSWORD": "x"},
    {"PGHOST": "x"}, {"DB_HOST": "x"}, {"MONGO_URI": "x"},
    {"HOME": "/tmp"}, {"PATH": "/tmp"}, {"TMPDIR": "/tmp"}, {"PYTHONPATH": "/tmp"},
    {"PYTEST_ADDOPTS": "-x"}, {"LD_PRELOAD": "x"}, {"DYLD_INSERT_LIBRARIES": "x"},
    {"PYTHON_DOTENV_DISABLED": "0"}, {"CODING_CONTROL_SETTINGS": "x"},
    {"OPENCODE_CONFIG_CONTENT": "{}"}, {"BAD-NAME": "x"},
    {"TEST_CASE_MODE": 1}, {"TEST_CASE_MODE": "a\nb"}, [],
])
def test_invalid_test_env_blocks_before_cli(b, test_env):
    change_task(b, test_env=test_env)
    for op in ("preflight", "run"):
        data, code = invoke(b, op, b["task"])
        assert data["status"] == "BLOCKED" and code != 0
    assert not (b["tmp"] / "record.json").exists()


@pytest.mark.parametrize("value", [0, -1, True, "5", 1.5])
def test_invalid_test_timeout_blocks_before_cli(b, value):
    change_task(b, test_timeout_seconds=value)
    data, code = invoke(b, "run", b["task"])
    assert data["status"] == "BLOCKED" and code != 0
    assert not (b["tmp"] / "record.json").exists()


@pytest.mark.smoke
def test_failed_executor_does_not_run_tests_and_records_reason(b):
    set_mode(b, "error")
    data, code = invoke(b, "run", b["task"])
    assert data["status"] == "FAILED" and code != 0
    record = evidence(b, data["run_id"])
    assert record["status"] == "not_run"
    assert record["not_run_reason"] and "not run" in record["not_run_reason"]
    assert record["commands"] == []
    assert record["candidate_after"] is None and record["candidate_changed"] is None
    assert ledger(b, data["run_id"])["tests"]["status"] == "not_run"


def test_control_plane_write_during_real_tests_fails_run(b, monkeypatch):
    plane = b["tmp"] / "control"
    plane.mkdir()
    (plane / "runner.py").write_text("before\n")
    control = importlib.import_module("coding_control")
    monkeypatch.setattr(control, "HERE", plane)
    set_project_test(b, '''import os, pathlib


def test_writes_control_plane():
    target = pathlib.Path(os.environ["B2_CONTROL_TARGET"])
    target.write_text("written during tests")
    assert target.read_text() == "written during tests"
''')
    change_task(b, test_env={"B2_CONTROL_TARGET": str(plane / "runner.py")})
    task = json.loads(b["task"].read_text())
    settings = json.loads(b["settings"].read_text())
    result = control.run(task, settings)
    assert result["status"] == "FAILED"
    info = ledger(b, result["run_id"])
    assert info["control_plane_changes"] == ["runner.py"]
    assert "control-plane write" in (info["error"] or "")
    record = evidence(b, result["run_id"])
    assert record["status"] == "passed"
    assert Path(record["commands"][0]["log_path"]).is_file()


def test_review_still_runs_authorized_verification(b):
    set_mode(b, "review_clean")
    change_task(b, operation="review")
    data, code = invoke(b, "run", b["task"])
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
    record = evidence(b, data["run_id"])
    assert record["status"] == "passed" and record["commands"][0]["exit_code"] == 0
