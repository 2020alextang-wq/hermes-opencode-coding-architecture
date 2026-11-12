"""Offline contract tests: the only model boundary is a local executable fixture."""
import json
import importlib
import os
import signal
import sqlite3
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


def invoke(e, op, value, timeout=8):
    r = subprocess.run([PY, str(CONTROL), op, str(value)], text=True,
                       capture_output=True, timeout=timeout,
                       env={**os.environ, "CODING_CONTROL_SETTINGS": str(e["settings"])})
    assert r.stdout.strip(), (r.returncode, r.stderr)
    data = json.loads(r.stdout)
    if op in ("run", "preflight", "cancel"):
        assert (r.returncode == 0) == (data["status"] in ("ok", "READY_FOR_REVIEW", "CANCELLED")), (r.returncode, data, r.stderr)
    return data, r.returncode


def message(mid, model="test-model", tokens=TOKENS, completed=1):
    return {"info": {"id": mid, "role": "assistant", "providerID": "test-provider",
                     "modelID": model, "tokens": tokens, "time": {"completed": completed}}, "parts": []}


FAKE = '''#!/usr/bin/env python3
import json, os, pathlib, sys, time
root = pathlib.Path(__file__).parent
mode = (root / "mode").read_text().strip()
record = {"cwd": os.getcwd(), "args": sys.argv[1:],
          "config": os.environ.get("OPENCODE_CONFIG_CONTENT"), "prompt": sys.argv[-1],
          "env_present": {name: name in os.environ for name in (
              "HOME", "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENAI_API_KEY",
              "DATABASE_URL", "PGPASSWORD", "PYTHONPATH", "PYTEST_ADDOPTS",
              "CODING_CONTROL_SETTINGS")}}
def load(name):
    path = root / name
    return json.loads(path.read_text()) if path.exists() else None
def state_path(name):
    folder = pathlib.Path(".fake_state")
    folder.mkdir(exist_ok=True)
    ignore = folder / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\\n")
    return folder / name
def load_messages():
    path = state_path("messages.json")
    return json.loads(path.read_text()) if path.exists() else []
def candidate_text(status="READY_FOR_REVIEW", operation=None, changed=None):
    if operation is None:
        operation = (root / "operation").read_text().strip() if (root / "operation").exists() else "implement"
    if changed is None:
        changed = load("changed_files.json")
    if changed is None:
        changed = ["src/main.py"]
    unverified = load("unverified.json")
    if unverified is None:
        unverified = ["tests"]
    data = {"operation": operation, "status": status,
            "changed_files": changed, "unverified": unverified}
    return "CODING_CONTROL_CANDIDATE " + json.dumps(data, separators=(",", ":"))
def bump():
    counter = state_path("counter")
    n = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(n))
    pathlib.Path("src/main.py").write_text("value = " + str(n) + "\\n")
if sys.argv[1] == "export":
    if mode == "export_fail": sys.exit(2)
    data = load_messages()
    if mode == "duplicate" and data: data.append(data[-1])
    print(json.dumps({"info": {"id": "ses_test"}, "messages": data}))
    sys.exit(0)
if sys.argv[1:] == ["--version"]:
    version = root / "version.txt"
    text = version.read_text() if version.exists() else "1.2.3\\n"
    if text.strip():
        print(text.strip())
    sys.exit(0)
if sys.argv[1:] == ["run", "--help"]:
    help_file = root / "help.txt"
    print(help_file.read_text() if help_file.exists() else "--dir\\n--agent\\n--format")
    sys.exit(0)
if mode == "slow":
    time.sleep(2)
if mode == "spawn_child":
    record["pid"] = os.getpid()
(root / "record.json").write_text(json.dumps(record))
if mode in ("tracked", "untracked", "dirty"):
    target = "src/other.py" if mode != "untracked" else "src/new.py"
    pathlib.Path(target).write_text("changed during run\\n")
if mode == "symlink_change":
    pathlib.Path("src/link.py").unlink()
    pathlib.Path("src/link.py").symlink_to("other.py")
if mode == "control_write":
    (root / "control" / "unexpected.py").write_text("outside project\\n")
if mode == "outside_write":
    watch_target = pathlib.Path((root / "watch_target").read_text().strip())
    watch_target.mkdir(parents=True, exist_ok=True)
    if (root / "outside_delete_marker").exists():
        (watch_target / "touched.txt").unlink()
    else:
        (watch_target / "touched.txt").write_text("external write\\n")
if mode == "content_secret":
    pathlib.Path("src/main.py").write_text("token = " + chr(34) + "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0sig+1" + chr(34) + chr(10))
if mode == "content_secret_other":
    pathlib.Path("src/other.py").write_text("token = " + chr(34) + "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0sig+1" + chr(34) + chr(10))
if mode == "content_downgrade":
    p = pathlib.Path("src/main.py")
    p.write_text(p.read_text() + "x = 1  # no" + "qa: secret" + chr(10) + "pytest." + "skip(" + chr(34) + "x" + chr(34) + ")" + chr(10))
if mode == "content_secret_plain":
    pathlib.Path("src/main.py").write_text("token = " + chr(34) + "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0" + chr(34) + chr(10))
if mode == "content_delete_assert":
    pathlib.Path("src/main.py").write_text("x = 1" + chr(10))
if mode == "content_binary":
    pathlib.Path("src/blob.bin").write_bytes(bytes([255, 254]) + b"A" * 20)
if mode == "delete_allowed":
    pathlib.Path("src/main.py").unlink()
if mode == "delete_other":
    pathlib.Path("src/other.py").unlink()
if mode == "delete_test":
    pathlib.Path("tests/test_control.py").unlink()
if mode == "escape_write":
    target_file = root / "escape_target"
    targets = [pathlib.Path("/tmp/coding-control-escape-" + str(os.getpid()))]
    if target_file.exists():
        targets.insert(0, pathlib.Path(target_file.read_text().strip()))
    else:
        targets.insert(0, root / "outside-project.txt")
    record["escape"] = {"targets": [str(p) for p in targets], "wrote": [], "errors": {}}
    for target in targets:
        try:
            target.write_text("escape\\n")
            record["escape"]["wrote"].append(str(target))
        except OSError as exc:
            record["escape"]["errors"][str(target)] = type(exc).__name__
    (root / "record.json").write_text(json.dumps(record))
if mode in ("normal", "outside_write", "slow", "duplicate", "content_clean", "candidate_blocked", "candidate_bad_json",
            "candidate_body_ready", "candidate_embedded", "candidate_fenced",
            "candidate_fenced_unclosed", "candidate_trailing", "candidate_midstream",
            "candidate_two", "candidate_duplicate_line", "candidate_missing",
            "candidate_tilde_fenced_unclosed", "candidate_tilde_fenced_closed_then_valid",
            "candidate_fence_length_mismatch", "candidate_fence_info_close",
            "review_write", "error_with_candidate", "exit_nonzero"):
    bump()
if mode == "escape_write":
    bump()
if mode in ("sleep", "grandchild", "silent", "leader_exits", "spawn_child"):
    if mode == "spawn_child":
        pid = os.fork()
        if pid == 0:
            time.sleep(4)
            (root / "child_marker").write_text("done")
            os._exit(0)
        print(json.dumps({"type": "step_start", "sessionID": "ses_test", "part": {"id": "s1"}}), flush=True)
        end = time.monotonic() + 25
        while time.monotonic() < end:
            time.sleep(1)
        sys.exit(0)
    if mode in ("grandchild", "leader_exits"):
        pid = os.fork()
        if pid == 0:
            grand = os.fork()
            if grand == 0:
                (root / "grandpid").write_text(str(os.getpid()))
                while True: time.sleep(1)
            (root / "childpid").write_text(str(os.getpid()))
            while True: time.sleep(1)
    if mode == "leader_exits": sys.exit(0)
    if mode != "silent":
        print(json.dumps({"type": "step_start", "sessionID": "ses_test", "part": {"id": "s1"}}), flush=True)
    while True: time.sleep(1)
if mode == "denials":
    for i in range(2):
        print(json.dumps({"type": "tool_use", "sessionID": "ses_test", "part":
            {"id": "deny" + str(i), "tool": "bash", "state": {"status": "error", "error": "PermissionDenied: rule which prevents"}}}), flush=True)
    while True: time.sleep(1)
if mode in ("error", "error_with_candidate"):
    print(json.dumps({"type": "error", "sessionID": "ses_test", "error": "model failed"}), flush=True)
elif mode == "tool_error":
    print(json.dumps({"type": "tool_use", "sessionID": "ses_test", "part": {"id": "e1", "tool": "bash", "state": {"status": "error", "error": "test failed"}}}), flush=True)
else:
    tokens = {"total": 190, "input": 100, "output": 50, "reasoning": 20, "cache": {"read": 30, "write": 10}} if mode == "budget" else {"total":19,"input":10,"output":5,"reasoning":2,"cache":{"read":3,"write":1}}
    event = {"type": "step_finish", "sessionID": "ses_test", "part": {"id": "step1", "tokens": tokens}}
    print(json.dumps(event), flush=True)
    if mode == "budget":
        print(json.dumps(event), flush=True)
        while True: time.sleep(1)
if mode != "export_fail":
    messages = load_messages()
    if mode != "no_new":
        if mode == "outside_write":
            texts = [candidate_text()]
        elif mode == "candidate_midstream":
            texts = [candidate_text(), "still working"]
        elif mode == "candidate_two":
            texts = [candidate_text(), candidate_text()]
        elif mode == "escape_write":
            texts = [candidate_text()]
        elif mode in ("normal", "duplicate", "content_clean", "error_with_candidate", "exit_nonzero"):
            texts = [candidate_text()]
        elif mode == "review_clean":
            texts = [candidate_text(operation="review", changed=[])]
        elif mode == "review_write":
            texts = [candidate_text(operation="review", changed=[])]
        elif mode == "candidate_blocked":
            texts = [candidate_text(status="BLOCKED")]
        elif mode == "candidate_bad_json":
            texts = ["CODING_CONTROL_CANDIDATE {not json"]
        elif mode == "candidate_body_ready":
            texts = ["READY_FOR_REVIEW"]
        elif mode == "candidate_embedded":
            texts = ["note: " + candidate_text()]
        elif mode == "candidate_fenced":
            texts = ["```json\\n" + candidate_text() + "\\n```"]
        elif mode == "candidate_fenced_unclosed":
            texts = ["```json\\n" + candidate_text()]
        elif mode == "candidate_trailing":
            texts = [candidate_text() + "\\nPLAN_READY"]
        elif mode == "candidate_duplicate_line":
            texts = [candidate_text() + "\\n" + candidate_text()]
        elif mode == "candidate_tilde_fenced_unclosed":
            texts = ["~~~json\\n" + candidate_text()]
        elif mode == "candidate_tilde_fenced_closed_then_valid":
            texts = ["~~~json\\n" + candidate_text() + "\\n~~~\\n" + candidate_text()]
        elif mode == "candidate_fence_length_mismatch":
            texts = ["````\\ninside fence\\n```\\n" + candidate_text()]
        elif mode == "candidate_fence_info_close":
            texts = ["```\\ninside fence\\n```python\\n" + candidate_text()]
        elif mode == "delete_allowed":
            texts = [candidate_text(changed=["src/main.py"])]
        elif mode == "delete_other":
            texts = [candidate_text(changed=["src/main.py"])]
        elif mode == "delete_test":
            texts = [candidate_text(changed=["tests/test_control.py"])]
        elif mode == "candidate_no_change":
            texts = [candidate_text(changed=[])]
        elif mode == "silent_exit0":
            texts = [None]
        elif mode == "slow":
            texts = [candidate_text()]
        else:
            texts = [None]
        for text in texts:
            mid = "m" + str(len(messages) + 1)
            parts = [{"type": "text", "text": text}] if text is not None else []
            messages.append({"info": {"id": mid, "role": "assistant", "providerID": "test-provider",
                "modelID": "wrong" if mode == "mismatch" else "test-model",
                "tokens": {"total":19,"input":10,"output":5,"reasoning":2,"cache":{"read":3,"write":1}},
                "time": {"completed": 1}}, "parts": parts})
        state_path("messages.json").write_text(json.dumps(messages))
        if mode == "unfinished":
            messages[-1]["info"]["time"] = {}
            state_path("messages.json").write_text(json.dumps(messages))
if mode == "exit_nonzero":
    sys.exit(3)
'''


