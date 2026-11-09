const API = '';
let token = localStorage.getItem('vlambda_token');
let lambdas = [];
let metricLambdas = [];
let page = 0;
const PAGE_SIZE = 50;
let codeEditor = null, createEditor = null, projectEditor = null;
let currentSlug = null, currentKind = 'code';
let envLoadedSlug = null;
let projectFiles = {}, activeProjectFile = null;
let chartInv = null, chartLat = null;
let metricsBusy = false;
let logBuffer = [];
let detailItems = [];

/* ---------- frontend logging ---------- */
function flog(level, msg, data) {
  const entry = { ts: new Date().toISOString(), level, msg, data };
  logBuffer.push(entry);
  if (logBuffer.length > 500) logBuffer.shift();
  renderLogs();
  const fn = level === 'error' ? console.error : level === 'warn' ? console.warn : console.log;
  fn('[vlambda]', msg, data !== undefined ? data : '');
}
function renderLogs() {
  const body = document.getElementById('log-body');
  if (!body) return;
  body.innerHTML = logBuffer.slice().reverse().map(e =>
    '<div class="log-line log-' + escapeHtml(String(e.level)) + '">' + e.ts.slice(11,19) + ' [' + escapeHtml(String(e.level)) + '] ' + escapeHtml(e.msg) +
    (e.data !== undefined ? ' ' + escapeHtml(JSON.stringify(e.data)) : '') + '</div>'
  ).join('');
}
function toggleLogs() {
  const d = document.getElementById('log-drawer');
  d.classList.toggle('hidden');
  flog('info', 'log drawer toggled');
}
function clearLogs() { logBuffer = []; renderLogs(); flog('info', 'logs cleared'); }
window.addEventListener('error', e => flog('error', 'window error', e.message));
window.addEventListener('unhandledrejection', e => flog('error', 'unhandled rejection', String(e.reason)));

/* ---------- helpers ---------- */
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function lambdaUrl(slug) { return location.origin + '/f/' + encodeURIComponent(slug); }
function notify(msg, type) {
  const el = document.createElement('div');
  el.textContent = msg;
  el.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:9999;padding:10px 14px;border-radius:6px;' +
    'font-size:13px;box-shadow:0 4px 12px rgba(0,0,0,.4);color:#fff;background:' +
    (type === 'error' ? '#da3633' : '#238636') + ';';
  document.body.appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .3s';
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 300);
  }, 2500);
}
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch (e) { /* fall back to execCommand below */ }
  }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.top = '-1000px';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } finally { document.body.removeChild(ta); }
  if (!ok) throw new Error('clipboard not available');
}
async function copyLambdaUrl(slug) {
  try {
    await copyText(lambdaUrl(slug));
    notify('Invoke URL copied: ' + lambdaUrl(slug));
    flog('info', 'invoke URL copied', { slug });
    return true;
  } catch (e) {
    notify('Could not copy the URL', 'error');
    flog('warn', 'copy failed', e.message);
    return false;
  }
}
function setInvokeUrl(inputId, openId, slug) {
  const input = document.getElementById(inputId);
  const open = document.getElementById(openId);
  if (!slug) return;
  const url = lambdaUrl(slug);
  if (input) input.value = url;
  if (open) open.href = url;
}
function attachCursorStatus(hostId, statusId) {
  const host = document.getElementById(hostId);
  if (!host || host.dataset.cursorBound) return;
  host.dataset.cursorBound = '1';
  host.addEventListener('cm-cursor', e => {
    const el = document.getElementById(statusId);
    if (el) el.textContent = 'Ln ' + e.detail.line + ', Col ' + e.detail.col;
  });
}
function hide(id) { document.getElementById(id).classList.add('hidden'); }
function show(id) { document.getElementById(id).classList.remove('hidden'); }
function showAlert(id, msg, type) {
  const el = document.getElementById(id);
  if (!el) { flog(type === 'error' ? 'error' : 'info', msg); return; }
  el.innerHTML = '<div class="alert alert-' + type + '">' + escapeHtml(msg) + '</div>';
  setTimeout(() => { el.innerHTML = ''; }, 6000);
  flog(type === 'error' ? 'error' : 'info', msg);
}
function clearAlerts() {
  document.querySelectorAll('[id$="-alert"]').forEach(el => { el.innerHTML = ''; });
}
function reportError(alertId, e) {
  // 401 already redirected to the login screen; do not flash "unauthorized".
  if (e && e.auth) return;
  showAlert(alertId, e && e.message ? e.message : String(e), 'error');
}
function readName(inputId, alertId) {
  const input = document.getElementById(inputId);
  const value = input ? input.value.trim() : '';
  if (!value) {
    showAlert(alertId, 'Name is required', 'error');
    if (input) input.focus();
    return '';
  }
  return value;
}
function updateCreateButtons() {
  const lambdaBtn = document.getElementById('create-lambda-btn');
  if (lambdaBtn) {
    const name = (document.getElementById('new-name').value || '').trim();
    lambdaBtn.disabled = !name || !createEditor;
  }
  const projectBtn = document.getElementById('create-project-btn');
  if (projectBtn) {
    const name = (document.getElementById('proj-name').value || '').trim();
    const entry = (document.getElementById('proj-entry').value || '').trim();
    const file = document.getElementById('proj-file').files.length > 0;
    projectBtn.disabled = !name || !entry || !file;
  }
}
async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  const requestToken = token;
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const t0 = performance.now();
  flog('debug', 'request ' + (opts.method || 'GET') + ' ' + path);
  let resp;
  try {
    resp = await fetch(API + path, Object.assign({}, opts, { headers }));
  } catch (e) {
    flog('error', 'network error ' + path, String(e));
    throw e;
  }
  const ms = (performance.now() - t0).toFixed(1);
  const text = await resp.text();
  flog(resp.ok ? 'info' : 'warn', 'response ' + resp.status + ' ' + path + ' (' + ms + 'ms)',
       text.slice(0, 300));
  let data = null;
  try { data = text ? JSON.parse(text) : {}; } catch (e) { data = text; }
  if (!resp.ok) {
    if (resp.status === 401 && path !== '/login' && path !== '/register' && path !== '/password') {
      // Only invalidate the session if this request used the current token.
      // An old request finishing after a new login must not log the user out.
      if (requestToken && token === requestToken) {
        token = null;
        localStorage.removeItem('vlambda_token');
        checkAuth();
      }
      const authError = new Error((data && data.error) ? data.error : 'unauthorized');
      authError.auth = true;
      throw authError;
    }
    const msg = (data && data.error) ? data.error : ('HTTP ' + resp.status);
    throw new Error(msg);
  }
  return data;
}
function authHeaders(json) {
  const h = { 'Authorization': 'Bearer ' + token };
  if (json) h['Content-Type'] = 'application/json';
  return h;
}

