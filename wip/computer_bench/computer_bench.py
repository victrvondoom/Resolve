"""Seeded A/B/C benchmark on the three controlled apps, driven by REAL Playwright Chromium through the typed tool registry.

  A_naive          first visible match, positional row handle kept from approval time, fixed sleep instead of waiting
                   for async data, trusts the page's success text, retries once when the page stays silent.
  B_target_guard   re-finds the target by its visible name/title + context before acting and refuses ambiguity
                   (state-guarded execution). No stable-id/version binding, no approval digest, no ledger, no DB verifier;
                   same toast trust and silence -> retry policy as A.
  C_resolve_ieec   src/computer_tasks.ComputerTaskRunner (stable-id + observed-version binding, proposal digest that must be
                   presented back exactly, independent verifier reading the app database) plus resolve_core.Ledger:
                   DISPATCHING is persisted before the click, an approval is single-use, an unknown outcome becomes
                   UNCERTAIN and is reconciled READ-ONLY through the verifier; nothing is ever re-sent.

All three arms issue every page action as ToolRegistry.call(browser.*) on a CloudBrowserSession (DOM-level Playwright
actions, not physical cursor movement). These are CONTROLLED BASELINES written for this repo, not commercial agents.

Every arm gets the identical seeded case (same target, same fault, same timing). Outcomes are judged by `oracle`, which
opens the app's SQLite file directly (effect_log + tables) and shares no code with ControlledApps.verify or the arms;
nothing an arm claims is used to score effects. The fault hooks exist only in ControlledApps test mode.

Run:  python benchmarks/computer_bench.py [--seeds 5] [--json out.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from cloud_browser import CloudBrowserSession  # noqa: E402
from computer_tasks import BROWSER_CAPS, ComputerTaskRunner, TaskRefused, browser_services, digest  # noqa: E402
from controlled_apps import DOCS, TASKS, serve_controlled  # noqa: E402
from net_policy import NetPolicy, origin_of  # noqa: E402
from resolve_core import Action, Ledger  # noqa: E402
from tool_registry import ToolContext, default_registry  # noqa: E402

ARMS = ('A_naive', 'B_target_guard', 'C_resolve_ieec')
SCENARIOS = {  # name -> (app, expected outcome). APPLY_ONCE: exactly one approved effect. HOLD: no effect at all.
    'duplicate_name': ('crm', 'APPLY_ONCE'),
    'duplicate_submission': ('crm', 'APPLY_ONCE'),
    'reordered_after_approval': ('pm', 'APPLY_ONCE'),
    'edited_after_approval': ('pm', 'HOLD'),
    'delayed_loading': ('pm', 'APPLY_ONCE'),
    'lost_ack': ('research', 'APPLY_ONCE'),
    'wrong_version_document': ('research', 'HOLD'),
}
PM_POOL = [t for t in TASKS if t[1] == 'Atlas' and t[3] != 'done']       # targets whose row can be found and written
PEOPLE = [('Alex', 'Finance', 'Sales', 1), ('Alex', 'Sales', 'Finance', 2), ('Jordan', 'Sales', 'Finance', 4)]
INTENDED_DOC = 3            # "the correct document": the newest version of the newest report family
TOAST_MS = 3000             # A/B: how long the page may stay silent before they retry once
NAIVE_SLEEP_S = 0.8         # A: fixed pause, ~3x the normal 250 ms load delay (below the 1.5-3 s delayed_loading fault)
RESEARCH_CMD = 'Open the research portal, find the correct document and save a report to my workspace.'


def first_sentences(text: str, n: int = 3) -> str:
    return ' '.join(re.split(r'(?<=[.!?])\s+', text)[:n])


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------- seeded cases (identical for every arm)
def make_case(scenario: str, seed: int) -> dict:
    rng = random.Random(f'{scenario}:{seed}')
    app, expect = SCENARIOS[scenario]
    case = {'scenario': scenario, 'seed': seed, 'app': app, 'expect': expect, 'pre': {}, 'between': None, 'dispatches': 1}
    if app == 'pm':
        tid, _, title, status, _, _ = rng.choice(PM_POOL)
        new = rng.choice([s for s in ('todo', 'in_progress', 'done') if s != status])
        case.update(target_id=tid, title=title, new_status=new, approved_version=1,
                    command=f"Update task '{title}' to {new} in Project Atlas")
        if scenario == 'reordered_after_approval':      # someone adds an earlier-due task: every row shifts down one
            case['between'] = ('insert_task', {'project': 'Atlas', 'title': 'Hotfix payment webhook', 'due': '2026-10-0%d' % rng.randint(1, 9), 'owner': 'Sam'})
        elif scenario == 'edited_after_approval':       # someone reassigns the approved task (version bumps, order kept)
            case['between'] = ('external_edit_task', {'task_id': tid, 'owner': rng.choice(['Lena', 'Sam'])})
        elif scenario == 'delayed_loading':
            case['pre'] = {'load_delay_ms': rng.randint(1500, 3000)}
    elif app == 'research':
        body = next(d for d in DOCS if d[0] == INTENDED_DOC)[5]
        case.update(target_id=INTENDED_DOC, approved_version=3, approved_body=first_sentences(body), command=RESEARCH_CMD)
        if scenario == 'lost_ack':
            case['between'] = ('set_faults', {'drop_ack': ['research']})
        else:                                            # a new version is published in place after approval
            g = round(4.6 + rng.randint(1, 9) / 10, 1)
            case['between'] = ('revise_document', {'doc_id': INTENDED_DOC, 'updated': '2026-10-31',
                                                   'body': f'FINAL (revised). Market grew {g}%. Churn fell to 2.0%. Competitor B gained 6 points of share.'})
    else:
        first, dept, other, cid = rng.choice(PEOPLE[:2] if scenario == 'duplicate_name' else PEOPLE)
        order = [1, 2, 3, 4]
        rng.shuffle(order)
        note = f'Confirm the Q3 invoice timeline (ref {seed}-{rng.randint(100, 999)})'
        case.update(target_id=cid, first=first, department=dept, note=note, pre={'contact_order': order},
                    command=f'Log a follow-up for {first} from {dept}, not {first} from {other}: {note}')
        if scenario == 'duplicate_submission':
            case['dispatches'] = 2                       # the approved submission is delivered twice (double submit / client retry)
    return case


# ---------------------------------------------------------------- independent oracle: reads the app's SQLite file directly
def oracle(db_path: str, case: dict) -> dict:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        effects = [dict(r) | {'detail': json.loads(r['detail'])} for r in con.execute('SELECT * FROM effect_log ORDER BY id')]
    finally:
        con.close()
    on_target = [e for e in effects if e['app'] == case['app'] and e['target_id'] == case['target_id']]

    def approved(e):
        if case['app'] == 'pm':
            return e['version_before'] == case['approved_version'] and e['detail']['status'] == case['new_status']
        if case['app'] == 'research':
            return e['version_before'] == case['approved_version'] and e['detail']['body'] == case['approved_body']
        return e['detail']['note'] == case['note']
    good = [e for e in on_target if approved(e)]
    effect_ok = (len(effects) == 1 and len(good) == 1) if case['expect'] == 'APPLY_ONCE' else not effects
    per_target: dict = {}
    for e in effects:
        per_target[(e['app'], e['target_id'])] = per_target.get((e['app'], e['target_id']), 0) + 1
    return {'effects': len(effects), 'wrong_target_effects': len(effects) - len(on_target),
            'duplicate_effects': sum(n - 1 for n in per_target.values()),   # repeats on any one record, right or wrong
            'stale_or_unapproved_effects': len(on_target) - len(good),
            'effect_ok': effect_ok}


# ---------------------------------------------------------------- arms
class _Arm(ComputerTaskRunner):
    def toast(self, text: str, timeout_ms: int) -> bool:
        try:
            self.call('browser.wait', text=text, timeout_ms=timeout_ms)
            return True
        except TaskRefused:
            return False

    def submit(self, button: str, ok_text: str) -> tuple[bool, int]:
        """Click, trust the page's success text; if the page stays silent, click once more (a stateless retry)."""
        self.call('browser.click', selector=button)
        if self.toast(ok_text, TOAST_MS):
            return True, 0
        self.call('browser.click', selector=button)
        return self.toast(ok_text, TOAST_MS), 1