@pytest.fixture
def e(tmp_path):
    root = tmp_path / "workspace"
    project = root / "project"
    (project / "src").mkdir(parents=True)
    (project / "src/main.py").write_text("original\n")
    (project / "src/other.py").write_text("original\n")
    (project / "tests").mkdir()
    (project / "tests/test_control.py").write_text(
        "def test_project_fixture():\n    assert 1 + 1 == 2\n")
    (project / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n*.pyc\n.fake_state/\n")
    for cmd in (["git", "init"],
                ["git", "add", "src/main.py", "src/other.py",
                 "tests/test_control.py", ".gitignore"]):
        subprocess.run(cmd, cwd=project, check=True, capture_output=True, timeout=3)
    cli = tmp_path / "fake_cli.py"
    cli.write_text(FAKE)
    cli.chmod(0o755)
    (tmp_path / "mode").write_text("normal")
    (tmp_path / "messages.json").write_text("[]")
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"workspace_root": str(root), "cli": str(cli),
                                    "runner_model": "test-provider/test-model",
                                    "state_dir": str(tmp_path / "state"), "fake_root": str(tmp_path)}))
    task = tmp_path / "task.json"
    task.write_text(json.dumps({"project": str(project), "goal": "Update src/main.py",
                                "allowed_files": ["src/main.py"],
                                "test_commands": [[PY, "-m", "pytest", "tests/test_control.py", "-q"]],
                                "timeout_seconds": 2, "max_tokens": 100, "max_permission_denials": 2}))
    return {"root": root, "project": project, "tmp": tmp_path, "settings": settings, "task": task}


