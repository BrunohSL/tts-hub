// Painel ADM do TTS Hub. Vanilla, sem build.
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let status = null;          // último /admin/api/status
let renderedOrder = '';     // ordem renderizada (rebuild só quando muda)
let refreshTimer = null;

async function api(path, { method = 'GET', body, raw = false } = {}) {
  const res = await fetch(`/admin/api${path}`, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
    credentials: 'same-origin',
  });
  if (res.status === 401 && path !== '/login') {
    showLogin();
    throw new Error('Sessão expirada');
  }
  if (raw) return res;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(errorText(data) || `HTTP ${res.status}`);
  return data;
}

function errorText(data) {
  if (!data) return '';
  if (typeof data.detail === 'string') return data.detail;
  if (Array.isArray(data.detail)) return data.detail.map((d) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join('\n');
  if (data.attempts) {
    const lines = data.attempts.map((a) => `• ${a.provider}: ${a.error}`);
    return [data.error, ...lines].join('\n');
  }
  return data.error || '';
}

const fmtTime = (epoch) => (epoch ? new Date(epoch * 1000).toLocaleString('pt-BR') : '—');
const fmtIso = (iso) => (iso ? new Date(iso).toLocaleString('pt-BR') : '—');
const fmtDur = (s) => (s < 60 ? `${s}s` : s < 3600 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);

// ------------------------------------------------------------------ login

function showLogin() {
  clearInterval(refreshTimer);
  $('#app-view').hidden = true;
  $('#login-view').hidden = false;
}

async function showApp(user) {
  $('#login-view').hidden = true;
  $('#app-view').hidden = false;
  $('#who').textContent = `${user.name} · ${user.role}${user.source === 'local' ? ' (acesso local)' : ''}`;
  await Promise.all([loadStatus(), loadKeys(), loadAudit()]);
  clearInterval(refreshTimer);
  refreshTimer = setInterval(() => loadStatus().catch(() => {}), 10000);
}

$('#login-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  const errEl = $('#login-error');
  errEl.hidden = true;
  form.querySelector('button').disabled = true;
  try {
    const { user } = await api('/login', { method: 'POST', body: { email: form.email.value, password: form.password.value } });
    form.password.value = '';
    await showApp(user);
  } catch (err) {
    errEl.textContent = err.message;
    errEl.hidden = false;
  } finally {
    form.querySelector('button').disabled = false;
  }
});

$('#logout').addEventListener('click', async () => {
  await api('/logout', { method: 'POST' }).catch(() => {});
  showLogin();
});

// -------------------------------------------------------------- providers

async function loadStatus() {
  status = await api('/status');
  $('#instance').textContent = status.instance;
  const order = status.providers.map((p) => `${p.name}:${p.config.enabled}`).join(',');
  if (order !== renderedOrder) {
    renderProviders();
    renderTestOptions();
    renderedOrder = order;
  } else {
    status.providers.forEach(updateProviderState);
  }
}

function stateView(p) {
  const s = p.state;
  if (!p.config.enabled) return { cls: 'off', text: 'Desabilitado' };
  if (!s.healthy) return { cls: 'bad', text: `Caído há ${fmtDur(s.down_for_s)}` };
  if (s.last_probe_ok === null && s.ok_count === 0) return { cls: 'warn', text: 'Sem teste ainda' };
  return { cls: 'ok', text: 'Saudável' };
}

function roleOf(p) {
  if (!p.config.enabled) return '';
  const enabled = status.providers.filter((x) => x.config.enabled);
  const idx = enabled.findIndex((x) => x.name === p.name);
  return idx === 0 ? 'Principal' : `Fallback ${idx}`;
}

function statsHtml(p) {
  const s = p.state;
  const queue = s.queue_waiting !== null ? ` · fila <b>${s.queue_waiting}</b> (rodando ${s.queue_active})` : '';
  return `
    <span>Última latência <b>${s.last_latency_ms != null ? `${s.last_latency_ms} ms` : '—'}</b></span>
    <span>Último sucesso <b>${fmtTime(s.last_ok_at)}</b></span>
    <span>Último probe <b>${fmtTime(s.last_probe_at)}</b> ${s.last_probe_ok === true ? '✓' : s.last_probe_ok === false ? '✗' : ''}</span>
    <span>Desde o boot: <b>${s.ok_count}</b> ok · <b>${s.error_count}</b> erro · <b>${s.fallback_count}</b> como fallback${queue}</span>`;
}

function updateProviderState(p) {
  const card = document.querySelector(`.provider[data-name="${p.name}"]`);
  if (!card) return;
  const v = stateView(p);
  const pill = card.querySelector('.pill.state');
  pill.className = `pill state ${v.cls}`;
  pill.textContent = v.text;
  card.querySelector('.stats').innerHTML = statsHtml(p);
  const errEl = card.querySelector('.last-error');
  const showErr = p.state.last_error && !p.state.healthy;
  errEl.hidden = !showErr;
  if (showErr) errEl.textContent = `${fmtTime(p.state.last_error_at)} — ${p.state.last_error}`;
}

