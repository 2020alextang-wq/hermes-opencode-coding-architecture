"""B17 runner adapter slot: settings.runner.kind resolution and honest rejection."""
import json

import pytest

from test_control import e, invoke, ledger


def settings(e):
    return json.loads(e["settings"].read_text())


def save_settings(e, data):
    e["settings"].write_text(json.dumps(data))


def set_kind(e, kind):
    config = settings(e)
    runner = config.get("runner")
    if not isinstance(runner, dict):
        runner = {}
    runner["kind"] = kind
    config["runner"] = runner
    save_settings(e, config)


def test_default_kind_is_opencode_for_preflight_and_run_ledger(e):
    checked, code = invoke(e, "preflight", e["task"])
    assert code == 0 and checked["status"] == "ok"
    assert checked["runner"]["kind"] == "opencode"
    assert not (e["tmp"] / "record.json").exists()

    result, code = invoke(e, "run", e["task"])
    assert code == 0 and result["status"] == "READY_FOR_REVIEW"
    assert ledger(e, result["run_id"])["runner_config"]["kind"] == "opencode"


def test_explicit_opencode_kind_matches_default(e):
    set_kind(e, "opencode")
    checked, code = invoke(e, "preflight", e["task"])
    assert code == 0 and checked["status"] == "ok"
    assert checked["runner"]["kind"] == "opencode"

    result, code = invoke(e, "run", e["task"])
    assert code == 0 and result["status"] == "READY_FOR_REVIEW"
    assert ledger(e, result["run_id"])["runner_config"]["kind"] == "opencode"


def test_dsh_kind_blocks_before_any_cli_start(e):
    dsh = e["tmp"] / "fake_dsh"
    dsh.write_text("#!/usr/bin/env python3\nprint('0.2.2-rc.1')\n")
    dsh.chmod(0o755)
    config = settings(e)
    config["cli"] = str(dsh)
    save_settings(e, config)
    set_kind(e, "dsh")
    for op in ("preflight", "run"):
        data, code = invoke(e, op, e["task"])
        assert data["status"] == "BLOCKED" and code != 0, (op, data)
        assert "dsh capabilities insufficient" in json.dumps(data), (op, data)
        assert not (e["tmp"] / "record.json").exists(), op


@pytest.mark.parametrize("kind", ["bogus", "", 123])
def test_unknown_kind_blocks_before_any_cli_start(e, kind):
    set_kind(e, kind)
    for op in ("preflight", "run"):
        data, code = invoke(e, op, e["task"])
        assert data["status"] == "BLOCKED" and code != 0, (op, kind, data)
        assert "unknown runner kind" in json.dumps(data), (op, kind, data)
        assert not (e["tmp"] / "record.json").exists(), (op, kind)
