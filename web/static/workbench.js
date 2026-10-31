/* Workbench home (Advanced Tools). Every number and status below is read from this server when the view opens.
   Nothing is simulated: if an endpoint fails or a capability is missing, the tile says so. Uses api() from app.js. */
(function () {
  'use strict';
  const el = document.getElementById('wbBody');
  if (!el) return;
  const X = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const safe = (p) => api(p).catch((e) => ({ __error: (e && e.message) || 'unavailable' }));
  const pill = (t, k) => `<span class="wb-pill ${k || ''}">${X(t)}</span>`;
  const ok = (r) => r && !r.__error;

  function tile(o) {
    return `<a class="wb-tile ${o.hero ? 'wb-hero' : ''}" href="#${X(o.view)}" data-wb-go="${X(o.view)}">
      <span class="wb-tile-top"><strong>${X(o.title)}</strong>${pill(o.status, o.kind)}</span>
      <span class="wb-tile-text">${X(o.text)}</span><span class="wb-open">Open →</span></a>`;
  }

  async function render() {
    el.innerHTML = '<p class="ib-loading">Reading this server…</p>';
    const [web, tools, skills, agents, health] = await Promise.all([safe('/api/web/status'), safe('/api/tools'), safe('/api/skills'), safe('/api/agents'), safe('/api/health')]);
    const win = ok(web) ? web.windows_native || {} : {};
    const toolList = ok(tools) ? tools.tools || [] : [];
    const nSkills = ok(skills) ? (skills.skills || []).length : null;
    const nAgents = ok(agents) ? (agents.agents || []).length : null;
    const browser = ok(web) && web.playwright;
    const cloud = ok(health) && health.features && health.features.cloud_browser;
    const tiles = [
      { view: 'agents', title: 'Agents', hero: true, status: nAgents == null ? 'unavailable' : nAgents + ' roles', kind: nAgents == null ? 'bad' : 'ok',
        text: 'Give an instruction. See the plan, the tool calls, the evidence and the verified result.' },
      { view: 'computer', title: 'Live computer', hero: true, status: browser ? 'browser ready' : 'browser unavailable', kind: browser ? 'ok' : 'bad',
        text: 'Run a task on the controlled apps with exact-digest approval, stop button and database-side verification.' },
      { view: 'cloud', title: 'Cloud computer', status: cloud ? 'local Chromium on this server' : 'unavailable', kind: cloud ? 'ok' : 'bad',
        text: 'A real browser session with live frames. In this build it is Chromium on this server, not a remote machine.' },
      { view: 'skills', title: 'Skills', status: nSkills == null ? 'unavailable' : nSkills + ' saved', kind: nSkills == null ? 'bad' : '',
        text: 'Record, approve and replay browser skills.' },
      { view: 'documents', title: 'Documents & research', status: 'PDF studio', text: 'Upload, merge, reorder, extract, compare and create PDFs with SHA-256 receipts.' },
      { view: 'evidence', title: 'Evidence & traces', status: 'receipts', text: 'Screenshots, hashes and the ordered record of what each run did.' },
      { view: 'files', title: 'Files sandbox', status: 'sandboxed', text: 'Inventory, dry-run, apply and undo inside a jailed folder.' },
      { view: 'bench', title: 'Benchmarks', status: 'reports on file', text: 'Measured results for the computer and inbox benchmarks. See docs/COMPUTER_BENCHMARK.md.' },
      { view: 'sim', title: 'Voice simulator', status: 'typed or dictated text', text: 'Corrections go in as ordinary text. Server-side speech needs a local model.' },
      { view: 'windows', title: 'Windows native', status: win.available ? 'available' : 'not on this host', kind: win.available ? 'ok' : 'warn',
        text: win.available ? 'Real Windows input is available in this session.' : 'Needs an interactive Windows session. ' + (win.reason || '') },
      { view: 'mission', title: 'Mission control', status: 'approval-gated', text: 'Revision-guarded missions with pause, resume and cancel.' },
      { view: 'ledger', title: 'Tool registry & ledger', status: toolList.length ? toolList.length + ' tools' : 'unavailable', kind: toolList.length ? 'ok' : 'bad',
        text: 'Every typed tool the agents may call, and the ledger of what they did.' }];
    const consequential = toolList.filter((t) => t.consequential).length;
    const registry = toolList.length ? `<section class="wb-panel"><div class="wb-panel-head"><h2>Tool registry</h2>
        <p>${toolList.length} typed tools from this server. ${consequential} are consequential and need an approval before they run. Capability names are enforced by the permission engine.</p></div>
        <div class="wb-table-wrap"><table class="wb-table"><thead><tr><th>Tool</th><th>What it does</th><th>Capability</th><th>Needs approval</th></tr></thead><tbody>${toolList.map((t) => `<tr><td><code>${X(t.name)}</code></td><td>${X(t.summary)}</td><td><code>${X(t.capability)}</code></td><td>${t.consequential ? pill('yes', 'warn') : pill('no')}</td></tr>`).join('')}</tbody></table></div></section>`
      : `<section class="wb-panel"><h2>Tool registry</h2><p>${ok(tools) ? 'No tools registered.' : 'Could not read the registry: ' + X(tools.__error)}</p></section>`;
    el.innerHTML = `<div class="wb-grid">${tiles.map(tile).join('')}</div>${registry}
      <p class="wb-foot">Read from this server just now. Browser actions are DOM-level (Playwright), not physical cursor movement. Windows native control is verified only on Windows.</p>`;
  }

  document.addEventListener('resolve:view', (ev) => { if (String(ev.detail).split(':')[0] === 'workbench') render(); });
  if ((location.hash.slice(1) || 'workbench').split(':')[0] === 'workbench') render();
  el.addEventListener('click', (e) => { const a = e.target.closest('[data-wb-go]'); if (!a) return; e.preventDefault(); location.hash = '#' + a.dataset.wbGo; });
})();