function renderProviders() {
  const wrap = $('#providers');
  const total = status.providers.length;
  wrap.innerHTML = status.providers.map((p, i) => {
    const voices = Object.entries(p.voices);
    const role = roleOf(p);
    return `
    <article class="card provider ${p.config.enabled ? '' : 'disabled'}" data-name="${esc(p.name)}">
      <div class="provider-head">
        <span class="pos">${i + 1}</span>
        <h3>${esc(p.label)}</h3>
        ${role ? `<span class="pill role">${role}</span>` : ''}
        <span class="pill state"></span>
        <span class="spacer"></span>
        <div class="actions">
          <button data-act="up" ${i === 0 ? 'disabled' : ''} title="Subir na cadeia">↑</button>
          <button data-act="down" ${i === total - 1 ? 'disabled' : ''} title="Descer na cadeia">↓</button>
          <button data-act="probe">Testar agora</button>
        </div>
      </div>
      <p class="muted">${esc(p.description)} · saída nativa ${esc(p.native_format)}${p.max_concurrent ? ` · ${p.max_concurrent} por vez` : ''}</p>
      <div class="stats"></div>
      <div class="last-error" hidden></div>
      <form class="settings">
        <label class="toggle"><input type="checkbox" name="enabled" ${p.config.enabled ? 'checked' : ''}> Habilitado</label>
        <label>Timeout (s) <input type="number" name="timeout_s" min="1" max="300" step="0.5" value="${p.config.timeout_s}"></label>
        <label>Cooldown (s) <input type="number" name="cooldown_s" min="5" max="3600" value="${p.config.cooldown_s}"></label>
        <label>Velocidade (%) <input type="number" name="speed" min="-50" max="50" value="${p.config.speed}"></label>
        <label>Voz padrão
          <select name="voice">
            <option value="">padrão (${esc(p.default_voice)})</option>
            ${voices.map(([alias, id]) => `<option value="${esc(alias)}" ${p.config.voice === alias || p.config.voice === id ? 'selected' : ''}>${esc(alias)} — ${esc(id)}</option>`).join('')}
          </select>
        </label>
        <button type="submit" class="primary">Salvar</button>
      </form>
      <p class="error form-error" hidden></p>
    </article>`;
  }).join('');
  status.providers.forEach(updateProviderState);
}

$('#providers').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button[data-act]');
  if (!btn) return;
  const card = btn.closest('.provider');
  const name = card.dataset.name;
  const errEl = card.querySelector('.form-error');
  errEl.hidden = true;
  btn.disabled = true;
  try {
    if (btn.dataset.act === 'probe') {
      btn.textContent = 'Testando…';
      const r = await api(`/providers/${name}/probe`, { method: 'POST' });
      if (!r.ok) throw new Error(`Probe falhou: ${r.error}`);
    } else {
      const names = status.providers.map((p) => p.name);
      const i = names.indexOf(name);
      const j = btn.dataset.act === 'up' ? i - 1 : i + 1;
      [names[i], names[j]] = [names[j], names[i]];
      await api('/providers/order', { method: 'PUT', body: { names } });
      renderedOrder = '';
      loadAudit();
    }
    await loadStatus();
  } catch (err) {
    errEl.textContent = err.message;
    errEl.hidden = false;
  } finally {
    btn.disabled = false;
    if (btn.dataset.act === 'probe') btn.textContent = 'Testar agora';
  }
});

$('#providers').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  const card = form.closest('.provider');
  const errEl = card.querySelector('.form-error');
  errEl.hidden = true;
  try {
    await api(`/providers/${card.dataset.name}`, {
      method: 'PATCH',
      body: {
        enabled: form.enabled.checked,
        timeout_s: Number(form.timeout_s.value),
        cooldown_s: Number(form.cooldown_s.value),
        speed: Number(form.speed.value),
        voice: form.voice.value || null,
      },
    });
    renderedOrder = '';
    await Promise.all([loadStatus(), loadAudit()]);
  } catch (err) {
    errEl.textContent = err.message;
    errEl.hidden = false;
  }
});

// ------------------------------------------------------------------ teste

function renderTestOptions() {
  const sel = $('#test-provider');
  const current = sel.value;
  sel.innerHTML = [
    '<option value="auto">auto — cadeia com fallback</option>',
    '<option value="primary">primary — só o principal</option>',
    ...status.providers.map((p) => `<option value="${esc(p.name)}">${esc(p.label)}${p.config.enabled ? '' : ' (desabilitado)'}</option>`),
  ].join('');
  if (current) sel.value = current;

  const fmt = $('#test-format');
  const curFmt = fmt.value;
  fmt.innerHTML = status.formats.map((f) => `<option value="${esc(f)}">${esc(f)}</option>`).join('');
  fmt.value = curFmt || (status.formats.includes('mp3') ? 'mp3' : status.formats[0]);

  const voices = new Set();
  status.providers.forEach((p) => Object.entries(p.voices).forEach(([a, id]) => { voices.add(a); voices.add(id); }));
  $('#voice-list').innerHTML = [...voices].map((v) => `<option value="${esc(v)}">`).join('');
}

