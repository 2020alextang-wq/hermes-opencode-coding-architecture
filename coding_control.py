#!/usr/bin/env python3
"""Single-attempt, offline-preflight OpenCode supervisor. No acceptance authority."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import queue
import re
import selectors
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
RID = re.compile(r"[0-9a-f]{12}\Z")
MODEL = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.:-]+)+\Z")
SESSION = re.compile(r"[\w-]+\Z")
SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
SENSITIVE = re.compile(
    r"(?:id_rsa|id_ed25519|secrets?|credentials?|passwords?|api_keys?)"
    r"(?:\.(?:json|txt|ya?ml|toml))?\Z|\.(?:pem|p12|pfx|key)\Z", re.I)
TERMINAL = {"READY_FOR_REVIEW", "BLOCKED", "CANCELLED", "TIMEOUT", "BUDGET_STOP", "FAILED"}
OPERATIONS = ("implement", "review")
CANDIDATE_PREFIX = "CODING_CONTROL_CANDIDATE "
CANDIDATE_FIELDS = {"operation", "status", "changed_files", "unverified"}
DEFAULT_TEST_TIMEOUT = 120
CLI_VERSION = re.compile(r"v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+].+)?\Z")
CLI_PROBE_FLAGS = ("--dir", "--agent", "--format")
CLI_PROBE_ENV = ("PATH", "HOME", "LANG", "TMPDIR")
DSH_VERSION = re.compile(r"v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+]\S*)?")
DSH_REQUIRED_FLAGS = ("--json", "--permission-mode", "--approval-policy", "--session-id")
DSH_SESSION_LOG_WAIT = 15.0
DSH_SESSION_LOG_INTERVAL = 0.5
DSH_ENV_KEYS = (
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "SHELL",
    "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "USER", "LOGNAME", "TMPDIR",
)
TEST_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
TEST_ENV_SENSITIVE = re.compile(
    r"(?:SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|API_KEY|ACCESS_KEY|PRIVATE_KEY|"
    r"DSN|DATABASE|CONNECTION|(?:^|_)KEY(?:_|$))", re.I)
TEST_ENV_FORBIDDEN = {
    "HOME", "PATH", "PWD", "OLDPWD", "TMP", "TMPDIR", "TEMP", "SHELL", "USER", "LOGNAME",
    "LANG", "LANGUAGE", "ENV", "IFS", "BASH_ENV", "BASHOPTS", "SHELLOPTS", "CDPATH",
    "GLOBIGNORE", "SSLKEYLOGFILE",
}
TEST_ENV_FORBIDDEN_PREFIX = ("XDG_", "LD_", "DYLD_", "PYTHON", "PYTEST", "DOTENV",
                             "OPENCODE", "CODING_CONTROL", "LC_", "NODE_", "PERL5",
                             "RUBY", "JAVA_", "_JAVA_", "DB_", "PG", "MYSQL", "POSTGRES",
                             "MONGO", "REDIS", "SQLITE", "ORACLE")

# Optional content scanner, loaded from the path in CODING_CONTROL_SECRET_SCAN.
# When the variable is unset (or the file is missing) secret/downgrade content
# scanning is gracefully skipped and the scan helpers below become no-ops.
_SECRET_SCAN_PATH = os.environ.get("CODING_CONTROL_SECRET_SCAN", "")
scan_file = lambda path: []
scan_downgrade = lambda path, only_lines=None: ([], [])
_issue_line = lambda issue: 0
if _SECRET_SCAN_PATH and Path(_SECRET_SCAN_PATH).is_file():
    _ss_spec = importlib.util.spec_from_file_location("secret_scan", _SECRET_SCAN_PATH)
    _ss = importlib.util.module_from_spec(_ss_spec)
    _ss_spec.loader.exec_module(_ss)
    scan_file = _ss.scan_file
    scan_downgrade = _ss.scan_downgrade
    _issue_line = _ss._issue_line


def output(data, code=0):
    print(json.dumps(data, ensure_ascii=False, default=str))
    return code


def inside(path, root):
    return path == root or root in path.parents


def state(settings):
    # A custom state directory is allowed for isolated tests; no state goes into a business project.
    return Path(settings.get("state_dir", HERE / "state")).resolve()


def ledger_path(settings, rid):
    return state(settings) / "runs" / rid / "ledger.json"


def load_ledger(settings, rid):
    path = ledger_path(settings, rid)
    return json.loads(path.read_text()) if path.is_file() else None


def save_ledger(settings, rid, data):
    path = ledger_path(settings, rid)
    private_dir(path.parent)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
        name = f.name
    os.replace(name, path)


def private_dir(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)


def write_private_json(path, data):
    private_dir(path.parent)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
        name = f.name
    os.chmod(name, 0o600)
    os.replace(name, path)
    path.chmod(0o600)


def test_env_problem(key, value):
    """Reject task test_env entries that could influence execution or carry secrets."""
    if not isinstance(key, str) or not TEST_ENV_NAME.fullmatch(key):
        return f"invalid test_env key: {key!r}"
    upper = key.upper()
    if TEST_ENV_SENSITIVE.search(upper):
        return f"test_env key looks sensitive: {key}"
    if upper in TEST_ENV_FORBIDDEN or upper.startswith(TEST_ENV_FORBIDDEN_PREFIX):
        return f"test_env cannot override runtime key: {key}"
    if (not isinstance(value, str) or "\x00" in value or "\n" in value or "\r" in value):
        return f"test_env value for {key} must be a single-line string"
    return None


def resolve_runner(task, settings):
    """Merge settings.runner with legacy cli/runner_model into one runner record."""
    errors = []
    config = settings.get("runner", {})
    if config is None:
        config = {}
    if not isinstance(config, dict):
        return ({"binary": None, "model": None, "provider": None, "sandbox": None,
                 "declared_version": None, "model_source": None},
                ["runner must be an object"])
    kind = config.get("kind", "opencode")
    if not isinstance(kind, str) or kind not in ("opencode", "dsh"):
        errors.append(f"unknown runner kind: {kind!r}")
    dsh_home = config.get("dsh_home")
    api_key_env = config.get("api_key_env")
    if kind == "dsh" and task.get("operation", "implement") != "implement":
        if (not isinstance(dsh_home, str) or not Path(dsh_home).is_absolute() or
                not Path(dsh_home).exists()):
            errors.append("invalid dsh runner config: dsh_home must be an absolute existing path")
        if not isinstance(api_key_env, str) or not api_key_env.strip():
            errors.append("invalid dsh runner config: api_key_env must be nonempty text")
    sandbox = config.get("sandbox", "none")
    if not isinstance(sandbox, str) or sandbox not in ("none", "seatbelt"):
        errors.append(f"unknown sandbox: {sandbox!r}")
    elif sandbox == "seatbelt" and not shutil.which("sandbox-exec"):
        errors.append("sandbox executable not found")
    binary = config.get("binary")
    legacy_cli = settings.get("cli")
    if binary is not None:
        if not isinstance(binary, str) or not binary:
            errors.append("invalid runner binary")
        elif legacy_cli is not None:
            try:
                if Path(binary).resolve() != Path(legacy_cli).resolve():
                    errors.append("runner config conflict: binary")
            except (OSError, TypeError, ValueError):
                errors.append("invalid runner binary")
    selected = binary if binary is not None else legacy_cli
    declared_present = "version" in config
    declared = config.get("version")
    if declared_present and (not isinstance(declared, str) or not declared):
        errors.append("runner version must be nonempty text")
    legacy_model = settings.get("runner_model")
    runner_model = config.get("model")
    if "model" in config and legacy_model is not None and runner_model != legacy_model:
        errors.append("runner config conflict: model")
    if task.get("model"):
        model, source = task["model"], "task"
    elif "model" in config:
        model, source = runner_model, "runner"
    else:
        model, source = legacy_model, "runner_model"
    if not isinstance(model, str) or not MODEL.fullmatch(model):
        errors.append("model must be provider/model")
    provider = config.get("provider")
    if "provider" in config and (not isinstance(provider, str) or not provider or
                                 not isinstance(model, str) or
                                 not model.startswith(provider + "/")):
        errors.append("runner provider/model mismatch")
    if selected is None:
        errors.append("CLI missing or not executable")
    elif (not isinstance(selected, str) or not Path(selected).is_file() or
          not os.access(selected, os.X_OK)):
        errors.append("CLI missing or not executable")
    resolved_binary = (str(Path(selected).resolve())
                       if isinstance(selected, str) and selected else None)
    result = {"kind": kind, "binary": resolved_binary, "model": model,
              "provider": provider, "sandbox": sandbox,
              "declared_version": declared if declared_present else None,
              "model_source": source}
    if kind == "dsh":
        result.update({"dsh_home": dsh_home, "api_key_env": api_key_env})
    return result, errors


def probe_env():
    env = {name: os.environ[name] for name in CLI_PROBE_ENV if name in os.environ}
    env.setdefault("PATH", os.defpath)
    return env


def probe_runner(binary):
    """Detect CLI version and required run flags without model or session side effects."""
    env = probe_env()
    try:
        version = subprocess.run([binary, "--version"], capture_output=True, text=True,
                                 timeout=10, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, None, f"cli probe failed: {exc}"
    if version.returncode != 0:
        return None, None, f"cli probe failed: --version exit {version.returncode}"
    lines = [line.strip() for line in version.stdout.splitlines() if line.strip()]
    if not lines:
        return None, None, "cli probe failed: empty version output"
    text = lines[0]
    if not CLI_VERSION.fullmatch(text):
        return None, None, f"cli probe failed: unparseable version {text!r}"
    detected = text[1:] if text.startswith("v") else text
    try:
        help_result = subprocess.run([binary, "run", "--help"], capture_output=True, text=True,
                                     timeout=10, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, None, f"cli probe failed: {exc}"
    if help_result.returncode != 0:
        return None, None, f"cli probe failed: run --help exit {help_result.returncode}"
    help_text = help_result.stdout + help_result.stderr
    missing = [flag for flag in CLI_PROBE_FLAGS if flag not in help_text]
    if missing:
        return None, None, "run --help missing required flags: " + ", ".join(missing)
    return detected, list(CLI_PROBE_FLAGS), None


def probe_dsh(binary):
    """Detect dsh version and headless capabilities without starting a profile."""
    env = probe_env()
    try:
        version = subprocess.run([binary, "--version"], capture_output=True, text=True,
                                 timeout=10, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, [], [], f"dsh probe failed: {exc}"
    if version.returncode != 0:
        return None, [], [], f"dsh probe failed: --version exit {version.returncode}"
    match = DSH_VERSION.search(version.stdout)
    if not match:
        return None, [], [], "dsh probe failed: version not found"
    detected = match.group(0)
    if detected.startswith("v"):
        detected = detected[1:]
    try:
        help_result = subprocess.run([binary, "--profile", "headless", "--help"],
                                     capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, [], [], f"dsh probe failed: {exc}"
    if help_result.returncode != 0:
        return None, [], [], f"dsh probe failed: headless help exit {help_result.returncode}"
    help_text = help_result.stdout + help_result.stderr
    capabilities = sorted(flag for flag in DSH_REQUIRED_FLAGS if flag in help_text)
    stdin_declared = any(line.strip() == "-" for line in help_text.splitlines())
    if stdin_declared:
        capabilities.append("stdin:-")
    warnings = [] if stdin_declared else ["dsh stdin '-' not declared in headless help"]
    return detected, capabilities, warnings, None


COMMAND_GROUPS = ("iteration", "block_gate", "final_gate")


def test_argv_problem(argv):
    if (not isinstance(argv, list) or len(argv) < 4 or
            any(not isinstance(a, str) or not a or
                re.search(r"[;&|`$<>\n\r*?\[\]{}\\]", a) for a in argv)):
        return "invalid test argv or shell operator"
    exe = shutil.which(argv[0])
    if (not exe or not os.access(exe, os.X_OK) or
            Path(exe).name not in ("python", "python3", "pytest") and
            not re.fullmatch(r"python3?\.\d+", Path(exe).name)):
        return "only installed Python/pytest executable accepted"
    if Path(exe).name.startswith("python"):
        if argv[1:3] != ["-m", "pytest"] or len(argv) < 4 or argv[3].startswith("-"):
            return "only python -m pytest FILE [pytest flags] accepted"
    elif len(argv) < 2 or argv[1].startswith("-"):
        return "pytest requires an explicit file"
    if any(a.startswith("/") for a in argv[1:]):
        return "absolute test arguments not allowed"
    return None


def validate_command_groups(task):
    if "command_groups" in task and "test_commands" in task:
        return None, ["command_groups and test_commands are mutually exclusive"]
    if "command_groups" in task:
        groups = task["command_groups"]
        if not isinstance(groups, dict):
            return None, ["command_groups must be an object"]
        unknown = sorted(set(groups) - set(COMMAND_GROUPS))
        if unknown:
            return None, ["unknown command group: " + ", ".join(unknown)]
        normalized, errors = {}, []
        for name in COMMAND_GROUPS:
            commands = groups.get(name, [])
            if not isinstance(commands, list):
                errors.append(f"{name} must be argv arrays")
                normalized[name] = []
                continue
            normalized[name] = commands
            for argv in commands:
                problem = test_argv_problem(argv)
                if problem:
                    errors.append(f"{name}: {problem}")
        if errors:
            return None, errors
        if not normalized["final_gate"]:
            return None, ["final_gate must be nonempty"]
        return normalized, []
    commands = task.get("test_commands")
    if not isinstance(commands, list) or not commands:
        return None, ["test_commands must be nonempty argv arrays"]
    errors = []
    for argv in commands:
        problem = test_argv_problem(argv)
        if problem:
            errors.append(problem)
    if errors:
        return None, errors
    return {"iteration": [], "block_gate": [], "final_gate": commands}, []


def effective_groups(task):
    if "command_groups" in task:
        groups = task["command_groups"]
        return {name: list(groups.get(name, [])) for name in COMMAND_GROUPS}
    commands = task.get("test_commands", [])
    return {"iteration": [], "block_gate": [], "final_gate": [list(argv) for argv in commands]}


def validate_task(task, settings, runner):
    """Validate without running commands or writing files; shared by run/preflight."""
    errors = []
    if not isinstance(task, dict) or not isinstance(settings, dict):
        return ["task/settings must be objects"]
    try:
        root = Path(settings["workspace_root"]).resolve(strict=True)
        project = Path(task["project"]).resolve(strict=True)
        if not root.is_dir() or not project.is_dir() or not inside(project, root) or project == root:
            errors.append("project must be a directory inside workspace_root")
    except (KeyError, TypeError, ValueError, OSError):
        errors.append("invalid workspace_root/project")
        project = None
    if not isinstance(task.get("goal"), str) or not task["goal"].strip():
        errors.append("goal must be nonempty text")
    files = task.get("allowed_files")
    if not isinstance(files, list) or not files:
        errors.append("allowed_files must be nonempty array")
    else:
        for rel in files:
            if not isinstance(rel, str) or not rel or rel == "." or "\\" in rel or any(c in rel for c in "*?[]{}"):
                errors.append(f"invalid exact file scope: {rel!r}")
                continue
            p = Path(rel)
            segments = rel.split("/")
            if (p.is_absolute() or any(s in ("", ".", "..", ".git") for s in segments)
                or sensitive(rel)):
                errors.append(f"prohibited file scope: {rel}")
            elif project:
                full = (project / rel).resolve(strict=False)
                if not inside(full, project) or full == project or full.is_dir():
                    errors.append(f"file scope leaves project or is directory: {rel}")
    deletions = task.get("allow_delete", [])
    if not isinstance(deletions, list):
        errors.append("invalid allow_delete: must be an array")
    else:
        allowed_set = set(files) if isinstance(files, list) else set()
        for rel in deletions:
            if (not isinstance(rel, str) or not rel or rel == "." or "\\" in rel or
                    any(c in rel for c in "*?[]{}")):
                errors.append(f"invalid allow_delete: {rel!r}")
                continue
            p = Path(rel)
            segments = rel.split("/")
            if p.is_absolute() or any(s in ("", ".", "..", ".git") for s in segments):
                errors.append(f"invalid allow_delete: {rel}")
            elif project and (project / rel).is_dir():
                errors.append(f"invalid allow_delete: directory {rel}")
            elif rel not in allowed_set:
                errors.append(f"allow_delete not in allowed_files: {rel}")
    _, command_errors = validate_command_groups(task)
    errors.extend(command_errors)
    operation = task.get("operation", "implement")
    if operation not in OPERATIONS:
        errors.append("operation must be implement or review")
    for field in ("timeout_seconds", "test_timeout_seconds", "max_tokens", "max_permission_denials"):
        val = task.get(field, {"timeout_seconds": 300, "test_timeout_seconds": DEFAULT_TEST_TIMEOUT,
                               "max_tokens": 500000, "max_permission_denials": 5}[field])
        if type(val) is not int or val <= 0:
            errors.append(f"{field} must be positive integer")
    test_env = task.get("test_env", {})
    if not isinstance(test_env, dict):
        errors.append("test_env must be a literal object")
    else:
        for key, value in test_env.items():
            problem = test_env_problem(key, value)
            if problem:
                errors.append(problem)
    watch = settings.get("watch_paths", [])
    if not isinstance(watch, list):
        errors.append("invalid watch_paths: must be an array of absolute directory paths")
    else:
        for path in watch:
            if (not isinstance(path, str) or not path or not Path(path).is_absolute()
                    or not Path(path).is_dir()):
                errors.append(f"invalid watch_paths: {path!r}")
    parent, session = task.get("parent_run"), task.get("session_id")
    model = runner["model"]
    for field in ("hermes_task_id", "hermes_session_id"):
        value = task.get(field)
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[\w./:-]{1,160}", value)):
            errors.append(f"invalid {field}")
    if bool(parent) != bool(session) or (session and (not isinstance(session, str) or not SESSION.fullmatch(session))):
        errors.append("session_id and parent_run must be provided together")
    if parent:
        if not isinstance(parent, str) or not RID.fullmatch(parent):
            errors.append("invalid parent_run")
        else:
            old = load_ledger(settings, parent)
            if (not old or old.get("status") != "READY_FOR_REVIEW" or
                old.get("project") != str(project) or old.get("model") != model or
                old.get("session_id") != session or
                old.get("operation", "implement") != operation):
                errors.append("parent project/model/session/status/operation mismatch")
    return errors


def git(project, *args):
    r = subprocess.run(["git", *args], cwd=project, capture_output=True, timeout=4)
    if r.returncode:
        raise ValueError(f"git {' '.join(args)} failed: {r.stderr.decode(errors='replace')[:200]}")
    return r.stdout


def git_info(project):
    def optional(*args):
        r = subprocess.run(["git", *args], cwd=project, capture_output=True, timeout=4)
        return r.stdout.decode().strip() if r.returncode == 0 else None
    return {"head": optional("rev-parse", "HEAD"), "branch": optional("branch", "--show-current"),
            "dirty": bool(git(project, "status", "--porcelain"))}


def sensitive(rel):
    return any(s.lower().startswith(".env") or SENSITIVE.search(s) for s in rel.split("/"))


def skill_manifest(settings):
    """Resolve declared skill names to absolute paths and file hashes without reading bodies."""
    declared = settings.get("skills", [])
    if declared is None:
        declared = []
    if not isinstance(declared, list):
        return [], ["invalid skill declaration: skills must be an array"]
    entries, errors = [], []
    for name in declared:
        if not isinstance(name, str) or not SKILL_NAME.fullmatch(name):
            errors.append(f"invalid skill: {name!r}")
            continue
        path = HERE / "skills" / name / "SKILL.md"
        try:
            path = path.resolve(strict=True)
            content = path.read_bytes() if path.is_file() else None
        except OSError:
            content = None
        if content is None:
            errors.append(f"unknown skill: {name}")
            continue
        entries.append({"name": name, "path": str(path),
                        "sha256": hashlib.sha256(content).hexdigest()})
    entries.sort(key=lambda entry: entry["name"])
    return entries, errors


def snapshot(project, settings):
    # Enumerate tracked and pre-existing untracked; hash only regular non-secret files.
    tracked = set(git(project, "ls-files", "-z").decode().strip("\0").split("\0"))
    others = set(git(project, "ls-files", "--others", "--exclude-standard", "-z").decode().strip("\0").split("\0"))
    result = {}
    own_state = state(settings)
    for rel in sorted((tracked | others) - {""}):
        target = project / rel
        if sensitive(rel) or inside(target.resolve(strict=False), own_state):
            continue
        if target.is_symlink():
            result[rel] = hashlib.sha256(os.readlink(target).encode()).hexdigest()
        elif target.is_file():
            result[rel] = hashlib.sha256(target.read_bytes()).hexdigest()
        elif rel in tracked:
            result[rel] = None
    return result


def control_snapshot(base, runner_state=None):
    """Pure source fingerprint for the control plane, excluding its own runtime output."""
    base = Path(base).resolve()
    runtime = Path(runner_state).resolve() if runner_state is not None else None
    result = {}
    ignored = {".git", "state", "logs", "__pycache__", ".pytest_cache"}
    for directory, dirs, files in os.walk(base, followlinks=False):
        parent = Path(directory)
        dirs[:] = [name for name in dirs if name not in ignored and
                   (runtime is None or not inside((parent / name).resolve(), runtime))]
        for name in dirs:
            rel = (parent / name).relative_to(base).as_posix() + "/"
            result[rel] = "directory"
        for name in files:
            path = parent / name
            rel = path.relative_to(base).as_posix()
            if name.endswith((".pyc", ".pyo")) or sensitive(rel) or (runtime and inside(path.resolve(), runtime)):
                continue
            if path.is_symlink():
                result[rel] = hashlib.sha256(os.readlink(path).encode()).hexdigest()
            elif path.is_file():
                result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def control_plane_delta(project, before, after, base):
    if Path(project).resolve() == Path(base).resolve():
        return []  # The project snapshot already covers this directory.
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))


def snapshot_tree(base):
    """Absolute-path hash snapshot; symlinks and unreadable files are skipped."""
    result = {}
    base = Path(base)

    def ignore(_error):
        return

    try:
        for directory, dirs, files in os.walk(base, followlinks=False, onerror=ignore):
            parent = Path(directory)
            dirs[:] = [name for name in dirs if not (parent / name).is_symlink()]
            for name in files:
                path = parent / name
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    result[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    continue
    except OSError:
        pass
    return result


def watch_snapshot(settings):
    paths = settings.get("watch_paths") or []
    if not paths:
        return None
    result = {}
    for path in paths:
        result.update(snapshot_tree(path))
    return result


def apply_external_writes(ledger, before, after):
    if before is None or after is None:
        return
    changes = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    ledger["external_writes"] = changes
    if not changes:
        return
    shown = changes[:5]
    suffix = f" (+{len(changes) - 5} more)" if len(changes) > 5 else ""
    message = "external-write: " + ", ".join(shown) + suffix
    ledger["error"] = ((ledger["error"] + "; ") if ledger.get("error") else "") + message
    if ledger["status"] != "FAILED":
        ledger["status"] = "FAILED"


def executor_environment(config):
    """Minimal executor env: host auth discovery plus proxies, never host secrets."""
    env = {}
    for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "SHELL",
                "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                "http_proxy", "https_proxy", "all_proxy", "no_proxy",
                "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
                "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "USER", "LOGNAME"):
        if key in os.environ:
            env[key] = os.environ[key]
    env.setdefault("PATH", os.defpath)
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    return env


def seatbelt_profile(project, state_dir, tmp_dir):
    """Build an SBPL profile that only permits writes inside trusted runtime paths."""
    def quoted(path):
        return str(Path(path).resolve()).replace("\\", "\\\\").replace('"', '\\"')

    return "\n".join([
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        '(allow file-write* (subpath "%s") (subpath "%s") (subpath "%s") '
        '(literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr"))'
        % (quoted(project), quoted(state_dir), quoted(tmp_dir)),
    ])


def dsh_environment(runner):
    """Minimal dsh env: host whitelist plus DSH_HOME and the single declared key."""
    env = {key: os.environ[key] for key in DSH_ENV_KEYS if key in os.environ}
    env.setdefault("PATH", os.defpath)
    env["DSH_HOME"] = runner["dsh_home"]
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    key = runner["api_key_env"]
    value = os.environ.get(key)
    if not value:
        raise ValueError(f"missing api key env: {key}")
    env[key] = value
    return env


def test_environment(task, folder):
    """Explicit test whitelist with private HOME/TMPDIR/XDG; no host passthrough."""
    env_root = folder / "env"
    home = env_root / "home"
    tmp = env_root / "tmp"
    xdg = env_root / "xdg"
    for directory in (home, tmp, xdg / "cache", xdg / "config", xdg / "data", xdg / "state"):
        private_dir(directory)
    path_dirs = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    for commands in effective_groups(task).values():
        for argv in commands:
            exe = shutil.which(argv[0])
            if exe:
                path_dirs.insert(0, str(Path(exe).resolve().parent))
    env = {
        "PATH": ":".join(dict.fromkeys(path_dirs)),
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "TMP": str(tmp),
        "XDG_CACHE_HOME": str(xdg / "cache"),
        "XDG_CONFIG_HOME": str(xdg / "config"),
        "XDG_DATA_HOME": str(xdg / "data"),
        "XDG_STATE_HOME": str(xdg / "state"),
        "LANG": os.environ.get("LANG") or "C.UTF-8",
        "TZ": "UTC",
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
    }
    env.update({key: value for key, value in task.get("test_env", {}).items()})
    return env, env_root


def permissions(task, project, workspace, model):
    edit = {"*": "deny"}
    if task.get("operation", "implement") == "implement":
        for rel in task["allowed_files"]:
            edit[rel] = edit["./" + rel] = edit[str(project / rel)] = "allow"
    external = {"*": "deny", str(workspace): "deny", str(workspace / "**"): "deny"}
    for p in (project, HERE / "skills", HERE / "EXECUTOR.md", HERE.parent.parent / "AGENTS.md"):
        external[str(p.resolve())] = "allow"
        if p.is_dir():
            external[str(p.resolve() / "**")] = "allow"
    bash = {"*": "deny"}
    for commands in effective_groups(task).values():
        for cmd in commands:
            bash[shlex.join(cmd)] = "allow"
    return {"small_model": model,
            "permission": {"edit": edit, "external_directory": external, "bash": bash,
                           "task": "deny", "webfetch": "deny", "websearch": "deny"}}


def export(cli, sid, project, env, path):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as stream:
        r = subprocess.run([cli, "export", sid], cwd=project, env=env,
                           stdout=stream, stderr=subprocess.PIPE, timeout=15)
    if r.returncode:
        raise ValueError(f"export exit {r.returncode}: {r.stderr.decode(errors='replace')[:200]}")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("info", {}).get("id") != sid or not isinstance(data.get("messages"), list):
        raise ValueError("invalid export session/schema")
    return data


def aggregate_usage(data, before=()):
    seen = set(before)
    totals = {k: 0 for k in ("input", "output", "cache_read", "cache_write", "reasoning", "total", "message_count")}
    models = set()
    incomplete = False
    for msg in data["messages"]:
        info = msg.get("info", {})
        if info.get("role") != "assistant" or not info.get("id") or info["id"] in seen:
            continue
        seen.add(info["id"])
        provider, model = info.get("providerID"), info.get("modelID")
        if provider and model:
            models.add(f"{provider}/{model}")
        else:
            incomplete = True
        tok = info.get("tokens")
        if not info.get("time", {}).get("completed") or not isinstance(tok, dict):
            incomplete = True
            continue
        cache = tok.get("cache") or {}
        parts = {"input": tok.get("input"), "output": tok.get("output"),
                 "cache_read": cache.get("read"), "cache_write": cache.get("write"),
                 "reasoning": tok.get("reasoning"), "total": tok.get("total")}
        if any(type(v) is not int or v < 0 for v in parts.values()):
            incomplete = True
            continue
        for k, v in parts.items():
            totals[k] += v
        totals["message_count"] += 1
    if not totals["message_count"] or incomplete:
        return None, models, True, seen
    return totals, models, False, seen


def _text_lines(message):
    lines = []
    for part in message.get("parts") or []:
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            lines.extend(part["text"].splitlines())
    return lines


FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)\Z")


def _fence_states(lines):
    """Mark each line as inside a fenced block, honouring fence type and length."""
    states = []
    opened = None
    for line in lines:
        match = FENCE.match(line)
        if opened is None:
            if match:
                opened = (match.group(1)[0], len(match.group(1)))
                states.append(True)
            else:
                states.append(False)
        elif (match and match.group(1)[0] == opened[0] and
              len(match.group(1)) >= opened[1] and not match.group(2).strip()):
            opened = None
            states.append(True)
        else:
            states.append(True)
    return states


def validate_candidate(env, task, operation):
    if not isinstance(env, dict) or set(env) != CANDIDATE_FIELDS:
        return None, "invalid candidate envelope: exact fields required"
    if env["operation"] not in OPERATIONS:
        return None, "invalid candidate envelope: operation value"
    if env["operation"] != operation:
        return None, "invalid candidate envelope: operation does not match task"
    if env["status"] not in ("READY_FOR_REVIEW", "BLOCKED"):
        return None, "invalid candidate envelope: status value"
    changed = env["changed_files"]
    if not isinstance(changed, list) or any(not isinstance(rel, str) or not rel for rel in changed):
        return None, "invalid candidate envelope: changed_files must be text list"
    if len(set(changed)) != len(changed):
        return None, "invalid candidate envelope: duplicate changed_files"
    allowed = set(task["allowed_files"])
    for rel in changed:
        segments = rel.split("/")
        if (Path(rel).is_absolute() or "\\" in rel or
                any(s in ("", ".", "..") for s in segments)):
            return None, f"invalid candidate envelope: not an exact relative path: {rel}"
        if rel not in allowed:
            return None, f"invalid candidate envelope: changed file outside allowed_files: {rel}"
    unverified = env["unverified"]
    if (not isinstance(unverified, list) or
            any(not isinstance(item, str) or not item.strip() for item in unverified)):
        return None, "invalid candidate envelope: unverified must be text list"
    if len(set(unverified)) != len(unverified):
        return None, "invalid candidate envelope: duplicate unverified entries"
    return env, None


def parse_candidate(data, previous, task, operation):
    seen = set()
    new = []
    for message in data["messages"]:
        info = message.get("info") or {}
        mid = info.get("id")
        if info.get("role") != "assistant" or not mid or mid in seen:
            continue
        seen.add(mid)
        if mid not in previous:
            new.append(message)
    if not new:
        return None, "missing candidate envelope: no new assistant message"
    lines = _text_lines(new[-1])
    index = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None)
    if index is None:
        return None, "missing candidate envelope: final assistant message has no text"
    line = lines[index]
    if not line.startswith(CANDIDATE_PREFIX):
        return None, "missing candidate envelope: final text line is not the candidate marker"
    if _fence_states(lines)[index]:
        return None, "invalid candidate envelope: marker inside code block"
    total = 0
    for message in new:
        message_lines = _text_lines(message)
        for text, fenced in zip(message_lines, _fence_states(message_lines)):
            if not fenced and text.startswith(CANDIDATE_PREFIX):
                total += 1
    if total != 1:
        return None, "invalid candidate envelope: exactly one required"
    try:
        env = json.loads(line[len(CANDIDATE_PREFIX):])
    except ValueError:
        return None, "invalid candidate envelope: not valid JSON"
    return validate_candidate(env, task, operation)


def parse_dsh_candidate(text, task, operation):
    if not isinstance(text, str):
        return None, "missing candidate envelope: final text is absent"
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None, "missing candidate envelope: final text is empty"
    line = lines[-1]
    if not line.startswith(CANDIDATE_PREFIX):
        return None, "missing candidate envelope: final text line is not the candidate marker"
    try:
        env = json.loads(line[len(CANDIDATE_PREFIX):])
    except ValueError:
        return None, "invalid candidate envelope: not valid JSON"
    env, problem = validate_candidate(env, task, operation)
    if problem:
        return None, problem
    if operation == "review" and env["changed_files"]:
        return None, "invalid candidate envelope: review changed_files must be []"
    return env, None


def hermes_snapshot(settings):
    result = {"status": "pending_after_turn/not_attributable", "snapshot": None}
    db, sid = settings.get("hermes_db"), settings.get("hermes_session_id")
    if not db or not sid:
        return result
    try:
        uri = Path(db).resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1) as conn:
            conn.execute("PRAGMA query_only=ON")
            # Only known numeric counters; never expose conversation content.
            tables = {x[0] for x in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("sessions", "session"):
                if table not in tables:
                    continue
                columns = {x[1] for x in conn.execute(f"PRAGMA table_info({table})")}
                ident = "session_id" if "session_id" in columns else "id" if "id" in columns else None
                counters = sorted(columns & {"input_tokens", "output_tokens", "total_tokens", "tokens_input", "tokens_output"})
                if ident and counters:
                    row = conn.execute(f"SELECT {', '.join(counters)} FROM {table} WHERE {ident}=?", (sid,)).fetchone()
                    result["snapshot"] = dict(zip(counters, row)) if row else None
                    break
    except (OSError, sqlite3.Error) as exc:
        result["error"] = str(exc)
    return result


def hermes_reference(task, settings):
    """Pin a task/session at run start; never re-select latest at usage time."""
    sid = task.get("hermes_session_id") or settings.get("hermes_session_id")
    if not isinstance(sid, str) or not re.fullmatch(r"[\w./:-]{1,160}", sid):
        return None, None
    tid = task.get("hermes_task_id")
    if tid:
        return {"task_id": tid, "session_id": sid}, None
    from hermes_usage import latest
    found = latest(session_id=sid)
    if found["status"] == "ok":
        return {"task_id": found["task_id"], "session_id": sid}, None
    return None, found if found["status"] == "recording_error" else None


def started(pid):
    r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
    return r.stdout.strip() if r.returncode == 0 else None


def group_live(pgid):
    # killpg(..., 0) includes zombies. ps distinguishes live descendants from zombies.
    r = subprocess.run(["ps", "-axo", "pgid=,stat="], capture_output=True, text=True, timeout=2)
    for line in r.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == str(pgid) and not fields[1].startswith("Z"):
            return True
    return False


def stop_group(proc, pgid):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        until = time.monotonic() + .6
        while time.monotonic() < until and group_live(pgid):
            time.sleep(.04)
        if not group_live(pgid):
            break
    proc.wait(timeout=2)


def run_tests(task, project, folder, cancel_flag, candidate_before):
    """Execute verified test argv under a shared deadline and private evidence."""
    env, env_root = test_environment(task, folder)
    tests_dir = folder / "tests"
    private_dir(tests_dir)
    timeout = task.get("test_timeout_seconds", DEFAULT_TEST_TIMEOUT)
    deadline = time.monotonic() + timeout
    final_gate = effective_groups(task)["final_gate"]
    evidence = {
        "run_id": None,
        "test_timeout_seconds": timeout,
        "runtime_dir": str(env_root),
        "started_at": time.time(),
        "ended_at": None,
        "status": "not_run",
        "not_run_reason": None,
        "candidate_before": candidate_before,
        "candidate_after": None,
        "candidate_changed": None,
        "planned_commands": [list(argv) for argv in final_gate],
        "commands": [],
    }
    status = "passed"
    for index, argv in enumerate(final_gate, 1):
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(argv[0]).name) or "command"
        log_path = tests_dir / f"{index:02d}-{safe_name}.log"
        command = {"argv": list(argv), "group": "final_gate", "cwd": str(project),
                   "started_at": time.time(), "ended_at": None, "exit_code": None,
                   "log_path": str(log_path), "status": "running"}
        evidence["commands"].append(command)
        proc = None
        outcome = None
        try:
            with os.fdopen(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as stream:
                proc = subprocess.Popen(argv, cwd=project, env=env, stdin=subprocess.DEVNULL,
                                        stdout=stream, stderr=subprocess.STDOUT,
                                        start_new_session=True)
                pgid = proc.pid
                while True:
                    if cancel_flag.is_set():
                        outcome = "cancelled"
                        break
                    if time.monotonic() >= deadline:
                        outcome = "timeout"
                        break
                    code = proc.poll()
                    if code is not None:
                        outcome = "passed" if code == 0 else "failed"
                        break
                    time.sleep(.05)
                if outcome in ("cancelled", "timeout"):
                    stop_group(proc, pgid)
                else:
                    proc.wait(timeout=2)
                    if group_live(pgid):
                        stop_group(proc, pgid)
                        outcome = "failed"
        except OSError as exc:
            outcome = "failed"
            command["error"] = str(exc)
        command["ended_at"] = time.time()
        command["exit_code"] = proc.returncode if proc is not None else None
        command["status"] = outcome or "failed"
        if outcome != "passed":
            status = outcome or "failed"
            break
    evidence["ended_at"] = time.time()
    evidence["status"] = status
    return evidence, tests_dir


def drive(proc, pgid, deadline, ledger, task, log, cancelled):
    q = queue.Queue()

    def reader():
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        buf = b""
        try:
            while True:
                if not sel.select(.1):
                    continue
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    if buf:
                        q.put(buf.decode(errors="replace"))
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    q.put(line.decode(errors="replace"))
        finally:
            sel.close()
            q.put(None)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    seen_steps, seen_denials = set(), set()
    failure = False
    stopping = None
    eof = False
    while not eof:
        if cancelled.is_set():
            stopping = "CANCELLED"
        elif time.monotonic() >= deadline:
            stopping = "TIMEOUT"
        if stopping:
            stop_group(proc, pgid)
            break
        try:
            line = q.get(timeout=min(.05, max(.001, deadline - time.monotonic())))
        except queue.Empty:
            continue
        if line is None:
            eof = True
            break
        log.write(line + "\n")
        log.flush()
        try:
            event = json.loads(line)
        except ValueError:
            continue
        part = event.get("part") or {}
        typ = event.get("type")
        if event.get("sessionID"):
            ledger["session_id"] = event["sessionID"]
        if typ == "error":
            failure = True
        if typ == "tool_use" and (part.get("state") or {}).get("status") == "error":
            error = str((part.get("state") or {}).get("error", ""))
            if "rule which prevents" in error or "PermissionDenied" in error:
                if part.get("id") not in seen_denials:
                    seen_denials.add(part.get("id"))
                    ledger["permission_denials"] += 1
                    if ledger["permission_denials"] >= task.get("max_permission_denials", 5):
                        stopping = "BLOCKED"
            else:
                failure = True
        if typ == "step_finish" and part.get("id") not in seen_steps:
            seen_steps.add(part.get("id"))
            tokens = part.get("tokens") or {}
            if type(tokens.get("total")) is int:
                ledger["token_total"] += tokens["total"]
                if ledger["token_total"] >= task.get("max_tokens", 500000):
                    stopping = "BUDGET_STOP"
            else:
                ledger["budget_incomplete"] = True
        if stopping:
            stop_group(proc, pgid)
            break
    if not stopping:
        proc.wait(timeout=2)
        # A forked descendant can keep running even after the leader exits.
        if group_live(pgid):
            stop_group(proc, pgid)
            failure = True
    thread.join(timeout=.3)
    return stopping or ("FAILED" if failure or proc.returncode != 0 else "READY_FOR_REVIEW")


def drive_dsh(proc, pgid, deadline, ledger, task, log, cancelled):
    q = queue.Queue()

    def reader():
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        buf = b""
        try:
            while True:
                if not sel.select(.1):
                    continue
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    if buf:
                        q.put(buf.decode(errors="replace"))
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    q.put(line.decode(errors="replace"))
        finally:
            sel.close()
            q.put(None)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0}
    usage_bad = False
    step_count = 0
    tools = {}
    policy_denials = []
    final_text = None
    final_session = None
    failure = False
    stopping = None
    eof = False
    while not eof:
        if cancelled.is_set():
            stopping = "CANCELLED"
        elif time.monotonic() >= deadline:
            stopping = "TIMEOUT"
        if stopping:
            stop_group(proc, pgid)
            break
        try:
            line = q.get(timeout=min(.05, max(.001, deadline - time.monotonic())))
        except queue.Empty:
            continue
        if line is None:
            eof = True
            break
        log.write(line + "\n")
        log.flush()
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        typ = event.get("type")
        if typ == "session":
            sid = event.get("sessionId")
            if isinstance(sid, str) and sid and not ledger.get("dsh_session_id"):
                ledger["dsh_session_id"] = sid
        elif typ == "tool_call":
            tools[event.get("callId")] = event.get("tool")
        elif typ == "tool_result" and event.get("status") == "error":
            failure = True
            error = event.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if (isinstance(code, str) and
                    (code.upper().startswith("FS_") or "PERMISSION" in code.upper())):
                policy_denials.append({"tool": tools.get(event.get("callId")), "code": code})
        elif typ == "status" and event.get("phase") == "step_end":
            step_count += 1
            values = event.get("usage")
            if not isinstance(values, dict):
                usage_bad = True
                continue
            parts = {key: values.get(key) for key in
                     ("inputTokens", "outputTokens", "cacheReadTokens",
                      "cacheWriteTokens", "totalTokens")}
            if any(type(value) is not int or value < 0 for value in parts.values()):
                usage_bad = True
                continue
            usage["input"] += parts["inputTokens"]
            usage["output"] += parts["outputTokens"]
            usage["cache_read"] += parts["cacheReadTokens"]
            usage["cache_write"] += parts["cacheWriteTokens"]
            usage["total"] += parts["totalTokens"]
        elif typ == "final":
            final_text = event.get("text")
            final_session = event.get("sessionId")
        elif typ == "error":
            failure = True
    if not stopping:
        proc.wait(timeout=2)
        if group_live(pgid):
            stop_group(proc, pgid)
            failure = True
    thread.join(timeout=.3)
    ledger["policy_denials"] = policy_denials
    if step_count and not usage_bad:
        ledger["usage"] = usage
        ledger["usage_incomplete"] = False
    else:
        ledger["usage"] = None
        ledger["usage_incomplete"] = True
    status = stopping or ("FAILED" if failure or proc.returncode != 0 else "READY_FOR_REVIEW")
    return status, final_text, final_session


def path_digest(path):
    path = Path(path)
    if path.is_symlink():
        return hashlib.sha256(os.readlink(path).encode()).hexdigest()
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    return None


def create_dsh_worktree(project, worktree):
    worktree = Path(worktree)
    private_dir(worktree.parent)
    result = subprocess.run(["git", "worktree", "add", str(worktree), "HEAD"],
                            cwd=project, capture_output=True, text=True, timeout=20)
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[:200]
        raise ValueError(f"git worktree add failed: {detail}")
    return worktree


def remove_dsh_worktree(project, worktree):
    worktree = Path(worktree)
    try:
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                       cwd=project, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)


def dsh_worktree_changes(worktree):
    result = subprocess.run(["git", "-c", "core.quotepath=false", "-C", str(worktree),
                             "status", "--porcelain", "--untracked-files=all"],
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise ValueError(f"git status failed in worktree: {result.stderr.strip()[:200]}")
    changes = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        code, rel = line[:2], line[3:]
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1]
        changes.append((code, rel))
    return changes


def dsh_ignored_artifact(rel):
    parts = rel.rstrip("/").split("/")
    return ("__pycache__" in parts or ".pytest_cache" in parts or
            rel.endswith(".pyc") or rel.endswith(".pyo"))


def apply_dsh_worktree(task, baseline, project, worktree, ledger):
    project = Path(project)
    worktree = Path(worktree)
    allowed = set(task["allowed_files"])
    allow_delete = set(task.get("allow_delete", []))
    out_of_scope, unauthorized, drift, ignored = [], [], [], []
    for code, rel in dsh_worktree_changes(worktree):
        if dsh_ignored_artifact(rel):
            if rel not in ignored:
                ignored.append(rel)
            continue
        if rel not in allowed:
            if rel not in out_of_scope:
                out_of_scope.append(rel)
            continue
        target = project / rel
        current = path_digest(target)
        if current != baseline.get(rel):
            if rel not in drift:
                drift.append(rel)
            continue
        if "D" in code:
            if rel not in allow_delete:
                if rel not in unauthorized:
                    unauthorized.append(rel)
                continue
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            continue
        source = worktree / rel
        if not source.is_file() or source.is_symlink():
            if rel not in out_of_scope:
                out_of_scope.append(rel)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    ledger["worktree_out_of_scope"] = sorted(out_of_scope)
    ledger["worktree_unauthorized_delete"] = sorted(unauthorized)
    ledger["worktree_ignored_artifacts"] = sorted(ignored)
    errors = []
    if out_of_scope:
        errors.append("worktree out-of-scope: " + ", ".join(sorted(out_of_scope)))
    if unauthorized:
        errors.append("worktree unauthorized delete: " + ", ".join(sorted(unauthorized)))
    if drift:
        errors.append("baseline drift: " + ", ".join(sorted(drift)))
    if errors:
        ledger["status"] = "FAILED"
        ledger["error"] = "; ".join(errors)


def _model_fields(value):
    if isinstance(value, dict):
        provider, model = value.get("provider"), value.get("model")
        if (isinstance(provider, str) and provider and
                isinstance(model, str) and model):
            return provider, model
        for item in value.values():
            found = _model_fields(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _model_fields(item)
            if found:
                return found
    return None


def _dsh_session_log_candidates(root, session_id):
    candidates = []
    if root.is_dir():
        # dsh_session_id already carries the "session-" prefix in real event
        # streams; accept both forms so fixtures and reality both resolve.
        dir_name = (session_id if session_id.startswith("session-")
                    else f"session-{session_id}")
        for path in root.glob(f"**/{dir_name}/session.v*.jsonl.zstd"):
            match = re.search(r"session\.v(\d+)\.jsonl\.zstd\Z", path.name)
            if match:
                candidates.append((int(match.group(1)), path))
    return candidates


def verify_dsh_model(ledger, runner, settings):
    ledger.setdefault("unverified", [])
    home = runner.get("dsh_home")
    session_id = ledger.get("dsh_session_id")
    if not isinstance(home, str) or not home or not session_id:
        ledger["actual_model"] = None
        ledger["model_verified"] = False
        ledger["unverified"].append("dsh session log not found: missing dsh_home/session id")
        return
    root = Path(home) / "sessions"
    candidates = _dsh_session_log_candidates(root, session_id)
    if not candidates and root.is_dir():
        deadline = time.monotonic() + DSH_SESSION_LOG_WAIT
        while not candidates and time.monotonic() < deadline:
            time.sleep(DSH_SESSION_LOG_INTERVAL)
            candidates = _dsh_session_log_candidates(root, session_id)
    if not candidates:
        ledger["actual_model"] = None
        ledger["model_verified"] = False
        ledger["unverified"].append(f"dsh session log not found for {session_id}")
        return
    _, path = max(candidates, key=lambda item: item[0])
    try:
        result = subprocess.run(["zstd", "-dc", str(path)], capture_output=True,
                                text=True, timeout=30)
    except FileNotFoundError:
        ledger["actual_model"] = None
        ledger["model_verified"] = False
        ledger["unverified"].append("zstd unavailable")
        return
    except (OSError, subprocess.SubprocessError):
        ledger["actual_model"] = None
        ledger["model_verified"] = False
        ledger["unverified"].append("zstd failed")
        return
    if result.returncode != 0:
        ledger["actual_model"] = None
        ledger["model_verified"] = False
        ledger["unverified"].append(f"zstd failed with exit {result.returncode}")
        return
    found = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        found = _model_fields(entry)
        if found:
            break
    if not found:
        ledger["actual_model"] = None
        ledger["model_verified"] = False
        ledger["unverified"].append(f"model fields not found in {path.name}")
        return
    provider, model = found
    actual = f"{provider}/{model}"
    ledger["actual_model"] = actual
    ledger["model_verified"] = True
    expected = runner.get("model")
    if isinstance(expected, str) and expected and actual != expected:
        ledger["status"] = "FAILED"
        ledger["error"] = f"actual model mismatch: {actual} != {expected}"


def preflight(task, settings):
    if not isinstance(task, dict) or not isinstance(settings, dict):
        return {"status": "BLOCKED", "reasons": ["task/settings must be objects"]}
    runner, errors = resolve_runner(task, settings)
    errors.extend(validate_task(task, settings, runner))
    skills, skill_errors = skill_manifest(settings)
    errors.extend(skill_errors)
    if errors:
        return {"status": "BLOCKED", "reasons": errors}
    project = Path(task["project"]).resolve()
    allowed_files = sorted(task["allowed_files"])
    if not task.get("parent_run") and check_rework_blocked(settings, str(project), allowed_files):
        return {"status": "BLOCKED", "reason": "quality rework_count >= 3 for project; manual review required"}
    operation = task.get("operation", "implement")
    if runner["kind"] == "dsh":
        detected, capabilities, warnings, probe_error = probe_dsh(runner["binary"])
        runner["detected_version"] = detected
        runner["capabilities"] = capabilities
        if warnings:
            runner["warnings"] = warnings
        if probe_error:
            return {"status": "BLOCKED", "reasons": [probe_error], "runner": runner}
        if runner["declared_version"] is not None and runner["declared_version"] != detected:
            return {"status": "BLOCKED", "reasons": [
                f"runner version mismatch: expected {runner['declared_version']}, actual {detected}"],
                "runner": runner}
        missing = [flag for flag in DSH_REQUIRED_FLAGS if flag not in capabilities]
        if missing:
            return {"status": "BLOCKED", "reasons": [
                "dsh capabilities insufficient: " + ", ".join(missing)], "runner": runner}
        if operation == "implement":
            try:
                git(project, "rev-parse", "--verify", "HEAD")
            except (OSError, ValueError, subprocess.TimeoutExpired):
                reasons = ["dsh implement requires git project"]
                try:
                    git(project, "rev-parse", "--git-dir")
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
                else:
                    # Legacy git repo without HEAD: retain the old staging reason.
                    reasons.append("dsh implement not implemented until B23")
                return {"status": "BLOCKED", "reasons": reasons, "runner": runner}
    else:
        detected, capabilities, probe_error = probe_runner(runner["binary"])
        if probe_error:
            return {"status": "BLOCKED", "reasons": [probe_error]}
        if runner["declared_version"] is not None and runner["declared_version"] != detected:
            return {"status": "BLOCKED", "reasons": [
                f"runner version mismatch: expected {runner['declared_version']}, actual {detected}"]}
        runner["detected_version"] = detected
        runner["capabilities"] = capabilities
    try:
        files = snapshot(project, settings)
        info = git_info(project)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"status": "BLOCKED", "reason": str(exc)}
    result = {"status": "ok", "project": str(project), "model": runner["model"],
              "cli": runner["binary"], "runner": runner,
              "git": info, "files": {rel: files.get(rel) for rel in task["allowed_files"]},
              "allowed_files": task["allowed_files"],
              "allow_delete": list(task.get("allow_delete", [])),
              "skills": skills,
              "command_groups": effective_groups(task)}
    if runner["kind"] == "dsh":
        result["warnings"] = runner.get("warnings", [])
    return result


def run(task, settings):
    checked = preflight(task, settings)
    if checked["status"] != "ok":
        return checked
    project = Path(checked["project"])
    allowed_files = sorted(task["allowed_files"])
    if not task.get("parent_run") and check_rework_blocked(settings, str(project), allowed_files):
        return {"status": "BLOCKED", "reason": "quality rework_count >= 3 for project; manual review required"}
    root = state(settings)
    locks = root / "locks"
    private_dir(root)
    private_dir(locks)
    lock = locks / (hashlib.sha256(str(project).encode()).hexdigest()[:16] + ".lock")
    with lock.open("a+") as fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "BLOCKED", "reason": "project lock held"}
        return locked_run(task, settings, checked)


def check_rework_blocked(settings, project_str, allowed_files):
    max_rw = max_quality_rework_count(settings, project_str)
    return max_rw is not None and max_rw >= 3


def max_quality_rework_count(settings, project_str, exclude_rid=None):
    runs_dir = state(settings) / "runs"
    if not runs_dir.is_dir():
        return None
    best = None
    for child in runs_dir.iterdir():
        if not child.is_dir() or not RID.fullmatch(child.name):
            continue
        lp = child / "ledger.json"
        if not lp.is_file():
            continue
        try:
            data = json.loads(lp.read_text())
        except (ValueError, OSError):
            continue
        if exclude_rid and data.get("run_id") == exclude_rid:
            continue
        if data.get("status") not in TERMINAL:
            continue
        if data.get("project") != project_str:
            continue
        if "quality_rework_count" in data:
            value = data.get("quality_rework_count")
            rw = value if type(value) is int else None
        elif data.get("status") == "FAILED":
            value = data.get("rework_count", 0)
            rw = value if type(value) is int else None
        else:
            rw = None
        if rw is not None and (best is None or rw > best):
            best = rw
    return best


def classify_quality_failure(ledger):
    if ledger.get("status") != "FAILED":
        return None
    error = ledger.get("error") or ""
    rules = (
        ("candidate_protocol", ("missing candidate envelope", "invalid candidate envelope")),
        ("candidate_changed_files", ("candidate changed_files mismatch",)),
        ("implement_no_change", ("implement requires actual allowed-file change",)),
        ("review_write", ("review must not modify covered files",)),
        ("out_of_scope", ("out-of-scope changes", "control-plane write")),
        ("content_scan", ("content-scan",)),
        ("test_modified_candidate", ("tests-modified-candidate",)),
        ("usage_model", ("missing usage/model", "actual model mismatch")),
        ("export_failure", ("export failed",)),
        ("verification_test_failure", ("verification tests failed", "test execution failed")),
    )
    for category, markers in rules:
        if any(marker in error for marker in markers):
            return category
    return None


def finalize_quality(ledger, prior, parent):
    category = classify_quality_failure(ledger)
    quality = category is not None
    if parent:
        count = prior if prior is not None else 0
    elif quality:
        count = (prior or 0) + 1
    else:
        count = prior if prior is not None else 0
    ledger["quality_failure"] = quality
    ledger["failure_category"] = category
    ledger["quality_rework_count"] = count
    ledger["rework_count"] = count


def locked_run(task, settings, checked):
    project = Path(checked["project"])
    cli = checked["cli"]
    model = checked["model"]
    kind = checked["runner"]["kind"]
    operation = task.get("operation", "implement")
    rid = uuid.uuid4().hex[:12]
    folder = ledger_path(settings, rid).parent
    private_dir(folder)
    private_dir(folder.parent)
    log = folder / "run.log"
    baseline = snapshot(project, settings)  # baseline under lock
    baseline_texts = text_snapshot(project, baseline)
    watch_before = watch_snapshot(settings)
    control_before = None if project == HERE else control_snapshot(HERE, state(settings))
    sid = task.get("session_id")
    hermes_ref, hermes_lookup_error = hermes_reference(task, settings)
    allowed_files = sorted(task["allowed_files"])
    prior_quality = max_quality_rework_count(settings, str(project), rid)
    groups = effective_groups(task)
    ledger = {"run_id": rid, "status": "RUNNING", "project": str(project), "model": model,
              "operation": operation, "candidate": None,
              "effective_groups": groups, "executed_groups": [],
              "runner_config": checked["runner"],
              "parent_run": task.get("parent_run"), "session_id": sid, "start_time": time.time(),
              "end_time": None, "supervisor_pid": os.getpid(), "supervisor_started": started(os.getpid()),
              "child_pgid": None, "exit_code": None, "log_path": str(log),
              "allowed_files": allowed_files,
              "allow_delete": list(task.get("allow_delete", [])),
              "deleted_files": [],
              "skills_declared": checked["skills"],
              "quality_rework_count": prior_quality if prior_quality is not None else 0,
              "rework_count": prior_quality if prior_quality is not None else 0,
              "quality_failure": False, "failure_category": None,
              "permission_denials": 0, "token_total": 0, "budget_incomplete": False,
              "budgets": {"token_total": 0, "token_limit": task.get("max_tokens", 500000),
                          "budget_incomplete": False},
              "usage": None, "usage_incomplete": True,
              "actual_models": [], "baseline": baseline, "final_fingerprints": None,
              "git_baseline": checked["git"], "modified_files": [], "out_of_scope_changes": [],
              "control_plane_changes": [],
              "error": None, "hermes_start": hermes_snapshot(settings),
              "hermes_task_id": hermes_ref["task_id"] if hermes_ref else None,
              "hermes_task_session_id": hermes_ref["session_id"] if hermes_ref else None,
              "hermes_task_lookup_error": hermes_lookup_error}
    if kind == "dsh":
        ledger.update({"dsh_session_id": None, "policy_denials": [],
                       "actual_model": None, "model_verified": False,
                       "unverified": [], "worktree": None,
                       "worktree_out_of_scope": [],
                       "worktree_unauthorized_delete": [],
                       "worktree_ignored_artifacts": []})
    save_ledger(settings, rid, ledger)
    config = permissions(task, project, Path(settings["workspace_root"]).resolve(), model)
    env = None if kind == "dsh" else executor_environment(config)
    previous = set()
    proc = None
    worktree = None
    cancel_flag = threading.Event()
    old_handlers = {}
    evidence_path = folder / "test_evidence.json"

    def handler(signum, frame):
        cancel_flag.set()  # no blocking work in a signal handler

    try:
        if kind == "dsh":
            env = dsh_environment(checked["runner"])
        if sid and kind != "dsh":
            before = export(cli, sid, project, env, folder / "parent_session.json")
            previous = {msg.get("info", {}).get("id") for msg in before["messages"]}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                old_handlers[sig] = signal.signal(sig, handler)
        if kind == "dsh" and operation == "implement":
            worktree = (Path(tempfile.gettempdir()) /
                        "coding-control-worktrees" / rid).resolve()
            create_dsh_worktree(project, worktree)
            ledger["worktree"] = str(worktree)
            save_ledger(settings, rid, ledger)
        prompt_project = worktree if worktree is not None else project
        candidate = ('CODING_CONTROL_CANDIDATE {"operation":"' + operation +
                     '","status":"READY_FOR_REVIEW|BLOCKED","changed_files":["<exact relative path>"],'
                     '"unverified":["<not verified, including tests>"]}')
        skills_prompt = ""
        if checked["skills"]:
            skills_prompt = ("Skills (read the skill before acting):\n" +
                             "".join(f"- {entry['name']}: {entry['path']}\n"
                                     for entry in checked["skills"]))
        prompt = (f"Goal: {task['goal']}\nModel: {model}\nProject: {prompt_project}\nExact allowed files: " +
                   json.dumps(task["allowed_files"]) +
                   "\nIteration commands: " + json.dumps(groups["iteration"]) +
                   "\nBlock gate: " + json.dumps(groups["block_gate"]) +
                   "\nFinal gate: " + json.dumps(groups["final_gate"]) +
                   "\nFinal gate is executed independently by the controller; all other commands are merely authorized" +
                   "\nPreflight: Git baseline recorded; " +
                   ("continue only the delta from parent run.\n" if sid else "first attempt.\n") +
                   "For file tools, must use absolute paths; do not use bash to create files or directories.\n" +
                   skills_prompt +
                   f"Candidate protocol: operation is {operation}.\n"
                   "The final non-empty text line of this turn's final assistant message must be "
                   "exactly one line:\n" + candidate + "\n"
                   "Set changed_files to the exact allowed relative paths actually changed this turn; "
                   "it must match the real diff exactly, with no duplicates, out-of-scope or "
                   "nonexistent files. List in unverified everything not verified, including tests. "
                   "For operation review do not edit any file and set changed_files to []. "
                   "A candidate status BLOCKED is recorded as BLOCKED and is never upgraded to READY; "
                   "real errors, nonzero exit or export failures take precedence.\n")
        if kind == "dsh":
            permission_mode = "workspace-write" if operation == "implement" else "read-only"
            cmd = [cli, "--profile", "headless", "--json", "--permission-mode",
                   permission_mode, "--approval-policy", "never"]
            if sid:
                cmd.extend(["--session-id", sid])
            cmd.append("-")
        else:
            cmd = [cli, "run", "--pure", "--agent", "build", "--model", model,
                   "--format", "json", "--title", "coding-control", "--dir", str(project)]
            if sid:
                cmd.extend(["--session", sid])
            cmd.append(prompt)
        if kind != "dsh" and checked["runner"].get("sandbox") == "seatbelt":
            sandbox_exec = shutil.which("sandbox-exec")
            if not sandbox_exec:
                raise ValueError("sandbox executable not found")
            sandbox_tmp = folder / "tmp"
            private_dir(sandbox_tmp)
            profile = seatbelt_profile(project, state(settings), sandbox_tmp)
            cmd = [sandbox_exec, "-p", profile, *cmd]
            env = {**env, "TMPDIR": str(sandbox_tmp), "TMP": str(sandbox_tmp)}
        proc = subprocess.Popen(cmd, cwd=prompt_project, env=env,
                                stdin=subprocess.PIPE if kind == "dsh" else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True)
        pgid = proc.pid  # start_new_session makes child's pid the process group id
        ledger["child_pgid"] = pgid
        save_ledger(settings, rid, ledger)
        if kind == "dsh":
            proc.stdin.write(prompt.encode())
            proc.stdin.close()
        with os.fdopen(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
            if kind == "dsh":
                ledger["status"], final_text, final_session = drive_dsh(
                    proc, pgid, time.monotonic() + task.get("timeout_seconds", 300),
                    ledger, task, stream, cancel_flag)
            else:
                ledger["status"] = drive(
                    proc, pgid, time.monotonic() + task.get("timeout_seconds", 300),
                    ledger, task, stream, cancel_flag)
        ledger["exit_code"] = proc.returncode
        if kind == "dsh":
            ledger["actual_model"] = None
            ledger["model_verified"] = False
            dsh_session = ledger.get("dsh_session_id")
            if (isinstance(dsh_session, str) and isinstance(final_session, str) and
                    final_session != dsh_session):
                ledger["status"] = "FAILED"
                ledger["error"] = "dsh session mismatch"
            denials = ledger.get("policy_denials") or []
            if denials:
                ledger["status"] = "FAILED"
                ledger["error"] = "policy-denial: " + ", ".join(
                    entry.get("code") or "" for entry in denials)
            if ledger.get("usage_incomplete") and ledger["status"] == "READY_FOR_REVIEW":
                ledger["status"] = "FAILED"
                ledger["error"] = "missing usage from dsh step_end events"
            if ledger["status"] == "READY_FOR_REVIEW":
                verify_dsh_model(ledger, checked["runner"], settings)
            if ledger["status"] == "READY_FOR_REVIEW":
                candidate, problem = parse_dsh_candidate(final_text, task, operation)
                ledger["candidate"] = candidate
                if problem:
                    ledger["status"] = "FAILED"
                    ledger["error"] = problem
            if ledger["status"] == "READY_FOR_REVIEW" and worktree is not None:
                apply_dsh_worktree(task, baseline, project, worktree, ledger)
        else:
            if ledger["status"] == "READY_FOR_REVIEW" and not ledger["session_id"]:
                ledger["status"] = "FAILED"
                ledger["error"] = "missing real sessionID event"
            if ledger["session_id"]:
                try:
                    data = export(cli, ledger["session_id"], project, env, folder / "session_export.json")
                    usage, models, incomplete, ids = aggregate_usage(data, previous)
                    ledger["usage"], ledger["usage_incomplete"] = usage, incomplete
                    ledger["actual_models"] = sorted(models)
                    ledger["message_ids"] = sorted(ids - previous)
                    if ledger["status"] == "READY_FOR_REVIEW" and (incomplete or models != {model}):
                        ledger["status"] = "FAILED"
                        ledger["error"] = "missing usage/model or actual model mismatch"
                    if ledger["status"] == "READY_FOR_REVIEW":
                        candidate, problem = parse_candidate(data, previous, task, operation)
                        ledger["candidate"] = candidate
                        if problem:
                            ledger["status"] = "FAILED"
                            ledger["error"] = problem
                except (ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                    ledger["error"] = f"export failed: {exc}"
                    if ledger["status"] == "READY_FOR_REVIEW":
                        ledger["status"] = "FAILED"
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        ledger["status"] = "BLOCKED" if not proc and kind != "dsh" else "FAILED"
        ledger["error"] = str(exc)
    finally:
        # Keep cancellation handlers through verification tests and final snapshot.
        if proc and group_live(proc.pid):
            stop_group(proc, proc.pid)
        if proc and ledger["exit_code"] is None:
            ledger["exit_code"] = proc.poll()
        if worktree is not None:
            remove_dsh_worktree(project, worktree)
        try:
            after = snapshot(project, settings)
            ledger["final_fingerprints"] = after
            changed = sorted(k for k in baseline.keys() | after.keys() if baseline.get(k) != after.get(k))
            deleted = sorted(k for k in baseline.keys()
                             if baseline.get(k) is not None and after.get(k) is None)
            allow_delete = set(task.get("allow_delete", []))
            ledger["deleted_files"] = [k for k in deleted if k in allow_delete]
            unauthorized_deleted = [k for k in deleted if k not in allow_delete]
            ledger["modified_files"] = [k for k in changed if k in task["allowed_files"]]
            ledger["out_of_scope_changes"] = [k for k in changed if k not in task["allowed_files"]
                                              and k not in deleted]
            if unauthorized_deleted:
                ledger["status"] = "FAILED"
                ledger["error"] = "deleted-file: " + ", ".join(unauthorized_deleted)
            elif ledger["out_of_scope_changes"]:
                ledger["status"] = "FAILED"
                ledger["error"] = "out-of-scope changes: " + ", ".join(ledger["out_of_scope_changes"])
            if control_before is not None:
                control_after = control_snapshot(HERE, state(settings))
                ledger["control_plane_changes"] = control_plane_delta(project, control_before, control_after, HERE)
                if ledger["control_plane_changes"]:
                    ledger["status"] = "FAILED"
                    ledger["error"] = (ledger["error"] + "; " if ledger["error"] else "") + \
                        "control-plane write: " + ", ".join(ledger["control_plane_changes"])
            candidate = ledger.get("candidate")
            if candidate is not None and ledger["status"] == "READY_FOR_REVIEW":
                declared = sorted(candidate["changed_files"])
                actual = sorted(ledger["modified_files"])
                if operation == "review" and actual:
                    ledger["status"] = "FAILED"
                    ledger["error"] = "review must not modify covered files: " + ", ".join(actual)
                elif declared != actual:
                    ledger["status"] = "FAILED"
                    ledger["error"] = (f"candidate changed_files mismatch: declared={declared} "
                                       f"actual={actual}")
                elif candidate["status"] == "BLOCKED":
                    ledger["status"] = "BLOCKED"
                    ledger["error"] = "candidate reported BLOCKED"
                elif operation == "implement" and not actual:
                    ledger["status"] = "FAILED"
                    ledger["error"] = "implement requires actual allowed-file change"
            content_issues = scan_content_additions(baseline, after, project, baseline_texts)
            if content_issues:
                ledger["status"] = "FAILED"
                entries = []
                for rel, lineno, rule in content_issues[:20]:
                    entries.append(f"{rel}:{lineno} [{rule}]")
                ledger["error"] = (ledger["error"] + "; " if ledger["error"] else "") + \
                    "content-scan: " + ", ".join(entries)
            if ledger["status"] == "READY_FOR_REVIEW":
                try:
                    evidence, _ = run_tests(task, project, folder, cancel_flag, after)
                except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                    evidence = None
                    ledger["status"] = "FAILED"
                    ledger["error"] = f"test execution failed: {exc}"
                if evidence is not None:
                    ledger["executed_groups"] = ["final_gate"]
                    evidence["run_id"] = rid
                    try:
                        post = snapshot(project, settings)
                    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                        post = after
                        ledger["status"] = "FAILED"
                        ledger["error"] = f"post-test snapshot failed: {exc}"
                    ledger["final_fingerprints"] = post
                    changed_after = sorted(k for k in after.keys() | post.keys()
                                           if after.get(k) != post.get(k))
                    evidence["candidate_after"] = post
                    evidence["candidate_changed"] = bool(changed_after)
                    if changed_after:
                        ledger["status"] = "FAILED"
                        ledger["error"] = "tests-modified-candidate: " + ", ".join(changed_after)
                    if evidence["status"] == "cancelled":
                        ledger["status"] = "CANCELLED"
                        ledger["error"] = "cancelled during verification tests"
                    elif evidence["status"] == "timeout":
                        ledger["status"] = "TIMEOUT"
                        ledger["error"] = ("verification tests timed out after "
                                           f"{evidence['test_timeout_seconds']}s")
                    elif evidence["status"] != "passed" and ledger["status"] == "READY_FOR_REVIEW":
                        ledger["status"] = "FAILED"
                        ledger["error"] = "verification tests failed"
                    write_private_json(evidence_path, evidence)
                    ledger["test_evidence_path"] = str(evidence_path)
                    ledger["tests"] = {
                        "status": evidence["status"],
                        "not_run_reason": evidence.get("not_run_reason"),
                        "candidate_before": after,
                        "candidate_after": post,
                        "candidate_changed": bool(changed_after),
                        "commands": [{key: command.get(key) for key in
                                      ("argv", "cwd", "started_at", "ended_at",
                                       "exit_code", "log_path", "status")}
                                     for command in evidence["commands"]],
                    }
            else:
                reason = "not run: " + (ledger.get("error") or f"executor status {ledger['status']}")
                evidence = {
                    "run_id": rid,
                    "test_timeout_seconds": task.get("test_timeout_seconds", DEFAULT_TEST_TIMEOUT),
                    "runtime_dir": str(folder / "env"),
                    "started_at": time.time(),
                    "ended_at": time.time(),
                    "status": "not_run",
                    "not_run_reason": reason,
                    "candidate_before": after,
                    "candidate_after": None,
                    "candidate_changed": None,
                    "planned_commands": [list(argv) for argv in groups["final_gate"]],
                    "commands": [],
                }
                write_private_json(evidence_path, evidence)
                ledger["test_evidence_path"] = str(evidence_path)
                ledger["tests"] = {"status": "not_run", "not_run_reason": reason,
                                   "candidate_before": after, "candidate_after": None,
                                   "candidate_changed": None, "commands": []}
            if control_before is not None:
                control_final = control_snapshot(HERE, state(settings))
                final_changes = control_plane_delta(project, control_before, control_final, HERE)
                if final_changes:
                    ledger["control_plane_changes"] = final_changes
                    message = "control-plane write: " + ", ".join(final_changes)
                    if message not in (ledger["error"] or ""):
                        ledger["error"] = ((ledger["error"] + "; ") if ledger["error"] else "") + message
                    if ledger["status"] == "READY_FOR_REVIEW":
                        ledger["status"] = "FAILED"
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            ledger["status"] = "FAILED"
            ledger["error"] = f"final snapshot failed: {exc}"
        finally:
            for sig, old in old_handlers.items():
                signal.signal(sig, old)
            apply_external_writes(ledger, watch_before, watch_snapshot(settings))
            ledger["hermes_end"] = hermes_snapshot(settings)
            finalize_quality(ledger, prior_quality, bool(task.get("parent_run")))
            ledger["budgets"] = {
                "token_total": ledger.get("token_total", 0),
                "token_limit": task.get("max_tokens", 500000),
                "budget_incomplete": bool(ledger.get("budget_incomplete", False)),
            }
            ledger["end_time"] = time.time()
            save_ledger(settings, rid, ledger)
    return {k: ledger[k] for k in ("run_id", "status", "log_path", "exit_code",
                                   "modified_files", "error", "test_evidence_path")}


def text_snapshot(project, fingerprints):
    texts = {}
    project = Path(project)
    for rel, digest in fingerprints.items():
        if digest is None or sensitive(rel):
            continue
        path = project / rel
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        if hashlib.sha256(text.encode()).hexdigest() == digest:
            texts[rel] = text.splitlines(keepends=True)
    return texts


def scan_content_additions(baseline, after, project, baseline_texts=None):
    project = Path(project)
    seen_issues = set()
    result = []

    for rel in sorted(after.keys()):
        before_hash = baseline.get(rel)
        after_hash = after.get(rel)
        if before_hash == after_hash:
            continue
        target = project / rel
        if sensitive(rel) or not target.is_file() or target.is_symlink():
            continue
        new_lines = []
        old_lines = []
        if before_hash is None:
            try:
                new_lines = target.read_text().splitlines(keepends=True)
            except UnicodeDecodeError:
                result.append((rel, 0, "binary-change"))
                continue
            added_line_nos = [i for i, line in enumerate(new_lines, 1) if line.strip()]
        else:
            try:
                new_lines = target.read_text().splitlines(keepends=True)
            except UnicodeDecodeError:
                result.append((rel, 0, "binary-change"))
                continue
            old_lines = (baseline_texts or {}).get(rel)
            if not old_lines:
                old_lines = _read_file_version(target, before_hash)
            if old_lines is None:
                added_line_nos = [i for i, ln in enumerate(new_lines, 1) if ln.strip()]
            else:
                added_line_nos = _diff_lines(old_lines, new_lines)
                removed = _removed_assertions(old_lines, new_lines)
                if removed:
                    result.append((rel, removed[0], "removed-assertion"))
        if not added_line_nos and not any(item[0] == rel for item in result):
            continue
        secret_issues = scan_file(target)
        for issue in secret_issues:
            lineno = _issue_line(issue)
            if lineno in added_line_nos:
                key = (rel, lineno, issue)
                if key not in seen_issues:
                    seen_issues.add(key)
                    rule = issue.split("]")[0].split("[", 1)[1] if "[" in issue else "secret"
                    result.append((rel, lineno, rule))
        downgrade_fails, _ = scan_downgrade(target, set(added_line_nos))
        for issue in downgrade_fails:
            lineno = _issue_line(issue)
            key = (rel, lineno, "downgrade")
            if key not in seen_issues:
                seen_issues.add(key)
                result.append((rel, lineno, "downgrade"))
    return result


def _read_file_version(path, target_hash):
    path = Path(path)
    if not path.is_file():
        return None
    content = path.read_text()
    if hashlib.sha256(content.encode()).hexdigest() == target_hash:
        return content.splitlines(keepends=True)
    return None


def _removed_assertions(old_lines, new_lines):
    removed = _diff_lines(new_lines, old_lines)
    hits = []
    for lineno in removed:
        text = old_lines[lineno - 1]
        if re.search(r"^\s*(assert\b|expect\(|self\.assert)", text):
            hits.append(lineno)
    return hits


def _diff_lines(old_lines, new_lines):
    old_stripped = [l.rstrip("\n").rstrip("\r") for l in old_lines]
    new_stripped = [l.rstrip("\n").rstrip("\r") for l in new_lines]
    added_line_nos = []
    old_counter = {}
    for line in old_stripped:
        old_counter[line] = old_counter.get(line, 0) + 1
    new_found = {}
    for i, line in enumerate(new_stripped, 1):
        before = new_found.get(line, 0)
        old_count = old_counter.get(line, 0)
        if before < old_count:
            new_found[line] = before + 1
        else:
            added_line_nos.append(i)
    return added_line_nos


def cancel(settings, rid):
    info = load_ledger(settings, rid)
    if not info or info.get("status") != "RUNNING":
        return {"status": "BLOCKED", "reason": "run not running"}
    pid = info.get("supervisor_pid")
    if not isinstance(pid, int) or not info.get("supervisor_started") or started(pid) != info["supervisor_started"]:
        return {"status": "BLOCKED", "reason": "stale or unverified supervisor PID"}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {"status": "BLOCKED", "reason": "supervisor disappeared"}
    until = time.monotonic() + 4
    while time.monotonic() < until:
        final = load_ledger(settings, rid)
        if final and final["status"] in TERMINAL:
            return {"run_id": rid, "status": final["status"]}
        time.sleep(.05)
    return {"run_id": rid, "status": "CANCEL_REQUESTED"}


def usage(settings, rid):
    info = load_ledger(settings, rid)
    if not info:
        return {"status": "BLOCKED", "reason": "run not found"}
    chain = {}
    current = info
    known = set()
    while current and current["run_id"] not in known:
        known.add(current["run_id"])
        if current.get("usage") is not None:
            for k, v in current["usage"].items():
                chain[k] = chain.get(k, 0) + v
        parent = current.get("parent_run")
        current = load_ledger(settings, parent) if parent and RID.fullmatch(parent) else None
    incomplete = any(load_ledger(settings, r).get("usage_incomplete", True) for r in known)
    ref = ({"task_id": info["hermes_task_id"], "session_id": info["hermes_task_session_id"]}
           if info.get("hermes_task_id") and info.get("hermes_task_session_id") else None)
    if ref:
        from hermes_usage import summarize
        hermes_usage = summarize(ref["task_id"], ref["session_id"])
        if hermes_usage["status"] == "complete":
            hermes_usage["status"] = "active_task_may_grow"
    else:
        hermes_usage = info.get("hermes_task_lookup_error")
    return {"status": info["status"], "run_id": rid, "usage": info.get("usage"),
            "chain_usage": None if incomplete else chain, "usage_incomplete": info.get("usage_incomplete", True),
            "actual_models": info.get("actual_models", []), "cost": None,
            "hermes": info.get("hermes_end") or info.get("hermes_start") or hermes_snapshot(settings),
            "hermes_task": ref, "hermes_usage": hermes_usage}


def load_batch(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        return None, f"invalid batch: {exc}"
    if not isinstance(data, dict):
        return None, "invalid batch: expected object"
    jobs = data.get("jobs", 1)
    if type(jobs) is not int or not 1 <= jobs <= 4:
        return None, "invalid jobs"
    tasks = data.get("tasks")
    if (not isinstance(tasks, list) or not tasks or
            any(not isinstance(item, str) or not item for item in tasks)):
        return None, "invalid batch: tasks must be a nonempty path array"
    return (jobs, tasks), None


def batch(path, settings):
    spec, problem = load_batch(path)
    if problem:
        return {"status": "blocked", "reason": problem}, 2
    jobs, paths = spec
    tasks, checked = [], []
    for item in paths:
        try:
            task = json.loads(Path(item).read_text())
        except (OSError, ValueError) as exc:
            return {"status": "blocked", "reason": f"invalid batch: {item}: {exc}"}, 2
        if not isinstance(task, dict):
            return {"status": "blocked", "reason": f"invalid batch: {item}"}, 2
        prechecked = preflight(task, settings)
        if prechecked["status"] != "ok":
            return {"status": "blocked", "runs": []}, 1
        tasks.append(task)
        checked.append(prechecked)
    results = [None] * len(tasks)
    seen = set()
    scheduled = []
    for index, prechecked in enumerate(checked):
        project = prechecked["project"]
        if project in seen:
            results[index] = {"task": paths[index], "status": "BLOCKED", "run_id": None,
                              "error": "project already scheduled"}
        else:
            seen.add(project)
            scheduled.append(index)
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {index: pool.submit(run, tasks[index], settings) for index in scheduled}
        for index, future in futures.items():
            try:
                outcome = future.result()
            except Exception as exc:
                outcome = {"status": "FAILED", "error": str(exc)}
            results[index] = {"task": paths[index], "status": outcome.get("status"),
                              "run_id": outcome.get("run_id"), "error": outcome.get("error")}
    status = "ok" if all(run["status"] == "READY_FOR_REVIEW" for run in results) else "failed"
    return {"status": status, "runs": results}, 0 if status == "ok" else 1


def main(argv):
    if len(argv) == 2 and argv[0] == "batch":
        try:
            settings = json.loads(Path(os.environ.get("CODING_CONTROL_SETTINGS",
                                                      HERE / "settings.json")).read_text())
        except (OSError, ValueError) as exc:
            return output({"status": "blocked", "reason": f"invalid batch: {exc}"}, 2)
        result, code = batch(argv[1], settings)
        return output(result, code)
    if len(argv) != 2 or argv[0] not in ("preflight", "run", "cancel", "status", "usage"):
        return output({"status": "BLOCKED", "reason": "usage: preflight|run TASK or cancel|status|usage RUN_ID"}, 1)
    op, argument = argv
    try:
        settings = json.loads(Path(os.environ.get("CODING_CONTROL_SETTINGS", HERE / "settings.json")).read_text())
        if op in ("preflight", "run"):
            task = json.loads(Path(argument).read_text())
            result = preflight(task, settings) if op == "preflight" else run(task, settings)
        else:
            if not RID.fullmatch(argument):
                return output({"status": "BLOCKED", "reason": "invalid run_id"}, 1)
            info = load_ledger(settings, argument)
            if op == "cancel":
                result = cancel(settings, argument)
            elif op == "usage":
                result = usage(settings, argument)
            else:
                result = ({"run_id": argument, "status": info["status"], "log_path": info.get("log_path"),
                           "exit_code": info.get("exit_code")} if info else
                          {"run_id": argument, "status": "NOT_FOUND", "log_path": None})
        return output(result, 0 if result["status"] in ("ok", "READY_FOR_REVIEW", "CANCELLED", "NOT_FOUND")
                      or op in ("status", "usage") and result["status"] != "BLOCKED" else 1)
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        return output({"status": "BLOCKED", "reason": str(exc)}, 1)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
