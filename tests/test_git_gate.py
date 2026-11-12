"""B16-T: staged-change git gate contract (tests only)."""
import importlib
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "tools" / "git_gate.py"
INSTALL = ROOT / "hooks" / "install.py"
PY = sys.executable
MARKER = "# coding-control git gate"
JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
       "eyJzdWIiOiIxMjM0NTY3ODkwIn0")


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, text=True,
                          capture_output=True, timeout=10)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    assert git(path, "init").returncode == 0
    assert git(path, "config", "user.email", "test@example.invalid").returncode == 0
    assert git(path, "config", "user.name", "Test User").returncode == 0
    return path


def stage(repo, name, content):
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content)
    assert git(repo, "add", name).returncode == 0
    return target


def run_gate(repo):
    return subprocess.run([PY, str(GATE)], cwd=repo, text=True,
                          capture_output=True, timeout=30)


def output(proc):
    return proc.stdout + proc.stderr


def runner_rules(text):
    control = importlib.import_module("coding_control")
    with tempfile.TemporaryDirectory() as folder:
        probe = Path(folder) / "probe.py"
        probe.write_text(text)
        issues = control.scan_file(probe)
    return {
        issue.split("]")[0].split("[", 1)[1] if "[" in issue else "secret"
        for issue in issues
    }


def findings(text):
    return [line for line in text.splitlines() if line.strip()]


def test_clean_staged_file_exits_zero(repo):
    stage(repo, "notes.txt", "ordinary text\n")
    proc = run_gate(repo)
    assert proc.returncode == 0, output(proc)
    assert findings(output(proc)) == []


def test_no_staged_changes_exits_zero(repo):
    (repo / "notes.txt").write_text("ordinary text\n")
    proc = run_gate(repo)
    assert proc.returncode == 0, output(proc)
    assert findings(output(proc)) == []


def test_jwt_literal_fails_with_rule_and_line_without_value(repo):
    text = f"value = 1\ntoken = \"{JWT}\"\n"
    stage(repo, "secret.py", text)
    expected = runner_rules(text)
    assert expected, "runner scanner no longer detects the test JWT"
    proc = run_gate(repo)
    out = output(proc)
    assert proc.returncode == 1, out
    assert "secret.py:2 " in out, out
    assert any(rule in out for rule in expected), (expected, out)
    assert JWT not in out and "eyJ" not in out, out
    for line in findings(out):
        assert re.fullmatch(r"\S+:\d+ \S+", line), line


@pytest.mark.parametrize("marker", [
    "pytest.mark.skip(reason=\"later\")",
    "value = 1  # noqa",
])
def test_downgrade_markers_fail(repo, marker):
    stage(repo, "tests/test_example.py", f"def test_example():\n    {marker}\n")
    proc = run_gate(repo)
    out = output(proc)
    assert proc.returncode == 1, out
    assert "tests/test_example.py:2 " in out, out
    assert marker not in out, out


def test_committed_secret_is_not_scanned(repo):
    stage(repo, "secret.py", f"token = \"{JWT}\"\n")
    assert git(repo, "commit", "-m", "old secret").returncode == 0
    stage(repo, "clean.py", "value = 1\n")
    proc = run_gate(repo)
    out = output(proc)
    assert proc.returncode == 0, out
    assert "eyJ" not in out, out


def test_staged_binary_fails_as_binary_change(repo):
    stage(repo, "blob.bin", b"\x00\x01binary\x00data")
    proc = run_gate(repo)
    out = output(proc)
    assert proc.returncode == 1, out
    assert "binary-change" in out, out
    assert "blob.bin" in out, out


def install(repo):
    return subprocess.run([PY, str(INSTALL)], cwd=repo, text=True,
                          capture_output=True, timeout=30)


def hook_path(repo):
    return repo / ".git/hooks/pre-commit"


def run_hook(repo):
    return subprocess.run(["/bin/sh", str(hook_path(repo))], cwd=repo,
                          text=True, capture_output=True, timeout=30)


def test_fresh_install_writes_executable_marked_hook(repo):
    proc = install(repo)
    assert proc.returncode == 0, output(proc)
    hook = hook_path(repo)
    assert hook.is_file()
    assert stat.S_IMODE(hook.stat().st_mode) == 0o755
    first = hook.read_text().splitlines()[0]
    assert MARKER in first


def test_installed_hook_blocks_staged_secret_then_passes_when_clean(repo):
    assert install(repo).returncode == 0
    stage(repo, "secret.py", f"token = \"{JWT}\"\n")
    blocked = run_hook(repo)
    out = output(blocked)
    assert blocked.returncode != 0, out
    assert "eyJ" not in out, out
    assert git(repo, "rm", "--cached", "-q", "secret.py").returncode == 0
    clean = run_hook(repo)
    assert clean.returncode == 0, output(clean)


def test_existing_custom_hook_is_not_overwritten(repo):
    hook = hook_path(repo)
    hook.parent.mkdir(parents=True, exist_ok=True)
    custom = "#!/bin/sh\necho custom hook\n"
    hook.write_text(custom)
    hook.chmod(0o755)
    proc = install(repo)
    assert proc.returncode == 1, output(proc)
    assert hook.read_text() == custom
    assert stat.S_IMODE(hook.stat().st_mode) == 0o755