let lastAudioUrl = null;
$('#test-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  const btn = form.querySelector('button[type=submit]');
  const errEl = $('#test-error');
  const meta = $('#test-meta');
  const player = $('#test-audio');
  errEl.hidden = true;
  meta.textContent = 'Gerando…';
  btn.disabled = true;
  try {
    const body = { text: form.text.value, provider: form.provider.value, format: form.format.value };
    if (form.voice.value.trim()) body.voice = form.voice.value.trim();
    if (form.speed.value !== '') body.speed = Number(form.speed.value);
    const res = await api('/test', { method: 'POST', body, raw: true });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(errorText(data) || `HTTP ${res.status}`);
    }
    const blob = await res.blob();
    if (lastAudioUrl) URL.revokeObjectURL(lastAudioUrl);
    lastAudioUrl = URL.createObjectURL(blob);
    player.src = lastAudioUrl;
    player.hidden = false;
    player.play().catch(() => {});
    const fb = res.headers.get('X-TTS-Fallback') === 'true' ? ' · FALLBACK' : '';
    meta.textContent = `${res.headers.get('X-TTS-Provider')} · ${res.headers.get('X-TTS-Voice')} · ${res.headers.get('X-TTS-Duration-Ms')} ms · ${(blob.size / 1024).toFixed(0)} KB${fb}`;
    loadStatus().catch(() => {});
  } catch (err) {
    meta.textContent = '';
    errEl.textContent = err.message;
    errEl.hidden = false;
  } finally {
    btn.disabled = false;
  }
});

// ------------------------------------------------------------------ chaves

async function loadKeys() {
  const { keys } = await api('/keys');
  $('#keys tbody').innerHTML = keys.map((k) => `
    <tr class="${k.revoked_at ? 'revoked' : ''}" data-id="${k.id}">
      <td>${esc(k.name)}</td>
      <td><code>${esc(k.prefix)}…</code></td>
      <td>${k.revoked_at ? k.priority : `<input type="number" min="-100" max="100" value="${k.priority}" data-act="priority">`}</td>
      <td>${fmtIso(k.last_used_at)}</td>
      <td title="${esc(k.created_by || '')}">${fmtIso(k.created_at)}</td>
      <td>${k.revoked_at ? `revogada ${fmtIso(k.revoked_at)}` : '<button class="danger" data-act="revoke">Revogar</button>'}</td>
    </tr>`).join('') || '<tr><td colspan="6" class="muted">Nenhuma chave ainda.</td></tr>';
}

$('#key-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  try {
    const created = await api('/keys', { method: 'POST', body: { name: form.name.value, priority: Number(form.priority.value) } });
    $('#new-key-value').textContent = created.key;
    $('#new-key').hidden = false;
    form.name.value = '';
    await Promise.all([loadKeys(), loadAudit()]);
  } catch (err) {
    alert(err.message);
  }
});

$('#copy-key').addEventListener('click', () => {
  navigator.clipboard.writeText($('#new-key-value').textContent).then(() => { $('#copy-key').textContent = 'Copiada'; });
});

$('#keys').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button[data-act=revoke]');
  if (!btn) return;
  const row = btn.closest('tr');
  if (!confirm(`Revogar a chave "${row.cells[0].textContent}"? O consumidor para de conseguir gerar áudio na hora.`)) return;
  await api(`/keys/${row.dataset.id}`, { method: 'DELETE' }).catch((err) => alert(err.message));
  await Promise.all([loadKeys(), loadAudit()]);
});

$('#keys').addEventListener('change', async (ev) => {
  const input = ev.target.closest('input[data-act=priority]');
  if (!input) return;
  const id = input.closest('tr').dataset.id;
  await api(`/keys/${id}`, { method: 'PATCH', body: { priority: Number(input.value) } }).catch((err) => alert(err.message));
  loadAudit();
});

// --------------------------------------------------------------- auditoria

const ACTIONS = {
  'provider.update': (d) => `alterou ${d.provider}: ${Object.entries(d).filter(([k]) => k !== 'provider').map(([k, v]) => `${k}=${v}`).join(', ')}`,
  'provider.reorder': (d) => `reordenou a cadeia: ${d.to.join(' → ')}`,
  'key.create': (d) => `criou a chave "${d.name}" (prioridade ${d.priority})`,
  'key.update': (d) => `mudou a prioridade da chave #${d.id} para ${d.priority}`,
  'key.revoke': (d) => `revogou a chave #${d.id}`,
};

async function loadAudit() {
  const { entries } = await api('/audit');
  $('#audit').innerHTML = entries.map((e) => {
    const text = (ACTIONS[e.action] || (() => e.action))(e.detail);
    return `<li><time>${fmtIso(e.at)}</time><span><b>${esc(e.actor)}</b> ${esc(text)}</span></li>`;
  }).join('') || '<li class="muted">Nada ainda.</li>';
}

// ------------------------------------------------------------------- boot

api('/me').then(({ user }) => showApp(user)).catch(showLogin);
