"""B23-T: dsh session-log model verification contract."""
import json
import os
from pathlib import Path
import subprocess
import threading
import time

import coding_control

from test_control import change, e, invoke, ledger
from test_dsh_adapter import dsh, set_mode

EXPECTED = "deepseek-official/deepseek-flash"
IDENTITY = ("-c", "user.email=test@example.invalid", "-c", "user.name=Test User")

ZSTD = r'''#!/usr/bin/env python3
import pathlib, sys
root = pathlib.Path(__file__).resolve().parent
mode_file = root / "zstd-mode.txt"
mode = mode_file.read_text().strip() if mode_file.exists() else "payload"
if mode == "fail":
    sys.exit(2)
if mode == "no_model":
    print('{"type":"session","note":"no model fields"}')
    sys.exit(0)
if not sys.argv[1:]:
    sys.exit(2)
path = pathlib.Path(sys.argv[-1])
payload = pathlib.Path(str(path) + ".payload")
print(payload.read_text() if payload.exists() else "")
'''


def install_zstd(dsh, monkeypatch, mode="payload"):
    bin_dir = dsh["e"]["tmp"] / "zstd-bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "zstd"
    script.write_text(ZSTD)
    script.chmod(0o755)
    (bin_dir / "zstd-mode.txt").write_text(mode)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    return bin_dir


def set_runner_model(dsh, model=EXPECTED):
    config = json.loads(dsh["e"]["settings"].read_text())
    config.pop("runner_model", None)
    config["runner"]["model"] = model
    dsh["e"]["settings"].write_text(json.dumps(config))
    return model


def write_session_log(dsh, version=1, payload=None):
    folder = dsh["home"] / "sessions" / "project" / "session-dsh-session-1"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"session.v{version}.jsonl.zstd"
    path.write_text("compressed-placeholder\n")
    if payload is not None:
        Path(str(path) + ".payload").write_text(payload)
    return path


def payload(provider="deepseek-official", model="deepseek-flash"):
    return (json.dumps({"type": "session", "sessionId": "dsh-session-1"}) + "\n" +
            json.dumps({"type": "request", "provider": provider, "model": model}) + "\n")


def commit_project(e):
    subprocess.run(["git", *IDENTITY, "add", "-A"], cwd=e["project"],
                   check=True, capture_output=True, timeout=5)
    subprocess.run(["git", *IDENTITY, "commit", "--allow-empty", "-m", "fixture"],
                   cwd=e["project"], check=True, capture_output=True, timeout=5)


def test_dsh_review_model_verified_from_session_log(dsh, monkeypatch):
    e = dsh["e"]
    expected = set_runner_model(dsh)
    install_zstd(dsh, monkeypatch)
    write_session_log(dsh, 1, payload())

    data, code = invoke(e, "run", e["task"])

    assert data["status"] == "READY_FOR_REVIEW" and code == 0, data
    info = ledger(e, data["run_id"])
    assert info.get("actual_model") == expected
    assert info.get("model_verified") is True
    assert not info.get("unverified")


def test_dsh_model_mismatch_fails(dsh, monkeypatch):
    e = dsh["e"]
    expected = set_runner_model(dsh)
    install_zstd(dsh, monkeypatch)
    write_session_log(dsh, 1, payload("other-provider", "other-model"))

    data, code = invoke(e, "run", e["task"])

    assert data["status"] == "FAILED" and code != 0, data
    error = ledger(e, data["run_id"]).get("error") or ""
    assert f"actual model mismatch: other-provider/other-model != {expected}" in error


def test_dsh_missing_session_log_stays_unverified(dsh, monkeypatch):
    e = dsh["e"]
    set_runner_model(dsh)
    install_zstd(dsh, monkeypatch)

    data, code = invoke(e, "run", e["task"])

    assert data["status"] == "READY_FOR_REVIEW" and code == 0, data
    info = ledger(e, data["run_id"])
    assert info.get("model_verified") is False
    assert info.get("actual_model") is None
    assert info.get("unverified")


def test_dsh_zstd_failure_stays_unverified(dsh, monkeypatch):
    e = dsh["e"]
    set_runner_model(dsh)
    install_zstd(dsh, monkeypatch, mode="fail")
    write_session_log(dsh, 1, payload())

    data, code = invoke(e, "run", e["task"])

    assert data["status"] == "READY_FOR_REVIEW" and code == 0, data
    info = ledger(e, data["run_id"])
    assert info.get("model_verified") is False
    assert info.get("unverified")


