# Hermes + OpenCode Coding Architecture

A two-level coding orchestration system: an AI planner/checker agent (**Hermes**) decomposes requirements into blocks with verifiable acceptance criteria, and a coding executor (**OpenCode CLI**) implements each block inside a hardened permission sandbox. The orchestrator never trusts the executor's self-report — every block is accepted only through snapshot diffs, independent test gates, and content scans.

> Governed by the same architecture as the companion repo `hermes-dsh-coding-architecture` (Hermes + DeepSeek Harness). The governance layer is executor-agnostic; this repo focuses on the OpenCode adapter.

## Why

LLM coding agents are fast but unsafe by default: they over-scope edits, delete tests to go green, leak secrets, and self-report success. This architecture separates **planning and acceptance** (trusted agent) from **execution** (untrusted executor) and makes every claim machine-checkable:

- **Four roles**: Macro Planner (Hermes) / In-block Planner + Executor (OpenCode) / Checker (Hermes, independent) / Ledger (machine-verifiable evidence)
- **Two trust boundaries**: the executor's output is a lead, never a conclusion; the executor receives a minimal environment and never host secrets
- **Two-level planning**: Hermes decides *what* and *when it counts as done*; the executor decides *how*. No whole-package handoffs, no micro-management

## What's in this repo

```
coding_control.py      # single-file controller: preflight/run CLI, permission
                       # injection, snapshot-diff acceptance, content scan,
                       # ledger, budget/rework gates, batch parallel runs
bootstrap.py           # thin alias helper for quick dispatch
task.example.json      # task-package schema example
batch.example.json     # multi-task batch example
tests/                 # full contract suite (fake CLI, behavioral matrix,
                       # delete detection, git gate, sandbox writes, ...)
skills/                # executor-facing skills loaded per run via manifest+hash
                       # (TDD, systematic debugging, project gates,
                       #  verification-before-completion, workspace execution)
tools/git_gate.py      # commit gate: secret/downgrade scan of staged additions
hooks/install.py       # git hook installer
EXECUTOR.md            # executor contract (candidate protocol, rules)
docs/deployment-guide.md  # from-zero deployment guide (install, configure, run)
UPSTREAM-LICENSE       # MIT license of upstream-derived components
```

## Quick start

1. **Prerequisites**: Python 3.11+, git, Node.js >= 20.
2. **Install OpenCode**: `npm install -g opencode-ai` (verify with `opencode --version`).
3. **Configure a model**: pick any provider OpenCode supports; keep the API key in an environment variable, never on disk.
4. **Write a settings file** (see `docs/deployment-guide.md` §3) pointing at your OpenCode binary, model `provider/model`, and a state directory.
5. **Run a review task first** (read-only), then an implement task in a scratch git repo — full walkthrough and the acceptance checklist are in `docs/deployment-guide.md`.

## Core contract (short version)

The executor must end its final message with:

```
CODING_CONTROL_CANDIDATE {"operation":"implement|review","status":"READY_FOR_REVIEW|BLOCKED","changed_files":["..."],"unverified":["..."]}
```

The controller then independently verifies: files changed ⊆ `allowed_files`, no unauthorized deletions, no secrets or assertion-downgrades in added lines, final-gate tests pass in an isolated environment, actual model matches the configured one, and token usage is complete. `completed` and exit code 0 prove nothing by themselves.

## Safety properties

- Deny-by-default edit/bash/read permissions, injected per run
- Snapshot-diff terminal-state judgment (out-of-scope / deleted-file / baseline drift → FAILED)
- Behavioral matrix tests prevent "self-consistent green" (timeout, cancel, missing candidate, removed assertions, budget stop, ...)
- Minimal executor environment; secrets only via environment variables

## License

MIT — see `LICENSE`. `UPSTREAM-LICENSE` preserves attribution for MIT-licensed upstream components.
