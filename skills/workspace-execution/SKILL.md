---
name: workspace-execution
description: Use at the start of an authorized Hermes task to respect workspace boundaries, prepared environments, narrow permissions and concise handoff.
---
# Controlled Programming Execution
1. Read the task package and the project's AGENTS.md; confirm the real working directory, the baseline, allowed files, and any existing changes.
2. Pre-checks are performed by deterministic tools; use the given interpreter and commands. Prefer native file tools; do not re-probe or install redundantly.
3. The implementation approach is left to the model's judgment; write scope, cancellation, concurrency mutual exclusion, and budgets are the runner's responsibility — the Skill is not a sandbox.
4. After a permission denial, do not bypass it and do not retry in a loop; if necessary report BLOCKED and state the minimal permissions needed. Never switch models or invoke additional roles on your own.
5. Same-task reviews stay in the original session and handle only the diff; do not reuse context across projects.
6. Unauthorized commit/tag/push, deployment, or migration of real databases is forbidden. On completion, output READY_FOR_REVIEW with evidence; this does not replace Hermes acceptance.