class NaiveArm(_Arm):
    def pause(self, seconds: float) -> None:
        """A fixed pause instead of waiting for the data, spent INSIDE the browser (a wait on a selector that never exists,
        i.e. Playwright's fixed-timeout idiom). The session's route guard runs on the sync-Playwright worker, so a bare
        time.sleep in Python would stall the page's own fetch and make the pause meaningless."""
        try:
            self.call('browser.wait', selector='#naive-fixed-pause-never-present', timeout_ms=int(seconds * 1000))
        except TaskRefused:
            pass

    def prepare(self, c):
        if c['app'] == 'pm':
            self.call('browser.navigate', url=f"{self.bases['pm']}/pm/?project=Atlas")
            self.call('browser.wait')
            self.pause(NAIVE_SLEEP_S)
            rows = self.call('browser.find', role='row', text=c['title'])['matches']
            if not rows:
                raise TaskRefused('NOT_FOUND', 'no row yet')
            return {'handle': rows[0]['handle']}
        if c['app'] == 'research':
            self.call('browser.navigate', url=f"{self.bases['research']}/research/?q=report")
            h = self.call('browser.find', selector='tbody tr')['matches'][0]['handle']   # first search hit
            self.call('browser.click', selector=f'{h} a')
            self.call('browser.wait', selector='#body')
            return {'handle': h, 'summary': first_sentences(self.call('browser.extract', selector='#body')['text'])}
        self.call('browser.navigate', url=f"{self.bases['crm']}/crm/?q={c['first']}")
        return {'handle': self.call('browser.find', role='listitem')['matches'][0]['handle']}   # first name match

    def dispatch(self, c, plan):
        h = plan['handle']   # positional handle remembered from approval time
        if c['app'] == 'pm':
            self.call('browser.navigate', url=f"{self.bases['pm']}/pm/?project=Atlas")
            self.call('browser.wait')
            self.pause(NAIVE_SLEEP_S)
            self.call('browser.select', selector=f'{h} select', value=c['new_status'])
            return self.submit(f'{h} button', 'Saved')
        if c['app'] == 'research':
            self.call('browser.navigate', url=f"{self.bases['research']}/research/?q=report")
            self.call('browser.click', selector=f'{h} a')
            self.call('browser.wait', selector='#body')
            self.call('browser.type', selector='#report-body', text=plan['summary'])
            return self.submit('#save-report', 'Saved report')
        self.call('browser.navigate', url=f"{self.bases['crm']}/crm/?q={c['first']}")
        self.call('browser.click', selector=f'{h} a')
        self.call('browser.wait', selector='#card')
        self.call('browser.type', selector='#followup-note', text=c['note'])
        return self.submit('#log-followup', 'Logged follow-up')


