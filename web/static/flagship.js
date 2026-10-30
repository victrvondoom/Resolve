/* RESOLVE shell (account gate, server mode, account menu) and the flagship mission view.
   Every status, number and timestamp shown here comes from the server's recorded mission events; nothing is scripted.
   Uses $, api(), authHeaders(), esc(), errText(), setMsg(), chipClass(), showView() from app.js, resolved at load time. */
'use strict';

/* ---------------- account gate (production mode has no shared demo token) ---------------- */
window.ResolveAuth = {
  prompt() {
    return new Promise((resolve) => {
      const byId = (i) => document.getElementById(i);
      const dlg = byId('authDialog'), msg = byId('authMsg'), email = byId('authEmail'), pw = byId('authPassword');
      const buttons = ['authDemo', 'authLogin', 'authRegister'].map(byId);
      const setBusy = (on, text) => {
        buttons.forEach(b => { b.disabled = on; b.title = on ? 'Working…' : ''; });
        msg.textContent = text || ''; msg.className = 'msg';
      };
      const fail = (text, field) => { setBusy(false); msg.textContent = text; msg.className = 'msg err'; if (field) field.focus(); };
      const finish = (session) => {
        try { sessionStorage.setItem('resolve_token', session.token); } catch (_) { /* storage may be blocked; the token still lives in memory */ }
        dlg.close(); resolve(session.token);
      };
      const call = async (path, em, pass, label, extra) => {
        setBusy(true, label);
        try {
          const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: em, password: pass, ...(extra || {}) }) });
          const d = await r.json().catch(() => ({}));
          if (!r.ok) return fail(d.error || ('HTTP ' + r.status));
          finish(d);
        } catch (e) { fail('Network error: ' + (e.message || e)); }
      };
      const valid = () => {
        if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.value.trim())) { fail('Enter a valid email address.', email); return false; }
        if (pw.value.length < 10) { fail('Passwords are at least 10 characters.', pw); return false; }
        return true;
      };
      byId('authForm').onsubmit = (e) => { e.preventDefault(); if (valid()) call('/api/auth/login', email.value.trim(), pw.value, 'Signing in…'); };
      byId('authRegister').onclick = () => { if (valid()) call('/api/auth/register', email.value.trim(), pw.value, 'Creating your account…'); };
      byId('authDemo').onclick = () => {
        const hex = [...crypto.getRandomValues(new Uint8Array(12))].map(b => b.toString(16).padStart(2, '0')).join('');
        call('/api/auth/register', 'demo-' + hex.slice(0, 10) + '@resolve.demo', hex + 'Aa1', 'Creating your demo account…', { display_name: 'Demo' });
      };
      dlg.addEventListener('cancel', (e) => e.preventDefault());   // no anonymous access in production
      if (!dlg.open) dlg.showModal();
      byId('authDemo').focus();
    });
  }
};