def change(e, **fields):
    task = json.loads(e["task"].read_text())
    task.update(fields)
    e["task"].write_text(json.dumps(task))


def mode(e, name):
    (e["tmp"] / "mode").write_text(name)


def ledger(e, rid):
    return json.loads((e["tmp"] / "state/runs" / rid / "ledger.json").read_text())


@pytest.mark.parametrize("scope", [".", "src", "src/*.py", "src/../../oops", "/tmp/a",
                                         "src/.env.local", "src/.ENV.local", ".git/config", "keys/api_key.txt"])
def test_scope_rejected_in_preflight_and_run(e, scope):
    change(e, allowed_files=[scope])
    for op in ("preflight", "run"):
        data, code = invoke(e, op, e["task"])
        assert data["status"] == "BLOCKED" and code != 0
    assert not (e["tmp"] / "record.json").exists()


@pytest.mark.parametrize("kind", ["file", "parent", "missing_child"])
def test_symlink_outside_project_rejected(e, kind):
    outside = e["root"] / "sibling"
    outside.mkdir()
    if kind == "file":
        (outside / "target.py").write_text("x")
        (e["project"] / "link.py").symlink_to(outside / "target.py")
        scope = "link.py"
    else:
        (e["project"] / "link").symlink_to(outside, target_is_directory=True)
        scope = "link/target.py" if kind == "parent" else "link/not_yet/sub.py"
    change(e, allowed_files=[scope])
    assert invoke(e, "run", e["task"])[0]["status"] == "BLOCKED"


