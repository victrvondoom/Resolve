/* RESOLVE flagship mission UI + account gate. Every number, status and timestamp shown here comes from the server's
   recorded mission events; nothing is scripted. Depends on $, api(), ensureToken(), authHeaders(), esc() from app.js
   (resolved lazily at load time). */
'use strict';

/* ---------------- account gate (production mode has no shared demo token) ---------------- */
window.ResolveAuth = {
  prompt() {
    return new Promise((resolve) => {
      const dlg = document.getElementById('authDialog'), msg = document.getElementById('authMsg');
      const email = document.getElementById('authEmail'), pw = document.getElementById('authPassword');
      const finish = (session) => { try { sessionStorage.setItem('resolve_token', session.token); } catch (_) { /* optional */ } dlg.close(); resolve(session.token); };
      const call = async (path, em, pass) => {
        msg.textContent = 'Working…';
        const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: em, password: pass }) });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { msg.textContent = d.error || ('HTTP ' + r.status); return; }
        finish(d);
      };
      document.getElementById('authLogin').onclick = () => call('/api/auth/login', email.value, pw.value);
      document.getElementById('authRegister').onclick = () => call('/api/auth/register', email.value, pw.value);
      document.getElementById('authDemo').onclick = () => {
        const rnd = crypto.getRandomValues(new Uint8Array(12)), hex = [...rnd].map(b => b.toString(16).padStart(2, '0')).join('');
        call('/api/auth/register', 'demo-' + hex.slice(0, 10) + '@resolve.demo', hex + 'Aa1');
      };
      dlg.addEventListener('cancel', (e) => e.preventDefault());   // no anonymous access in production
      if (!dlg.open) dlg.showModal();
    });
  }
};

