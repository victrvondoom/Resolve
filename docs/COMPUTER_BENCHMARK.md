# Computer-task benchmark (A naive / B target-guard / C RESOLVE)

Status: **MEASURED** on a Linux cloud sandbox, real Chromium, 105 runs, 0 harness errors. Reproduce with the command below.
Raw per-run rows: [`computer_bench_raw.json`](computer_bench_raw.json). Source: `benchmarks/computer_bench.py`; test: `tests/test_computer_bench.py`.

## What is compared
All three arms drive the same three **controlled local web apps** (`src/controlled_apps.py`: project board `pm`, research portal
`research`, contact workspace `crm`; loopback only, SQLite-backed) through the typed `ToolRegistry` `browser.*` tools on a real
Playwright Chromium session. These are DOM-level actions, not physical cursor movement. Every arm gets the identical seeded case
(same target, same fault, same timing).

| Arm | What it actually does |
|---|---|
| `A_naive` | First visible match; positional row handle kept from approval time; fixed in-browser pause (0.8 s) instead of waiting for async data; trusts the page's success text; clicks again once if the page stays silent for 3 s. |
| `B_target_guard` | Re-finds the target by visible title/name + context before acting and refuses ambiguity (state-guarded execution). No stable-id/version binding, no approval digest, no ledger, no DB verifier; same success-text trust and silent-retry policy as A. |
| `C_resolve_ieec` | `src/computer_tasks.ComputerTaskRunner` (stable-id + observed-version binding; proposal digest that must be presented back exactly; independent verifier reading the app database) wrapped in `resolve_core.Ledger`: DISPATCHING persisted before the click, single-use approval, unknown outcome becomes UNCERTAIN and is reconciled read-only, never re-sent. |

