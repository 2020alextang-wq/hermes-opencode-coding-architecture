"""Structured candidate envelope contract for the controller (B1/F2)."""
import json

import pytest

from test_control import change, e, invoke, ledger, mode


def run(e):
    return invoke(e, "run", e["task"])


@pytest.mark.smoke
def test_operation_defaults_to_implement(e):
    result, code = run(e)
    assert code == 0 and result["status"] == "READY_FOR_REVIEW"
    assert ledger(e, result["run_id"])["operation"] == "implement"


def test_invalid_operation_blocks_before_cli(e):
    change(e, operation="ship")
    for op in ("preflight", "run"):
        data, code = invoke(e, op, e["task"])
        assert data["status"] == "BLOCKED" and code != 0
    assert not (e["tmp"] / "record.json").exists()


def test_review_requires_candidate_and_denies_all_edits(e):
    change(e, operation="review")
    mode(e, "review_clean")
    result, code = run(e)
    assert code == 0 and result["status"] == "READY_FOR_REVIEW"
    record = json.loads((e["tmp"] / "record.json").read_text())
    edit = json.loads(record["config"])["permission"]["edit"]
    assert edit == {"*": "deny"}
    assert ledger(e, result["run_id"])["operation"] == "review"


def test_review_rejects_any_covered_change(e):
    change(e, operation="review")
    mode(e, "review_write")
    result, code = run(e)
    assert result["status"] == "FAILED" and code != 0
    assert "review must not modify" in ledger(e, result["run_id"])["error"]


def test_implement_requires_a_real_allowed_file_change(e):
    mode(e, "candidate_no_change")
    result, code = run(e)
    assert result["status"] == "FAILED" and code != 0
    assert "requires actual allowed-file change" in ledger(e, result["run_id"])["error"]


@pytest.mark.smoke
def test_candidate_blocked_is_never_ready(e):
    mode(e, "candidate_blocked")
    result, code = run(e)
    assert result["status"] == "BLOCKED" and code != 0
    assert ledger(e, result["run_id"])["status"] == "BLOCKED"


@pytest.mark.parametrize("declared,allowed", [
    (["src/main.py", "src/main.py"], None),
    (["src/missing.py"], None),
    (["src/other.py"], ["src/main.py", "src/other.py"]),
])
def test_declared_changed_files_must_be_exact(e, declared, allowed):
    (e["tmp"] / "changed_files.json").write_text(json.dumps(declared))
    if allowed:
        change(e, allowed_files=allowed)
    result, code = run(e)
    assert result["status"] == "FAILED" and code != 0
    assert "changed" in ledger(e, result["run_id"])["error"]


def test_candidate_changed_files_type_is_strict(e):
    (e["tmp"] / "changed_files.json").write_text(json.dumps("src/main.py"))
    result, code = run(e)
    assert result["status"] == "FAILED" and code != 0
    assert "changed_files" in ledger(e, result["run_id"])["error"]


@pytest.mark.parametrize("name", [
    "candidate_body_ready",
    "candidate_embedded",
    "candidate_fenced",
    "candidate_fenced_unclosed",
    "candidate_trailing",
    "candidate_midstream",
    "candidate_two",
    "candidate_duplicate_line",
    "candidate_bad_json",
])
def test_candidate_protocol_negatives(e, name):
    mode(e, name)
    result, code = run(e)
    assert result["status"] == "FAILED" and code != 0
    assert "candidate" in ledger(e, result["run_id"])["error"].lower()


@pytest.mark.smoke
def test_parent_candidate_does_not_upgrade_the_new_turn(e):
    first, _ = run(e)
    assert first["status"] == "READY_FOR_REVIEW"
    mode(e, "candidate_missing")
    change(e, parent_run=first["run_id"], session_id="ses_test")
    second, code = run(e)
    assert second["status"] == "FAILED" and code != 0
    assert "candidate" in ledger(e, second["run_id"])["error"].lower()