@pytest.mark.parametrize("fields", [{"goal": ""}, {"test_commands": []},
                                         {"max_tokens": True}, {"timeout_seconds": 0},
                                         {"model": "invalid"}, {"test_commands": [[PY, "-c", "print(1); rm -rf x"]]},
                                         {"test_commands": [[PY, "-m", "pytest", "x;touch bad"]]},
                                         {"test_commands": [[PY, "-m", "pytest", "tests/*.py"]]},
                                         {"session_id": "ses_test"}, {"parent_run": "../bad"}])
def test_invalid_tasks_block_before_cli(e, fields):
    change(e, **fields)
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "BLOCKED" and code != 0
    assert not (e["tmp"] / "record.json").exists()


def test_task_settings_key_is_ignored_environment_settings_win(e):
    alternate = e["tmp"] / "alternate-settings.json"
    alternate.write_text(json.dumps({"runner": {"kind": "dsh"}}))
    change(e, settings=str(alternate))
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
    assert ledger(e, data["run_id"])["runner_config"]["kind"] == "opencode"


@pytest.mark.smoke
def test_preflight_is_pure_no_model(e):
    data, code = invoke(e, "preflight", e["task"])
    assert code == 0 and data["status"] == "ok"
    assert data["files"]["src/main.py"]
    assert not (e["tmp"] / "record.json").exists()


def test_run_passes_real_prompt_cwd_and_permission_config(e):
    result, code = invoke(e, "run", e["task"])
    assert result["status"] == "READY_FOR_REVIEW" and code == 0
    rec = json.loads((e["tmp"] / "record.json").read_text())
    assert rec["cwd"] == str(e["project"])
    assert rec["args"][:7] == ["run", "--pure", "--agent", "build", "--model", "test-provider/test-model", "--format"]
    assert rec["args"][rec["args"].index("--dir") + 1] == str(e["project"].resolve())
    assert "Update src/main.py" in rec["prompt"] and "src/main.py" in rec["prompt"]
    p = json.loads(rec["config"])["permission"]
    assert p["edit"]["*"] == "deny" and p["edit"]["src/main.py"] == "allow"
    assert p["external_directory"]["*"] == "deny"
    assert p["external_directory"][str(e["root"].resolve())] == "deny"
    assert p["bash"]["*"] == "deny"
    assert p["task"] == "deny" and p["webfetch"] == "deny"


def test_usage_real_schema_deduplicates_and_preserves_reasoning_subset(e):
    result, _ = invoke(e, "run", e["task"])
    u = invoke(e, "usage", result["run_id"])[0]
    assert u["usage"] == {"input": 10, "output": 5, "cache_read": 3, "cache_write": 1,
                          "reasoning": 2, "total": 19, "message_count": 1}
    assert u["cost"] is None and u["hermes"]["status"] == "pending_after_turn/not_attributable"


def test_parent_run_counts_only_new_messages_and_chain_total(e):
    first, _ = invoke(e, "run", e["task"])
    change(e, parent_run=first["run_id"], session_id="ses_test")
    second, _ = invoke(e, "run", e["task"])
    assert second["status"] == "READY_FOR_REVIEW"
    u = invoke(e, "usage", second["run_id"])[0]
    assert u["usage"]["total"] == 19 and u["usage"]["message_count"] == 1
    assert u["chain_usage"]["total"] == 38 and u["chain_usage"]["message_count"] == 2


