/* ═══════════════════════════════════════════════════════════════
   ARTOYOU — Frontend logic
   ═══════════════════════════════════════════════════════════════ */
'use strict';

// ── Optional shared-secret token (?token=… in URL → sessionStorage) ──
const urlToken = new URLSearchParams(window.location.search).get('token');
if (urlToken) sessionStorage.setItem('artoyou_token', urlToken);
const TOKEN = sessionStorage.getItem('artoyou_token') || '';
const apiFetch = (path, options = {}) => {
  const headers = Object.assign({}, options.headers || {});
  if (TOKEN) headers['x-artoyou-token'] = TOKEN;
  return fetch(path, Object.assign({}, options, { headers }));
};
const fileUrl = (id) => `/api/jobs/${encodeURIComponent(id)}/file` + (TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : '');

// ── Element handles ──
const $ = (sel) => document.querySelector(sel);
const urlInput = $('#urlInput');
const urlBox = $('#urlBox');
const pasteBtn = $('#pasteBtn');
const analyzeBtn = $('#analyzeBtn');
const errorBox = $('#errorBox');
const errorText = $('#errorText');
const resultSection = $('#resultSection');
const thumbImg = $('#thumbImg');
const durationBadge = $('#durationBadge');
const videoTitle = $('#videoTitle');
const uploaderName = $('#uploaderName');
const viewCount = $('#viewCount');
const uploadDate = $('#uploadDate');
const uploadDateItem = $('#uploadDateItem');
const rangeWarning = $('#rangeWarning');
const rangeWarningText = $('#rangeWarningText');
const qualityGrid = $('#qualityGrid');
const audioRow = $('#audioRow');
const jobsList = $('#jobsList');
const jobsEmpty = $('#jobsEmpty');
const statusPill = $('#statusPill');
const statusText = $('#statusText');
const limitsPill = $('#limitsPill');
const toastContainer = $('#toastContainer');
const historySection = $('#historySection');
const historyList = $('#historyList');

const state = {
  info: null,
  jobsTimer: null,
  lastJobIds: new Set(),
};

// ── Formatters ──
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const faNum = new Intl.NumberFormat('fa-IR');
const enNum = new Intl.NumberFormat('en-US');

function fmtBytes(bytes) {
  if (!bytes && bytes !== 0) return 'حجم نامشخص';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, n = Number(bytes);
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n >= 100 ? Math.round(n) : n.toFixed(n >= 10 ? 1 : 2)} ${units[i]}`;
}
function fmtSpeed(bytesPerSec) {
  if (!bytesPerSec) return '';
  return `${fmtBytes(bytesPerSec)}/s`;
}
function fmtEta(seconds) {
  if (seconds == null || !isFinite(seconds)) return '';
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} ثانیه`;
  const m = Math.floor(s / 60), r = s % 60;
  return `${m}:${String(r).padStart(2, '0')} دقیقه`;
}
function fmtDate(yyyymmdd) {
  if (!yyyymmdd || String(yyyymmdd).length !== 8) return '';
  const y = +String(yyyymmdd).slice(0, 4), m = +String(yyyymmdd).slice(4, 6) - 1, d = +String(yyyymmdd).slice(6, 8);
  try { return new Date(y, m, d).toLocaleDateString('fa-IR', { year: 'numeric', month: 'long', day: 'numeric' }); }
  catch { return `${y}/${m + 1}/${d}`; }
}
function fmtViews(v) { return v == null ? '—' : faNum.format(v); }

function toast(message, kind = 'info') {
  const el = document.createElement('div');
  el.className = `toast ${kind === 'ok' ? 't-ok' : kind === 'err' ? 't-err' : ''}`;
  el.textContent = message;
  toastContainer.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transform = 'translateY(6px)'; }, 4200);
  setTimeout(() => el.remove(), 4800);
}

// ── Health check ──
async function checkHealth() {
  try {
    const res = await apiFetch('/api/health');
    const data = await res.json();
    statusPill.dataset.state = 'online';
    statusText.textContent = 'سرور آنلاین';
    if (data?.limits) {
      const min = Math.round(data.limits.min / 60), max = Math.round(data.limits.max / 60);
      limitsPill.textContent = `ویدیوهای ${min}–${max} دقیقه`;
    }
  } catch {
    statusPill.dataset.state = 'offline';
    statusText.textContent = 'سرور در دسترس نیست';
  }
}

