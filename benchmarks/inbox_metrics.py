"""Measure the inbox operator on labelled fixtures (v1: 12 earlier fixtures, v2: 32 scenarios written before scoring).

    python benchmarks/inbox_metrics.py  -> writes benchmarks/results/inbox_metrics.json and prints a report

Engines are reported separately. RULES_ONLY is measured here. MODEL_ENABLED is NOT measured (no model provider is
configured in this environment). REPLAY_EXECUTION uses the scripted Gmail double from tests/test_send_broker.py (a
simulation, not a provider). LIVE provider results are not measured: no OAuth credentials and no test account exist here.
Ledger scoring goes through a real InboxStore in a temporary directory, so duplicate control and revisions are measured
on the persisted ledger, not on raw extraction.
"""
import base64
import datetime as dt
import json
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))
import inbox_intel as ii  # noqa: E402
from at_rest import SealedStore  # noqa: E402
from inbox_api import InboxApi  # noqa: E402,F401  (import check: the API module loads)
from inbox_service import draft_reply  # noqa: E402
from inbox_store import InboxError, InboxStore  # noqa: E402

CATEGORIES = list(ii.CATEGORIES)


def ts(iso):
    return dt.datetime.fromisoformat(iso + '+00:00').timestamp() if '+' not in iso else dt.datetime.fromisoformat(iso).timestamp()


def load():
    v1 = json.loads((ROOT / 'benchmarks' / 'inbox_fixtures.json').read_text())['fixtures']
    v1n = []
    for fx in v1:
        exp = fx['expect']
        commits = [{'kind': c['kind'], 'owner': 'none' if c['kind'] == 'DEPENDENCY' else ('user' if c['kind'] in ('REQUEST_TO_USER', 'PROMISE_BY_USER') else 'other'),
                    'deadline': c.get('deadline')} for c in exp.get('commitments', [])]
        v1n.append({'id': fx['id'], 'name': fx['name'], 'user_address': fx['user_address'], 'messages': fx['messages'], 'version': 1,
                    'expect': {'category': exp['category'], 'reply_needed': None, 'urgent': None, 'injection': False, 'commitments': commits}})
    v2 = json.loads((ROOT / 'benchmarks' / 'inbox_fixtures_v2.json').read_text())['fixtures']
    for fx in v2:
        fx['version'] = 2
    return v1n, v2


def run(fx, tz='UTC'):
    """Persist the thread in a temporary store and return (decision, ledger, drafts, store, ids, reprocess_new)."""
    tmp = tempfile.TemporaryDirectory()
    key = base64.urlsafe_b64encode(bytes(range(32))).decode()
    store = InboxStore(Path(tmp.name) / 'i.sqlite3', SealedStore(Path(tmp.name) / 'k', env={'RESOLVE_MASTER_KEY': key}))
    cid = store.upsert_connection('u', 'w', 'gmail', 'CONNECTED', ['gmail.readonly'])['id']
    thread_id = None
    for m in fx['messages']:
        res = store.upsert_message('u', 'w', cid, 'thread-' + fx['id'], m['id'], m['sender'], ts(m['sent_at']), m.get('subject', 'Thread'), m['body'],
                                   recipients=m.get('to', ''))
        thread_id = res['thread_id']
    thread = [{'id': x['id'], 'sender': x['sender'], 'to': x.get('to', ''), 'subject': x.get('subject', ''), 'body': x['body'], 'sent_at': x['sent_at']}
              for x in store.list_messages('u', 'w', thread_id)]
    thread = [{**t, 'sent_at': t['sent_at']} for t in thread]
    for t in thread:
        t['sent_at'] = t['sent_at']
    decision = ii.triage_thread(thread, fx['user_address'], tz)
    store.save_triage('u', 'w', thread_id, decision)
    def reconcile_all():
        new = 0
        for c in ii.extract_commitments(thread, fx['user_address'], tz):
            r = store.reconcile_obligation('u', 'w', thread_id, c['kind'], c['obligation_key'], c)
            new += 1 if r['action'] == 'CREATED' else 0
        return new
    first = reconcile_all()
    second = reconcile_all()                       # re-processing the same thread must create nothing
    ledger = store.list_commitments('u', 'w')
    return decision, ledger, thread, fx['user_address'], second, tmp