def test_parent_model_or_project_mismatch_blocks(e):
    first, _ = invoke(e, "run", e["task"])
    change(e, parent_run=first["run_id"], session_id="ses_test", model="other/model")
    assert invoke(e, "run", e["task"])[0]["status"] == "BLOCKED"


@pytest.mark.parametrize("bad", ["../bad", "nothex", "a" * 13])
@pytest.mark.parametrize("op", ["status", "usage", "cancel"])
def test_runid_rejected(e, op, bad):
    assert invoke(e, op, bad)[0]["status"] == "BLOCKED"


def test_silent_process_times_out(e):
    mode(e, "silent")
    change(e, timeout_seconds=1)
    data, code = invoke(e, "run", e["task"], timeout=5)
    assert data["status"] == "TIMEOUT" and code != 0


def live(pid):
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], text=True,
                       capture_output=True, timeout=2)
    return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")


def wait_for(path, timeout=3):
    until = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < until:
        time.sleep(.02)
    assert path.exists(), path


def test_cancel_kills_grandchild_even_when_leader_exited(e):
    mode(e, "leader_exits")
    change(e, timeout_seconds=4)
    runner = subprocess.Popen([PY, str(CONTROL), "run", str(e["task"])],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              env={**os.environ, "CODING_CONTROL_SETTINGS": str(e["settings"])})
    try:
        wait_for(e["tmp"] / "grandpid")
        run_dirs = list((e["tmp"] / "state/runs").iterdir())
        rid = run_dirs[0].name
        cancelled, _ = invoke(e, "cancel", rid, timeout=6)
        assert cancelled["status"] == "CANCELLED"
        runner.communicate(timeout=3)
        for key in ("childpid", "grandpid"):
            pid = int((e["tmp"] / key).read_text())
            assert not live(pid), (key, pid)
    finally:
        if runner.poll() is None:
            runner.terminate()
            runner.communicate(timeout=3)
        state = e["tmp"] / "state/runs"
        if state.exists():
            for d in state.iterdir():
                info = ledger(e, d.name)
                if info.get("child_pgid"):
                    try: os.killpg(info["child_pgid"], signal.SIGKILL)
                    except ProcessLookupError: pass


def test_flock_conflict_does_not_start_second_cli(e):
    mode(e, "sleep")
    runner = subprocess.Popen([PY, str(CONTROL), "run", str(e["task"])],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              env={**os.environ, "CODING_CONTROL_SETTINGS": str(e["settings"])})
    try:
        wait_for(e["tmp"] / "record.json")
        data, code = invoke(e, "run", e["task"])
        assert data["status"] == "BLOCKED" and code != 0
        rid = next((e["tmp"] / "state/runs").iterdir()).name
        assert invoke(e, "cancel", rid)[0]["status"] == "CANCELLED"
    finally:
        if runner.poll() is None:
            runner.terminate()
        runner.communicate(timeout=4)


def test_stale_pid_denied_without_signal(e):
    first, _ = invoke(e, "run", e["task"])
    info = ledger(e, first["run_id"])
    info.update(status="RUNNING", supervisor_pid=os.getpid(), supervisor_started="wrong")
    (e["tmp"] / "state/runs" / first["run_id"] / "ledger.json").write_text(json.dumps(info))
    data, code = invoke(e, "cancel", first["run_id"])
    assert data["status"] == "BLOCKED" and code != 0


@pytest.mark.parametrize("name,status", [("denials", "BLOCKED"), ("budget", "BUDGET_STOP"),
                                          ("mismatch", "FAILED"), ("export_fail", "FAILED"),
                                          ("error", "FAILED"), ("tool_error", "FAILED")])
def test_event_or_export_failure_never_ready(e, name, status):
    mode(e, name)
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == status and code != 0
    if name == "export_fail":
        u = invoke(e, "usage", data["run_id"])[0]
        assert u["usage"] is None and u["usage_incomplete"] is True
    if name == "budget":
        assert ledger(e, data["run_id"])["token_total"] == 190


@pytest.mark.parametrize("name,expected", [("tracked", ["src/other.py"]),
                                             ("untracked", ["src/new.py"])])
def test_run_detects_new_outside_scope_deltas(e, name, expected):
    mode(e, name)
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "FAILED" and code != 0
    assert ledger(e, data["run_id"])["out_of_scope_changes"] == expected


def test_existing_dirty_file_is_preserved_not_new_violation(e):
    (e["project"] / "src/other.py").write_text("existing user work\n")
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
    assert (e["project"] / "src/other.py").read_text() == "existing user work\n"