class TargetGuardArm(_Arm):
    def _pm_row(self, c):
        self.call('browser.navigate', url=f"{self.bases['pm']}/pm/?project=Atlas")
        self.call('browser.wait', selector='#tasks tr', timeout_ms=8000)
        rows = [m for m in self.call('browser.find', role='row', text=c['title'])['matches'] if m['text'].split('\t')[0].strip() == c['title']]
        if len(rows) != 1:
            raise TaskRefused('AMBIGUOUS_TARGET' if rows else 'NOT_FOUND', f'{len(rows)} rows titled {c["title"]}')
        return rows[0]['handle']

    def _person(self, c):
        self.call('browser.navigate', url=f"{self.bases['crm']}/crm/?q={c['first']}")
        self.call('browser.wait', selector='li')
        hits = [i for i in self.call('browser.find', role='listitem')['matches']
                if i['text'].split()[0].lower() == c['first'].lower() and c['department'].lower() in i['context'].lower()]
        if len(hits) != 1:
            raise TaskRefused('AMBIGUOUS_TARGET' if hits else 'NOT_FOUND', f'{len(hits)} people match')
        return hits[0]

    def prepare(self, c):
        if c['app'] == 'pm':
            self._pm_row(c)
            return {'title': c['title']}
        if c['app'] == 'research':
            doc = self._pick_latest_report()
            return {'title': doc['title'], 'summary': first_sentences(self.call('browser.extract', selector='#body')['text'])}
        return {'name': ' '.join(self._person(c)['text'].split()[:2])}

    def dispatch(self, c, plan):
        if c['app'] == 'pm':
            h = self._pm_row(c)                 # target check: the live row still carries the approved title
            self.call('browser.select', selector=f'{h} select', value=c['new_status'])
            return self.submit(f'{h} button', 'Saved')
        if c['app'] == 'research':
            doc = self._pick_latest_report()
            if doc['title'] != plan['title']:   # target check: same document family as approved
                raise TaskRefused('TARGET_CHANGED', 'a different document is now the latest')
            self.call('browser.type', selector='#report-body', text=plan['summary'])
            return self.submit('#save-report', 'Saved report')
        hit = self._person(c)
        if ' '.join(hit['text'].split()[:2]) != plan['name']:
            raise TaskRefused('TARGET_CHANGED', 'the matching person changed')
        self.call('browser.click', selector=self.call('browser.find', selector=f"{hit['handle']} a")['matches'][0]['handle'])
        self.call('browser.wait', selector='#card')
        if not self.call('browser.verify', text=c['department'])['verified']:
            raise TaskRefused('TARGET_CHANGED', 'the card shows another department')
        self.call('browser.type', selector='#followup-note', text=c['note'])
        return self.submit('#log-followup', 'Logged follow-up')


