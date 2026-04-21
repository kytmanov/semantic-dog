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

function fmtDateTime(iso) {
  if (!iso) return null;
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return iso;
  return dt.toLocaleString();
}

function fmtRelativeTime(iso) {
  if (!iso) return null;
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return null;

  const deltaS = Math.round((dt.getTime() - Date.now()) / 1000);
  const absS = Math.abs(deltaS);
  if (absS < 5) return deltaS >= 0 ? 'in a moment' : 'just now';

  const affix = (value) => deltaS >= 0 ? `in ${value}` : `${value} ago`;
  if (absS < 60) return affix(`${absS}s`);

  const m = Math.floor(absS / 60);
  if (m < 60) return affix(`${m}m`);

  const h = Math.floor(m / 60);
  if (h < 24) return affix(`${h}h`);

  return affix(`${Math.floor(h / 24)}d`);
}

function fmtNextRun(iso) {
  if (!iso) return 'Scheduler enabled';
  return `Next scan ${fmtDateTime(iso)}`;
}

const SCHEDULE_PRESETS = [
  {
    value: '',
    description: 'Automatic scans are disabled. Use Run Scan manually when needed.',
  },
  {
    value: '0 * * * *',
    description: 'Runs at the start of every hour.',
  },
  {
    value: '0 */6 * * *',
    description: 'Runs every 6 hours at minute 00.',
  },
  {
    value: '0 2 * * *',
    description: 'Runs every day at 2:00 AM.',
  },
  {
    value: '0 2 * * 0',
    description: 'Runs every Sunday at 2:00 AM.',
  },
];

function scheduleDescription(value) {
  const cron = String(value || '').trim();
  const preset = SCHEDULE_PRESETS.find((item) => item.value === cron);
  if (preset) return preset.description;
  if (!cron) return SCHEDULE_PRESETS[0].description;
  return 'Custom cron schedule. Uses 5 fields: minute hour day-of-month month day-of-week.';
}