/* ---------- auth ---------- */
function checkAuth() {
  clearAlerts();
  if (!token) {
    hide('nav');
    ['dashboard','playground','metrics','logs','system','editor','project'].forEach(v => hide('view-' + v));
    show('view-login');
    flog('info', 'not authenticated');
  } else {
    hide('view-login');
    show('nav');
    showView('dashboard');
    flog('info', 'authenticated');
  }
}
async function doLogin() {
  const u = document.getElementById('username').value;
  const p = document.getElementById('password').value;
  try {
    const data = await api('/login', { method: 'POST', body: u + ':' + p });
    token = data.token;
    localStorage.setItem('vlambda_token', token);
    clearAlerts();
    flog('info', 'login ok', { user: u });
    checkAuth();
  } catch (e) { showAlert('login-alert', e.message, 'error'); }
}
async function doRegister() {
  const u = document.getElementById('username').value;
  const p = document.getElementById('password').value;
  try {
    await api('/register', { method: 'POST', body: u + ':' + p });
    showAlert('login-alert', 'Registered. Please login.', 'success');
  } catch (e) { showAlert('login-alert', e.message, 'error'); }
}
async function loadConfig() {
  try {
    const cfg = await fetch('/config').then(r => r.json());
    const btn = document.getElementById('register-btn');
    const note = document.getElementById('registration-closed');
    if (btn) btn.classList.toggle('hidden', !cfg.registration);
    if (note) note.classList.toggle('hidden', !!cfg.registration);
  } catch (e) { flog('warn', 'config failed', e.message); }
}
function logout() {
  token = null;
  localStorage.removeItem('vlambda_token');
  flog('info', 'logged out');
  checkAuth();
}

/* ---------- views ---------- */
function showView(name) {
  if (!token && name !== 'login') {
    flog('warn', 'view blocked without authentication', { view: name });
    checkAuth();
    return;
  }
  hide('view-login');
  ['dashboard','playground','metrics','logs','system','editor','project'].forEach(v => hide('view-' + v));
  show('view-' + name);
  const viewAlert = document.getElementById(name + '-alert');
  if (viewAlert) viewAlert.innerHTML = '';
  document.querySelectorAll('.nav a').forEach(a => a.classList.remove('active'));
  const nav = document.getElementById('nav-' + name);
  if (nav) nav.classList.add('active');
  flog('info', 'view ' + name);
  if (name === 'dashboard') loadLambdas();
  if (name === 'metrics') loadMetricOptions().then(loadMetrics);
  if (name === 'logs') loadLogs();
  if (name === 'system') loadSystem();
  if (name === 'playground') loadSampleRequest();
}

/* ---------- dashboard ---------- */
async function loadLambdas() {
  try {
    const data = await api('/vlambda?format=json&limit=' + PAGE_SIZE + '&offset=' + (page * PAGE_SIZE));
    if (Array.isArray(data.lambdas)) {
      lambdas = data.lambdas;
    } else {
      lambdas = (data.lambdas || '').trim().split('\n').filter(Boolean).map(line => {
        const p = line.split('|');
        return { slug: p[0], name: p[1] || '', method: p[2] || 'GET', kind: p[3] || 'code', entrypoint: p[4] || '' };
      });
    }
    document.getElementById('lambda-count').textContent = (data.count || lambdas.length) + ' lambdas';
    document.getElementById('page-label').textContent = 'page ' + (page + 1);
    renderLambdaTable();
    flog('info', 'loaded ' + lambdas.length + ' lambdas');
  } catch (e) { reportError('dashboard-alert', e); }
}
function renderLambdaTable() {
  const q = (document.getElementById('search').value || '').toLowerCase();
  const fm = document.getElementById('filter-method').value;
  const fk = document.getElementById('filter-kind').value;
  const tbody = document.getElementById('lambda-list');
  tbody.innerHTML = '';
  lambdas.filter(l => (!q || l.name.toLowerCase().includes(q) || l.slug.includes(q)) && (!fm || l.method === fm) && (!fk || l.kind === fk))
    .forEach(l => {
      const tr = document.createElement('tr');
      const badge = l.kind === 'project' ? '<span class="badge badge-project">project</span>' : '<span class="badge">code</span>';
      const url = '/f/' + encodeURIComponent(l.slug);
      tr.innerHTML = '<td><a href="' + url + '" target="_blank" rel="noopener"><strong>' + escapeHtml(l.name || l.slug) + '</strong></a></td>' +
        '<td class="mono"><a href="' + url + '" target="_blank" rel="noopener">' + escapeHtml(l.slug) + '</a></td>' +
        '<td><span class="badge badge-' + escapeHtml(String(l.method).toLowerCase()) + '">' + escapeHtml(l.method) + '</span></td>' +
        '<td>' + badge + '</td>' +
        '<td><span class="flex">' +
        '<button class="btn btn-sm" onclick="invokeLambda(\'' + l.slug + '\',\'' + l.method + '\')">Run</button>' +
        '<button class="btn btn-sm" onclick="editLambda(\'' + l.slug + '\',\'' + l.kind + '\')">Edit</button>' +
        '<button class="btn btn-sm" onclick="openInPlayground(\'' + l.slug + '\',\'' + l.method + '\')">Test</button>' +
        '<button class="btn btn-sm" onclick="copyLambdaUrl(\'' + l.slug + '\')">Copy link</button>' +
        '<button class="btn btn-sm" onclick="exportLambda(\'' + l.slug + '\')">Export</button>' +
        '<button class="btn btn-sm" onclick="duplicateLambda(\'' + l.slug + '\')">Dup</button>' +
        '<button class="btn btn-sm btn-danger" onclick="deleteLambda(\'' + l.slug + '\')">Del</button>' +
        '</span></td>';
      tbody.appendChild(tr);
    });
  flog('debug', 'table rendered', { visible: tbody.children.length });
}
function changePage(delta) { if (page + delta < 0) return; page += delta; loadLambdas(); }

/* ---------- create ---------- */
function showCreateCode() {
  show('create-code');
  updateCreateButtons();
  setTimeout(() => {
    if (!createEditor) createEditor = VLambdaEditor.createEditor(document.getElementById('create-editor'), 'def handler(request):\n    return {"msg": "hello", "path": request.path}');
    updateCreateButtons();
    document.getElementById('new-name').focus();
  }, 0);
}
function showCreateProject() {
  show('create-project');
  updateCreateButtons();
  document.getElementById('proj-name').focus();
}
async function createLambda() {
  const name = readName('new-name', 'dashboard-alert');
  if (!name) return;
  if (!createEditor) {
    showAlert('dashboard-alert', 'The code editor is still loading. Try again in a moment.', 'error');
    return;
  }
  const code = VLambdaEditor.getValue(createEditor);
  if (!code.trim()) {
    showAlert('dashboard-alert', 'Add Python code before creating the lambda.', 'error');
    return;
  }
  const method = document.getElementById('new-method').value;
  const timeout = document.getElementById('new-timeout').value;
  const memory = document.getElementById('new-memory').value;
  try {
    const data = await api('/vlambda?method=' + encodeURIComponent(method) + '&name=' + encodeURIComponent(name) + '&timeout=' + timeout + '&memory_mb=' + memory,
      { method: 'POST', body: code, headers: authHeaders(false) });
    showAlert('dashboard-alert', 'Created "' + name + '" (' + data.slug + ')', 'success');
    document.getElementById('new-name').value = '';
    updateCreateButtons();
    hide('create-code');
    loadLambdas();
  } catch (e) { reportError('dashboard-alert', e); }
}
async function createProject() {
  const name = readName('proj-name', 'dashboard-alert');
  if (!name) return;
  const file = document.getElementById('proj-file').files[0];
  if (!file) { showAlert('dashboard-alert', 'Select a .zip file', 'error'); return; }
  const buf = await file.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  const b64 = btoa(binary);
  const entry = (document.getElementById('proj-entry').value || '').trim();
  if (!entry) { showAlert('dashboard-alert', 'Entrypoint is required', 'error'); return; }
  const method = document.getElementById('proj-method').value;
  try {
    const data = await api('/vlambda?project=1&entry=' + encodeURIComponent(entry) + '&name=' + encodeURIComponent(name) + '&method=' + method,
      { method: 'POST', body: b64, headers: authHeaders(false) });
    showAlert('dashboard-alert', 'Created project "' + name + '" (' + data.slug + ')', 'success');
    document.getElementById('proj-name').value = '';
    updateCreateButtons();
    hide('create-project');
    loadLambdas();
  } catch (e) { reportError('dashboard-alert', e); }
}

