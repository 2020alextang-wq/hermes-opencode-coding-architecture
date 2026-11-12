"""B22-T: dsh headless probe capabilities and runner config gate."""
import json

import pytest

from test_control import change, e, invoke

HELP = "--profile <name>\n--json\n--permission-mode <mode>\n--approval-policy <policy>\n--session-id <id>\n-\n"

DSH = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(__file__).resolve().parent
with (root / "dsh-calls.log").open("a") as log:
    log.write(json.dumps({"args": sys.argv[1:], "env": dict(os.environ)}) + "\\n")
if (root / "dsh-fail").exists():
    sys.exit(3)
if sys.argv[1:] == ["--version"]:
    version = root / "dsh-version.txt"
    print(version.read_text().strip() if version.exists() else "")
    sys.exit(0)
if "--help" in sys.argv[1:]:
    help_file = root / "dsh-help.txt"
    print(help_file.read_text() if help_file.exists() else "")
    sys.exit(0)
sys.exit(4)
'''


def configure_dsh(e, binary, *, dsh_home=None, api_key_env="DSH_TEST_API_KEY"):
    config = json.loads(e["settings"].read_text())
    config.pop("cli", None)
    runner = {"kind": "dsh", "binary": str(binary)}
    if dsh_home is not None:
        runner["dsh_home"] = str(dsh_home)
    if api_key_env is not None:
        runner["api_key_env"] = api_key_env
    config["runner"] = runner
    e["settings"].write_text(json.dumps(config))


def make_dsh(e, version="0.2.2-rc.1", help_text=HELP, fail=False):
    script = e["tmp"] / "fake_dsh.py"
    script.write_text(DSH)
    script.chmod(0o755)
    home = e["tmp"] / "dsh-home"
    home.mkdir()
    (e["tmp"] / "dsh-version.txt").write_text(version + "\n")
    (e["tmp"] / "dsh-help.txt").write_text(help_text)
    flag = e["tmp"] / "dsh-fail"
    if fail:
        flag.write_text("1")
    elif flag.exists():
        flag.unlink()
    configure_dsh(e, script, dsh_home=home)
    change(e, operation="review")
    return script


def reset_calls(e):
    log = e["tmp"] / "dsh-calls.log"
    if log.exists():
        log.unlink()


def calls(e):
    log = e["tmp"] / "dsh-calls.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def test_dsh_probe_reports_version_and_capabilities_then_ok(e, monkeypatch):
    monkeypatch.setenv("PROBE_HOST_SENTINEL", "must-not-pass")
    make_dsh(e)

    data, code = invoke(e, "preflight", e["task"])

    assert data["status"] == "ok" and code == 0, data
    runner = data["runner"]
    assert runner["kind"] == "dsh"
    assert runner["detected_version"] == "0.2.2-rc.1"
    capabilities = set(runner["capabilities"])
    assert {"--json", "--permission-mode", "--approval-policy", "--session-id"} <= capabilities
    log = calls(e)
    assert [entry["args"] for entry in log] == [
        ["--version"], ["--profile", "headless", "--help"]]
    assert all("PROBE_HOST_SENTINEL" not in entry["env"] for entry in log)
    assert not (e["tmp"] / "record.json").exists()


def test_dsh_probe_failure_blocks_without_evidence_or_exception(e):
    make_dsh(e, fail=True)

    data, code = invoke(e, "preflight", e["task"])

    assert data["status"] == "BLOCKED" and code != 0, data
    assert "dsh probe failed" in json.dumps(data), data
    assert not (e["tmp"] / "record.json").exists()


def test_missing_dsh_binary_blocks_before_any_probe(e):
    make_dsh(e)
    config = json.loads(e["settings"].read_text())
    config["runner"]["binary"] = str(e["tmp"] / "absent_dsh")
    e["settings"].write_text(json.dumps(config))

    for op in ("preflight", "run"):
        data, code = invoke(e, op, e["task"])
        assert data["status"] == "BLOCKED" and code != 0, (op, data)
        text = json.dumps(data)
        assert ("runner binary not found" in text or
                "CLI missing or not executable" in text), (op, data)

    assert calls(e) == []
    assert not (e["tmp"] / "record.json").exists()


def test_dsh_implement_blocks_until_b23(e):
    make_dsh(e)
    change(e, operation="implement")

    for op in ("preflight", "run"):
        data, code = invoke(e, op, e["task"])
        assert data["status"] == "BLOCKED" and code != 0, (op, data)
        assert "dsh implement not implemented until B23" in json.dumps(data), (op, data)
        assert not (e["tmp"] / "record.json").exists()


def test_dsh_missing_required_capabilities_blocks(e):
    help_text = HELP.replace("--approval-policy <policy>\n", "").replace(
        "--session-id <id>\n", "")
    make_dsh(e, help_text=help_text)

    data, code = invoke(e, "preflight", e["task"])

    assert data["status"] == "BLOCKED" and code != 0, data
    text = json.dumps(data)
    assert "dsh capabilities insufficient" in text, data
    assert "--approval-policy" in text and "--session-id" in text, data
    assert not (e["tmp"] / "record.json").exists()


def test_dsh_missing_stdin_capability_warns_but_does_not_block(e):
    make_dsh(e, help_text=HELP.replace("-\n", ""))

    data, code = invoke(e, "preflight", e["task"])

    assert data["status"] == "ok" and code == 0, data
    assert "stdin" in json.dumps(data).lower(), data


@pytest.mark.parametrize("mutation", [
    "missing_home", "relative_home", "missing_path", "missing_key", "empty_key"])
def test_invalid_dsh_runner_config_blocks_before_cli(e, mutation):
    make_dsh(e)
    config = json.loads(e["settings"].read_text())
    runner = config["runner"]
    if mutation == "missing_home":
        runner.pop("dsh_home")
    elif mutation == "relative_home":
        runner["dsh_home"] = "dsh-home"
    elif mutation == "missing_path":
        runner["dsh_home"] = str(e["tmp"] / "absent-home")
    elif mutation == "missing_key":
        runner.pop("api_key_env")
    elif mutation == "empty_key":
        runner["api_key_env"] = ""
    e["settings"].write_text(json.dumps(config))
    reset_calls(e)

    data, code = invoke(e, "preflight", e["task"])

    assert data["status"] == "BLOCKED" and code != 0, data
    assert "invalid dsh runner config" in json.dumps(data), data
    assert calls(e) == []
    assert not (e["tmp"] / "record.json").exists()