def test_dsh_log_without_model_fields_stays_unverified(dsh, monkeypatch):
    e = dsh["e"]
    set_runner_model(dsh)
    install_zstd(dsh, monkeypatch, mode="no_model")
    write_session_log(dsh, 1, payload())

    data, code = invoke(e, "run", e["task"])

    assert data["status"] == "READY_FOR_REVIEW" and code == 0, data
    info = ledger(e, data["run_id"])
    assert info.get("model_verified") is False
    assert info.get("unverified")


def test_dsh_picks_numerically_largest_session_version(dsh, monkeypatch):
    e = dsh["e"]
    expected = set_runner_model(dsh)
    install_zstd(dsh, monkeypatch)
    write_session_log(dsh, 2, payload("wrong-provider", "wrong-model"))
    write_session_log(dsh, 10, payload())

    data, code = invoke(e, "run", e["task"])

    assert data["status"] == "READY_FOR_REVIEW" and code == 0, data
    info = ledger(e, data["run_id"])
    assert info.get("actual_model") == expected
    assert info.get("model_verified") is True


def test_dsh_implement_model_verified_from_session_log(dsh, monkeypatch):
    e = dsh["e"]
    commit_project(e)
    expected = set_runner_model(dsh)
    install_zstd(dsh, monkeypatch)
    write_session_log(dsh, 1, payload())
    change(e, operation="implement")
    set_mode(dsh, "implement_allowed")

    data, code = invoke(e, "run", e["task"], timeout=25)

    assert data["status"] == "READY_FOR_REVIEW" and code == 0, data
    info = ledger(e, data["run_id"])
    assert info.get("actual_model") == expected
    assert info.get("model_verified") is True
    assert (Path(e["project"]) / "src/main.py").read_text() == "worktree write\n"


def test_verify_waits_for_delayed_session_log(tmp_path, monkeypatch):
    home = tmp_path / "dsh-home"
    (home / "sessions").mkdir(parents=True)
    install_zstd({"e": {"tmp": tmp_path}}, monkeypatch)
    fake = {"home": home}

    def delayed_write():
        time.sleep(2)
        write_session_log(fake, 1, payload())

    thread = threading.Thread(target=delayed_write)
    thread.start()
    info = {"dsh_session_id": "dsh-session-1", "unverified": []}
    runner = {"dsh_home": str(home), "model": EXPECTED}
    coding_control.verify_dsh_model(info, runner, {})
    thread.join(timeout=5)

    assert info["model_verified"] is True
    assert info["actual_model"] == EXPECTED
    assert not info["unverified"]


def test_verify_without_session_log_times_out(tmp_path, monkeypatch):
    home = tmp_path / "dsh-home"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setattr(coding_control, "DSH_SESSION_LOG_WAIT", 0.6)
    monkeypatch.setattr(coding_control, "DSH_SESSION_LOG_INTERVAL", 0.1)
    info = {"dsh_session_id": "dsh-session-1", "unverified": []}
    runner = {"dsh_home": str(home), "model": EXPECTED}
    started = time.monotonic()
    coding_control.verify_dsh_model(info, runner, {})
    elapsed = time.monotonic() - started

    assert info["model_verified"] is False
    assert info["actual_model"] is None
    assert any("dsh session log not found" in reason for reason in info["unverified"])
    assert elapsed < 3


def test_dsh_prefixed_session_id_still_finds_log(dsh, monkeypatch):
    """Regression: real event streams report dsh_session_id already carrying
    the 'session-' prefix; the glob must not double the prefix (2026-11-12
    real smoke defe5fe2f4ac caught this after fake-only tests stayed green)."""
    install_zstd(dsh, monkeypatch)
    write_session_log(dsh, 1, payload())
    runner = json.loads(dsh["e"]["settings"].read_text())["runner"]
    info = {"dsh_session_id": "session-dsh-session-1", "unverified": []}
    coding_control.verify_dsh_model(info, runner, {})
    assert info["model_verified"] is True, info
    assert info["actual_model"] == EXPECTED
    assert not info["unverified"]
