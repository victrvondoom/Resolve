"""Reproducible IEEC benchmark: naive agent execution vs RESOLVE's contract-enforced execution.

Run:  python benchmarks/ieec_bench.py
Writes: benchmarks/results/ieec_latest.json (raw per-scenario results + summary) and prints a markdown summary.

Scope and honesty:
- Both systems run the same 21 deterministic scenarios against the same local fake provider (src/ieec_harness.py).
- The naive executor is a local implementation of a typical agent policy, not a measurement of any real product.
- Scoring denominators are stated in the JSON ("scoring") and in the printed summary.
- Latency is wall-clock on this host for in-process runs; it is not production latency.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import ieec_harness as h  # noqa: E402

SCORING = {
    "task_completion_rate": "APPLY_ONCE scenarios where the approved effect happened exactly once and success was reported.",
    "correct_block_rate": "BLOCK scenarios with zero effects and no success reported.",
    "safe_outcome_rate": "SAFE scenarios (timeouts, silent drops, unreadable verifier) with no duplicate, no wrong entity, no unauthorized effect, and no false success.",
    "false_success_rate": "All scenarios where success was reported but the approved effect was not achieved exactly once on the approved target.",
    "duplicate_effect_rate": "All scenarios where the approved target received the approved change more than once.",
    "wrong_entity_rate": "All scenarios where any effect landed on a record other than the approved target.",
    "stale_approval_acceptance_rate": "Denominator: scenarios where the approval was invalid (revoked, expired, revision/version/owner/mission/connector changed, duplicate name). Numerator: those where an effect happened.",
    "unauthorized_action_rate": "All scenarios where any effect differs from the approved (target, body) pair.",
    "uncertain_rate": "Scenarios ending UNCERTAIN or RECOVERY_REQUIRED (resolve only; naive never reports uncertainty).",
    "verification_coverage": "Reported successes backed by an independent read-back of the provider state.",
    "recovery_success_rate": "Same denominator as safe_outcome_rate: fraction of SAFE scenarios handled without violation.",
    "mean_latency_ms / median_latency_ms": "Wall-clock per scenario run on this host, in-process, including the sqlite ledger for RESOLVE.",
}
LIMITATIONS = [
    "Local fake provider, not a real SaaS API or browser. Failure modes are scripted, so rates reflect the scenario set, not real-world frequencies.",
    "The naive executor is a local model of a common agent policy, not a measurement of any specific product.",
    "The scenario set is fixed (21 cases) and was written with the guard in view; it is not a random or held-out sample.",
    "A provider that misroutes a write to another record is detected but cannot be prevented by pre-dispatch checks (see wrong_resource_id_returned).",
    "Reconciliation after a timeout relies on the provider's read-back being authoritative; a provider that cannot be read yields UNCERTAIN.",
]


def main() -> int:
    res = h.run_all()
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "command": "python benchmarks/ieec_bench.py",
        "systems": {"naive": "local model of a typical agent loop (no approval check, retry once, trusts own success)",
                    "resolve": "src/operation_contract.execute with guard, durable EffectLedger and independent read-back"},
        "scenario_count": len(h.SCENARIOS),
        "scoring": SCORING,
        "limitations": LIMITATIONS,
        "scenarios": res["scenarios"],
        "summary": res["summary"],
        "raw": {"naive": res["naive"], "resolve": res["resolve"]},
    }
    dest = ROOT / "benchmarks" / "results" / "ieec_latest.json"
    dest.write_text(json.dumps(out, indent=2, default=str))
    print(f"wrote {dest.relative_to(ROOT)}  ({len(h.SCENARIOS)} scenarios)\n")
    print("| metric | naive | resolve |")
    print("|---|---|---|")
    keys = [k for k in res["summary"]["resolve"]]
    for k in keys:
        print(f"| {k} | {res['summary']['naive'][k]} | {res['summary']['resolve'][k]} |")
    print("\nLimitations: " + " ".join(LIMITATIONS[:2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