def owner_of(c, user):
    if c['responsible_person'] is None:
        return 'none'
    return 'user' if c['responsible_person'] == user else 'other'


def score_fixture(fx, decision, ledger, thread, user):
    exp = fx['expect']
    out = {'id': fx['id'], 'name': fx['name'], 'version': fx['version'], 'category_expected': exp['category'], 'category_got': decision['category'],
           'category_ok': exp['category'] == decision['category']}
    last = max(thread, key=lambda m: m['sent_at'])
    from_user_last = user in last['sender'].lower()
    predicted_reply = decision['category'] in ('REPLY_REQUIRED', 'ACTION_REQUIRED', 'DEADLINE_TIME_SENSITIVE') and not from_user_last
    out['reply_pred'] = predicted_reply
    out['reply_expected'] = exp.get('reply_needed')
    out['urgent_pred'] = decision['priority'] == 'HIGH'
    out['urgent_expected'] = exp.get('urgent')
    # commitments: greedy match on (kind, owner)
    expected = list(exp.get('commitments', []))
    got = [{'kind': c['kind'], 'owner': owner_of(c, user), 'deadline': c['deadline_at'], 'conf': c['deadline_confidence'],
            'review': c['review_reason']} for c in ledger]
    tp, matched_owner, matched_deadline, amb_expected, amb_detected = 0, 0, 0, 0, 0
    used = set()
    deadline_checks = []
    for e in expected:
        idx = next((i for i, g in enumerate(got) if i not in used and g['kind'] == e['kind']), None)
        if idx is None:
            continue
        used.add(idx)
        tp += 1
        g = got[idx]
        matched_owner += 1 if g['owner'] == e['owner'] else 0
        if e['deadline'] == 'AMBIG':
            amb_expected += 1
            amb_detected += 1 if (g['conf'] == 'AMBIGUOUS' or g['review']) else 0
            deadline_checks.append(g['deadline'] is None)
        elif e['deadline'] is None:
            deadline_checks.append(g['deadline'] is None)
        else:
            ok = g['deadline'] is not None and dt.datetime.fromtimestamp(g['deadline'], dt.timezone.utc).date().isoformat() == e['deadline']
            deadline_checks.append(ok)
            matched_deadline += 1 if ok else 0
    out['commit_expected'] = len(expected)
    out['commit_ledger'] = len(got)
    out['commit_tp'] = tp
    out['owner_correct'] = matched_owner
    out['deadline_checks'] = deadline_checks
    out['ambiguity_expected'] = amb_expected
    out['ambiguity_detected'] = amb_detected
    out['extra_commitments'] = max(0, len(got) - len(expected)) if 'commitments' in exp else 0   # only fully labelled lists count extras
    out['injection_expected'] = bool(exp.get('injection'))
    out['injection_resisted'] = (decision['category'] == 'UNCERTAIN_HUMAN_REVIEW' and len(got) == 0) if exp.get('injection') else None
    return out


def prf(tp, fp, fn):
    p = tp / (tp + fp) if (tp + fp) else None
    r = tp / (tp + fn) if (tp + fn) else None
    return p, r


def round_or_none(x):
    return None if x is None else round(x, 3)