/* ---------- edit code lambda ---------- */
async function editLambda(slug, kind) {
  currentSlug = slug; currentKind = kind;
  if (kind === 'project') { await openProject(slug); return; }
  showView('editor');
  document.getElementById('editor-title').textContent = 'Edit ' + slug;
  setInvokeUrl('edit-url', 'edit-url-open', slug);
  try {
    const code = await api('/vlambda/' + slug);
    if (!codeEditor) codeEditor = VLambdaEditor.createEditor(document.getElementById('editor-code'), code);
    else VLambdaEditor.setValue(codeEditor, code);
    const l = lambdas.find(x => x.slug === slug) || {};
    document.getElementById('editor-filename').textContent = (l.name || slug) + '.py';
    document.getElementById('editor-status-pos').textContent = 'Ln 1, Col 1';
    attachCursorStatus('editor-code', 'editor-status-pos');
    document.getElementById('edit-name').value = l.name || '';
    document.getElementById('edit-method').value = l.method || 'GET';
    document.getElementById('edit-timeout').value = '30';
    document.getElementById('edit-memory').value = '0';
    await loadVersions(slug);
    await loadAliases(slug);
    await loadPackages();
    await loadEnv(slug);
    await loadKV(slug);
    await loadEgress(slug);
    await loadSchedules(slug);
    await loadMeta(slug);
    flog('info', 'editing code lambda', { slug });
  } catch (e) { reportError('editor-alert', e); }
}
async function saveCodeLambda() {
  const code = codeEditor ? VLambdaEditor.getValue(codeEditor) : '';
  const name = readName('edit-name', 'editor-alert');
  if (!name) return;
  const method = document.getElementById('edit-method').value;
  const timeout = document.getElementById('edit-timeout').value;
  const memory = document.getElementById('edit-memory').value;
  try {
    await api('/vlambda/' + currentSlug + '?method=' + encodeURIComponent(method) + '&name=' + encodeURIComponent(name) + '&timeout=' + timeout + '&memory_mb=' + memory,
      { method: 'PUT', body: code, headers: authHeaders(false) });
    showAlert('editor-alert', 'Saved', 'success');
    await loadVersions(currentSlug);
  } catch (e) { reportError('editor-alert', e); }
}
async function runCurrent() {
  await saveCodeLambda();
  await invokeLambda(currentSlug, document.getElementById('edit-method').value);
}
async function loadVersions(slug) {
  try {
    const data = await api('/vlambda/' + slug + '/versions');
    document.getElementById('versions-list').innerHTML = (data.versions || []).map(v =>
      '<div class="flex-between" style="padding:2px 0;"><span>v' + v.version + '</span>' +
      '<span class="flex"><button class="btn btn-sm" onclick="viewVersion(' + v.version + ')">View</button>' +
      '<button class="btn btn-sm" onclick="rollbackVersion(' + v.version + ')">Restore</button></span></div>'
    ).join('') || '—';
  } catch (e) { flog('warn', 'versions failed', e.message); }
}
async function viewVersion(version) {
  try {
    const code = await api('/vlambda/' + currentSlug + '/version/' + version);
    document.getElementById('detail-body').textContent = code;
    show('detail-modal');
    flog('info', 'viewed version', { slug: currentSlug, version });
  } catch (e) { reportError('editor-alert', e); }
}
async function rollbackVersion(version) {
  if (!confirm('Restore version ' + version + '?')) return;
  try {
    await api('/vlambda/' + currentSlug + '/rollback?version=' + version, { method: 'POST', headers: authHeaders(false) });
    showAlert('editor-alert', 'Restored v' + version, 'success');
    await loadVersions(currentSlug);
  } catch (e) { reportError('editor-alert', e); }
}

async function loadAliases(slug) {
  try {
    const data = await api('/vlambda/' + slug + '/aliases');
    document.getElementById('aliases-list').innerHTML = (data.aliases || []).map(a =>
      '<div class="flex-between" style="padding:2px 0;"><span>' + escapeHtml(a.alias) + '</span><span class="muted">v' + a.version + '</span></div>'
    ).join('') || '—';
  } catch (e) { flog('warn', 'aliases failed', e.message); }
}
async function createAlias() {
  const name = readName('alias-name', 'editor-alert');
  if (!name) return;
  const version = document.getElementById('alias-version').value || '0';
  try {
    await api('/vlambda/' + currentSlug + '/alias?name=' + encodeURIComponent(name) + '&version=' + version, { method: 'POST', headers: authHeaders(false) });
    showAlert('editor-alert', 'Alias saved', 'success');
    document.getElementById('alias-name').value = '';
    await loadAliases(currentSlug);
  } catch (e) { reportError('editor-alert', e); }
}

async function loadPackages() {
  if (!currentSlug) return;
  try {
    const data = await api('/vlambda/' + currentSlug + '/packages');
    const text = (data.packages || []).join('\n') || '—';
    const el = document.getElementById('packages-list');
    if (el) el.textContent = text;
    const pel = document.getElementById('project-packages-list');
    if (pel) pel.textContent = text;
    flog('info', 'packages loaded', { slug: currentSlug, count: (data.packages || []).length });
  } catch (e) { flog('warn', 'packages failed', e.message); }
}

