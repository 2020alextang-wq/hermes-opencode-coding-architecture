"""B22-T: dsh review execution path (argv, env, events, candidate, denials, cancel)."""
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from test_control import CONTROL, PY, change, e, invoke, ledger

HELP = "--profile <name>\n--json\n--permission-mode <mode>\n--approval-policy <policy>\n--session-id <id>\n-\n"
API_KEY_ENV = "DSH_TEST_API_KEY"

DSH = r'''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys, time

root = pathlib.Path(__file__).resolve().parent
args = sys.argv[1:]
record = {"args": args, "cwd": os.getcwd(), "env": dict(os.environ),
          "pid": os.getpid(), "pgid": os.getpgid(0), "sid": os.getsid(0),
          "stdin": None}

def log():
    with (root / "dsh-calls.jsonl").open("a") as stream:
        stream.write(json.dumps(record) + "\n")

if args == ["--version"]:
    log()
    path = root / "dsh-version.txt"
    print(path.read_text().strip() if path.exists() else "")
    sys.exit(0)

if "--help" in args:
    log()
    path = root / "dsh-help.txt"
    print(path.read_text() if path.exists() else "")
    sys.exit(0)

record["stdin"] = sys.stdin.read()
log()

mode_path = root / "dsh-mode.txt"
mode = mode_path.read_text().strip() if mode_path.exists() else "ready"

def emit(event):
    print(json.dumps(event), flush=True)

def candidate(status="READY_FOR_REVIEW", changed=None, operation="review"):
    body = {"operation": operation, "status": status,
            "changed_files": [] if changed is None else changed,
            "unverified": ["tests not run"]}
    return ("Review complete\n\nCODING_CONTROL_CANDIDATE " +
            json.dumps(body, separators=(",", ":")))

session_id = "dsh-session-1"
final_session = "dsh-session-1"
if mode == "session_mismatch":
    final_session = "dsh-session-other"

emit({"type": "session", "sessionId": session_id, "cwd": os.getcwd()})

if mode == "implement_drift":
    pathlib.Path("src/main.py").write_text("worktree write\n")
    (root / "dsh-implement-started").write_text("1")
    while not (root / "dsh-release").exists():
        time.sleep(.02)

if mode == "implement_hang":
    pathlib.Path("src/main.py").write_text("worktree write\n")
    marker = root / "late-marker"
    code = "import pathlib, time; time.sleep(2); pathlib.Path(%r).touch()" % str(marker)
    child = subprocess.Popen([sys.executable, "-c", code])
    (root / "dsh-child-pid").write_text(str(child.pid))
    while True:
        time.sleep(1)

if mode == "hang":
    marker = root / "late-marker"
    code = "import pathlib, time; time.sleep(2); pathlib.Path(%r).touch()" % str(marker)
    child = subprocess.Popen([sys.executable, "-c", code])
    (root / "dsh-child-pid").write_text(str(child.pid))
    while True:
        time.sleep(1)

emit({"type": "status", "phase": "turn_start", "turn": 1})
emit({"type": "status", "phase": "step_start", "turn": 1, "step": 1})
emit({"type": "thinking", "text": "checking review"})
emit({"type": "text", "text": "Reviewing files"})
if mode != "no_usage":
    emit({"type": "status", "phase": "step_end", "turn": 1, "step": 1,
          "usage": {"inputTokens": 10, "outputTokens": 5, "cacheReadTokens": 3,
                    "cacheWriteTokens": 1, "totalTokens": 19}})
    emit({"type": "status", "phase": "step_start", "turn": 1, "step": 2})
    emit({"type": "status", "phase": "step_end", "turn": 1, "step": 2,
          "usage": {"inputTokens": 20, "outputTokens": 7, "cacheReadTokens": 0,
                    "cacheWriteTokens": 0, "totalTokens": 27}})

if mode in ("implement_allowed", "implement_out_of_scope", "implement_denied"):
    pathlib.Path("src/main.py").write_text("worktree write\n")
if mode == "implement_secret":
    pathlib.Path("src/main.py").write_text(
        "token = " + chr(34) + "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0sig+1" + chr(34) + chr(10))
if mode == "implement_out_of_scope":
    pathlib.Path("src/outside.py").write_text("outside worktree write\n")
if mode in ("implement_delete_allowed", "implement_delete_unauthorized"):
    pathlib.Path("src/obsolete.py").unlink()

if mode in ("denied", "implement_denied"):
    code_path = root / "dsh-denial-code.txt"
    code = code_path.read_text().strip() if code_path.exists() else "FS_SANDBOX_DENIED"
    emit({"type": "tool_call", "callId": "call-1", "tool": "write",
          "input": {"path": "blocked.txt"}})
    emit({"type": "tool_result", "callId": "call-1", "status": "error",
          "error": {"name": "FsError", "code": code}})

emit({"type": "status", "phase": "turn_end", "turn": 1,
      "reason": {"kind": "completed"}})

if mode in ("ready", "denied", "session_mismatch", "no_usage"):
    text = candidate()
elif mode.startswith("implement_"):
    changed = []
    if mode in ("implement_allowed", "implement_out_of_scope",
                "implement_denied", "implement_drift", "implement_secret"):
        changed = ["src/main.py"]
    elif mode in ("implement_delete_allowed", "implement_delete_unauthorized"):
        changed = ["src/obsolete.py"]
    text = candidate(changed=changed, operation="implement")
elif mode == "blocked":
    text = candidate(status="BLOCKED")
elif mode == "changed_nonempty":
    text = candidate(changed=["src/main.py"])
elif mode == "invalid_status":
    text = candidate(status="ACCEPTED")
elif mode == "bad_candidate":
    text = "Review complete\n\nnot a candidate"
elif mode == "candidate_missing":
    text = "Review complete"
else:
    text = candidate()

emit({"type": "final", "sessionId": final_session,
      "reason": {"kind": "completed", "text": "done"}, "text": text})
'''


