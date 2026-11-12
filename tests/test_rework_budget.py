"""Rework and token-budget semantics contract for B7/F9."""
import json

import pytest

from test_control import FAKE, change, e, invoke, ledger, mode


def prior_run(e, n, **fields):
    rid = f"{n:012x}"
    folder = e["tmp"] / "state" / "runs" / rid
    folder.mkdir(parents=True)
    data = {
        "run_id": rid,
        "status": "FAILED",
        "project": str(e["project"]),
        "allowed_files": ["src/main.py"],
        "rework_count": 0,
    }
    data.update(fields)
    (folder / "ledger.json").write_text(json.dumps(data))
    return rid


def quality_failure(e, n, count):
    return prior_run(e, n, status="FAILED", quality_rework_count=count,
                     rework_count=count, quality_failure=True,
                     failure_category="candidate")


@pytest.mark.smoke
def test_two_quality_failures_allow_ready_run_and_inherit_quality_count(e):
    quality_failure(e, 1, 1)
    quality_failure(e, 2, 2)
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
    info = ledger(e, data["run_id"])
    assert info["quality_rework_count"] == 2
    assert info["rework_count"] == 2
    assert info["quality_failure"] is False
    assert info["failure_category"] is None


def test_ready_runs_inherit_prior_max_without_increment(e):
    for n, count in ((1, 0), (2, 1), (3, 2)):
        prior_run(e, n, status="READY_FOR_REVIEW", quality_rework_count=count,
                  rework_count=count, quality_failure=False, failure_category=None)
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
    info = ledger(e, data["run_id"])
    assert info["quality_rework_count"] == 2
    assert info["rework_count"] == 2
    assert info["quality_failure"] is False


def test_three_ready_zero_ledgers_do_not_reach_quality_gate(e):
    for n in (1, 2, 3):
        prior_run(e, n, status="READY_FOR_REVIEW", quality_rework_count=0,
                  rework_count=0, quality_failure=False, failure_category=None)
    checked, code = invoke(e, "preflight", e["task"])
    assert checked["status"] == "ok" and code == 0
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
    info = ledger(e, data["run_id"])
    assert info["quality_rework_count"] == 0
    assert info["rework_count"] == 0


def test_legacy_ready_ledger_does_not_contribute_quality_count(e):
    prior_run(e, 1, status="READY_FOR_REVIEW", rework_count=2)
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
    info = ledger(e, data["run_id"])
    assert info["quality_rework_count"] == 0
    assert info["rework_count"] == 0
    assert info["quality_failure"] is False


def test_three_legacy_ready_ledgers_do_not_trigger_quality_gate(e):
    for n in (1, 2, 3):
        prior_run(e, n, status="READY_FOR_REVIEW", rework_count=3)
    checked, code = invoke(e, "preflight", e["task"])
    assert checked["status"] == "ok" and code == 0
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "READY_FOR_REVIEW" and code == 0


@pytest.mark.smoke
def test_three_quality_failures_block_before_cli(e):
    for n in (1, 2, 3):
        quality_failure(e, n, 3)
    for op in ("preflight", "run"):
        data, code = invoke(e, op, e["task"])
        assert data["status"] == "BLOCKED" and code != 0
        text = json.dumps(data, ensure_ascii=False)
        assert "quality" in text.lower()
        assert "allowed_files" not in text
    assert not (e["tmp"] / "record.json").exists()


@pytest.mark.parametrize("status", ["TIMEOUT", "BLOCKED"])
def test_three_non_quality_terminals_do_not_trigger_quality_gate(e, status):
    for n in (1, 2, 3):
        prior_run(e, n, status=status, rework_count=3)
    checked, code = invoke(e, "preflight", e["task"])
    assert checked["status"] == "ok" and code == 0
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
    assert (e["tmp"] / "record.json").exists()


def test_legacy_failed_rework_count_blocks_with_quality_reason(e):
    prior_run(e, 1, status="FAILED", rework_count=3)
    data, code = invoke(e, "preflight", e["task"])
    assert data["status"] == "BLOCKED" and code != 0
    text = json.dumps(data, ensure_ascii=False)
    assert "rework_count" in text
    assert "quality" in text.lower()
    assert "allowed_files" not in text
    run, run_code = invoke(e, "run", e["task"])
    assert run["status"] == "BLOCKED" and run_code != 0
    assert not (e["tmp"] / "record.json").exists()


def test_quality_failure_run_records_category_and_increment(e):
    quality_failure(e, 1, 2)
    mode(e, "tracked")
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "FAILED" and code != 0
    info = ledger(e, data["run_id"])
    assert info["quality_rework_count"] == 3
    assert info["rework_count"] == 3
    assert info["quality_failure"] is True
    assert isinstance(info["failure_category"], str) and info["failure_category"]


def test_timeout_inherits_quality_count_without_increment(e):
    quality_failure(e, 1, 2)
    mode(e, "silent")
    change(e, timeout_seconds=1)
    data, code = invoke(e, "run", e["task"], timeout=5)
    assert data["status"] == "TIMEOUT" and code != 0
    info = ledger(e, data["run_id"])
    assert info["quality_rework_count"] == 2
    assert info["rework_count"] == 2
    assert info["quality_failure"] is False
    assert info["failure_category"] is None


def test_parent_run_bypasses_quality_gate_and_records_inherited_count(e):
    parent = prior_run(e, 1, status="READY_FOR_REVIEW", quality_rework_count=3,
                       rework_count=3, quality_failure=False, failure_category=None,
                       model="test-provider/test-model", session_id="ses_test",
                       operation="implement")
    change(e, parent_run=parent, session_id="ses_test")
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
    info = ledger(e, data["run_id"])
    assert info["parent_run"] == parent
    assert info["quality_rework_count"] == 3
    assert info["rework_count"] == 3


def test_token_equal_to_limit_stops_as_budget_stop_with_complete_budget(e):
    mode(e, "budget")
    change(e, max_tokens=190)
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "BUDGET_STOP" and code != 0
    info = ledger(e, data["run_id"])
    assert info["token_total"] == 190
    assert info["budget_incomplete"] is False
    assert info["budgets"] == {"token_total": 190, "token_limit": 190,
                               "budget_incomplete": False}


def test_missing_step_total_marks_budget_incomplete_without_counting(e):
    patched = FAKE.replace(
        '    event = {"type": "step_finish"',
        '    if mode == "budget_missing_total":\n'
        '        tokens = {k: v for k, v in tokens.items() if k != "total"}\n'
        '    event = {"type": "step_finish"',
        1,
    )
    assert patched != FAKE
    cli = e["tmp"] / "fake_cli.py"
    cli.write_text(patched)
    cli.chmod(0o755)
    mode(e, "budget_missing_total")
    data, code = invoke(e, "run", e["task"])
    info = ledger(e, data["run_id"])
    assert info["token_total"] == 0
    assert info["budget_incomplete"] is True
    assert info["budgets"] == {"token_total": 0, "token_limit": 100,
                               "budget_incomplete": True}
    assert data["status"] != "BUDGET_STOP"
