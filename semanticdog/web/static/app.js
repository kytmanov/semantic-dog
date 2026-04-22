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
let _dashboardRefreshSeq = 0;
let _terminalRefreshScheduled = false;

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

function scheduleTerminalDashboardRefresh() {
  if (_terminalRefreshScheduled) return;
  _terminalRefreshScheduled = true;
  setTimeout(() => {
    _terminalRefreshScheduled = false;
    refreshDashboard();
  }, 150);
}

// ── Dashboard ─────────────────────────────────────────────────

function setDotClass(dot, cls) {
  if (!dot) return;
  dot.className = 'status-dot ' + cls;
}

function setHeroClass(state) {
  const hero = document.getElementById('dashboard-hero');
  if (!hero) return;
  hero.classList.remove('is-healthy', 'is-warning', 'is-running', 'is-ready', 'is-degraded');
  hero.classList.add(state);
}

function setNodeChildren(node, children) {
  if (!node) return;
  node.replaceChildren(...children.filter(Boolean));
}

function textNode(value) {
  return document.createTextNode(String(value ?? ''));
}

function buildTimestampSpan({ id, value, attrName = 'data-time', className = '' }) {
  const span = document.createElement('span');
  if (id) span.id = id;
  if (className) span.className = className;
  if (value) span.setAttribute(attrName, value);
  span.textContent = value || '';
  return span;
}

