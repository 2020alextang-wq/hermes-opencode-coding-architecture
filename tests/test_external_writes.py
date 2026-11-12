"""B21-T: external write monitoring through settings.watch_paths."""
import json
from pathlib import Path

import pytest

from test_control import e, invoke, ledger, mode


def set_watch(e, value):
    settings = json.loads(e["settings"].read_text())
    settings["watch_paths"] = value
    e["settings"].write_text(json.dumps(settings))


def watched_dir(e):
    watch = (e["tmp"] / "watched").resolve()
    watch.mkdir(parents=True, exist_ok=True)
    return watch


def arm_fake(e, watch):
    (e["tmp"] / "watch_target").write_text(str(watch))


def assert_recorded(e, data, watch):
    touched = str(watch / "touched.txt")
    info = ledger(e, data["run_id"])
    assert data["status"] == "FAILED"
    assert "external-write:" in (info.get("error") or "")
    assert touched in (info.get("error") or "")
    entries = info["external_writes"]
    assert entries == sorted(entries)
    assert touched in entries
    return info


def test_default_watch_paths_disabled_and_ready(e):
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
    info = ledger(e, data["run_id"])
    assert info.get("external_writes") in (None, [])


def test_watched_directory_without_changes_stays_ready(e):
    watch = watched_dir(e)
    (watch / "stable.txt").write_text("keep\n")
    set_watch(e, [str(watch)])
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
    info = ledger(e, data["run_id"])
    assert info.get("external_writes") in (None, [])


def test_external_new_file_fails_and_is_recorded(e):
    watch = watched_dir(e)
    set_watch(e, [str(watch)])
    arm_fake(e, watch)
    mode(e, "outside_write")
    data, code = invoke(e, "run", e["task"])
    assert code != 0
    assert_recorded(e, data, watch)


def test_external_modified_file_fails_and_is_recorded(e):
    watch = watched_dir(e)
    (watch / "touched.txt").write_text("before\n")
    set_watch(e, [str(watch)])
    arm_fake(e, watch)
    mode(e, "outside_write")
    data, code = invoke(e, "run", e["task"])
    assert code != 0
    assert_recorded(e, data, watch)


def test_external_deleted_file_fails_and_is_recorded(e):
    watch = watched_dir(e)
    (watch / "touched.txt").write_text("before\n")
    (e["tmp"] / "outside_delete_marker").write_text("delete\n")
    set_watch(e, [str(watch)])
    arm_fake(e, watch)
    mode(e, "outside_write")
    data, code = invoke(e, "run", e["task"])
    assert code != 0
    assert_recorded(e, data, watch)


@pytest.mark.parametrize("kind", ["string", "object", "relative", "missing"])
def test_invalid_watch_paths_block_preflight(e, kind):
    if kind == "string":
        value = "watched"
    elif kind == "object":
        value = {"path": "watched"}
    elif kind == "relative":
        value = "relative/watched"
    else:
        value = str(e["tmp"] / "does-not-exist")
    set_watch(e, value)
    data, code = invoke(e, "preflight", e["task"])
    assert data["status"] == "BLOCKED" and code != 0
    assert "invalid watch_paths" in json.dumps(data)
