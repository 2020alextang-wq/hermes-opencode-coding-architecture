# Hermes + OpenCode Coding System — Deployment Guide

- **Audience**: any third-party machine or AI orchestration agent ("Hermes") that wants to run OpenCode CLI as a controlled coding executor
- **Self-contained**: no prior installation assumed; all paths/models/keys are placeholders
- **Origin**: distilled from the internal mechanism guide; contains no revision history or run evidence

## 1. System at a glance

**Hermes plans and accepts; OpenCode only executes.**

- **Four roles**: Macro Planner (Hermes: block decomposition + acceptance criteria) / In-block Planner + Executor (OpenCode) / Checker (Hermes, independent) / Ledger (machine-verifiable evidence)
- **Two trust boundaries**: Hermes never trusts executor self-reports; the executor never receives host secrets (minimal environment injection)
- **Two-level planning**: Hermes decides *what* and *what counts as done*; OpenCode decides *how*. Forbidden: whole-package handoffs, delegating macro planning, micro-managing in-block steps, treating executor returns as conclusions

## 2. Install OpenCode from zero

1. Prerequisites: Node.js >= 20 (`node --version`) and git.
2. Install (either): `npm install -g opencode-ai`, or the official install script (opencode.ai/docs).
3. Verify: `opencode --version` prints a version.
4. Model configuration — prepare three things:
   - `PROVIDER_ID/MODEL_ID` (your coding model identifier)
   - the API key (environment variable, never on disk)
   - the API base URL (needed for custom gateways)
5. Smoke test: `opencode run --pure --model <PROVIDER_ID/MODEL_ID> "reply OK"` returns.

## 3. Controlled-run contract (dispatch layer)

### 3.1 Process shape

```
opencode run --pure --agent build --model <PROVIDER>/<MODEL> \
  --format json --title <block-title> --dir <PROJECT_DIR> [--session <SESSION_ID>] \
  "<PROMPT_TEXT>"
```

- `--pure` isolates host config; all permissions are injected via the `OPENCODE_CONFIG_CONTENT` environment variable (JSON):
  - `permission.edit`: `{"*": "deny", "<rel>": "allow", "./<rel>": "allow", "<abs>": "allow"}` — only the block's allowed_files
  - `permission.bash`: `{"*": "deny", "<exact command>": "allow"}` — authorize iteration commands in BOTH interpreter forms (absolute and relative, e.g. `<PYTHON> -m pytest *` and `./venv/bin/python -m pytest *`)
  - `permission.read`: deny sensitive files (`.env*`, `*auth.json`, ...)
  - `task`/`webfetch`/`websearch`: `deny`
- Minimal child environment: allowlist (PATH/HOME/proxy/TZ) + the key variable; never inherit the full host environment.
- `--format json`: stdout is a JSON event stream (tool calls, text, steps, usage); persist as the run log.

### 3.2 Task package (produced by Hermes)

```json
{
  "project": "<PROJECT_DIR>",
  "operation": "implement | review",
  "goal": "<block goal and acceptance criteria>",
  "allowed_files": ["<exact relative paths>"],
  "allow_delete": ["<authorized deletable relative paths>"],
  "command_groups": {
    "iteration": [["<PYTHON>", "-m", "pytest", "tests/<file>", "-q"]],
    "block_gate": [],
    "final_gate": [["<PYTHON>", "-m", "pytest", "tests/", "-q"]]
  },
  "timeout_seconds": 600,
  "max_tokens": 200000,
  "max_permission_denials": 6
}
```

Notes: `final_gate` accepts only `python -m pytest <paths> [flags]` and is executed solely by the controller for evidence; `iteration`/`block_gate` are for the executor's own use.

### 3.3 Candidate protocol (executor must return)

The final non-empty text line must be:

```
CODING_CONTROL_CANDIDATE {"operation":"<op>","status":"READY_FOR_REVIEW|BLOCKED","changed_files":["<exact relative paths>"],"unverified":["<unverified items>"]}
```

