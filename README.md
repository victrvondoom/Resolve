<p align="center">
<img width="632" height="631" alt="image" src="https://github.com/user-attachments/assets/993fdb9c-34d9-41af-9ac2-df77b08cfcbc" />

</p>

<p align="center">
  <img src="docs/readme/intro.gif" alt="RESOLVE intro video: two sculpted blocks enter from opposite sides, assemble, and reveal RESOLVE, Wispr &amp; Solve, the Wispr Flow mark and the WeMakeDevs challenge" width="86%">
  <br>
  <a href="docs/readme/intro.mp4">

<h1 align="center">RESOLVE</h1>
<p align="center"><b>RESOLVE — Inbox-to-Commitment Operator</b><br><i>FROM INBOX NOISE TO VERIFIED FOLLOW-THROUGH.</i></p>

---

## Who it is for and what problem it solves

**Who:** independent developers, indie founders and small technical teams who run their work through email.

**Problem:** important email quietly creates obligations. Someone asks you for a deliverable by a time. You promise something in a reply and forget it. A colleague promises you a file and never sends it. Inbox tools show messages; they do not track who owes what, by when, and whether it actually happened.

**Promise:** RESOLVE turns the conversation into an obligation, shows you what needs a decision, prepares a reply you can review, and sends only the exact text you approved. It reads the sent message back from Gmail and records only what it could verify.

## What is built

1. **Morning inbox.** The default screen. Summary cards and sections (needs attention, replies to send, what you promised, what you are waiting for, upcoming and overdue, drafts to review, recently resolved), all computed from stored records.
2. **Inbox intelligence.** Complete-thread triage with the sentence that caused each decision, linked to its message. Vague deadlines are flagged, not invented. Automated notices create no commitments.
3. **Commitment ledger.** Requests to you, your promises, and promises made to you are kept separate. Each has an owner, a requester, a deadline in your timezone, its exact source sentence, revisions, and a validated status. Verified completion and your own confirmation are recorded differently.
4. **Wispr Flow-ready voice workflow.** A Voice Rules page with a large instruction field. Wispr Flow (or any dictation tool) types into ordinary text fields; RESOLVE receives the text, shows a rule preview, activates only what you approve, and applies corrections with before, after and impact. RESOLVE has no Wispr Flow integration and does not claim one.
5. **Drafting and approval.** Replies are prepared from the thread and from facts you supply; anything missing is listed, not invented. Approval applies to the exact recipient, subject, body and digest, is single-use, and expires after 15 minutes. Any edit invalidates it.
6. **Verified execution.** A send is confirmed only when the stored message is read back and matches the approved content. A timeout is UNCERTAIN, shown as not successful, and reconciled read-only; nothing is resent automatically.
7. **Replay mode.** A labelled synthetic morning (12 threads, 16 messages) runs through the same code as the live mailbox. Sends are simulated and labelled as such.
8. **Local mail mode.** A real mail server on your machine (SMTP + POP3 on loopback, Maildir on disk) that RESOLVE starts for you. Create a mailbox, receive messages from local colleague addresses, approve a reply, and RESOLVE delivers it over SMTP and verifies it by reading the Sent folder back over POP3. It delivers only to `@mail.resolve.test` addresses and is never presented as Gmail.
9. **Optional model endpoint.** `RULES_ONLY` by default and labelled so. With `RESOLVE_MODEL_BASE_URL` and `RESOLVE_MODEL_NAME` (Ollama, llama.cpp or any OpenAI-compatible server), RESOLVE can ask a model for an advisory thread analysis or a reply draft; answers are schema-validated, every quote is checked against the stored message, and drafts with unsupported prices, dates or promises fall back to the rules draft. No model was run while building this repository.

## Screens