class IEECRunner(ComputerTaskRunner):
    """ComputerTaskRunner plus one consequential CRM task (a follow-up note) built from its own primitives."""
    def classify(self, text):
        m = re.match(r'log a follow-up for (\w+) from (\w+)(?:, not \w+ from (\w+))?: (.+)$', ' '.join(text.split()), re.I)
        if m:
            return 'log_followup', {'first': m.group(1), 'department': m.group(2), 'exclude': m.group(3) or '', 'note': m.group(4)}
        return super().classify(text)

    def _log_followup(self, p, approved_digest, proposal):
        found = self._find_person(p)        # visible-context resolution, refuses ambiguity, verified against the CRM DB
        if found['status'] != 'CONFIRMED':
            raise TaskRefused('NOT_FOUND', 'the person could not be verified')
        cid = found['person']['id']
        prop = {'task': 'log_followup', 'contact_id': cid, 'note_sha256': sha(p['note']), 'app': 'controlled:crm'}
        prop['digest'] = digest(prop)
        if approved_digest is None:
            return self._done('log_followup', ok=True, status='AWAITING_APPROVAL', proposal=prop, person=found['person'])
        if approved_digest != prop['digest'] or (proposal and proposal != prop):
            raise TaskRefused('APPROVAL_MISMATCH', 'the approved digest does not match the target as it is now: nothing was written')
        if not self.call('browser.find', selector=f'#card[data-contact-id="{cid}"]')['matches']:
            raise TaskRefused('STALE_TARGET', 'the open card is not the approved contact id')
        self.call('browser.type', selector='#followup-note', text=p['note'])
        self.call('browser.click', selector='#log-followup')
        self.call('browser.wait', text='Logged follow-up', timeout_ms=6000)
        mine = [f for f in self.verifier('followups', {'contact_id': cid})['followups'] if f['note'] == p['note']]
        ok = len(mine) == 1
        return self._done('log_followup', ok=ok, status='CONFIRMED' if ok else 'UNCERTAIN', effect={'contact_id': cid, 'followup_id': mine[0]['id'] if mine else None}, verified_against='application database')


PRE_DISPATCH_REFUSALS = {'APPROVAL_MISMATCH', 'STALE_TARGET', 'AMBIGUOUS_TARGET', 'NOT_FOUND', 'PERMISSION_DENIED', 'CANCELLED', 'UNSUPPORTED_TASK'}


