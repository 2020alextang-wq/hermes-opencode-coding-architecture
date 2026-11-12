"""B23-T: dsh implement isolation (worktree + trusted apply) contract."""
import json
import os
from pathlib import Path
import subprocess
import time

from test_control import CONTROL, PY, change, e, invoke, ledger
from test_dsh_adapter import dsh, run_calls, set_mode, wait_for

IDENTITY = ("-c", "user.email=test@example.invalid", "-c", "user.name=Test User")


def commit_project(e):
    subprocess.run(["git", *IDENTITY, "add", "-A"], cwd=e["project"],
                   check=True, capture_output=True, timeout=5)
    subprocess.run(["git", *IDENTITY, "commit", "--allow-empty", "-m", "fixture"],
                   cwd=e["project"], check=True, capture_output=True, timeout=5)


def worktree_paths(e):
    result = subprocess.run(["git", "worktree", "list", "--porcelain"],
                            cwd=e["project"], capture_output=True, text=True,
                            timeout=5, check=True)
    return [line[len("worktree "):] for line in result.stdout.splitlines()
            if line.startswith("worktree ")]


def assert_only_main_worktree(e):
    assert [Path(path).resolve() for path in worktree_paths(e)] == [
        Path(e["project"]).resolve()]


def install_bytecode_writes(dsh):
    script = dsh["script"]
    text = script.read_text()
    needle = ('if mode in ("implement_allowed", "implement_out_of_scope", "implement_denied"):\n'
              '    pathlib.Path("src/main.py").write_text("worktree write\\n")\n')
    addition = (needle +
                'if mode == "implement_allowed":\n'
                '    pathlib.Path("src/__pycache__").mkdir(parents=True, exist_ok=True)\n'
                '    pathlib.Path("src/__pycache__/main.cpython-313.pyc").write_bytes(bytes([1, 2, 3]))\n'
                '    pathlib.Path(".pytest_cache/v").mkdir(parents=True, exist_ok=True)\n'
                '    pathlib.Path(".pytest_cache/v/cache").write_text("cache\\n")\n')
    assert needle in text
    script.write_text(text.replace(needle, addition, 1))
    script.chmod(0o755)


def test_dsh_implement_requires_git_project(dsh):
    e = dsh["e"]
    sibling = e["root"] / "no-git"
    sibling.mkdir()
    (sibling / "ok.py").write_text("ok\n")
    change(e, operation="implement", project=str(sibling), allowed_files=["ok.py"])

    data, code = invoke(e, "preflight", e["task"])

    assert data["status"] == "BLOCKED" and code != 0, data
    assert "dsh implement requires git project" in json.dumps(data), data
    assert not (e["tmp"] / "record.json").exists()


def test_dsh_implement_applies_allowed_file_from_worktree(dsh):
    e = dsh["e"]
    commit_project(e)
    change(e, operation="implement")
    set_mode(dsh, "implement_allowed")

    data, code = invoke(e, "run", e["task"], timeout=25)

    assert data["status"] == "READY_FOR_REVIEW" and code == 0, data
    project = Path(e["project"])
    assert (project / "src/main.py").read_text() == "worktree write\n"
    info = ledger(e, data["run_id"])
    assert info["modified_files"] == ["src/main.py"]
    assert info.get("worktree_out_of_scope", []) == []
    record = run_calls(dsh)[0]
    assert record["args"] == ["--profile", "headless", "--json", "--permission-mode",
                              "workspace-write", "--approval-policy", "never", "-"]
    worktree = Path(record["cwd"])
    assert worktree.resolve() != project.resolve()
    assert project.resolve() not in worktree.resolve().parents
    assert f"Project: {record['cwd']}" in record["stdin"]
    assert_only_main_worktree(e)


def test_dsh_implement_out_of_scope_change_is_not_applied(dsh):
    e = dsh["e"]
    commit_project(e)
    change(e, operation="implement")
    set_mode(dsh, "implement_out_of_scope")

    data, code = invoke(e, "run", e["task"], timeout=25)

    assert data["status"] == "FAILED" and code != 0, data
    info = ledger(e, data["run_id"])
    assert "src/outside.py" in info.get("worktree_out_of_scope", [])
    assert "worktree out-of-scope: src/outside.py" in (info.get("error") or "")
    assert not (Path(e["project"]) / "src/outside.py").exists()
    assert_only_main_worktree(e)


def test_dsh_implement_authorized_delete_is_applied(dsh):
    e = dsh["e"]
    project = Path(e["project"])
    (project / "src/obsolete.py").write_text("obsolete\n")
    commit_project(e)
    change(e, operation="implement", allowed_files=["src/main.py", "src/obsolete.py"],
           allow_delete=["src/obsolete.py"])
    set_mode(dsh, "implement_delete_allowed")

    data, code = invoke(e, "run", e["task"], timeout=25)

    assert data["status"] == "READY_FOR_REVIEW" and code == 0, data
    assert not (project / "src/obsolete.py").exists()
    info = ledger(e, data["run_id"])
    assert "src/obsolete.py" in info.get("deleted_files", [])
    assert "src/obsolete.py" in info["modified_files"]
    assert_only_main_worktree(e)