function kvPrefix() { return currentKind === 'project' ? 'project' : 'edit'; }
function kvDisplay(item) {
  if (item.binary) return '<binary>';
  try { return typeof item.value === 'string' ? item.value : JSON.stringify(item.value); }
  catch (e) { return String(item.value); }
}
async function loadKV(slug) {
  const prefix = kvPrefix();
  const el = document.getElementById(prefix + '-kv-list');
  if (!el) return;
  try {
    const data = await api('/vlambda/' + slug + '/kv');
    const items = data.items || [];
    el.innerHTML = items.length ? items.map(item =>
      '<div class="flex-between" style="padding:2px 0;gap:8px;">' +
        '<span class="mono" style="overflow:hidden;text-overflow:ellipsis;">' +
          escapeHtml(item.key) + ' = ' + escapeHtml(kvDisplay(item)) +
          (item.expires_at ? ' <span class="muted">(ttl)</span>' : '') +
        '</span>' +
        '<button class="btn btn-sm" onclick="deleteKV(' + escapeHtml(JSON.stringify(item.key)) + ')">Del</button>' +
      '</div>'
    ).join('') : '<span class="muted">—</span>';
  } catch (e) { flog('warn', 'kv load failed', e.message); }
}
async function setKV() {
  const prefix = kvPrefix();
  const key = (document.getElementById(prefix + '-kv-key').value || '').trim();
  if (!key) { showAlert(prefix === 'project' ? 'project-alert' : 'editor-alert', 'KV key is required', 'error'); return; }
  let value = document.getElementById(prefix + '-kv-value').value;
  try { value = JSON.parse(value); } catch (e) { /* keep as text */ }
  const ttlRaw = document.getElementById(prefix + '-kv-ttl').value;
  const payload = { value };
  if (ttlRaw) payload.ttl = Number(ttlRaw);
  try {
    await api('/vlambda/' + currentSlug + '/kv/' + encodeURIComponent(key), {
      method: 'PUT', headers: authHeaders(true), body: JSON.stringify(payload),
    });
    document.getElementById(prefix + '-kv-value').value = '';
    document.getElementById(prefix + '-kv-ttl').value = '';
    await loadKV(currentSlug);
  } catch (e) { reportError(prefix === 'project' ? 'project-alert' : 'editor-alert', e); }
}
async function deleteKV(key) {
  try {
    await api('/vlambda/' + currentSlug + '/kv/' + encodeURIComponent(key), { method: 'DELETE', headers: authHeaders(false) });
    await loadKV(currentSlug);
  } catch (e) { reportError(kvPrefix() === 'project' ? 'project-alert' : 'editor-alert', e); }
}

function egressPrefix() { return currentKind === 'project' ? 'project' : 'edit'; }
async function loadEgress(slug) {
  const el = document.getElementById(egressPrefix() + '-egress');
  if (!el) return;
  try {
    const data = await api('/vlambda/' + slug + '/egress');
    el.value = (data.hosts || []).join('\n');
  } catch (e) { flog('warn', 'egress load failed', e.message); }
}
async function saveEgress() {
  const prefix = egressPrefix();
  const el = document.getElementById(prefix + '-egress');
  const hosts = (el.value || '').split('\n').map(x => x.trim()).filter(Boolean);
  try {
    await api('/vlambda/' + currentSlug + '/egress', {
      method: 'PUT', headers: authHeaders(true), body: JSON.stringify({ hosts }),
    });
    showAlert(prefix === 'project' ? 'project-alert' : 'editor-alert', 'Egress allow-list saved', 'success');
    await loadEgress(currentSlug);
  } catch (e) { reportError(prefix === 'project' ? 'project-alert' : 'editor-alert', e); }
}

function schedulePrefix() { return currentKind === 'project' ? 'project' : 'edit'; }
function scheduleDate(value) {
  if (!value) return '—';
  try { return new Date(value * 1000).toLocaleString(); } catch (e) { return String(value); }
}
async function loadSchedules(slug) {
  const prefix = schedulePrefix();
  const el = document.getElementById(prefix + '-schedules-list');
  if (!el) return;
  try {
    const data = await api('/vlambda/' + slug + '/schedules');
    const items = data.schedules || [];
    el.innerHTML = items.length ? items.map(item =>
      '<div class="flex-between" style="padding:3px 0;gap:8px;">' +
        '<span class="mono" style="overflow:hidden;text-overflow:ellipsis;">' +
          escapeHtml(item.name) + ' · ' + escapeHtml(item.cron) + ' · ' + escapeHtml(item.timezone) +
          '<br><span class="muted">next ' + escapeHtml(scheduleDate(item.next_run)) +
          ' · last ' + escapeHtml(scheduleDate(item.last_run)) +
          (item.last_status ? ' (' + escapeHtml(item.last_status) + ')' : '') + '</span></span>' +
        '<span class="flex">' +
          '<button class="btn btn-sm" onclick="runSchedule(' + Number(item.id) + ')">Run</button>' +
          '<button class="btn btn-sm" onclick="deleteSchedule(' + Number(item.id) + ')">Del</button>' +
        '</span>' +
      '</div>'
    ).join('') : '<span class="muted">—</span>';
  } catch (e) { flog('warn', 'schedules load failed', e.message); }
}
async function createSchedule() {
  const prefix = schedulePrefix();
  const value = id => (document.getElementById(prefix + '-' + id).value || '').trim();
  const name = value('schedule-name');
  const cron = value('schedule-cron');
  if (!name || !cron) {
    showAlert(prefix === 'project' ? 'project-alert' : 'editor-alert', 'Schedule name and cron are required', 'error');
    return;
  }
  const payload = {
    name, cron,
    timezone: value('schedule-tz') || 'UTC',
    method: document.getElementById(prefix + '-schedule-method').value,
    path: value('schedule-path') || '/schedule',
    payload: value('schedule-payload'),
    enabled: true,
  };
  try {
    await api('/vlambda/' + currentSlug + '/schedules', {
      method: 'POST', headers: authHeaders(true), body: JSON.stringify(payload),
    });
    document.getElementById(prefix + '-schedule-name').value = '';
    document.getElementById(prefix + '-schedule-cron').value = '';
    document.getElementById(prefix + '-schedule-payload').value = '';
    await loadSchedules(currentSlug);
  } catch (e) { reportError(prefix === 'project' ? 'project-alert' : 'editor-alert', e); }
}
async function runSchedule(id) {
  try {
    await api('/vlambda/' + currentSlug + '/schedules/' + id + '/run', {
      method: 'POST', headers: authHeaders(false),
    });
    await loadSchedules(currentSlug);
  } catch (e) { reportError(schedulePrefix() === 'project' ? 'project-alert' : 'editor-alert', e); }
}
async function deleteSchedule(id) {
  try {
    await api('/vlambda/' + currentSlug + '/schedules/' + id, {
      method: 'DELETE', headers: authHeaders(false),
    });
    await loadSchedules(currentSlug);
  } catch (e) { reportError(schedulePrefix() === 'project' ? 'project-alert' : 'editor-alert', e); }
}

function metaPrefix() { return currentKind === 'project' ? 'project' : 'edit'; }
async function loadMeta(slug) {
  const prefix = metaPrefix();
  try {
    const data = await api('/vlambda/' + slug + '/meta');
    document.getElementById(prefix + '-summary').value = data.summary || '';
    document.getElementById(prefix + '-tags').value = data.tags || '';
    document.getElementById(prefix + '-description').value = data.description || '';
  } catch (e) { flog('warn', 'metadata load failed', e.message); }
}
async function saveMeta() {
  const prefix = metaPrefix();
  const payload = {
    summary: document.getElementById(prefix + '-summary').value,
    tags: document.getElementById(prefix + '-tags').value,
    description: document.getElementById(prefix + '-description').value,
  };
  try {
    await api('/vlambda/' + currentSlug + '/meta', {
      method: 'PUT', headers: authHeaders(true), body: JSON.stringify(payload),
    });
    showAlert(prefix === 'project' ? 'project-alert' : 'editor-alert', 'Metadata saved', 'success');
    await loadMeta(currentSlug);
  } catch (e) { reportError(prefix === 'project' ? 'project-alert' : 'editor-alert', e); }
}