| | |
|---|---|
| ![Morning brief after replay](docs/screenshots/inbox/01_morning_brief_replay.png) | ![Inbox](docs/screenshots/inbox/02_inbox_replay.png) |
| Morning brief (synthetic data) | Inbox with filters (synthetic data) |
| ![Commitments](docs/screenshots/inbox/04_commitments_replay.png) | ![Voice rules preview](docs/screenshots/inbox/06_voice_rules_preview.png) |
| Commitment ledger (synthetic data) | Voice rules: parsed preview (synthetic data) |
| ![Draft editor](docs/screenshots/inbox/07_draft_editor_replay.png) | ![Exact approval](docs/screenshots/inbox/08_approval_exact_content_replay.png) |
| Draft editor with grounding (synthetic data) | Exact-content approval (synthetic data) |
| ![Replay lab](docs/screenshots/inbox/09_replay_lab.png) | ![Simulated send and timeline](docs/screenshots/inbox/10_simulated_send_and_timeline.png) |
| Replay lab (synthetic data) | After a simulated send (synthetic data) |

All screenshots were captured from the running app by `python scripts/capture_inbox_screens.py` using REPLAY data only. Each screen is labelled synthetic.

## Gmail architecture

- OAuth 2.0 authorization code flow with PKCE (S256). The verifier stays on the server. State is single-use, expires in 10 minutes, and is bound to the user who started it.
- Read-only by default (`gmail.readonly`). `gmail.compose` (drafts) and `gmail.send` are requested only when you enable them.
- Tokens are sealed at rest and never returned by any route. Revocation and rate limits are shown as states, never as success.
- The Gmail adapter is the only code that performs provider HTTP. It is injected, so every test uses a recorded or scripted transport.

## Security

- Email text is untrusted data, sealed at rest with per-tenant keys. Instruction-like sentences are excluded from obligations and routed to human review.
- Tenant isolation on every store call; cross-user and cross-workspace access returns 404.
- Approval is bound to the exact digest, single-use, expiring, and refused after any change.
- Outgoing messages contain only the approved text. Correlation uses an RFC 822 Message-ID header, never a marker in the body.
- Duplicate-send protection is mutation-tested: removing both protections makes the same approved content send twice.
- Model output is never trusted with permissions; no model is configured in this build.

Details: `docs/INBOX_SECURITY.md`. Known limitations are listed there. The database file is not fully encrypted; only sensitive content fields are sealed.

## Local mail mode

No credentials needed, real delivery on this machine. **Connections → Local mail server → Create local mailbox → Deliver a message → Sync local mailbox**, then switch the Morning brief to **Local mail**. Command-line server and accounts: `python src/local_mail.py --help`. Details in `docs/INBOX_OPERATOR_SETUP.md`.

## Replay mode

No credentials needed. **Replay lab → Start → Step**. Every synthetic message is labelled, uses the reserved test domain `replay.example.test`, and is stored only in a separate replay workspace. Reset removes replay data only.

## Benchmarks

Measured on labelled fixtures in this repository, not on real inboxes. See `docs/INBOX_BENCHMARK_REPORT.md` (generated by `python benchmarks/inbox_metrics.py`).

- Classification: 9 of 12 earlier fixtures fully passing (the three failures are kept visible, not tuned away). 31 of 32 new scenarios classified correctly, written before scoring; the one miss (V32) is a label we consider wrong on review and kept as written; see the report.
- Owners: 100% of matched commitments. Ambiguous deadlines flagged: 2 of 2. Re-processing the same thread creates no new commitments.
- Send path (simulation with a scripted provider double): no false successes and no duplicate sends in six scenarios.
- **Not measured:** live Gmail, and any model-assisted path (none configured).

## Setup

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
python src/resolve_app.py        # open http://127.0.0.1:8765/app
```

Replay works immediately. For live Gmail, see `docs/INBOX_OPERATOR_SETUP.md` (three OAuth variables and an authorized test account).

## Configuration

| Variable | Purpose |
|---|---|
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI` | Live Gmail. Without them the app shows GMAIL NOT CONFIGURED |
| `RESOLVE_MASTER_KEY` | Encryption key. Required in production |
| `RESOLVE_INBOX_DB` | Inbox database path (default `data/inbox.sqlite3`) |
| `RESOLVE_PUBLIC_BASE` | Base URL for OAuth redirects |

Set your timezone in Privacy & settings. Until you do, the app says UTC is in use.

## Known limitations