@pytest.fixture
def dsh(e, monkeypatch):
    home = e["tmp"] / "dsh-home"
    home.mkdir()
    monkeypatch.setenv(API_KEY_ENV, "test-secret-value")
    script = e["tmp"] / "fake_dsh.py"
    script.write_text(DSH)
    script.chmod(0o755)
    (e["tmp"] / "dsh-version.txt").write_text("0.2.2-rc.1\n")
    (e["tmp"] / "dsh-help.txt").write_text(HELP)
    (e["tmp"] / "dsh-mode.txt").write_text("ready")
    config = json.loads(e["settings"].read_text())
    config.pop("cli", None)
    config["runner"] = {"kind": "dsh", "binary": str(script),
                        "dsh_home": str(home), "api_key_env": API_KEY_ENV}
    e["settings"].write_text(json.dumps(config))
    change(e, operation="review")
    return {"e": e, "script": script, "home": home, "key": API_KEY_ENV}


def set_mode(dsh, name):
    (dsh["e"]["tmp"] / "dsh-mode.txt").write_text(name)


def run_calls(dsh):
    log = dsh["e"]["tmp"] / "dsh-calls.jsonl"
    if not log.exists():
        return []
    records = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    return [record for record in records if record.get("stdin") is not None]


def wait_for(path, timeout=8):
    until = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < until:
        time.sleep(.02)
    assert path.exists(), path


def test_dsh_review_ready_parses_events_candidate_usage_and_session(dsh):
    e = dsh["e"]

    data, code = invoke(e, "run", e["task"])

    assert data["status"] == "READY_FOR_REVIEW" and code == 0, data
    info = ledger(e, data["run_id"])
    assert info["dsh_session_id"] == "dsh-session-1"
    assert info.get("actual_model") is None
    assert info.get("model_verified") is False
    usage = info.get("usage") or {}
    assert usage.get("input") == 30 and usage.get("output") == 12
    assert usage.get("total") == 46
    assert all(type(usage.get(key)) is int for key in ("input", "output", "total"))
    assert info.get("usage_incomplete") is False
    assert info.get("policy_denials", []) == []
    assert (info.get("candidate") or {}).get("status") == "READY_FOR_REVIEW"
    events = [json.loads(line) for line in Path(info["log_path"]).read_text().splitlines()
              if line.strip()]
    assert any(event.get("type") == "session" for event in events)
    assert any(event.get("type") == "final" for event in events)


def test_dsh_review_argv_stdin_cwd_and_process_group(dsh):
    e = dsh["e"]

    data, code = invoke(e, "run", e["task"])

    assert data["status"] == "READY_FOR_REVIEW" and code == 0, data
    records = run_calls(dsh)
    assert len(records) == 1, records
    record = records[0]
    assert record["args"] == ["--profile", "headless", "--json", "--permission-mode",
                              "read-only", "--approval-policy", "never", "-"]
    assert record["cwd"] == str(Path(e["project"]).resolve())
    assert "Goal:" in record["stdin"] and "src/main.py" in record["stdin"]
    assert "Goal:" not in " ".join(record["args"])
    assert record["pid"] == record["pgid"] == record["sid"]


def test_dsh_review_parent_session_passes_session_id(dsh):
    e = dsh["e"]
    rid = "a" * 12
    folder = e["tmp"] / "state/runs" / rid
    folder.mkdir(parents=True)
    (folder / "ledger.json").write_text(json.dumps({
        "run_id": rid, "status": "READY_FOR_REVIEW",
        "project": str(Path(e["project"]).resolve()),
        "model": "test-provider/test-model",
        "session_id": "ses-parent-123", "dsh_session_id": "ses-parent-123",
        "operation": "review", "rework_count": 0}))
    change(e, parent_run=rid, session_id="ses-parent-123")

    data, code = invoke(e, "run", e["task"])

    assert data["status"] == "READY_FOR_REVIEW" and code == 0, data
    assert run_calls(dsh)[0]["args"] == [
        "--profile", "headless", "--json", "--permission-mode", "read-only",
        "--approval-policy", "never", "--session-id", "ses-parent-123", "-"]