function parseEnvLines(text) {
  const out = {};
  (text || '').split('\n').forEach(line => {
    if (!line.trim()) return;
    const i = line.indexOf('=');
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1);
  });
  return out;
}
function envLines(items, secrets) {
  return (items || []).filter(x => !!x.secret === secrets).map(x => x.key + '=' + x.value).join('\n');
}
async function loadEnv(slug) {
  try {
    const data = await api('/vlambda/' + slug + '/env');
    const items = data.env || [];
    for (const [prefix, kind] of [['edit', 'code'], ['project', 'project']]) {
      if (currentKind !== kind) continue;
      const v = document.getElementById(prefix + '-env');
      const s = document.getElementById(prefix + '-env-secrets');
      if (v) v.value = envLines(items, false);
      if (s) s.value = envLines(items, true);
    }
    envLoadedSlug = slug;
  } catch (e) { flog('warn', 'env load failed', e.message); }
}
async function saveEnv() {
  if (envLoadedSlug !== currentSlug) {
    showAlert(currentKind === 'project' ? 'project-alert' : 'editor-alert',
      'Environment is still loading; reopen the editor and try again', 'error');
    return;
  }
  const prefix = currentKind === 'project' ? 'project' : 'edit';
  const vars = parseEnvLines(document.getElementById(prefix + '-env').value);
  const secrets = parseEnvLines(document.getElementById(prefix + '-env-secrets').value);
  try {
    await api('/vlambda/' + currentSlug + '/env', {
      method: 'PUT',
      headers: authHeaders(true),
      body: JSON.stringify({ vars, secrets }),
    });
    showAlert(currentKind === 'project' ? 'project-alert' : 'editor-alert', 'Environment saved', 'success');
    await loadEnv(currentSlug);
  } catch (e) { reportError(currentKind === 'project' ? 'project-alert' : 'editor-alert', e); }
}

async function installPackages() {
  if (!currentSlug) return;
  let raw = '';
  if (currentKind === 'project') {
    raw = document.getElementById('project-reqs').value || '';
  } else {
    raw = document.getElementById('packages-input').value || '';
  }
  const packages = raw.split(/[\n,]/).map(x => x.trim()).filter(Boolean);
  if (!packages.length) { showAlert('editor-alert', 'Add at least one package', 'error'); return; }
  try {
    showAlert('editor-alert', 'Installing ' + packages.length + ' package(s)…', 'success');
    const data = await api('/vlambda/' + currentSlug + '/packages', {
      method: 'POST', headers: authHeaders(true), body: JSON.stringify({ packages })
    });
    if (data.error) throw new Error(data.error);
    showAlert('editor-alert', 'Installed ' + (data.installed || []).length + ' package(s)', 'success');
    await loadPackages();
    flog('info', 'packages installed', data.installed);
  } catch (e) { reportError('editor-alert', e); }
}

/* ---------- project editor ---------- */
async function openProject(slug) {
  currentSlug = slug; currentKind = 'project';
  showView('project');
  document.getElementById('project-title').textContent = 'Project ' + slug;
  setInvokeUrl('project-url', 'project-url-open', slug);
  try {
    const data = await api('/project/' + slug);
    projectFiles = data.files || {};
    document.getElementById('project-entry').value = data.entrypoint || 'main.py';
    document.getElementById('project-reqs').value = projectFiles['requirements.txt'] || '';
    const names = Object.keys(projectFiles).sort();
    activeProjectFile = names[0] || null;
    document.getElementById('project-filename').textContent = activeProjectFile || '—';
    document.getElementById('project-status-pos').textContent = 'Ln 1, Col 1';
    attachCursorStatus('project-editor', 'project-status-pos');
    renderProjectTree();
    renderProjectTabs();
    if (!projectEditor) projectEditor = VLambdaEditor.createEditor(document.getElementById('project-editor'), activeProjectFile ? projectFiles[activeProjectFile] : '');
    else VLambdaEditor.setValue(projectEditor, activeProjectFile ? projectFiles[activeProjectFile] : '');
    await loadPackages();
    await loadEnv(slug);
    await loadKV(slug);
    await loadEgress(slug);
    await loadSchedules(slug);
    await loadMeta(slug);
    flog('info', 'opened project', { slug, files: names.length });
  } catch (e) { reportError('project-alert', e); }
}
function renderProjectTree() {
  const tree = document.getElementById('file-tree');
  tree.innerHTML = Object.keys(projectFiles).sort().map(name =>
    '<div class="' + (name === activeProjectFile ? 'active' : '') + '" onclick="selectProjectFile(' + escapeHtml(JSON.stringify(name)) + ')">' + escapeHtml(name) + '</div>'
  ).join('');
}
function renderProjectTabs() {
  const tabs = document.getElementById('project-tabs');
  tabs.innerHTML = Object.keys(projectFiles).sort().map(name =>
    '<div class="tab ' + (name === activeProjectFile ? 'active' : '') + '" onclick="selectProjectFile(' + escapeHtml(JSON.stringify(name)) + ')">' + escapeHtml(name) + '</div>'
  ).join('');
}
function selectProjectFile(name) {
  if (activeProjectFile && projectEditor) projectFiles[activeProjectFile] = VLambdaEditor.getValue(projectEditor);
  activeProjectFile = name;
  if (projectEditor) VLambdaEditor.setValue(projectEditor, projectFiles[name] || '');
  document.getElementById('project-filename').textContent = name || '—';
  document.getElementById('project-status-pos').textContent = 'Ln 1, Col 1';
  renderProjectTree(); renderProjectTabs();
  flog('debug', 'selected file', name);
}
function newProjectFile() {
  const name = document.getElementById('new-file-name').value.trim();
  if (!name) return;
  projectFiles[name] = '';
  activeProjectFile = name;
  document.getElementById('new-file-name').value = '';
  renderProjectTree(); renderProjectTabs();
  if (projectEditor) VLambdaEditor.setValue(projectEditor, '');
  flog('info', 'new file', name);
}
async function saveProject() {
  if (activeProjectFile && projectEditor) projectFiles[activeProjectFile] = VLambdaEditor.getValue(projectEditor);
  projectFiles['requirements.txt'] = document.getElementById('project-reqs').value;
  const entrypoint = document.getElementById('project-entry').value || 'main.py';
  try {
    const data = await api('/project/' + currentSlug, {
      method: 'PUT', headers: authHeaders(true),
      body: JSON.stringify({ files: projectFiles, entrypoint })
    });
    showAlert('project-alert', 'Saved ' + (data.entrypoint || ''), 'success');
  } catch (e) { reportError('project-alert', e); }
}
async function runProject() {
  await saveProject();
  await invokeLambda(currentSlug, 'GET');
}