window.addEventListener('load', () => {
  const view = document.getElementById('view-flagship');
  if (!view) return;
  let mission = null, timer = null, previewUrl = null, docs = [], projects = { bundled: [], uploaded: [] }, renderedDigest = null;
  const el = (id) => document.getElementById(id);
  const say = (t, kind) => setMsg(el('fmStatus'), t, kind);
  const fmt = (ts) => ts ? new Date(ts * 1000).toISOString().slice(11, 23) : '—';

  window.ResolveVoice.attach({ btn: el('fmMic'), badge: el('fmMicBadge'), note: el('fmMicNote'), target: el('fmIntent') });

  async function loadDocs(select) {
    const d = await api('/api/documents');
    docs = d.documents || [];
    const sel = el('fmDoc'); sel.replaceChildren();
    if (!docs.length) { sel.append(new Option('No PDFs yet — load the sample brief or upload one in Documents', '')); return; }
    docs.forEach(x => sel.append(new Option(`${x.name} · ${x.sha256.slice(0, 10)}… · ${new Date(x.created_at * 1000).toLocaleString()}`, x.id)));
    if (select) sel.value = select;
  }

  const allProjects = () => [...projects.bundled, ...projects.uploaded];
  async function loadProjects(select) {
    projects = await api('/api/projects');
    const sel = el('fmProject'); const keep = select || sel.value; sel.replaceChildren();
    projects.bundled.forEach(p => sel.append(new Option(`${p.name} · ${p.files_count} files`, p.id)));
    projects.uploaded.forEach(p => sel.append(new Option(`${p.name} · ${p.files_count} files · uploaded`, p.id)));
    if (keep && allProjects().some(p => p.id === keep)) sel.value = keep;
    showProjectInfo();
  }
  function showProjectInfo() {
    const p = allProjects().find(x => x.id === el('fmProject').value);
    el('fmProjectInfo').textContent = p ? `${p.bundled ? 'Bundled sample' : 'Your upload'} · ${p.files_count} source files · SHA-256 ${p.sha256.slice(0, 12)}… (the mission binds to this exact content hash)` : 'No project selected.';
    el('fmProjectDelete').disabled = !p || !!p.bundled;
    el('fmProjectDelete').title = el('fmProjectDelete').disabled ? 'Select one of your uploaded projects to delete it' : 'Delete this uploaded project';
  }
  async function loadHistory() {
    const r = await api('/api/brief-missions'), ul = el('fmHistory'); ul.replaceChildren();
    if (!r.missions.length) { ul.innerHTML = '<li class="muted">No missions yet.</li>'; return; }
    r.missions.forEach(m => {
      const li = document.createElement('li'), b = document.createElement('button');
      b.type = 'button'; if (mission && mission.id === m.id) b.setAttribute('aria-current', 'true');
      b.innerHTML = `<span><b>${esc(m.intent_text.slice(0, 70))}${m.intent_text.length > 70 ? '…' : ''}</b></span><span class="chip ${chipClass(m.state)}">${esc(m.state)}</span><small>${esc(m.id)} · ${new Date(m.created_at * 1000).toLocaleString()}${m.output_sha256 ? ' · ' + esc(m.output_sha256.slice(0, 12)) + '…' : ''}</small>`;
      b.onclick = async () => { stopPoll(); mission = await api('/api/brief-missions/' + m.id); renderedDigest = null; render(); if (['RUNNING', 'COMPOSING'].includes(mission.state)) poll(); loadHistory(); };
      li.append(b); ul.append(li);
    });
  }
  async function applyMode() {
    let h = {}; try { h = await (await fetch('/api/health')).json(); } catch (_) { /* shown as unknown below */ }
    const prod = h.mode === 'production', f = h.features || {};
    const speech = f.speech_server ? 'server speech-to-text' : ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window) ? 'browser speech only (vendor service)' : 'none (type or use Wispr Flow)';
    el('sidebarNote') && (el('sidebarNote').innerHTML = (prod ? '<b>Production mode</b>: private accounts, shared-ledger demos disabled.' : '<b>Local demo mode</b> (127.0.0.1). Outbox is <b>SIMULATED DELIVERY</b>.') +
      ` Speech: ${esc(speech)}. Cloud browser: ${f.cloud_browser ? 'enabled' : 'disabled (no isolated runtime declared)'}. Browser actions are DOM-level, not physical cursor movement.`);
    if (prod) {   // legacy agent controls and the demo status strip belong to the shared-ledger demo, which is off in production
      ['estop', 'resume', 'statusStrip'].forEach(id => { const n = document.getElementById(id); if (n) n.hidden = true; });
    }
    if (prod) {   // views backed by the shared demo ledger do not exist in production: do not offer dead screens
      ['mission', 'agents', 'skills', 'approvals', 'bench', 'sim', 'windows', 'evidence', 'ledger'].forEach(v => { const a = document.querySelector(`.sidebar a[data-view="${v}"]`); if (a) a.closest('li').remove(); });
      if (!document.querySelector(`.sidebar a[data-view="${location.hash.slice(1)}"]`)) showView('flagship', false);
    }
    if (prod && !f.cloud_browser) { const a = document.querySelector('.sidebar a[data-view="cloud"]'); if (a) { a.closest('li').remove(); if (location.hash === '#cloud') showView('flagship', false); } }
  }

  async function blobOf(path) {
    const r = await fetch(path, { headers: await authHeaders() });
    if (!r.ok) throw Error('HTTP ' + r.status);
    return r.blob();
  }

  function drawDiagram(g) {
    const ids = g.nodes.map(n => n.id), depth = Object.fromEntries(ids.map(i => [i, 0]));
    for (let k = 0; k < ids.length; k++) g.edges.forEach(e => { if (depth[e.to] <= depth[e.from]) depth[e.to] = depth[e.from] + 1; });
    const layers = {}; g.nodes.forEach(n => (layers[depth[n.id]] = layers[depth[n.id]] || []).push(n));
    const pos = {}; let row = 0; const bw = 150, bh = 46, gx = 16, gy = 36;
    Object.keys(layers).sort((a, b) => a - b).forEach(k => { for (let i = 0; i < layers[k].length; i += 4) { layers[k].slice(i, i + 4).forEach((n, c) => { pos[n.id] = [10 + c * (bw + gx), 10 + row * (bh + gy)]; }); row++; } });
    const W = 10 + 4 * (bw + gx), H = 20 + row * (bh + gy);
    const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('width', '100%'); svg.setAttribute('role', 'img');
    const mk = (tag, attrs, text) => { const e = document.createElementNS(ns, tag); Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v)); if (text !== undefined) e.textContent = text; return e; };
    g.edges.forEach(e => { if (pos[e.from] && pos[e.to]) { const [x1, y1] = pos[e.from], [x2, y2] = pos[e.to]; svg.append(mk('line', { x1: x1 + bw / 2, y1: y1 + bh, x2: x2 + bw / 2, y2, stroke: 'var(--sage,#8A9C89)', 'stroke-width': 1.4, 'stroke-dasharray': e.dashed ? '4 3' : '0' })); } });
    g.nodes.forEach(n => {
      const [x, y] = pos[n.id];
      svg.append(mk('rect', { x, y, width: bw, height: bh, rx: 8, fill: 'var(--raised,#FFFDF9)', stroke: n.kind === 'proposed' ? 'var(--gold,#C8A977)' : 'var(--sage,#8A9C89)', 'stroke-width': 2 }));
      const words = n.label.length > 22 ? [n.label.slice(0, n.label.lastIndexOf(' ', 22) > 0 ? n.label.lastIndexOf(' ', 22) : 22), n.label.slice(n.label.lastIndexOf(' ', 22) > 0 ? n.label.lastIndexOf(' ', 22) + 1 : 22)] : [n.label];
      words.slice(0, 2).forEach((w, i) => svg.append(mk('text', { x: x + bw / 2, y: y + (words.length > 1 ? 19 + i * 16 : 28), 'text-anchor': 'middle', 'font-size': 15, fill: 'var(--text)' }, w)));
    });
    el('fmDiagram').replaceChildren(svg);
  }

  function render() {
    const m = mission;
    el('fmState').textContent = m ? m.state : 'NO MISSION';
    el('fmState').className = 'chip ' + (m ? chipClass(m.state) : 'chip-muted');
    el('fmCancel').disabled = !m || ['CONFIRMED', 'FAILED', 'CANCELLED'].includes(m.state);
    el('fmCancel').title = el('fmCancel').disabled ? 'No running mission' : 'Cancel the mission; late worker results will be rejected';
    const ol = el('fmNodes'); ol.replaceChildren();
    if (!m) { ol.innerHTML = '<li class="muted">Start a mission to see real worker execution.</li>'; return; }
    const order = ['DOCUMENT_READER', 'REPOSITORY_ANALYZER', 'ARCHITECTURE_PLANNER', 'REPORT_COMPOSER', 'EFFECT_VERIFIER', 'PRINT_PREPARER'];
    const by = Object.fromEntries((m.nodes || []).map(n => [n.node, n]));
    order.forEach(name => {
      const n = by[name] || { node: name, status: 'PENDING' };
      const li = document.createElement('li');
      li.innerHTML = `<span class="fm-node-name">${esc(name.replace('_', ' '))}</span><span class="chip ${chipClass(n.status === 'DONE' ? 'DONE' : n.status === 'FAILED' || n.status === 'REJECTED_LATE' ? 'FAILED' : n.status === 'RUNNING' ? 'DISPATCHING' : 'PENDING_REVIEW')}">${esc(n.status)}</span><small>${n.started_at ? esc(n.worker || '') + ' · ' + fmt(n.started_at) + ' → ' + fmt(n.finished_at) : 'waiting'}</small>${n.error ? `<small class="msg err">${esc(n.error)}</small>` : ''}`;
      ol.append(li);
    });
    el('fmEvents').textContent = (m.events || []).map(e => `${fmt(e.ts)}  ${e.kind.padEnd(22)} ${e.node}${e.worker ? ' [' + e.worker + ']' : ''}`).join('\n') || '—';
    const plan = m.plan;
    el('fmPlanCard').hidden = !(plan && m.state === 'AWAITING_APPROVAL');
    if (plan && m.state === 'AWAITING_APPROVAL') {
      el('fmPlanMeta').textContent = `Plan digest ${plan.digest.slice(0, 16)}… · intent revision ${plan.intent.revision} · brief ${plan.source.name} (${plan.source.sha256.slice(0, 12)}…) · project ${plan.project.name} (${plan.project.sha256.slice(0, 12)}…, ${plan.project.files_scanned} files)` + ` · ${plan.issues.length} issues` + (plan.project.findings_omitted ? ` (+${plan.project.findings_omitted} lower-severity omitted)` : '') + (plan.edited_by_user ? ' · edited by you' : '');
      el('fmIssues').innerHTML = plan.issues.map(i => `<tr><td><code>${esc(i.id)}</code></td><td><span class="chip ${i.severity === 'HIGH' ? 'chip-bad' : i.severity === 'MEDIUM' ? 'chip-warn' : 'chip-muted'}">${esc(i.severity)}</span></td><td><code>${esc(i.file)}:${esc(i.line)}</code><br><small>${esc(i.rule)}</small></td><td>${i.requirement ? esc(i.requirement.id) + ' <small>(brief line ' + esc(i.requirement.brief_line) + ')</small>' : '<small>no matching requirement</small>'}</td></tr>`).join('');
      drawDiagram(plan.architecture);
      if (renderedDigest !== plan.digest) { el('fmMermaid').value = plan.mermaid; renderedDigest = plan.digest; el('fmEditMsg').textContent = plan.edited_by_user ? 'Showing your edited architecture.' : ''; }
    }
    const done = m.state === 'CONFIRMED';
    el('fmResultCard').hidden = !(done || m.state === 'FAILED');
    if (m.state === 'FAILED') { el('fmResultChip').textContent = 'FAILED'; el('fmResultChip').className = 'chip chip-bad'; el('fmChecks').innerHTML = `<li class="msg err">${esc(m.error || 'Mission failed')}</li>`; ['fmDownload', 'fmPrint'].forEach(id => el(id).disabled = true); el('fmHash').textContent = '—'; el('fmProv').textContent = '—'; }
    if (done) {
      const v = m.receipt.verification;
      el('fmResultChip').textContent = m.chain_broken && m.chain_broken.length ? 'CHAIN BROKEN' : 'CONFIRMED · chain verified';
      el('fmResultChip').className = 'chip ' + (m.chain_broken && m.chain_broken.length ? 'chip-bad' : 'chip-ok');
      el('fmChecks').innerHTML = Object.entries(v.checks).map(([k, ok]) => `<li><span class="chip ${ok ? 'chip-ok' : 'chip-bad'}">${ok ? 'PASS' : 'FAIL'}</span> ${esc(k.replace(/_/g, ' '))}</li>`).join('') + `<li class="small muted">Read back ${v.pages} pages from storage and re-hashed them independently of the composer.</li>`;
      el('fmHash').textContent = m.output_sha256;
      el('fmProv').textContent = `generated from brief ${m.source_doc_id} (${m.source_sha256.slice(0, 12)}…) and project ${m.plan.project.name} (${m.project_sha256.slice(0, 12)}…), intent revision ${m.receipt.intent_revision}, approved digest ${m.receipt.approved_plan_digest.slice(0, 12)}…`;
      ['fmDownload', 'fmPrint'].forEach(id => el(id).disabled = false);
      if (!previewUrl || previewUrl.id !== m.output_doc_id) {
        blobOf('/api/documents/' + m.output_doc_id + '/download').then(b => { if (previewUrl) URL.revokeObjectURL(previewUrl.url); previewUrl = { id: m.output_doc_id, url: URL.createObjectURL(b) }; el('fmPreview').src = previewUrl.url; }).catch(e => say('Preview failed: ' + errText(e), 'err'));
      }
    }
  }

  async function refresh() {
    if (!mission) return;
    try { mission = await api('/api/brief-missions/' + mission.id); } catch (e) { say(errText(e), 'err'); stopPoll(); return; }
    render();
    if (!['RUNNING', 'COMPOSING'].includes(mission.state)) { stopPoll(); loadHistory().catch(() => {}); }
  }
  const stopPoll = () => { if (timer) { clearInterval(timer); timer = null; } };
  const poll = () => { stopPoll(); timer = setInterval(refresh, 600); };

  el('fmSample').onclick = async () => {
    try { const d = await api('/api/documents/sample-brief', {}); await loadDocs(d.document.id); say('Sample brief created as a PDF in your private workspace.', 'ok'); }
    catch (e) { say(errText(e), 'err'); }
  };
  el('fmStart').onclick = async () => {
    const id = el('fmDoc').value;
    if (!id) { say('Choose a source brief first (or load the sample brief).', 'err'); return; }
    const d = docs.find(x => x.id === id);
    try {
      mission = await api('/api/brief-missions', { intent_text: el('fmIntent').value, document_id: id, expected_sha256: d.sha256, project: el('fmProject').value });
      say('Mission started. Workers are running.', 'ok'); renderedDigest = null; render(); poll(); loadHistory();
    } catch (e) { say(errText(e), 'err'); }
  };
  el('fmApprove').onclick = async () => {
    try { mission = await api(`/api/brief-missions/${mission.id}/approve`, { plan_digest: mission.plan.digest }); say('Approved exactly this plan digest. Composing and verifying…', 'ok'); render(); poll(); }
    catch (e) { say(errText(e), 'err'); await refresh(); }
  };
  el('fmRevise').onclick = async () => {
    try { mission = await api(`/api/brief-missions/${mission.id}/revise`, { intent_text: el('fmIntent').value }); say('Instruction revised; the earlier plan and approval are superseded.', 'ok'); render(); poll(); }
    catch (e) { say(errText(e), 'err'); }
  };
  el('fmCancel').onclick = async () => {
    try { mission = await api(`/api/brief-missions/${mission.id}/cancel`, {}); stopPoll(); say('Mission cancelled.', 'ok'); render(); } catch (e) { say(errText(e), 'err'); }
  };
  el('fmDownload').onclick = () => {
    if (!previewUrl) return; const a = document.createElement('a'); a.href = previewUrl.url; a.download = 'resolve-report.pdf'; document.body.append(a); a.click(); a.remove();
  };
  el('fmPrint').onclick = async () => {
    try { el('fmPreview').contentWindow.print(); await api(`/api/brief-missions/${mission.id}/print-event`, {}); say('Browser print dialog opened (recorded as a dialog event only). Physical printing is unverified.', 'ok'); }
    catch (e) { say('Use your browser’s Print command on the preview. ' + errText(e), 'err'); }
  };
  el('fmDelete').onclick = async () => {
    if (!confirm('Delete this mission and its generated PDF?')) return;
    try { await api(`/api/brief-missions/${mission.id}/delete`, {}); mission = null; if (previewUrl) { URL.revokeObjectURL(previewUrl.url); previewUrl = null; } el('fmPreview').removeAttribute('src'); say('Mission and PDF deleted.', 'ok'); el('fmPlanCard').hidden = el('fmResultCard').hidden = true; render(); await loadDocs(); await loadHistory(); }
    catch (e) { say(errText(e), 'err'); }
  };

  el('fmProject').onchange = showProjectInfo;
  el('fmHistRefresh').onclick = () => loadHistory().catch(e => say(errText(e), 'err'));
  el('fmApplyEdit').onclick = async () => {
    if (!mission) return;
    try { mission = await api(`/api/brief-missions/${mission.id}/edit-architecture`, { mermaid: el('fmMermaid').value }); renderedDigest = null; render(); el('fmEditMsg').textContent = 'Applied. The plan digest changed — approve the edited plan.'; el('fmEditMsg').className = 'msg ok'; }
    catch (e) { el('fmEditMsg').textContent = errText(e); el('fmEditMsg').className = 'msg err'; }
  };
  el('fmProjectFile').onchange = async (ev) => {
    const f = ev.target.files[0]; ev.target.value = ''; if (!f) return;
    if (f.size > 5 * 1024 * 1024) { say('Project archive must be 5 MB or smaller.', 'err'); return; }
    try {
      const u8 = new Uint8Array(await f.arrayBuffer()); let bin = ''; for (let i = 0; i < u8.length; i += 0x8000) bin += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
      const r = await api('/api/projects/upload', { name: f.name, content_base64: btoa(bin) });
      await loadProjects(r.project.id); say(`Uploaded ${r.project.name}: ${r.project.files_count} source files analyzed-ready.`, 'ok');
    } catch (e) { say('Upload failed: ' + errText(e), 'err'); }
  };
  el('fmProjectDelete').onclick = async () => {
    const id = el('fmProject').value; if (!id || !confirm('Delete this uploaded project?')) return;
    try { await api(`/api/projects/${id}/delete`, {}); await loadProjects(); say('Project deleted.', 'ok'); } catch (e) { say(errText(e), 'err'); }
  };
  applyMode().catch(() => {});
  loadProjects().catch(() => {});
  loadHistory().catch(() => {});
  loadDocs().catch(() => { /* auth dialog handles production sign-in; retried on next action */ });
  api('/api/brief-missions').then(r => { if (r.missions.length) { mission = r.missions[0]; return api('/api/brief-missions/' + mission.id); } }).then(m => { if (m) { mission = m; render(); if (['RUNNING', 'COMPOSING'].includes(m.state)) poll(); } }).catch(() => {});
});