def test_existing_untracked_file_mutated_is_detected(e):
    (e["project"] / "src/new.py").write_text("old untracked\n")
    mode(e, "untracked")
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "FAILED" and code != 0
    assert "src/new.py" in ledger(e, data["run_id"])["out_of_scope_changes"]


def test_tracked_symlink_target_change_is_detected(e):
    (e["project"] / "src/link.py").symlink_to("main.py")
    subprocess.run(["git", "add", "src/link.py"], cwd=e["project"], check=True, timeout=3)
    mode(e, "symlink_change")
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "FAILED" and code != 0
    assert "src/link.py" in ledger(e, data["run_id"])["out_of_scope_changes"]


def test_no_new_assistant_message_is_unknown(e):
    mode(e, "no_new")
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "FAILED" and code != 0
    assert invoke(e, "usage", data["run_id"])[0]["usage"] is None


def test_duplicate_export_message_id_is_counted_once(e):
    mode(e, "duplicate")
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
    assert invoke(e, "usage", data["run_id"])[0]["usage"]["total"] == 19


def test_unfinished_assistant_usage_is_unknown(e):
    mode(e, "unfinished")
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "FAILED" and code != 0
    u = invoke(e, "usage", data["run_id"])[0]
    assert u["usage"] is None and u["usage_incomplete"] is True


def test_project_without_git_fails_closed(e):
    sibling = e["root"] / "no-git"
    sibling.mkdir()
    (sibling / "ok.py").write_text("ok")
    change(e, project=str(sibling), allowed_files=["ok.py"])
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "BLOCKED" and code != 0


def test_model_missing_from_task_and_settings_blocks(e):
    settings = json.loads(e["settings"].read_text())
    settings["runner_model"] = ""
    e["settings"].write_text(json.dumps(settings))
    assert invoke(e, "preflight", e["task"])[0]["status"] == "BLOCKED"


def test_hermes_sqlite_snapshot_is_reference_not_exact_delta(e):
    db = e["tmp"] / "hermes.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT, input_tokens INTEGER, output_tokens INTEGER)")
        conn.execute("INSERT INTO sessions VALUES ('telegram-turn', 13, 7)")
    settings = json.loads(e["settings"].read_text())
    settings.update(hermes_db=str(db), hermes_session_id="telegram-turn")
    e["settings"].write_text(json.dumps(settings))
    run, code = invoke(e, "run", e["task"])
    assert code == 0
    result = invoke(e, "usage", run["run_id"])[0]["hermes"]
    assert result["snapshot"] == {"input_tokens": 13, "output_tokens": 7}
    assert result["status"] == "pending_after_turn/not_attributable"


def test_run_evidence_is_private_to_current_user(e):
    data, code = invoke(e, "run", e["task"])
    assert code == 0
    folder = e["tmp"] / "state/runs" / data["run_id"]
    for path in (folder, folder / "ledger.json", folder / "run.log", folder / "session_export.json"):
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, path


def test_export_streams_stdout_to_private_file_not_pipe(e, monkeypatch):
    control = importlib.import_module("coding_control")
    original = control.subprocess.run
    calls = []

    def checked(argv, **kwargs):
        if argv[1] == "export":
            assert kwargs.get("capture_output") is not True
            assert kwargs.get("stdout") is not subprocess.PIPE
            assert kwargs["stdout"].fileno() >= 0
            assert kwargs["stderr"] is subprocess.PIPE
            assert kwargs["timeout"] == 15
            calls.append(argv)
        return original(argv, **kwargs)

    monkeypatch.setattr(control.subprocess, "run", checked)
    result = control.export(str(e["tmp"] / "fake_cli.py"), "ses_test", e["project"],
                            os.environ.copy(), e["tmp"] / "streamed.json")
    assert calls and result == {"info": {"id": "ses_test"}, "messages": []}
    assert stat.S_IMODE((e["tmp"] / "streamed.json").stat().st_mode) == 0o600


def test_run_config_has_directory_descendants_and_model_title(e):
    result, code = invoke(e, "run", e["task"])
    assert code == 0 and result["status"] == "READY_FOR_REVIEW"
    record = json.loads((e["tmp"] / "record.json").read_text())
    config = json.loads(record["config"])
    external = config["permission"]["external_directory"]
    project = str(e["project"].resolve())
    shared = str(CONTROL.parent / "skills")
    assert external[project] == external[project + "/**"] == "allow"
    assert external[shared] == external[shared + "/**"] == "allow"
    assert external[str(e["root"].resolve()) + "/**"] == "deny"
    assert config["small_model"] == "test-provider/test-model"
    assert record["args"][record["args"].index("--title") + 1] == "coding-control"


