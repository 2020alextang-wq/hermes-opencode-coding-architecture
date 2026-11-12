#!/usr/bin/env python3
import os
import shlex
import subprocess
import sys
from pathlib import Path

MARKER = "# coding-control git gate"


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def hook_content():
    gate = Path(__file__).resolve().parents[1] / "tools" / "git_gate.py"
    return "\n".join([
        MARKER,
        "#!/bin/sh",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(gate))}",
        "exit $?",
        "",
    ])


def main():
    top = git("rev-parse", "--show-toplevel")
    if top.returncode != 0:
        print("not inside a git repository", file=sys.stderr)
        return 1
    root = Path(top.stdout.strip())
    resolved = git("rev-parse", "--git-path", "hooks", cwd=root)
    if resolved.returncode != 0:
        print("cannot resolve git hooks directory", file=sys.stderr)
        return 1
    hooks = Path(resolved.stdout.strip())
    if not hooks.is_absolute():
        hooks = root / hooks
    hook = hooks / "pre-commit"
    if hook.exists():
        try:
            existing = hook.read_text()
        except (OSError, UnicodeDecodeError):
            existing = ""
        if MARKER not in existing:
            print(f"refusing to overwrite existing hook: {hook}", file=sys.stderr)
            return 1
    hooks.mkdir(parents=True, exist_ok=True)
    temporary = hooks / ".pre-commit.coding-control.tmp"
    temporary.write_text(hook_content())
    os.chmod(temporary, 0o755)
    os.replace(temporary, hook)
    os.chmod(hook, 0o755)
    return 0


if __name__ == "__main__":
    sys.exit(main())
