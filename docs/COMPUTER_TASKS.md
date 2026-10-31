# Generalized computer tasks

`src/computer_tasks.py` runs a small, documented set of tasks through the typed tool registry against a real Chromium session. It is a bounded rule-based planner, **not** an unrestricted agent.

| Command shape | Task | Consequential |
|---|---|---|
| "Open Project Atlas and show my overdue tasks." | `overdue_tasks` | no |
| "Find the latest project report and summarize it." | `latest_report` (extractive summary, labelled) | no |
| "Update task 'X' to done in Project Atlas" | `update_task` | yes: proposal, then approval of the exact digest |
| "Open the research portal ... save a report to my workspace." | `save_report` | yes |
| "Find Alex from Finance, not Alex from Sales." | `find_person` (refuses when ambiguous) | no |

Properties tested with real Chromium (`tests/test_computer_tasks.py`) and over HTTP (`tests/test_computer_api.py`):
- Every page action is a registry call (validation, capability, timeout, evidence). Page text cannot grant a capability.
- Targets are resolved from visible context and bound by stable id and observed version. A proposal changes nothing; it carries a digest of the exact arguments and version. A wrong digest, or a target that changed after approval, is refused (`APPROVAL_MISMATCH`) and nothing is written.
- Success is `CONFIRMED` only when an independent verifier reading the application's own database agrees; a write the verifier cannot see is `UNCERTAIN`.
- Mutation-checked: removing the digest comparison fails two tests; removing the database comparison fails one.
- `/api/computer/run` gives each tenant its own controlled-app instance and database (own loopback port, own file), loopback-only origin allowlist, and is gated like diagnostics (`RESOLVE_DIAGNOSTICS=1` in production). It is not production-grade per-tenant VM isolation.

The apps are fictional and local. This proves the tool layer and the continuity guards on three differently-shaped sites, not that arbitrary websites work.