def test_run_allows_only_each_scoped_file_in_three_edit_path_forms(e):
    change(e, allowed_files=["src/main.py", "src/new.py"])
    result, code = invoke(e, "run", e["task"])
    assert code == 0 and result["status"] == "READY_FOR_REVIEW"
    record = json.loads((e["tmp"] / "record.json").read_text())
    edit = json.loads(record["config"])["permission"]["edit"]
    assert edit["*"] == "deny"
    assert "../*" not in edit
    for rel in ("src/main.py", "src/new.py"):
        assert edit[rel] == edit["./" + rel] == edit[str(e["project"] / rel)] == "allow"
    assert str(e["project"] / "src/other.py") not in edit
    assert "src/other.py" not in edit
    assert "must use absolute paths" in record["prompt"]
    assert "do not use bash to create files or directories" in record["prompt"]


def test_control_plane_fingerprint_detects_new_and_changed_files_but_ignores_state(tmp_path):
    control = importlib.import_module("coding_control")
    base = tmp_path / "control"
    base.mkdir()
    (base / "runner.py").write_text("before\n")
    before = control.control_snapshot(base)
    (base / "runner.py").write_text("after\n")
    (base / "unexpected.py").write_text("new\n")
    (base / "state").mkdir()
    (base / "state" / "ledger.json").write_text("runtime\n")
    after = control.control_snapshot(base)
    assert control.control_plane_delta(base.parent / "business", before, after, base) == ["runner.py", "unexpected.py"]
    assert control.control_plane_delta(base, before, after, base) == []


def test_run_fails_on_control_plane_write_outside_project(e, monkeypatch):
    control = importlib.import_module("coding_control")
    plane = e["tmp"] / "control"
    plane.mkdir()
    monkeypatch.setattr(control, "HERE", plane)
    mode(e, "control_write")
    task = json.loads(e["task"].read_text())
    settings = json.loads(e["settings"].read_text())
    result = control.run(task, settings)
    assert result["status"] == "FAILED"
    info = ledger(e, result["run_id"])
    assert info["control_plane_changes"] == ["unexpected.py"]
    assert "control-plane write" in info["error"]


def test_namespaced_model_and_normal_auth_source_accepted(e):
    (e["project"] / "src/auth_token.py").write_text("# normal code\n")
    change(e, model="example-provider/example-model", allowed_files=["src/auth_token.py"])
    checked, code = invoke(e, "preflight", e["task"])
    assert checked["status"] == "ok" and code == 0
    assert checked["model"] == "example-provider/example-model"


@pytest.mark.parametrize("name", [".env.prod", "src/id_rsa", "src/id_ed25519", "src/cert.pem",
                                       "config/credentials.json", "config/api_keys.json"])
def test_actual_credential_file_is_denied(e, name):
    change(e, allowed_files=[name])
    assert invoke(e, "preflight", e["task"])[0]["status"] == "BLOCKED"


def test_namespaced_model_rejects_control_character(e):
    change(e, model="example-provider/example-model\n--agent evil")
    assert invoke(e, "preflight", e["task"])[0]["status"] == "BLOCKED"


def test_runner_fixes_latest_hermes_task_at_run_start(e, monkeypatch):
    from hermes_usage import on_post_api_request

    db = e["tmp"] / "plugin.sqlite"
    monkeypatch.setenv("CODING_HERMES_USAGE_DB", str(db))
    settings = json.loads(e["settings"].read_text())
    settings["hermes_session_id"] = "telegram-session"
    e["settings"].write_text(json.dumps(settings))
    metric = {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 3,
              "cache_write_tokens": 1, "reasoning_tokens": 2}
    on_post_api_request(task_id="task-one", session_id="telegram-session", api_call_count=1,
                        provider="test-provider", model="test-model", response={"id": "r1"}, usage=metric)
    run, code = invoke(e, "run", e["task"])
    assert code == 0 and ledger(e, run["run_id"])["hermes_task_id"] == "task-one"
    on_post_api_request(task_id="task-two", session_id="telegram-session", api_call_count=1,
                        provider="test-provider", model="test-model", response={"id": "r2"}, usage=metric)
    on_post_api_request(task_id="task-one", session_id="telegram-session", api_call_count=2,
                        provider="test-provider", model="test-model", response={"id": "r3"}, usage=metric)
    result = invoke(e, "usage", run["run_id"])[0]
    assert result["hermes_task"]["task_id"] == "task-one"
    assert result["hermes_task"]["session_id"] == "telegram-session"
    assert result["hermes_usage"]["usage"]["total"] == 38
    assert result["hermes_usage"]["status"] == "active_task_may_grow"


def prior_ledger(e, n, rework, files=None):
    rid = f"{n:012x}"
    folder = e["tmp"] / "state/runs" / rid
    folder.mkdir(parents=True)
    (folder / "ledger.json").write_text(json.dumps({
        "run_id": rid, "status": "FAILED", "project": str(e["project"]),
        "allowed_files": files or ["src/main.py"], "rework_count": rework}))


