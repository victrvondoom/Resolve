#!/usr/bin/env python3
"""Mutation check: prove that RESOLVE's critical guards are covered by tests that fail when the guard is removed.

For every mutant below the script
  1. copies the repository into a temporary directory (the real tree is never modified),
  2. applies one precise source edit (exact old string -> replacement; the old string must occur exactly once,
     otherwise the mutant is reported as ERROR, never silently passed),
  3. runs only the named relevant test files with `python -m pytest -q -p no:cacheprovider -x`,
  4. REQUIRES the tests to fail (mutant KILLED). A passing run means the guard is not covered (SURVIVED).
Before any mutant runs, the unmutated union of all named test files must PASS in the same temporary copy.
The edit is reverted in a `finally` block and the restored bytes are hash-checked before the next mutant runs.

Usage:
    python scripts/mutation_check.py              # baseline + all mutants, writes docs/MUTATION_REPORT.md
    python scripts/mutation_check.py --only M05   # run selected mutants (comma separated); report not written
    python scripts/mutation_check.py --list       # print the mutant catalogue
    python scripts/mutation_check.py --verbose    # also print pytest output for any mutant that was not KILLED

Exit code 0 only when the baseline passes and every mutant is KILLED.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs" / "MUTATION_REPORT.md"
COMMAND = "python scripts/mutation_check.py"
PYTEST = ["-m", "pytest", "-q", "-p", "no:cacheprovider", "-x", "-rfE"]
COPY_IGNORE = shutil.ignore_patterns(".git", ".venv", ".claude", "__pycache__", ".pytest_cache", "node_modules", "*.pyc")
PER_MUTANT_TIMEOUT_S = 900


@dataclass(frozen=True)
class Mutant:
    id: str
    guard: str
    file: str
    old: str
    new: str
    tests: tuple[str, ...]


@dataclass
class Result:
    mutant: Mutant
    status: str                      # KILLED | SURVIVED | ERROR
    detail: str = ""
    failing: list[str] = field(default_factory=list)
    line: int | None = None
    seconds: float = 0.0
    output: str = ""


MUTANTS: tuple[Mutant, ...] = (
    # ---- 1. stale intent-revision guards
    Mutant("M01", "Web mission: actions of a superseded intent revision may not execute (_guard_revision)",
           "src/web_mission.py",
           "if mission['intent_revision'] != action['intent_revision'] or action['status'] == 'SUPERSEDED':",
           "if False:  # MUTANT: revision guard disabled",
           ("tests/test_w7_voice_correction.py", "tests/test_guard_coverage.py")),
    Mutant("M02", "Web mission: approval refused when it refers to a superseded intent revision",
           "src/web_mission.py",
           "if a['intent_revision'] != mission['intent_revision']:",
           "if False:  # MUTANT: stale approval revision accepted",
           ("tests/test_guard_coverage.py",)),
    Mutant("M03", "Brief mission: state commits are compare-and-set on the intent revision (_cas)",
           "src/brief_mission.py",
           'guard, extra = ("", ()) if revision is None else (" AND intent_revision=?", (revision,))',
           'guard, extra = ("", ())  # MUTANT: revision ignored in CAS',
           ("tests/test_brief_mission.py", "tests/test_ieec_adversarial.py", "tests/test_guard_coverage.py")),
    Mutant("M04", "IEEC operation contract: REVISION_CHANGED when the mission revision moved after approval",
           "src/operation_contract.py",
           "if o.mission_revision != c.revision:",
           "if False:  # MUTANT: revision check disabled",
           ("tests/test_remediation_ieec.py", "tests/test_ieec_adversarial.py")),
    # ---- 2. approval digest verification
    Mutant("M05", "Web mission: approval must present the exact action digest (recipient/content/attachment/revision)",
           "src/web_mission.py",
           "if presented_digest != self.action_digest(a):",
           "if False:  # MUTANT: digest not verified",
           ("tests/test_web_g3.py", "tests/test_guard_coverage.py")),
    Mutant("M06", "Brief mission: plan approval must match the current plan digest",
           "src/brief_mission.py",
           'if plan_digest != plan["digest"]:',
           "if False:  # MUTANT: plan digest not verified",
           ("tests/test_brief_mission.py", "tests/test_ieec_adversarial.py")),
    Mutant("M07", "Computer tasks: update_task executes only with the exact approved proposal digest",
           "src/computer_tasks.py",
           'if approved_digest != prop["digest"] or (proposal and proposal != prop):',
           "if False:  # MUTANT: proposal digest not verified",
           ("tests/test_computer_tasks.py",)),
    Mutant("M08", "Computer tasks: save_report executes only with the exact approved proposal digest",
           "src/computer_tasks.py",
           'if approved_digest != prop["digest"]:',
           "if False:  # MUTANT: report digest not verified",
           ("tests/test_computer_tasks.py", "tests/test_guard_coverage.py")),
    Mutant("M09", "Gmail send broker: consumed approval must be bound to the presented content digest",
           "src/inbox_store.py",
           'if r["operation"] != operation or r["digest"] != digest:',
           'if r["operation"] != operation:  # MUTANT: digest binding dropped',
           ("tests/test_send_broker.py", "tests/test_inbox_store.py", "tests/test_guard_coverage.py")),
    Mutant("M10", "Gmail send broker: approval refused when the draft changed after approval",
           "src/inbox_store.py",
           'if r["current_digest"] != r["digest"]:',
           "if False:  # MUTANT: changed draft accepted",
           ("tests/test_send_broker.py", "tests/test_inbox_store.py")),
    Mutant("M11", "Inbox approval: presented digest must equal the current draft digest",
           "src/inbox_store.py",
           'if presented_digest != draft["digest"]:',
           "if False:  # MUTANT: approval of a non-current digest",
           ("tests/test_send_broker.py", "tests/test_inbox_store.py", "tests/test_inbox_api.py", "tests/test_guard_coverage.py")),
    # ---- 3. tenant ownership
    Mutant("M12", "Documents: get_document is scoped to user_id AND workspace_id",
           "src/document_studio.py",
           '"SELECT * FROM documents WHERE id=? AND user_id=? AND workspace_id=?",\n'
           "                             (document_id, user_id, workspace_id)",
           '"SELECT * FROM documents WHERE id=?",\n'
           "                             (document_id,)",
           ("tests/test_document_studio.py", "tests/test_hidden_failures.py", "tests/test_tenant_security.py")),
    Mutant("M13", "Brief missions: mission rows are scoped to user_id AND workspace_id (_row)",
           "src/brief_mission.py",
           '"SELECT * FROM brief_missions WHERE id=? AND user_id=? AND workspace_id=?", (mission_id, user_id, workspace_id)',
           '"SELECT * FROM brief_missions WHERE id=?", (mission_id,)',
           ("tests/test_brief_mission.py", "tests/test_hidden_failures.py", "tests/test_tenant_security.py")),
    Mutant("M14", "IEEC operation contract: NOT_OWNER when user/workspace differ from the approved contract",
           "src/operation_contract.py",
           "if o.user_id != c.user_id or o.workspace_id != c.workspace_id:",
           "if False:  # MUTANT: owner check disabled",
           ("tests/test_remediation_ieec.py", "tests/test_ieec_adversarial.py", "tests/test_guard_coverage.py")),
    Mutant("M15", "Effect graph: cross-tenant node detection",
           "src/effect_graph.py",
           "if node.user_id != self.user_id or node.workspace_id != self.workspace_id:",
           "if False:  # MUTANT: tenant check disabled",
           ("tests/test_effect_graph.py",)),
    # ---- 4. output hash verification of the generated PDF
    Mutant("M16", "Brief mission: causal chain requires the read-back output hash to equal the stored output hash",
           "src/brief_mission.py",
           'if not m.get("output_sha256") or ver.get("output_sha256") != m.get("output_sha256"):',
           "if False:  # MUTANT: output hash link not checked",
           ("tests/test_brief_mission.py", "tests/test_guard_coverage.py")),
    Mutant("M17", "Brief mission: EFFECT_VERIFIER re-hash must equal the written document hash before CONFIRMED",
           "src/brief_mission.py",
           'if not ver["passed"] or ver["output_sha256"] != doc["sha256"]:',
           'if not ver["passed"]:  # MUTANT: output hash comparison dropped',
           ("tests/test_brief_mission.py", "tests/test_guard_coverage.py")),
    Mutant("M18", "Documents: stored PDF bytes are re-hashed on read and refused when changed",
           "src/document_studio.py",
           'if hashlib.sha256(data).hexdigest() != doc["sha256"]:',
           "if False:  # MUTANT: integrity check disabled",
           ("tests/test_document_studio.py", "tests/test_hidden_failures.py")),
    # ---- extra guards
    Mutant("M19", "Ledger: DISPATCHING without a result becomes UNCERTAIN on restart (crash window)",
           "src/resolve_core.py",
           "\"SELECT id,data FROM actions WHERE state='DISPATCHING'\"",
           "\"SELECT id,data FROM actions WHERE 0\"",
           ("tests/test_core.py", "tests/test_w12_crash_restart.py")),
    Mutant("M20", "Gmail send broker: a send timeout is UNCERTAIN (reconciled read-only), never FAILED/retryable",
           "src/send_broker.py",
           'self.store.finish_execution(user_id, workspace_id, rec["id"], "UNCERTAIN", None, "TIMEOUT",\n'
           '                                        "the send timed out; Sent will be checked read-only before any further action")\n'
           '            return {"execution_id": rec["id"], "state": "UNCERTAIN", "reason": "TIMEOUT"}',
           'self.store.finish_execution(user_id, workspace_id, rec["id"], "FAILED", None, "TIMEOUT",\n'
           '                                        "the send timed out; Sent will be checked read-only before any further action")\n'
           '            return {"execution_id": rec["id"], "state": "FAILED", "reason": "TIMEOUT"}  # MUTANT',
           ("tests/test_send_broker.py",)),
    Mutant("M21", "Gmail send broker: Sent is checked by correlation key before any (re)send",
           "src/send_broker.py",
           "        if found:\n            return self._verify_sent(",
           "        if False:  # MUTANT: Sent check skipped\n            return self._verify_sent(",
           ("tests/test_send_broker.py", "tests/test_guard_coverage.py")),
    Mutant("M22", "SSRF net policy: destinations resolving to non-public addresses are blocked",
           "src/net_policy.py",
           "if not ip.is_global:",
           "if False:  # MUTANT: private-IP block disabled",
           ("tests/test_cloud_browser.py",)),
)


# ------------------------------------------------------------------ helpers

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_pytest(cwd: Path, tests: list[str]) -> tuple[int, str, float]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTEST_ADDOPTS")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"       # never reuse bytecode from a different (mutated/unmutated) source
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, *PYTEST, *tests], cwd=cwd, env=env, capture_output=True, text=True,
                           timeout=PER_MUTANT_TIMEOUT_S)
        out, code = p.stdout + p.stderr, p.returncode
    except subprocess.TimeoutExpired as exc:
        out, code = f"TIMEOUT after {PER_MUTANT_TIMEOUT_S}s\n{exc.stdout or ''}", -1
    return code, out, time.time() - t0


def failing_tests(output: str) -> list[str]:
    names = []
    for line in output.splitlines():
        # "FAILED tests/x.py::T::t - ...", "ERROR tests/...", and subtests: "SUBFAILED(sid='a') tests/x.py::T::t"
        m = re.match(r"^(?:SUB)?(?:FAILED|FAIL|ERROR)\S*\s.*?(tests/\S+)", line)
        if m and m.group(1) not in names:
            names.append(m.group(1))
    return names


def last_line(output: str) -> str:
    lines = [ln for ln in output.strip().splitlines() if ln.strip()]
    tail = [ln for ln in lines if re.search(r"\b(passed|failed|error|errors|skipped|no tests ran)\b", ln)]
    return (tail or lines or ["(no output)"])[-1].strip("= ")


def apply_and_test(copy: Path, m: Mutant) -> Result:
    target = copy / m.file
    if not target.exists():
        return Result(m, "ERROR", f"file not found: {m.file}")
    missing = [t for t in m.tests if not (copy / t).exists()]
    if missing:
        return Result(m, "ERROR", f"test file(s) not found: {', '.join(missing)}")
    original = target.read_bytes()
    text = original.decode("utf-8")
    count = text.count(m.old)
    if count != 1:
        return Result(m, "ERROR", f"old string found {count} times in {m.file} (must be exactly 1)")
    line = text[: text.index(m.old)].count("\n") + 1
    before = sha(target)
    try:
        mutated = text.replace(m.old, m.new, 1)
        try:
            compile(mutated, str(target), "exec")
        except SyntaxError as exc:
            return Result(m, "ERROR", f"mutant does not compile: {exc}", line=line)
        target.write_text(mutated, encoding="utf-8")
        code, out, secs = run_pytest(copy, list(m.tests))
    finally:
        target.write_bytes(original)
        if sha(target) != before:
            raise SystemExit(f"FATAL: could not restore {m.file} in the temporary copy")
    names = failing_tests(out)
    if code == 1 and names:
        return Result(m, "KILLED", last_line(out), names, line, secs, out)
    if code == 0:
        return Result(m, "SURVIVED", last_line(out), [], line, secs, out)
    return Result(m, "ERROR", f"pytest exit {code}: {last_line(out)}", names, line, secs, out)


def git_rev() -> str:
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "src", "tests", "scripts"], cwd=ROOT,
                               capture_output=True, text=True).stdout.strip()
        return rev + (" (+ uncommitted changes in src/tests/scripts)" if dirty else "")
    except OSError:
        return "unknown"


def table(results: list[Result]) -> list[str]:
    rows = ["| ID | Guard | Mutated at | Tests run | Result | Failing test (first, -x) |",
            "|---|---|---|---|---|---|"]
    for r in results:
        m = r.mutant
        where = f"`{m.file}:{r.line}`" if r.line else f"`{m.file}`"
        tests = "<br>".join(f"`{Path(t).name}`" for t in m.tests)
        fail = "<br>".join(f"`{n}`" for n in r.failing) if r.failing else (r.detail or "-")
        rows.append(f"| {m.id} | {m.guard} | {where} | {tests} | **{r.status}** | {fail} |")
    return rows


def write_report(baseline: tuple[int, str, float, list[str]], results: list[Result]) -> None:
    code, summary, secs, tests = baseline
    killed = sum(r.status == "KILLED" for r in results)
    lines = [
        "# Mutation report: critical guard coverage",
        "",
        "Generated by `scripts/mutation_check.py`. Do not edit by hand; re-run the command to refresh it.",
        "",
        "## Command",
        "",
        "```",
        COMMAND,
        "```",
        "",
        f"- Tree under test (HEAD when generated): `{git_rev()}`",
        f"- Python {platform.python_version()} on {platform.system()} ({platform.machine()})",
        f"- Test runner per mutant: `python {' '.join(PYTEST[:-1])} <test files>` (`-rfE` only adds the failure summary used below)",
        "- Each mutant is applied to a temporary copy of the repository; the real tree is never modified, and the copy is",
        "  restored (hash-checked) after every mutant and deleted at the end.",
        "- A mutant whose exact old string is missing or not unique is reported as ERROR, never as a pass.",
        "",
        "## Baseline (unmutated)",
        "",
        f"- Test files: {', '.join(f'`{Path(t).name}`' for t in tests)}",
        f"- Result: **{'PASS' if code == 0 else 'FAIL'}** — {summary} ({secs:.1f}s)",
        "",
        f"## Mutants: {killed}/{len(results)} KILLED",
        "",
        *table(results),
        "",
        "## Mutations (exact edits)",
        "",
    ]
    for r in results:
        m = r.mutant
        lines += [f"### {m.id} — {r.status}", "", m.guard, "", f"`{m.file}` line {r.line or '?'}", "",
                  "```diff", *[f"- {ln}" for ln in m.old.splitlines()], *[f"+ {ln}" for ln in m.new.splitlines()], "```", "",
                  f"Result: {r.detail} ({r.seconds:.1f}s)", ""]
    lines += [
        "## Notes",
        "",
        "- `tests/test_guard_coverage.py` was added because the pre-existing suite let these mutants SURVIVE (first run of",
        "  this script): M01, M02, M03, M08, M09, M11, M14, M16, M17, M21. Each mutant lists the pre-existing files first, so",
        "  the failing test shown is the first test that actually catches the mutant. M05 was already killed by",
        "  `test_web_g3.py`; a fast non-browser copy of that check lives in the new file too.",
        "- The new tests drive public entry points and inject faults only through inputs, scripted doubles or a patched",
        "  collaborator (M03 interleaves a revision between the approval's read and its commit; M17 forges the verifier's",
        "  re-hash). No production code was changed.",
        "- Browser-backed files (`test_w7_voice_correction.py`, `test_web_g3.py`, `test_computer_tasks.py`,",
        "  `test_cloud_browser.py`, and `SaveReportDigestGuard` in the new file) use Playwright Chromium against local",
        "  fixture apps; their actions are DOM-level, not physical cursor movement.",
        "- This report covers guard coverage by the portable test suite only. It says nothing about live Gmail, models or",
        "  native Windows input (WINDOWS_NATIVE_TESTS_NOT_RUN on Linux hosts).",
        "",
    ]
    REPORT.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", help="comma-separated mutant ids; the report is not rewritten")
    ap.add_argument("--list", action="store_true", help="print the mutant catalogue and exit")
    ap.add_argument("--verbose", action="store_true", help="print the pytest output tail of every mutant that was not KILLED")
    args = ap.parse_args()
    if args.list:
        for m in MUTANTS:
            print(f"{m.id}  {m.file:28} {m.guard}")
        return 0
    selected = list(MUTANTS)
    if args.only:
        want = {x.strip().upper() for x in args.only.split(",") if x.strip()}
        unknown = want - {m.id for m in MUTANTS}
        if unknown:
            print(f"unknown mutant id(s): {', '.join(sorted(unknown))}")
            return 2
        selected = [m for m in MUTANTS if m.id in want]

    tmp = Path(tempfile.mkdtemp(prefix="resolve-mutation-"))
    copy = tmp / "repo"
    try:
        shutil.copytree(ROOT, copy, ignore=COPY_IGNORE, symlinks=True)
        tests = sorted({t for m in selected for t in m.tests if (copy / t).exists()})
        print(f"[baseline] {len(tests)} test files, unmutated copy at {copy}")
        code, out, secs = run_pytest(copy, tests)
        summary = last_line(out)
        print(f"[baseline] {'PASS' if code == 0 else 'FAIL'}: {summary} ({secs:.1f}s)")
        if code != 0:
            print(out[-4000:])
            print("Baseline must pass before mutants are meaningful.")
            return 1
        results = []
        for m in selected:
            r = apply_and_test(copy, m)
            results.append(r)
            first = r.failing[0] if r.failing else r.detail
            print(f"[{m.id}] {r.status:8} {m.file}:{r.line or '?'}  ({r.seconds:.1f}s)  {first}")
            if args.verbose and r.status != "KILLED" and r.output:
                print(r.output[-3000:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("\n".join(table(results)))
    killed = sum(r.status == "KILLED" for r in results)
    print(f"\n{killed}/{len(results)} mutants KILLED")
    if not args.only:
        write_report((code, summary, secs, tests), results)
        print(f"report: {REPORT.relative_to(ROOT)}")
    return 0 if killed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
