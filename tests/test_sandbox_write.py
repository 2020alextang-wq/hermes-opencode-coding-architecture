"""B19-T: executor write confinement through macOS seatbelt."""
import importlib
import json
from pathlib import Path

from test_control import FAKE, change, e, invoke, ledger, mode


def settings(e):
    return json.loads(e["settings"].read_text())


def save_settings(e, data):
    e["settings"].write_text(json.dumps(data))


def set_sandbox(e, value):
    data = settings(e)
    runner = data.setdefault("runner", {})
    if value is None:
        runner.pop("sandbox", None)
    else:
        runner["sandbox"] = value
    save_settings(e, data)


def outside_path(e):
    return e["tmp"] / "outside-project.txt"


def cleanup(targets):
    for target in targets:
        try:
            Path(target).unlink()
        except FileNotFoundError:
            pass


def install_sandbox_fake(e):
    """Keep the fake and its evidence inside the project so seatbelt can write them."""
    root = e["project"] / ".fake_state"
    root.mkdir()
    cli = root / "fake_cli.py"
    cli.write_text(FAKE)
    cli.chmod(0o755)
    (root / "mode").write_text("escape_write")
    (root / "escape_target").write_text(str(outside_path(e)))
    data = settings(e)
    data["cli"] = str(cli)
    save_settings(e, data)
    return root


def test_default_sandbox_none_is_recorded_and_allows_escape_write(e):
    set_sandbox(e, None)
    mode(e, "escape_write")
    checked, code = invoke(e, "preflight", e["task"])
    assert code == 0 and checked["status"] == "ok", checked
    assert checked["runner"]["sandbox"] == "none"

    result, code = invoke(e, "run", e["task"])
    assert code == 0 and result["status"] == "READY_FOR_REVIEW", result
    info = ledger(e, result["run_id"])
    assert info["runner_config"]["sandbox"] == "none"

    record = json.loads((e["tmp"] / "record.json").read_text())
    targets = record["escape"]["targets"]
    assert targets[0] == str(outside_path(e))
    try:
        assert outside_path(e).exists()
        assert all(Path(target).exists() for target in targets), record["escape"]
    finally:
        cleanup(targets)


def test_seatbelt_denies_escape_write_and_wraps_executor_command(e, monkeypatch):
    root = install_sandbox_fake(e)
    set_sandbox(e, "seatbelt")

    checked, code = invoke(e, "preflight", e["task"])
    assert code == 0 and checked["status"] == "ok", checked
    assert checked["runner"]["sandbox"] == "seatbelt"
    assert not (root / "record.json").exists()

    control = importlib.import_module("coding_control")
    original = control.subprocess.Popen
    seen = []

    def capture(argv, *args, **kwargs):
        seen.append(list(argv))
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(control.subprocess, "Popen", capture)
    task = json.loads(e["task"].read_text())
    result = control.run(task, settings(e))
    assert result["status"] == "READY_FOR_REVIEW", result

    info = ledger(e, result["run_id"])
    assert info["runner_config"]["sandbox"] == "seatbelt"

    wrappers = [argv for argv in seen if Path(str(argv[0])).name == "sandbox-exec"]
    assert wrappers, seen
    argv = wrappers[0]
    assert "-p" in argv
    profile = argv[argv.index("-p") + 1]
    assert "deny file-write*" in profile
    assert str(e["project"].resolve()) in profile
    assert str((e["tmp"] / "state").resolve()) in profile
    assert "/dev/null" in profile

    record = json.loads((root / "record.json").read_text())
    assert record["cwd"] == str(e["project"])
    assert record["escape"]["wrote"] == []
    assert record["escape"]["errors"]
    for target in record["escape"]["targets"]:
        assert not Path(target).exists(), target


def test_unknown_sandbox_value_blocks_before_cli(e):
    set_sandbox(e, "bwrap")
    for op in ("preflight", "run"):
        data, code = invoke(e, op, e["task"])
        assert data["status"] == "BLOCKED" and code != 0, (op, data)
        assert "unknown sandbox" in json.dumps(data), (op, data)
    assert not (e["tmp"] / "record.json").exists()


def test_seatbelt_without_sandbox_executable_blocks(e, monkeypatch):
    set_sandbox(e, "seatbelt")
    control = importlib.import_module("coding_control")
    original_which = control.shutil.which

    def which(name):
        return None if name == "sandbox-exec" else original_which(name)

    monkeypatch.setattr(control.shutil, "which", which)
    task = json.loads(e["task"].read_text())

    checked = control.preflight(task, settings(e))
    assert checked["status"] == "BLOCKED", checked
    assert "sandbox executable not found" in json.dumps(checked)

    result = control.run(task, settings(e))
    assert result["status"] == "BLOCKED", result
    assert "sandbox executable not found" in json.dumps(result)
    assert not (e["tmp"] / "record.json").exists()