def test_content_scan_fake_jwt_fails_run(e):
    mode(e, "content_secret")
    data, code = invoke(e, "run", e["task"])
    info = ledger(e, data["run_id"])
    assert data["status"] == "FAILED" and code != 0
    assert "content-scan" in (info.get("error") or "")
    assert "eyJ" not in (info.get("error") or "")


def test_content_scan_downgrade_marker_fails_run(e):
    mode(e, "content_downgrade")
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "FAILED" and code != 0
    assert "content-scan" in (ledger(e, data["run_id"]).get("error") or "")


def test_content_scan_preexisting_noqa_is_not_this_run(e):
    (e["project"] / "src/main.py").write_text("original  # no" + "qa: secret\n")
    mode(e, "content_clean")
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
    assert "content-scan" not in (ledger(e, data["run_id"]).get("error") or "")


def test_content_scan_out_of_scope_secret_still_fails(e):
    mode(e, "content_secret_other")
    data, code = invoke(e, "run", e["task"])
    info = ledger(e, data["run_id"])
    assert data["status"] == "FAILED" and code != 0
    assert "content-scan" in (info.get("error") or "")
    assert "src/other.py" in (info.get("error") or "")
    assert "eyJ" not in (info.get("error") or "")


def test_content_scan_alphanumeric_jwt_fails_run(e):
    mode(e, "content_secret_plain")
    data, code = invoke(e, "run", e["task"])
    info = ledger(e, data["run_id"])
    assert data["status"] == "FAILED" and code != 0
    assert "content-scan" in (info.get("error") or "")
    assert "eyJ" not in (info.get("error") or "")


def test_content_scan_deleted_assertion_fails_run(e):
    (e["project"] / "src/main.py").write_text("x = 1\nassert value == 1\n")
    subprocess.run(["git", "add", "src/main.py"], cwd=e["project"], check=True, timeout=3)
    mode(e, "content_delete_assert")
    data, code = invoke(e, "run", e["task"])
    info = ledger(e, data["run_id"])
    assert data["status"] == "FAILED" and code != 0
    assert "content-scan" in (info.get("error") or "")
    assert "removed-assertion" in (info.get("error") or "")


def test_content_scan_binary_change_is_not_snapshot_failure(e):
    mode(e, "content_binary")
    data, code = invoke(e, "run", e["task"])
    info = ledger(e, data["run_id"])
    assert data["status"] == "FAILED" and code != 0
    assert "content-scan" in (info.get("error") or "")
    assert "snapshot failed" not in (info.get("error") or "")


def test_rework_count_follows_project_not_file_list(e):
    prior_ledger(e, 1, 3, files=["src/other.py"])
    data, code = invoke(e, "preflight", e["task"])
    assert data["status"] == "BLOCKED" and code != 0
    assert not (e["tmp"] / "record.json").exists()


def test_rework_count_blocks_a_fourth_dispatch(e):
    prior_ledger(e, 1, 3)
    data, code = invoke(e, "preflight", e["task"])
    assert data["status"] == "BLOCKED" and code != 0
    assert "rework_count" in json.dumps(data)
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "BLOCKED" and code != 0
    assert not (e["tmp"] / "record.json").exists()


def test_rework_count_inherits_previous_failed_count_for_clean_run(e):
    # v2.5 supersedes the old READY +1 behavior: READY inherits the prior count.
    prior_ledger(e, 1, 1)
    mode(e, "content_clean")
    data, code = invoke(e, "run", e["task"])
    info = ledger(e, data["run_id"])
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
    assert info["quality_rework_count"] == 1
    assert info["rework_count"] == 1
    assert info["quality_failure"] is False


def test_explicit_hermes_task_reference_overrides_latest(e, monkeypatch):
    from hermes_usage import on_post_api_request
    monkeypatch.setenv("CODING_HERMES_USAGE_DB", str(e["tmp"] / "plugin.sqlite"))
    settings = json.loads(e["settings"].read_text())
    settings["hermes_session_id"] = "telegram-session"
    e["settings"].write_text(json.dumps(settings))
    on_post_api_request(task_id="latest-turn", session_id="telegram-session", api_call_count=1,
                        provider="test", model="model", response={"id": "r1"},
                        usage={"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 3,
                               "cache_write_tokens": 1, "reasoning_tokens": 2})
    change(e, hermes_task_id="chosen-turn", hermes_session_id="telegram-session")
    run, code = invoke(e, "run", e["task"])
    assert code == 0
    result = invoke(e, "usage", run["run_id"])[0]
    assert result["hermes_task"] == {"task_id": "chosen-turn", "session_id": "telegram-session"}
    assert result["hermes_usage"]["status"] == "not_found" and result["hermes_usage"]["usage"] is None