- review operations must set `changed_files` to `[]`; a BLOCKED candidate is never upgraded to READY
- `completed`/exit 0 only proves the turn ended — not task completion, not ACCEPTED

### 3.4 Prompt skeleton

```
Goal: <block goal>
Model: <PROVIDER>/<MODEL>
Project: <PROJECT_DIR>
Exact allowed files: [...]
Iteration commands: [...] (executor's own use)
Final gate: [...] (executed independently by the controller)
Candidate protocol: <format above>
Rules: file tools must use absolute paths; do not create files/dirs via bash;
       output a brief implementation-step list first, then execute
```

## 4. Acceptance (Checker layer)

1. **Snapshot diff**: file hashes before/after; changes must be a subset of allowed_files, else FAILED (out-of-scope); unauthorized deletions FAILED (deleted-file)
2. **Content scan**: added lines scanned for secret shapes and assertion downgrades (removed assertions, always-true assertions, comments replacing assertions)
3. **Test evidence**: final_gate executed by the controller in an isolated environment (private HOME/TMPDIR/XDG); exit code and output recorded
4. **Model consistency**: actual model and token usage parsed from the session export; mismatch or missing usage → FAILED
5. **Three-layer check**: per-block acceptance / cross-block contract re-run / overall alignment with the original plan (doing extra or skipping planned work both count as deviation)
6. **Resume**: micro-reworks resume via `opencode session list` + `--session`; never re-dispatch the whole block

## 5. Pipeline (P0–P6)

P0 archive originals verbatim (secrets redacted first) → P1 number items → P2 per-item four-state pre-check (valid / invalid / already-done / ambiguous — ambiguous items are never dispatched) → P3 cut into cohesive blocks (each block: goal / scope / acceptance criteria / in-block authorization) → P4 dependency order → P5 dispatch with inter-block gates + Check → P6 report per item.

- High-risk blocks (core contracts / permissions / migrations / multi-module coupling) use two-stage dispatch: `--agent plan` (read-only) produces a step list → Hermes reviews → the list is attached verbatim to the build run
- Short report per block: result vs acceptance + deviations

## 6. Behavioral matrix (anti "self-consistent green")

| Scenario | Expected |
|---|---|
| Executor timeout | not READY; process group terminated |
| Cancel | not READY; no residual writes |
| exit 0 without candidate text | FAILED |
| Deleted test assertions to go green | FAILED (removed-assertion) |
| Token budget reached | stop, not READY |
| Unauthorized file deletion | FAILED (deleted-file) |
| Unknown skill / out-of-scope path | BLOCKED |

## 7. Common pitfalls (field-tested, environment-independent)

1. Whitelisting only gate scripts starves the red-green iteration loop; pre-authorize both interpreter forms
2. Executor gives up after a single permission denial → prompt states "denial is not termination; retry with an authorized form"
3. Executor-reported test results are leads only; the Checker re-runs independently
4. Overly narrow mock signatures (`**kw` vs positional) cause false failures; standardize on `*args, **kw`
5. Whole-package handoffs cause multi-round rework; cut cohesive blocks with gates
6. Running the full gate during in-flight edits causes false failures from half-done state; full verification goes in the gaps
7. Every writable file named in the task package's evidence section must be in allowed_files, or the executor hits permission walls at cleanup
8. Fake-CLI green is not real-path green: after changing the runner, smoke-test once with the real CLI

## 8. Minimal acceptance checklist

- [ ] Install verified: `opencode --version`; one real `run` returns
- [ ] Permission injection effective: out-of-scope writes denied; whitelisted commands run
- [ ] Candidate protocol parses; missing candidate → FAILED
- [ ] Snapshot-diff, final-gate, and model-consistency judgments each triggered once for real
- [ ] All 7 behavioral-matrix scenarios covered
