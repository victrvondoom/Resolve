# RESOLVE — Builders submission (No Hands: The Wispr Challenge)

> Status legend: **VERIFIED** = automated test + observed effect in this repo · **PARTIAL** · **NOT DONE**. Items not marked VERIFIED are not claimed.

## Problem
Developers lose time moving between a spec/brief, the code it describes, and the report someone has to write about the gap. Agents that
"do it for you" can click everything correctly and still analyze the **wrong document version** or produce a report that cites the wrong source.

## What RESOLVE does (VERIFIED)
One instruction — typed, or spoken into the in-page microphone — runs the **flagship mission**:
1. Binds the intent to **one document by stable id + SHA-256** (same-name documents are refused until you choose one).
2. Two real worker threads run **concurrently**: `DOCUMENT_READER` (extracts requirements from your brief PDF) and `REPOSITORY_ANALYZER`
   (static analysis of **your uploaded project .zip** — Python, JS/TS, Go, Java, Ruby, PHP — or the bundled sample; never executed, never extracted to disk, bound by content hash). Their recorded timestamps overlap — asserted by a barrier test.
3. `ARCHITECTURE_PLANNER` produces issues with file:line + brief-requirement citations, a corrected architecture (Mermaid + diagram built from the findings of *your* project), a task graph. **You can edit the Mermaid in the page**; the edit changes the plan digest, so only the edited plan can be approved, and the edit appears in the PDF.
   It is **deterministic rule-based analysis, labelled DRAFT — not an LLM.**
4. You **approve the exact plan digest**. Revising the instruction changes the digest, so earlier approvals become invalid.
5. `REPORT_COMPOSER` writes a real PDF; `EFFECT_VERIFIER` independently re-reads the stored bytes, re-hashes, parses with pypdf, and checks title,
   every issue, architecture, plan digest and that the **embedded source hash equals the bound source**. A forged-source report is rejected and deleted.
6. `CONFIRMED` is only reachable through `verify_chain`: user → intent revision → source entity → approved digest → output hash → read-back.
7. Download (hash shown == hash of the file received, asserted in a real-browser test), browser print dialog (**physical printing is never claimed**), delete.

Reproduce: `pip install -r requirements-dev.txt && python -m pytest tests/test_brief_mission.py tests/test_brief_mission_api.py tests/test_flagship_e2e.py -q`.
Screenshots from that real-browser run: `harness/evidence/screens/flagship-{plan-desktop,result-desktop,mobile}.png`.

## Why voice matters
Spec-to-report work is dictation-shaped: long, structured intent. The page has its own push-to-talk (real `getUserMedia` → 16 kHz WAV → server STT)
**and** works with **Wispr Flow dictating into the instruction box** — those are separate things; RESOLVE has no Wispr SDK integration and claims none.

## Honest limitations
- **Speech recognition is PARTIAL / unverified end-to-end.** The audio pipeline, consent gate, validation and error paths are tested with a *fake STT server*.
  No STT model was reachable in the build environment, so no recognition accuracy or live-microphone result is claimed. With no provider configured the mic
  button is disabled and says "NO SPEECH ENGINE" (it never inserts a scripted sentence). Configure `RESOLVE_VOSK_MODEL` (offline) or `RESOLVE_STT_URL`.
- **No public URL yet.** `docs/DEPLOYMENT.md` + `Dockerfile` are prepared and untested. Production mode exists and is tested locally against a real server.
- Analysis is **static pattern/AST checking** (hard-coded secrets, SQL string building, silent exception handling, TODO/FIXME, missing test files, import graph); it is not a security audit or a semantic bug finder. Python uses the AST; other languages use line-level regexes and can miss or over-report. Projects: ≤5 MiB zip, ≤400 source files. The brief can be any PDF with `R1 …must…` style lines.
- Cloud browser is **disabled in production** unless per-tenant isolation (microVM/gVisor) is declared; a Playwright context is not a VM.
- With no server STT configured the mic falls back to the **browser's own speech service** (audio goes to the browser vendor; per-use consent; labelled). Not tested with real speech here.
- Shared-ledger fixture demos (CRM/outbox, A/B/C benchmark) remain **demo-mode only** and are disabled in production. Outbox is SIMULATED DELIVERY.
- Native Windows input was not run here (`WINDOWS_NATIVE_TESTS_NOT_RUN`). Browser actions are DOM-level, not physical cursor movement.
- Sample data only for judges; the demo-account flow creates a disposable account (deletable under *Privacy & data*). Audio is processed in memory and not stored;
  an off-server STT provider needs explicit per-recording consent.

## Tests (see `harness/evidence/FINAL_REGRESSION.md`)
244 passed, 12 skipped (all native-Windows/Tk, with reasons), 0 failed on Linux. GitHub CI workflow written but not yet run.

## Demo script (≈2 min, no fabricated steps)
0:00 problem (scattered brief + code + report) · 0:12 *show real Wispr Flow dictating* a prompt (**user records this**) · 0:25 open RESOLVE, sign in with a demo account ·
0:35 push-to-talk or dictate the instruction · 0:45 Start → two workers visibly overlap · 1:05 plan with cited issues + diagram → Approve · 1:25 verified PDF, hash, read-back checks ·
1:40 download / print dialog · 1:50 delete → receipt. Do not label anything pre-recorded as live.

## Still missing before submission (user actions)
- Genuine Wispr Flow Insights screenshot from **your** account → `docs/WISPR_EVIDENCE.md`.
- Record the ≤2-minute video. Configure an STT provider and verify a live microphone run. Deploy and test the URL. Submit before **Oct 31, 2026 11:59 PM IST**.