def draft_claims(fx, thread):
    """A reply that states a price, a currency amount, an estimate or a calendar date that appears nowhere in the thread is unsupported."""
    d = draft_reply(thread, fx['user_address'], 'Best', 'neutral', {})
    body = d['body']
    source = ' '.join(m['body'] for m in thread).lower()
    claims = re.findall(r'(\$\s?\d[\d,]*|€\s?\d[\d,]*|\d[\d,]*\s?(?:eur|usd|dollars|euros)|\bestimate\b|\bprice\b|\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}\b)', body, re.I)
    # a date the thread itself resolved (for example 'Friday, October 30' from 'by Friday') is supported by the thread
    for c in ii.extract_commitments(thread, fx['user_address']):
        if c['deadline_at']:
            d = dt.datetime.fromtimestamp(c['deadline_at'], dt.timezone.utc)
            source += ' ' + d.strftime('%B %d').lower() + ' ' + d.strftime('%B %-d').lower() + ' ' + d.strftime('%A').lower()
    unsupported = [c for c in claims if c.lower() not in source]
    return len(unsupported), len(claims)


def send_path_scenarios():
    """Scripted Gmail double (simulation). Ground truth comes from what the double actually stored."""
    from test_send_broker import FakeGmail, BODY
    from send_broker import SendBroker, operation_key  # noqa: F401
    rows = []
    cases = [('ok', 'ok', 'delivered once; read back'), ('timeout_after_store', 'ok', 'stored; response lost; retry must find it'),
             ('timeout_before_store', 'ok', 'never stored; retry must not send silently'), ('store_wrong_recipient', 'ok', 'stored a different recipient'),
             ('store_altered_body', 'ok', 'stored an altered body'), ('drop_message_id', 'ok', 'correlation header missing')]
    for behaviour, _retry, label in cases:
        tmp = tempfile.TemporaryDirectory()
        key = base64.urlsafe_b64encode(bytes(range(32))).decode()
        store = InboxStore(Path(tmp.name) / 'i.sqlite3', SealedStore(Path(tmp.name) / 'k', env={'RESOLVE_MASTER_KEY': key}))
        U, W = 'u', 'w'
        cid = store.upsert_connection(U, W, 'gmail', 'CONNECTED', ['gmail.readonly', 'gmail.compose', 'gmail.send'])['id']
        m = store.upsert_message(U, W, cid, 't', 'pm', 'Sam <sam@partner.io>', time.time(), 'Proposal', 'Could you send the proposal?')
        created = store.create_draft(U, W, m['thread_id'], 'sam@partner.io', 'Proposal', BODY)
        draft = store.get_draft(U, W, created['id'])
        clock = [1000.0]
        g = FakeGmail()
        g.send_behaviour = behaviour
        broker = SendBroker(store, g, lambda: clock[0])
        results = []
        for attempt in range(2):
            g.send_behaviour = behaviour if attempt == 0 else 'ok'
            try:
                aid = store.approve(U, W, draft['id'], draft['digest'], 'SEND_EMAIL', clock[0])['id']
            except InboxError as e:
                # a verified draft is terminal: a second approval of the same content is refused before any dispatch
                results.append({'state': 'REFUSED_TERMINAL_DRAFT', 'reason': str(e.message)})
                continue
            results.append(broker.send_email(U, W, aid, draft['id'], draft['digest']))
        sends = g.calls.count('send')
        delivered = sum(1 for raw in g.sent.values() if parse_stored(raw, draft))
        first, second = results
        rows.append({'scenario': behaviour, 'label': label, 'first_state': first['state'], 'first_reason': first.get('reason'),
                     'second_state': second['state'], 'sends_dispatched': sends, 'stored_exact_matches': delivered,
                     'false_success': first['state'] == 'VERIFIED' and delivered == 0,
                     'duplicate_send': sends > 1})
        tmp.cleanup()
    return rows


def parse_stored(raw, draft):
    from gmail_adapter import parse_raw, normalize_body
    p = parse_raw(raw)
    return p['to'].strip() == draft['recipient'] and p['subject'] == draft['subject'] and normalize_body(p['body']) == normalize_body(draft['body']) and p['message_id'] != ''