class ResolveArm:
    """C: the runner's exact-digest proposal/approval, wrapped in the durable action ledger."""
    def __init__(self, runner: IEECRunner, ledger: Ledger, ablate: frozenset = frozenset()):
        self.r, self.ledger, self.ablate = runner, ledger, ablate

    @staticmethod
    def _target(prop):
        return prop.get('task_id') or prop.get('source_doc_id') or prop.get('contact_id')

    def prepare(self, c):
        res = self.r.run(c['command'])
        if res.get('status') != 'AWAITING_APPROVAL':
            raise TaskRefused((res.get('error') or {}).get('code', 'NO_PROPOSAL'), 'no proposal')
        prop = res['proposal']
        aid = 'act_' + prop['digest'][:24]
        self.ledger.create_action(Action(aid, 'bench', 1, prop['task'], str(self._target(prop)), 'bench_c', prop['app'], None, prop, None,
                                         {'kind': prop['task']}, 'NON_IDEMPOTENT', 'consequential', approval_digest=prop['digest']))
        for state in ('BOUND', 'NEEDS_APPROVAL', 'AUTHORIZED', 'READY'):   # the user approved exactly this digest
            self.ledger.transition(aid, state)
        return {'proposal': prop, 'action_id': aid}

    def reconcile(self, prop) -> int:
        """Read-only: how many effects matching the approved proposal does the application's database hold?"""
        v = self.r.verifier
        if prop['task'] == 'update_task':
            t = v('task', {'id': prop['task_id']})
            return int(t.get('status') == prop['new_status'] and t.get('version') == prop['observed_version'] + 1)
        if prop['task'] == 'save_report':
            return sum(1 for x in v('reports', {})['reports'] if x['source_doc_id'] == prop['source_doc_id'] and x['source_version'] == prop['source_version'] and sha(x['body']) == prop['summary_sha256'])
        return sum(1 for f in v('followups', {'contact_id': prop['contact_id']})['followups'] if sha(f['note']) == prop['note_sha256'])

    def dispatch(self, c, plan):
        prop, aid = plan['proposal'], plan['action_id']
        if self.ledger.action(aid)['status'] != 'READY' and 'single_use_approval' not in self.ablate:
            return False, 0, 'REFUSED:APPROVAL_ALREADY_CONSUMED'
        approved = prop
        if 'digest_binding' in self.ablate:   # ablation: silently re-approve whatever the target looks like now
            approved = self.r.run(c['command']).get('proposal') or prop
        if self.ledger.action(aid)['status'] == 'READY':
            self.ledger.transition(aid, 'DISPATCHING')   # persisted BEFORE any side effect
        res = self.r.run(c['command'], approved_digest=approved['digest'], proposal=approved)
        state = self.ledger.action(aid)['status']
        if res.get('status') == 'CONFIRMED':
            if state == 'DISPATCHING':
                for s in ('DISPATCHED', 'OBSERVING', 'CONFIRMED'):
                    self.ledger.transition(aid, s)
            return True, 0, 'CONFIRMED'
        code = (res.get('error') or {}).get('code', res.get('status'))
        if code in PRE_DISPATCH_REFUSALS:
            if state == 'DISPATCHING':
                self.ledger.transition(aid, 'FAILED', {'reason': code})
            return False, 0, f'HELD:{code}'
        if state == 'DISPATCHING':
            self.ledger.transition(aid, 'UNCERTAIN', {'reason': code})
            self.ledger.transition(aid, 'OBSERVING')
        n = self.reconcile(approved)          # never re-sent: only read back
        if state == 'DISPATCHING':
            self.ledger.transition(aid, 'CONFIRMED' if n == 1 else 'FAILED', {'reconciled_matches': n})
        return n == 1, 0, f'UNCERTAIN->reconciled:{n}'


