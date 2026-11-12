"""Executor skill declaration and hash contract for B12-T."""
import hashlib
import json
import shutil

import pytest

from test_control import CONTROL, e, invoke, ledger

SKILLS = CONTROL.parent / "skills"


def set_skills(e, skills):
    settings = json.loads(e["settings"].read_text())
    if skills is None:
        settings.pop("skills", None)
    else:
        settings["skills"] = skills
    e["settings"].write_text(json.dumps(settings))


def expected(name):
    path = (SKILLS / name / "SKILL.md").resolve()
    return {"name": name, "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def prompt_of(e):
    return json.loads((e["tmp"] / "record.json").read_text())["prompt"]


@pytest.fixture
def temp_skill():
    folder = SKILLS / "b12t-skill-probe"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    path = folder / "SKILL.md"
    path.write_text("synthetic skill body sentinel B12T\n")
    try:
        yield folder, path
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@pytest.mark.smoke
def test_preflight_echoes_single_skill_with_real_hash(e):
    set_skills(e, ["workspace-execution"])
    data, code = invoke(e, "preflight", e["task"])
    assert code == 0 and data["status"] == "ok"
    assert data["skills"] == [expected("workspace-execution")]


def test_preflight_sorts_multiple_skills_by_name(e):
    names = ["workspace-execution", "test-driven-development"]
    set_skills(e, names)
    data, code = invoke(e, "preflight", e["task"])
    assert code == 0 and data["status"] == "ok"
    assert [entry["name"] for entry in data["skills"]] == sorted(names)
    assert data["skills"] == [expected(name) for name in sorted(names)]


def test_run_ledger_records_declared_skills(e):
    set_skills(e, ["workspace-execution"])
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
    assert ledger(e, data["run_id"])["skills_declared"] == [expected("workspace-execution")]


def test_prompt_lists_skills_with_absolute_paths_and_read_first_rule(e):
    names = ["workspace-execution", "test-driven-development"]
    set_skills(e, names)
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
    prompt = prompt_of(e)
    assert "Skills" in prompt
    for name in names:
        assert f"- {name}: {expected(name)['path']}" in prompt
    assert "read the skill before acting" in prompt


def test_prompt_carries_path_not_skill_body(e, temp_skill):
    folder, path = temp_skill
    name = folder.name
    body = path.read_text().strip()
    set_skills(e, [name])
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
    prompt = prompt_of(e)
    assert f"- {name}: {path.resolve()}" in prompt
    assert body not in prompt


def test_hash_tracks_skill_file_content_change(e, temp_skill):
    folder, path = temp_skill
    name = folder.name
    set_skills(e, [name])
    first = invoke(e, "preflight", e["task"])[0]
    before = first["skills"][0]["sha256"]
    assert before == hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text("changed skill bytes B12T\n")
    second = invoke(e, "preflight", e["task"])[0]
    after = second["skills"][0]["sha256"]
    assert after == hashlib.sha256(path.read_bytes()).hexdigest()
    assert after != before


@pytest.mark.smoke
def test_unknown_skill_blocks_before_cli(e):
    name = "b12t-no-such-skill"
    set_skills(e, [name])
    for op in ("preflight", "run"):
        data, code = invoke(e, op, e["task"])
        assert data["status"] == "BLOCKED" and code != 0
        assert f"unknown skill: {name}" in json.dumps(data, ensure_ascii=False)
    assert not (e["tmp"] / "record.json").exists()


def test_directory_without_skill_file_is_unknown(e):
    folder = SKILLS / "b12t-dir-only"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    try:
        set_skills(e, [folder.name])
        data, code = invoke(e, "run", e["task"])
        assert data["status"] == "BLOCKED" and code != 0
        assert f"unknown skill: {folder.name}" in json.dumps(data, ensure_ascii=False)
        assert not (e["tmp"] / "record.json").exists()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@pytest.mark.parametrize("bad", ["../escape", "Bad/Name", "", "/abs/path", ".."])
def test_invalid_skill_shape_blocks_before_cli(e, bad):
    set_skills(e, [bad])
    for op in ("preflight", "run"):
        data, code = invoke(e, op, e["task"])
        assert data["status"] == "BLOCKED" and code != 0
        assert "invalid skill" in json.dumps(data, ensure_ascii=False)
    assert not (e["tmp"] / "record.json").exists()


def test_default_skills_are_empty_and_prompt_has_no_section(e):
    checked, code = invoke(e, "preflight", e["task"])
    assert code == 0 and checked["status"] == "ok"
    assert checked["skills"] == []
    data, code = invoke(e, "run", e["task"])
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
    assert ledger(e, data["run_id"])["skills_declared"] == []
    prompt = prompt_of(e)
    assert "Skills" not in prompt
    assert "loaded skills" not in prompt