def write_report(m):
    """docs/INBOX_BENCHMARK_REPORT.md is generated from the results file; no number is typed by hand."""
    c = m['category']
    def pct(x):
        return 'n/a' if x is None else f'{x * 100:.1f}%'
    lines = ['# Inbox operator benchmark report', '',
             '_Generated by `benchmarks/inbox_metrics.py` from `benchmarks/results/inbox_metrics.json`. Do not edit by hand._', '',
             '**Scope.** Synthetic, labelled fixtures in this repository. These numbers do not describe real inboxes or live provider behaviour.', '',
             '## Engines', '']
    lines += [f'- **{k}**: {v}' for k, v in m['engines'].items()]
    lines += ['', '## Classification', '', '| Set | Fixtures | Category accuracy |', '|---|---:|---:|',
              f"| v1 earlier fixtures (unchanged labels) | {c['v1_earlier_fixtures']['fixtures']} | {pct(c['v1_earlier_fixtures']['category_accuracy'])} |",
              f"| v2 new scenarios (written before scoring) | {c['v2_new_scenarios']['fixtures']} | {pct(c['v2_new_scenarios']['category_accuracy'])} |",
              '', '### Precision and recall by category (v2 scenarios)', '', '| Category | Precision | Recall | Support |', '|---|---:|---:|---:|']
    for k, v in c['v2_new_scenarios']['per_category'].items():
        lines.append(f"| {k} | {pct(v['precision'])} | {pct(v['recall'])} | {v['support']} |")
    ce = m['commitment_extraction']; an = m['ambiguity_detection']; rn = m['reply_needed']; dc = m['duplicate_commitments']
    lines += ['', '## Commitments (persisted ledger)', '', '| Measure | Result |', '|---|---:|',
              f"| Expected commitments | {ce['expected']} |", f"| Ledger items | {ce['ledger_items']} |",
              f"| Kind matched | {ce['matched_kind']} |", f"| Precision | {pct(ce['precision'])} |", f"| Recall | {pct(ce['recall'])} |",
              f"| Owner identification accuracy | {pct(m['owner_identification_accuracy'])} |",
              f"| Deadline accuracy | {pct(m['deadline_accuracy'])} |",
              f"| Ambiguous deadlines flagged | {an['detected']} of {an['expected']} |",
              f"| Extra items beyond fully labelled lists | {dc['extra_items_beyond_expected']} |",
              f"| New items on a second pass over the same thread (duplicate control) | {dc['reprocess_new_items_on_second_pass']} |",
              '', '## Replies, urgency and injection', '', '| Measure | Result |', '|---|---:|',
              f"| Reply-needed precision | {pct(rn['precision'])} (tp {rn['tp']}, fp {rn['fp']}) |",
              f"| Reply-needed recall | {pct(rn['recall'])} (fn {rn['fn']}) |",
              f"| False urgent rate (HIGH on non-urgent) | {pct(m['false_urgent_rate'])} |",
              f"| Unsupported draft claims (price, amount, estimate, date not in the thread) | {pct(m['unsupported_draft_claim_rate'])} |",
              f"| Prompt-injection fixtures resisted | {m['injection_resistance']['resisted']} of {m['injection_resistance']['fixtures']} |",
              '', '## Send path (scripted provider double, simulation)', '', '| Scenario | First attempt | Second attempt | Dispatches | Exact stored matches | False success |', '|---|---|---|---:|---:|---|']
    for r in m['send_path_simulation']:
        lines.append(f"| {r['scenario']} | {r['first_state']} | {r['second_state']} | {r['sends_dispatched']} | {r['stored_exact_matches']} | {r['false_success']} |")
    lines += ['', f"False successes: {m['send_path_summary']['false_success']} of {m['send_path_summary']['scenarios']}. Duplicate sends: {m['send_path_summary']['duplicate_sends']}.", '',
              '## Known misses (kept visible, not tuned away)', '']
    for s in m['scored_fixtures']:
        if not s['category_ok'] or s['commit_tp'] != s['commit_expected'] or s['extra_commitments']:
            lines.append(f"- {s['id']} `{s['name']}`: expected {s['category_expected']}, got {s['category_got']}; commitments matched {s['commit_tp']} of {s['commit_expected']}, ledger {s['commit_ledger']}.")
    lines += ['', 'V32 is counted as a miss. On review its label looks wrong: the partner\'s earlier request is a genuine, unquoted request that is still unanswered, so a reply is still owed. The label was kept as written, not changed to match the output.', '',
              'Not measured: MODEL_ENABLED (no model provider configured), LIVE provider (no OAuth credentials or authorized test account).']
    (ROOT / 'docs' / 'INBOX_BENCHMARK_REPORT.md').write_text('\n'.join(lines) + '\n')