function matchingSchedulePreset(value) {
  const cron = String(value || '').trim();
  const preset = SCHEDULE_PRESETS.find((item) => item.value === cron);
  return preset ? preset.value : '__custom__';
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

// ── Fast-poll state ───────────────────────────────────────────
// Activated after triggering a scan so we don't miss fast completions.

let _fastTimer   = null;
let _fastUntil   = 0;
let _scanPending = false; // optimistic: show progress until first confirmed state

function startFastPoll() {
  _fastUntil = Date.now() + 45000; // 45s window
  if (_fastTimer) return;
  _fastTimer = setInterval(async () => {
    await refreshDashboard();
    if (Date.now() >= _fastUntil) stopFastPoll();
  }, 600);
}

function stopFastPoll() {
  if (_fastTimer) { clearInterval(_fastTimer); _fastTimer = null; }
  _scanPending = false;
}

// ── Dashboard ─────────────────────────────────────────────────

function setDotClass(dot, cls) {
  if (!dot) return;
  dot.className = 'status-dot ' + cls;
}

function dashboardBanner(status) {
  const hasBad = (status.by_status?.corrupt ?? 0) > 0 || (status.by_status?.unreadable ?? 0) > 0;
  if (status.status === 'scanning') {
    return {
      state: 'Scan running',
      detail: 'SemanticDog is validating files in the background. You can refresh safely.',
    };
  }
  if (hasBad) {
    return {
      state: 'Issues found',
      detail: 'Corrupt or unreadable files need attention. Check the latest scan details below.',
    };
  }
  if ((status.status === 'idle') && (status.files_indexed ?? 0) > 0) {
    return {
      state: 'Healthy',
      detail: 'No current corruption or access issues are recorded in the indexed library.',
    };
  }
  if (status.status === 'degraded' || status.status === 'error') {
    return {
      state: 'Configuration needed',
      detail: 'The server started in degraded mode. Review setup warnings before scanning.',
    };
  }
  return {
    state: 'Ready to scan',
    detail: 'SemanticDog is configured. Run the first scan to establish a baseline.',
  };
}

function updateScanSection(current, last) {
  const progressSection = document.getElementById('scan-progress-section');
  const idleSection     = document.getElementById('scan-idle-section');
  const runBtn          = document.getElementById('run-scan-btn');
  if (!progressSection) return;

  const scanning = current && ['starting', 'running'].includes(current.state);
  // Show progress while actively scanning OR briefly while optimistic pending
  const showProgress = scanning || _scanPending;

  progressSection.style.display = showProgress ? '' : 'none';
  if (idleSection) idleSection.style.display = showProgress ? 'none' : '';

  if (runBtn) {
    runBtn.disabled    = showProgress;
    runBtn.textContent = showProgress ? 'Scan running…' : 'Run Scan';
  }

  const fill = document.getElementById('progress-fill');

  if (_scanPending && !scanning) {
    // Optimistic state: show indeterminate shimmer while waiting for first snapshot
    if (fill) fill.classList.add('indeterminate');
    const pctEl = document.getElementById('progress-pct');
    const count = document.getElementById('scan-count');
    const rate  = document.getElementById('scan-rate');
    const eta   = document.getElementById('scan-eta');
    if (pctEl) pctEl.textContent = '…';
    if (count) count.textContent = 'discovering files…';
    if (rate)  rate.textContent  = '';
    if (eta)   eta.textContent   = '';
  }

  if (scanning) {
    _scanPending = false; // got real data — no longer optimistic
    if (fill) fill.classList.remove('indeterminate');

    const pct = current.discovered_total > 0
      ? Math.min(100, Math.floor(current.processed / current.discovered_total * 100))
      : 0;

    const pctEl = document.getElementById('progress-pct');
    const count = document.getElementById('scan-count');
    const rate  = document.getElementById('scan-rate');
    const eta   = document.getElementById('scan-eta');

    if (fill)  fill.style.width = pct + '%';
    if (pctEl) pctEl.textContent = pct + '%';
    if (count) count.textContent =
      `${(current.processed || 0).toLocaleString()} / ${(current.discovered_total || 0).toLocaleString()}`;
    if (rate)  rate.textContent  = `${Number(current.files_per_sec || 0).toFixed(1)} files/sec`;
    if (eta)   eta.textContent   = fmtEta(current.eta_s);
  } else if (!showProgress) {
    // Scan finished — extend fast poll a bit to let the next status poll refresh counts,
    // then stop it so we fall back to the slow 5s interval.
    if (_fastTimer) _fastUntil = Math.min(_fastUntil, Date.now() + 2000);
  }

  // Update "files examined" from the last in-memory snapshot only when the current
  // page-load value is 0 (stale DB from a pre-fix scan) and we have live data.
  if (last && last.processed > 0) {
    const el = document.getElementById('last-scan-files-checked');
    if (el && el.textContent.trim() === '0') {
      el.textContent = last.processed.toLocaleString();
    }
  }
}

function updateSchedulerCard(scheduler) {
  const nextScanInfo = document.getElementById('next-scan-info');
  const nextScanRelative = document.getElementById('next-scan-relative');
  const schedulerBadge = document.getElementById('scheduler-badge');
  const schedulerLastRun = document.getElementById('scheduler-last-run');
  const schedulerLastResult = document.getElementById('scheduler-last-result');
  const schedulerCron = document.getElementById('scheduler-cron');
  const schedulerError = document.getElementById('scheduler-error');

  const enabled = Boolean(scheduler?.enabled);
  const hasError = Boolean(scheduler?.last_error);

  if (nextScanInfo) {
    if (hasError) nextScanInfo.textContent = 'Schedule unavailable';
    else if (enabled) nextScanInfo.textContent = fmtNextRun(scheduler?.next_run_at);
    else nextScanInfo.textContent = 'Scheduler disabled';
  }

  if (nextScanRelative) {
    if (hasError) nextScanRelative.textContent = 'Fix the schedule expression in Configuration.';
    else if (enabled && scheduler?.next_run_at) {
      nextScanRelative.textContent = fmtRelativeTime(scheduler.next_run_at) || 'scheduled automatically';
    } else if (enabled) {
      nextScanRelative.textContent = 'scheduled automatically';
    } else {
      nextScanRelative.textContent = 'waiting for a schedule';
    }
  }

  if (schedulerBadge) {
    schedulerBadge.className = hasError
      ? 'badge badge-red'
      : enabled
        ? 'badge badge-indigo'
        : 'badge badge-gray';
    schedulerBadge.textContent = hasError ? 'Error' : enabled ? 'Active' : 'Disabled';
  }

  if (schedulerLastRun) {
    schedulerLastRun.textContent = scheduler?.last_run_at ? fmtDateTime(scheduler.last_run_at) : 'Never';
  }

  if (schedulerLastResult) {
    schedulerLastResult.textContent = scheduler?.last_trigger_result || 'No runs yet';
  }

  if (schedulerCron) {
    schedulerCron.textContent = scheduler?.cron || 'disabled';
  }

  if (schedulerError) {
    if (hasError) {
      schedulerError.textContent = scheduler.last_error;
      schedulerError.style.display = '';
    } else {
      schedulerError.textContent = '';
      schedulerError.style.display = 'none';
    }
  }
}

function initScheduleField() {
  const input = document.getElementById('schedule-input');
  const preset = document.getElementById('schedule-preset');
  const description = document.getElementById('schedule-description');
  if (!input || !preset || !description) return;

  let customMode = false;

  function syncFromValue() {
    const current = String(input.value || '').trim();
    const selected = customMode ? '__custom__' : matchingSchedulePreset(current);
    if (preset.value !== selected) preset.value = selected;
    input.readOnly = !customMode;
    description.textContent = scheduleDescription(current);
  }

  preset.addEventListener('change', () => {
    customMode = preset.value === '__custom__';
    if (!customMode) input.value = preset.value;
    syncFromValue();
  });

  input.addEventListener('input', () => {
    customMode = true;
    syncFromValue();
  });

  customMode = matchingSchedulePreset(String(input.value || '').trim()) === '__custom__';
  syncFromValue();
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
    const bannerState = document.getElementById('banner-state');
    const bannerDetail = document.getElementById('banner-detail');

    if (rtEl) rtEl.textContent = status.status;
    if (fiEl) fiEl.textContent = (status.files_indexed ?? 0).toLocaleString();
    if (okEl) okEl.textContent = (status.files_indexed ?? 0).toLocaleString();
    if (crEl) crEl.textContent = (status.by_status?.corrupt ?? 0).toLocaleString();
    if (urEl) urEl.textContent = (status.by_status?.unreadable ?? 0).toLocaleString();

    const corruptCard    = document.getElementById('corrupt-card');
    const unreadableCard = document.getElementById('unreadable-card');
    if (corruptCard)    corruptCard.className    = (status.by_status?.corrupt    ?? 0) > 0 ? 'card card-danger' : 'card';
    if (unreadableCard) unreadableCard.className = (status.by_status?.unreadable ?? 0) > 0 ? 'card card-warn'   : 'card';

    const s = status.status;
    const hasBad = (status.by_status?.corrupt ?? 0) > 0 || (status.by_status?.unreadable ?? 0) > 0;
    if      (s === 'scanning')                            setDotClass(dot, 'dot-blue');
    else if (hasBad)                                      setDotClass(dot, 'dot-red');
    else if (s === 'idle' && (status.files_indexed ?? 0) > 0) setDotClass(dot, 'dot-green');
    else if (s === 'degraded' || s === 'error')           setDotClass(dot, 'dot-amber');
    else                                                  setDotClass(dot, 'dot-gray');

    const banner = dashboardBanner(status);
    if (bannerState) bannerState.textContent = banner.state;
    if (bannerDetail) bannerDetail.textContent = banner.detail;
    updateSchedulerCard(status.scheduler);
  }

  if (scanState) {
    updateScanSection(scanState.current, scanState.last);
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
      // Show progress section immediately (optimistic) and start fast polling
      // so we don't miss scans that complete within the normal 5s interval.
      _scanPending = true;
      updateScanSection(null, null);
      startFastPoll();
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

  initScheduleField();
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