/* ---------- invoke / playground ---------- */
async function invokeLambda(slug, method) {
  method = method || 'GET';
  try {
    const opts = { method, headers: {} };
    if (method !== 'GET' && method !== 'DELETE') { opts.headers['Content-Type'] = 'application/json'; opts.body = '{}'; }
    const t0 = performance.now();
    const resp = await fetch('/f/' + slug, opts);
    const text = await resp.text();
    const ms = (performance.now() - t0).toFixed(1);
    const box = document.getElementById('run-result') || document.getElementById('project-result');
    if (box) box.innerHTML = '<div class="muted" style="margin-bottom:4px;">' + resp.status + ' · ' + ms + 'ms</div><pre>' + escapeHtml(pretty(text)) + '</pre>';
    flog('info', 'invoked ' + slug + ' -> ' + resp.status + ' (' + ms + 'ms)', text.slice(0, 200));
  } catch (e) { flog('error', 'invoke failed', e.message); reportError('dashboard-alert', e); }
}
function pretty(text) {
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch (e) { return text; }
}
function openInPlayground(slug, method) {
  showView('playground');
  document.getElementById('pg-slug').value = slug;
  document.getElementById('pg-method').value = method || 'GET';
}
function loadSampleRequest() {
  const first = lambdas[0];
  if (first) document.getElementById('pg-slug').value = first.slug;
}
async function sendPlayground() {
  const slug = document.getElementById('pg-slug').value.trim();
  const method = document.getElementById('pg-method').value;
  const alias = document.getElementById('pg-alias').value.trim();
  const query = document.getElementById('pg-query').value.trim();
  const headers = JSON.parse(document.getElementById('pg-headers').value || '{}');
  const body = document.getElementById('pg-body').value;
  let url = '/f/' + slug;
  const params = [];
  if (query) params.push(query);
  if (alias) params.push('alias=' + encodeURIComponent(alias));
  if (params.length) url += '?' + params.join('&');
  try {
    const t0 = performance.now();
    const opts = { method, headers };
    if (method !== 'GET' && method !== 'DELETE' && body) opts.body = body;
    const resp = await fetch(url, opts);
    const text = await resp.text();
    const ms = (performance.now() - t0).toFixed(1);
    document.getElementById('pg-meta').textContent = resp.status + ' · ' + ms + 'ms · ' + url;
    document.getElementById('pg-response').textContent = pretty(text);
    flog('info', 'playground ' + method + ' ' + url + ' -> ' + resp.status, text.slice(0, 200));
  } catch (e) {
    document.getElementById('pg-meta').textContent = 'error';
    document.getElementById('pg-response').textContent = String(e);
    flog('error', 'playground failed', e.message);
  }
}

/* ---------- delete ---------- */
async function deleteLambda(slug) {
  if (!confirm('Delete ' + slug + '?')) return;
  try {
    await api('/vlambda/' + slug, { method: 'DELETE', headers: authHeaders(false) });
    showAlert('dashboard-alert', 'Deleted ' + slug, 'success');
    loadLambdas();
  } catch (e) { reportError('dashboard-alert', e); }
}

