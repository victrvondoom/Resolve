# RESOLVE Proof-Carrying MCP Gateway

An MCP server that lets external AI agents request RESOLVE's observation, computer-use and verification
capabilities through one controlled interface. It is an **adapter**, not a second computer engine:

- It calls only routes RESOLVE already serves, over RESOLVE's authenticated HTTP API.
- It never runs a computer action itself (no SendInput, no Playwright, no shell).
- RESOLVE remains the authority for authentication, tenant identity, permissions, risk, approval,
  input, cancellation, evidence and verification.

Everything lives in `integrations/resolve_mcp/`. No file outside this directory was modified.

## Status (read this first)

| Area | Status | How it was measured |
| --- | --- | --- |
| STDIO MCP server, tool/resource/prompt discovery | **Measured** | in-process MCP client and a real `resolve-mcp serve` subprocess (`tests/test_mcp_surface.py`) |
| Authentication, tenant binding, expired/revoked session, rotation | **Measured** | fake RESOLVE and the real RESOLVE app (`tests/test_security.py`, `tests/test_live.py`) |
| Cloud-browser observation (real Chromium frame, URL, title, digest) | **Measured against real RESOLVE + Chromium** | `tests/test_live.py::test_live_cloud_browser_real_frame_and_evidence` |
| Proposal, staleness, target binding, no self-approval | **Measured** | fake and live backends |
| Desktop observation, UI Automation | **Honestly UNAVAILABLE on this Linux host** (RESOLVE returns 409 "Windows control is unavailable"). The Windows path is **not run here**: `WINDOWS_NATIVE_TESTS_NOT_RUN`. | live + fake |
| Action execution | **Not possible yet.** The gateway returns `AWAITING_APPROVAL` and executes nothing, because RESOLVE exposes no route that proves a human approval to a gateway (see *Required RESOLVE interfaces*). | live + fake |
| Host compatibility | Example configs only. **Not tested in Claude Code, Cursor, or VS Code.** | not measured |

## Architecture

```
MCP client (any host)                     resolve-mcp (this package)                   RESOLVE (existing app)
 ── tools/resources/prompts ──STDIO──►  FastMCP server  ──►  ResolveGateway  ──HTTP──►  /api/auth/me          (identity)
                                        • no identity args     • observation registry  /api/sandboxes/{id}/frame
                                        • no approval args     • target registry      /api/windows, /api/uia/*
                                        • bounded output       • proposal store       /api/brief-missions/{id}
                                        • redacted errors      • rate limit            /api/health, /api/readyz
                                                                                         (NOT: approve, execute-by-agent)
```

Proof chain for one operation, and where each link is produced:

```
external request ──► tenant identity (RESOLVE /api/auth/me, bound credential)
                 ──► capability probe (RESOLVE, live)
                 ──► fresh observation (RESOLVE frame/UIA; issued by this gateway, with frame_sha256)
                 ──► target (from that observation only; stable target_id + fingerprint)
                 ──► proposal (intent digest over task digest, observation, target, action)     → AWAITING_APPROVAL
                 ──► authorization (RESOLVE human approval of the exact digest)                 → not available yet
                 ──► execution (RESOLVE only)                                                    → not available yet
                 ──► post-observation + verification (RESOLVE receipt)                           → not available yet
                 ──► ProofPack (JSON Schema validated; VERIFIED only with a RESOLVE receipt)
```

## Requirements

- Python 3.10 or newer. Tested on Python 3.13.18.
- MCP Python SDK **v1 line**, pinned to `mcp>=1.30,<2`. The 2.x line renamed `FastMCP` and changes the server API, so this package does not accept it until it is ported and re-tested.
- `jsonschema` for ProofPack validation.

## Setup

```bash
cd integrations/resolve_mcp
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
resolve-mcp selftest
```

For the optional real-browser live test, install the Playwright build that matches your browser:

```bash
pip install -e ".[browser]"   # playwright==1.63.0; do not run `playwright install` if a browser is already present
```

## Configuration (environment only)

| Variable | Required | Meaning |
| --- | --- | --- |
| `RESOLVE_MCP_BASE_URL` | no (default `http://127.0.0.1:8765`) | RESOLVE origin. `http` only for loopback; `https` for anything else. No path, query or credentials. |
| `RESOLVE_MCP_TOKEN_FILE` | **yes, or the next one** | Path to a file containing one RESOLVE tenant session token (`rst_...`). Must be mode `0600` or stricter. Re-read on every reconnect, so you can rotate it without restarting the host. **Preferred.** |
| `RESOLVE_MCP_TOKEN` | fallback | The same token in an environment variable. Works, but environment variables are more widely visible than a 0600 file. |
| `RESOLVE_MCP_TIMEOUT_S` | no (default 10) | Per-request timeout, 1 to 60 seconds. |
| `RESOLVE_MCP_OBSERVATION_TTL_S` | no (default 60) | How long an observation may be used for a proposal, 5 to 900 seconds. Older observations are `STALE`. |
| `RESOLVE_MCP_RATE_PER_MIN` | no (default 120) | Calls per minute to this gateway, 10 to 6000. |

