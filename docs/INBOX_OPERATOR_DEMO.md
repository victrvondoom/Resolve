# RESOLVE inbox operator: demonstration

This is the product walk-through for the Inbox-to-Commitment Operator. The default screen is the Morning brief. Every screen reads from the API; nothing is hard-coded.

## Run it

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
python src/resolve_app.py        # then open http://127.0.0.1:8765/app
```

Replay works with no credentials. Live Gmail needs the three OAuth variables in `docs/INBOX_OPERATOR_SETUP.md` and an authorized test account.

## Walk-through (REPLAY, synthetic)

1. **Replay lab → Start → Step** (repeat). Each step reveals one synthetic message and runs the same sync, triage and commitment code as the live mailbox. After 16 messages the script drafts a reply to the deployment checklist, approves it, and sends it through the simulated outbox. Then it drafts a reply that needs a price the thread does not contain.
2. **Morning brief.** Summary cards come from stored records. Needs your attention shows the reason and the quoted sentence. Replies to send, what you promised, what you are waiting for, and the timing groups (overdue, due today, due soon, future, unknown) are each computed from the database.
3. **Inbox.** Filter by classification. Open a conversation to see the full thread, the direction of each message, the evidence linked to message ids, the commitments with owners, and the uncertainty.
4. **Commitments.** Each obligation keeps its owner, requester, deadline phrase, timezone, confidence and exact sentence. Changing a status goes through a validated transition. Confirming completion yourself is recorded as user-confirmed, not verified.
5. **Drafts.** The editor shows the grounding (sources, missing information, assumptions, potential new commitments). Editing creates a new version. **Review and approve this version** shows the exact recipient, subject, body and digest, and expires after 15 minutes.
6. **Send.** In REPLAY the send is labelled SIMULATED. The execution timeline shows the verification record. Inject a failure from the replay lab to see an UNCERTAIN send, and reconcile it read-only.
7. **Voice rules.** Paste or dictate an instruction, parse it, inspect the rule cards, and activate only what you approve. Unknown instructions stay inactive.
8. **Connections.** Shows the honest Gmail state. Without OAuth variables it shows GMAIL NOT CONFIGURED and the names of the variables.
9. **Privacy & settings.** Set your timezone (deadlines use it), export your data, or delete the workspace.

## Walk-through (LOCAL, real local mail server)

1. `python src/resolve_app.py` and open `http://127.0.0.1:8765/app`.
2. **Connections → Local mail server**: name the mailbox (for example `founder`) and **Create local mailbox**. RESOLVE
   creates `founder@mail.resolve.test` on the local server and proves it with a POP3 login.
3. **Deliver a message into this mailbox**: from `priya`, subject `Deployment checklist`, body
   `Hi, could you send the revised deployment checklist before 4 PM?` (type, paste, or dictate with Wispr Flow).
4. **Sync local mailbox**, then **Morning brief → Local mail**: the request appears under what needs attention.
5. Open the conversation, **Prepare reply**, give the fact you can confirm (`status: I will send it at 3 PM.`).
6. **Approve** the exact content, then **Send through the local mail server**. The draft becomes **Sent · verified** only
   after the Sent folder is read back over POP3 and recipient, subject, body and Message-ID match.
7. Independent check outside RESOLVE: `python src/local_mail.py accounts` shows Priya's INBOX count, and the message is a
   file under `data/localmail/mail/priya@mail.resolve.test/INBOX/new/`.

Browser test that runs exactly this: `python -m pytest tests/test_inbox_ui.py -k local`. Screenshot:
`docs/screenshots/inbox/13_local_send_verified.png`.

## Screenshots

Captured from the running app by `python scripts/capture_inbox_screens.py` (REPLAY data only; every screen is labelled synthetic). They are in `docs/screenshots/inbox/`:

| File | Screen |
|---|---|
| `00_morning_not_connected.png` | Morning brief, LIVE, not connected: honest empty state |
| `01_morning_brief_replay.png` | Morning brief after the replay |
| `02_inbox_replay.png` | Inbox list with filters |
| `03_thread_detail_replay.png` | Conversation with analysis and correction field |
| `04_commitments_replay.png` | Commitment ledger |
| `05_commitment_detail_replay.png` | Commitment detail |
| `06_voice_rules_preview.png` | Voice rules: parsed rule preview |
| `07_draft_editor_replay.png` | Draft editor with grounding |
| `08_approval_exact_content_replay.png` | Exact-content approval |
| `09_replay_lab.png` | Replay lab |
| `10_simulated_send_and_timeline.png` | After the simulated send |
| `11_connections_gmail_not_configured.png` | Connections, Gmail not configured |
| `12_mobile_morning_replay.png` | Phone width |

The full-page captures show the app's existing sticky header in the middle of the page. That is a screenshot artifact of full-page capture, not a layout fault in normal scrolling.

## What is measured

See `docs/INBOX_BENCHMARK_REPORT.md`, generated by `python benchmarks/inbox_metrics.py`. It separates RULES_ONLY (measured), MODEL_ENABLED (not measured), simulated send results, and LIVE (not measured).

## Not in this demonstration

- Live Gmail results. They require credentials and an authorized account.
- A model. The engine is RULES_ONLY and is labelled as such.
- A Wispr Flow recording. See `docs/WISPR_DEMO_SCRIPT.md`.