- **Live Gmail is not verified** in this repository. Google's handling of the Message-ID correlation and of `rfc822msgid:` search must be confirmed with a test account; if it differs, read-back fails closed.
- **No model is configured.** The engine is RULES_ONLY and is labelled as such. Model-assisted understanding and drafting are not implemented.
- **Wispr Flow was not run here.** The dictation workflow is designed for ordinary text fields; the demonstration recording is user-owned (`docs/WISPR_DEMO_SCRIPT.md`).
- Follow-ups are recorded but not delivered. The unread filter is not offered. Pending OAuth state is in memory.
- Instruction detection is a phrase list and is not a security boundary.
- **Demo video:** there is no operator video yet. The intro video below was made for an earlier landing page and is kept for reference.

## Demo video

No operator demonstration video has been produced. The original intro (earlier landing page): [▶ Watch the intro video (MP4)](docs/readme/intro.mp4).

## Documentation

- `docs/INBOX_OPERATOR_DEMO.md` — walk-through
- `docs/INBOX_API.md` — routes
- `docs/INBOX_SECURITY.md` — security model and limitations
- `docs/INBOX_OPERATOR_SETUP.md` — setup and Gmail configuration
- `docs/WISPR_DEMO_SCRIPT.md` — dictation demonstration (user-owned recording)
- `docs/INBOX_BENCHMARK_REPORT.md` — measured results
- `docs/INBOX_OPERATOR_TRACKER.md` — what is done, partial, blocked

<details>
<summary>Earlier product description (secondary: the personal AI cloud computer and Windows-first automation)</summary>

## Start here (60 seconds)

1. **Say or type one goal.** For example: *"Read my project brief, check my web app for errors, and make a report."*
2. **RESOLVE works in a private cloud browser.** Three helpers run at the same time: one reads your brief, one checks your code, one tests your web app.
3. **You approve one plan.** You see exactly which file, which version and what it will produce. Nothing happens without your yes.
4. **RESOLVE writes the PDF, then checks the PDF itself.** It confirms the file is the one you approved, built from the versions you picked.
5. **You download, print or delete it.** Your files stay in your own private space.

> RESOLVE is a working prototype. Some parts are real today and some are simulated. The section **"What is real and what is simulated"** says exactly which.

```mermaid
flowchart LR
    You([You: say or type a goal]) --> Site[RESOLVE website]
    Site --> Plan{{Plan to approve}}
    subgraph Helpers [Helpers working at the same time]
      Reader[Reads your brief]
      Checker[Checks your code]
      Tester[Tests your web app in a private browser]
    end
    Plan --> Helpers
    Helpers --> Report[Writes the PDF report]
    You -. approves the exact plan .-> Report
    Report --> Verify[Checks the file itself]
    Verify --> Done([Verified result: download, print or delete])
    Verify --> Record[(Private record of every step)]
```

## What it does for you

| You want to | RESOLVE does | You can see |
|---|---|---|
| Understand a project brief | Reads the document and lists its requirements | Each requirement, with its source |
| Find problems in code | Scans the code and its web app | Each problem, with file and line |
| Get a corrected design | Proposes a fixed architecture diagram | The diagram, before you approve it |
| Get a report | Writes a PDF from your approved plan | The file's fingerprint and every check it passed |
| Know it's right | Re-reads the saved file and checks it | A list of checks, and what was *not* checked |

## Architecture, in plain words

```mermaid
flowchart TB
    subgraph Browser [Your browser: the website]
      UI[Mission Control and landing page]
    end
    subgraph Server [RESOLVE server]
      API[Front door: checks who you are]
      Missions[Mission engine: runs the helpers in order]
      Studio[Document studio: stores and makes PDFs]
      Audit[Activity log: every change, every attempt]
    end
    subgraph Cloud [Private cloud browser]
      Chrome[Real Chromium browser]
      App[Controlled demo web app]
    end
    UI -->|requests| API --> Missions
    Missions --> Studio
    Missions --> Chrome --> App
    Studio --> Audit
    Missions --> Audit
```

## Glossary (technical words, explained)