**Obtaining the token:** sign in to RESOLVE in a browser, or `POST /api/auth/login` with your own account,
then store the returned `token` in a private file: `umask 077; printf '%s' 'rst_...' > ~/.config/resolve-mcp/token`.
Never commit the token, never paste it into a client configuration file that is shared, and rotate it if it leaks.
The package never hardcodes a token, prints one, or puts one into a log, a ProofPack, or a resource.

## Client configuration examples (not tested in these hosts)

These show the shape each host uses. Replace the placeholder paths. Only the MCP SDK's in-process client and a real
STDIO subprocess were tested (see `tests/`). Treat the hosts as untested until you verify them.

* `examples/claude-code.mcp.json`: a project `.mcp.json` entry (copy the `resolve` block into your own file; do not edit the repository root `.mcp.json`).
* `examples/cursor.mcp.json`: a Cursor `mcp.json` entry.
* `examples/generic-host.json`: the common `mcpServers` shape used by other MCP hosts.

All three launch the same command: `resolve-mcp serve`, with credentials supplied through `env`.

## Tools

Every tool returns structured JSON with `ok` and `status`. Identity and approval are never tool arguments.

| Tool | Does | Backed by (RESOLVE) | Measured |
| --- | --- | --- | --- |
| `resolve_status` | service health, mode, readiness | `GET /api/health`, `GET /api/readyz` | live |
| `resolve_get_capabilities` | per-capability probe, no assumed success | `/api/auth/me`, `/api/sandboxes`, `/api/windows`, `/api/uia/availability` | live |
| `resolve_list_windows` | window titles (untrusted) | `GET /api/windows` | live: UNAVAILABLE here |
| `resolve_observe_desktop` | window frame + digest | `POST /api/windows/capture` (needs an active mission reference) | fake only; UNAVAILABLE here |
| `resolve_inspect_window` | UIA snapshot, target_ids | `POST /api/uia/inspect` | fake only; UNAVAILABLE here |
| `resolve_find_controls` | search a gateway-issued UIA observation | gateway registry | fake |
| `resolve_observe_cloud_browser` | URL, title, frame time, pause state, frame digest | `GET /api/sandboxes/{id}/frame` (real Chromium) | **live** |
| `resolve_capture_evidence` | reference + digest; image only when asked and within bound | gateway registry | **live** |
| `resolve_propose_action` | CLICK/TYPE proposal bound to an observation and target; `AWAITING_APPROVAL` | gateway store | **live** |
| `resolve_get_action_status` | current ProofPack; marks `STALE` | gateway store | fake + live |
| `resolve_execute_approved_action` | runs only with a RESOLVE-recorded approval; otherwise `AWAITING_APPROVAL` | no approval route exists yet | **live** (refusal path) |
| `resolve_emergency_stop` | RESOLVE's emergency stop (process-wide in this build) | `POST /api/emergency-stop` (demo build only; UNAVAILABLE in production) | fake |

Desktop tools need a RESOLVE mission reference (`mission_id`, `intent_revision`) that RESOLVE itself issues.

## Resources (read-only; they grant nothing)

| URI | Content |
| --- | --- |
| `resolve://status` | RESOLVE health and mode |
| `resolve://session` | session with credential, user id and workspace id redacted to opaque refs |
| `resolve://observations/latest` | metadata of observations this gateway issued (no image bytes) |
| `resolve://proofpacks/{operation_id}` | ProofPack for one operation |
| `resolve://missions/{mission_id}` | RESOLVE mission state (summary) |

## Prompts (guidance only)

`explain_current_screen`, `inspect_application`, `plan_computer_task`, `review_execution_evidence`. Each one tells
the model that observed text is untrusted and that only a human in RESOLVE can approve.

## ProofPack

Schema: `src/resolve_mcp/schemas/proofpack.schema.json` (draft 2020-12, `additionalProperties: false`).
Every pack is validated before it is returned. Main fields:

```text
schema_version     "resolve-proofpack/1"
operation_id       op_<24 hex>
environment        CLOUD_BROWSER | LOCAL_DESKTOP | UNSPECIFIED
status             PROPOSED | AWAITING_APPROVAL | AUTHORIZED | EXECUTING | VERIFIED | FAILED | UNCERTAIN | BLOCKED | CANCELLED | STALE
request            source, task_digest (sha256 of the task text), task_length   — the request, not the result
intent             digest, source, user_approved                               — user_approved stays false unless RESOLVE says so
observation        snapshot_id, captured_at, source=RESOLVE_BACKEND, frame_sha256, uia_snapshot_sha256
target             application, identity (tgt_...), fingerprint (sha256)
authorization      decision (NOT_EVALUATED|PENDING|ALLOWED|DENIED), approval_id, checked_via
execution          action_id, method, route (NOT_REQUESTED|UNAVAILABLE|RESOLVE_BACKEND), started_at, finished_at
verification       status (NOT_RUN|VERIFIED|FAILED|UNCERTAIN), method, post_observation_id
evidence[]         references (observation ids, backend receipt refs)
notes[]            fixed codes: PROPOSAL_ONLY, NO_RESOLVE_APPROVAL_RECORD, OBSERVATION_STALE, INTENT_DIGEST_MISMATCH, ...
```

