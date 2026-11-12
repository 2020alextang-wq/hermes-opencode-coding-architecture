"""Offline tests of the Hermes post_api_request persistence boundary."""
import importlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
PY = sys.executable
TOKENS = {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 3,
          "cache_write_tokens": 1, "reasoning_tokens": 2}


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    db = tmp_path / "usage" / "calls.sqlite"
    monkeypatch.setenv("CODING_HERMES_USAGE_DB", str(db))
    return importlib.import_module("hermes_usage"), db


def record(plugin, **overrides):
    defaults = {"task_id": "turn-a", "session_id": "session-a", "provider": "example-provider",
                "model": "example-model", "api_call_count": 1,
                "response": {"id": "resp-1"}, "usage": TOKENS}
    defaults.update(overrides)
    return plugin.on_post_api_request(**defaults)


def test_registers_only_post_api_request(plugin):
    module, _ = plugin
    class Context:
        def __init__(self):
            self.hooks = []
        def register_hook(self, name, callback):
            self.hooks.append((name, callback))
    ctx = Context()
    module.register(ctx)
    assert ctx.hooks == [("post_api_request", module.on_post_api_request)]
    manifest = (HERE / "hermes_usage/plugin.yaml").read_text()
    assert "name: hermes-coding-usage" in manifest
    assert 'version: "1.0.0"' in manifest
    assert "hooks: [post_api_request]" in manifest


def test_duplicate_hook_is_not_counted_twice_and_cache_is_distinct(plugin):
    module, _ = plugin
    assert record(module)["status"] == "recorded"
    assert record(module)["status"] == "duplicate"
    summary = module.summarize("turn-a", "session-a")
    assert summary["usage"] == {"input": 10, "output": 5, "cache_read": 3,
                                "cache_write": 1, "reasoning": 2, "total": 19, "request_count": 1}
    assert summary["status"] == "complete"


@pytest.mark.parametrize("bad_usage", [None, {}, {**TOKENS, "input_tokens": -1},
                                            {**TOKENS, "reasoning_tokens": True},
                                            {**TOKENS, "cache_read_tokens": "3"}])
def test_missing_or_invalid_usage_marks_incomplete(plugin, bad_usage):
    module, _ = plugin
    assert record(module, usage=bad_usage)["status"] == "incomplete"
    summary = module.summarize("turn-a", "session-a")
    assert summary["status"] == "incomplete/not_attributable"
    assert summary["usage"] is None and summary["incomplete_count"] == 1


def test_no_task_or_session_is_unattributable(plugin):
    module, db = plugin
    assert record(module, task_id="")["status"] == "incomplete/not_attributable"
    assert record(module, session_id=None)["status"] == "incomplete/not_attributable"
    assert not db.exists()


def test_isolates_tasks_and_sessions_latest_filters_session(plugin):
    module, _ = plugin
    record(module)
    record(module, task_id="turn-b", session_id="session-a", response={"id": "resp-b"})
    record(module, task_id="turn-c", session_id="session-b", response={"id": "resp-c"})
    assert module.latest(session_id="session-a")["task_id"] == "turn-b"
    assert module.latest()["task_id"] == "turn-c"
    assert module.summarize("turn-a", "session-a")["usage"]["total"] == 19
    assert module.summarize("turn-a", "session-b")["usage"] is None


def test_multithreaded_hooks_store_each_request_once(plugin):
    module, _ = plugin
    def write(i):
        return record(module, api_call_count=i, response={"id": f"resp-{i}"})
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(write, range(1, 17)))
    assert all(item["status"] == "recorded" for item in results), results
    assert module.summarize("turn-a", "session-a")["usage"]["total"] == 304


def test_unwritable_database_returns_visible_error_without_raising(plugin, monkeypatch):
    module, _ = plugin
    forbidden = Path(__file__).resolve() / "not-a-database.sqlite"
    monkeypatch.setenv("CODING_HERMES_USAGE_DB", str(forbidden))
    result = record(module)
    assert result["status"] == "recording_error" and result["error"]
    assert module.latest()["status"] == "recording_error"


def test_unexpected_response_property_error_never_interrupts_chat(plugin):
    module, _ = plugin
    class FaultyResponse:
        @property
        def id(self):
            raise RuntimeError("private response text")
    result = record(module, response=FaultyResponse())
    assert result["status"] == "recording_error"
    assert "private response text" not in str(result)


def test_only_allowlisted_fields_saved_not_prompt_or_key(plugin):
    module, db = plugin
    record(module, prompt="INJECTED_PROMPT_MARKER", api_key="INJECTED_SECRET_MARKER",
           base_url="INJECTED_BASE_URL_MARKER", response={"id": "resp-1", "text": "PRIVATE_BODY_MARKER"})
    with sqlite3.connect(db) as conn:
        rows = repr(conn.execute("SELECT * FROM api_requests").fetchall())
        columns = {x[1] for x in conn.execute("PRAGMA table_info(api_requests)")}
    assert {"session_id", "task_id", "api_call_count", "response_id"} <= columns
    assert not any(s in rows for s in ("INJECTED_PROMPT_MARKER", "INJECTED_SECRET_MARKER",
                                         "INJECTED_BASE_URL_MARKER", "PRIVATE_BODY_MARKER"))
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700


def test_cli_latest_and_task_are_json_without_model(plugin):
    module, db = plugin
    record(module)
    script = HERE / "hermes_usage/__init__.py"
    env = {**os.environ, "CODING_HERMES_USAGE_DB": str(db)}
    for args in (["--latest"], ["--task", "turn-a", "--session", "session-a"]):
        result = subprocess.run([PY, str(script), *args], capture_output=True, text=True,
                                timeout=3, env=env)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["task_id"] == "turn-a" and data["session_id"] == "session-a"
