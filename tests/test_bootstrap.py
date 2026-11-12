"""B3 bootstrap thin-entry contract tests.

The legacy bootstrap executes a real OpenCode CLI at import time. Every probe
runs a byte-identical copy inside a temporary directory while intercepting
subprocess.Popen and Path.read_text, so the repository bootstrap/logs/phase
files are never imported, truncated or executed.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BOOTSTRAP = ROOT / "bootstrap.py"
PY = sys.executable
LEGACY_CLI_NAME = "opencode"
OPERATIONS = ("preflight", "run", "status", "usage", "cancel")


class _FakeProc:
    def __init__(self):
        self.pid = 4242
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


STUB_CONTROL = '''import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ["BOOTSTRAP_STUB_DIR"])
(ROOT / "imported").write_text("1")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    with (ROOT / "calls.jsonl").open("a") as stream:
        stream.write(json.dumps(argv) + "\\n")
    print(json.dumps({"stub": True, "argv": argv}))
    return int(os.environ.get("BOOTSTRAP_STUB_EXIT", "0"))
'''


def _copy_bootstrap(folder):
    original = BOOTSTRAP.read_bytes()
    target = folder / "bootstrap.py"
    target.write_bytes(original)
    assert (hashlib.sha256(target.read_bytes()).hexdigest() ==
            hashlib.sha256(original).hexdigest())
    return target


def _phase_sentinels(folder, argv):
    names = {"IMPLEMENTATION-v1.0.md"}
    if argv:
        names.add(argv[0])
    blocked = set()
    for name in names:
        path = folder / name
        if not path.exists():
            path.write_text("PHASE SENTINEL: bootstrap must not read this\n")
        blocked.add(path.resolve())
    probe = folder / "bootstrap-execution.jsonl"
    if not probe.exists():
        probe.write_text("")
    return blocked


def _install_stub(folder, monkeypatch):
    stub = folder / "stub"
    stub.mkdir()
    (stub / "coding_control.py").write_text(STUB_CONTROL)
    monkeypatch.setenv("BOOTSTRAP_STUB_DIR", str(stub))
    return stub


def _patch_popen(monkeypatch, calls):
    real = subprocess.Popen

    def fake(cmd, *args, **kwargs):
        sequence = list(cmd) if isinstance(cmd, (list, tuple)) else [cmd]
        executable = str(sequence[0]) if sequence else ""
        if Path(executable).name == LEGACY_CLI_NAME:
            calls.append({"argv": sequence, "cwd": kwargs.get("cwd")})
            return _FakeProc()
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake)


def run_bootstrap(folder, monkeypatch, argv, *, stub, as_main=True):
    target = _copy_bootstrap(folder)
    blocked = _phase_sentinels(folder, argv)

    popen_calls = []
    phase_reads = []
    real_read_text = Path.read_text

    def guarded_read_text(self, *args, **kwargs):
        try:
            resolved = self.resolve()
        except OSError:
            resolved = self
        if resolved in blocked:
            phase_reads.append(str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    _patch_popen(monkeypatch, popen_calls)

    stub_dir = _install_stub(folder, monkeypatch) if stub else None
    sys.modules.pop("coding_control", None)
    sys.path.insert(0, str(stub_dir if stub else ROOT))

    saved_argv = sys.argv
    sys.argv = [str(target), *argv]
    stdout, stderr = io.StringIO(), io.StringIO()
    exit_code = None
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            runpy.run_path(str(target),
                           run_name="__main__" if as_main else "bootstrap_probe")
    except SystemExit as exc:
        exit_code = 0 if exc.code is None else exc.code
    finally:
        sys.argv = saved_argv
        sys.path.pop(0)
        sys.modules.pop("coding_control", None)

    imported = bool(stub_dir and (stub_dir / "imported").is_file())
    stub_calls = []
    if stub_dir and (stub_dir / "calls.jsonl").is_file():
        stub_calls = [json.loads(line) for line in
                      (stub_dir / "calls.jsonl").read_text().splitlines() if line]
    return SimpleNamespace(
        exit_code=exit_code,
        stdout=stdout.getvalue(),
        stderr=stderr.getvalue(),
        popen=popen_calls,
        phase_reads=phase_reads,
        stub_imported=imported,
        stub_calls=stub_calls,
    )


def test_import_bootstrap_has_no_top_level_side_effects(tmp_path, monkeypatch):
    probe = tmp_path / "bootstrap-execution.jsonl"
    probe.write_text("PROBE-SENTINEL\n")

    result = run_bootstrap(tmp_path, monkeypatch, [], stub=True, as_main=False)

    assert result.exit_code is None
    assert result.popen == []
    assert result.phase_reads == []
    assert result.stub_imported is False
    assert result.stub_calls == []
    assert probe.read_text() == "PROBE-SENTINEL\n"


def test_help_works_without_config_and_never_calls_cli(tmp_path, monkeypatch):
    monkeypatch.delenv("CODING_CONTROL_SETTINGS", raising=False)

    for index, flag in enumerate(("--help", "-h")):
        folder = tmp_path / f"help-{index}"
        folder.mkdir()
        result = run_bootstrap(folder, monkeypatch, [flag], stub=True)

        assert result.exit_code == 0
        assert result.popen == []
        assert result.phase_reads == []
        assert result.stub_imported is False
        assert result.stub_calls == []
        text = (result.stdout + result.stderr).lower()
        assert "usage" in text or all(op in text for op in OPERATIONS)


@pytest.mark.parametrize("argv", [
    [],
    ["deploy", "task.json"],
    ["IMPLEMENTATION-v1.0.md"],
    ["B3-task.md"],
    ["preflight", "task.json", "some/provider-model"],
    ["run", "task.json", "ses_abcdef123456"],
    ["run", "task.json", "some/provider-model", "ses_abcdef123456"],
    ["status", "abcdef123456", "extra"],
])
def test_invalid_legacy_or_extra_arguments_are_rejected_without_model(
        tmp_path, monkeypatch, argv):
    result = run_bootstrap(tmp_path, monkeypatch, argv, stub=True)

    assert result.exit_code not in (None, 0)
    assert result.popen == []
    assert result.phase_reads == []
    assert result.stub_imported is False
    assert result.stub_calls == []


@pytest.mark.parametrize("operation", OPERATIONS)
def test_single_operation_argument_is_forwarded_unchanged(
        tmp_path, monkeypatch, operation):
    argument = "abcdef123456" if operation in ("status", "usage", "cancel") else "task.json"
    monkeypatch.setenv("BOOTSTRAP_STUB_EXIT", "7")

    result = run_bootstrap(tmp_path, monkeypatch, [operation, argument], stub=True)

    assert result.stub_imported is True
    assert result.stub_calls == [[operation, argument]]
    assert json.loads(result.stdout) == {"stub": True, "argv": [operation, argument]}
    assert result.exit_code == 7
    assert result.popen == []
    assert result.phase_reads == []


def test_malformed_task_json_propagates_failure_without_model(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    bad_task = tmp_path / "task.json"
    bad_task.write_text("{not json")
    monkeypatch.setenv("CODING_CONTROL_SETTINGS", str(settings))
    folder = tmp_path / "entry"
    folder.mkdir()

    result = run_bootstrap(folder, monkeypatch, ["preflight", str(bad_task)], stub=False)

    assert result.exit_code not in (None, 0)
    assert result.popen == []
    assert result.phase_reads == []
    assert json.loads(result.stdout)["status"] == "BLOCKED"


def test_real_isolated_preflight_is_wired_through_bootstrap(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    project = workspace / "project"
    (project / "src").mkdir(parents=True)
    (project / "src/main.py").write_text("original\n")
    (project / "tests").mkdir()
    (project / "tests/test_ok.py").write_text(
        "def test_ok(tmp_path):\n    assert tmp_path.is_dir()\n")
    (project / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.pyc\n")
    subprocess.run(["git", "init"], cwd=project, check=True,
                   capture_output=True, timeout=5)
    subprocess.run(["git", "add", "."], cwd=project, check=True,
                   capture_output=True, timeout=5)

    cli = tmp_path / "fake_cli.py"
    cli.write_text("#!/usr/bin/env python3\nimport sys\n"
                   "if sys.argv[1:] == ['--version']:\n"
                   "    print('1.2.3')\n"
                   "elif sys.argv[1:] == ['run', '--help']:\n"
                   "    print('--dir --agent --format')\n"
                   "sys.exit(0)\n")
    cli.chmod(0o755)

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "workspace_root": str(workspace),
        "cli": str(cli),
        "runner_model": "test-provider/test-model",
        "state_dir": str(tmp_path / "state"),
    }))
    task = tmp_path / "task.json"
    task.write_text(json.dumps({
        "project": str(project),
        "goal": "Update src/main.py",
        "allowed_files": ["src/main.py"],
        "test_commands": [[PY, "-m", "pytest", "tests/test_ok.py", "-q"]],
    }))

    monkeypatch.setenv("CODING_CONTROL_SETTINGS", str(settings))
    folder = tmp_path / "entry"
    folder.mkdir()

    result = run_bootstrap(folder, monkeypatch, ["preflight", str(task)], stub=False)

    assert result.exit_code == 0
    assert result.popen == []
    assert result.phase_reads == []
    data = json.loads(result.stdout)
    assert data["status"] == "ok"
    assert data["project"] == str(project.resolve())
    assert data["files"]["src/main.py"]
