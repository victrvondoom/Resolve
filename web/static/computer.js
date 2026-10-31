/* RESOLVE "Live computer" panel: one supported task per request through POST /api/computer/run.
   Everything rendered here is what the server returned for that request: status, target ids and versions, registry
   steps with their evidence, and the frame captured from the server's real Chromium. Nothing is scripted or simulated.
   Browser actions are DOM-level (Playwright), not physical cursor movement. A session is shown as LIVE only if the
   server reports one; /api/computer/run uses a one-shot session that is closed when the request returns.
   Uses api(), esc(), errText() and setMsg() from app.js (loaded before this file). */
'use strict';

window.addEventListener('load', () => {
  const view = document.getElementById('view-computer');
  if (!view) return;
  const el = (id) => document.getElementById(id);
  const cmd = el('cmpCommand'), runBtn = el('cmpRun'), msg = el('cmpMsg');
  let pending = null, busy = false, enabled = true;

  const STATUS_CLASS = { CONFIRMED: 'chip-ok', REFUSED: 'chip-bad', MISMATCH: 'chip-bad', ERROR: 'chip-bad',
    UNCERTAIN: 'chip-warn', AWAITING_APPROVAL: 'chip-warn', NEEDS_CLARIFICATION: 'chip-warn', CANCELLED: 'chip-muted' };
  const setChip = (node, text, cls) => { node.className = 'chip ' + (cls || STATUS_CLASS[text] || 'chip-muted'); node.textContent = text; };
  const short = (h) => (h ? String(h).slice(0, 12) + '…' : '');
  const kv = (pairs) => pairs.filter(p => p[1] !== undefined && p[1] !== null && p[1] !== '')
    .map(([k, v, mono]) => `<dt>${esc(k)}</dt><dd${mono ? ' class="cmp-mono"' : ''}>${esc(v)}</dd>`).join('');

  function updateButtons() {
    runBtn.disabled = busy || !enabled;
    runBtn.title = !enabled ? 'Computer tasks are off on this server' : busy ? 'A task is running' : '';
    const stale = !!pending && cmd.value.trim() !== pending.command;
    el('cmpPropStale').hidden = !stale;
    const why = busy ? 'A task is running' : !pending ? 'No proposal is waiting' : '';
    const ap = el('cmpApprove'), cn = el('cmpCancel');
    ap.disabled = busy || !pending || stale; ap.title = why || (stale ? 'The task box was edited after this proposal' : '');
    cn.disabled = busy || !pending; cn.title = why;
  }

  function sessionChip(r) {
    if (r && r.session && r.session.live === true) return setChip(el('cmpSession'), 'LIVE SESSION', 'chip-ok');
    if (r && r.session) return setChip(el('cmpSession'), 'ONE-SHOT RUN · NOT LIVE', 'chip-muted');
    setChip(el('cmpSession'), 'NO SESSION', 'chip-muted');
  }

  function targetText(r) {
    const p = r.proposal, e = r.effect;
    if (r.task === 'update_task') {
      if (e) return `task #${e.task_id} · version ${e.version} · status ${e.status} (read from the app database)`;
      if (p) return `task #${p.task_id} · observed version ${p.observed_version} · ${p.app}`;
    }
    if (r.task === 'save_report') {
      if (e) return `report #${e.report_id ?? '—'} from document #${e.source_doc_id} v${e.source_version}`;
      if (p) return `document #${p.source_doc_id} v${p.source_version} · ${p.app}`;
    }
    if (r.document) return `document #${r.document.id} v${r.document.version} · ${r.document.title}`;
    if (r.person) return `contact #${r.person.id} · ${r.person.name} (${r.person.department})`;
    if (r.task === 'overdue_tasks' && r.project) return `Project ${r.project} (selected by project name; no per-task write)`;
    return 'not resolved';
  }

  function verifiedText(r) {
    if (r.status === 'CONFIRMED') return 'Independent verifier agreed: read the ' + (r.verified_against || 'application database') + ', not the page.';
    if (r.status === 'UNCERTAIN') return 'UNCERTAIN: the page reported a write the verifier could not confirm. It is not retried.';
    if (r.status === 'MISMATCH') return 'The page and the ' + (r.verified_against || 'application database') + ' disagree; not reported as done.';
    if (r.status === 'AWAITING_APPROVAL') return 'Nothing written yet: a proposal only reads.';
    if (r.status === 'NEEDS_CLARIFICATION') return 'Nothing chosen: more than one match, so RESOLVE asks instead of guessing.';
    if (r.status === 'REFUSED') return 'Refused: nothing was written.';
    return '—';
  }

  function bodyHtml(r) {
    const parts = [];
    if (r.error) parts.push(`<p class="msg err" id="cmpError"><b>${esc(r.error.code)}</b>: ${esc(r.error.message)}</p>`);
    if (Array.isArray(r.overdue_rows)) {
      parts.push(`<h3 class="cmp-h3">Overdue in Project ${esc(r.project)} (${r.overdue_rows.length}, as read from the page)</h3>` +
        '<div class="cmp-table-wrap"><table class="cmp-table" id="cmpRows"><thead><tr><th>Task</th><th>Owner</th><th>Due</th></tr></thead><tbody>' +
        r.overdue_rows.map(x => `<tr><td>${esc(x.title)}</td><td>${esc(x.owner)}</td><td>${esc(x.due)}</td></tr>`).join('') + '</tbody></table></div>');
    } else if (Array.isArray(r.overdue)) {
      parts.push('<ul id="cmpRows">' + r.overdue.map(t => `<li>${esc(t)}</li>`).join('') + '</ul>');
    }
    if (r.summary && r.status !== 'AWAITING_APPROVAL') parts.push(`<h3 class="cmp-h3">Summary <span class="small muted">(${esc(r.summary_kind || 'extractive')})</span></h3><blockquote class="cmp-quote">${esc(r.summary)}</blockquote>`);
    if (r.person) parts.push(`<dl class="cmp-kv" id="cmpPerson">${kv([['Name', r.person.name], ['Department', r.person.department], ['Email', r.person.email], ['Stable id', r.person.id], ['Excluded department', r.excluded_department]])}</dl>`);
    if (r.status === 'NEEDS_CLARIFICATION' && Array.isArray(r.candidates)) {
      parts.push(`<div class="cmp-clarify" id="cmpClarify" role="group" aria-labelledby="cmpClarifyQ"><p id="cmpClarifyQ"><b>${esc(r.question || 'Which one do you mean?')}</b></p>` +
        '<ul>' + r.candidates.map((c, i) => `<li><button class="btn btn-small" type="button" data-cand="${i}">${esc(c.name)} · ${esc(c.department)}</button></li>`).join('') + '</ul>' +
        '<p class="small muted">Choosing one runs the task again with that department; the contact is then bound by its stable id.</p></div>');
    }
    if (r.effect && r.status !== 'AWAITING_APPROVAL') parts.push(`<h3 class="cmp-h3">Effect read back</h3><dl class="cmp-kv" id="cmpEffect">${kv(Object.entries(r.effect).map(([k, v]) => [k, v]))}</dl>`);
    return parts.join('');
  }

  function stepsHtml(r) {
    const ev = new Map((r.evidence || []).map(e => [e.ref, e]));
    const steps = r.steps || [];
    if (!steps.length) return '<li class="muted">No tool step ran.</li>';
    return steps.map((s, i) => {
      const e = ev.get(s.evidence_ref) || (r.evidence || [])[i] || {};
      return `<li class="${s.ok ? 'ok' : 'bad'}"><b>${esc(s.tool)}</b> <span class="chip ${s.ok ? 'chip-ok' : 'chip-bad'} small">${s.ok ? 'OK' : esc(s.error || 'FAILED')}</span>` +
        `<span class="small muted cmp-mono"> evidence ${esc(s.evidence_ref || 'none')}${e.args_sha256 ? ' · args sha256 ' + esc(short(e.args_sha256)) : ''}</span></li>`;
    }).join('');
  }

  function renderProposal(r, command) {
    const card = el('cmpProposal');
    if (r.status !== 'AWAITING_APPROVAL' || !r.proposal) { pending = null; card.hidden = true; return; }
    pending = { command, proposal: r.proposal };
    const p = r.proposal;
    const fields = [['Task', p.task], ['App', p.app], ['Task id', p.task_id], ['Observed version', p.observed_version], ['New status', p.new_status],
      ['Source document', p.source_doc_id], ['Source version', p.source_version], ['Summary sha256', p.summary_sha256, true]];
    if (r.current_row) fields.push(['Row on the page now', Array.isArray(r.current_row) ? r.current_row.slice(0, 3).join(' · ') : r.current_row]);
    if (r.document) fields.push(['Document', `${r.document.title} v${r.document.version}`]);
    if (r.summary) fields.push(['Report text to save', r.summary]);
    fields.push(['Digest (sha256)', p.digest, true]);
    el('cmpPropFields').innerHTML = kv(fields);
    el('cmpPropFields').querySelector('dd:last-child').id = 'cmpDigest';
    el('cmpPropCmd').textContent = command;
    card.hidden = false;
  }

  function render(r, command) {
    sessionChip(r);
    el('cmpResult').hidden = false;
    setChip(el('cmpStatus'), r.status || 'UNKNOWN');
    el('cmpTask').textContent = r.task || '—';
    el('cmpTarget').textContent = targetText(r);
    el('cmpVerified').textContent = verifiedText(r);
    el('cmpBody').innerHTML = bodyHtml(r);
    el('cmpSteps').innerHTML = stepsHtml(r);
    const f = r.frame || {}, img = el('cmpFrame');
    if (typeof f.data_url === 'string' && f.data_url.startsWith('data:image/')) {
      img.src = f.data_url; img.hidden = false;
      el('cmpFrameCap').textContent = `Final frame from the server's Chromium after the last step · ${f.title || ''} · ${f.url || ''}`;
    } else { img.hidden = true; img.removeAttribute('src'); el('cmpFrameCap').textContent = 'No frame returned.'; }
    renderProposal(r, command);
    el('cmpBody').querySelectorAll('button[data-cand]').forEach(b => b.addEventListener('click', () => {
      const c = r.candidates[Number(b.dataset.cand)];
      cmd.value = `Find ${String(c.name).split(/\s+/)[0]} from ${c.department}`;
      run(cmd.value, null);
    }));
  }

  async function run(command, digest) {
    command = String(command || '').trim();
    if (command.length < 3) { setMsg(msg, 'Type a task first (at least 3 characters).', 'err'); return; }
    busy = true; updateButtons();
    setMsg(msg, digest ? 'Sending your approval; the server re-checks the target before writing…' : 'Running in a real Chromium on the server (DOM-level actions)…');
    try {
      const r = await api('/api/computer/run', digest ? { command, approved_digest: digest } : { command });
      render(r, command);
      setMsg(msg, `${r.task}: ${r.status}`, r.status === 'CONFIRMED' ? 'ok' : undefined);
    } catch (e) {
      if (e.status === 409 && e.body && e.body.status === 'REFUSED') {   // a refusal still carries steps, evidence and the frame
        render(e.body, command);
        pending = null; el('cmpProposal').hidden = true;
        setMsg(msg, `${e.body.task}: REFUSED (${e.body.error && e.body.error.code}). Nothing was written.`, 'err');
      } else {
        setMsg(msg, 'Not run: ' + errText(e), 'err');
      }
    } finally { busy = false; updateButtons(); }
  }

  runBtn.addEventListener('click', () => { pending = null; el('cmpProposal').hidden = true; run(cmd.value, null); });
  cmd.addEventListener('input', updateButtons);
  cmd.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !runBtn.disabled) runBtn.click(); });
  el('cmpExamples').addEventListener('click', (e) => { const b = e.target.closest('button[data-cmd]'); if (b) { cmd.value = b.dataset.cmd; updateButtons(); cmd.focus(); } });
  el('cmpApprove').addEventListener('click', () => { if (pending) run(pending.command, pending.proposal.digest); });
  el('cmpCancel').addEventListener('click', () => {
    pending = null; el('cmpProposal').hidden = true;
    setChip(el('cmpStatus'), 'CANCELLED');
    el('cmpVerified').textContent = 'Cancelled in this browser. No approval was sent, and the proposal only read, so nothing changed.';
    setMsg(msg, 'Cancelled: no approval was sent; nothing changed.');
    updateButtons();
  });

  if (window.ResolveVoice) window.ResolveVoice.attach({ btn: el('cmpMic'), badge: el('cmpMicBadge'), note: el('cmpMicNote'), target: cmd });
  fetch('/api/health').then(r => r.json()).then(h => {
    enabled = !!(h.features && h.features.diagnostics);
    if (!enabled) setMsg(msg, 'Computer tasks are off on this server: they launch Chromium in the server process (enable with RESOLVE_DIAGNOSTICS=1).', 'err');
    updateButtons();
  }).catch(() => { /* the run itself reports the server's answer */ });
  updateButtons();
});
