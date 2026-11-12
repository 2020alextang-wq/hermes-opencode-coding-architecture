"""B5 runner configuration single-source resolution and CLI capability probes."""
import json
from pathlib import Path

import pytest

from test_control import change, e, invoke, ledger

MODEL = "test-provider/test-model"
FLAGS = {"--dir", "--agent", "--format"}


def settings(e):
    return json.loads(e["settings"].read_text())


def save_settings(e, data):
    e["settings"].write_text(json.dumps(data))


def without(e, *keys):
    data = settings(e)
    for key in keys:
        data.pop(key, None)
    save_settings(e, data)
    return data


def fake_cli(e):
    return Path(settings(e)["cli"])


def probe_files(e, version=None, help_text=None):
    if version is not None:
        (e["tmp"] / "version.txt").write_text(version)
    if help_text is not None:
        (e["tmp"] / "help.txt").write_text(help_text)


def assert_blocked(e, *needles):
    for op in ("preflight", "run"):
        data, code = invoke(e, op, e["task"])
        assert data["status"] == "BLOCKED" and code != 0, (op, data)
        text = json.dumps(data)
        for needle in needles:
            assert needle in text, (op, needle, data)
        assert not (e["tmp"] / "record.json").exists()


def test_legacy_config_preflight_and_run_share_runner_model(e):
    checked, code = invoke(e, "preflight", e["task"])
    assert code == 0 and checked["status"] == "ok"
    runner = checked["runner"]
    assert runner["model"] == MODEL
    assert runner["model_source"] == "runner_model"
    assert runner["detected_version"] == "1.2.3"
    assert runner["declared_version"] is None
    assert Path(runner["binary"]).resolve() == fake_cli(e).resolve()
    assert FLAGS <= set(runner["capabilities"])
    assert "provider" in runner
    assert not (e["tmp"] / "record.json").exists()

    result, code = invoke(e, "run", e["task"])
    assert code == 0 and result["status"] == "READY_FOR_REVIEW"
    info = ledger(e, result["run_id"])
    assert info["runner_config"]["model"] == MODEL
    assert info["runner_config"]["model_source"] == "runner_model"
    assert info["runner_config"]["detected_version"] == "1.2.3"
    assert Path(info["runner_config"]["binary"]).resolve() == fake_cli(e).resolve()


def test_runner_object_alone_resolves_binary_model_and_version(e):
    binary = fake_cli(e)
    config = without(e, "cli", "runner_model")
    config["runner"] = {"binary": str(binary), "version": "1.2.3",
                        "provider": "test-provider", "model": MODEL}
    save_settings(e, config)

    checked, code = invoke(e, "preflight", e["task"])
    assert code == 0 and checked["status"] == "ok"
    runner = checked["runner"]
    assert Path(runner["binary"]).resolve() == binary.resolve()
    assert runner["model"] == MODEL
    assert runner["provider"] == "test-provider"
    assert runner["declared_version"] == "1.2.3"
    assert runner["detected_version"] == "1.2.3"
    assert runner["model_source"] == "runner"
    assert FLAGS <= set(runner["capabilities"])
    assert not (e["tmp"] / "record.json").exists()


def test_runner_binary_conflict_blocks_before_executor(e):
    other = e["tmp"] / "other_cli.py"
    other.write_text(fake_cli(e).read_text())
    other.chmod(0o755)
    config = settings(e)
    config["runner"] = {"binary": str(other)}
    save_settings(e, config)
    assert_blocked(e, "runner config conflict: binary")


def test_runner_model_conflict_blocks_before_executor(e):
    config = settings(e)
    config["runner"] = {"model": "other-provider/other-model"}
    save_settings(e, config)
    assert_blocked(e, "runner config conflict: model")


def test_runner_provider_model_mismatch_blocks_before_executor(e):
    config = settings(e)
    config["runner"] = {"provider": "acme", "model": MODEL}
    save_settings(e, config)
    assert_blocked(e, "runner provider/model mismatch")


@pytest.mark.parametrize("version", [123, ""])
def test_invalid_runner_version_blocks_before_executor(e, version):
    config = settings(e)
    config["runner"] = {"version": version}
    save_settings(e, config)
    assert_blocked(e, "version")


def test_missing_runner_binary_and_legacy_cli_keep_original_reason(e):
    without(e, "cli")
    assert_blocked(e, "CLI missing or not executable")


def test_declared_version_mismatch_blocks_with_both_versions(e):
    config = settings(e)
    config["runner"] = {"version": "9.9.9"}
    save_settings(e, config)
    assert_blocked(e, "9.9.9", "1.2.3")


def test_empty_version_output_with_zero_exit_is_probe_failure(e):
    probe_files(e, version="")
    assert_blocked(e, "cli probe failed")


def test_help_missing_required_flag_blocks_and_lists_it(e):
    probe_files(e, help_text="--agent\n--format\n")
    assert_blocked(e, "--dir")


def test_run_uses_one_resolved_model_for_command_config_and_ledger(e):
    config = without(e, "runner_model")
    config["runner"] = {"binary": str(fake_cli(e)), "model": MODEL}
    save_settings(e, config)

    result, code = invoke(e, "run", e["task"])
    assert code == 0 and result["status"] == "READY_FOR_REVIEW"
    record = json.loads((e["tmp"] / "record.json").read_text())
    assert record["args"][record["args"].index("--model") + 1] == MODEL
    assert json.loads(record["config"])["small_model"] == MODEL
    info = ledger(e, result["run_id"])
    assert info["runner_config"]["model"] == MODEL
    assert info["runner_config"]["model_source"] == "runner"


def test_task_model_override_wins_and_is_reported(e):
    config = settings(e)
    config["runner_model"] = "other-provider/other-model"
    save_settings(e, config)
    change(e, model=MODEL)

    checked, code = invoke(e, "preflight", e["task"])
    assert code == 0 and checked["status"] == "ok"
    assert checked["model"] == MODEL
    assert checked["runner"]["model"] == MODEL
    assert checked["runner"]["model_source"] == "task"

    result, code = invoke(e, "run", e["task"])
    assert code == 0 and result["status"] == "READY_FOR_REVIEW"
    record = json.loads((e["tmp"] / "record.json").read_text())
    assert record["args"][record["args"].index("--model") + 1] == MODEL
    assert json.loads(record["config"])["small_model"] == MODEL
    info = ledger(e, result["run_id"])
    assert info["runner_config"]["model"] == MODEL
    assert info["runner_config"]["model_source"] == "task"
