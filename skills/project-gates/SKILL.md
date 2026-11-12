---
name: project-gates
description: Use when selecting or running project-specific checks for any repository; isolate external side effects and report skipped gates honestly.
---
# Project Gates
- The project's own AGENTS.md, README, development docs, and current code are the source of truth; do not treat old remembered ports or versions as the current state.
- Data-driven projects: first confirm metric definitions and pure-computation tests; when databases / data collection / scheduling are involved, use the authorized isolated databases and data. Offline adapters passing does not mean the whole site's business logic passes.
- Service projects: verify authentication and scopes, frontend-backend integration, database isolation, and version synchronization; follow the project's existing static / regression gates.
- Any project: specify the test list up front; accidentally scanning production databases or running repo-wide tests with side effects is forbidden. Establish a baseline first, then targeted regression, then widen scope only with permission.
- When browser acceptance testing is required, confirm the real execution tool is actually available; verify clicks, error states, and recovery; merely installing a Skill does not confer browser capability.
- Report the actual file hashes of candidates, the commands run, exit codes, pass/fail/skipped results, and unverified boundaries. Keep acceptance independent; never substitute the model's self-report for evidence.
