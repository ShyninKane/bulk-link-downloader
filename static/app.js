const ROW_HEIGHT = 28;
const BUFFER = 10;

const uiState = {
  statusFilter: 'all',
  search: '',
};

const viewport = document.getElementById('viewport');
const spacer = document.getElementById('spacer');
const rowsEl = document.getElementById('rows');

function computeVisibleCount() {
  return Math.ceil(viewport.clientHeight / ROW_HEIGHT) + BUFFER * 2;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function formatBytes(n) {
  if (n === null || n === undefined) return '?';
  if (n < 1024) return n + ' B';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let u = -1;
  do { n /= 1024; u++; } while (n >= 1024 && u < units.length - 1);
  return n.toFixed(1) + ' ' + units[u];
}

function statusBadge(status) {
  return `<span class="badge ${status}">${status}</span>`;
}

function progressCell(item) {
  if (item.status === 'downloading') {
    const pct = item.bytesTotal ? Math.floor((100 * item.bytesDone) / item.bytesTotal) : null;
    return `${formatBytes(item.bytesDone)} / ${formatBytes(item.bytesTotal)}${pct !== null ? ' (' + pct + '%)' : ''}`;
  }
  if (item.status === 'downloaded') {
    return formatBytes(item.bytesTotal);
  }
  if (item.status === 'failed') {
    return `${formatBytes(item.bytesDone)} / ${formatBytes(item.bytesTotal)}`;
  }
  return '-';
}

async function fetchWindow(offset, limit) {
  const params = new URLSearchParams({
    offset: String(offset), limit: String(limit),
    status: uiState.statusFilter, search: uiState.search,
  });
  const res = await fetch('/api/state?' + params.toString());
  return res.json();
}

function renderRows(start, items) {
  rowsEl.innerHTML = '';
  const frag = document.createDocumentFragment();
  items.forEach((item, i) => {
    const idx = start + i;
    const el = document.createElement('div');
    el.className = 'row-item';
    el.style.top = (idx * ROW_HEIGHT) + 'px';
    el.innerHTML = `
      <div class="col col-idx">${idx + 1}</div>
      <div class="col col-id" title="${escapeHtml(item.identifier)}">${escapeHtml(item.identifier)}</div>
      <div class="col col-file" title="${escapeHtml(item.filename)}">${escapeHtml(item.filename)}</div>
      <div class="col col-status">${statusBadge(item.status)}</div>
      <div class="col col-progress">${progressCell(item)}</div>
      <div class="col col-attempts">${item.attempts}</div>
      <div class="col col-error err-cell" title="${escapeHtml(item.error || '')}">${escapeHtml(item.error || '')}</div>
    `;
    frag.appendChild(el);
  });
  rowsEl.appendChild(frag);
}

async function refreshWindow() {
  const scrollTop = viewport.scrollTop;
  const start = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - BUFFER);
  const count = computeVisibleCount();
  const { items, totalMatched } = await fetchWindow(start, count);
  spacer.style.height = (totalMatched * ROW_HEIGHT) + 'px';
  renderRows(start, items);
}

let scrollScheduled = false;
viewport.addEventListener('scroll', () => {
  if (!scrollScheduled) {
    scrollScheduled = true;
    requestAnimationFrame(() => { scrollScheduled = false; refreshWindow(); });
  }
});
window.addEventListener('resize', () => refreshWindow());

async function refreshSummary() {
  const res = await fetch('/api/summary');
  const s = await res.json();
  document.getElementById('c-total').textContent = s.total;
  document.getElementById('c-pending').textContent = s.pending;
  document.getElementById('c-downloading').textContent = s.downloading;
  document.getElementById('c-downloaded').textContent = s.downloaded;
  document.getElementById('c-failed').textContent = s.failed;
  const pct = s.total ? Math.floor((100 * s.downloaded) / s.total) : 0;
  document.getElementById('progress-fill').style.width = pct + '%';
  document.getElementById('running-indicator').textContent =
    s.running ? 'Running…' : (s.stopRequested ? 'Stopping…' : 'Idle');
  document.getElementById('start-btn').disabled = s.running;
  document.getElementById('stop-btn').disabled = !s.running;

  if (!document.getElementById('max-retries').dataset.userEdited) {
    document.getElementById('max-retries').value = s.maxRetries;
  }
  if (!document.getElementById('downloads-dir').dataset.userEdited) {
    document.getElementById('downloads-dir').value = s.downloadsDir;
  }
  document.getElementById('cookie-status').textContent = s.cookieSet ? 'Cookie: set' : 'Cookie: not set';
}

