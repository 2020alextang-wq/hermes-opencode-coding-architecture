#!/usr/bin/env python3
"""Secret scanning gate for the coding-control toolchain.

Scans files for credential-shaped content and for changes that weaken
quality gates (suppression comments, skipped tests, no-op assertions).

Usage:
  pre-commit hook passes staged files as arguments
  manual whole-repo scan: python tools/secret_scan.py --all

Rules:
  1) Text secret patterns: api_key/password/secret/token assignments,
     DSN-embedded passwords, Bearer tokens, JWT literals, and dedicated
     service credential keys -- a hit means FAIL (values are masked in
     output and never printed).
  2) Binary evidence ban: newly added .db/.sqlite/.zip/.gz/.png/.jpg
     files under debug/ FAIL by default (already-tracked files are not
     affected; .gitignore only blocks untracked files, this rule blocks
     additions of tracked ones).
  3) Allowlist tools/.secret_scan_allowlist: one regex per line, matched
     against the path or "path:rule-name"; entries are only for known
     public placeholder values pending rotation, and each new allowlist
     entry must carry an inline comment with a reason and rotation date.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# -- Text secret patterns (name, regex; IGNORECASE) ---------------------
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Assignment form: key = "..." (value >= 16 chars, non-placeholder)
    ("generic-assign", re.compile(
        r"(?i)\b(api[_-]?key|apikey|password|passwd|pass[_-]?word|secret|"
        r"app[_-]?secret|client[_-]?secret|token)\b[\"']?\s*[:=]\s*[\"']?"
        r"([A-Za-z0-9_\-./+=]{16,})")),
    # DSN with embedded password
    ("dsn-embedded-password", re.compile(
        r"(?i)(postgres(?:ql)?|mysql|redis|mongodb(?:\+srv)?)://[^\s\"'/@:]+:[^\s\"'@]{4,}@")),
    # Bearer header
    ("bearer-token", re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-.+]{20,}")),
    # JWT literal (two-segment eyJ header; avoids flagging truncated examples)
    ("jwt-literal", re.compile(r"eyJ[A-Za-z0-9_\-]{15,}\.[A-Za-z0-9_\-]{8,}")),
    # Dedicated service keys
    ("asm-token", re.compile(
        r"(?i)\basm[_-]?token\b[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_\-]{12,})")),
    ("fofa-credential", re.compile(
        r"(?i)\bfofa[_-]?(email|key|token|password)\b[\"']?\s*[:=]\s*[\"']?([^\s\"']{8,})")),
    ("feishu-secret", re.compile(
        r"(?i)\bfeishu[_-]?(app[_-]?)?secret\b[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_\-]{12,})")),
    # Hardcoded password constant
    ("builtin-password-const", re.compile(
        r"(?i)BUILTIN_[A-Z_]*PASSWORD\s*=\s*\"[^\"]{6,}\"")),
]

# -- Placeholder values (a hit means the value is not a secret) ---------
VALUE_PLACEHOLDERS = re.compile(
    r"(?i)^(test|fake|dummy|mock|sample|example|placeholder|your[_-]|"
    r"xxxx|changeme|change-me|redacted|soc_test_only|<[^>]+>|\$\{[^}]*\}|\*\*\*|none|null)")
# Identifier-reference heuristic: a plain snake_case lowercase identifier
# (e.g. _feishu_secret_from_env) is a variable reference, not a literal
# secret; real secrets are rarely pure lowercase-underscore strings.
# dots variant: JS member expressions (localStorage.getItem / formData.xxx)
# are excluded for the same reason.
IDENTIFIER_LIKE = re.compile(r"^[a-z_][a-z0-9_]*$")
MEMBER_EXPR_LIKE = re.compile(r"^[a-z_][A-Za-z0-9_.()\[\]]*$")

# -- Binary ban under debug/ (rule 2) ------------------------------------
BINARY_EXT = {".db", ".sqlite", ".sqlite3", ".zip", ".gz", ".tar",
              ".png", ".jpg", ".jpeg", ".gif"}

ALLOWLIST_PATH = REPO / "tools" / ".secret_scan_allowlist"


def load_allowlist() -> list[re.Pattern[str]]:
    rules: list[re.Pattern[str]] = []
    if ALLOWLIST_PATH.exists():
        for raw in ALLOWLIST_PATH.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if line:
                rules.append(re.compile(line))
    return rules


def allowlisted(rel: str, rule_name: str, rules: list[re.Pattern[str]]) -> bool:
    keys = (rel, f"{rel}:{rule_name}")
    return any(r.search(k) for k in keys for r in rules)


def scan_file(path: Path) -> list[str]:
    """Return the list of issue lines for this file (values masked)."""
    rel = str(path.relative_to(REPO)) if str(path).startswith(str(REPO)) else str(path)
    issues: list[str] = []
    allow = load_allowlist()

    # Rule 2: binary ban under debug/ (new additions)
    if "debug/" in rel and path.suffix.lower() in BINARY_EXT:
        if not allowlisted(rel, "binary-evidence", allow):
            issues.append(f"{rel}:0 [binary-evidence] binary evidence under debug/ is "
                          f"banned by default ({path.suffix}); to commit it anyway, "
                          f"register a reason and approval in tools/.secret_scan_allowlist")
            return issues

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        issues.append(f"{rel}:0 [read-error] {e}")
        return issues

    for lineno, line in enumerate(text.splitlines(), 1):
        for name, pat in PATTERNS:
            m = pat.search(line)
            if not m:
                continue
            if allowlisted(rel, name, allow):
                continue
            # Take the matched capture group (the last non-empty group is
            # the value) for the placeholder check.
            groups = [g for g in m.groups() if g]
            value = groups[-1] if groups else m.group(0)
            if VALUE_PLACEHOLDERS.search(value):
                continue
            masked = (value[:4] + "..." + f"({len(value)} chars)") if len(value) > 8 else "****"
            if name != "jwt-literal" and (IDENTIFIER_LIKE.match(value) or MEMBER_EXPR_LIKE.match(value)):
                continue
            issues.append(f"{rel}:{lineno} [{name}] secret-shaped value detected, value masked {masked}"
                          f" -- if this is a real credential: rotate it immediately and follow the "
                          f"governance doc; if it is a false positive: register a reason in "
                          f"tools/.secret_scan_allowlist")
    return issues


def scan_downgrade(path: Path, only_lines: set[int] | None = None) -> tuple[list[str], list[str]]:
    """Detect standard-gate downgrades on added lines. Returns (failures, gate-change warnings)."""
    rel = str(path.relative_to(REPO)) if str(path).startswith(str(REPO)) else str(path)
    fails: list[str] = []
    warns: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError as e:
        return [f"{rel}:0 [read-error] {e}"], []
    suppress = re.compile(
        r"(?i)(#\s*noqa\b|eslint-disable|@ts-ignore|@ts-expect-error|type:\s*ignore)")
    skipped = re.compile(
        r"(?i)(pytest\.skip|pytest\.mark\.(?:skip|skipif|xfail|only)\b|\bit\.skip\b|\btest\.skip\b|\bdescribe\.skip\b|\.only\()")
    assert_true = re.compile(r"\bassert\s+True\b")
    for lineno, line in enumerate(lines, 1):
        if only_lines is not None and lineno not in only_lines:
            continue
        stripped = line.strip()
        if "re.compile" in line or stripped.startswith(("r\"", "r'", "\"(?", "'(?")):
            continue
        rule = None
        if suppress.search(line):
            rule = "suppress-comment"
        elif skipped.search(line):
            rule = "skip-test"
        elif assert_true.search(line):
            rule = "assert-true"
        elif rel.endswith(".secret_scan_allowlist") and line.strip() and not line.strip().startswith("#"):
            if not re.search(r"20\d{2}-\d{2}-\d{2}|expiry|expires|deadline", line):
                rule = "allowlist-no-expiry"
        if rule:
            fails.append(f"{rel}:{lineno} [{rule}] new line weakens the standard instead of fixing the problem")
    if only_lines and rel.endswith((
            ".pre-commit-config.yaml", "secret_scan.py", ".secret_scan_allowlist")):
        warns.append(f"{rel} [gate-touched] gate file has added lines; confirm they are not there just to pass checks")
    return fails, warns


def _issue_line(issue: str) -> int:
    m = re.search(r":(\d+) \[", issue)
    return int(m.group(1)) if m else 0


def staged_added_lines() -> dict[str, set[int]]:
    """Lines added in the index relative to HEAD. All non-empty lines of a new file count as added."""
    import subprocess
    out = subprocess.run(
        ["git", "-C", str(REPO), "diff", "--cached", "-U0", "--"],
        capture_output=True, text=True, check=True,
    ).stdout
    added: dict[str, set[int]] = {}
    rel = ""
    new_line = 0
    for line in out.splitlines():
        if line.startswith("+++ b/"):
            rel = line[6:]
            added.setdefault(rel, set())
        elif line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            new_line = int(match.group(1)) if match else 0
        elif line.startswith("+") and not line.startswith("+++") and rel:
            added.setdefault(rel, set()).add(new_line)
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        elif rel and not line.startswith("\\"):
            new_line += 1
    return added


def diff_added_lines() -> dict[str, set[int]]:
    """Line numbers added in the worktree relative to HEAD. All non-empty lines of untracked files count as added."""
    import subprocess
    out = subprocess.run(
        ["git", "-C", str(REPO), "diff", "-U0", "HEAD", "--"],
        capture_output=True, text=True, check=True,
    ).stdout
    added: dict[str, set[int]] = {}
    rel = ""
    new_line = 0
    for line in out.splitlines():
        if line.startswith("+++ b/"):
            rel = line[6:]
            added.setdefault(rel, set())
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            new_line = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++") and rel:
            added.setdefault(rel, set()).add(new_line)
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        elif rel and not line.startswith("\\"):
            new_line += 1
    untracked = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--others", "--exclude-standard", "-z"],
        capture_output=True, text=True, check=True,
    ).stdout
    for name in untracked.split("\0"):
        if not name.strip():
            continue
        path = REPO / name
        if not path.is_file():
            continue
        try:
            n = len(path.read_text(encoding="utf-8", errors="ignore").splitlines())
        except OSError:
            continue
        added[name] = set(range(1, n + 1))
    return added


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", help="files to scan (passed by pre-commit)")
    ap.add_argument("--all", action="store_true", help="scan the whole repo (including tracked files)")
    ap.add_argument("--diff", action="store_true",
                    help="scan only worktree lines added/modified relative to HEAD (block acceptance, before commit)")
    ap.add_argument("--cached", action="store_true",
                    help="scan only staged lines added relative to HEAD (commit hook)")
    args = ap.parse_args()

    line_filter: dict[str, set[int]] | None = None
    if args.cached:
        line_filter = staged_added_lines()
        targets = [REPO / rel for rel in line_filter if (REPO / rel).is_file()]
    elif args.diff:
        line_filter = diff_added_lines()
        targets = [REPO / rel for rel in line_filter if (REPO / rel).is_file()]
    elif args.all:
        import subprocess
        out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"],
                             capture_output=True, text=True, check=True).stdout
        targets = [REPO / f for f in out.split("\0") if f.strip()]
    else:
        targets = [Path(f) for f in args.files if Path(f).is_file()]

    all_issues: list[str] = []
    downgrade_warns: list[str] = []
    for p in targets:
        rel = str(p.relative_to(REPO)) if str(p).startswith(str(REPO)) else str(p)
        issues = scan_file(p)
        only = line_filter.get(rel, set()) if line_filter is not None else None
        downs, warns = scan_downgrade(p, only)
        if line_filter is not None:
            keep = only or set()
            issues = [i for i in issues if _issue_line(i) in keep or "[binary-evidence]" in i]
            downs = [i for i in downs if _issue_line(i) in keep]
        all_issues.extend(issues)
        all_issues.extend(downs)
        downgrade_warns.extend(warns)

    if all_issues:
        print("FAIL: secret-scan found suspected secrets or banned evidence files:")
        for i in all_issues:
            print("  " + i)
        print("Handling: see the governance documentation for security development and evidence archiving.")
        return 1
    if downgrade_warns:
        print("WARN: gate files have added lines; confirm they are not there to let things through:")
        for w in downgrade_warns:
            print("  " + w)
    print(f"OK: secret-scan passed ({len(targets)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
