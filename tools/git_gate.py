#!/usr/bin/env python3
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import coding_control

HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
STATUSES = b"AMDTUXRCB"


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True)


def added_lines(patch):
    added = set()
    new_line = None
    for raw in patch.splitlines():
        if raw.startswith("@@"):
            match = HUNK.match(raw)
            new_line = int(match.group(1)) if match else None
            continue
        if new_line is None:
            continue
        if raw.startswith("+"):
            added.add(new_line)
            new_line += 1
        elif raw.startswith("-") or raw.startswith("\\"):
            continue
        elif raw.startswith(" "):
            new_line += 1
    return added


def staged_entries(root):
    proc = git("diff", "--cached", "--name-status", "-z", "--no-renames", cwd=root)
    if proc.returncode != 0:
        return []
    tokens = proc.stdout.split(b"\0")
    entries = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token:
            index += 1
            continue
        if len(token) > 2 and token[:1] in STATUSES and b"\t" in token:
            status, path = token.split(b"\t", 1)
            index += 1
        else:
            status = token[:1]
            path = tokens[index + 1] if index + 1 < len(tokens) else b""
            index += 2
        if path:
            entries.append((status.decode("ascii", "replace"), os.fsdecode(path)))
    return entries


def secret_rule(issue):
    return issue.split("]")[0].split("[", 1)[1] if "[" in issue else "secret"


def findings_for(root, path, added):
    proc = git("show", f":{path}", cwd=root)
    if proc.returncode != 0:
        return [(path, 0, "binary-change")]
    data = proc.stdout
    if b"\x00" in data:
        return [(path, 0, "binary-change")]
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return [(path, 0, "binary-change")]
    if not added:
        return []
    found = []
    with tempfile.TemporaryDirectory() as folder:
        probe = Path(folder) / ("staged" + Path(path).suffix)
        probe.write_bytes(data)
        for issue in coding_control.scan_file(probe):
            lineno = coding_control._issue_line(issue)
            if lineno in added:
                found.append((path, lineno, secret_rule(issue)))
        downgrade, _ = coding_control.scan_downgrade(probe, set(added))
        for issue in downgrade:
            found.append((path, coding_control._issue_line(issue), "downgrade"))
    return found


def main():
    top = git("rev-parse", "--show-toplevel")
    if top.returncode != 0:
        sys.stderr.write(top.stderr.decode("utf-8", "replace"))
        return 1
    root = Path(os.fsdecode(top.stdout.strip()))
    findings = set()
    for status, path in staged_entries(root):
        if status == "D":
            continue
        patch = git("diff", "--cached", "--unified=0", "--no-color",
                    "--no-renames", "--", path, cwd=root)
        added = added_lines(patch.stdout.decode("utf-8", "surrogateescape"))
        findings.update(findings_for(root, path, added))
    for path, line, rule in sorted(findings):
        print(f"{path}:{line} {rule}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