def test_dsh_review_env_is_minimal_and_uses_host_key(dsh, monkeypatch):
    e = dsh["e"]
    monkeypatch.setenv("HOST_UNRELATED_SENTINEL", "must-not-pass")

    data, code = invoke(e, "run", e["task"])

    assert data["status"] == "READY_FOR_REVIEW" and code == 0, data
    env = run_calls(dsh)[0]["env"]
    assert "OPENCODE_CONFIG_CONTENT" not in env
    assert env.get("DSH_HOME") == str(dsh["home"])
    assert env.get(dsh["key"]) == "test-secret-value"
    assert env.get("HOST_UNRELATED_SENTINEL") is None
    assert env.get("CODING_CONTROL_SETTINGS") is None


def test_dsh_review_missing_api_key_env_fails(dsh, monkeypatch):
    e = dsh["e"]
    monkeypatch.delenv(dsh["key"], raising=False)

    data, code = invoke(e, "run", e["task"])

    assert data["status"] == "FAILED" and code != 0, data
    info = ledger(e, data["run_id"])
    assert "missing api key env" in (info.get("error") or "").lower()


def test_dsh_review_blocked_candidate_stays_blocked(dsh):
    e = dsh["e"]
    set_mode(dsh, "blocked")

    data, code = invoke(e, "run", e["task"])

    assert data["status"] == "BLOCKED" and code != 0, data
    assert ledger(e, data["run_id"])["candidate"]["status"] == "BLOCKED"


@pytest.mark.parametrize("name", [
    "changed_nonempty", "invalid_status", "bad_candidate", "candidate_missing"])
def test_dsh_review_candidate_contract_failures(dsh, name):
    e = dsh["e"]
    set_mode(dsh, name)

    data, code = invoke(e, "run", e["task"])

    assert data["status"] == "FAILED" and code != 0, data
    error = ledger(e, data["run_id"]).get("error") or ""
    assert "candidate" in error.lower(), error


@pytest.mark.parametrize("code_value", ["FS_SANDBOX_DENIED", "PERMISSION_DENIED"])
def test_dsh_review_policy_denial_fails_and_records(dsh, code_value):
    e = dsh["e"]
    (e["tmp"] / "dsh-denial-code.txt").write_text(code_value)
    set_mode(dsh, "denied")

    data, code = invoke(e, "run", e["task"])

    assert data["status"] == "FAILED" and code != 0, data
    info = ledger(e, data["run_id"])
    denials = json.dumps(info.get("policy_denials"))
    assert "write" in denials and code_value in denials, denials
    assert f"policy-denial: {code_value}" in (info.get("error") or "")


def test_dsh_review_missing_step_end_marks_usage_incomplete(dsh):
    e = dsh["e"]
    set_mode(dsh, "no_usage")

    data, code = invoke(e, "run", e["task"])

    assert data["status"] == "FAILED" and code != 0, data
    info = ledger(e, data["run_id"])
    assert info.get("usage_incomplete") is True
    assert "usage" in (info.get("error") or "").lower()


def test_dsh_review_final_session_mismatch_fails(dsh):
    e = dsh["e"]
    set_mode(dsh, "session_mismatch")

    data, code = invoke(e, "run", e["task"])

    assert data["status"] == "FAILED" and code != 0, data
    error = ledger(e, data["run_id"]).get("error") or ""
    assert "session" in error.lower(), error


def test_dsh_timeout_kills_child_process_group(dsh):
    e = dsh["e"]
    set_mode(dsh, "hang")
    change(e, operation="review", timeout_seconds=1)

    data, code = invoke(e, "run", e["task"], timeout=15)

    assert data["status"] == "TIMEOUT" and code != 0, data
    time.sleep(2.5)
    assert not (e["tmp"] / "late-marker").exists()


def test_dsh_cancel_kills_child_process_group(dsh):
    e = dsh["e"]
    set_mode(dsh, "hang")
    change(e, operation="review", timeout_seconds=30)
    runner = subprocess.Popen(
        [PY, str(CONTROL), "run", str(e["task"])],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "CODING_CONTROL_SETTINGS": str(e["settings"])})
    try:
        wait_for(e["tmp"] / "dsh-child-pid", timeout=10)
        rid = next(child.name for child in (e["tmp"] / "state/runs").iterdir()
                   if child.is_dir())
        cancelled, _ = invoke(e, "cancel", rid, timeout=8)
        assert cancelled["status"] == "CANCELLED"
        runner.communicate(timeout=8)
        time.sleep(2.5)
        assert not (e["tmp"] / "late-marker").exists()
    finally:
        if runner.poll() is None:
            runner.terminate()
            runner.communicate(timeout=3)