window.addEventListener('load', () => {
  const view = document.getElementById('view-flagship');
  if (!view) return;
  const el = (id) => document.getElementById(id);
  const reduceMotion = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
  const canInlinePdf = navigator.pdfViewerEnabled !== false;   // false on phones: an iframe would show nothing
  let mission = null, timer = null, previewUrl = null, docs = [], projects = { bundled: [], uploaded: [] };
  let renderedDigest = null, lastState = null, watching = false, busy = false, production = false, diagnosticsOn = false;
  let frameUrl = null, frameId = null, pendingCmd = null, intentVoice = false, cmdVoice = false;

  const NODES = [
    ['DOCUMENT_READER', 'Document reader', 'Extracts the requirements from your brief.'],
    ['REPOSITORY_ANALYZER', 'Repository analyzer', 'Scans the project for issues, in parallel with the reader.'],
    ['ARCHITECTURE_PLANNER', 'Architecture planner', 'Cites each issue and proposes a corrected design.'],
    ['REPORT_COMPOSER', 'Report composer', 'Writes the PDF once you approve the plan.'],
    ['EFFECT_VERIFIER', 'Effect verifier', 'Re-reads and re-hashes the stored PDF.'],
    ['PRINT_PREPARER', 'Print preparer', 'Prepares the verified PDF for printing.']
  ];
  const CHECK_LABELS = {
    pdf_readable: 'PDF parses and has pages',
    contains_title: 'Report title is present',
    references_correct_source_sha: 'Cites the bound brief hash',
    no_other_source_sha: 'Cites no other brief',
    references_correct_project_sha: 'Cites the bound project hash',
    no_other_project_sha: 'Cites no other project',
    contains_every_issue: 'Every issue and its file:line is in the PDF',
    contains_architecture: 'Architecture is included',
    contains_plan_digest: 'Approved plan digest is included',
    contains_runtime_evidence: 'Runtime evidence (browser frames) is cited',
    no_other_runtime_evidence: 'Cites no other browser frames'
  };
  const DIAG_NODE = ['BROWSER_DIAGNOSTICS', 'Browser diagnostics', 'Operates the web app in a real browser and records console errors and failed requests.'];
  const ACTIVE = ['RUNNING', 'AWAITING_APPROVAL', 'COMPOSING'];
  const say = (t, kind) => setMsg(el('fmStatus'), t, kind);
  const sevClass = (s) => s === 'HIGH' ? 'chip-bad' : s === 'MEDIUM' ? 'chip-warn' : 'chip-muted';
  const ms = (seconds) => Math.max(0, Math.round(seconds * 1000));
  const clock = (ts) => {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) + '.' + String(d.getMilliseconds()).padStart(3, '0');
  };
  const reveal = (id) => {
    const card = el(id);
    if (!card || card.hidden) return;
    card.scrollIntoView({ behavior: reduceMotion.matches ? 'auto' : 'smooth', block: 'start' });
    const h = card.querySelector('h2[tabindex]');
    if (h) h.focus({ preventScroll: true });
  };

  /* ---------------- server mode, account menu ---------------- */
  async function applyMode() {
    let h = {};
    try { h = await (await fetch('/api/health')).json(); } catch (_) { /* rendered as unknown below */ }
    production = h.mode === 'production';
    const f = h.features || {};
    diagnosticsOn = !!f.diagnostics; updateButtons();
    const browserSpeech = 'webkitSpeechRecognition' in window || 'SpeechRecognition' in window;
    const rows = [
      [production ? 'ok' : 'warn', production ? 'Production mode · private accounts' : 'Local demo mode · 127.0.0.1'],
      f.speech_server ? ['ok', 'Speech: server speech-to-text'] : browserSpeech ? ['warn', 'Speech: browser service (vendor)'] : ['muted', 'Speech: type, or dictate with Wispr Flow'],
      [f.cloud_browser ? 'ok' : 'muted', 'Cloud browser: ' + (f.cloud_browser ? 'enabled' : 'disabled (no isolated runtime)')],
      [f.diagnostics ? 'ok' : 'muted', 'Web diagnostics: ' + (f.diagnostics ? 'real Chromium vs demo app' : 'off on this server')]
    ];
    el('sidebarNote').innerHTML = '<p class="side-h">This server</p><ul class="side-status">' +
      rows.map(([k, t]) => `<li><span class="dot ${k}" aria-hidden="true"></span>${esc(t)}</li>`).join('') + '</ul>' +
      (production ? '' : '<p>Outbox is <b>SIMULATED DELIVERY</b>. Browser actions are DOM-level, not physical cursor movement.</p>');
    if (production) {
      // The shared-ledger demo and its controls do not exist in production, and the connectors are simulated: offer neither.
      ['estop', 'resume', 'statusStrip'].forEach(id => { const n = el(id); if (n) n.hidden = true; });
      ['mission', 'agents', 'skills', 'approvals', 'bench', 'sim', 'windows', 'evidence', 'ledger', 'connectors', 'files'].concat(f.cloud_browser ? [] : ['cloud'])
        .forEach(v => { const a = document.querySelector(`.sidebar a[data-view="${v}"]`); if (a) a.closest('li').remove(); });
      if (!document.querySelector(`.sidebar a[data-view="${location.hash.slice(1)}"]`)) showView('flagship', false);
      const foot = el('appFooter');
      if (foot) foot.textContent = 'Your documents, missions and projects are private to your account. Export or delete them at any time under Privacy & data. RESOLVE never sends external messages.';
    }
    return production;
  }

  async function initAccount() {
    const pill = el('userAccountPill'), menu = el('accountMenu');
    let me = null;
    try { me = await api('/api/auth/me'); } catch (_) { /* the auth gate or an error message covers this */ }
    el('userEmailBadge').textContent = me ? (production ? me.email : 'Local demo operator') : 'Signed out';
    el('userAvatar').textContent = me && me.email ? me.email[0].toUpperCase() : '?';
    el('accountWho').textContent = me ? `${production ? me.email : 'Local demo operator'} · workspace “${me.workspace_name}”` : '';
    el('signOutBtn').hidden = !production;
    try {
      const ws = await api('/api/workspaces?tenant=1'), sel = el('workspaceSwitcher');
      sel.replaceChildren(...ws.workspaces.map(w => new Option(w.name, w.id, false, w.id === ws.active_workspace_id)));
      sel.hidden = false;
      sel.title = 'Switch workspace. Each workspace has its own documents, projects and missions.';
      sel.onchange = async () => { try { await api('/api/workspaces/switch', { workspace_id: sel.value }); location.reload(); } catch (e) { say(errText(e), 'err'); } };
    } catch (_) { /* switcher stays hidden */ }
    const setMenu = (open) => {
      menu.hidden = !open; pill.setAttribute('aria-expanded', String(open));
      if (open) { const first = menu.querySelector('[role=menuitem]:not([hidden])'); if (first) first.focus(); }
    };
    pill.onclick = () => setMenu(menu.hidden);
    document.addEventListener('click', (e) => { if (!menu.hidden && !e.target.closest('.account')) setMenu(false); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !menu.hidden) { setMenu(false); pill.focus(); } });
    menu.querySelectorAll('a[role=menuitem]').forEach(a => a.addEventListener('click', () => setMenu(false)));
    el('newWorkspaceBtn').onclick = async () => {
      setMenu(false);
      const name = prompt('Name for the new workspace (1-60 characters):');
      if (!name) return;
      try { const w = await api('/api/workspaces/create', { name }); await api('/api/workspaces/switch', { workspace_id: w.id }); location.reload(); }
      catch (e) { say(errText(e), 'err'); }
    };
    el('signOutBtn').onclick = async () => {
      try { await api('/api/auth/logout', {}); } catch (_) { /* the token is dropped locally either way */ }
      try { sessionStorage.removeItem('resolve_token'); } catch (_) { /* nothing stored */ }
      location.href = '/app';
    };
  }

  /* ---------------- inputs ---------------- */
  function updateButtons() {
    const running = mission && ['RUNNING', 'COMPOSING'].includes(mission.state);
    const noDoc = !el('fmDoc').value, start = el('fmStart'), cancel = el('fmCancel');
    start.disabled = busy || running || noDoc;
    start.title = busy ? 'Working…' : running ? 'A mission is already running. Wait for it, or cancel it.' : noDoc ? 'Load the sample brief or choose a PDF first' : 'Start the mission with this brief and project';
    const live = mission && ['RUNNING', 'COMPOSING'].includes(mission.state), pause = el('fmPause'), rn = el('fmReviseNow');
    pause.disabled = busy || !live; pause.textContent = mission && mission.paused ? 'Resume' : 'Pause';
    pause.title = live ? (mission.paused ? 'Continue from the next action boundary' : 'Pause at the next action boundary') : 'No running mission';
    rn.disabled = busy || !(mission && ACTIVE.includes(mission.state));
    rn.title = rn.disabled ? 'No active mission' : 'Replace the instruction (and optionally the brief) now; in-flight work and approvals are superseded';
    const ins = el('fmInspectWeb'), note = el('fmInspectNote');
    ins.disabled = !diagnosticsOn; if (!diagnosticsOn) ins.checked = false;
    note.textContent = diagnosticsOn ? '(real Chromium on the bundled demo web app)' : '(not available on this server)';
    cancel.disabled = !mission || !ACTIVE.includes(mission.state);
    cancel.title = cancel.disabled ? 'No running mission' : 'Cancel the mission. Late worker results are rejected.';
  }

  async function loadDocs(select) {
    const d = await api('/api/documents');
    docs = d.documents || [];
    const sel = el('fmDoc'), keep = select || sel.value;
    sel.replaceChildren();
    if (!docs.length) sel.append(new Option('No PDFs yet', ''));
    docs.forEach(x => sel.append(new Option(`${x.name} · ${x.sha256.slice(0, 8)}… · ${new Date(x.created_at * 1000).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' })}`, x.id)));
    if (keep && docs.some(x => x.id === keep)) sel.value = keep;
    updateButtons();
  }

  const allProjects = () => [...projects.bundled, ...projects.uploaded];
  async function loadProjects(select) {
    projects = await api('/api/projects');
    const sel = el('fmProject'), keep = select || sel.value;
    sel.replaceChildren();
    projects.bundled.forEach(p => sel.append(new Option(`${p.name} · ${p.files_count} files`, p.id)));
    projects.uploaded.forEach(p => sel.append(new Option(`${p.name} · ${p.files_count} files · uploaded`, p.id)));
    if (keep && allProjects().some(p => p.id === keep)) sel.value = keep;
    showProjectInfo();
  }
  function showProjectInfo() {
    const p = allProjects().find(x => x.id === el('fmProject').value), del = el('fmProjectDelete');
    el('fmProjectInfo').textContent = p
      ? `${p.bundled ? 'Bundled sample' : 'Your upload'} · ${p.files_count} source files · SHA-256 ${p.sha256.slice(0, 12)}…. The mission binds to this exact content.`
      : 'No project selected.';
    del.disabled = !p || !!p.bundled;
    del.title = del.disabled ? 'Select one of your uploaded projects to delete it' : 'Delete this uploaded project';
  }

  async function loadHistory() {
    const r = await api('/api/brief-missions'), ul = el('fmHistory');
    ul.replaceChildren();
    if (!r.missions.length) { ul.innerHTML = '<li class="muted">No missions yet.</li>'; return; }
    r.missions.forEach(m => {
      const li = document.createElement('li'), b = document.createElement('button');
      b.type = 'button';
      if (mission && mission.id === m.id) b.setAttribute('aria-current', 'true');
      const when = new Date(m.created_at * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
      b.innerHTML = `<span class="fm-hist-title">${esc(m.intent_text)}</span><span class="chip ${chipClass(m.state)}">${esc(m.state.replace('_', ' '))}</span>` +
        `<small>${esc(when)} · ${esc(m.id)}${m.output_sha256 ? ' · PDF ' + esc(m.output_sha256.slice(0, 10)) + '…' : ''}</small>`;
      b.onclick = async () => {
        stopPoll();
        try { mission = await api('/api/brief-missions/' + m.id); } catch (e) { say(errText(e), 'err'); return; }
        renderedDigest = null; lastState = mission.state; watching = false;
        render();
        if (['RUNNING', 'COMPOSING'].includes(mission.state)) { watching = true; poll(); }
        loadHistory().catch(() => {});
        reveal(mission.state === 'AWAITING_APPROVAL' ? 'fmPlanCard' : 'fmResultCard');
      };
      li.append(b); ul.append(li);
    });
  }

  async function blobOf(path) {
    const r = await fetch(path, { headers: await authHeaders() });
    if (!r.ok) throw Error('HTTP ' + r.status);
    return r.blob();
  }

  /* ---------------- rendering ---------------- */
  function renderSteps(m) {
    const st = m ? m.state : null, approved = m && (m.events || []).some(e => e.kind === 'PLAN_APPROVED');
    let current = 1, stopped = '';
    if (st === 'RUNNING') current = 2;
    else if (st === 'AWAITING_APPROVAL') current = 3;
    else if (st === 'COMPOSING') current = 4;
    else if (st === 'CONFIRMED') current = 5;
    else if (st === 'FAILED' || st === 'CANCELLED') { current = approved ? 4 : (m.plan ? 3 : 2); stopped = st === 'FAILED' ? 'failed' : 'cancelled'; }
    document.querySelectorAll('#fmSteps li').forEach(li => {
      const n = Number(li.dataset.step);
      li.className = n < current ? 'done' : n === current ? (stopped || 'current') : '';
      if (n === current && !stopped) li.setAttribute('aria-current', 'step'); else li.removeAttribute('aria-current');
    });
  }

  function renderNodes(m) {
    const by = Object.fromEntries(((m && m.nodes) || []).map(n => [n.node, n]));
    const ol = el('fmNodes');
    ol.replaceChildren();
    const withDiag = m && (m.steps || []).includes('inspect_web');
    const rows = withDiag ? [NODES[0], NODES[1], DIAG_NODE, ...NODES.slice(2)] : NODES;
    rows.forEach(([key, title, desc]) => {
      const n = by[key], status = n ? n.status : (m ? 'PENDING' : 'NOT STARTED');
      const cls = status === 'DONE' ? 'chip-ok' : (status === 'FAILED' || status === 'REJECTED_LATE') ? 'chip-bad' : status === 'RUNNING' ? 'chip-warn' : 'chip-muted';
      const detail = n && n.started_at
        ? `${esc(n.worker || '')} · ${clock(n.started_at)} → ${n.finished_at ? clock(n.finished_at) + ` (${ms(n.finished_at - n.started_at)} ms)` : 'running'}`
        : esc(desc);
      const li = document.createElement('li');
      li.className = 'node-' + status.toLowerCase().replace(/[^a-z]+/g, '-');
      li.innerHTML = `<span class="fm-node-name">${esc(title)}</span><span class="chip ${cls}">${esc(status)}</span><small>${detail}</small>` +
        (n && n.error ? `<small class="msg err">${esc(n.error)}</small>` : '');
      ol.append(li);
    });
    const par = [by.DOCUMENT_READER, by.REPOSITORY_ANALYZER, withDiag ? by.BROWSER_DIAGNOSTICS : null].filter(Boolean), ov = el('fmOverlap');
    if (par.length >= 2 && par.every(n => n.finished_at)) {
      // sweep line over the recorded start/finish times: the most workers that were running at the same instant
      const pts = par.flatMap(n => [[n.started_at, 1], [n.finished_at, -1]]).sort((x, y) => x[0] - y[0] || x[1] - y[1]);
      let cur = 0, peak = 0; pts.forEach(([, d]) => { cur += d; peak = Math.max(peak, cur); });
      const work = par.reduce((t, n) => t + (n.finished_at - n.started_at), 0), wall = Math.max(...par.map(n => n.finished_at)) - Math.min(...par.map(n => n.started_at));
      ov.hidden = false;
      ov.className = 'small fm-overlap' + (peak > 1 ? ' ok' : '');
      ov.textContent = peak > 1
        ? `Measured: up to ${peak} of ${par.length} workers (reader, analyzer${par.length === 3 ? ', browser diagnostics' : ''}) ran at the same time; ${ms(work)} ms of work took ${ms(wall)} ms of wall time.`
        : `Measured: the workers did not overlap this time (${ms(work)} ms of work, ${ms(wall)} ms wall time).`;
    } else ov.hidden = true;
    el('fmEvents').textContent = ((m && m.events) || []).map(e => `${clock(e.ts)}  ${e.kind.padEnd(24)} ${e.node}${e.worker ? '  [' + e.worker + ']' : ''}`).join('\n') || '—';
  }

  function wrapLabel(label, width, maxLines = 3) {
    const parts = label.replace(/\//g, '/​').split(/[ ​]/).filter(Boolean), lines = [''];
    parts.forEach(w => {
      const sep = lines[lines.length - 1] && !lines[lines.length - 1].endsWith('/') ? ' ' : '';
      if ((lines[lines.length - 1] + sep + w).length <= width) lines[lines.length - 1] += sep + w;
      else lines.push(w.length > width ? w.slice(0, width - 1) + '…' : w);
    });
    if (lines.length > maxLines) { lines.length = maxLines; lines[maxLines - 1] = lines[maxLines - 1].slice(0, width - 1) + '…'; }
    return lines;
  }

  function drawDiagram(g) {
    const ids = g.nodes.map(n => n.id), depth = Object.fromEntries(ids.map(i => [i, 0]));
    for (let k = 0; k < ids.length; k++) g.edges.forEach(e => { if (e.to in depth && e.from in depth && depth[e.to] <= depth[e.from]) depth[e.to] = depth[e.from] + 1; });
    const layers = {};
    g.nodes.forEach(n => (layers[depth[n.id]] = layers[depth[n.id]] || []).push(n));
    // A layer is never split across rows unless it has more than 6 nodes: an edge from an upper row would run
    // straight into a box on the lower row and read as a link that does not exist. Boxes shrink to fit instead
    // (down to a readable minimum) and the diagram scrolls horizontally beyond that.
    const rows = [], pad = 14, gx = 18, gy = 44;
    const avail = Math.max(260, (el('fmDiagram').clientWidth || 520) - 22);
    const perRow = Math.min(6, Math.max(1, ...Object.values(layers).map(l => l.length)));
    const bw = Math.max(96, Math.min(156, Math.floor((avail - 2 * pad - (perRow - 1) * gx) / perRow)));
    const chars = Math.max(10, Math.floor((bw - 14) / 6.8)), font = bw < 130 ? 12 : 13;
    const labels = Object.fromEntries(g.nodes.map(n => [n.id, wrapLabel(n.label, chars)]));
    const bh = 22 + 15 * Math.max(1, ...Object.values(labels).map(l => l.length));
    Object.keys(layers).sort((a, b) => a - b).forEach(k => { for (let i = 0; i < layers[k].length; i += perRow) rows.push(layers[k].slice(i, i + perRow)); });
    const cols = Math.max(1, ...rows.map(r => r.length));
    const W = pad * 2 + cols * bw + (cols - 1) * gx, H = pad * 2 + rows.length * bh + (rows.length - 1) * gy;
    const pos = {};
    rows.forEach((r, ri) => { const off = (cols - r.length) * (bw + gx) / 2; r.forEach((n, ci) => { pos[n.id] = [pad + off + ci * (bw + gx), pad + ri * (bh + gy)]; }); });
    const ns = 'http://www.w3.org/2000/svg', mk = (tag, attrs, text) => {
      const e = document.createElementNS(ns, tag);
      Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v));
      if (text !== undefined) e.textContent = text;
      return e;
    };
    const svg = mk('svg', { viewBox: `0 0 ${W} ${H}`, width: W > avail ? W : '100%', role: 'img', 'aria-label': `Architecture: ${g.nodes.length} components, ${g.edges.length} links` });
    svg.style.maxHeight = Math.max(240, H) + 'px';
    const defs = mk('defs', {}), marker = mk('marker', { id: 'fmArrow', viewBox: '0 0 10 10', refX: '9', refY: '5', markerWidth: '7', markerHeight: '7', orient: 'auto-start-reverse' });
    marker.append(mk('path', { d: 'M0,0 L10,5 L0,10 z', fill: '#8A9C89' }));
    defs.append(marker); svg.append(defs);
    g.edges.forEach(e => {
      if (!pos[e.from] || !pos[e.to]) return;
      const [x1, y1] = pos[e.from], [x2, y2] = pos[e.to];
      const sx = x1 + bw / 2, sy = y1 + bh, tx = x2 + bw / 2, ty = y2, bend = Math.max(20, (ty - sy) / 2);
      const d = ty > sy ? `M${sx},${sy} C${sx},${sy + bend} ${tx},${ty - bend} ${tx},${ty}` : `M${x1 + bw},${y1 + bh / 2} L${x2},${y2 + bh / 2}`;
      svg.append(mk('path', { d, fill: 'none', stroke: '#8A9C89', 'stroke-width': 1.5, 'stroke-dasharray': e.dashed ? '5 4' : '0', 'marker-end': 'url(#fmArrow)', opacity: e.dashed ? 0.8 : 1 }));
    });
    g.nodes.forEach(n => {
      const [x, y] = pos[n.id], proposed = n.kind === 'proposed', grp = mk('g', {});
      grp.append(mk('title', {}, n.label + (proposed ? ' (proposed)' : '')));
      grp.append(mk('rect', { x, y, width: bw, height: bh, rx: 10, fill: proposed ? '#FFF8EA' : '#FFFDF9', stroke: proposed ? '#C8A977' : '#8A9C89', 'stroke-width': 1.8 }));
      const lines = labels[n.id], top = y + bh / 2 - (lines.length - 1) * 7.5 + 4;
      lines.forEach((t, i) => grp.append(mk('text', { x: x + bw / 2, y: top + i * 15, 'text-anchor': 'middle', 'font-size': font, fill: '#272722' }, t)));
      svg.append(grp);
    });
    el('fmDiagram').replaceChildren(svg);
    el('fmDiagramHint').hidden = W <= avail;
  }

  let drawnGraph = null, drawnWidth = 0;
  if (window.ResizeObserver) new ResizeObserver(() => {
    const w = el('fmDiagram').clientWidth;
    if (drawnGraph && Math.abs(w - drawnWidth) > 40) { drawnWidth = w; drawDiagram(drawnGraph); }
  }).observe(el('fmDiagram'));

  function renderPlan(m) {
    const plan = m && m.plan, show = !!(plan && m.state === 'AWAITING_APPROVAL');
    el('fmPlanCard').hidden = !show;
    if (!show) return;
    el('fmPlanMeta').innerHTML = [
      `Plan digest <code>${esc(plan.digest.slice(0, 16))}…</code>`,
      `intent revision ${esc(plan.intent.revision)}`,
      `brief ${esc(plan.source.name)} <code>${esc(plan.source.sha256.slice(0, 10))}…</code>`,
      `project ${esc(plan.project.name)} <code>${esc(plan.project.sha256.slice(0, 10))}…</code> (${esc(plan.project.files_scanned)} files)`
    ].join(' · ') + (plan.edited_by_user ? ' · <b>edited by you</b>' : '');
    const count = (s) => plan.issues.filter(i => i.severity === s).length;
    el('fmSeverity').innerHTML = ['HIGH', 'MEDIUM', 'LOW'].map(s => `<span class="chip ${sevClass(s)}">${count(s)} ${s}</span>`).join('') +
      `<span class="small muted">${plan.issues.length} issues against ${plan.requirements.length} brief requirements` +
      (plan.project.findings_omitted ? `; ${plan.project.findings_omitted} lower-severity findings not listed` : '') + '</span>';
    el('fmIssues').innerHTML = plan.issues.map(i => `<tr>
      <td data-label="ID"><code>${esc(i.id)}</code></td>
      <td data-label="Severity"><span class="chip ${sevClass(i.severity)}">${esc(i.severity)}</span></td>
      <td data-label="Where"><code>${esc(i.file)}:${esc(i.line)}</code><small class="fm-rule">${esc(i.rule)}</small>
        <details class="fm-ev"><summary>Evidence &amp; fix</summary><code class="fm-snippet">${esc(i.snippet)}</code><p><b>Fix:</b> ${esc(i.fix)}</p>${i.requirement ? `<p class="muted">Brief ${esc(i.requirement.id)}: ${esc(i.requirement.text)}</p>` : ''}</details></td>
      <td data-label="Violates">${i.requirement ? `<b>${esc(i.requirement.id)}</b><small> brief line ${esc(i.requirement.brief_line)}</small>` : '<small class="muted">no matching requirement</small>'}</td></tr>`).join('');
    drawnGraph = plan.architecture; drawnWidth = el('fmDiagram').clientWidth;
    drawDiagram(plan.architecture);
    if (renderedDigest !== plan.digest) {
      el('fmMermaid').value = plan.mermaid;
      renderedDigest = plan.digest;
      if (!plan.edited_by_user) { el('fmEditMsg').textContent = ''; el('fmEditMsg').className = 'msg'; }
    }
  }

  async function loadPreview(m) {
    if (previewUrl && previewUrl.id === m.output_doc_id) return;
    const b = await blobOf('/api/documents/' + m.output_doc_id + '/download');
    if (previewUrl) URL.revokeObjectURL(previewUrl.url);
    previewUrl = { id: m.output_doc_id, url: URL.createObjectURL(b) };
    el('fmPreview').hidden = !canInlinePdf;
    el('fmPreviewFallback').hidden = canInlinePdf;
    if (canInlinePdf) el('fmPreview').src = previewUrl.url;   // never point a hidden iframe at a PDF: without a viewer that downloads it
  }

  function renderResult(m) {
    const card = el('fmResultCard'), show = !!m && (m.state === 'CONFIRMED' || m.state === 'FAILED');
    card.hidden = !show;
    if (!show) return;
    const chip = el('fmResultChip'), out = ['fmDownload', 'fmPrint', 'fmCopyHash'];
    if (m.state === 'FAILED') {
      chip.textContent = 'FAILED'; chip.className = 'chip chip-bad';
      el('fmChecks').innerHTML = `<li class="fm-check fail"><span class="msg err">${esc(m.error || 'Mission failed')}</span></li>`;
      el('fmChecksNote').textContent = 'Nothing was confirmed and no unverified PDF was kept.';
      el('fmHash').textContent = '—'; el('fmProv').innerHTML = '';
      out.forEach(id => { el(id).disabled = true; el(id).title = 'No verified PDF for this mission'; });
      el('fmPreview').hidden = true; el('fmPreviewFallback').hidden = true;
      return;
    }
    const v = m.receipt.verification, broken = m.chain_broken && m.chain_broken.length;
    chip.textContent = broken ? 'CHAIN BROKEN' : 'CONFIRMED · chain verified';
    chip.className = 'chip ' + (broken ? 'chip-bad' : 'chip-ok');
    el('fmChecks').innerHTML = Object.entries(v.checks).map(([k, ok]) =>
      `<li class="fm-check ${ok ? 'pass' : 'fail'}"><span class="chip ${ok ? 'chip-ok' : 'chip-bad'}">${ok ? 'PASS' : 'FAIL'}</span><span>${esc(CHECK_LABELS[k] || k.replace(/_/g, ' '))}</span></li>`).join('');
    el('fmChecksNote').textContent = `The verifier re-read ${v.pages} pages from storage and re-hashed them, independently of the code that wrote the PDF.`;
    el('fmHash').textContent = m.output_sha256;
    const prints = (m.events || []).filter(e => e.kind === 'PRINT_DIALOG_OPENED').length;
    el('fmProv').innerHTML = [
      ['Brief', `${esc(m.plan.source.name)} <code>${esc(m.source_sha256.slice(0, 12))}…</code>`],
      ['Project', `${esc(m.plan.project.name)} <code>${esc(m.project_sha256.slice(0, 12))}…</code>`],
      ['Intent revision', esc(m.receipt.intent_revision)],
      ['Approved digest', `<code>${esc(m.receipt.approved_plan_digest.slice(0, 16))}…</code>`],
      ['Printing', prints ? `print dialog opened ${prints}× (physical printing not verifiable)` : 'not printed yet']
    ].map(([k, v2]) => `<dt>${k}</dt><dd>${v2}</dd>`).join('');
    out.forEach(id => { el(id).disabled = false; el(id).title = ''; });
    loadPreview(m).catch(e => say('Preview failed: ' + errText(e), 'err'));
  }

  async function showFrame(ev) {
    if (!ev || ev.id === frameId) return;
    frameId = ev.id;
    try {
      const blob = await blobOf(`/api/brief-missions/${mission.id}/evidence/${ev.id}`), url = URL.createObjectURL(blob);
      if (frameUrl) URL.revokeObjectURL(frameUrl);
      frameUrl = url; el('fmFrame').src = url; el('fmFrame').hidden = false; el('fmFrameEmpty').hidden = true;
    } catch (_) { frameId = null; }
  }

  function renderComputer(m) {
    const diag = m && (m.steps || []).includes('inspect_web'), chip = el('fmComputerChip');
    const frames = ((m && m.evidence) || []).filter(e => e.kind === 'frame' && e.rev === m.intent_revision);
    const steps = ((m && m.events) || []).filter(e => e.kind === 'TOOL_STEP' && e.rev === m.intent_revision);
    const node = ((m && m.nodes) || []).find(n => n.node === 'BROWSER_DIAGNOSTICS');
    let label = 'DISCONNECTED', cls = 'chip-muted';
    if (diag && node) {
      if (node.status === 'RUNNING') [label, cls] = [m.paused ? 'PAUSED' : 'LIVE', 'chip-warn'];
      else if (node.status === 'DONE') [label, cls] = ['SESSION CLOSED', 'chip-ok'];
      else if (node.status === 'FAILED' || node.status === 'REJECTED_LATE') [label, cls] = ['FAILED', 'chip-bad'];
      else [label, cls] = ['CONNECTING', 'chip-warn'];
    } else if (diag) [label, cls] = ['CONNECTING', 'chip-warn'];
    chip.textContent = label; chip.className = 'chip ' + cls;
    if (!frames.length) {
      frameId = null; el('fmFrame').hidden = true; el('fmFrameEmpty').hidden = false;
      if (frameUrl) { URL.revokeObjectURL(frameUrl); frameUrl = null; }
    } else showFrame(frames[frames.length - 1]);
    const last = steps[steps.length - 1], d = last ? last.detail : {};
    el('fmComputerFacts').innerHTML = diag ? [
      ['Page', d.url ? esc(d.url) : '—'], ['Title', d.title ? esc(d.title) : '—'], ['Last action', last ? esc(d.action || '') + (d.ok === false ? ' (failed)' : '') : '—'],
      ['Frames stored', String(frames.length)], ['Browser', 'Chromium via Playwright (DOM-level)']
    ].map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('') : '<dt>Status</dt><dd>No browser session for this mission</dd>';
    const ol = el('fmComputerSteps'); ol.replaceChildren();
    steps.forEach(e => {
      const li = document.createElement('li'); if (e.detail.ok === false) li.className = 'bad';
      li.innerHTML = `<span class="muted small">${esc(clock(e.ts))}</span><code>${esc(e.detail.action || '')}</code><span class="small">${esc(e.detail.detail || '')}</span>`;
      ol.append(li);
    });
  }

  function render() {
    const m = mission;
    el('fmState').textContent = m ? (m.paused ? 'PAUSED' : m.state.replace('_', ' ')) : 'NO MISSION';
    el('fmState').className = 'chip ' + (m ? (m.paused ? 'chip-warn' : chipClass(m.state)) : 'chip-muted');
    renderSteps(m); renderNodes(m); renderComputer(m); renderPlan(m); renderResult(m); updateButtons();
  }

  function announce(prev, m) {
    if (!watching || prev === m.state) return;
    if (m.state === 'AWAITING_APPROVAL') { say('Plan ready. Review the issues and the architecture, then approve.', 'ok'); requestAnimationFrame(() => reveal('fmPlanCard')); }
    else if (m.state === 'CONFIRMED') { say('Confirmed: the PDF passed every independent check.', 'ok'); requestAnimationFrame(() => reveal('fmResultCard')); }
    else if (m.state === 'FAILED') { say('Mission failed: ' + (m.error || 'see the worker list.'), 'err'); requestAnimationFrame(() => reveal('fmResultCard')); }
    else if (m.state === 'CANCELLED') say('Mission cancelled. Late worker results are rejected.', 'ok');
  }

  async function refresh() {
    if (!mission) return;
    try { mission = await api('/api/brief-missions/' + mission.id); } catch (e) { say(errText(e), 'err'); stopPoll(); return; }
    const prev = lastState; lastState = mission.state;
    render(); announce(prev, mission);
    if (!['RUNNING', 'COMPOSING'].includes(mission.state)) { stopPoll(); loadHistory().catch(() => {}); loadSkills().catch(() => {}); }
  }
  const stopPoll = () => { if (timer) { clearInterval(timer); timer = null; } };
  const poll = () => { stopPoll(); timer = setInterval(refresh, 600); };
  const act = async (fn) => {   // one request at a time; buttons reflect it
    if (busy) return;
    busy = true; updateButtons();
    try { await fn(); } finally { busy = false; updateButtons(); }
  };

  /* ---------------- actions ---------------- */
  el('fmSample').onclick = () => act(async () => {
    try { const d = await api('/api/documents/sample-brief', {}); await loadDocs(d.document.id); say('Sample brief created as a PDF in your private workspace.', 'ok'); }
    catch (e) { say(errText(e), 'err'); }
  });
  el('fmStart').onclick = () => act(async () => {
    const id = el('fmDoc').value, d = docs.find(x => x.id === id);
    if (!d) { say('Choose a source brief first, or load the sample brief.', 'err'); return; }
    try {
      const steps = ['read_brief', 'analyze_code', ...(el('fmInspectWeb').checked ? ['inspect_web'] : []), 'architecture', 'report', 'print'];
      mission = await api('/api/brief-missions', { intent_text: el('fmIntent').value, document_id: id, expected_sha256: d.sha256, project: el('fmProject').value, steps, source: intentVoice ? 'voice' : 'text' });
      say('Mission started. The workers are running.', 'ok');
      renderedDigest = null; lastState = mission.state; watching = true;
      render(); poll(); loadHistory().catch(() => {});
    } catch (e) { say(errText(e), 'err'); }
  });
  el('fmApprove').onclick = () => act(async () => {
    try {
      mission = await api(`/api/brief-missions/${mission.id}/approve`, { plan_digest: mission.plan.digest });
      lastState = mission.state; watching = true;
      say('Approved exactly this plan. Writing and verifying the PDF…', 'ok'); render(); poll();
    } catch (e) { say(errText(e), 'err'); await refresh(); }
  });
  el('fmRevise').onclick = () => act(async () => {
    try {
      mission = await api(`/api/brief-missions/${mission.id}/revise`, { intent_text: el('fmIntent').value, document_id: el('fmDoc').value || undefined });
      lastState = mission.state; watching = true; renderedDigest = null;
      say('Instruction revised. The earlier plan and any approval of it are superseded.', 'ok'); render(); poll();
      el('fmIntent').scrollIntoView({ behavior: reduceMotion.matches ? 'auto' : 'smooth', block: 'center' });
    } catch (e) { say(errText(e), 'err'); }
  });
  el('fmCancel').onclick = () => act(async () => {
    try { mission = await api(`/api/brief-missions/${mission.id}/cancel`, {}); lastState = mission.state; stopPoll(); say('Mission cancelled. Late worker results are rejected.', 'ok'); render(); loadHistory().catch(() => {}); }
    catch (e) { say(errText(e), 'err'); }
  });
  el('fmDownload').onclick = () => {
    if (!previewUrl) return;
    const a = document.createElement('a'); a.href = previewUrl.url; a.download = 'resolve-report.pdf'; document.body.append(a); a.click(); a.remove();
  };
  el('fmOpenPdf').onclick = () => { if (previewUrl) window.open(previewUrl.url, '_blank', 'noopener'); };
  el('fmPrint').onclick = async () => {
    if (!canInlinePdf) {   // no inline viewer: printing happens from the PDF tab, which RESOLVE cannot observe, so nothing is recorded
      if (previewUrl) window.open(previewUrl.url, '_blank', 'noopener');
      say('The PDF opened in a new tab. Print it from there with your browser’s print or share menu.', 'ok');
      return;
    }
    try {
      el('fmPreview').contentWindow.print();
      mission = await api(`/api/brief-missions/${mission.id}/print-event`, {});
      render();
      say('Print dialog opened. That is recorded as a dialog event only: physical printing cannot be verified.', 'ok');
    } catch (e) { say('Use your browser’s Print command on the preview. ' + errText(e), 'err'); }
  };
  el('fmCopyHash').onclick = async () => {
    const btn = el('fmCopyHash'), text = el('fmHash').textContent;
    try { await navigator.clipboard.writeText(text); btn.textContent = 'Copied'; }
    catch (_) { const r = document.createRange(); r.selectNodeContents(el('fmHash')); const s = getSelection(); s.removeAllRanges(); s.addRange(r); btn.textContent = 'Press Ctrl+C'; }
    setTimeout(() => { btn.textContent = 'Copy'; }, 1800);
  };
  el('fmDelete').onclick = () => act(async () => {
    if (!confirm('Delete this mission and its generated PDF? This cannot be undone.')) return;
    try {
      await api(`/api/brief-missions/${mission.id}/delete`, {});
      mission = null; lastState = null; watching = false;
      if (previewUrl) { URL.revokeObjectURL(previewUrl.url); previewUrl = null; }
      el('fmPreview').removeAttribute('src');
      say('Mission and PDF deleted.', 'ok'); render();
      await loadDocs(); await loadHistory();
      view.scrollIntoView({ behavior: reduceMotion.matches ? 'auto' : 'smooth', block: 'start' });
    } catch (e) { say(errText(e), 'err'); }
  });
  el('fmEditToggle').onclick = () => {
    const ed = el('fmEditor'), open = ed.hidden;
    ed.hidden = !open;
    el('fmEditToggle').setAttribute('aria-expanded', String(open));
    el('fmEditToggle').textContent = open ? 'Close editor' : 'Edit';
    if (open) el('fmMermaid').focus();
  };
  el('fmApplyEdit').onclick = () => act(async () => {
    if (!mission) return;
    try {
      mission = await api(`/api/brief-missions/${mission.id}/edit-architecture`, { mermaid: el('fmMermaid').value });
      renderedDigest = null; render();
      setMsg(el('fmEditMsg'), 'Applied. The plan digest changed, so approve the edited plan.', 'ok');
    } catch (e) { setMsg(el('fmEditMsg'), errText(e), 'err'); }
  });
  el('fmDoc').onchange = updateButtons;
  el('fmProject').onchange = showProjectInfo;
  el('fmHistRefresh').onclick = () => loadHistory().catch(e => say(errText(e), 'err'));
  el('fmProjectFile').onchange = (ev) => act(async () => {
    const f = ev.target.files[0];
    ev.target.value = '';
    if (!f) return;
    if (f.size > 5 * 1024 * 1024) { say('Project archives must be 5 MB or smaller.', 'err'); return; }
    say('Uploading ' + f.name + '…');
    try {
      const u8 = new Uint8Array(await f.arrayBuffer());
      let bin = '';
      for (let i = 0; i < u8.length; i += 0x8000) bin += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
      const r = await api('/api/projects/upload', { name: f.name, content_base64: btoa(bin) });
      await loadProjects(r.project.id);
      say(`Uploaded ${r.project.name}: ${r.project.files_count} source files ready to analyze.`, 'ok');
    } catch (e) { say('Upload failed: ' + errText(e), 'err'); }
  });
  el('fmProjectDelete').onclick = () => act(async () => {
    const id = el('fmProject').value;
    if (!id || !confirm('Delete this uploaded project?')) return;
    try { await api(`/api/projects/${id}/delete`, {}); await loadProjects(); say('Project deleted.', 'ok'); } catch (e) { say(errText(e), 'err'); }
  });
  el('fmPause').onclick = () => act(async () => {
    try {
      mission = await api(`/api/brief-missions/${mission.id}/${mission.paused ? 'resume' : 'pause'}`, {});
      say(mission.paused ? 'Paused. Work stops at the next action boundary.' : 'Resumed.', 'ok'); render(); poll();
    } catch (e) { say(errText(e), 'err'); await refresh(); }
  });
  el('fmReviseNow').onclick = () => act(async () => {
    try {
      mission = await api(`/api/brief-missions/${mission.id}/revise`, { intent_text: el('fmIntent').value, document_id: el('fmDoc').value || undefined });
      lastState = mission.state; watching = true; renderedDigest = null;
      say('Instruction replaced. In-flight workers and any earlier approval are superseded; the plan is being rebuilt.', 'ok'); render(); poll();
    } catch (e) { say(errText(e), 'err'); await refresh(); }
  });
  el('fmIntent').addEventListener('input', (e) => { intentVoice = !e.isTrusted; });   // dictated text arrives as a synthetic event

  /* ---------------- commands (interpret -> confirm -> execute, bound by digest) ---------------- */
  const cmdBadge = (t, cls) => { el('fmCmdBadge').textContent = t; el('fmCmdBadge').className = 'chip ' + (cls || 'chip-muted'); };
  const cmdSay = (t, kind) => setMsg(el('fmCmdMsg'), t, kind);
  el('fmCmd').addEventListener('input', (e) => { cmdVoice = !e.isTrusted; pendingCmd = null; el('fmCmdOut').hidden = true; });
  async function runCommand(confirmed) {
    const text = el('fmCmd').value.trim(), source = cmdVoice ? 'voice' : 'text';
    if (!text) { cmdSay('Type or speak a command first.', 'err'); return; }
    if (!confirmed) {
      cmdBadge('INTERPRETING', 'chip-warn');
      try {
        const r = (await api('/api/commands/interpret', { text, source })).interpretation;
        pendingCmd = { text, source, interp: r };
        el('fmCmdOut').hidden = false;
        el('fmCmdSummary').textContent = r.summary;
        el('fmCmdMeta').textContent = `${r.command} · ${r.requires_confirmation ? 'needs your confirmation' : 'safe to run'} · digest ${r.digest.slice(0, 12)}…` +
          (r.warnings.length ? ' · ' + r.warnings.join('; ') : '') + (r.ambiguity.length ? ' · ambiguous: choose one first' : '');
        el('fmCmdConfirm').hidden = r.command === 'UNKNOWN' || r.ambiguity.length > 0;
        if (r.command === 'UNKNOWN' || r.ambiguity.length) { cmdBadge(r.command === 'UNKNOWN' ? 'NOT UNDERSTOOD' : 'AMBIGUOUS', 'chip-warn'); cmdSay('', ''); return; }
        if (r.requires_confirmation) { cmdBadge('AWAITING CONFIRMATION', 'chip-warn'); el('fmCmdConfirm').focus(); cmdSay('Nothing has happened yet. Confirm to run it.', ''); return; }
      } catch (e) { cmdBadge('FAILED', 'chip-bad'); cmdSay(errText(e), 'err'); return; }
    }
    if (!pendingCmd) return;
    cmdBadge('EXECUTING', 'chip-warn');
    try {
      const r = await api('/api/commands/execute', { text: pendingCmd.text, source: pendingCmd.source, digest: pendingCmd.interp.digest, confirmed: !!confirmed || undefined,
                                                      document_id: el('fmDoc').value || undefined });
      cmdBadge('DONE', 'chip-ok'); cmdSay(`Done: ${r.interpretation.summary}`, 'ok'); el('fmCmdOut').hidden = true; pendingCmd = null;
      const res = r.result || {};
      if (res.id && res.intent_text) { mission = res; lastState = mission.state; watching = true; render(); poll(); }
      else if (res.mission && res.mission.id) { mission = res.mission; lastState = mission.state; render(); }
      else if (res.deleted) { mission = null; render(); }
      loadHistory().catch(() => {}); loadSkills().catch(() => {}); loadDocs().catch(() => {});
    } catch (e) { cmdBadge('FAILED', 'chip-bad'); cmdSay(errText(e), 'err'); await refresh().catch(() => {}); }
  }
  el('fmCmdGo').onclick = () => runCommand(false);
  el('fmCmd').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); runCommand(false); } });
  el('fmCmdConfirm').onclick = () => runCommand(true);
  el('fmCmdDismiss').onclick = () => { pendingCmd = null; el('fmCmdOut').hidden = true; cmdBadge('IDLE'); cmdSay('Dismissed. Nothing was run.', ''); };

  async function loadSkills() {
    const r = await api('/api/brief-skills'), ul = el('fmSkills');
    ul.replaceChildren();
    if (!r.skills.length) { ul.innerHTML = '<li class="muted">No skills yet. Finish a mission, then say “save this process as a skill”.</li>'; return; }
    r.skills.forEach(k => {
      const li = document.createElement('li'), b = document.createElement('button');
      b.type = 'button';
      b.innerHTML = `<span class="fm-hist-title">${esc(k.name)}</span><span class="chip chip-muted">${k.spec.steps.length} steps</span><small>Runs on the brief selected above · saved from ${esc(k.spec.saved_from)}</small>`;
      b.title = 'Run this saved process on the selected brief (a fresh mission that still needs your approval)';
      b.onclick = () => act(async () => {
        const id = el('fmDoc').value;
        if (!id) { say('Choose a source brief first.', 'err'); return; }
        try { mission = await api(`/api/brief-skills/${k.id}/run`, { document_id: id }); lastState = mission.state; watching = true; renderedDigest = null; render(); poll(); say(`Skill “${k.name}” started on the selected brief.`, 'ok'); }
        catch (e) { say(errText(e), 'err'); }
      });
      li.append(b); ul.append(li);
    });
  }

  const grow = () => { const t = el('fmIntent'); t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight + 2, 260) + 'px'; };
  el('fmIntent').addEventListener('input', grow);

  document.addEventListener('resolve:view', (e) => {   // pick up PDFs and projects added in other views
    if (e.detail === 'flagship') { loadDocs().catch(() => {}); loadProjects().catch(() => {}); }
  });

  /* ---------------- start-up ---------------- */
  window.ResolveVoice.attach({ btn: el('fmMic'), badge: el('fmMicBadge'), note: el('fmMicNote'), target: el('fmIntent') });
  window.ResolveVoice.attach({ btn: el('fmCmdMic'), badge: el('fmCmdMicBadge'), note: el('fmCmdMicNote'), target: el('fmCmd') });
  render(); grow();
  applyMode().then(initAccount).catch(() => {});
  loadProjects().catch(() => {});
  loadDocs().catch(() => { /* production sign-in runs first; data loads after it */ });
  api('/api/brief-missions').then(r => r.missions.length ? api('/api/brief-missions/' + r.missions[0].id) : null).then(m => {
    if (!m) return;
    mission = m; lastState = m.state; render();
    if (['RUNNING', 'COMPOSING'].includes(m.state)) { watching = true; poll(); }
  }).catch(() => {});
  loadHistory().catch(() => {});
  loadSkills().catch(() => {});
});