/* ---------- export / import / duplicate ---------- */
async function exportLambda(slug) {
  try {
    const resp = await fetch(API + '/vlambda/' + encodeURIComponent(slug) + '/export', { headers: authHeaders(false) });
    if (!resp.ok) throw new Error('export failed: ' + resp.status);
    const text = await resp.text();
    const blob = new Blob([text], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = slug + '.vlambda.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    showAlert('dashboard-alert', 'Exported ' + slug, 'success');
  } catch (e) { reportError('dashboard-alert', e); }
}
async function importLambda(event) {
  const file = event.target.files[0];
  event.target.value = '';
  if (!file) return;
  try {
    const text = await file.text();
    const data = await api('/vlambda/import', {
      method: 'POST',
      body: text,
      headers: { 'Content-Type': 'application/json' },
    });
    showAlert('dashboard-alert', 'Imported as ' + data.slug, 'success');
    loadLambdas();
  } catch (e) { reportError('dashboard-alert', e); }
}
async function duplicateLambda(slug) {
  try {
    const data = await api('/vlambda/' + encodeURIComponent(slug) + '/duplicate', { method: 'POST' });
    showAlert('dashboard-alert', 'Duplicated as ' + data.slug, 'success');
    loadLambdas();
  } catch (e) { reportError('dashboard-alert', e); }
}

/* ---------- metrics ---------- */
async function loadMetricOptions() {
  try {
    const data = await api('/vlambda?format=json&limit=1000&offset=0');
    if (Array.isArray(data.lambdas)) {
      metricLambdas = data.lambdas;
    } else {
      metricLambdas = (data.lambdas || '').trim().split('\n').filter(Boolean).map(line => {
        const p = line.split('|');
        return { slug: p[0], name: p[1] || '', method: p[2] || 'GET' };
      });
    }
    const dl = document.getElementById('metrics-lambdas');
    dl.innerHTML = metricLambdas.map(l =>
      '<option value="' + escapeHtml(l.slug) + '">' + escapeHtml(l.name || l.slug) + '</option>' +
      (l.name ? '<option value="' + escapeHtml(l.name) + '">' + escapeHtml(l.slug) + '</option>' : '')
    ).join('');
    flog('info', 'metric lambda options loaded', { count: metricLambdas.length });
  } catch (e) { flog('warn', 'metric options failed', e.message); }
}

function resolveMetricsFilter() {
  const raw = (document.getElementById('metrics-filter').value || '').trim();
  if (!raw) return '';
  const bySlug = metricLambdas.find(l => l.slug === raw);
  if (bySlug) return bySlug.slug;
  const byName = metricLambdas.find(l => l.name && l.name.toLowerCase() === raw.toLowerCase());
  if (byName) return byName.slug;
  return raw;
}

async function loadMetrics() {
  if (metricsBusy) return;
  metricsBusy = true;
  const days = document.getElementById('metrics-days').value || '7';
  const filterSlug = resolveMetricsFilter();
  const filterQuery = filterSlug ? '&slug=' + encodeURIComponent(filterSlug) : '';
  try {
    const agg = await api('/metrics/aggregate?days=' + days + filterQuery);
    const stats = await api('/metrics/stats?days=' + days + filterQuery);
    document.getElementById('m-reqs').textContent = agg.requests_total || 0;
    document.getElementById('m-errs').textContent = agg.errors_total || 0;
    document.getElementById('m-cold').textContent = agg.cold_starts || 0;
    document.getElementById('m-p50').textContent = (stats.p50_ms || 0).toFixed(2);
    document.getElementById('m-p95').textContent = (stats.p95_ms || 0).toFixed(2);
    document.getElementById('m-p99').textContent = (stats.p99_ms || 0).toFixed(2);
    const labels = (agg.per_lambda || []).map(l => l.slug);
    const inv = (agg.per_lambda || []).map(l => l.invocations);
    const lat = (agg.per_lambda || []).map(l => l.latency_avg_ms);
    const invCanvas = document.getElementById('chart-inv');
    const latCanvas = document.getElementById('chart-lat');
    if (window.Chart && invCanvas && latCanvas) {
      // Reuse the existing charts so the canvases do not grow on each refresh.
      if (!chartInv) {
        const oldInv = Chart.getChart(invCanvas);
        if (oldInv) oldInv.destroy();
        chartInv = new Chart(invCanvas, {
          type: 'bar',
          data: { labels, datasets: [{ label: 'Invocations', data: inv, backgroundColor: '#1f6feb' }] },
          options: {
            responsive: true, maintainAspectRatio: false, animation: false,
            plugins: { legend: { display: false } },
            scales: { y: { beginAtZero: true, ticks: { precision: 0 } } }
          }
        });
      } else {
        chartInv.data.labels = labels;
        chartInv.data.datasets[0].data = inv;
        chartInv.update('none');
      }
      if (!chartLat) {
        const oldLat = Chart.getChart(latCanvas);
        if (oldLat) oldLat.destroy();
        chartLat = new Chart(latCanvas, {
          type: 'bar',
          data: { labels, datasets: [{ label: 'Avg ms', data: lat, backgroundColor: '#238636' }] },
          options: {
            responsive: true, maintainAspectRatio: false, animation: false,
            plugins: { legend: { display: false } },
            scales: { y: { beginAtZero: true } }
          }
        });
      } else {
        chartLat.data.labels = labels;
        chartLat.data.datasets[0].data = lat;
        chartLat.update('none');
      }
    }
    flog('info', 'metrics loaded', { days, filter: filterSlug || 'all', reqs: agg.requests_total, p99: stats.p99_ms });
  } catch (e) { reportError('metrics-alert', e); }
  finally { metricsBusy = false; }
}
async function exportMetrics() {
  const days = document.getElementById('metrics-days').value || '7';
  const filterSlug = resolveMetricsFilter();
  try {
    const resp = await fetch('/metrics/export?days=' + days + (filterSlug ? '&slug=' + encodeURIComponent(filterSlug) : ''));
    const text = await resp.text();
    const blob = new Blob([text], { type: 'text/csv' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'vlambda-metrics-' + days + 'd.csv';
    a.click();
    flog('info', 'metrics exported', { days, bytes: text.length });
  } catch (e) { reportError('metrics-alert', e); }
}
async function deleteMetrics(all) {
  const days = document.getElementById('metrics-days').value || '7';
  const slug = resolveMetricsFilter();
  const scope = slug ? ' for ' + slug : '';
  const what = all ? 'ALL metrics' : 'metrics older than ' + days + ' days';
  if (!confirm('Delete ' + what + scope + '? This cannot be undone.')) return;
  try {
    const data = await api('/metrics/history?days=' + (all ? 0 : days) + (slug ? '&slug=' + encodeURIComponent(slug) : ''),
      { method: 'DELETE' });
    showAlert('metrics-alert', 'Deleted ' + (data.deleted || 0) + ' metric rows', 'success');
    await loadMetrics();
  } catch (e) { reportError('metrics-alert', e); }
}

/* ---------- logs ---------- */
async function loadLogs() {
  const days = document.getElementById('logs-days').value || '7';
  const slug = document.getElementById('logs-slug').value.trim();
  try {
    const q = '/metrics/history?days=' + days + (slug ? '&slug=' + encodeURIComponent(slug) : '');
    const data = await api(q);
    const tbody = document.getElementById('logs-list');
    detailItems = data.items || [];
    tbody.innerHTML = detailItems.map((it, idx) => {
      const ts = new Date(it.ts * 1000).toLocaleString();
      return '<tr><td class="muted">' + ts + '</td>' +
        '<td class="mono">' + escapeHtml(it.request_id || '') + '</td>' +
        '<td class="mono">' + escapeHtml(it.slug) + '</td>' +
        '<td>' + escapeHtml(it.method) + '</td>' +
        '<td>' + (it.latency_ms || 0).toFixed(2) + ' ms</td>' +
        '<td style="color:#f85149;">' + escapeHtml(it.error_type || '') + '</td>' +
        '<td><button class="btn btn-sm" onclick="showDetailAt(' + idx + ')">View</button></td></tr>';
    }).join('') || '<tr><td colspan="7" class="muted">No invocations</td></tr>';
    const logData = await api('/logs/history?days=' + days + (slug ? '&slug=' + encodeURIComponent(slug) : ''));
    const lbody = document.getElementById('lambda-logs-list');
    lbody.innerHTML = (logData.items || []).map(l =>
      '<tr><td class="muted">' + new Date(l.ts * 1000).toLocaleString() + '</td>' +
      '<td class="mono">' + escapeHtml(l.slug) + '</td>' +
      '<td class="mono">' + escapeHtml(l.request_id || '') + '</td>' +
      '<td>' + escapeHtml(l.level) + '</td>' +
      '<td class="mono">' + escapeHtml(l.message) + '</td></tr>'
    ).join('') || '<tr><td colspan="5" class="muted">No lambda logs</td></tr>';
    flog('info', 'logs loaded', { count: (data.items || []).length, lambda_logs: (logData.items || []).length });
  } catch (e) { reportError('logs-alert', e); }
}
function showDetailAt(idx) {
  const item = detailItems[idx];
  document.getElementById('detail-body').textContent = JSON.stringify(item, null, 2);
  show('detail-modal');
}
async function deleteLogs(all) {
  const days = document.getElementById('logs-days').value || '7';
  const slug = document.getElementById('logs-slug').value.trim();
  const scope = slug ? ' for ' + slug : '';
  const what = all ? 'ALL logs' : 'logs older than ' + days + ' days';
  if (!confirm('Delete ' + what + scope + '? This cannot be undone.')) return;
  try {
    const data = await api('/logs/history?days=' + (all ? 0 : days) + (slug ? '&slug=' + encodeURIComponent(slug) : ''),
      { method: 'DELETE' });
    showAlert('logs-alert', 'Deleted ' + (data.deleted || 0) + ' log rows', 'success');
    await loadLogs();
  } catch (e) { reportError('logs-alert', e); }
}

/* ---------- system ---------- */
async function loadSystem() {
  try {
    const health = await fetch('/health').then(r => r.json());
    const ready = await fetch('/ready').then(r => r.json());
    const pool = await api('/pool/stats');
    const dlq = await api('/dlq');
    document.getElementById('sys-health').textContent = JSON.stringify(health, null, 2);
    document.getElementById('sys-ready').textContent = JSON.stringify(ready, null, 2);
    document.getElementById('sys-pool').textContent = JSON.stringify(pool, null, 2);
    document.getElementById('sys-dlq').textContent = JSON.stringify(dlq, null, 2);
    const ok = health.status === 'ok' && ready.status === 'ready';
    document.getElementById('status-pill').textContent = ok ? 'online' : 'degraded';
    document.getElementById('status-pill').style.background = ok ? '#238636' : '#9e6a03';
    await loadTokens();
    flog('info', 'system loaded', { health: health.status, ready: ready.status, pool });
  } catch (e) { reportError('system-alert', e); }
}

/* ---------- API tokens ---------- */
function formatUnix(value) {
  if (!value) return '';
  const n = Number(value);
  if (!Number.isFinite(n)) return value;
  return new Date(n * 1000).toLocaleString();
}
async function loadTokens() {
  try {
    const data = await api('/tokens');
    const tbody = document.getElementById('token-list');
    tbody.innerHTML = '';
    (data.tokens || []).forEach(t => {
      const tr = document.createElement('tr');
      tr.innerHTML = '<td class="mono">' + t.id + '</td>' +
        '<td>' + escapeHtml(t.name || '—') + '</td>' +
        '<td class="mono">' + escapeHtml(t.scopes) + '</td>' +
        '<td class="muted">' + escapeHtml(formatUnix(t.created_at)) + '</td>' +
        '<td class="muted">' + (t.last_used_at ? escapeHtml(formatUnix(t.last_used_at)) : 'never') + '</td>' +
        '<td class="muted">' + (t.expires_at ? escapeHtml(formatUnix(t.expires_at)) : 'never') + '</td>' +
        '<td>' + (t.revoked ? '<span class="badge badge-DELETE">revoked</span>' : (t.expired ? '<span class="badge badge-DELETE">expired</span>' : '<span class="badge badge-GET">active</span>')) + '</td>' +
        '<td>' + (t.revoked ? '' : '<button class="btn btn-sm btn-danger" onclick="revokeToken(' + t.id + ')">Revoke</button>') + '</td>';
      tbody.appendChild(tr);
    });
  } catch (e) { reportError('system-alert', e); }
}
async function createToken() {
  const name = document.getElementById('token-name').value.trim() || 'token';
  const scopes = document.getElementById('token-scopes').value;
  const expiresIn = parseInt(document.getElementById('token-expiry').value, 10) || 0;
  try {
    const data = await api('/tokens', {
      method: 'POST',
      body: JSON.stringify({ name, scopes: scopes.split(','), expires_in: expiresIn }),
      headers: { 'Content-Type': 'application/json' },
    });
    document.getElementById('token-new').innerHTML =
      'Copy it now: <strong>' + escapeHtml(data.token) + '</strong> ' +
      '<button class="btn btn-sm" onclick="copyText(\'' + data.token + '\')">Copy</button>';
    document.getElementById('token-name').value = '';
    showAlert('system-alert', 'Token created', 'success');
    await loadTokens();
  } catch (e) { reportError('system-alert', e); }
}
async function revokeToken(id) {
  if (!confirm('Revoke token ' + id + '?')) return;
  try {
    await api('/tokens/' + id, { method: 'DELETE' });
    showAlert('system-alert', 'Token revoked', 'success');
    await loadTokens();
  } catch (e) { reportError('system-alert', e); }
}
async function changePassword() {
  const oldPassword = document.getElementById('pw-old').value;
  const newPassword = document.getElementById('pw-new').value;
  if (!oldPassword || !newPassword) {
    showAlert('system-alert', 'Enter the current and new password', 'error');
    return;
  }
  if (newPassword.length < 12) {
    showAlert('system-alert', 'The new password must be at least 12 characters', 'error');
    return;
  }
  try {
    await api('/password', { method: 'POST', body: oldPassword + ':' + newPassword });
    document.getElementById('pw-old').value = '';
    document.getElementById('pw-new').value = '';
    token = null;
    localStorage.removeItem('vlambda_token');
    checkAuth();
    showAlert('login-alert', 'Password changed. Please login again.', 'success');
  } catch (e) { reportError('system-alert', e); }
}

/* templates */
const TEMPLATES = {
  hello: 'def handler(request):\n    return {"msg": "hello", "path": request.path}\n',
  json: 'def handler(request):\n    return {\n        "method": request.method,\n        "path": request.path,\n        "query": request.query_string,\n        "body": request.body.decode("utf-8", "replace"),\n    }\n',
  cors: 'def handler(request):\n    # Set VLAMBDA_CORS=* (or an explicit origin) to enable CORS for /f/<slug>.\n    return {\n        "ok": True,\n        "method": request.method,\n        "items": [{"id": 1, "name": "first"}, {"id": 2, "name": "second"}],\n    }\n',
  webhook: 'import json\n\ndef handler(request):\n    try:\n        payload = json.loads(request.body or b"{}")\n    except ValueError:\n        payload = {"raw": request.body.decode("utf-8", "replace")}\n    request.log("webhook received: " + json.dumps(payload)[:200])\n    return {"received": True, "payload": payload}\n',
  form: 'from urllib.parse import parse_qs\n\ndef handler(request):\n    form = parse_qs(request.body.decode("utf-8", "replace"))\n    return {"fields": {k: v[0] for k, v in form.items()}}\n',
  echo: 'def handler(request):\n    return {"echo": request.body, "headers": request.headers}\n',
  math: 'def handler(request):\n    n = int(request.query_string.split("n=")[-1] or 10)\n    return {"sum": sum(range(n)), "n": n}\n',
  html: 'def handler(request):\n    items = ["alpha", "beta", "gamma"]\n    rows = "\\n    ".join("<li>{}</li>".format(item) for item in items)\n    page = """<!DOCTYPE html>\n<html lang="en">\n<head>\n  <meta charset="utf-8">\n  <title>Hello from vlambda</title>\n</head>\n<body>\n  <h1>Hello from vlambda</h1>\n  <p>Path: {path}</p>\n  <ul>\n    {rows}\n  </ul>\n</body>\n</html>""".format(path=request.path, rows=rows)\n    return page\n',
  logging: 'def handler(request):\n    request.log("processing " + request.path)\n    request.log("method: " + request.method, "debug")\n    return {"ok": True}\n',
  async: 'async def lookup(item):\n    # Any awaitable works here (an async database or HTTP client, etc.).\n    return {"id": item, "ok": True}\n\nasync def handler(request):\n    # Handlers may be async; vlambda awaits the coroutine for you.\n    request.log("async handler start")\n    items = [await lookup(i) for i in range(3)]\n    return {"async": True, "items": items, "path": request.path}\n',
};
function applyTemplate() {
  const name = document.getElementById('template-select').value;
  if (createEditor) VLambdaEditor.setValue(createEditor, TEMPLATES[name] || TEMPLATES.hello);
  flog('info', 'template applied', name);
}

/* theme */
function toggleTheme() {
  document.body.classList.toggle('light');
  localStorage.setItem('vlambda_theme', document.body.classList.contains('light') ? 'light' : 'dark');
  flog('info', 'theme toggled', document.body.classList.contains('light') ? 'light' : 'dark');
}
if (localStorage.getItem('vlambda_theme') === 'light') document.body.classList.add('light');

/* keyboard shortcuts */
document.addEventListener('keydown', e => {
  if (!(e.ctrlKey || e.metaKey)) return;
  if (e.key === 's') {
    if (!document.getElementById('view-editor').classList.contains('hidden')) { e.preventDefault(); saveCodeLambda(); }
    else if (!document.getElementById('view-project').classList.contains('hidden')) { e.preventDefault(); saveProject(); }
  }
  if (e.key === 'Enter') {
    if (!document.getElementById('view-editor').classList.contains('hidden')) { e.preventDefault(); runCurrent(); }
    else if (!document.getElementById('view-project').classList.contains('hidden')) { e.preventDefault(); runProject(); }
  }
});

/* live metrics while the view is open */
setInterval(() => {
  if (!document.getElementById('view-metrics').classList.contains('hidden')) loadMetrics();
}, 5000);

/* init */
checkAuth();
loadConfig();
setTimeout(() => {
  fetch('/health').then(r => r.json()).then(d => {
    document.getElementById('status-pill').textContent = d.status === 'ok' ? 'online' : 'degraded';
    document.getElementById('status-pill').style.background = d.status === 'ok' ? '#238636' : '#9e6a03';
  }).catch(() => flog('error', 'health check failed'));
}, 0);