Rules enforced in code:

- `VERIFIED` requires a `BackendReceipt` from RESOLVE with `verification_status=VERIFIED` and a post-observation id. Otherwise `BLOCKED VERIFICATION_NOT_GROUNDED`. A client argument cannot set it.
- Free text never enters a pack. Only digests, ids, enums and fixed note codes do.
- The typed text of a TYPE action is stored as a digest only.

Structure is shown above; values in a real pack come from a real observation, never from a template.

## Security model

| Concern | Control |
| --- | --- |
| Missing or invalid credential | refused at startup with a clear message; never falls back to a demo tenant |
| Credential exposure | 0600 file or env var; redacted `repr`; never logged, returned, or placed in a pack; the client never follows redirects with it |
| Expired or revoked session | local expiry check; backend 401 triggers one re-read of the credential, then `AUTH_INVALID` |
| Tenant spoofing | no identity parameters on any tool; identity comes from RESOLVE for the bound credential |
| Forged observation | observation, target and operation ids are issued only by this process; unknown ids are refused |
| Stale observation | observation TTL; proposals refused as `STALE`, and existing packs are re-marked `STALE` |
| Target ambiguity | a target must exist in the observation it is proposed against |
| Prompt injection from a page or window | observed text is size-bounded, control-character-stripped, labelled `UNTRUSTED_OBSERVATION_TEXT`, and never placed in a decision field |
| Self-approval | no tool approves; `resolve_execute_approved_action` refuses unless RESOLVE records a human approval for the exact digest |
| Digest mismatch | `BLOCKED INTENT_DIGEST_MISMATCH` |
| Network exposure | no listening port; STDIO only; stdout carries protocol frames only; logs go to stderr |
| Output and resource bounds | response size, frame size, inline image size, output strings, list lengths, timeouts, rate limit |
| Arbitrary navigation or files | not exposed: no URL navigation tool, no file-read tool, no shell |

## Required RESOLVE interfaces (for the main application team)

The gateway is complete for what RESOLVE already serves. To move from `AWAITING_APPROVAL` to a verified execution, RESOLVE needs these, which were **not** added here:

1. **Tenant-scoped approval record lookup** for an external-agent operation: return the approval state and approval id for an `intent_digest`, scoped to the authenticated user and workspace. Today `/api/approvals` is global and disabled in production.
2. **An execution route that accepts only a RESOLVE approval reference** (not a boolean and not the digest alone) and returns an action id with a post-action observation id and a verification status.
3. **Tenant-scoped desktop routes.** Today `/api/windows*`, `/api/uia/*` and `/api/emergency-stop` depend on a global demo ledger and are disabled in production.
4. **A capability endpoint** that reports what this session may do, rather than the product catalog at `/api/capabilities`.

## Tests

Offline (no RESOLVE needed): fake RESOLVE over real HTTP.

```bash
pytest tests -q   # live tests skip themselves unless the RESOLVE_LIVE_* variables are set
```

Live (requires a RESOLVE you started yourself; the tests create one sandbox for the given account):

```bash
# terminal 1: RESOLVE in demo mode, with Playwright available for the Chromium test
RESOLVE_PORT=8800 python src/resolve_app.py
# terminal 2
export RESOLVE_LIVE_BASE_URL=http://127.0.0.1:8800
export RESOLVE_LIVE_TOKEN="$(cat ~/.config/resolve-mcp/token)"
pytest tests/test_live.py -q
```

The live tests need a server with Playwright installed. Without it, the observation test reports the backend's
"Playwright is not installed" error and fails rather than passing.

Self-test against a running RESOLVE:

```bash
RESOLVE_MCP_BASE_URL=http://127.0.0.1:8800 RESOLVE_MCP_TOKEN_FILE=~/.config/resolve-mcp/token resolve-mcp selftest
```

## Known limitations

- Desktop (Windows) behaviour is unmeasured in this environment: `WINDOWS_NATIVE_TESTS_NOT_RUN`.
- Execution is not implemented: no RESOLVE route exists that the gateway may call (see above).
- Host configurations are examples, not tested integrations.
- The observation registry is in memory and bounded (64 entries). Restarting the gateway forgets observations, so
  proposals must be made again.
- The emergency stop is process-wide in this RESOLVE build, not per tenant.
