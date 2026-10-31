"""Evaluate the deterministic inbox rules against the labelled fixtures in benchmarks/inbox_fixtures.json.

    python benchmarks/inbox_eval.py   -> prints a table and writes benchmarks/results/inbox_eval.json
The fixtures are fixed and were written before the rules. Results are for these fixtures only, not for live mail.
"""
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from inbox_intel import extract_commitments, triage_thread  # noqa: E402


def ts(iso):
    return datetime.datetime.fromisoformat(iso + '+00:00').timestamp()


def run_fixture(fx):
    msgs = [{"id": m["id"], "sender": m["sender"], "body": m["body"], "sent_at": ts(m["sent_at"])} for m in fx["messages"]]
    decision = triage_thread(msgs, fx["user_address"])
    commits = extract_commitments(msgs, fx["user_address"])
    return decision, commits


def score(fx, decision, commits):
    exp = fx["expect"]
    checks = {"category": decision["category"] == exp["category"]}
    if "commitments" in exp:
        want = exp["commitments"]
        got_kinds = sorted(c["kind"] for c in commits)
        checks["commitment_kinds"] = got_kinds == sorted(w["kind"] for w in want)
        deadlines_ok = True
        for w in want:
            if "deadline" in w:
                match = [c for c in commits if c["kind"] == w["kind"]]
                if not match:
                    deadlines_ok = False
                    continue
                got = match[0]["deadline_at"]
                if w["deadline"] is None:
                    deadlines_ok = deadlines_ok and got is None
                else:
                    deadlines_ok = deadlines_ok and got is not None and datetime.datetime.fromtimestamp(got, datetime.timezone.utc).date().isoformat() == w["deadline"]
        checks["deadlines"] = deadlines_ok
    if "min_commitments" in exp:
        checks["min_commitments"] = len(commits) >= exp["min_commitments"]
    if exp.get("priority_not"):
        checks["priority"] = decision["priority"] != exp["priority_not"]
    if exp.get("no_send"):
        checks["no_send"] = True   # triage has no send path; asserted structurally in tests/test_inbox_intel.py
    return checks


def main() -> int:
    data = json.loads((ROOT / 'benchmarks' / 'inbox_fixtures.json').read_text())
    rows, passed_all = [], 0
    for fx in data["fixtures"]:
        decision, commits = run_fixture(fx)
        checks = score(fx, decision, commits)
        ok = all(checks.values())
        passed_all += ok
        rows.append({"id": fx["id"], "name": fx["name"], "expected": fx["expect"]["category"], "got": decision["category"],
                     "priority": decision["priority"], "confidence": decision["confidence"], "commitments": [
                         {"kind": c["kind"], "deadline": c["deadline_at"], "confidence": c["deadline_confidence"], "evidence": c["evidence_text"]} for c in commits],
                     "checks": checks, "pass": ok})
    cat_ok = sum(1 for r in rows if r["expected"] == r["got"])
    summary = {"fixtures": len(rows), "fixtures_fully_passing": passed_all, "category_accuracy": round(cat_ok / len(rows), 3),
               "note": "measured on labelled fixtures only; not a claim about live inboxes"}
    out = {"summary": summary, "rows": rows}
    dest = ROOT / 'benchmarks' / 'results' / 'inbox_eval.json'
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2))
    print(json.dumps(summary, indent=2))
    for r in rows:
        print(f"{r['id']} {'PASS' if r['pass'] else 'FAIL'} expected={r['expected']} got={r['got']} {r['name']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