def test_parent_operation_must_match(e):
    first, _ = run(e)
    change(e, parent_run=first["run_id"], session_id="ses_test", operation="review")
    data, code = run(e)
    assert data["status"] == "BLOCKED" and code != 0
    assert "operation" in json.dumps(data)


def test_legacy_parent_ledger_without_operation_is_implement(e):
    first, _ = run(e)
    path = e["tmp"] / "state/runs" / first["run_id"] / "ledger.json"
    info = json.loads(path.read_text())
    info.pop("operation")
    path.write_text(json.dumps(info))
    change(e, parent_run=first["run_id"], session_id="ses_test")
    second, code = run(e)
    assert code == 0 and second["status"] == "READY_FOR_REVIEW"


@pytest.mark.parametrize("name,status", [("error_with_candidate", "FAILED"),
                                         ("exit_nonzero", "FAILED")])
def test_failure_event_or_nonzero_exit_is_not_overridden(e, name, status):
    mode(e, name)
    result, code = run(e)
    assert result["status"] == status and code != 0


@pytest.mark.parametrize("value,reason", [
    ("tests", "must be text list"),
    ([1], "must be text list"),
    (["  "], "must be text list"),
    (["ok", None], "must be text list"),
])
def test_unverified_list_is_strictly_typed(e, value, reason):
    (e["tmp"] / "unverified.json").write_text(json.dumps(value))
    result, code = run(e)
    assert result["status"] == "FAILED" and code != 0
    assert reason in ledger(e, result["run_id"])["error"]


def test_empty_unverified_is_allowed_but_real_tests_still_gate_ready(e):
    (e["tmp"] / "unverified.json").write_text(json.dumps([]))
    result, code = run(e)
    assert code == 0 and result["status"] == "READY_FOR_REVIEW"
    evidence = json.loads((e["tmp"] / "state/runs" / result["run_id"]
                           / "test_evidence.json").read_text())
    assert evidence["status"] == "passed"


def test_prompt_declares_frozen_candidate_protocol(e):
    result, code = run(e)
    assert code == 0 and result["status"] == "READY_FOR_REVIEW"
    prompt = json.loads((e["tmp"] / "record.json").read_text())["prompt"]
    assert "operation is implement" in prompt
    assert '"operation":"implement"' in prompt
    assert "CODING_CONTROL_CANDIDATE" in prompt
    assert '"changed_files"' in prompt and '"unverified"' in prompt
    assert "final non-empty text line" in prompt
    assert "exactly one line" in prompt
    assert "must match the real diff exactly" in prompt
    assert "do not edit any file" in prompt
    assert "BLOCKED" in prompt


def test_review_prompt_states_read_only(e):
    change(e, operation="review")
    mode(e, "review_clean")
    result, code = run(e)
    assert code == 0 and result["status"] == "READY_FOR_REVIEW"
    prompt = json.loads((e["tmp"] / "record.json").read_text())["prompt"]
    assert "operation is review" in prompt
    assert '"operation":"review"' in prompt
    assert "do not edit any file" in prompt


def test_tilde_fence_unclosed_is_not_a_valid_candidate(e):
    mode(e, "candidate_tilde_fenced_unclosed")
    result, code = run(e)
    assert result["status"] == "FAILED" and code != 0
    assert "code block" in ledger(e, result["run_id"])["error"]


@pytest.mark.smoke
def test_tilde_fence_closed_then_valid_trailing_candidate_is_accepted(e):
    mode(e, "candidate_tilde_fenced_closed_then_valid")
    result, code = run(e)
    assert code == 0 and result["status"] == "READY_FOR_REVIEW"


@pytest.mark.parametrize("name", ["candidate_fence_length_mismatch",
                                  "candidate_fence_info_close"])
def test_fence_closing_requires_matching_type_and_length(e, name):
    mode(e, name)
    result, code = run(e)
    assert result["status"] == "FAILED" and code != 0
    assert "code block" in ledger(e, result["run_id"])["error"]