def main():
    v1, v2 = load()
    fixtures = v1 + v2
    scored, draft_unsupported, draft_total, reprocess_new = [], 0, 0, 0
    raw_rows = []
    for fx in fixtures:
        decision, ledger, thread, user, second, tmp = run(fx)
        s = score_fixture(fx, decision, ledger, thread, user)
        scored.append(s)
        reprocess_new += second
        if fx['expect'].get('reply_needed') or fx['expect']['category'] in ('REPLY_REQUIRED', 'ACTION_REQUIRED', 'DEADLINE_TIME_SENSITIVE'):
            u, t = draft_claims(fx, thread)
            draft_unsupported += u
            draft_total += max(t, 0)
        raw_rows.append({'id': fx['id'], 'name': fx['name'], 'expected_category': fx['expect']['category'], 'got_category': decision['category'],
                         'priority': decision['priority'], 'ledger': [{'kind': c['kind'], 'owner': owner_of(c, user), 'deadline_at': c['deadline_at'],
                                                                         'deadline_confidence': c['deadline_confidence']} for c in ledger],
                         'expected_commitments': fx['expect'].get('commitments', [])})
        tmp.cleanup()

    def cat_summary(subset):
        n = len(subset)
        correct = sum(1 for s in subset if s['category_ok'])
        per = {}
        for c in CATEGORIES:
            tp = sum(1 for s in subset if s['category_expected'] == c and s['category_got'] == c)
            fp = sum(1 for s in subset if s['category_expected'] != c and s['category_got'] == c)
            fn = sum(1 for s in subset if s['category_expected'] == c and s['category_got'] != c)
            p, r = prf(tp, fp, fn)
            if tp + fp + fn:
                per[c] = {'precision': round_or_none(p), 'recall': round_or_none(r), 'support': tp + fn}
        return {'fixtures': n, 'category_accuracy': round(correct / n, 3) if n else None, 'per_category': per}

    v1s = [s for s in scored if s['version'] == 1]
    v2s = [s for s in scored if s['version'] == 2]
    exp_total = sum(s['commit_expected'] for s in scored)
    tp_total = sum(s['commit_tp'] for s in scored)
    ledger_total = sum(s['commit_ledger'] for s in scored)
    owner_ok = sum(s['owner_correct'] for s in scored)
    dl_checks = [c for s in scored for c in s['deadline_checks']]
    amb_exp = sum(s['ambiguity_expected'] for s in scored)
    amb_det = sum(s['ambiguity_detected'] for s in scored)
    reply_tp = sum(1 for s in scored if s['reply_pred'] and s['reply_expected'] is True)
    reply_fp = sum(1 for s in scored if s['reply_pred'] and s['reply_expected'] is False)
    reply_fn = sum(1 for s in scored if (not s['reply_pred']) and s['reply_expected'] is True)
    urgent_neg = [s for s in scored if s['urgent_expected'] is False]
    false_urgent = sum(1 for s in urgent_neg if s['urgent_pred'])
    inj = [s for s in scored if s['injection_expected']]
    extra_total = sum(s['extra_commitments'] for s in scored)
    send = send_path_scenarios()
    commit_p, commit_r = prf(tp_total, ledger_total - tp_total, exp_total - tp_total)
    reply_p, reply_r = prf(reply_tp, reply_fp, reply_fn)
    metrics = {
        'engines': {'RULES_ONLY': 'measured', 'MODEL_ENABLED': 'NOT MEASURED (no model provider configured)',
                    'REPLAY_EXECUTION': 'simulated broker run with scripted Gmail double (tests/test_send_broker.py)',
                    'LIVE_PROVIDER': 'NOT MEASURED (no OAuth credentials or authorized test account in this environment)'},
        'category': {'all': cat_summary(scored), 'v1_earlier_fixtures': cat_summary(v1s), 'v2_new_scenarios': cat_summary(v2s)},
        'fixtures_fully_passing_v1': sum(1 for s in v1s if s['category_ok'] and s['commit_tp'] == s['commit_expected'] and s['extra_commitments'] == 0),
        'commitment_extraction': {'expected': exp_total, 'ledger_items': ledger_total, 'matched_kind': tp_total,
                                  'precision': round_or_none(commit_p), 'recall': round_or_none(commit_r)},
        'owner_identification_accuracy': round_or_none(owner_ok / exp_total) if exp_total else None,
        'deadline_accuracy': round_or_none(sum(1 for c in dl_checks if c) / len(dl_checks)) if dl_checks else None,
        'ambiguity_detection': {'expected': amb_exp, 'detected': amb_det, 'rate': round_or_none(amb_det / amb_exp) if amb_exp else None},
        'reply_needed': {'precision': round_or_none(reply_p), 'recall': round_or_none(reply_r), 'tp': reply_tp, 'fp': reply_fp, 'fn': reply_fn,
                         'note': 'fixtures with reply_needed labels only (v2); v1 has no reply label'},
        'false_urgent_rate': round_or_none(false_urgent / len(urgent_neg)) if urgent_neg else None,
        'duplicate_commitments': {'extra_items_beyond_expected': extra_total, 'reprocess_new_items_on_second_pass': reprocess_new},
        'unsupported_draft_claim_rate': round_or_none(draft_unsupported / draft_total) if draft_total else None,
        'injection_resistance': {'fixtures': len(inj), 'resisted': sum(1 for s in inj if s['injection_resisted'])},
        'send_path_simulation': send,
        'send_path_summary': {'false_success': sum(1 for r in send if r['false_success']), 'duplicate_sends': sum(1 for r in send if r['duplicate_send']),
                              'scenarios': len(send)},
        'raw_fixture_results': raw_rows,
        'scored_fixtures': scored,
        'note': 'Measured on labelled fixtures in this repository only. Not a claim about real inboxes or live provider behaviour.',
    }
    dest = ROOT / 'benchmarks' / 'results' / 'inbox_metrics.json'
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(metrics, indent=2, default=str))
    short = {k: metrics[k] for k in ('engines', 'category', 'fixtures_fully_passing_v1', 'commitment_extraction', 'owner_identification_accuracy',
                                     'deadline_accuracy', 'ambiguity_detection', 'reply_needed', 'false_urgent_rate', 'duplicate_commitments',
                                     'unsupported_draft_claim_rate', 'injection_resistance', 'send_path_summary')}
    write_report(metrics)
    print(json.dumps(short, indent=2, default=str))
    print('\nPer-fixture (expected vs got):')
    for s in scored:
        flag = 'ok ' if s['category_ok'] else 'MISS'
        print(f"  {s['id']} {flag} expected={s['category_expected']:<28} got={s['category_got']:<28} commits {s['commit_tp']}/{s['commit_expected']} ledger={s['commit_ledger']}  {s['name']}")
    print('\nSend-path simulation:')
    for r in send:
        print(f"  {r['scenario']:<22} first={r['first_state']:<12} second={r['second_state']:<16} sends={r['sends_dispatched']} stored_exact={r['stored_exact_matches']} false_success={r['false_success']}")


if __name__ == '__main__':
    main()
