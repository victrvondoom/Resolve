# Wispr Flow demonstration script

**Status: user-owned evidence. Nothing in this repository records a Wispr Flow session, a usage screenshot or an Insights report. Do not add any without a real recording made by the account holder.**

Wispr Flow was used while building this project (confirmed by the project owner). RESOLVE has no Wispr SDK or API integration: no Wispr call exists in this code, and none is needed. Wispr Flow is an operating-system dictation tool. In this workflow it types into ordinary text fields: the coding assistant's prompt box, the RESOLVE instruction field, and the RESOLVE correction field. RESOLVE receives only the text that appears in those fields.

## What the recording must show

Record the screen and the microphone in one take, if possible. Do not edit out the failure cases.

1. **Dictate a feature correction into the coding assistant** (Cursor or Claude Code). Wispr Flow types the request. The assistant changes the code. Show the diff.
2. **Open RESOLVE in the browser** (`python src/resolve_app.py`, then open `http://127.0.0.1:8765/app`). Show the Morning brief in REPLAY mode, labelled synthetic.
3. **Dictate an inbox policy into the Voice Rules field.** Use this text, or your own:
   > Prioritize emails from my project collaborators. Show actual deadlines above ordinary questions. Track all promises I make. Show me what other people owe me. Never send an email without my approval. Use a friendly, concise tone.
   Click **Parse instructions**. Show the rule preview: each rule's type, scope, explanation, and whether it needs confirmation.
4. **Show an unknown instruction** dictated by mistake, for example "Blorp the frobnicator". It must appear as UNKNOWN and must not be activated. Show that the Activate control is disabled.
5. **Correct a triage decision by dictation.** Open a conversation in the inbox (for example, the automated build notice). In the correction field, dictate: *"That isn't urgent. It is an ordinary status update."* Choose the one-time scope. Click **Preview and save correction**. Show the before, your correction, the after, and the impact.
6. **Show that the correction stays scoped.** Return to the inbox. The one-time correction applies to that conversation only.
7. **Open a draft and dictate a reply change** into the body. Show that the text is saved as your text (origin DICTATED) and that an earlier approval is invalidated.

## Recording checklist

- [ ] Wispr Flow visibly types into each field (screen recording shows the text appearing).
- [ ] The account holder is identified in the recording, or the recording is marked as the account holder's own.
- [ ] No real email addresses, names or message contents are on screen. Use REPLAY mode only.
- [ ] The failure case (unknown instruction, step 4) is included.
- [ ] Nothing is edited to remove a mistake.
- [ ] Any usage figure shown comes from the Wispr Flow app itself, not from this repository.

## Where the evidence goes

Save the recording outside the repository, or add it under `docs/readme/` only after the account holder approves it for publication. Update `docs/WISPR_EVIDENCE.md` with the date, the duration and what the recording shows. Do not add a screenshot of a Wispr Flow usage page that was not taken by the account holder.

## What this demonstration does not prove

- It does not prove that Wispr Flow is installed, running or recording on any particular machine. RESOLVE cannot detect that.
- It does not test dictation accuracy. Dictation quality belongs to Wispr Flow.
- It does not use a speech server. The in-browser speech path (Web Speech API, where the browser exposes it) is a separate optional input and is not Wispr Flow.