Scenarios (7 x 5 seeds; fault is injected by the app's test mode between approval and dispatch): `duplicate_name`,
`duplicate_submission` (approved submission delivered twice), `reordered_after_approval` (a task is inserted, rows shift),
`edited_after_approval` (task reassigned, version bumps; expected HOLD), `delayed_loading` (1.5-3 s data delay),
`lost_ack` (write commits, response dropped), `wrong_version_document` (document revised after approval; expected HOLD).

Scoring is by an **oracle** that opens each app's SQLite `effect_log` directly and shares no code with the arms or with
`ControlledApps.verify`; what an arm *claims* is never used to score effects.

## Command and environment
```
python benchmarks/computer_bench.py --seeds 5 --json docs/computer_bench_raw.json
```
- Linux 6.18 x86_64, 4 vCPU, 16 GB RAM (cloud sandbox); Python 3.13.18; Playwright 1.64.1; Chromium from `/opt/pw-browsers` (headless);
  pytest 9.2.0. Wall time 200.6 s. Peak Chromium RSS is sampled from `/proc` (Linux only).
- Branch `claude/gracious-ride-qzs42x`; the benchmark's test-mode hooks come from the patch applied to `src/controlled_apps.py`.

## Measured output (verbatim)
```
computer_bench: 105 runs = 3 arms x 7 scenarios x 5 seeds; wall 200.6 s; harness errors 0

| arm | runs | correct outcomes | wrong-target effects | duplicate effects | stale/unapproved effects | false-success claims | lost-ack recovered w/o resend | resend clicks | median latency ms | peak Chromium RSS MB |
|---|---|---|---|---|---|---|---|---|---|---|
| A_naive | 35 | 3 | 28 | 10 | 5 | 27 | 0/5 | 5 | 909.5 | 1259.0 |
| B_target_guard | 35 | 15 | 0 | 10 | 10 | 20 | 0/5 | 5 | 929.5 | 1276.2 |
| C_resolve_ieec | 35 | 35 | 0 | 0 | 0 | 0 | 5/5 | 0 | 867.2 | 1280.5 |

| scenario (expected) | A_naive correct | B_target_guard correct | C_resolve_ieec correct |
|---|---|---|---|
| duplicate_name (APPLY_ONCE) | 3/5 (wt 2, dup 0, stale 0, fs 2) | 5/5 (wt 0, dup 0, stale 0, fs 0) | 5/5 (wt 0, dup 0, stale 0, fs 0) |
| duplicate_submission (APPLY_ONCE) | 0/5 (wt 6, dup 5, stale 0, fs 5) | 0/5 (wt 0, dup 5, stale 0, fs 5) | 5/5 (wt 0, dup 0, stale 0, fs 0) |
| reordered_after_approval (APPLY_ONCE) | 0/5 (wt 5, dup 0, stale 0, fs 5) | 5/5 (wt 0, dup 0, stale 0, fs 0) | 5/5 (wt 0, dup 0, stale 0, fs 0) |
| edited_after_approval (HOLD) | 0/5 (wt 0, dup 0, stale 5, fs 5) | 0/5 (wt 0, dup 0, stale 5, fs 5) | 5/5 (wt 0, dup 0, stale 0, fs 0) |
| delayed_loading (APPLY_ONCE) | 0/5 (wt 0, dup 0, stale 0, fs 0) | 5/5 (wt 0, dup 0, stale 0, fs 0) | 5/5 (wt 0, dup 0, stale 0, fs 0) |
| lost_ack (APPLY_ONCE) | 0/5 (wt 10, dup 5, stale 0, fs 5) | 0/5 (wt 0, dup 5, stale 0, fs 5) | 5/5 (wt 0, dup 0, stale 0, fs 0) |
| wrong_version_document (HOLD) | 0/5 (wt 5, dup 0, stale 0, fs 5) | 0/5 (wt 0, dup 0, stale 5, fs 5) | 5/5 (wt 0, dup 0, stale 0, fs 0) |

wt = wrong-target effects, dup = duplicate effects, stale = effects on the right target but not the approved version/content, fs = false-success claims.
Effects are counted from each app's SQLite effect_log by an oracle that shares no code with the arms; controlled local baselines, not commercial agents.
raw rows written to docs/computer_bench_raw.json
```

## Reading the results
- C produced 35/35 correct outcomes with zero wrong-target, duplicate and stale effects and zero false-success claims; it recovered all
  5 lost acknowledgements by read-only reconciliation with no re-send.
- A and B both claimed success in 27 and 20 runs respectively where the oracle found the effect wrong or absent (false-success column).
- B's target re-finding fixes the target-identity faults (`duplicate_name`, `reordered_after_approval`, `delayed_loading`: 5/5) but
  not the faults that need version/digest binding or a ledger (`edited_after_approval`, `wrong_version_document`, `duplicate_submission`, `lost_ack`).
- A's `delayed_loading` failure is a no-op (nothing written), counted incorrect because the task was expected to complete; it is not an unsafe write.
- Latency is similar across arms (median 867-930 ms per run); C is not slower in this setup. Memory is dominated by Chromium.

## What this does and does not prove
- It shows, on seeded faults in controlled apps, that exact-digest + stable-id/version binding + a persisted DISPATCHING/UNCERTAIN ledger prevents the
  failure classes above, and that weaker guards do not. The faults bite: the test fails if A shows no wrong-target/duplicate effects or if ablating C's guards does not reintroduce failures.
- It does **not** compare against commercial computer-use agents; A and B are baselines written for this repo and may be weaker or stronger
  than real ones. The apps, faults and scenarios were authored together with the guards, so results are not an out-of-sample estimate.
- In arm C the harness itself plays the approving user (it presents the exact proposal digest); human approval behaviour is not measured.
- 5 seeds per scenario is a small sample; there are no confidence intervals. Seeds change targets and values, not the structure of the faults.
- Not tested: native Windows input (`WINDOWS_NATIVE_TESTS_NOT_RUN`), real third-party sites, live Gmail, any model-driven agent. Numbers
  came from one run on one machine; latency/RSS will vary with hardware.