| Word you may see | What it means |
|---|---|
| **Digest / fingerprint** | A short code made from a file or plan. Change one letter and the code changes, so you can prove two things are identical. |
| **SHA-256** | The fingerprint method used for every file and plan. |
| **Approval** | Your yes for one exact plan. If the plan changes, your yes no longer counts. |
| **Revision** | A numbered version of your request. Changing your request makes a new revision. |
| **Ledger / record** | The list of every step, in order, with the time and who did it. |
| **Idempotent** | Safe to repeat: doing it twice still gives one result. |
| **Verified** | RESOLVE re-read the result from storage and it matched what you approved. |
| **Uncertain** | Something may have happened, but RESOLVE cannot prove it. It will not retry on its own. |
| **Cloud browser** | A real web browser that runs on the server, not on your computer. |
| **Sandbox** | A walled-off space for one user. |
| **Connector** | A link to another service (for example Google Drive). Connectors are simulated in this build. |
| **Tenant / workspace** | Your private area. Other people cannot see it. |

## Evidence and numbers

- **Tests:** the full suite passes on the current code: **442 passed, 12 skipped, 0 failed** (the 12 skipped are desktop-window tests that need a display). Run it yourself with the command below.
- **Browser tests:** real Chromium tests check the landing page, the intro, the approval screen, the live viewer and the phone layout.
- **Privacy tests:** users cannot read each other's documents, sandboxes, browser sessions, missions or evidence. These are checked against the running server.
- **Controlled comparison** (a test we wrote, not real-world data): on 4 fault scenarios with 6 random seeds, a plain scripted agent made an unsafe or wrong outcome in **21 of 24** runs; RESOLVE's checks made **0 of 24**. Raw results: `data/benchmarks/`. This shows the checks work on the faults we built; it does not prove general reliability.

## Try it

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt && python -m playwright install chromium
python src/resolve_app.py            # then open http://127.0.0.1:8765
```

Regenerate the screenshots and intro frames from the running app:

```bash
python scripts/capture_readme_screens.py 8771
python scripts/capture_intro_frames.py 8772
```

## What is real and what is simulated

| Part | Status |
|---|---|
| Reading briefs, checking code, writing and re-checking PDFs | **Real** |
| Browser tests against a controlled demo web app | **Real** (a real Chromium browser) |
| Your files and records stored privately | **Real** (on the server's disk) |
| Encryption of stored files | **Real, partial:** AES-256-GCM seals every stored PDF, project archive and evidence frame, with a separate key per user and workspace. Database rows and filenames are **not** encrypted. Production refuses to start without `RESOLVE_MASTER_KEY`. |
| Sending email, calendar or cloud-drive changes | **Simulated** (no real accounts connected) |
| Speech-to-text from a microphone | **Not verified here** (no speech model is installed) |
| Browser sessions on a public server | **Blocked** until isolated infrastructure exists |

The full audit, including what is weak and what we will fix next, is in [`docs/RESOLVE_UI_UX_AUDIT.md`](docs/RESOLVE_UI_UX_AUDIT.md).

---

<details>
<summary><h2 style="display:inline">College cloud sprint (document workflow)</h2></summary>


The browser app now has a Document studio: upload a PDF (up to 5 MiB), rotate pages, remove pages, merge PDFs, create a report from text, preview the generated PDF, download it, or open the browser print dialog. Each operation stores an actual PDF with a SHA-256 receipt. Transformations require the source hash; document reads and writes are scoped to the current account and workspace. Workspace and account deletion purge their stored PDFs. Install `requirements-dev.txt`, run `python src/resolve_app.py`, then open `/app` and choose **Documents & print**. The HTTP and PDF checks are in `tests/test_document_api.py` and `tests/test_document_studio.py`.

This branch is a local server prototype. It binds to loopback, exposes a shared demo session for local use, has no production OAuth, and has no deployed per-user VM isolation. Its `/api/voice/transcribe` route accepts submitted text; it does not transcribe audio. The print dialog cannot prove that a physical printer completed a job. See `docs/COLLEGE_HANDOFF.md` for the implementation boundary and next steps.

**Say what you want done. RESOLVE makes sure it happens to the right person, with the right file, exactly once — and proves it.**

RESOLVE is a local, Windows-first, voice-operable agent system for computer work where *acting correctly* matters more than clicking quickly. Many tools can click, type and read a screen. RESOLVE targets a different failure: an agent that does every click right and still emails the wrong Jordan Lee, attaches last week's report, sends twice after a lost acknowledgement, or acts on an instruction you already corrected — then reports success.

The working hypothesis is **Intent–Entity–Effect Continuity (IEEC)** plus a **Causal State Ledger (CSL)**: carry your intent revision, the stable identity of the target, the exact version of the content and the observed external effect through every step, and refuse or hold whenever they come apart. It is a hypothesis we measure (below), not a claim of novelty or of universal exactly-once delivery.

</details>
<details>
<summary><h2 style="display:inline">Architecture</h2></summary>


RESOLVE is organized as four isolated layers. Each layer only talks to the layer below it, so a compromise of the browser or an agent run can never reach another user's data or bypass the permission core.

```mermaid
flowchart TB
    subgraph APP["RESOLVE Web App"]
        V["Voice · Chat · Live Browser · Missions · Files · Approvals"]
    end

    subgraph ORCH["Authenticated Multi-Agent Orchestrator"]
        O["Planning · Identity · Permissions · IEEC · Causal Ledger"]
    end

    subgraph SBX["Isolated Sandboxes"]
        A["User A Sandbox<br/>Private browser, files, sessions, execution"]
        B["User B Sandbox<br/>Separate browser, files, sessions, execution"]
    end

    subgraph DATA["Private Data & Permission Control"]
        D["Encrypted storage · Scoped connectors · Export · Revoke · Delete"]
    end

    APP --> ORCH --> SBX --> DATA
