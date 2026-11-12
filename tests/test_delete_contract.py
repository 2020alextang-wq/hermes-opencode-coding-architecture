"""Delete contract: whole-file deletion needs explicit allow_delete and is attributed."""
import json
import subprocess

import pytest

from test_control import FAKE, PY, change, invoke, ledger, mode


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
    (project / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.pyc\n")
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


def test_deleted_allowed_file_without_declaration_fails_as_deleted_file(e):
    mode(e, "delete_allowed")
    data, code = invoke(e, "run", e["task"])
    info = ledger(e, data["run_id"])
    assert data["status"] == "FAILED" and code != 0
    assert "deleted-file: src/main.py" in (info.get("error") or "")


def test_declared_allow_delete_permits_deletion(e):
    change(e, allow_delete=["src/main.py"])
    mode(e, "delete_allowed")
    data, code = invoke(e, "run", e["task"])
    info = ledger(e, data["run_id"])
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
    assert info.get("allow_delete") == ["src/main.py"]
    assert info.get("deleted_files") == ["src/main.py"]
    assert "deleted-file" not in (info.get("error") or "")


def test_deleted_unauthorized_file_is_attributed_to_deleted_file(e):
    mode(e, "delete_other")
    data, code = invoke(e, "run", e["task"])
    info = ledger(e, data["run_id"])
    assert data["status"] == "FAILED" and code != 0
    assert "deleted-file: src/other.py" in (info.get("error") or "")
    assert "out-of-scope changes" not in (info.get("error") or "")


def test_deleting_allowed_test_file_without_declaration_fails(e):
    change(e, allowed_files=["src/main.py", "tests/test_control.py"])
    mode(e, "delete_test")
    data, code = invoke(e, "run", e["task"])
    info = ledger(e, data["run_id"])
    assert data["status"] == "FAILED" and code != 0
    assert "deleted-file: tests/test_control.py" in (info.get("error") or "")


@pytest.mark.parametrize("bad,reason", [
    ("/tmp/absolute.py", "invalid allow_delete"),
    ("src/*.py", "invalid allow_delete"),
    ("src/other.py", "allow_delete not in allowed_files"),
])
def test_invalid_allow_delete_blocks_before_cli(e, bad, reason):
    change(e, allow_delete=[bad])
    data, code = invoke(e, "preflight", e["task"])
    assert data["status"] == "BLOCKED" and code != 0
    assert reason in json.dumps(data)
    ran, run_code = invoke(e, "run", e["task"])
    assert ran["status"] == "BLOCKED" and run_code != 0
    assert not (e["tmp"] / "record.json").exists()


def test_no_deletion_records_empty_allow_delete_and_deleted_files(e):
    checked, code = invoke(e, "preflight", e["task"])
    assert code == 0 and checked["status"] == "ok"
    assert checked.get("allow_delete") == []
    data, code = invoke(e, "run", e["task"])
    info = ledger(e, data["run_id"])
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
    assert info.get("allow_delete") == []
    assert info.get("deleted_files") == []
