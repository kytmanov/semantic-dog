// ── Utilities ────────────────────────────────────────────────

function timeAgo(iso) {
  if (!iso) return null;
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 5)   return 'just now';
  if (s < 60)  return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60)  return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24)  return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function fmtDuration(startIso, endIso) {
  if (!startIso || !endIso) return null;
  const s = Math.floor((new Date(endIso) - new Date(startIso)) / 1000);
  if (s < 0)   return null;
  if (s < 60)  return `${s}s`;
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60)  return r ? `${m}m ${r}s` : `${m}m`;
  const h = Math.floor(m / 60), mr = m % 60;
  return mr ? `${h}h ${mr}m` : `${h}h`;
}

function fmtEta(seconds) {
  if (!seconds || seconds < 1) return '';
  const s = Math.ceil(seconds);
  if (s < 60)  return `${s}s remaining`;
  return `~${Math.ceil(s / 60)}m remaining`;
}

async function fetchJson(path) {
  try {
    const r = await fetch(path, { headers: { Accept: 'application/json' } });
    return r.ok ? r.json() : null;
  } catch { return null; }
}

function toast(msg, type = '') {
  const c = document.getElementById('toast-container');
  if (!c) return;
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  c.appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity 200ms, transform 200ms';
    el.style.opacity = '0';
    el.style.transform = 'translateY(4px)';
    setTimeout(() => el.remove(), 220);
  }, 3500);
}

// ── Relative timestamps ───────────────────────────────────────

function refreshTimestamps() {
  for (const el of document.querySelectorAll('[data-time]')) {
    const rel = timeAgo(el.dataset.time);
    if (rel) el.textContent = rel;
  }
  for (const el of document.querySelectorAll('[data-dur-start]')) {
    const d = fmtDuration(el.dataset.durStart, el.dataset.durEnd);
    el.textContent = d || '—';
  }
}

// ── Dashboard ─────────────────────────────────────────────────

function setDotClass(dot, cls) {
  if (!dot) return;
  dot.className = 'status-dot ' + cls;
}

function updateScanSection(snapshot) {
  const progressSection = document.getElementById('scan-progress-section');
  const idleSection     = document.getElementById('scan-idle-section');
  const runBtn          = document.getElementById('run-scan-btn');
  if (!progressSection) return;

  const active = snapshot && ['starting', 'running'].includes(snapshot.state);

  progressSection.style.display = active ? '' : 'none';
  if (idleSection) idleSection.style.display = active ? 'none' : '';

  if (runBtn) {
    runBtn.disabled = active;
    runBtn.textContent = active ? 'Scan running…' : 'Run Scan';
  }

  if (active) {
    const pct = snapshot.discovered_total > 0
      ? Math.min(100, Math.floor(snapshot.processed / snapshot.discovered_total * 100))
      : 0;

    const fill  = document.getElementById('progress-fill');
    const pctEl = document.getElementById('progress-pct');
    const count = document.getElementById('scan-count');
    const rate  = document.getElementById('scan-rate');
    const eta   = document.getElementById('scan-eta');

    if (fill)  fill.style.width = pct + '%';
    if (pctEl) pctEl.textContent = pct + '%';
    if (count) count.textContent =
      `${(snapshot.processed || 0).toLocaleString()} / ${(snapshot.discovered_total || 0).toLocaleString()}`;
    if (rate)  rate.textContent = `${Number(snapshot.files_per_sec || 0).toFixed(1)} files/sec`;
    if (eta)   eta.textContent  = fmtEta(snapshot.eta_s);
  }
}

async function refreshDashboard() {
  if (!document.getElementById('runtime-status')) return;

  const [status, scanState] = await Promise.all([
    fetchJson('/status'),
    fetchJson('/api/scan/current'),
  ]);

  if (status) {
    const rtEl  = document.getElementById('runtime-status');
    const fiEl  = document.getElementById('files-indexed');
    const okEl  = document.getElementById('count-ok');
    const crEl  = document.getElementById('count-corrupt');
    const urEl  = document.getElementById('count-unreadable');
    const dot   = document.getElementById('status-dot');

    if (rtEl) rtEl.textContent = status.status;
    if (fiEl) fiEl.textContent = (status.files_indexed ?? 0).toLocaleString();
    if (okEl) okEl.textContent = (status.by_status?.ok ?? 0).toLocaleString();
    if (crEl) crEl.textContent = (status.by_status?.corrupt ?? 0).toLocaleString();
    if (urEl) urEl.textContent = (status.by_status?.unreadable ?? 0).toLocaleString();

    // Update stat card highlights
    const corruptCard    = document.getElementById('corrupt-card');
    const unreadableCard = document.getElementById('unreadable-card');
    if (corruptCard) {
      corruptCard.className = (status.by_status?.corrupt ?? 0) > 0 ? 'card card-danger' : 'card';
    }
    if (unreadableCard) {
      unreadableCard.className = (status.by_status?.unreadable ?? 0) > 0 ? 'card card-warn' : 'card';
    }

    // Update status dot
    const s = status.status;
    const hasBadFiles = (status.by_status?.corrupt ?? 0) > 0 || (status.by_status?.unreadable ?? 0) > 0;
    if (s === 'scanning')     setDotClass(dot, 'dot-blue');
    else if (hasBadFiles)     setDotClass(dot, 'dot-red');
    else if (s === 'idle' && (status.files_indexed ?? 0) > 0) setDotClass(dot, 'dot-green');
    else if (s === 'degraded' || s === 'error') setDotClass(dot, 'dot-amber');
    else                      setDotClass(dot, 'dot-gray');
  }

  if (scanState) {
    updateScanSection(scanState.current);
  }
}