```

Equivalent plain-text view:

```
┌──────────────────────────────────────────────────────────────────────────┐
│                             RESOLVE Web App                              │
│        Voice · Chat · Live Browser · Missions · Files · Approvals        │
└───────────────────────────────────────┬──────────────────────────────────┘
                                        │
┌───────────────────────────────────────▼──────────────────────────────────┐
│                  Authenticated Multi-Agent Orchestrator                  │
│          Planning · Identity · Permissions · IEEC · Causal Ledger        │
└──────────────┬───────────────────────────────────────┬───────────────────┘
               │                                       │
┌──────────────▼──────────────┐         ┌──────────────▼──────────────┐
│        User A Sandbox       │         │        User B Sandbox       │
│  Private browser, files,    │         │  Separate browser, files,   │
│  sessions, execution        │         │  sessions, execution        │
└──────────────┬──────────────┘         └──────────────┬──────────────┘
               │                                       │
┌──────────────▼───────────────────────────────────────▼──────────────────┐
│                    Private Data & Permission Control                     │
│   Encrypted storage · Scoped connectors · Export · Revoke · Delete       │
└──────────────────────────────────────────────────────────────────────────┘
```

**How the layers work together**

1. **RESOLVE Web App** — the only surface you touch. Voice and chat input, a live browser view of what agents are doing, mission control, file transfer and a human approval queue. Nothing executes here; it only collects intent and displays evidence.
2. **Authenticated Multi-Agent Orchestrator** — the brain. Every request is authenticated, planned, and stamped with an identity and permission scope before any action runs. The **IEEC** check carries your intent revision, the stable entity ID, and the exact content hash through every step; the **Causal Ledger** records every observed effect so a lost acknowledgement becomes `UNCERTAIN` and a reconcile — never a blind resend.
3. **Per-user Sandboxes** — User A and User B get fully separate browsers, files, sessions and execution contexts. An agent operating for User A physically cannot read User B's tabs, cookies, or files; there is no shared state to leak.
4. **Private Data & Permission Control** — the trust root. Data at rest is encrypted, connectors are scoped to the minimum access a mission needs, and you keep irreversible control: export your data, revoke a connector mid-mission, or delete everything. The orchestrator enforces these scopes on every tool call, not just at login.

</details>
<details>
<summary><h2 style="display:inline">What is verified, what is not</h2></summary>


| Capability | Status | Evidence |
|---|---|---|
| Three separate stateful apps (CRM, report store, outbox — **simulated delivery**) driven through a real browser (Playwright, DOM-level) | Tested on Linux | `tests/test_web_g3.py`, `harness/evidence/G3.md` |
| Same-name contacts refused until bound by stable ID; stale report blocked by hash; lost acknowledgement → `UNCERTAIN` → read-only reconcile, never resent | Tested | `tests/test_web_g3.py`, `tests/test_w12_crash_restart.py` (real SIGKILL mid-dispatch) |
| Approvals bound to the exact recipient + content + attachment hash + intent revision; voice-style corrections invalidate them | Tested with typed transcripts | `tests/test_w7_voice_correction.py` |
| 13 tool-bounded agent roles, parallel read-only fan-out, entity-gated planner, verifier separate from executor, retry of timed-out reads, exclusive fenced executor lease | Tested | `tests/test_supervisor_g5.py`, `tests/test_web_g3.py::ConcurrencyTests` |
| Recorded browser skills: record → compile → approve → replay on a fresh app → repair proposal when the UI changes | Tested | `tests/test_web_skills.py` |
| Sandboxed file workflow: inventory, content-hash duplicates, dry-run plan, stale-plan guard, apply, undo across restart | Tested | `tests/test_file_worker.py` |
| Live UI: landing page, Mission Control, SSE event stream from the persisted ledger, reload/restart survival | Tested in headless Chromium | `tests/test_dashboard_e2e.py`, `tests/test_landing_ui.py` |
| **Native Windows**: real cursor, Win32 input, Notepad type+save+disk check, focus-steal refusal, UIA inspection | **Code present, NOT run in this build environment** (`WINDOWS_NATIVE_TESTS_NOT_RUN`) | run `scripts/smoke-windows.ps1` on Windows |
| Microphone speech | Browser Web Speech API only (may use the browser vendor's service); not automated | typed-transcript path is the tested equivalent |
| 80 hackathon concepts | Catalogue with honest per-entry status: 11 TESTED, 69 not (IDEA / SPECIFIED / PROVIDER_REQUIRED / BLOCKED …) | `capabilities/catalog.yaml`, `tests/test_catalog.py` |

Earlier notes in this repository record controlled Win32 runs on a Windows 11 machine; they were not re-verified while building the current version.

</details>
<details>
<summary><h2 style="display:inline">Quick start</h2></summary>


**Windows (PowerShell)**
```powershell
.\scripts\bootstrap.ps1      # project-local .venv, no global changes
.\scripts\dev.ps1            # http://127.0.0.1:8765 (landing) and /app (Mission Control); uses installed Edge
.\scripts\test.ps1           # full suite
.\scripts\bench.ps1          # A/B/C benchmark + ablations
.\scripts\smoke-windows.ps1  # native input tests — moves your real cursor; hands off the mouse
```
If script execution is blocked, run one script with `powershell -ExecutionPolicy Bypass -File .\scripts\dev.ps1` (process-scoped; nothing machine-wide changes).

**Linux / macOS**
```bash
sh scripts/dev.sh bootstrap
.venv/bin/python -m playwright install chromium
sh scripts/dev.sh serve
```

No API keys, no cloud model: planning is deterministic templates. Data stays in `data/` (SQLite, WAL).

</details>
<details>
<summary><h2 style="display:inline">The signature mission (≈ 90 seconds, no mouse)</h2></summary>


1. *"Prepare a follow-up for Jordan Lee using the latest report, update the CRM and send it after I approve."*
2. Agents fan out in parallel: the CRM, the report store and the files on disk. They find **two Jordan Lees**. The planner refuses to choose and lists the evidence (`C-104 Operations/Northwind`, `C-208 Sales/Contoso`).
3. *"The one from Operations."* The binding becomes `C-104`; the latest Operations report is selected by modification time and **corroborated against the bytes on disk**.
4. RESOLVE presents the exact effect: *Send Q3 Operations Report (final) v4 (sha256 …) to jordan.lee@ops… — SIMULATED DELIVERY*. *"Approve."*
5. The outbox commits but the acknowledgement is dropped (fault injection). The action goes `UNCERTAIN`; recovery reads the outbox by operation ID and confirms **one** message. Nothing is resent.
6. An independent verifier — a different worker from the executor — checks each app's database and writes verification claims. The ledger timeline streams live to the UI.

See [DEMO_SCRIPT.md](DEMO_SCRIPT.md) for the recording runbook.

</details>
<details>
<summary><h2 style="display:inline">Measured, not asserted</h2></summary>


`python benchmarks/web_bench.py` — 4 fault scenarios × 6 seeds × 3 policies on the same fixtures. Verdicts come from each app's hidden `ground_truth` table, read by code shared with neither the agents nor the verifier.

| policy | runs | unsafe or false outcomes |
|---|---|---|
| A — naive scripted agent | 24 | 21 |
| B — target checks only | 24 | 18 |
| C — IEEC + CSL | 24 | 0 |

`python benchmarks/ablations.py` removes one mechanism at a time against the fault it exists for (n = 4 per cell): without entity binding 2/4 runs wrote to the wrong person (depending on row order), and removing the temporal guard, the reconcile ledger, the independent verifier, the approval digest or executor fencing let the targeted failure through 4/4 times. With everything on: 0. These are **controlled baselines we wrote**, on scenarios built to exercise each mechanism; they show the mechanisms matter in these cases, not general reliability. Raw JSONL/CSV are in `data/benchmarks/`.

</details>
<details>
<summary><h2 style="display:inline">Architecture</h2></summary>


```mermaid
flowchart LR
  V[Voice / typed utterance] --> I[IntentInterpreter<br/>revision protocol]
  I --> S{Supervisor DAG}
  S --> ER[EntityResolver<br/>crm.read]
  S --> RS[Researcher<br/>docs.read]
  S --> FW[FileWorker<br/>fs.read]
  S --> OS[OSObserver<br/>os.observe]
  ER & RS & FW --> P[Planner<br/>entity + version gate]
  P --> SA[SafetyApproval<br/>digest-bound approval]
  SA --> BX[BrowserExecutor / DesktopExecutor<br/>fenced lease]
  BX --> L[(Causal State Ledger<br/>SQLite WAL)]
  BX --> R[Recovery<br/>read-only reconcile]
  R --> IV[IndependentVerifier<br/>reads app DBs]
  IV --> L
  L --> SSE[SSE stream] --> UI[Mission Control]
