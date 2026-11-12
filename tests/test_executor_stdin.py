"""Executor stdin must not inherit the controller stdin (B9)."""
import json
import os
import subprocess

from test_control import CONTROL, FAKE, PY, change, e, mode


def install_stdin_fake(e):
    patched = FAKE.replace(
        '(root / "record.json").write_text(json.dumps(record))',
        'if mode == "stdin_probe":\n'
        '    try:\n'
        '        record["stdin"] = os.readlink("/dev/fd/0")\n'
        '    except OSError:\n'
        '        record["stdin"] = None\n'
        '    import select\n'
        '    ready, _, _ = select.select([0], [], [], 0.2)\n'
        '    record["stdin_eof"] = bool(ready and os.read(0, 1) == b"")\n'
        'if mode == "stdin_wait":\n'
        '    import select\n'
        '    ready, _, _ = select.select([0], [], [], 0.5)\n'
        '    eof = bool(ready and os.read(0, 1) == b"")\n'
        '    if not eof:\n'
        '        (root / "stdin_wait_marker").write_text("stdin still open")\n'
        '        while True:\n'
        '            time.sleep(1)\n'
        '(root / "record.json").write_text(json.dumps(record))',
        1,
    )
    patched = patched.replace(
        '"exit_nonzero"):\n    bump()',
        '"exit_nonzero", "stdin_probe", "stdin_wait"):\n    bump()',
        1,
    )
    patched = patched.replace(
        'elif mode in ("normal", "duplicate", "content_clean", "error_with_candidate", "exit_nonzero"):\n'
        '            texts = [candidate_text()]',
        'elif mode in ("normal", "duplicate", "content_clean", "error_with_candidate", "exit_nonzero",\n'
        '                      "stdin_probe", "stdin_wait"):\n'
        '            texts = [candidate_text()]',
        1,
    )
    assert patched.count('"stdin_probe"') >= 3
    cli = e["tmp"] / "fake_cli.py"
    cli.write_text(patched)
    cli.chmod(0o755)


def invoke_with_stdin(e, op, value, timeout=8):
    read_fd, write_fd = os.pipe()
    try:
        r = subprocess.run([PY, str(CONTROL), op, str(value)], text=True,
                           capture_output=True, timeout=timeout, stdin=read_fd,
                           env={**os.environ, "CODING_CONTROL_SETTINGS": str(e["settings"])})
    finally:
        os.close(read_fd)
        os.close(write_fd)
    assert r.stdout.strip(), (r.returncode, r.stderr)
    return json.loads(r.stdout), r.returncode


def test_executor_stdin_is_devnull(e):
    install_stdin_fake(e)
    mode(e, "stdin_probe")
    data, code = invoke_with_stdin(e, "run", e["task"])
    record = json.loads((e["tmp"] / "record.json").read_text())
    assert record["stdin_eof"] is True
    if record.get("stdin"):
        assert record["stdin"] == "/dev/null"
    assert data["status"] == "READY_FOR_REVIEW" and code == 0


def test_executor_does_not_wait_for_inherited_stdin(e):
    install_stdin_fake(e)
    mode(e, "stdin_wait")
    change(e, timeout_seconds=3)
    data, code = invoke_with_stdin(e, "run", e["task"])
    assert not (e["tmp"] / "stdin_wait_marker").exists()
    assert data["status"] == "READY_FOR_REVIEW" and code == 0