function toast(msg, type) {
  const el = document.createElement('div');
  el.className = 'toast' + (type ? ' ' + type : '');
  el.textContent = msg;
  document.getElementById('toasts').appendChild(el);
  setTimeout(() => el.remove(), 8000);
  const notifyEnabled = document.getElementById('enable-notify').checked;
  if (notifyEnabled && window.Notification && Notification.permission === 'granted') {
    new Notification('Bulk Link Downloader', { body: msg });
  }
}

function connectEvents() {
  const es = new EventSource('/api/events');
  es.onopen = () => { document.getElementById('conn-status').textContent = 'connected'; };
  es.onerror = () => { document.getElementById('conn-status').textContent = 'reconnecting…'; };
  es.onmessage = (e) => {
    const evt = JSON.parse(e.data);
    if (evt.type === 'error') {
      toast(`Failed after ${evt.attempts} attempt(s): ${evt.filename} — ${evt.error}`, 'error');
      refreshWindow();
    } else if (evt.type === 'finished') {
      const s = evt.summary;
      toast(`Finished: ${s.downloaded} downloaded, ${s.failed} failed, ${s.pending} pending.`, s.failed ? 'error' : 'success');
      refreshSummary(); refreshWindow();
    } else if (evt.type === 'started') {
      toast('Download started.', 'success');
    }
  };
}
connectEvents();

document.getElementById('file-input').addEventListener('change', (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    document.getElementById('links-input').value = reader.result;
    document.getElementById('load-info').textContent = `Loaded file: ${file.name}`;
  };
  reader.readAsText(file);
});

document.getElementById('load-btn').addEventListener('click', async () => {
  const text = document.getElementById('links-input').value;
  const links = text.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
  if (!links.length) { toast('No links to load.', 'error'); return; }
  const res = await fetch('/api/load', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ links }),
  });
  const data = await res.json();
  document.getElementById('load-info').textContent = `Loaded ${data.total} unique link(s).`;
  refreshSummary(); refreshWindow();
});

document.getElementById('start-btn').addEventListener('click', async () => {
  await fetch('/api/start', { method: 'POST' });
  refreshSummary();
});

document.getElementById('stop-btn').addEventListener('click', async () => {
  await fetch('/api/stop', { method: 'POST' });
  toast('Stop requested — finishing current file…', 'info');
  refreshSummary();
});

document.getElementById('retry-failed-btn').addEventListener('click', async () => {
  const res = await fetch('/api/retry-failed', { method: 'POST' });
  const data = await res.json();
  toast(`Reset ${data.count} failed link(s) to pending.`, 'success');
  refreshSummary(); refreshWindow();
});

document.getElementById('reset-btn').addEventListener('click', async () => {
  if (!confirm('Clear the entire link list and progress? This cannot be undone.')) return;
  await fetch('/api/load', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ links: [] }),
  });
  document.getElementById('links-input').value = '';
  document.getElementById('load-info').textContent = 'Cleared.';
  refreshSummary(); refreshWindow();
});

function debounce(fn, wait) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), wait);
  };
}

async function applySettings() {
  const maxRetries = parseInt(document.getElementById('max-retries').value, 10) || 5;
  const downloadsDir = document.getElementById('downloads-dir').value.trim();
  await fetch('/api/config', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ maxRetries, downloadsDir }),
  });
}
const applySettingsDebounced = debounce(applySettings, 600);

['max-retries', 'downloads-dir'].forEach((id) => {
  const el = document.getElementById(id);
  el.addEventListener('input', () => { el.dataset.userEdited = '1'; applySettingsDebounced(); });
});

document.getElementById('status-filter').addEventListener('change', (e) => {
  uiState.statusFilter = e.target.value;
  viewport.scrollTop = 0;
  refreshWindow();
});

let searchDebounce;
document.getElementById('search-input').addEventListener('input', (e) => {
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(() => {
    uiState.search = e.target.value.trim();
    viewport.scrollTop = 0;
    refreshWindow();
  }, 300);
});

const COOKIE_STORAGE_KEY = 'bulkLinkDownloader_iaCookie';

async function sendCookie(value) {
  await fetch('/api/config', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cookie: value }),
  });
}

const applyCookieDebounced = debounce(async () => {
  const value = document.getElementById('ia-cookie').value.trim();
  localStorage.setItem(COOKIE_STORAGE_KEY, value);
  await sendCookie(value);
  refreshSummary();
}, 600);

document.getElementById('ia-cookie').addEventListener('input', applyCookieDebounced);

(function restoreCookie() {
  const saved = localStorage.getItem(COOKIE_STORAGE_KEY);
  if (saved) {
    document.getElementById('ia-cookie').value = saved;
    sendCookie(saved);
  }
})();

document.getElementById('enable-notify').addEventListener('change', (e) => {
  if (e.target.checked && window.Notification && Notification.permission !== 'granted') {
    Notification.requestPermission();
  }
});

setInterval(() => { refreshSummary(); refreshWindow(); }, 1000);

refreshSummary();
refreshWindow();