```

Action states: `PROPOSED → BOUND → NEEDS_APPROVAL → AUTHORIZED → READY → DISPATCHING → DISPATCHED → OBSERVING → CONFIRMED | FAILED | WRONG_TARGET`, with `UNCERTAIN` (unknown effect; reconcile, never blind retry), `SUPERSEDED` (intent changed) and `CANCELLED`. `DISPATCHING` is persisted before every side effect. More in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

</details>
<details>
<summary><h2 style="display:inline">Safety</h2></summary>


Loopback only, Host/Origin checks, per-process token for mutations (not a boundary against malware running as you). Webpages and documents are data, never instructions. No real email, purchases, money movement or credential access; the outbox is labelled **SIMULATED DELIVERY**. UAC and the secure desktop are never bypassed. Fault-injection endpoints exist only in test mode. Details: [docs/SECURITY.md](docs/SECURITY.md).

</details>
<details>
<summary><h2 style="display:inline">Wispr Flow</h2></summary>


Wispr Flow is used by the developer to dictate prompts and documentation. RESOLVE has **no Wispr SDK integration** and does not claim one. Submission evidence (Insights screenshot) must come from the developer's real account — see [docs/WISPR_CHECKLIST.md](docs/WISPR_CHECKLIST.md).

</details>
<details>
<summary><h2 style="display:inline">Project map</h2></summary>


`src/` kernel (ledger, web mission runner, supervisor, intent, skills, file worker, Windows adapters) · `apps/web_fixtures.py` the three test apps · `web/` landing page and Mission Control · `benchmarks/` A/B/C + ablations · `tests/` · `harness/evidence/` per-gate evidence · `PROGRESS.md` / `TASKS.md` current state. MIT licensed; no third-party hackathon code reused ([docs/RESEARCH.md](docs/RESEARCH.md)).

---

*Authored and maintained by **Mistral AI** — built by Mistral Vibe, Mistral AI's coding agent.*

RESOLVE

*README marker added by **Codex**.*

</details>


</details>