// ── Run Scan ──────────────────────────────────────────────────

async function triggerScan() {
  const btn = document.getElementById('run-scan-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Starting…'; }

  try {
    const r    = await fetch('/trigger', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: '{}',
    });
    const body = await r.json();
    if (r.ok) {
      toast('Scan started', 'success');
    } else if (r.status === 409) {
      toast('Scan already running', '');
      if (btn) { btn.disabled = false; btn.textContent = 'Run Scan'; }
    } else if (r.status === 429) {
      toast(`Cooldown active — retry in ${body.retry_after_s}s`, '');
      if (btn) { btn.disabled = false; btn.textContent = 'Run Scan'; }
    } else {
      toast(body.error || 'Failed to start scan', 'error');
      if (btn) { btn.disabled = false; btn.textContent = 'Run Scan'; }
    }
  } catch {
    toast('Network error', 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Run Scan'; }
  }
}

// ── Form submission ───────────────────────────────────────────

function readFormPayload(form) {
  const payload = {};
  for (const [key, value] of new FormData(form).entries()) {
    if (key === 'paths' || key === 'exclude') {
      payload[key] = String(value).split('\n').map(s => s.trim()).filter(Boolean);
      continue;
    }
    if (value === 'true')  { payload[key] = true;  continue; }
    if (value === 'false') { payload[key] = false; continue; }
    if (value !== '' && /^-?\d+$/.test(String(value))) { payload[key] = Number(value); continue; }
    payload[key] = value;
  }
  return payload;
}

async function submitSettingsForm(event) {
  event.preventDefault();
  const form     = event.currentTarget;
  const payload  = readFormPayload(form);
  const feedback = form.querySelector('.form-feedback');

  if (feedback) { feedback.textContent = 'Validating…'; feedback.className = 'form-feedback'; }

  let validation;
  try {
    const r = await fetch(form.dataset.endpoint + '/validate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(payload),
    });
    validation = await r.json();
  } catch {
    if (feedback) { feedback.textContent = 'Network error.'; feedback.className = 'form-feedback err'; }
    return;
  }

  if (!validation.valid) {
    if (feedback) { feedback.textContent = validation.error || 'Validation failed.'; feedback.className = 'form-feedback err'; }
    return;
  }

  const saveRes  = await fetch(form.dataset.endpoint, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(payload),
  });
  const saveBody = await saveRes.json();

  if (!saveRes.ok) {
    if (feedback) { feedback.textContent = saveBody.error || 'Save failed.'; feedback.className = 'form-feedback err'; }
    return;
  }

  const msg = saveBody.restart_required?.length
    ? `Saved. Restart required for: ${saveBody.restart_required.join(', ')}.`
    : 'Saved successfully.';
  if (feedback) { feedback.textContent = msg; feedback.className = 'form-feedback ok'; }
  toast(msg, 'success');
}

// ── Issue filters ─────────────────────────────────────────────

function initIssueFilters() {
  const btns   = document.querySelectorAll('.filter-btn[data-filter]');
  const search = document.getElementById('issue-search');
  if (!btns.length) return;

  let activeFilter = 'all';
  let searchTerm   = '';

  function apply() {
    const rows = document.querySelectorAll('.issue-row');
    let n = 0;
    for (const row of rows) {
      const matchF = activeFilter === 'all' || row.dataset.status === activeFilter;
      const matchS = !searchTerm || (row.dataset.path || '').toLowerCase().includes(searchTerm);
      const show   = matchF && matchS;
      row.style.display = show ? '' : 'none';
      if (show) n++;
    }
    const empty = document.getElementById('issues-empty-filtered');
    if (empty) empty.style.display = n === 0 ? '' : 'none';
  }

  for (const btn of btns) {
    btn.addEventListener('click', () => {
      activeFilter = btn.dataset.filter;
      for (const b of btns) b.classList.toggle('active', b === btn);
      apply();
    });
  }
  if (search) {
    search.addEventListener('input', () => { searchTerm = search.value.toLowerCase(); apply(); });
  }
}

// ── Init ──────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  refreshTimestamps();
  setInterval(refreshTimestamps, 30000);

  refreshDashboard();
  setInterval(refreshDashboard, 5000);

  for (const form of document.querySelectorAll('.settings-form')) {
    form.addEventListener('submit', submitSettingsForm);
  }

  const runBtn = document.getElementById('run-scan-btn');
  if (runBtn) runBtn.addEventListener('click', triggerScan);

  const testBtn = document.getElementById('test-notify-button');
  if (testBtn) {
    testBtn.addEventListener('click', async () => {
      testBtn.disabled = true;
      const r    = await fetch('/api/notify/test', { method: 'POST', headers: { Accept: 'application/json' } });
      const body = await r.json();
      testBtn.disabled = false;
      if (body.errors?.length) {
        toast(`Notification errors: ${body.errors.join(', ')}`, 'error');
      } else {
        toast('Test notification sent', 'success');
      }
    });
  }

  initIssueFilters();
});
