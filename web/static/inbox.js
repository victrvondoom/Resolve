/* RESOLVE Inbox-to-Commitment Operator screens. Every value shown comes from /api/inbox/*; every action calls a route.
   Email text is untrusted: it is always escaped before it reaches the page. Uses esc() and authHeaders() from app.js. */
'use strict';
(function () {
  const IB = { mode: 'LIVE', tz: 'UTC', tzConfigured: false, filter: 'all', sort: 'date', q: '', page: 1, since: 0, cfilter: 'open',
               threadId: null, commitmentId: null, draftId: null, approval: null, replayTimer: null, lastBrief: null };
  try { IB.mode = sessionStorage.getItem('resolve_inbox_mode') === 'REPLAY' ? 'REPLAY' : 'LIVE'; } catch (_) { /* storage blocked */ }

  const KIND_LABEL = { REQUEST_TO_USER: 'Request to you', PROMISE_BY_USER: 'Your promise', PROMISE_BY_OTHER: 'Promised to you',
                       DEPENDENCY: 'Dependency', FOLLOW_UP: 'Follow-up', UNRESOLVED_OBLIGATION: 'Unresolved obligation',
                       SUGGESTION: 'Suggestion', HISTORICAL: 'Historical' };
  const STATUS_LABEL = { OPEN: 'Open', IN_PROGRESS: 'In progress', WAITING: 'Waiting', UNCERTAIN: 'Uncertain', NEEDS_REVIEW: 'Needs review',
                         RESOLVED_VERIFIED: 'Verified by provider', RESOLVED_USER_CONFIRMED: 'Confirmed by you (not verified)',
                         DISMISSED: 'Dismissed', CANCELLED: 'Cancelled' };
  const CATEGORY_LABEL = { ACTION_REQUIRED: 'Action required', REPLY_REQUIRED: 'Reply required', WAITING_ON_SOMEONE: 'Waiting on someone',
                           DEADLINE_TIME_SENSITIVE: 'Deadline sensitive', IMPORTANT_INFORMATION: 'Important information',
                           GENERAL_INFORMATION: 'General information', AUTOMATED_LOW_PRIORITY: 'Automated · low priority',
                           UNCERTAIN_HUMAN_REVIEW: 'Needs human review' };
  const TRANSITIONS = { OPEN: ['IN_PROGRESS', 'WAITING', 'NEEDS_REVIEW', 'RESOLVED_USER_CONFIRMED', 'DISMISSED', 'CANCELLED'],
                        IN_PROGRESS: ['WAITING', 'NEEDS_REVIEW', 'RESOLVED_USER_CONFIRMED', 'DISMISSED', 'CANCELLED'],
                        WAITING: ['IN_PROGRESS', 'NEEDS_REVIEW', 'RESOLVED_USER_CONFIRMED', 'DISMISSED', 'CANCELLED'],
                        UNCERTAIN: ['OPEN', 'IN_PROGRESS', 'WAITING', 'NEEDS_REVIEW', 'RESOLVED_USER_CONFIRMED', 'DISMISSED', 'CANCELLED'],
                        NEEDS_REVIEW: ['OPEN', 'IN_PROGRESS', 'WAITING', 'DISMISSED', 'CANCELLED'] };
  const CORRECTIONS = [
    ['WRONG_PRIORITY', 'Wrong priority'], ['WRONG_CATEGORY', 'Wrong category'], ['NOT_ACTIONABLE', 'Not an actionable request'],
    ['NOT_COMMITMENT', 'Not a commitment'], ['WRONG_OWNER', 'Wrong responsible person'], ['WRONG_DEADLINE', 'Wrong deadline'],
    ['ALREADY_COMPLETED', 'Already completed'], ['AUTOMATED', 'This is automated mail'], ['OTHER', 'Other correction (recorded only)']];
  const FILTERS = [['all', 'All'], ['action_required', 'Action required'], ['reply_required', 'Reply required'], ['my_commitments', 'My commitments'],
                   ['waiting', 'Waiting on others'], ['deadline', 'Deadline sensitive'], ['important', 'Important information'],
                   ['automated', 'Automated'], ['needs_review', 'Needs human review']];

  const $ = (s, root) => (root || document).querySelector(s);
  const $$ = (s, root) => Array.from((root || document).querySelectorAll(s));
  const E = (v) => esc(v === null || v === undefined ? '' : String(v));

  async function call(method, path, body) {
    if (body === undefined && method !== 'GET' && method !== 'DELETE') body = {};     // the server requires a JSON body and content type on mutations
    const headers = await authHeaders(body !== undefined ? { 'Content-Type': 'application/json' } : {});
    const r = await fetch(path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
    let d = {};
    try { d = await r.json(); } catch (_) { d = {}; }
    if (!r.ok) { const err = new Error(d.error || ('Request failed (HTTP ' + r.status + ')')); err.status = r.status; err.code = d.code; err.body = d; throw err; }
    return d;
  }
  const q = (extra) => 'mode=' + IB.mode + (extra ? '&' + extra : '');
  const when = (ts) => ts ? new Intl.DateTimeFormat(undefined, { timeZone: IB.tz, dateStyle: 'medium', timeStyle: 'short' }).format(new Date(ts * 1000)) : 'no date';
  const day = (ts) => ts ? new Intl.DateTimeFormat(undefined, { timeZone: IB.tz, weekday: 'short', month: 'short', day: 'numeric' }).format(new Date(ts * 1000)) : '';
  const ago = (ts) => {
    if (!ts) return 'never';
    const s = Math.round(Date.now() / 1000 - ts);
    if (s < 90) return 'just now';
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    if (s < 86400) return Math.round(s / 3600) + ' h ago';
    return Math.round(s / 86400) + ' days ago';
  };
  const chip = (text, cls) => `<span class="ib-chip ${cls || ''}">${E(text)}</span>`;
  const modeChip = () => IB.mode === 'REPLAY' ? chip('REPLAY · SYNTHETIC', 'sim') : chip('LIVE · GMAIL', 'ok');
  const priorityChip = (p) => chip(p === 'HIGH' ? 'High priority' : p === 'LOW' ? 'Low priority' : 'Normal priority', p === 'HIGH' ? 'hi' : '');
  const timingChip = (t) => ({ OVERDUE: chip('Overdue', 'bad'), DUE_TODAY: chip('Due today', 'warn'), DUE_SOON: chip('Due soon', 'warn'),
                               FUTURE: chip('Future', ''), UNKNOWN: chip('Deadline unknown', '') })[t] || '';
  const banner = () => IB.mode === 'REPLAY'
    ? '<div class="ib-banner ib-replay" role="status"><strong>REPLAY · SYNTHETIC DATA.</strong> These messages are a labelled test dataset, not your mailbox. Sends are SIMULATED and never reach Gmail.</div>'
    : '';
  const loading = (what) => `<p class="ib-loading">Loading ${E(what)}…</p>`;
  const errorBox = (e) => `<div class="ib-error" role="alert"><strong>Could not load this.</strong> ${E(e.message || 'Unknown error')}${e.code ? ' <span class="ib-chip bad">' + E(e.code) + '</span>' : ''}</div>`;
  const empty = (title, text, extra) => `<div class="ib-empty"><strong>${E(title)}</strong>${E(text)}${extra || ''}</div>`;
  const body = (view) => $(`[data-ib-body="${view}"]`);
  const tools = (html) => $$('[data-ib-tools]').forEach((el) => { el.innerHTML = html; });
  const modeTools = () => `<div class="ib-mode" role="group" aria-label="Data mode">
      <button type="button" data-ib="mode" data-value="LIVE" aria-pressed="${IB.mode === 'LIVE'}">Live</button>
      <button type="button" data-ib="mode" data-value="REPLAY" aria-pressed="${IB.mode === 'REPLAY'}">Replay (synthetic)</button></div>`;

  function go(route) { location.hash = route; }
  async function loadSettings() {
    try {
      const s = await call('GET', '/api/inbox/settings');
      IB.tz = s.timezone || 'UTC';
      IB.tzConfigured = !!s.timezone_configured;
    } catch (_) { /* keep UTC; the settings view says it is not configured */ }
  }

  /* ------------------------------------------------------------ Morning brief */
  async function renderMorning() {
    tools(modeTools() + `<button class="btn btn-small" type="button" data-ib="sync">Sync now</button>`);
    const el = body('morning');
    el.innerHTML = loading('your morning brief');
    try {
      const [brief, status] = await Promise.all([call('GET', '/api/inbox/brief?' + q()), call('GET', '/api/inbox/sync/status?' + q())]);
      IB.lastBrief = brief;
      const hour = new Date().toLocaleString('en-GB', { timeZone: IB.tz, hour: 'numeric', hour12: false });
      const greet = Number(hour) < 12 ? 'Good morning' : Number(hour) < 18 ? 'Good afternoon' : 'Good evening';
      $('#h-morning').textContent = greet;
      $('#morningEyebrow').textContent = 'Morning brief · ' + (brief.local_date || '');
      const tzNote = IB.tzConfigured ? '' : ' · timezone not set (UTC used)';
      $('#morningSub').textContent = `Timezone ${IB.tz}${tzNote}. Account: ${brief.account || (IB.mode === 'REPLAY' ? 'synthetic replay mailbox' : 'not connected')}.`;
      const conn = brief.connected ? (IB.mode === 'REPLAY' ? 'REPLAY' : 'LIVE') : (brief.connection_status || 'NOT_CONNECTED');
      const stateChip = { LIVE: chip('LIVE', 'ok'), REPLAY: chip('REPLAY', 'sim'), NOT_CONNECTED: chip('NOT CONNECTED', 'warn'),
                          REVOKED: chip('REVOKED', 'bad'), EXPIRED: chip('TOKEN EXPIRED', 'bad'), RATE_LIMITED: chip('RATE-LIMITED', 'warn'),
                          ERROR: chip('ERROR', 'bad'), STALE: chip('STALE', 'warn') }[status.processing_state === 'STALE' ? 'STALE' : conn] || chip(conn);
      const header = `<div class="ib-status">${modeChip()}${stateChip}${chip('Engine: ' + (brief.engine || 'RULES_ONLY') + ' (no model inference)', '')}
        ${chip('Last sync ' + ago(status.last_successful_sync_at), status.stale ? 'warn' : '')}
        ${chip((status.messages || 0) + ' messages · ' + (status.threads || 0) + ' threads', '')}</div>`;
      if (!brief.connected) {
        el.innerHTML = banner() + header + (IB.mode === 'LIVE'
          ? empty('Your inbox is not connected yet', brief.connection_status === 'NOT_CONNECTED' ? 'Connect Gmail with read-only access to see what needs attention. Nothing is read until you connect.' : 'Your Gmail connection needs attention. Check Connections.',
                  `<p><a class="btn btn-primary" href="#connections" data-ib="nav" data-route="#connections">Open Connections</a> <a class="btn" href="#replay" data-ib="nav" data-route="#replay">Try the replay lab</a></p>`)
          : empty('No replay started', 'Open the replay lab to run the synthetic morning.', `<p><a class="btn btn-primary" href="#replay" data-ib="nav" data-route="#replay">Open the replay lab</a></p>`));
        return;
      }
      const s = brief.summary || {};
      const kpi = (n, label, route, filter) => `<button type="button" class="ib-kpi" data-ib="kpi" data-route="${E(route)}" data-filter="${E(filter || '')}"><span class="n">${Number(n || 0)}</span><span class="l">${E(label)}</span></button>`;
      const kpis = `<div class="ib-kpis">${kpi(s.needs_attention, 'Needs your attention', '#inbox', 'action_required')}
        ${kpi(s.replies_required, 'Replies to send', '#inbox', 'reply_required')}
        ${kpi(s.my_open_commitments, 'My open commitments', '#commitments', 'mine')}
        ${kpi(s.waiting_on_others, 'Waiting on others', '#commitments', 'waiting')}
        ${kpi(s.overdue, 'Overdue', '#commitments', 'overdue')}
        ${kpi(s.drafts_waiting, 'Drafts to review', '#drafts', '')}</div>`;
      const attention = (brief.needs_attention || []).map((r) => `<li class="ib-item">
          <div class="ib-item-head"><a class="ib-item-title" href="#thread:${E(r.thread_id)}" data-ib="nav" data-route="#thread:${E(r.thread_id)}">${E(r.subject || '(no subject)')}</a>
          <span>${priorityChip(r.priority)} ${chip(CATEGORY_LABEL[r.category] || r.category, '')}</span></div>
          <div class="ib-meta">From ${E(r.sender)} · last message ${E(when(r.last_message_at))}${r.deadline ? ' · deadline ' + E(when(r.deadline)) : ''}</div>
          <p class="ib-why"><strong>Why it matters:</strong> ${E((r.reasons || [])[0] || 'needs your decision')}</p>
          ${r.evidence && r.evidence[0] ? `<blockquote class="ib-evidence">${E(r.evidence[0])}</blockquote>` : ''}
          <p class="ib-meta">Decision needed: ${E(r.review_required ? 'review the classification and set the deadline' : r.awaiting_user_reply ? 'reply or decline' : 'read and decide')} · confidence ${Math.round((r.confidence || 0) * 100)}%${r.review_required ? ' · needs human review' : ''}</p>
          <div class="ib-actions"><a class="btn btn-small" href="#thread:${E(r.thread_id)}" data-ib="nav" data-route="#thread:${E(r.thread_id)}">Open conversation</a>
          ${r.awaiting_user_reply ? `<button class="btn btn-small btn-primary" type="button" data-ib="new-draft" data-thread="${E(r.thread_id)}">Prepare reply</button>` : ''}
          <button class="btn btn-small" type="button" data-ib="thread-state" data-state="DEFERRED" data-thread="${E(r.thread_id)}">Defer</button>
          <button class="btn btn-small" type="button" data-ib="thread-state" data-state="DISMISSED" data-thread="${E(r.thread_id)}">Dismiss</button></div></li>`).join('');
      const replies = (brief.replies_to_send || []).map((r) => `<li class="ib-item"><div class="ib-item-head"><a class="ib-item-title" href="#thread:${E(r.thread_id)}" data-ib="nav" data-route="#thread:${E(r.thread_id)}">${E(r.subject)}</a><span>${chip('Reply owed', 'hi')}</span></div>
          <div class="ib-meta">${E(r.sender)} · ${E(when(r.last_message_at))}</div>${r.question ? `<blockquote class="ib-evidence">${E(r.question)}</blockquote>` : ''}
          <div class="ib-actions"><button class="btn btn-small btn-primary" type="button" data-ib="new-draft" data-thread="${E(r.thread_id)}">Generate reply</button>
          <a class="btn btn-small" href="#thread:${E(r.thread_id)}" data-ib="nav" data-route="#thread:${E(r.thread_id)}">Open conversation</a>
          <button class="btn btn-small" type="button" data-ib="thread-state" data-state="DEFERRED" data-thread="${E(r.thread_id)}">Defer</button>
          <button class="btn btn-small" type="button" data-ib="thread-state" data-state="DISMISSED" data-thread="${E(r.thread_id)}">Dismiss with reason</button></div></li>`).join('');
      const commitItem = (c, mine) => `<li class="ib-item"><div class="ib-item-head"><a class="ib-item-title" href="#commitment:${E(c.id)}" data-ib="nav" data-route="#commitment:${E(c.id)}">${E(c.required_action)}</a>
          <span>${chip(KIND_LABEL[c.kind] || c.kind, '')} ${timingChip(c.timing)}</span></div>
          <div class="ib-meta">${mine ? 'To ' + E(c.requesting_person || 'unknown recipient') : 'From ' + E(c.responsible_person || 'unknown owner')} · ${c.deadline_at ? 'due ' + E(when(c.deadline_at)) : 'no deadline stated'}${c.deadline_confidence === 'AMBIGUOUS' ? ' (ambiguous: not invented)' : ''}</div>
          <blockquote class="ib-evidence">${E(c.evidence)}</blockquote>
          <div class="ib-actions"><a class="btn btn-small" href="#commitment:${E(c.id)}" data-ib="nav" data-route="#commitment:${E(c.id)}">Open details</a>
          ${mine ? '' : `<button class="btn btn-small" type="button" data-ib="follow-up" data-id="${E(c.id)}" data-trigger="OTHER_PARTY_PROMISE">Record follow-up</button>`}</div></li>`;
      const mine = (brief.my_commitments || []).map((c) => commitItem(c, true)).join('');
      const waiting = (brief.waiting_on_others || []).map((c) => commitItem(c, false)).join('');
      const timing = brief.timing || {};
      const bucket = (key, title) => {
        const items = timing[key] || [];
        if (!items.length) return '';
        return `<h3 style="margin-top:14px">${E(title)} <span class="muted">(${items.length})</span></h3><ul class="ib-list">${items.map((c) => commitItem(c, c.kind !== 'PROMISE_BY_OTHER')).join('')}</ul>`;
      };
      const drafts = (brief.drafts || []).map((d) => `<li class="ib-item"><div class="ib-item-head"><a class="ib-item-title" href="#draft:${E(d.id)}" data-ib="nav" data-route="#draft:${E(d.id)}">${E(d.subject || 'Draft reply')}</a><span>${chip(d.status === 'APPROVED' ? 'Approved · not sent' : d.status === 'SAVED_TO_GMAIL' ? 'Saved to Gmail (verified)' : 'Draft', d.status === 'APPROVED' ? 'hi' : '')}</span></div>
          <div class="ib-meta">To ${E(d.recipient)} · created ${E(when(d.created_at))} · version ${E(d.version)} · source <a href="#thread:${E(d.thread_id)}" data-ib="nav" data-route="#thread:${E(d.thread_id)}">conversation</a></div>
          <div class="ib-actions"><a class="btn btn-small btn-primary" href="#draft:${E(d.id)}" data-ib="nav" data-route="#draft:${E(d.id)}">Review</a><button class="btn btn-small" type="button" data-ib="reject" data-id="${E(d.id)}">Reject</button></div></li>`).join('');
      const resolved = (brief.recently_resolved || []).map((c) => `<li class="ib-item"><div class="ib-item-head"><span class="ib-item-title">${E(c.required_action)}</span><span>${chip(STATUS_LABEL[c.status] || c.status, c.status === 'RESOLVED_VERIFIED' ? 'ok' : '')}</span></div>
          <div class="ib-meta">Updated ${E(when(c.updated_at))} · ${c.status === 'RESOLVED_VERIFIED' ? 'verified by an execution record' : c.status === 'RESOLVED_USER_CONFIRMED' ? 'your confirmation; not independently verified' : 'closed without completion'}</div></li>`).join('');
      el.innerHTML = banner() + header + kpis +
        `<section class="ib-section"><h2>Needs your attention</h2><p class="muted">Messages that need a decision, with the reason and the sentence that caused it.</p>
          ${attention ? `<ul class="ib-list">${attention}</ul>` : empty('Nothing needs your attention', 'Nothing in the synchronized mail needs a decision right now.')}</section>` +
        `<section class="ib-section"><h2>Replies to send</h2><p class="muted">Conversations where you owe an answer. Replies you already sent are not listed.</p>
          ${replies ? `<ul class="ib-list">${replies}</ul>` : empty('No replies owed', 'Every synchronized conversation is answered or needs no reply.')}</section>` +
        `<section class="ib-section"><h2>What you promised</h2><p class="muted">Your open commitments, with the recipient and the source sentence.</p>
          ${mine ? `<ul class="ib-list">${mine}</ul>` : empty('No open promises', 'No promise you made is open in the synchronized mail.')}</section>` +
        `<section class="ib-section"><h2>What you are waiting for</h2><p class="muted">Promises other people made to you. No response deadline is invented.</p>
          ${waiting ? `<ul class="ib-list">${waiting}</ul>` : empty('Nothing you are waiting for', 'No one has promised you a deliverable in the synchronized mail.')}</section>` +
        `<section class="ib-section"><h2>Upcoming and overdue</h2><p class="muted">Grouped by timing in ${E(IB.tz)}. Items with no stated deadline are listed separately.</p>
          ${bucket('OVERDUE', 'Overdue')}${bucket('DUE_TODAY', 'Due today')}${bucket('DUE_SOON', 'Due soon')}${bucket('FUTURE', 'Future')}${bucket('UNKNOWN', 'Deadline unknown')}
          ${Object.values(timing).every((a) => !a.length) ? empty('Nothing scheduled', 'No open obligations carry a deadline.') : ''}</section>` +
        `<section class="ib-section"><h2>Ready to review</h2><p class="muted">Prepared drafts. Approval applies to the exact text you review.</p>
          ${drafts ? `<ul class="ib-list">${drafts}</ul>` : empty('No drafts waiting', 'Prepare a reply from a conversation, then review it here.')}</section>` +
        `<section class="ib-section"><h2>Recently resolved</h2><p class="muted">Last ${E(brief.recent_window_days || 14)} days. Verified and user-confirmed completion are different things.</p>
          ${resolved ? `<ul class="ib-list">${resolved}</ul>` : empty('Nothing resolved recently', 'Resolved and dismissed obligations appear here for ' + (brief.recent_window_days || 14) + ' days.')}</section>`;
    } catch (e) {
      el.innerHTML = banner() + errorBox(e);
    }
  }

  /* ------------------------------------------------------------ Inbox list */
  async function renderInbox() {
    tools(modeTools());
    const el = body('inbox');
    const filters = `<div class="ib-filters" role="group" aria-label="Filter conversations">${FILTERS.map(([v, l]) => `<button type="button" data-ib="filter" data-value="${v}" aria-pressed="${IB.filter === v}">${E(l)}</button>`).join('')}</div>`;
    const toolbar = `<form class="ib-toolbar" data-ib-form="search" role="search">
      <input type="search" name="q" aria-label="Search conversations" placeholder="Search sender, subject or text" value="${E(IB.q)}">
      <label class="visually-hidden" for="ibSort">Sort</label><select id="ibSort" name="sort"><option value="date" ${IB.sort === 'date' ? 'selected' : ''}>Newest first</option><option value="priority" ${IB.sort === 'priority' ? 'selected' : ''}>Priority</option></select>
      <label class="visually-hidden" for="ibSince">Date</label><select id="ibSince" name="since"><option value="0" ${!IB.since ? 'selected' : ''}>Any date</option><option value="7" ${IB.since === 7 ? 'selected' : ''}>Last 7 days</option><option value="30" ${IB.since === 30 ? 'selected' : ''}>Last 30 days</option></select>
      <button class="btn btn-small" type="submit">Apply</button></form>`;
    el.innerHTML = banner() + filters + toolbar + loading('conversations');
    try {
      const params = `filter=${IB.filter}&sort=${IB.sort}&page=${IB.page}&size=25&q=${encodeURIComponent(IB.q)}${IB.since ? '&since_days=' + IB.since : ''}`;
      const d = await call('GET', '/api/inbox/threads?' + q(params));
      const rows = d.threads.map((t) => `<li class="ib-item">
          <div class="ib-item-head"><a class="ib-item-title" href="#thread:${E(t.thread_id)}" data-ib="nav" data-route="#thread:${E(t.thread_id)}">${E(t.subject || '(no subject)')}</a>
          <span class="ib-meta">${E(when(t.last_activity))}</span></div>
          <div class="ib-meta">${E(t.sender)} · ${t.message_count} message${t.message_count === 1 ? '' : 's'}</div>
          <p class="ib-why">${E(t.preview)}</p>
          <div class="ib-actions">${chip(CATEGORY_LABEL[t.category] || t.category, '')}${priorityChip(t.priority)}
            ${t.commitments ? chip(t.commitments + ' commitment' + (t.commitments === 1 ? '' : 's'), '') : ''}
            ${t.reply_status === 'REPLY_OWED' ? chip('Reply owed', 'hi') : chip('No reply owed', '')}
            ${t.overridden ? chip('Corrected by you', 'warn') : ''}${t.review_required ? chip('Needs review', 'warn') : ''}
            <span class="ib-meta">decided ${E(when(t.last_decision_at))}</span></div></li>`).join('');
      const pager = `<nav class="ib-pager" aria-label="Pagination"><span>${d.total} conversation${d.total === 1 ? '' : 's'} · page ${d.page}</span>
        <button class="btn btn-small" type="button" data-ib="page" data-delta="-1" ${IB.page <= 1 ? 'disabled' : ''}>Previous</button>
        <button class="btn btn-small" type="button" data-ib="page" data-delta="1" ${d.page * d.size >= d.total ? 'disabled' : ''}>Next</button></nav>`;
      el.innerHTML = banner() + filters + toolbar +
        (rows ? `<ul class="ib-list" aria-label="Conversations">${rows}</ul>${pager}`
              : empty('No conversations here', IB.filter === 'all' ? 'Sync your mailbox, or open the replay lab to load synthetic data.' : 'Nothing matches this filter. Try All.',
                      `<p><a class="btn btn-small" href="#morning" data-ib="nav" data-route="#morning">Back to the morning brief</a></p>`));
    } catch (e) {
      el.innerHTML = banner() + filters + toolbar + errorBox(e);
    }
  }

  /* ------------------------------------------------------------ Thread detail */
  async function renderThread() {
    tools(modeTools());
    const el = body('thread');
    if (!IB.threadId) { el.innerHTML = empty('No conversation selected', 'Open one from the inbox.'); return; }
    el.innerHTML = loading('the conversation');
    try {
      const t = await call('GET', `/api/inbox/threads/${encodeURIComponent(IB.threadId)}?` + q());
      const a = t.analysis;
      const msgs = t.messages.map((m) => `<article class="ib-msg ${m.direction === 'OUTBOUND' ? 'out' : ''}" id="msg-${E(m.id)}" tabindex="-1">
          <header><span><strong>${E(m.sender)}</strong> ${chip(m.direction === 'OUTBOUND' ? 'Outbound (you)' : 'Inbound', '')}</span><span>${E(when(m.sent_at))}</span></header>
          <div class="ib-meta">To ${E(m.recipients || '—')}</div><div class="ib-meta"><strong>${E(m.subject)}</strong></div>
          <pre>${E(m.body)}</pre></article>`).join('');
      const commits = a.commitments.map((c) => `<li class="ib-item"><div class="ib-item-head"><a class="ib-item-title" href="#commitment:${E(c.id)}" data-ib="nav" data-route="#commitment:${E(c.id)}">${E(KIND_LABEL[c.kind] || c.kind)}: ${E(c.required_action)}</a>${chip(STATUS_LABEL[c.status] || c.status, '')}</div>
          <blockquote class="ib-evidence">${E(c.evidence)}${c.source_message_id ? ` <a href="#msg-${E(c.source_message_id)}" data-ib="anchor" data-id="${E(c.source_message_id)}">source message</a>` : ''}</blockquote>
          <div class="ib-meta">Owner: ${E(c.responsible_person || 'not resolved')} · requested by ${E(c.requesting_person || 'not resolved')} · ownership ${E(c.ownership_state)}</div>
          <div class="ib-meta">Deadline: ${c.deadline_at ? E(when(c.deadline_at)) + ' (' + E(c.deadline_timezone) + ', ' + E(c.deadline_confidence) + ')' : 'not stated' }${c.deadline_text ? ' · phrase "' + E(c.deadline_text) + '"' : ''}</div></li>`).join('');
      const corrOptions = CORRECTIONS.map(([v, l]) => `<option value="${v}">${E(l)}</option>`).join('');
      const draftList = (t.drafts || []).map((d) => `<li><a href="#draft:${E(d.id)}" data-ib="nav" data-route="#draft:${E(d.id)}">${E(d.subject || 'Draft')}</a> ${chip(d.status, '')}</li>`).join('');
      el.innerHTML = banner() + `<div class="ib-two">
        <section aria-label="Conversation">${msgs}</section>
        <aside aria-label="Analysis">
          <div class="ib-panel"><h3>RESOLVE's understanding</h3><p>${E(a.understanding)}</p>
            <dl class="ib-kv"><dt>Classification</dt><dd>${E(CATEGORY_LABEL[a.classification] || a.classification)}${a.classification_as_stored !== a.classification ? ' <span class="muted">(stored: ' + E(a.classification_as_stored) + ')</span>' : ''}</dd>
            <dt>Priority</dt><dd>${E(a.priority)}</dd><dt>Confidence</dt><dd>${Math.round((a.confidence || 0) * 100)}% (rules engine)</dd>
            <dt>Why</dt><dd>${E((a.decision_explanation || []).join('; '))}</dd></dl></div>
          <div class="ib-panel"><h3>Evidence</h3>${(a.evidence || []).length ? a.evidence.map((e) => `<blockquote class="ib-evidence">${E(e.text)}${e.message_id ? ` <a href="#msg-${E(e.message_id)}" data-ib="anchor" data-id="${E(e.message_id)}">message</a>` : ''}</blockquote>`).join('') : '<p class="muted">No sentence supports a classification.</p>'}</div>
          <div class="ib-panel"><h3>Obligations</h3>${commits ? `<ul class="ib-list">${commits}</ul>` : '<p class="muted">No requests, promises or dependencies were found in this conversation.</p>'}</div>
          <div class="ib-panel"><h3>Dates and uncertainty</h3>${(a.dates || []).length ? '<ul>' + a.dates.map((d) => `<li>${E(d.phrase || '')} → ${d.at ? E(when(d.at)) : 'no date'} (${E(d.confidence || 'none')})</li>`).join('') + '</ul>' : '<p class="muted">No date was stated.</p>'}
            ${(a.uncertainty || []).length ? '<p class="ib-hint">Uncertain: ' + E(a.uncertainty.join('; ')) + '</p>' : ''}</div>
          <div class="ib-panel"><h3>Correct this decision</h3>
            <p class="ib-hint">Dictate or type into the field below. Wispr Flow (or any dictation tool) can type into this box; RESOLVE receives only the text.</p>
            <form data-ib-form="correct" data-thread="${E(t.thread_id)}">
              <div class="ib-field"><label for="ibCorrKind">Correction</label><select id="ibCorrKind" name="kind">${corrOptions}</select></div>
              <div class="ib-field"><label for="ibCorrValue">Value (where needed)</label><input id="ibCorrValue" name="value" placeholder="e.g. LOW, or a corrected email address, or 2026-11-06T16:00"></div>
              <div class="ib-field dictate"><label for="ibCorrText">Your words (dictate here)</label><textarea id="ibCorrText" name="dictated_text" spellcheck="true" placeholder="That isn't urgent. It is an ordinary status update."></textarea></div>
              <div class="ib-field"><label for="ibCorrScope">Scope</label><select id="ibCorrScope" name="scope">
                <option value="ONE_TIME">One-time correction (this conversation only)</option>
                <option value="PERSISTENT_THREAD">Remember for this conversation</option>
                <option value="PERSISTENT_WORKSPACE">Remember for this sender (needs your confirmation)</option></select></div>
              <label class="ib-row"><input type="checkbox" name="confirmed"> I confirm the remembered preference</label>
              <div class="ib-actions"><button class="btn btn-primary" type="submit">Preview and save correction</button></div>
              <div id="ibCorrResult" aria-live="polite"></div>
            </form></div>
          <div class="ib-panel"><h3>Replies</h3>${draftList ? `<ul>${draftList}</ul>` : '<p class="muted">No reply has been prepared yet.</p>'}
            <div class="ib-actions"><button class="btn btn-primary" type="button" data-ib="new-draft" data-thread="${E(t.thread_id)}">Prepare reply</button></div></div>
        </aside></div>`;
    } catch (e) {
      el.innerHTML = banner() + errorBox(e);
    }
  }

  /* ------------------------------------------------------------ Commitments */
  async function renderCommitments() {
    tools(modeTools());
    const el = body('commitments');
    const filters = `<div class="ib-filters" role="group" aria-label="Filter commitments">${[['open', 'Open'], ['mine', 'My promises'], ['requests', 'Requests to me'], ['waiting', 'Waiting on others'], ['overdue', 'Overdue'], ['resolved', 'Resolved']]
      .map(([v, l]) => `<button type="button" data-ib="cfilter" data-value="${v}" aria-pressed="${IB.cfilter === v}">${E(l)}</button>`).join('')}</div>`;
    el.innerHTML = banner() + filters + loading('commitments');
    try {
      const params = { open: 'status=OPEN', mine: 'kind=PROMISE_BY_USER', requests: 'kind=REQUEST_TO_USER', waiting: 'kind=PROMISE_BY_OTHER', overdue: 'timing=OVERDUE', resolved: 'status=RESOLVED_VERIFIED' }[IB.cfilter] || '';
      let list = (await call('GET', '/api/inbox/commitments?' + q(params))).commitments;
      if (IB.cfilter === 'open') list = list.filter((c) => ['OPEN', 'IN_PROGRESS', 'WAITING', 'UNCERTAIN', 'NEEDS_REVIEW'].includes(c.status));
      if (IB.cfilter === 'resolved') list = (await call('GET', '/api/inbox/commitments?' + q('status=RESOLVED_USER_CONFIRMED'))).commitments.concat(list);
      const rows = list.map((c) => `<li class="ib-item"><div class="ib-item-head"><a class="ib-item-title" href="#commitment:${E(c.id)}" data-ib="nav" data-route="#commitment:${E(c.id)}">${E(c.required_action)}</a>
          <span>${chip(KIND_LABEL[c.kind] || c.kind, '')} ${c.timing ? timingChip(c.timing) : ''} ${chip(STATUS_LABEL[c.status] || c.status, c.status === 'RESOLVED_VERIFIED' ? 'ok' : '')}</span></div>
          <div class="ib-meta">Owner ${E(c.responsible_person || 'not resolved')} · requested by ${E(c.requesting_person || 'not resolved')} · ${c.deadline_at ? 'due ' + E(when(c.deadline_at)) : 'deadline not stated'}</div>
          <blockquote class="ib-evidence">${E(c.evidence)}</blockquote></li>`).join('');
      el.innerHTML = banner() + filters + (rows ? `<ul class="ib-list">${rows}</ul>`
        : empty('No commitments here', IB.mode === 'REPLAY' ? 'Run the replay to create synthetic commitments.' : 'Commitments appear after a sync finds a request, a promise or a dependency in your mail.'));
    } catch (e) {
      el.innerHTML = banner() + filters + errorBox(e);
    }
  }

  async function renderCommitment() {
    tools(modeTools());
    const el = body('commitment');
    if (!IB.commitmentId) { el.innerHTML = empty('No commitment selected', 'Open one from the ledger.'); return; }
    el.innerHTML = loading('the commitment');
    try {
      const c = await call('GET', `/api/inbox/commitments/${encodeURIComponent(IB.commitmentId)}?` + q());
      const next = TRANSITIONS[c.status] || [];
      const verifiedExecs = (await call('GET', '/api/inbox/executions?' + q())).executions.filter((x) => x.state === 'VERIFIED');
      const history = c.status_history.map((h) => `<li>${E(when(h.created_at))} · ${E(STATUS_LABEL[h.from_status] || h.from_status)} → ${E(STATUS_LABEL[h.to_status] || h.to_status)}${h.reason ? ' · ' + E(h.reason) : ''}</li>`).join('');
      const revs = c.revisions.map((r) => `<li>${E(when(r.created_at))} · previous deadline ${r.deadline_at ? E(when(r.deadline_at)) : 'none'} (${E(r.reason)})</li>`).join('');
      const notes = c.notes.map((n) => `<li>${E(when(n.created_at))} · ${E(n.note)}</li>`).join('');
      const fus = c.follow_ups.map((f) => `<li>${E(when(f.created_at))} · ${E(f.trigger)} → ${E(f.target)} (${E(f.status)})</li>`).join('');
      const related = c.related.map((r) => `<li>${E(KIND_LABEL[r.kind] || r.kind)} · ${E(r.required_action)} · ${E(STATUS_LABEL[r.status] || r.status)}</li>`).join('');
      el.innerHTML = banner() + `<div class="ib-two"><section>
        <div class="ib-panel"><h3>${E(c.required_action)}</h3>
          <dl class="ib-kv"><dt>Type</dt><dd>${E(KIND_LABEL[c.kind] || c.kind)}</dd>
          <dt>Status</dt><dd>${chip(STATUS_LABEL[c.status] || c.status, c.status === 'RESOLVED_VERIFIED' ? 'ok' : '')}</dd>
          <dt>Responsible</dt><dd>${E(c.responsible_person || 'not resolved (not invented)')}</dd>
          <dt>Requested by</dt><dd>${E(c.requesting_person || 'not resolved')}</dd>
          <dt>Ownership</dt><dd>${E(c.ownership_state)}</dd>
          <dt>Deadline</dt><dd>${c.deadline_at ? E(when(c.deadline_at)) + ' · ' + E(c.deadline_timezone || '') + ' · ' + E(c.deadline_confidence || '') : 'not stated' }${c.deadline_text ? ' · phrase "' + E(c.deadline_text) + '"' : ''}</dd>
          <dt>Source</dt><dd>${c.source_message ? `<a href="#thread:${E(c.source_message.thread_id)}" data-ib="nav" data-route="#thread:${E(c.source_message.thread_id)}">${E(c.source_message.subject)}</a> · ${E(c.source_message.sender)} · ${E(when(c.source_message.sent_at))}` : ''}</dd>
          <dt>Exact sentence</dt><dd><blockquote class="ib-evidence">${E(c.evidence)}</blockquote></dd>
          <dt>Completion evidence</dt><dd>${c.resolution_evidence_id ? E(c.resolution_evidence_id) : 'none recorded'}</dd></dl></div>
        <div class="ib-panel"><h3>Related commitments in this conversation</h3>${related ? `<ul>${related}</ul>` : '<p class="muted">None.</p>'}</div>
        <div class="ib-panel"><h3>Previous revisions</h3>${revs ? `<ul>${revs}</ul>` : '<p class="muted">No deadline has been revised.</p>'}</div>
        <div class="ib-panel"><h3>Status history</h3>${history ? `<ul>${history}</ul>` : '<p class="muted">No status change yet.</p>'}</div>
        <div class="ib-panel"><h3>Follow-ups and notes</h3>${fus ? `<ul>${fus}</ul>` : '<p class="muted">No follow-up recorded.</p>'}${notes ? `<ul>${notes}</ul>` : ''}</div>
        </section><aside>
        <div class="ib-panel"><h3>Change the status</h3>
          ${next.length ? `<div class="ib-actions">${next.map((s) => `<button class="btn btn-small" type="button" data-ib="status" data-status="${s}" data-id="${E(c.id)}">${E(STATUS_LABEL[s])}</button>`).join('')}</div>` : '<p class="muted">This status is final.</p>'}
          <p class="ib-hint">Dismissing or cancelling needs a reason. Completion you confirm is recorded as yours, not as verified.</p>
          <form data-ib-form="status-reason" data-id="${E(c.id)}"><div class="ib-field"><label for="ibReason">Reason</label><input id="ibReason" name="reason" placeholder="why this changes"></div></form></div>
        <div class="ib-panel"><h3>Verified completion</h3>
          ${verifiedExecs.length ? `<form data-ib-form="resolve-verified" data-id="${E(c.id)}"><div class="ib-field"><label for="ibExec">Verified execution</label><select id="ibExec" name="evidence">${verifiedExecs.map((x) => `<option value="${E(x.id)}">${E(x.operation)} · ${E(x.provider_id || '')}</option>`).join('')}</select></div><button class="btn btn-primary" type="submit">Resolve with this evidence</button></form>`
            : '<p class="muted">No verified execution exists yet. Verified completion needs an email RESOLVE sent and read back.</p>'}</div>
        <div class="ib-panel"><h3>Confirm completion yourself</h3><p class="ib-hint">Recorded as RESOLVED_USER_CONFIRMED. It is not independent verification.</p>
          <form data-ib-form="confirm" data-id="${E(c.id)}"><div class="ib-field"><label for="ibConfirm">What did you do?</label><input id="ibConfirm" name="reason" required placeholder="e.g. sent the schema from my laptop"></div><button class="btn" type="submit">Confirm completion</button></form></div>
        <div class="ib-panel"><h3>Correct the details</h3>
          <form data-ib-form="edit" data-id="${E(c.id)}">
            <div class="ib-field"><label for="ibEditAction">What must happen</label><textarea id="ibEditAction" name="required_action" rows="3">${E(c.required_action)}</textarea></div>
            <div class="ib-field"><label for="ibEditOwner">Responsible person (email)</label><input id="ibEditOwner" name="responsible_person" value="${E(c.responsible_person || '')}"></div>
            <div class="ib-field"><label for="ibEditDeadline">Deadline (local, ${E(IB.tz)})</label><input id="ibEditDeadline" name="deadline_local" placeholder="2026-11-06T16:00"></div>
            <div class="ib-field"><label for="ibEditReason">Reason for the change</label><input id="ibEditReason" name="reason" placeholder="optional"></div>
            <button class="btn" type="submit">Save corrections</button></form></div>
        <div class="ib-panel"><h3>Add context</h3><form data-ib-form="note" data-id="${E(c.id)}"><div class="ib-field dictate"><label for="ibNote">Context (dictate or type)</label><textarea id="ibNote" name="note" spellcheck="true"></textarea></div><button class="btn" type="submit">Add context</button></form></div>
        <div class="ib-panel"><h3>Create follow-up</h3><form data-ib-form="follow-up" data-id="${E(c.id)}">
          <div class="ib-field"><label for="ibFuTrigger">Reminder for</label><select id="ibFuTrigger" name="trigger"><option value="OTHER_PARTY_PROMISE">Another person's outstanding promise</option><option value="OUTSTANDING_OWN_COMMITMENT">My outstanding commitment</option><option value="UNANSWERED_THREAD">An unanswered thread</option><option value="DUE_DATE_APPROACHING">A due date approaching</option></select></div>
          <div class="ib-field"><label for="ibFuTarget">Target (email; optional)</label><input id="ibFuTarget" name="target" placeholder="defaults to the responsible or requesting person"></div>
          <button class="btn" type="submit">Record follow-up</button></form></div>
        </aside></div>`;
    } catch (e) {
      el.innerHTML = banner() + errorBox(e);
    }
  }

  /* ------------------------------------------------------------ Drafts */
  async function renderDrafts() {
    tools(modeTools());
    const el = body('drafts');
    el.innerHTML = banner() + loading('drafts');
    try {
      const list = (await call('GET', '/api/inbox/drafts?' + q())).drafts;
      const rows = list.map((d) => `<li class="ib-item"><div class="ib-item-head"><a class="ib-item-title" href="#draft:${E(d.id)}" data-ib="nav" data-route="#draft:${E(d.id)}">${E(d.subject || 'Draft reply')}</a>
          ${chip(d.status === 'SENT_VERIFIED' ? 'Sent · verified' : d.status === 'SAVED_TO_GMAIL' ? 'Gmail draft verified' : d.status === 'APPROVED' ? 'Approved · not sent' : d.status, d.status === 'APPROVED' ? 'hi' : '')}</div>
          <div class="ib-meta">To ${E(d.recipient)} · version ${E(d.version)} · updated ${E(when(d.updated_at))}</div>
          <div class="ib-actions"><a class="btn btn-small btn-primary" href="#draft:${E(d.id)}" data-ib="nav" data-route="#draft:${E(d.id)}">Open</a><a class="btn btn-small" href="#thread:${E(d.thread_id)}" data-ib="nav" data-route="#thread:${E(d.thread_id)}">Source conversation</a></div></li>`).join('');
      el.innerHTML = banner() + (rows ? `<ul class="ib-list">${rows}</ul>` : empty('No drafts yet', 'Open a conversation that needs a reply and choose Prepare reply. Drafts are saved only when you ask.'));
    } catch (e) { el.innerHTML = banner() + errorBox(e); }
  }

  async function renderDraft() {
    tools(modeTools());
    const el = body('draft');
    if (!IB.draftId) { el.innerHTML = empty('No draft selected', 'Open one from the drafts list.'); return; }
    el.innerHTML = loading('the draft');
    try {
      const d = await call('GET', `/api/inbox/drafts/${encodeURIComponent(IB.draftId)}?` + q());
      const meta = d.meta || {};
      const sources = (meta.sources || []).map((s) => `<li>${E(s.claim)}: <q>${E(s.evidence)}</q></li>`).join('');
      const missing = (meta.missing_information || []).map((m) => `<li>${E(m)}</li>`).join('');
      const assumptions = (meta.assumptions || []).map((m) => `<li>${E(m)}</li>`).join('');
      const pcs = (meta.potential_commitments || []).map((m) => `<li>${E(m)} <span class="muted">(not active until the message is sent and read back)</span></li>`).join('');
      const versions = (d.versions || []).map((v) => `<li>v${E(v.version)} · ${E(v.origin)} · ${E(when(v.created_at))}</li>`).join('');
      const execs = (d.executions || []).map((x) => `<li>${E(when(x.updated_at))} · ${E(x.operation)} · ${E(x.state)}${x.provider_id ? ' · ' + E(x.provider_id) : ''}${IB.mode === 'REPLAY' ? ' · SIMULATED' : ''}</li>`).join('');
      const editable = ['DRAFT', 'APPROVED', 'SAVED_TO_GMAIL'].includes(d.status);
      const approval = IB.approval && IB.approval.draft_id === d.id ? IB.approval : null;
      const approvalBox = approval ? `<div class="ib-approval" role="region" aria-label="Exact approval">
          <h3>Approve this exact version</h3>
          <dl class="ib-kv"><dt>Recipient</dt><dd>${E(d.recipient)}</dd><dt>Cc</dt><dd>${E(d.cc || '—')}</dd><dt>Subject</dt><dd>${E(d.subject)}</dd>
          <dt>Draft version</dt><dd>${E(d.version)}</dd><dt>Digest</dt><dd><code>${E(d.digest)}</code></dd><dt>Approval expires</dt><dd>${E(when(approval.expires_at))}</dd></dl>
          <p class="ib-hint">The exact text that will be sent:</p><pre>${E(d.body)}</pre>
          <p class="ib-hint">Source conversation: <a href="#thread:${E(d.thread_id)}" data-ib="nav" data-route="#thread:${E(d.thread_id)}">open</a>.</p>
          <p class="ib-hint"><strong>Missing information:</strong> ${missing ? '<ul>' + missing + '</ul>' : 'none listed'}</p>
          ${IB.mode === 'LIVE' ? '' : '<p class="ib-banner ib-replay">Simulated. This will not be delivered by Gmail.</p>'}
          <div class="ib-actions">
            ${IB.mode === 'LIVE' ? `<button class="btn" type="button" data-ib="gmail-draft" data-approval="${E(approval.id)}" data-digest="${E(d.digest)}">Save as Gmail draft (verified by read-back)</button>` : ''}
            <button class="btn btn-primary" type="button" data-ib="send" data-approval="${E(approval.id)}" data-digest="${E(d.digest)}">${IB.mode === 'LIVE' ? 'Send this exact message' : 'Send (simulated)'}</button>
            <button class="btn btn-ghost" type="button" data-ib="cancel-approval">Cancel approval</button></div>
          <div id="ibSendResult" aria-live="polite"></div></div>` : '';
      el.innerHTML = banner() + `<div class="ib-two"><section>
        <div class="ib-panel"><h3>Draft</h3>
          <div class="ib-status">${chip(d.status === 'SENT_VERIFIED' ? 'Sent · verified' : d.status, d.status === 'APPROVED' ? 'hi' : '')}${chip('Version ' + d.version, '')}${chip('Origin: ' + (d.versions && d.versions[0] ? d.versions[0].origin : 'unknown'), '')}</div>
          ${editable ? `<form data-ib-form="draft-save" data-id="${E(d.id)}">
            <div class="ib-field"><label for="ibDraftTo">Recipient</label><input id="ibDraftTo" name="recipient" value="${E(d.recipient)}"></div>
            <div class="ib-field"><label for="ibDraftCc">Cc (comma-separated)</label><input id="ibDraftCc" name="cc" value="${E(d.cc || '')}"></div>
            <div class="ib-field"><label for="ibDraftSubject">Subject</label><input id="ibDraftSubject" name="subject" value="${E(d.subject)}"></div>
            <div class="ib-field dictate"><label for="ibDraftBody">Reply body (type, paste or dictate)</label><textarea id="ibDraftBody" name="body" spellcheck="true" rows="9">${E(d.body)}</textarea>
              <p class="ib-hint">Dictation: click in this box, then use your dictation tool. The words you dictate are saved as your text (origin DICTATED). Editing invalidates any earlier approval.</p></div>
            <div class="ib-actions"><button class="btn btn-primary" type="submit">Save changes</button>
              <button class="btn" type="button" data-ib="regenerate" data-thread="${E(d.thread_id)}">Regenerate from the thread</button>
              <button class="btn" type="button" data-ib="approve" data-id="${E(d.id)}" data-digest="${E(d.digest)}">Review and approve this version</button>
              <button class="btn btn-danger" type="button" data-ib="reject" data-id="${E(d.id)}">Reject / discard</button></div></form>`
            : `<p class="muted">This draft is ${E(d.status.toLowerCase().replace(/_/g, ' '))} and can no longer be edited.</p><pre>${E(d.body)}</pre>`}
        </div>
        ${approvalBox}
        <div class="ib-panel"><h3>Execution timeline</h3>${execs ? `<ul class="ib-timeline">${execs}</ul>` : '<p class="muted">Nothing has been dispatched for this draft.</p>'}</div>
        </section><aside>
        <div class="ib-panel"><h3>Grounding</h3><p class="ib-hint">Every claim below comes from the thread or from facts you supplied. Nothing else is stated.</p>
          <h4>Sources</h4>${sources ? `<ul>${sources}</ul>` : '<p class="muted">No request in the thread needed a source statement.</p>'}
          <h4>Missing information</h4>${missing ? `<ul>${missing}</ul>` : '<p class="muted">Nothing missing.</p>'}
          <h4>Assumptions</h4>${assumptions ? `<ul>${assumptions}</ul>` : '<p class="muted">None.</p>'}
          <h4>Potential new commitments</h4>${pcs ? `<ul>${pcs}</ul>` : '<p class="muted">None introduced by this reply.</p>'}
          <p class="ib-hint">Engine: ${E(meta.engine || 'RULES_ONLY')} — deterministic rules, no model inference.</p></div>
        <div class="ib-panel"><h3>Versions</h3><ul>${versions}</ul></div>
        <div class="ib-panel"><h3>Actions</h3>
          ${editable ? `<div class="ib-field"><label for="ibApprovalOp">Operation</label><select id="ibApprovalOp"><option value="SEND_EMAIL">Send this message</option>${IB.mode === 'LIVE' ? '<option value="CREATE_GMAIL_DRAFT">Save as a Gmail draft</option>' : ''}</select></div>
            <div class="ib-actions"><button class="btn btn-primary" type="button" data-ib="approve-selected" data-id="${E(d.id)}" data-digest="${E(d.digest)}">Approve exact version</button></div>
            <p class="ib-hint">Approval expires after 15 minutes, is single-use, and is refused if the text changes.</p>` : ''}
          ${IB.mode === 'LIVE' ? '<p class="ib-hint">Gmail drafting and sending need extra permissions. Enable them in Connections if the buttons say so.</p>' : ''}
        </div></aside></div>`;
      if (approval) {
        const ap = $('#ibSendResult');
        if (ap && approval.result) ap.innerHTML = approval.result;
      }
    } catch (e) { el.innerHTML = banner() + errorBox(e); }
  }

  /* ------------------------------------------------------------ Activity */
  async function renderActivity() {
    tools('');
    const el = body('activity');
    el.innerHTML = loading('activity');
    try {
      const d = await call('GET', '/api/inbox/activity?limit=100');
      const rows = d.events.map((e) => `<li><time>${E(when(e.at))}</time> <strong>${E(e.event)}</strong> <span class="muted">${E(e.action)}${e.subject_id ? ' · ' + E(e.subject_id) : ''}</span></li>`).join('');
      el.innerHTML = `<p class="ib-hint">${E(d.note)}</p>` + (rows ? `<ul class="ib-timeline">${rows}</ul>` : empty('No activity yet', 'Sync, approvals and corrections appear here as they happen.'));
    } catch (e) { el.innerHTML = errorBox(e); }
  }

  /* ------------------------------------------------------------ Voice rules */
  const EXAMPLE = 'Prioritize emails from my project collaborators. Show actual deadlines above ordinary questions. Track all promises I make. Show me what other people owe me. Never send an email without my approval. Use a friendly, concise tone.';
  async function renderRules() {
    tools('');
    const el = body('rules');
    el.innerHTML = `<section class="ib-panel"><h3>1–7 · How to use this page</h3>
      <ol class="ib-steps"><li>Focus the instruction field below.</li><li>Dictate with Wispr Flow (or type).</li><li>Review the resulting text.</li><li>Click Parse instructions.</li><li>Inspect the structured rule preview.</li><li>Correct anything inaccurate.</li><li>Activate only the rules you approve.</li></ol>
      <p class="ib-hint">RESOLVE has no Wispr Flow integration. Wispr Flow types into this text box like any keyboard. RESOLVE receives only the text.</p>
      <form data-ib-form="rules-parse"><div class="ib-field dictate"><label for="ibRulesText">Instructions</label><textarea id="ibRulesText" name="text" spellcheck="true" rows="6" placeholder="Tell RESOLVE how you want your inbox handled."></textarea></div>
        <div class="ib-actions"><button class="btn btn-primary" type="submit">Parse instructions</button><button class="btn" type="button" data-ib="example">Insert the example instruction</button></div></form>
      <div id="ibRulesPreview" aria-live="polite"></div></section>
      <section class="ib-panel"><h3>Active rules</h3><div id="ibRulesActive">${loading('rules')}</div></section>
      <section class="ib-panel"><h3>Corrections you saved</h3><div id="ibRulesCorr">${loading('corrections')}</div></section>`;
    await renderRulesLists();
  }
  async function renderRulesLists() {
    try {
      const d = await call('GET', '/api/inbox/rules');
      const active = d.rules.filter((r) => r.active);
      $('#ibRulesActive').innerHTML = active.length ? active.map((r) => `<div class="ib-rule"><h4>${E(r.parsed.type)} · ${E(r.scope)}</h4><p>${E(r.instruction)}</p>
          <div class="ib-actions"><button class="btn btn-small" type="button" data-ib="rule-reverse" data-id="${E(r.id)}">Deactivate</button><span class="ib-meta">version ${E(r.version)}</span></div></div>`).join('')
        : empty('No active rules', 'Parse an instruction and activate only what you approve. Until then, RESOLVE uses its default behaviour.');
      $('#ibRulesCorr').innerHTML = d.corrections.length ? d.corrections.map((c) => `<div class="ib-rule"><h4>${E(c.scope)} · ${c.active ? 'active' : 'reversed'}</h4>
          <p><strong>Before:</strong> ${E(c.original.category)} / ${E(c.original.priority)} &nbsp; <strong>After:</strong> ${E(JSON.stringify(c.corrected))}</p><p class="muted">${E(c.explanation || '')}</p></div>`).join('')
        : '<p class="muted">No corrections saved.</p>';
    } catch (e) { $('#ibRulesActive').innerHTML = errorBox(e); }
  }

  /* ------------------------------------------------------------ Connections */
  async function renderConnections() {
    tools('');
    const el = body('connections');
    el.innerHTML = loading('connection status');
    try {
      const c = await call('GET', '/api/inbox/connections');
      const g = c.gmail;
      const stateCls = { CONNECTED: 'ok', NOT_CONFIGURED: 'warn', NOT_CONNECTED: 'warn', REVOKED: 'bad', EXPIRED: 'bad', RATE_LIMITED: 'warn', ERROR: 'bad' }[g.status] || '';
      const required = (c.required_env || []).map((v) => `<li><code>${E(v)}</code></li>`).join('');
      const scopes = (g.scopes || []).map((s) => `<li><code>${E(s.replace('https://www.googleapis.com/auth/', ''))}</code></li>`).join('');
      el.innerHTML = `<div class="ib-two"><section>
        <div class="ib-panel"><h3>Gmail</h3><div class="ib-status">${chip(g.status === 'NOT_CONFIGURED' ? 'GMAIL NOT CONFIGURED' : g.status, stateCls)}${g.account ? chip(g.account, '') : ''}</div>
          <p>${E(g.detail || '')}</p>
          ${g.status === 'NOT_CONFIGURED' ? `<div class="ib-banner ib-warn"><strong>Live Gmail is not configured on this server.</strong> RESOLVE shows no mailbox until the operator sets these variables and restarts the server:<ul class="ib-steps">${required}</ul>
            The redirect URI must match the one registered with Google. Replay mode works without these variables.</div>` : ''}
          <div class="ib-actions">
            ${g.configured && g.status !== 'CONNECTED' ? `<button class="btn btn-primary" type="button" data-ib="gmail-start" data-scopes="read">Connect Gmail (read-only)</button>` : ''}
            ${g.status === 'CONNECTED' ? `<button class="btn" type="button" data-ib="sync-live">Sync now</button>` : ''}
            ${g.status === 'CONNECTED' ? `<button class="btn" type="button" data-ib="gmail-start" data-scopes="read,compose">Enable drafting (gmail.compose)</button>` : ''}
            ${g.status === 'CONNECTED' ? `<button class="btn" type="button" data-ib="gmail-start" data-scopes="read,compose,send">Enable sending (gmail.send)</button>` : ''}
            ${g.status !== 'NOT_CONNECTED' && g.status !== 'NOT_CONFIGURED' ? `<button class="btn btn-danger" type="button" data-ib="gmail-disconnect">Disconnect and remove access</button>` : ''}
          </div>
          <p class="ib-hint">Permissions granted: ${scopes ? '<ul>' + scopes + '</ul>' : 'none yet'}</p></div>
      </section><aside>
        <div class="ib-panel"><h3>Replay mode</h3><p>${E(c.replay.label)}. Replay uses synthetic messages only.</p><a class="btn" href="#replay" data-ib="nav" data-route="#replay">Open the replay lab</a></div>
        <div class="ib-panel"><h3>Sync</h3><p class="muted">Sync reads only the messages in the last 30 days and stores them encrypted. Each run is recorded, including failed ones.</p>
          <div class="ib-actions"><button class="btn btn-primary" type="button" data-ib="sync-live">Sync now</button></div><div id="ibSyncResult" aria-live="polite"></div></div>
      </aside></div>`;
    } catch (e) { el.innerHTML = errorBox(e); }
  }

  /* ------------------------------------------------------------ Settings */
  async function renderSettings() {
    tools('');
    const el = body('settings');
    el.innerHTML = loading('settings');
    try {
      const s = await call('GET', '/api/inbox/settings');
      const zones = (Intl.supportedValuesOf ? Intl.supportedValuesOf('timeZone') : ['UTC', 'Europe/Berlin', 'America/New_York', 'Asia/Kolkata']).slice(0, 600);
      el.innerHTML = `<div class="ib-two"><section>
        <div class="ib-panel"><h3>Timezone and reply defaults</h3>
          ${s.timezone_configured ? '' : '<div class="ib-banner ib-warn">Your timezone is not set. Deadlines are shown in UTC until you choose one.</div>'}
          <form data-ib-form="settings">
            <div class="ib-field"><label for="ibTz">IANA timezone</label><input id="ibTz" name="timezone" list="ibZones" value="${E(s.timezone_configured ? s.timezone : '')}" placeholder="Europe/Berlin" required>
              <datalist id="ibZones">${zones.map((z) => `<option value="${E(z)}">`).join('')}</datalist></div>
            <div class="ib-field"><label for="ibSign">Sign-off</label><input id="ibSign" name="signoff" value="${E(s.signoff)}"></div>
            <div class="ib-field"><label for="ibTone">Tone</label><input id="ibTone" name="tone" value="${E(s.tone)}" placeholder="neutral, friendly, direct"></div>
            <button class="btn btn-primary" type="submit">Save settings</button><span id="ibSettingsMsg" class="muted small" aria-live="polite"></span></form></div>
      </section><aside>
        <div class="ib-panel"><h3>Your data</h3><p class="muted">Export everything RESOLVE stores for you, including synchronized mail, commitments, drafts and the activity log. Tokens are never included.</p>
          <div class="ib-actions"><button class="btn" type="button" data-ib="export">Export my data (JSON)</button></div></div>
        <div class="ib-panel"><h3>Delete this workspace</h3><p class="muted">Removes live and replay inbox data, drafts, commitments and documents in this workspace. Use Privacy &amp; data in Advanced tools for the full screen.</p>
          <div class="ib-actions"><button class="btn btn-danger" type="button" data-ib="delete-ws">Delete workspace data…</button></div></div>
      </aside></div>`;
    } catch (e) { el.innerHTML = errorBox(e); }
  }

  /* ------------------------------------------------------------ Replay lab */
  async function renderReplay() {
    tools('');
    const el = body('replay');
    el.innerHTML = loading('the replay lab');
    try {
      const s = await call('GET', '/api/inbox/replay/scenarios');
      const threads = s.threads.map((t) => `<li class="ib-item"><div class="ib-item-head"><strong>${E(t.subject)}</strong>${chip(t.messages + ' message' + (t.messages === 1 ? '' : 's'), '')}</div><div class="ib-meta">${E(t.purpose)}</div></li>`).join('');
      const log = (s.log || []).map((e) => `<li><strong>${E(e.action)}</strong> · ${E(e.label || '')} · ${E(e.thread || '')}${e.result ? ' · ' + E(JSON.stringify(e.result)) : ''}</li>`).join('');
      el.innerHTML = `<div class="ib-banner ib-replay" role="status"><strong>SYNTHETIC REPLAY DATA.</strong> Replay messages use the reserved test domain replay.example.test and never touch your mailbox. Every simulated send is labelled SIMULATED.</div>
        <div class="ib-two"><section>
          <div class="ib-panel"><h3>Playback</h3>
            <div class="ib-status">${chip('State: ' + s.state, s.state === 'RUNNING' ? 'ok' : s.state === 'COMPLETE' ? 'hi' : '')}${chip('Messages shown: ' + s.visible + ' of ' + s.messages, '')}${chip('Steps run: ' + (s.log || []).length + ' of ' + s.steps_total, '')}</div>
            <div class="ib-actions">
              <button class="btn btn-primary" type="button" data-ib="replay" data-action="start">Start</button>
              <button class="btn" type="button" data-ib="replay" data-action="step">Step</button>
              <button class="btn" type="button" data-ib="replay" data-action="pause">Pause</button>
              <button class="btn" type="button" data-ib="replay" data-action="resume">Resume</button>
              <button class="btn" type="button" data-ib="replay" data-action="replay">Replay from the beginning</button>
              <button class="btn btn-danger" type="button" data-ib="replay" data-action="reset">Reset replay data</button></div>
            <label class="ib-row"><input type="checkbox" id="ibInject"> Inject a failure: the next simulated send is stored but its acknowledgement times out</label>
            <p class="ib-hint">A timed-out send becomes UNCERTAIN, is never shown as sent, and is reconciled read-only in Activity or Drafts.</p></div>
          <div class="ib-panel"><h3>Processing log</h3>${log ? `<ul class="ib-log" aria-live="polite">${log}</ul>` : '<p class="muted">Start the replay to see each step as RESOLVE processes it.</p>'}</div>
        </section><aside>
          <div class="ib-panel"><h3>Synthetic conversations</h3><ul class="ib-list">${threads}</ul></div>
          <div class="ib-panel"><h3>Open the results</h3><p class="ib-actions"><a class="btn btn-small" href="#morning" data-ib="nav" data-route="#morning">Morning brief</a><a class="btn btn-small" href="#inbox" data-ib="nav" data-route="#inbox">Inbox</a><a class="btn btn-small" href="#commitments" data-ib="nav" data-route="#commitments">Commitments</a><a class="btn btn-small" href="#drafts" data-ib="nav" data-route="#drafts">Drafts</a></p></div>
        </aside></div>`;
    } catch (e) { el.innerHTML = errorBox(e); }
  }

  /* ------------------------------------------------------------ actions (one delegated handler) */
  async function action(kind, el) {
    const id = el.dataset.id;
    const flash = (msg, bad) => { const r = $('#ibFlash') || document.body.appendChild(Object.assign(document.createElement('div'), { id: 'ibFlash', className: 'ib-banner', role: 'status' })); r.textContent = msg; r.className = 'ib-banner' + (bad ? ' ib-warn' : ''); setTimeout(() => { if (r.isConnected) r.remove(); }, 6000); };
    try {
      if (kind === 'nav') { go(el.dataset.route); return; }
      if (kind === 'mode') { IB.mode = el.dataset.value; try { sessionStorage.setItem('resolve_inbox_mode', IB.mode); } catch (_) {} await loadSettings(); return rerender(); }
      if (kind === 'kpi') { if (el.dataset.filter === 'overdue') IB.cfilter = 'overdue'; else if (el.dataset.filter === 'mine') IB.cfilter = 'mine'; else if (el.dataset.filter === 'waiting') IB.cfilter = 'waiting'; else if (el.dataset.filter) { IB.filter = el.dataset.filter; } go(el.dataset.route); return; }
      if (kind === 'filter') { IB.filter = el.dataset.value; IB.page = 1; return renderInbox(); }
      if (kind === 'cfilter') { IB.cfilter = el.dataset.value; return renderCommitments(); }
      if (kind === 'page') { IB.page = Math.max(1, IB.page + Number(el.dataset.delta)); return renderInbox(); }
      if (kind === 'sync' || kind === 'sync-live') {
        const mode = kind === 'sync' ? IB.mode : 'LIVE';
        el.disabled = true; el.textContent = 'Syncing…';
        try {
          const r = await call('POST', '/api/inbox/sync', { mode });
          flash(`Synchronized ${r.fetched} messages in ${r.threads} conversations; ${r.new_commitments} new commitments.`);
        } catch (e) { flash(e.message, true); }
        return rerender();
      }
      if (kind === 'thread-state') {
        const reason = prompt(el.dataset.state === 'DISMISSED' ? 'Why are you dismissing this conversation? (required)' : 'Why defer it? (required)');
        if (!reason) return;
        await call('POST', `/api/inbox/threads/${encodeURIComponent(el.dataset.thread)}/state?` + q(), { state: el.dataset.state, reason });
        flash(el.dataset.state === 'DISMISSED' ? 'Dismissed. The messages are unchanged.' : 'Deferred. It returns when you next review it.');
        return rerender();
      }
      if (kind === 'new-draft') {
        const facts = prompt('Facts you can confirm for the reply (optional). Example: "status: I will send it today".', '');
        const body = { facts: {} };
        if (facts && facts.includes(':')) { const [k, ...v] = facts.split(':'); body.facts[k.trim().slice(0, 30) || 'status'] = v.join(':').trim(); }
        const d = await call('POST', `/api/inbox/threads/${encodeURIComponent(el.dataset.thread)}/drafts?` + q(), body);
        IB.draftId = d.id;
        flash('Draft prepared from the thread. Review it before anything is sent.');
        go('#draft:' + d.id);
        return;
      }
      if (kind === 'reject') {
        const reason = prompt('Why reject this draft? (optional)', '') || '';
        await call('POST', `/api/inbox/drafts/${encodeURIComponent(id)}/reject?` + q(), { reason });
        flash('Draft rejected. Nothing was sent.');
        return rerender();
      }
      if (kind === 'regenerate') {
        const d = await call('POST', `/api/inbox/threads/${encodeURIComponent(el.dataset.thread)}/drafts?` + q(), {});
        IB.draftId = d.id; IB.approval = null;
        flash('A new draft was generated. The previous draft is kept.');
        go('#draft:' + d.id);
        return;
      }
      if (kind === 'approve' || kind === 'approve-selected') {
        const op = kind === 'approve-selected' ? ($('#ibApprovalOp') || {}).value || 'SEND_EMAIL' : 'SEND_EMAIL';
        const ap = await call('POST', `/api/inbox/drafts/${encodeURIComponent(id)}/approve?` + q(), { digest: el.dataset.digest, operation: op });
        IB.approval = { id: ap.id, draft_id: id, expires_at: ap.expires_at, operation: op };
        return renderDraft();
      }
      if (kind === 'cancel-approval') { IB.approval = null; return renderDraft(); }
      if (kind === 'gmail-draft' || kind === 'send') {
        const path = kind === 'send' ? 'send' : 'gmail-draft';
        const res = await call('POST', `/api/inbox/drafts/${encodeURIComponent(IB.draftId)}/${path}?` + q(), { approval_id: el.dataset.approval, digest: el.dataset.digest });
        const label = res.label || (IB.mode === 'REPLAY' ? 'SIMULATED' : 'Gmail');
        const verdict = res.state === 'VERIFIED' ? `Verified (${label}): the stored message was read back and matches the approved text.`
          : res.state === 'UNCERTAIN' ? `UNCERTAIN (${res.reason || 'unknown'}). This is not a success. Reconcile it read-only from Activity; nothing has been resent.`
          : res.state === 'NEEDS_RECOVERY' ? 'Could not confirm the earlier attempt. Nothing was resent. Review the draft again to approve a new attempt.'
          : `${res.state}${res.reason ? ' (' + res.reason + ')' : ''}`;
        if (IB.approval) IB.approval.result = `<p class="${res.state === 'VERIFIED' ? '' : 'ib-error'}" role="status">${E(verdict)}</p>`;
        IB.approval = null;
        return renderDraft().then(() => { const r = $('#ibSendResult'); if (r) r.innerHTML = `<p role="status">${E(verdict)}</p>`; });
      }
      if (kind === 'status') {
        const reason = ($('#ibReason') || {}).value || '';
        await call('PATCH', `/api/inbox/commitments/${encodeURIComponent(id)}?` + q(), { status: el.dataset.status, reason });
        flash('Status changed.');
        return renderCommitment();
      }
      if (kind === 'follow-up') {
        await call('POST', `/api/inbox/commitments/${encodeURIComponent(id)}/follow-up?` + q(), { trigger: el.dataset.trigger });
        flash('Follow-up recorded. Nothing was sent to anyone.');
        return rerender();
      }
      if (kind === 'example') { const t = $('#ibRulesText'); if (t) t.value = EXAMPLE; return; }
      if (kind === 'rule-reverse') { await call('POST', `/api/inbox/rules/${encodeURIComponent(id)}/reverse`); flash('Rule deactivated.'); return renderRulesLists(); }
      if (kind === 'replay') {
        const a = el.dataset.action;
        if (a === 'reset') { if (!confirm('Remove the synthetic replay data? Live data is not touched.')) return; await call('POST', '/api/inbox/replay/reset'); flash('Replay data removed.'); return renderReplay(); }
        if (a === 'start') { await call('POST', '/api/inbox/replay/start'); return renderReplay(); }
        if (a === 'replay') { await call('POST', '/api/inbox/replay/replay'); return renderReplay(); }
        if (a === 'pause' || a === 'resume') { await call('POST', `/api/inbox/replay/${a}`); return renderReplay(); }
        if (a === 'step') {
          const inject = ($('#ibInject') || {}).checked ? 'send_timeout' : undefined;
          const r = await call('POST', '/api/inbox/replay/step', inject ? { inject } : {});
          flash(r.entry ? `${r.entry.action} · ${r.entry.label}` : r.note || 'Step complete.');
          return renderReplay();
        }
      }
      if (kind === 'gmail-start') {
        const scopes = el.dataset.scopes.split(',');
        const r = await call('POST', '/api/inbox/connections/gmail/start', { scopes });
        location.href = r.authorization_url;
        return;
      }
      if (kind === 'gmail-disconnect') {
        if (!confirm('Disconnect Gmail and remove RESOLVE\'s access? Stored messages stay until you delete them.')) return;
        const r = await call('DELETE', '/api/inbox/connections/gmail');
        flash(r.revoked_at_provider ? 'Disconnected and revoked at Google.' : 'Disconnected locally. Google did not confirm revocation; revoke it in your Google account.', !r.revoked_at_provider);
        return renderConnections();
      }
      if (kind === 'export') {
        const r = await fetch('/api/privacy/export', { headers: await authHeaders({ 'Content-Type': 'application/json' }), method: 'POST', body: '{}' });
        if (!r.ok) { flash('Export failed. Try again.', true); return; }
        const blob = new Blob([await r.text()], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = Object.assign(document.createElement('a'), { href: url, download: 'resolve-export.json' });
        document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
        return;
      }
      if (kind === 'delete-ws') {
        if (!confirm('Delete this workspace? This removes live and replay inbox data, drafts, commitments, documents and sandboxes in this workspace. It cannot be undone.')) return;
        if (!confirm('Second confirmation: delete everything in this workspace now?')) return;
        const receipt = await call('POST', '/api/privacy/delete-workspace', {});
        flash('Workspace data deleted. Receipt ' + (receipt.receipt_id || '') + '.');
        return renderSettings();
      }
      if (kind === 'anchor') { const t = document.getElementById('msg-' + el.dataset.id); if (t) { ev_scroll(t); } return; }
    } catch (e) {
      flash(e.message || 'Request failed', true);
    }
  }
  function ev_scroll(node) { node.scrollIntoView({ block: 'center', behavior: 'auto' }); node.focus({ preventScroll: true }); }

  async function formSubmit(form) {
    const kind = form.dataset.ibForm;
    const data = Object.fromEntries(new FormData(form).entries());
    if (kind === 'search') { IB.q = (data.q || '').trim().slice(0, 200); IB.sort = data.sort || 'date'; IB.since = Number(data.since || 0); IB.page = 1; return renderInbox(); }
    if (kind === 'correct') {
      const out = $('#ibCorrResult');
      const body = { kind: data.kind, scope: data.scope, value: data.value || '', dictated_text: data.dictated_text || '', confirmed: !!form.querySelector('[name="confirmed"]').checked };
      try {
        const r = await call('POST', `/api/inbox/threads/${encodeURIComponent(form.dataset.thread)}/corrections?` + q(), body);
        out.innerHTML = `<div class="ib-diff" role="status">
          <div><b>Before</b>${E(r.before.category)} · ${E(r.before.priority)}</div>
          <div><b>Your correction</b>${E(r.user_correction)}</div>
          <div><b>After</b>${E(r.after.category)} · ${E(r.after.priority)}</div>
          <div><b>Impact</b>${E(r.impact)}</div></div>
          ${(r.effects || []).length ? '<ul>' + r.effects.map((x) => '<li>' + E(x) + '</li>').join('') + '</ul>' : ''}
          <p class="ib-hint">Saved as a correction; the original decision is kept in history. Reverse it any time from Voice rules.</p>`;
        return renderThread();
      } catch (e) { out.innerHTML = `<div class="ib-error" role="alert">${E(e.message)}</div>`; }
      return;
    }
    if (kind === 'rules-parse') {
      const out = $('#ibRulesPreview');
      try {
        const r = await call('POST', '/api/inbox/rules/parse', { text: data.text || '' });
        const cards = r.parsed.map((p, i) => `<div class="ib-rule ${p.type === 'UNKNOWN' ? 'unknown' : ''}"><h4>${E(p.type)} · scope ${E(p.scope)}${p.value ? ' · value ' + E(p.value) : ''}${p.priority ? ' · ' + E(p.priority) : ''}</h4>
          <p><strong>Your words:</strong> ${E(p.instruction)}</p><p class="muted">${E(p.explanation || 'Not recognised. This will not be applied.')}</p>
          <p class="ib-hint">${p.type === 'UNKNOWN' ? 'Unknown: never activated.' : (['APPROVAL_REQUIRED_TO_SEND', 'CAPTURE_COMMITMENTS', 'NO_DRAFT'].includes(p.type) ? 'High impact: needs explicit confirmation.' : 'Requires your confirmation to activate.')}</p>
          <label class="ib-row"><input type="checkbox" class="ibRuleOk" data-i="${i}" ${p.type === 'UNKNOWN' ? 'disabled' : ''}> Activate this rule</label></div>`).join('');
        out.innerHTML = `<h3>Preview</h3>${r.conflicts.length ? `<div class="ib-banner ib-warn">Conflicts: ${E(r.conflicts.join(' · '))}</div>` : ''}${cards}
          <div class="ib-actions"><button class="btn btn-primary" type="button" data-ib="rules-activate" data-text="${E(data.text || '')}">Activate the checked rules</button></div>
          <p class="ib-hint">Nothing is active until you check a rule and activate it. Unknown instructions stay inactive.</p>`;
        out.querySelectorAll('[data-ib="rules-activate"]').forEach((b) => b.addEventListener('click', async () => {
          const high = r.parsed.some((p) => ['APPROVAL_REQUIRED_TO_SEND', 'CAPTURE_COMMITMENTS', 'NO_DRAFT'].includes(p.type));
          const checked = Array.from(out.querySelectorAll('.ibRuleOk:checked')).length;
          if (!checked) { out.insertAdjacentHTML('beforeend', '<p class="ib-error">Check at least one rule to activate.</p>'); return; }
          const hi = high && confirm('One or more checked rules is high impact (for example, every send needs approval). Activate them?');
          try {
            const res = await call('POST', '/api/inbox/rules', { text: data.text || '', confirm: true, confirm_high_impact: hi || !high });
            out.insertAdjacentHTML('beforeend', `<p role="status">${res.activated ? 'Activated.' : 'Stored. Unknown instructions were not activated.'}</p>`);
            renderRulesLists();
          } catch (e) { out.insertAdjacentHTML('beforeend', `<p class="ib-error" role="alert">${E(e.message)}</p>`); }
        }));
      } catch (e) { out.innerHTML = `<div class="ib-error" role="alert">${E(e.message)}</div>`; }
      return;
    }
    if (kind === 'resolve-verified') { await call('POST', `/api/inbox/commitments/${encodeURIComponent(form.dataset.id)}/resolve?` + q(), { evidence_execution_id: data.evidence }); return renderCommitment(); }
    if (kind === 'confirm') { await call('POST', `/api/inbox/commitments/${encodeURIComponent(form.dataset.id)}/resolve?` + q(), { confirm: true, reason: data.reason }); return renderCommitment(); }
    if (kind === 'edit') {
      const body = {};
      if (data.required_action) body.required_action = data.required_action;
      if (data.responsible_person) body.responsible_person = data.responsible_person;
      if (data.deadline_local) body.deadline_local = data.deadline_local;
      if (data.reason) body.reason = data.reason;
      await call('PATCH', `/api/inbox/commitments/${encodeURIComponent(form.dataset.id)}?` + q(), body);
      return renderCommitment();
    }
    if (kind === 'note') { await call('POST', `/api/inbox/commitments/${encodeURIComponent(form.dataset.id)}/notes?` + q(), { note: data.note }); return renderCommitment(); }
    if (kind === 'follow-up') { await call('POST', `/api/inbox/commitments/${encodeURIComponent(form.dataset.id)}/follow-up?` + q(), { trigger: data.trigger, target: data.target || '' }); return renderCommitment(); }
    if (kind === 'status-reason') return;
    if (kind === 'draft-save') {
      const body = { recipient: data.recipient, cc: data.cc, subject: data.subject, body: data.body, origin: 'USER' };
      await call('PATCH', `/api/inbox/drafts/${encodeURIComponent(form.dataset.id)}?` + q(), body);
      IB.approval = null;
      return renderDraft();
    }
    if (kind === 'settings') {
      const msg = $('#ibSettingsMsg');
      try {
        await call('PUT', '/api/inbox/settings', { timezone: data.timezone, signoff: data.signoff, tone: data.tone });
        await loadSettings();
        if (msg) msg.textContent = 'Saved.';
      } catch (e) { if (msg) msg.textContent = e.message; }
      return;
    }
    if (kind === 'rules-activate') return;
  }

  function rerender() {
    const v = (location.hash.slice(1) || 'morning').split(':')[0];
    return renderView(v);
  }
  function renderView(v) {
    const map = { morning: renderMorning, inbox: renderInbox, thread: renderThread, commitments: renderCommitments, commitment: renderCommitment,
                  drafts: renderDrafts, draft: renderDraft, activity: renderActivity, rules: renderRules, connections: renderConnections,
                  settings: renderSettings, replay: renderReplay };
    if (!map[v]) return;
    return map[v]();
  }

  /* routing: hash deep links carry an id (#thread:<id>, #commitment:<id>, #draft:<id>) */
  function applyHash() {
    const raw = location.hash.slice(1);
    const [name, id] = raw.split(':');
    if (name === 'thread') IB.threadId = id || IB.threadId;
    if (name === 'commitment') IB.commitmentId = id || IB.commitmentId;
    if (name === 'draft') IB.draftId = id || IB.draftId;
  }
  document.addEventListener('click', (ev) => {
    const el = ev.target.closest('[data-ib]');
    if (!el || el.tagName === 'FORM') return;
    if (el.tagName === 'A' && el.dataset.ib === 'nav') {
      ev.preventDefault();
      const route = el.dataset.route || '';
      if (location.hash === route) { applyHash(); renderView(route.slice(1).split(':')[0]); return; }   // same route: no hashchange will fire
      location.hash = route;                                                                              // app.js routes it and dispatches resolve:view
      return;
    }
    if (el.tagName === 'A' && el.dataset.ib === 'anchor') { ev.preventDefault(); action('anchor', el); return; }
    ev.preventDefault();
    action(el.dataset.ib, el);
  });
  document.addEventListener('submit', (ev) => {
    const form = ev.target.closest('form[data-ib-form]');
    if (!form) return;
    ev.preventDefault();
    formSubmit(form).catch((e) => { const out = document.getElementById('ibFlash') || document.body; out.insertAdjacentHTML('beforeend', `<div class="ib-error" role="alert">${E(e.message)}</div>`); });
  });
  document.addEventListener('resolve:view', (ev) => {
    const name = String(ev.detail || '').split(':')[0];
    applyHash();
    if (!['morning', 'inbox', 'thread', 'commitments', 'commitment', 'drafts', 'draft', 'activity', 'rules', 'connections', 'settings', 'replay'].includes(name)) return;
    loadSettings().then(() => renderView(name));
  });
  window.addEventListener('hashchange', () => { applyHash(); });
  applyHash();
  // app.js dispatches resolve:view before this file loads, so the first screen is rendered here
  const first = (location.hash.slice(1) || 'morning').split(':')[0];
  if (['morning', 'inbox', 'thread', 'commitments', 'commitment', 'drafts', 'draft', 'activity', 'rules', 'connections', 'settings', 'replay'].includes(first)) {
    loadSettings().then(() => renderView(first));
  }
})();