function buildInlineMetric({ label, id, value, attrName, className = '' }) {
  const fragment = document.createDocumentFragment();
  fragment.appendChild(textNode(`${label}: `));
  const span = buildTimestampSpan({ id, value, attrName, className });
  fragment.appendChild(span);
  return fragment;
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

function dashboardHeroCopy(status) {
  const banner = dashboardBanner(status);
  const lastScan = status.last_scan;
  const corrupt = status.by_status?.corrupt ?? 0;
  const unreadable = status.by_status?.unreadable ?? 0;

  if (banner.state === 'Healthy') {
    return {
      heroClass: 'is-healthy',
      title: 'All clear. Your library is healthy.',
      detail: lastScan?.scope
        ? `Last scan completed without issues for ${lastScan.scope}.`
        : 'No current corruption or access issues are recorded in the indexed library.',
    };
  }

  if (banner.state === 'Issues found') {
    return {
      heroClass: 'is-warning',
      title: 'Library Health Warning: Data Compromised.',
      detail: `${corrupt} files are corrupted. ${unreadable} files are unreadable. View details below.`,
    };
  }

  if (banner.state === 'Scan running') {
    return {
      heroClass: 'is-running',
      title: 'Scan in progress. SemanticDog is checking your library.',
      detail: 'Live progress appears below while files are being validated.',
    };
  }

  if (banner.state === 'Configuration needed') {
    return {
      heroClass: 'is-degraded',
      title: 'Configuration attention needed before trusting results.',
      detail: banner.detail,
    };
  }

  if (banner.state === 'Access problem suspected') {
    return {
      heroClass: 'is-degraded',
      title: 'Library access warning. Scan roots may be unavailable.',
      detail: banner.detail,
    };
  }

  return {
    heroClass: 'is-ready',
    title: 'SemanticDog is ready to scan your library.',
    detail: banner.detail,
  };
}

function updateHero(status) {
  const dot = document.getElementById('status-dot');
  const stateEl = document.getElementById('banner-state');
  const titleEl = document.getElementById('dashboard-hero-title');
  const detailEl = document.getElementById('banner-detail');
  const filesEl = document.getElementById('files-indexed');
  const runtimeEl = document.getElementById('runtime-status');
  const copy = dashboardHeroCopy(status);
  const banner = dashboardBanner(status);
  const hasBad = (status.by_status?.corrupt ?? 0) > 0 || (status.by_status?.unreadable ?? 0) > 0;

  if (runtimeEl) runtimeEl.textContent = status.status;
  if (filesEl) filesEl.textContent = (status.files_indexed ?? 0).toLocaleString();
  if (stateEl) stateEl.textContent = banner.state;
  if (titleEl) titleEl.textContent = copy.title;
  if (detailEl) detailEl.textContent = copy.detail;
  setHeroClass(copy.heroClass);

  if (status.status === 'scanning') setDotClass(dot, 'dot-blue');
  else if (hasBad) setDotClass(dot, 'dot-red');
  else if (status.status === 'idle' && (status.files_indexed ?? 0) > 0) setDotClass(dot, 'dot-green');
  else if (status.status === 'degraded' || status.status === 'error') setDotClass(dot, 'dot-amber');
  else setDotClass(dot, 'dot-gray');
}

function updateTimeline(status) {
  const last = status.last_scan;
  const corrupt = status.by_status?.corrupt ?? 0;
  const unreadable = status.by_status?.unreadable ?? 0;
  const issueCount = corrupt + unreadable;

  const lastDot = document.getElementById('timeline-last-dot');
  const lastTitle = document.getElementById('timeline-last-title');
  const lastMeta = document.getElementById('timeline-last-meta');
  const lastSubmeta = document.getElementById('timeline-last-submeta');
  const lastScope = document.getElementById('timeline-last-scope');
  const lastFiles = document.getElementById('last-scan-files-checked');

  if (lastDot) {
    lastDot.classList.remove('is-success', 'is-danger', 'is-info', 'is-muted');
    if (!last) lastDot.classList.add('is-muted');
    else if (issueCount > 0) lastDot.classList.add('is-danger');
    else lastDot.classList.add('is-success');
  }

  if (lastTitle) {
    if (!last) {
      lastTitle.textContent = '1. No scans yet';
    } else if (last.finished_at && issueCount > 0) {
      lastTitle.textContent = '1. Last scan completed with issues';
    } else if (last.finished_at) {
      const strong = document.createElement('strong');
      const when = buildTimestampSpan({ id: 'timeline-last-when', value: last.started_at || '', attrName: 'data-time' });
      strong.appendChild(when);
      setNodeChildren(lastTitle, [textNode('1. Last Scan: '), strong]);
    } else {
      lastTitle.textContent = '1. Last scan incomplete';
    }
  }

  if (lastMeta) {
    if (!last) {
      lastMeta.textContent = 'Run your first scan to establish a baseline.';
    } else if (last.finished_at) {
      const duration = document.createElement('span');
      duration.id = 'timeline-last-duration';
      if (last.started_at) duration.dataset.durStart = last.started_at;
      if (last.finished_at) duration.dataset.durEnd = last.finished_at;
      duration.textContent = last.finished_at || '';

      const rate = document.createElement('span');
      rate.id = 'timeline-last-rate';
      rate.textContent = last.files_per_sec ? `${Number(last.files_per_sec).toFixed(1)} f/s` : '—';

      setNodeChildren(lastMeta, [
        textNode('Duration: '),
        duration,
        textNode('. Scan Speed: '),
        rate,
      ]);
    } else {
      lastMeta.textContent = 'Scan started but did not finish cleanly.';
    }
  }

  if (lastSubmeta) lastSubmeta.style.display = last ? '' : 'none';
  if (lastScope) lastScope.textContent = last?.scope || 'all configured paths';
  if (lastFiles) lastFiles.textContent = Number(last?.total || 0).toLocaleString();

  updateSchedulerCard(status.scheduler);
}

function updateIssuePills(status) {
  const corrupt = status.by_status?.corrupt ?? 0;
  const unreadable = status.by_status?.unreadable ?? 0;
  const corruptPill = document.getElementById('overview-corrupt-pill');
  const unreadablePill = document.getElementById('overview-unreadable-pill');
  const corruptCount = document.getElementById('count-corrupt');
  const unreadableCount = document.getElementById('count-unreadable');

  if (corruptCount) corruptCount.textContent = Number(corrupt).toLocaleString();
  if (unreadableCount) unreadableCount.textContent = Number(unreadable).toLocaleString();
  if (corruptPill) corruptPill.classList.toggle('is-active', corrupt > 0);
  if (unreadablePill) unreadablePill.classList.toggle('is-active', unreadable > 0);
}

function updateScanSection(current, last) {
  const progressSection = document.getElementById('scan-progress-section');
  const idleSection     = document.getElementById('scan-idle-section');
  const runBtn          = document.getElementById('run-scan-btn');
  if (!progressSection) return;

  const scanning = current && ['starting', 'running'].includes(current.state);
  const terminalLast = !scanning && last && ['completed', 'failed', 'interrupted'].includes(last.state);
  if (terminalLast) _scanPending = false;
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
    _terminalRefreshScheduled = false;
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
  } else if (terminalLast) {
    scheduleTerminalDashboardRefresh();
    if (_fastTimer) _fastUntil = Math.max(_fastUntil, Date.now() + 1200);
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
  const nextDot = document.getElementById('timeline-next-dot');

  const enabled = Boolean(scheduler?.enabled);
  const hasError = Boolean(scheduler?.last_error);

  if (nextDot) {
    nextDot.classList.remove('is-success', 'is-danger', 'is-info', 'is-muted');
    if (hasError) nextDot.classList.add('is-danger');
    else if (enabled) nextDot.classList.add('is-info');
    else nextDot.classList.add('is-muted');
  }

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

const OVERVIEW_COLORS = {
  healthy: '#4f8ef7',
  corrupt: '#f8b84a',
  unreadable: '#f06b6b',
  other: '#94a3b8',
};

function polarToCartesian(cx, cy, r, angleDeg) {
  const angle = ((angleDeg - 90) * Math.PI) / 180;
  return {
    x: cx + r * Math.cos(angle),
    y: cy + r * Math.sin(angle),
  };
}

function describeArc(cx, cy, rOuter, rInner, startAngle, endAngle) {
  const startOuter = polarToCartesian(cx, cy, rOuter, endAngle);
  const endOuter = polarToCartesian(cx, cy, rOuter, startAngle);
  const startInner = polarToCartesian(cx, cy, rInner, startAngle);
  const endInner = polarToCartesian(cx, cy, rInner, endAngle);
  const largeArcFlag = endAngle - startAngle > 180 ? 1 : 0;
  return [
    `M ${startOuter.x} ${startOuter.y}`,
    `A ${rOuter} ${rOuter} 0 ${largeArcFlag} 0 ${endOuter.x} ${endOuter.y}`,
    `L ${startInner.x} ${startInner.y}`,
    `A ${rInner} ${rInner} 0 ${largeArcFlag} 1 ${endInner.x} ${endInner.y}`,
    'Z',
  ].join(' ');
}

function renderOverviewChart(data, filesIndexed = 0) {
  const chart = document.getElementById('overview-chart');
  const layout = document.getElementById('overview-chart-layout');
  const empty = document.getElementById('overview-empty');
  const legend = document.getElementById('overview-legend');
  const total = document.getElementById('overview-chart-total');
  const totalText = document.getElementById('overview-total-files');
  const emptyTitle = empty?.querySelector('.empty-title');
  const emptyDesc = empty?.querySelector('.empty-desc');
  if (!chart || !layout || !empty || !legend || !total) return;

  const items = Array.isArray(data) ? data.filter((item) => Number(item?.count || 0) > 0) : [];
  chart.innerHTML = '';
  if (!items.length) {
    layout.style.display = 'none';
    empty.style.display = '';
    legend.replaceChildren();
    total.textContent = Number(filesIndexed || 0).toLocaleString();
    if (totalText) totalText.textContent = Number(filesIndexed || 0).toLocaleString();
    if (emptyTitle) emptyTitle.textContent = 'No indexed files yet';
    if (emptyDesc) emptyDesc.textContent = 'Run a scan to see the health of this library and which file types dominate it.';
    return;
  }

  const totalFiles = items.reduce((sum, item) => sum + Number(item.count || 0), 0);
  if (!totalFiles) return;

  layout.style.display = '';
  empty.style.display = 'none';
  total.textContent = totalFiles.toLocaleString();
  if (totalText) totalText.textContent = totalFiles.toLocaleString();
  legend.replaceChildren();
  const legendFragment = document.createDocumentFragment();
  items.forEach((item) => {
    const percent = totalFiles ? ((Number(item.count || 0) / totalFiles) * 100).toFixed(1) : '0.0';
    const tone = Object.prototype.hasOwnProperty.call(OVERVIEW_COLORS, item?.tone) ? item.tone : 'other';
    const color = OVERVIEW_COLORS[tone] || OVERVIEW_COLORS.other;
    const toneClass = `overview-tone-${tone}`;

    const row = document.createElement('div');
    row.className = `filetype-legend-row overview-legend-row ${toneClass}`;

    const rowInner = document.createElement('div');
    rowInner.className = 'row';
    rowInner.style.gap = '0.625rem';
    rowInner.style.minWidth = '0';

    const swatch = document.createElement('span');
    swatch.className = `filetype-swatch overview-swatch ${toneClass}`;
    swatch.style.background = color;

    const label = document.createElement('span');
    label.className = 'filetype-label';
    label.textContent = String(item?.label ?? '');

    const meta = document.createElement('div');
    meta.className = 'filetype-meta';

    const count = document.createElement('span');
    count.className = 'filetype-count';
    count.textContent = Number(item?.count || 0).toLocaleString();

    const percentEl = document.createElement('span');
    percentEl.className = 'filetype-percent';
    percentEl.textContent = `${percent}%`;

    rowInner.appendChild(swatch);
    rowInner.appendChild(label);
    meta.appendChild(count);
    meta.appendChild(percentEl);
    row.appendChild(rowInner);
    row.appendChild(meta);
    legendFragment.appendChild(row);
  });
  legend.appendChild(legendFragment);

  let startAngle = 0;
  for (let index = 0; index < items.length; index += 1) {
    const item = items[index];
    const tone = Object.prototype.hasOwnProperty.call(OVERVIEW_COLORS, item?.tone) ? item.tone : 'other';
    const angle = (Number(item.count || 0) / totalFiles) * 360;
    const endAngle = startAngle + angle;
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', describeArc(120, 120, 108, 68, startAngle, endAngle));
    path.setAttribute('fill', OVERVIEW_COLORS[tone] || OVERVIEW_COLORS.other);
    path.setAttribute('stroke', '#08101f');
    path.setAttribute('stroke-width', '2');
    const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
    const percent = totalFiles ? ((Number(item.count || 0) / totalFiles) * 100).toFixed(1) : '0.0';
    title.textContent = `${item.label}: ${item.count} files (${percent}%)`;
    path.appendChild(title);
    chart.appendChild(path);
    startAngle = endAngle;
  }
}

function renderSetupDiagnostics(setup) {
  const scanRoots = document.getElementById('setup-scan-roots-list');
  const warnings = document.getElementById('setup-warnings');
  if (!scanRoots || !warnings || !setup) return;

  const roots = Array.isArray(setup.scan_roots) ? setup.scan_roots : [];
  scanRoots.replaceChildren();
  if (!roots.length) {
    const empty = document.createElement('div');
    empty.className = 'text-3';
    empty.style.fontSize = '0.8125rem';
    empty.style.padding = '0.5rem 0';
    empty.textContent = 'No scan roots configured yet.';
    scanRoots.appendChild(empty);
  } else {
    const rootsFragment = document.createDocumentFragment();
    roots.forEach((root) => {
      const allOk = Boolean(root.exists && root.is_dir && root.readable);
      const iconClass = allOk ? 'ok' : root.exists ? 'warn' : 'error';
      const icon = allOk ? '✓' : root.exists ? '!' : '✗';
      let badgeClass = 'badge-green';
      let badgeText = 'accessible';
      if (!root.exists) {
        badgeClass = 'badge-red';
        badgeText = 'not found';
      } else if (!root.is_dir) {
        badgeClass = 'badge-amber';
        badgeText = 'not a directory';
      } else if (!root.readable) {
        badgeClass = 'badge-amber';
        badgeText = 'not readable';
      }
      const row = document.createElement('div');
      row.className = 'diag-row';

      const iconEl = document.createElement('div');
      iconEl.className = `diag-icon ${iconClass}`;
      iconEl.textContent = icon;

      const content = document.createElement('div');

      const name = document.createElement('div');
      name.className = 'diag-name mono';
      name.style.fontSize = '0.8125rem';
      name.textContent = String(root.path || '');

      const badgeRow = document.createElement('div');
      badgeRow.className = 'row';
      badgeRow.style.gap = '0.5rem';
      badgeRow.style.marginTop = '3px';

      const badge = document.createElement('span');
      badge.className = `badge ${badgeClass}`;
      badge.textContent = badgeText;

      badgeRow.appendChild(badge);
      content.appendChild(name);
      content.appendChild(badgeRow);
      row.appendChild(iconEl);
      row.appendChild(content);
      rootsFragment.appendChild(row);
    });
    scanRoots.appendChild(rootsFragment);
  }

  const warningList = Array.isArray(setup.warnings) ? setup.warnings : [];
  if (!warningList.length) {
    warnings.style.display = 'none';
    warnings.replaceChildren();
    return;
  }

  warnings.replaceChildren();
  const alert = document.createElement('div');
  alert.className = 'alert alert-amber';

  const icon = document.createElement('span');
  icon.style.flexShrink = '0';
  icon.textContent = '⚠';

  const content = document.createElement('div');
  const summary = document.createElement('strong');
  summary.textContent = `${warningList.length} warning${warningList.length === 1 ? '' : 's'} detected`;

  const list = document.createElement('ul');
  list.className = 'alert-list';
  warningList.forEach((warning) => {
    const item = document.createElement('li');
    item.textContent = String(warning);
    list.appendChild(item);
  });

  content.appendChild(summary);
  content.appendChild(list);
  alert.appendChild(icon);
  alert.appendChild(content);
  warnings.appendChild(alert);
  warnings.style.display = '';
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

  const refreshSeq = ++_dashboardRefreshSeq;

  const [status, scanState] = await Promise.all([
    fetchJson('/status'),
    fetchJson('/api/scan/current'),
  ]);

  if (refreshSeq !== _dashboardRefreshSeq) return;

  if (status) {
    updateHero(status);
    updateTimeline(status);
    updateIssuePills(status);
    renderOverviewChart(status.overview_breakdown, status.files_indexed ?? 0);
    refreshTimestamps();
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

  if (form.id === 'setup-form') {
    const setup = await fetchJson('/api/setup');
    if (setup) renderSetupDiagnostics(setup);
  }
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
  const initialChart = document.getElementById('overview-chart');
  if (initialChart?.dataset.overviewBreakdown) {
    try {
      renderOverviewChart(
        JSON.parse(initialChart.dataset.overviewBreakdown),
        Number(document.getElementById('overview-chart-total')?.textContent || '0'),
      );
    } catch {
      renderOverviewChart([], Number(document.getElementById('overview-chart-total')?.textContent || '0'));
    }
  }
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