// ── History (localStorage) ──
const HISTORY_KEY = 'artoyou_history_v1';
function loadHistory() { try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]'); } catch { return []; } }
function saveHistory(items) { localStorage.setItem(HISTORY_KEY, JSON.stringify(items.slice(0, 20))); }
function pushHistory(entry) {
  const items = loadHistory();
  items.unshift(Object.assign({ date: Date.now() }, entry));
  saveHistory(items);
  renderHistory();
}
function renderHistory() {
  const items = loadHistory();
  historySection.hidden = items.length === 0;
  historyList.innerHTML = items.slice(0, 8).map((item) => `
    <li class="history-item">
      <b title="${esc(item.title)}">${esc(item.title)}</b>
      <span style="display:flex;align-items:center;gap:.6rem;">
        <span class="job-quality">${esc(item.label)}</span>
        <time dir="ltr">${new Date(item.date).toLocaleDateString('fa-IR')}</time>
      </span>
    </li>`).join('');
}

// ── Analyze flow ──
function setLoading(loading) {
  analyzeBtn.classList.toggle('loading', loading);
  analyzeBtn.disabled = loading;
}
function showError(message) {
  errorText.textContent = message;
  errorBox.hidden = false;
  urlBox.classList.add('shake');
  setTimeout(() => urlBox.classList.remove('shake'), 500);
}
function clearError() { errorBox.hidden = true; }

async function analyze() {
  const raw = urlInput.value.trim();
  if (!raw) { showError('اول لینک ویدیوی یوتیوب را بچسبان.'); return; }
  clearError();
  setLoading(true);
  resultSection.hidden = true;
  try {
    const res = await apiFetch('/api/info', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: raw }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data?.detail || 'تحلیل لینک ناموفق بود.');
    state.info = data;
    renderResult(data);
  } catch (err) {
    showError(err.message || 'ارتباط با سرور برقرار نشد.');
  } finally {
    setLoading(false);
  }
}