def test_dsh_implement_unauthorized_delete_is_not_applied(dsh):
    e = dsh["e"]
    project = Path(e["project"])
    (project / "src/obsolete.py").write_text("obsolete\n")
    commit_project(e)
    change(e, operation="implement", allowed_files=["src/main.py", "src/obsolete.py"])
    set_mode(dsh, "implement_delete_unauthorized")

    data, code = invoke(e, "run", e["task"], timeout=25)

    assert data["status"] == "FAILED" and code != 0, data
    info = ledger(e, data["run_id"])
    assert "src/obsolete.py" in info.get("worktree_unauthorized_delete", [])
    assert (project / "src/obsolete.py").read_text() == "obsolete\n"
    assert_only_main_worktree(e)


def test_dsh_implement_baseline_drift_blocks_overwrite(dsh):
    e = dsh["e"]
    commit_project(e)
    change(e, operation="implement")
    set_mode(dsh, "implement_drift")
    runner = subprocess.Popen(
        [PY, str(CONTROL), "run", str(e["task"])],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "CODING_CONTROL_SETTINGS": str(e["settings"])})
    try:
        wait_for(e["tmp"] / "dsh-implement-started", timeout=10)
        project = Path(e["project"])
        (project / "src/main.py").write_text("external change\n")
        (e["tmp"] / "dsh-release").write_text("go")
        out, err = runner.communicate(timeout=25)
        assert runner.returncode != 0, (runner.returncode, out, err)
        data = json.loads(out)
        assert data["status"] == "FAILED", data
        info = ledger(e, data["run_id"])
        assert "baseline drift: src/main.py" in (info.get("error") or "")
        assert (project / "src/main.py").read_text() == "external change\n"
        assert_only_main_worktree(e)
    finally:
        if runner.poll() is None:
            runner.terminate()
            runner.communicate(timeout=3)


def test_dsh_implement_timeout_cleans_worktree_without_apply(dsh):
    e = dsh["e"]
    commit_project(e)
    change(e, operation="implement", timeout_seconds=1)
    set_mode(dsh, "implement_hang")

    data, code = invoke(e, "run", e["task"], timeout=20)

    assert data["status"] == "TIMEOUT" and code != 0, data
    assert (Path(e["project"]) / "src/main.py").read_text() == "original\n"
    time.sleep(2.5)
    assert not (e["tmp"] / "late-marker").exists()
    assert_only_main_worktree(e)


def test_dsh_implement_cancel_cleans_worktree_without_apply(dsh):
    e = dsh["e"]
    commit_project(e)
    change(e, operation="implement", timeout_seconds=30)
    set_mode(dsh, "implement_hang")
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
        runner.communicate(timeout=10)
        assert (Path(e["project"]) / "src/main.py").read_text() == "original\n"
        time.sleep(2.5)
        assert not (e["tmp"] / "late-marker").exists()
        assert_only_main_worktree(e)
    finally:
        if runner.poll() is None:
            runner.terminate()
            runner.communicate(timeout=3)


def test_dsh_implement_policy_denial_fails(dsh):
    e = dsh["e"]
    commit_project(e)
    (e["tmp"] / "dsh-denial-code.txt").write_text("FS_SANDBOX_DENIED")
    change(e, operation="implement")
    set_mode(dsh, "implement_denied")

    data, code = invoke(e, "run", e["task"], timeout=25)

    assert data["status"] == "FAILED" and code != 0, data
    info = ledger(e, data["run_id"])
    assert "policy-denial: FS_SANDBOX_DENIED" in (info.get("error") or "")
    assert_only_main_worktree(e)


def test_dsh_implement_applies_then_content_scan_fails(dsh):
    e = dsh["e"]
    commit_project(e)
    change(e, operation="implement")
    set_mode(dsh, "implement_secret")

    data, code = invoke(e, "run", e["task"], timeout=25)

    assert data["status"] == "FAILED" and code != 0, data
    error = ledger(e, data["run_id"]).get("error") or ""
    assert "content-scan" in error
    assert "eyJ" not in error
    assert_only_main_worktree(e)


def test_dsh_implement_ignores_bytecode_artifacts(dsh):
    e = dsh["e"]
    project = Path(e["project"])
    (project / ".gitignore").write_text("\n")
    commit_project(e)
    install_bytecode_writes(dsh)
    change(e, operation="implement")
    set_mode(dsh, "implement_allowed")

    data, code = invoke(e, "run", e["task"], timeout=25)

    assert data["status"] == "READY_FOR_REVIEW" and code == 0, data
    assert (project / "src/main.py").read_text() == "worktree write\n"
    info = ledger(e, data["run_id"])
    assert info.get("worktree_out_of_scope", []) == []
    ignored = info.get("worktree_ignored_artifacts", [])
    assert "src/__pycache__/main.cpython-313.pyc" in ignored
    assert ".pytest_cache/v/cache" in ignored
    assert_only_main_worktree(e)


def test_dsh_env_disables_bytecode(dsh):
    e = dsh["e"]

    data, code = invoke(e, "run", e["task"])

    assert data["status"] == "READY_FOR_REVIEW" and code == 0, data
    env = run_calls(dsh)[0]["env"]
    assert env.get("PYTHONDONTWRITEBYTECODE") == "1"