# ---------------------------------------------------------------- resource sampling (Linux /proc; None elsewhere)
def browser_tree_rss_kb(root_pid: int) -> int | None:
    if not Path('/proc').is_dir():
        return None
    children: dict[int, list[int]] = {}
    for d in os.listdir('/proc'):
        if d.isdigit():
            try:
                ppid = int(Path(f'/proc/{d}/stat').read_text().rsplit(')', 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                continue
            children.setdefault(ppid, []).append(int(d))
    total, stack = 0, list(children.get(root_pid, []))   # descendants only: the Chromium processes
    while stack:
        pid = stack.pop()
        try:
            for line in Path(f'/proc/{pid}/status').read_text().splitlines():
                if line.startswith('VmRSS:'):
                    total += int(line.split()[1])
        except OSError:
            pass
        stack.extend(children.get(pid, []))
    return total


class RssSampler(threading.Thread):
    def __init__(self, interval=0.1):
        super().__init__(daemon=True)
        self.interval, self.peak_kb, self._stop_evt = interval, None, threading.Event()

    def run(self):
        while not self._stop_evt.is_set():
            kb = browser_tree_rss_kb(os.getpid())
            if kb is not None:
                self.peak_kb = max(self.peak_kb or 0, kb)
            self._stop_evt.wait(self.interval)

    def stop(self):
        self._stop_evt.set()
        self.join(5)
        return self.peak_kb


# ---------------------------------------------------------------- driver
def run_case(arm_name, case, apps, session, reg, bases, tmp: Path, ablate=frozenset()) -> dict:
    apps.reset()
    if case['pre']:
        apps.set_faults(**case['pre'])
    evidence: list = []
    ctx = ToolContext(user_id='bench_user', workspace_id='bench_ws', mission_id=f'{arm_name}-{case["scenario"]}-{case["seed"]}',
                      capabilities=BROWSER_CAPS, services=browser_services(session), record=lambda r: (evidence.append(r), f'ev{len(evidence)}')[1])

    def no_db(*_):
        raise AssertionError('baseline arms may not read the application database')
    if arm_name == 'C_resolve_ieec':
        runner = IEECRunner(reg, ctx, bases, apps.verify)
        arm = ResolveArm(runner, Ledger(tmp / f'ledger-{case["scenario"]}-{case["seed"]}.sqlite3'), ablate)
    else:
        runner = arm = (NaiveArm if arm_name == 'A_naive' else TargetGuardArm)(reg, ctx, bases, no_db)
    claimed, resends, finals = False, 0, []
    t0 = time.perf_counter()
    try:
        plan = arm.prepare(case)
        if case['between']:
            name, kw = case['between']
            getattr(apps, name)(**kw)
        for _ in range(case['dispatches']):
            try:
                out = arm.dispatch(case, plan)
                ok, retried = out[0], out[1]
                finals.append(out[2] if len(out) > 2 else ('SUCCESS_TEXT_SEEN' if ok else 'NO_SUCCESS_TEXT'))
            except TaskRefused as e:
                ok, retried = False, 0
                finals.append(f'REFUSED:{e.code}')
            claimed = claimed or ok
            resends += retried
    except TaskRefused as e:
        finals.append(f'NOT_PREPARED:{e.code}')
    except Exception as e:  # surfaced in the output and asserted on by the test; never silently scored
        finals.append(f'HARNESS_ERROR:{type(e).__name__}:{str(e)[:160]}')
    elapsed_ms = (time.perf_counter() - t0) * 1000
    o = oracle(apps.db_path, case)
    correct = o['effect_ok'] and (claimed if case['expect'] == 'APPLY_ONCE' else not claimed)
    return {'arm': arm_name, 'scenario': case['scenario'], 'seed': case['seed'], 'expect': case['expect'], 'claimed_success': claimed,
            'final': finals, 'resend_clicks': resends, 'tool_calls': len(runner.steps), 'latency_ms': round(elapsed_ms, 1),
            'correct': correct, 'false_success': claimed and not o['effect_ok'], **o}


def summarize(rows: list[dict], rss: dict) -> dict:
    out = {}
    for arm in dict.fromkeys(r['arm'] for r in rows):
        rs = [r for r in rows if r['arm'] == arm]
        lost = [r for r in rs if r['scenario'] == 'lost_ack']
        out[arm] = {'runs': len(rs), 'correct': sum(r['correct'] for r in rs),
                    'wrong_target_effects': sum(r['wrong_target_effects'] for r in rs),
                    'duplicate_effects': sum(r['duplicate_effects'] for r in rs),
                    'stale_or_unapproved_effects': sum(r['stale_or_unapproved_effects'] for r in rs),
                    'false_success_claims': sum(r['false_success'] for r in rs),
                    'lost_ack_runs': len(lost),
                    'lost_ack_recovered_without_resend': sum(1 for r in lost if r['correct'] and r['resend_clicks'] == 0),
                    'resend_clicks': sum(r['resend_clicks'] for r in rs),
                    'median_latency_ms': round(statistics.median(r['latency_ms'] for r in rs), 1),
                    'peak_browser_rss_mb': round(rss[arm] / 1024, 1) if rss.get(arm) else None}
    return out


def run_bench(seeds=range(1, 6), scenarios=tuple(SCENARIOS), arms=ARMS, ablate=frozenset(), measure_rss=True) -> dict:
    reg = default_registry()
    rows, rss = [], {}
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        server, base, apps = serve_controlled(str(tmp / 'apps.sqlite3'), test_mode=True)
        bases = {'pm': base, 'research': base, 'crm': base}
        policy = NetPolicy(allowed_origins=frozenset({origin_of(base)}))
        try:
            for arm in arms:
                sampler = RssSampler() if measure_rss else None
                if sampler:
                    sampler.start()
                session = CloudBrowserSession(f'bench_{arm[0]}', 'bench_user', tmp / f'profile_{arm}', policy=policy)
                try:
                    for scenario in scenarios:
                        for seed in seeds:
                            rows.append(run_case(arm, make_case(scenario, seed), apps, session, reg, bases, tmp, ablate))
                finally:
                    session.close()
                    if sampler:
                        rss[arm] = sampler.stop()
        finally:
            server.shutdown()
    return {'seeds': list(seeds), 'scenarios': list(scenarios), 'arms': list(arms), 'ablate': sorted(ablate), 'rows': rows, 'summary': summarize(rows, rss)}


def tables(res: dict) -> str:
    s = res['summary']
    cols = [('runs', 'runs'), ('correct', 'correct outcomes'), ('wrong_target_effects', 'wrong-target effects'), ('duplicate_effects', 'duplicate effects'),
            ('stale_or_unapproved_effects', 'stale/unapproved effects'), ('false_success_claims', 'false-success claims'),
            ('lost_ack_recovered_without_resend', 'lost-ack recovered w/o resend'), ('resend_clicks', 'resend clicks'),
            ('median_latency_ms', 'median latency ms'), ('peak_browser_rss_mb', 'peak Chromium RSS MB')]
    lines = ['| arm | ' + ' | '.join(c[1] for c in cols) + ' |', '|---' * (len(cols) + 1) + '|']
    for arm, v in s.items():
        cells = [f"{v[k]}/{v['lost_ack_runs']}" if k == 'lost_ack_recovered_without_resend' else str(v[k]) for k, _ in cols]
        lines.append(f'| {arm} | ' + ' | '.join(cells) + ' |')
    lines += ['', '| scenario (expected) | ' + ' | '.join(f'{a} correct' for a in s) + ' |', '|---' * (len(s) + 1) + '|']
    for sc in res['scenarios']:
        cells = []
        for a in s:
            rs = [r for r in res['rows'] if r['arm'] == a and r['scenario'] == sc]
            cells.append(f"{sum(r['correct'] for r in rs)}/{len(rs)} (wt {sum(r['wrong_target_effects'] for r in rs)}, dup {sum(r['duplicate_effects'] for r in rs)}, stale {sum(r['stale_or_unapproved_effects'] for r in rs)}, fs {sum(r['false_success'] for r in rs)})")
        lines.append(f'| {sc} ({SCENARIOS[sc][1]}) | ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--seeds', type=int, default=5, help='seeds 1..N per scenario (default 5)')
    ap.add_argument('--json', help='optional path for raw per-run rows + summary')
    a = ap.parse_args(argv)
    t0 = time.time()
    res = run_bench(seeds=range(1, a.seeds + 1))
    res['wall_seconds'] = round(time.time() - t0, 1)
    errors = [r for r in res['rows'] if any(f.startswith('HARNESS') for f in r['final'])]
    print(f"computer_bench: {len(res['rows'])} runs = {len(ARMS)} arms x {len(SCENARIOS)} scenarios x {a.seeds} seeds; wall {res['wall_seconds']} s; harness errors {len(errors)}\n")
    print(tables(res))
    print('\nwt = wrong-target effects, dup = duplicate effects, stale = effects on the right target but not the approved version/content, fs = false-success claims.')
    print('Effects are counted from each app\'s SQLite effect_log by an oracle that shares no code with the arms; controlled local baselines, not commercial agents.')
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=2, default=str))
        print(f'raw rows written to {a.json}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