function renderResult(data) {
  thumbImg.src = data.thumbnail || '';
  thumbImg.alt = data.title || 'thumbnail';
  durationBadge.textContent = data.duration_string || '';
  videoTitle.textContent = data.title || '—';
  uploaderName.textContent = data.uploader || '—';
  viewCount.textContent = fmtViews(data.views);
  const d = fmtDate(data.upload_date);
  uploadDate.textContent = d || '';
  uploadDateItem.style.display = d ? 'inline-flex' : 'none';

  const within = data.within_limits !== false;
  rangeWarning.hidden = within;
  if (!within) {
    const mins = Math.round((data.duration || 0) / 60);
    const minL = Math.round((data.limits?.min || 300) / 60), maxL = Math.round((data.limits?.max || 3600) / 60);
    rangeWarningText.textContent = `این ویدیو ${faNum.format(mins)} دقیقه است؛ این سامانه فعلاً فقط بازه ${minL} تا ${maxL} دقیقه را پردازش می‌کند.`;
  }

  // Quality grid
  qualityGrid.innerHTML = '';
  const options = data.options || [];
  if (!options.length) {
    qualityGrid.innerHTML = '<p class="hint">هیچ کیفیت تصویری برای این ویدیو یافت نشد.</p>';
  }
  for (const opt of options) {
    const disabled = !within || (opt.needs_ffmpeg && !data.has_ffmpeg);
    const btn = document.createElement('button');
    btn.className = 'quality-btn';
    btn.disabled = disabled;
    const title = opt.needs_ffmpeg && !data.has_ffmpeg ? 'ffmpeg روی سرور نصب نیست' : `دانلود با کیفیت ${opt.label}`;
    btn.title = title;
    btn.innerHTML = `
      <span class="q-top"><span class="q-badge ${badgeClass(opt.badge)}">${esc(opt.badge)}</span></span>
      <span class="q-label" dir="ltr">${esc(opt.label)}</span>
      <span class="q-meta" dir="ltr">${esc((opt.ext || 'mp4').toUpperCase())} · ${esc(opt.codec || '')}${opt.fps ? ' · ' + Math.round(opt.fps) + 'fps' : ''}</span>
      <span class="q-size">${opt.size ? '≈ ' + fmtBytes(opt.size) : 'حجم نامشخص'}</span>
      <span class="q-mode">${opt.mode === 'merge' ? 'ترکیب تصویر + صدا' : 'فایل مستقیم'}</span>
      <svg class="q-dl" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>`;
    btn.addEventListener('click', () => startDownload({
      url: data.webpage_url, kind: 'video', format_id: opt.format_id, container: 'mp4',
      label: opt.label, title: data.title, thumbnail: data.thumbnail,
    }, `ویدیو با کیفیت ${opt.label} به صف اضافه شد`));
    qualityGrid.appendChild(btn);
  }

  // Audio options
  audioRow.innerHTML = '';
  const audios = data.audio || [];
  if (!audios.length) {
    audioRow.innerHTML = '<p class="hint">فرمت صوتی جداگانه‌ای موجود نیست.</p>';
  }
  for (const opt of audios) {
    const disabled = !within || (opt.needs_ffmpeg && !data.has_ffmpeg);
    const btn = document.createElement('button');
    btn.className = 'audio-btn';
    btn.disabled = disabled;
    btn.title = opt.needs_ffmpeg && !data.has_ffmpeg ? 'ffmpeg روی سرور نصب نیست' : `دانلود صدا به صورت ${opt.label}`;
    btn.innerHTML = `
      <span class="audio-icon"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg></span>
      <span class="audio-info"><b>${esc(opt.label)}${opt.size ? ' · ≈ ' + fmtBytes(opt.size) : ''}</b><small>${esc(opt.sub || '')}</small></span>`;
    btn.addEventListener('click', () => startDownload({
      url: data.webpage_url, kind: 'audio', format_id: opt.format_id, container: opt.container,
      label: opt.label, title: data.title, thumbnail: data.thumbnail,
    }, `صدای ${opt.label} به صف اضافه شد`));
    audioRow.appendChild(btn);
  }

  resultSection.hidden = false;
  resultSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function badgeClass(badge) {
  switch (String(badge).toUpperCase()) {
    case '8K': return 'b-8k';
    case '4K': return 'b-4k';
    case '2K': return 'b-2k';
    case 'HD': return 'b-hd';
    default: return 'b-sd';
  }
}

// ── Download + jobs ──
async function startDownload(payload, successMsg) {
  try {
    const res = await apiFetch('/api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data?.detail || 'شروع دانلود ناموفق بود.');
    pushHistory({ title: payload.title, label: `${payload.kind === 'audio' ? '🎵' : '🎬'} ${payload.label}` });
    toast(successMsg, 'ok');
    await refreshJobs();
    ensurePolling();
  } catch (err) {
    toast(err.message || 'خطا در شروع دانلود.', 'err');
  }
}

const STATUS_FA = {
  queued: 'در صف', downloading: 'در حال دانلود', processing: 'در حال پردازش',
  ready: 'آماده', error: 'خطا', cancelled: 'لغو شده',
};

function jobStatusClass(s) { return `s-${s in STATUS_FA ? s : 'queued'}`; }

async function refreshJobs() {
  try {
    const res = await apiFetch('/api/jobs?limit=30');
    const data = await res.json().catch(() => ({ jobs: [] }));
    renderJobs(data.jobs || []);
  } catch { /* keep last render */ }
}

function renderJobs(jobs) {
  jobsEmpty.hidden = jobs.length > 0;
  jobsList.innerHTML = jobs.map((job) => {
    const status = job.status || 'queued';
    const pct = status === 'ready' ? 100 : Math.round((job.progress || 0) * 1000) / 10;
    const total = job.total_bytes;
    const sub = [];
    if (status === 'downloading') {
      sub.push(`<span class="pct dir-ltr" dir="ltr">${pct}%</span>`);
      if (job.speed) sub.push(`<span dir="ltr">${fmtSpeed(job.speed)}</span>`);
      if (job.eta != null) sub.push(`<span>باقی‌مانده: ${fmtEta(job.eta)}</span>`);
      if (total) sub.push(`<span dir="ltr">${fmtBytes(job.downloaded_bytes)} / ${fmtBytes(total)}</span>`);
    } else if (status === 'processing') {
      sub.push('<span>در حال ترکیب/تبدیل نهایی با ffmpeg…</span>');
    } else if (status === 'ready') {
      sub.push(`<span dir="ltr">${fmtBytes(job.size_bytes)}</span>`);
      sub.push('<span>فایل آماده دانلود است</span>');
    } else if (status === 'queued') {
      sub.push('<span>منتظر نوبت در صف…</span>');
    }
    const thumb = job.thumbnail
      ? `<img class="job-thumb" src="${esc(job.thumbnail)}" alt="" loading="lazy" onerror="this.outerHTML='&lt;div class=&quot;job-thumb-placeholder&quot;&gt;▶&lt;/div&gt;'"/>`
      : `<div class="job-thumb-placeholder"><svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg></div>`;
    const progressCls = status === 'processing' ? 'progress-fill processing' : 'progress-fill';
    const actions = status === 'ready'
      ? `<a class="file-btn" href="${fileUrl(job.id)}" download><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>دانلود فایل</a>
         <button class="icon-action danger" data-action="remove" data-id="${esc(job.id)}" title="حذف از لیست"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>حذف</button>`
      : (status === 'downloading' || status === 'processing' || status === 'queued')
        ? `<button class="icon-action danger" data-action="cancel" data-id="${esc(job.id)}" title="لغو دانلود"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>لغو</button>`
        : `<button class="icon-action danger" data-action="remove" data-id="${esc(job.id)}" title="حذف از لیست"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>حذف</button>`;
    return `
      <article class="job-item" data-id="${esc(job.id)}">
        ${thumb}
        <div class="job-info">
          <p class="job-title" title="${esc(job.title)}">${esc(job.title || 'در حال دریافت اطلاعات…')}</p>
          <div class="job-meta">
            <span class="job-quality" dir="ltr">${esc(job.label)}</span>
            <span class="job-status ${jobStatusClass(status)}">${STATUS_FA[status] || esc(status)}</span>
          </div>
          <div class="progress" role="progressbar" aria-valuenow="${Math.round(pct)}" aria-valuemin="0" aria-valuemax="100">
            <div class="${progressCls}" style="width:${pct}%"></div>
          </div>
          <div class="job-sub">${sub.join('')}</div>
          ${status === 'error' && job.error ? `<p class="job-error">${esc(job.error)}</p>` : ''}
        </div>
        <div class="job-actions">${actions}</div>
      </article>`;
  }).join('');
}

function ensurePolling() {
  if (state.jobsTimer) return;
  state.jobsTimer = setInterval(async () => {
    await refreshJobs();
    const active = jobsList.querySelectorAll('.job-status.s-downloading, .job-status.s-processing, .job-status.s-queued').length > 0;
    if (!active) { clearInterval(state.jobsTimer); state.jobsTimer = null; }
  }, 1500);
}

jobsList.addEventListener('click', async (event) => {
  const btn = event.target.closest('[data-action]');
  if (!btn) return;
  const { action, id } = btn.dataset;
  if (!id) return;
  try {
    if (action === 'cancel') {
      await apiFetch(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
      toast('لغو درخواست شد…');
    } else if (action === 'remove') {
      await apiFetch(`/api/jobs/${encodeURIComponent(id)}`, { method: 'DELETE' });
    }
    await refreshJobs();
  } catch {
    toast('عملیات ناموفق بود.', 'err');
  }
});

$('#clearFinishedBtn').addEventListener('click', async () => {
  const jobs = jobsList.querySelectorAll('.job-item');
  if (!jobs.length) return;
  // Ask the server to drop every non-active job.
  try {
    const res = await apiFetch('/api/jobs?limit=100');
    const data = await res.json().catch(() => ({ jobs: [] }));
    await Promise.all((data.jobs || [])
      .filter((j) => !['downloading', 'processing', 'queued'].includes(j.status))
      .map((j) => apiFetch(`/api/jobs/${encodeURIComponent(j.id)}`, { method: 'DELETE' })));
    await refreshJobs();
    toast('دانلودهای تمام‌شده پاک شد.', 'ok');
  } catch { toast('پاک‌سازی ناموفق بود.', 'err'); }
});

$('#clearHistoryBtn').addEventListener('click', () => { localStorage.removeItem(HISTORY_KEY); renderHistory(); });

// ── Wire up analyze ──
analyzeBtn.addEventListener('click', analyze);
urlInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') analyze(); });
pasteBtn.addEventListener('click', async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (text) { urlInput.value = text.trim(); clearError(); urlInput.focus(); }
    else toast('کلیپ‌بورد خالی است.', 'err');
  } catch {
    toast('دسترسی به کلیپ‌بورد ممکن نشد — لینک را دستی بچسبان (Ctrl+V).', 'err');
  }
});

// ── Boot ──
checkHealth();
renderHistory();
refreshJobs().then(() => ensurePolling());
