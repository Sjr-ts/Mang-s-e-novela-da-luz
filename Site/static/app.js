/* ==========================================================
   Baixador de Manga — frontend
   ========================================================== */
'use strict';

const $ = (sel, ctx = document) => ctx.querySelector(sel);
const $$ = (sel, ctx = document) => [...ctx.querySelectorAll(sel)];

const LANG_LABELS = {
  'pt-br': 'Português (Brasil)',
  'en': 'English',
  'es': 'Español',
  'es-la': 'Español (Latino)',
  'fr': 'Français',
  'id': 'Indonesia',
  'de': 'Deutsch',
  'it': 'Italiano',
  'tr': 'Türkçe',
};

const state = {
  url: '',
  manga: null,
  lang: 'pt-br',
  chapters: [],
  volumes: [],
  activeTab: 'chapters',
  selected: new Map(),
  jobId: null,
  es: null,
  page: 'download',
  system: null,
};

/* ---------------- helpers ---------------- */
function toast(msg, kind = 'ok', ms = 4200) {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $('#toastWrap').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transform = 'translateX(24px)'; }, ms - 350);
  setTimeout(() => el.remove(), ms);
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function fmtBytes(bytes) {
  if (!bytes) return '0 MB';
  const units = ['B', 'KB', 'MB', 'GB'];
  let v = bytes, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

function fmtEta(seconds) {
  if (seconds == null || !isFinite(seconds)) return 'calculando…';
  const s = Math.max(1, Math.round(seconds));
  if (s < 60) return `~${s}s restantes`;
  const m = Math.floor(s / 60), rem = s % 60;
  if (m < 60) return `~${m}min ${rem.toString().padStart(2, '0')}s restantes`;
  const h = Math.floor(m / 60);
  return `~${h}h ${String(m % 60).padStart(2, '0')}min restantes`;
}

async function fetchJson(url, opts = {}) {
  const res = await fetch(url, opts);
  let data = null;
  try { data = await res.json(); } catch { /* corpo não-JSON */ }
  if (!res.ok || !data || data.ok === false) {
    const msg = (data && data.error) ? data.error : `Erro HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data;
}

/* ---------------- sistema (IA / formatos) ---------------- */
async function loadSystem() {
  try {
    state.system = await fetchJson('/api/system');
  } catch {
    state.system = { ok: true, ai: { available: false, models: {} } };
  }
  // desabilita opções de IA se o Waifu2x não estiver instalado
  // popula seletor de sites para busca por nome
  const siteSel = $('#siteSearchSel');
  if (siteSel && state.system.sites) {
    state.system.sites.forEach(s => {
      if (!s.available) return;
      const o = document.createElement('option');
      o.value = s.key; o.textContent = s.label;
      siteSel.appendChild(o);
    });
  }
  const aiModels = (state.system.ai && state.system.ai.models) || {};
  $$('#resSel option[data-ai]').forEach(opt => {
    const mode = opt.dataset.ai;
    const available = !!aiModels[mode];
    opt.disabled = !available;
    opt.textContent = available
      ? opt.textContent
      : opt.textContent.replace(/\s*\([^)]*\)\s*$/, '') + ' — não instalado';
  });
}
loadSystem();

/* mostra/oculta "manter imagens" conforme o formato escolhido */
const formatSel = $('#formatSel');
const keepRow = $('#keepRow');
formatSel.addEventListener('change', () => {
  keepRow.classList.toggle('hidden', formatSel.value === 'images');
});

/* ---------------- desligar o servidor ---------------- */
$('#shutdownBtn').addEventListener('click', async () => {
  if (!window.confirm('Desligar o Baixador de Manga?\n(O que estiver baixando agora será interrompido.)')) return;
  try {
    await fetchJson('/api/shutdown', { method: 'POST' });
    document.body.innerHTML =
      '<div style="min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;font-family:Segoe UI,sans-serif;background:#0e0f13;color:#eef1f6">' +
      '<h1 style="margin:0;font-size:22px">Servidor desligado</h1>' +
      '<p style="margin:0;color:#a9b1c2">Pode fechar esta aba. Para abrir de novo, clique duas vezes em <strong>Baixar Manga (Site).bat</strong>.</p>' +
      '</div>';
  } catch (err) {
    toast('Não consegui desligar: ' + err.message, 'err');
  }
});

/* ---------------- navegação ---------------- */
$$('.nav-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const page = btn.dataset.page;
    state.page = page;
    $$('.nav-btn').forEach(b => b.classList.toggle('active', b === btn));
    $$('.page').forEach(p => p.classList.toggle('active', p.id === `page-${page}`));
    if (page === 'library') loadLibrary();
  });
});

/* ---------------- busca de mangá ---------------- */
const urlInput = $('#urlInput');
const searchBtn = $('#searchBtn');

$('#pasteBtn').addEventListener('click', async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (text) { urlInput.value = text.trim(); toast('Link colado.', 'ok', 1800); }
  } catch {
    toast('Permita acesso à área de transferência.', 'err');
  }
});

urlInput.addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });
searchBtn.addEventListener('click', doSearch);

async function doSearch() {
  const raw = urlInput.value.trim();
  if (!raw) { toast('Cole o link ou digite o nome do mangá.', 'err'); return; }
  const isUrl = /^https?:\/\//i.test(raw);
  searchBtn.disabled = true;
  searchBtn.innerHTML = '<span>Buscando…</span>';
  try {
    if (isUrl) {
      const data = await fetchJson(`/api/info?url=${encodeURIComponent(raw)}&lang=${encodeURIComponent(state.lang)}`);
      applyInfo(data);
      toast(`${data.manga.title} — ${data.chapters.length} capítulos encontrados.`, 'ok');
    } else {
      // busca por nome
      const site = ($('#siteSearchSel') ? $('#siteSearchSel').value : '') || '';
      const data = await fetchJson(`/api/search?q=${encodeURIComponent(raw)}${site ? '&site=' + encodeURIComponent(site) : ''}`);
      renderSearchResults(data.results || []);
      if (!(data.results || []).length) toast('Nenhum resultado. Tente outro termo ou cole o link.', 'err', 4000);
    }
  } catch (err) {
    toast(err.message, 'err', 6000);
  } finally {
    searchBtn.disabled = false;
    searchBtn.innerHTML = '<span>Buscar</span>';
  }
}

function renderSearchResults(results) {
  let box = $('#searchResults');
  if (!box) {
    box = document.createElement('div');
    box.id = 'searchResults';
    box.className = 'search-results';
    $('#resultCard').before ? $('#resultCard').before(box) : $('#resultCard').parentNode.insertBefore(box, $('#resultCard'));
  }
  if (!results.length) { box.innerHTML = ''; box.classList.add('hidden'); return; }
  box.classList.remove('hidden');
  box.innerHTML = results.map(r => `
    <div class="search-result" data-url="${escapeHtml(r.url)}">
      ${r.cover ? `<img src="/api/cover?u=${encodeURIComponent(r.cover)}" loading="lazy" alt="">` : '<div class="sr-ph"></div>'}
      <div class="sr-meta">
        <div class="sr-title">${escapeHtml(r.title)}</div>
        <div class="sr-site">${escapeHtml(r.site_label || r.site)}</div>
      </div>
    </div>`).join('');
  box.querySelectorAll('.search-result').forEach(el => {
    el.addEventListener('click', () => {
      urlInput.value = el.dataset.url;
      box.classList.add('hidden');
      doSearch();
    });
  });
}

function applyInfo(data) {
  state.manga = data.manga;
  state.url = data.manga.url;
  state.lang = data.lang;
  state.chapters = data.chapters || [];
  state.volumes = data.volumes || [];
  state.selected.clear();

  // capa + metadados
  $('#coverImg').src = data.manga.cover
    ? `/api/cover?u=${encodeURIComponent(data.manga.cover)}` : '';
  $('#mTitle').textContent = data.manga.title;
  // badge do site
  const siteBadge = $('#siteBadge');
  if (siteBadge) siteBadge.textContent = data.site_label || data.manga.site_label || '';
  const alt = (data.manga.alt_titles || []).slice(0, 4);
  $('#mAlt').textContent = alt.length ? alt.join(' · ') : '';
  $('#mAuthors').textContent = [...(data.manga.authors || []), ...(data.manga.artists || [])].join(' · ');

  const chips = [];
  (data.manga.demographics || []).forEach(d => chips.push(`<span class="chip demographic">${escapeHtml(d)}</span>`));
  (data.manga.tags || []).slice(0, 8).forEach(t => chips.push(`<span class="chip">${escapeHtml(t)}</span>`));
  $('#mTags').innerHTML = chips.join('');
  $('#mDesc').textContent = data.manga.description || '';

  const s = data.stats;
  const pills = [
    `<div class="stat-pill"><b>${s.chapters}</b><span>capítulos</span></div>`,
    s.chapters_official ? `<div class="stat-pill"><b>${s.chapters_official}</b><span>oficiais</span></div>` : '',
    s.volumes ? `<div class="stat-pill"><b>${s.volumes}</b><span>volumes</span></div>` : '',
    data.manga.type ? `<div class="stat-pill"><b>${escapeHtml(data.manga.type)}</b><span>tipo</span></div>` : '',
  ];
  $('#mStats').innerHTML = pills.join('');

  // idioma select (usa labels do site quando disponível)
  const langSel = $('#langSel');
  const langs = data.languages && data.languages.length ? data.languages : (data.manga.languages || []).map(c => ({ code: c, label: (LANG_LABELS[c] || c) }));
  langSel.innerHTML = langs.map(l => {
    const code = typeof l === 'string' ? l : l.code;
    const label = typeof l === 'string' ? (LANG_LABELS[l] || l) : (l.label || LANG_LABELS[l.code] || l.code);
    return `<option value="${escapeHtml(code)}"${code === data.lang ? ' selected' : ''}>${escapeHtml(label)}</option>`;
  }).join('');

  // esconde "Edição" se o site não tem filtro oficial
  const qualGroup = $('#qualitySel') ? $('#qualitySel').closest('.opt-group') : null;
  if (qualGroup) qualGroup.classList.toggle('hidden', !data.official_filter);

  // esconde aba Volumes se não houver
  const volTab = document.querySelector('.tab[data-tab="volumes"]');
  if (volTab) volTab.classList.toggle('hidden', !(data.volumes && data.volumes.length));

  // tabs counts
  $('#tabCountCh').textContent = state.chapters.length;
  $('#tabCountVol').textContent = state.volumes.length;
  $('#listEmpty').classList.toggle('hidden', state.chapters.length > 0 || state.volumes.length > 0);

  renderList();
  $('#resultCard').classList.remove('hidden');
  $('#selectCard').classList.remove('hidden');
  $('#progressCard').classList.add('hidden');
  updateSelectionUI();
}

/* ---------------- listas / seleção ---------------- */
const chapterList = $('#chapterList');
const volumeList = $('#volumeList');

function visibleChapters() {
  const onlyOfficial = $('#qualitySel').value === 'official';
  return state.chapters.filter(c => !onlyOfficial || c.official);
}

function itemKey(item) { return `${item.kind}:${item.id}`; }

function renderList() {
  const list = state.activeTab === 'chapters' ? chapterList : volumeList;
  const items = state.activeTab === 'chapters' ? visibleChapters() : state.volumes;
  list.innerHTML = items.map(it => {
    const key = itemKey(it);
    const checked = state.selected.has(key) ? ' checked' : '';
    const num = it.number != null ? (it.kind === 'volume' ? `V${String(it.number).padStart(2, '0')}` : String(it.number)) : '?';
    const name = escapeHtml(it.name || (it.kind === 'volume' ? `Volume ${it.number}` : `Capítulo ${it.number}`));
    const ofc = it.official ? '<span class="tag-official">Oficial</span>' : '';
    const count = it.chapterCount ? `<span class="ch-num" style="min-width:64px">${it.chapterCount} cap</span>` : '';
    return `<li class="chapter-row${state.selected.has(key) ? ' selected' : ''}" data-key="${escapeHtml(key)}">
      <input type="checkbox"${checked} aria-label="Selecionar ${name}">
      <span class="ch-num">${escapeHtml(num)}</span>
      <span class="ch-name" title="${name}">${name}</span>
      ${count}${ofc}
    </li>`;
  }).join('');
}

function toggleItem(key, checked) {
  const all = state.activeTab === 'chapters' ? state.chapters : state.volumes;
  const item = all.find(i => itemKey(i) === key);
  if (!item) return;
  if (checked) state.selected.set(key, item);
  else state.selected.delete(key);
  updateSelectionUI();
}

function updateSelectionUI() {
  const n = state.selected.size;
  $('#selCount').textContent = `${n} selecionado${n === 1 ? '' : 's'}`;
  const btn = $('#downloadBtn');
  btn.disabled = n === 0;
  $('#ctaTitle').textContent = n === 0
    ? 'Selecione os capítulos ou volumes'
    : `${n} item${n === 1 ? '' : 'ns'} prontos para baixar`;
  // marca a linha selecionada
  $$('.chapter-row').forEach(row => {
    const key = row.dataset.key;
    const on = state.selected.has(key);
    row.classList.toggle('selected', on);
    const box = row.querySelector('input[type="checkbox"]');
    if (box && box.checked !== on) box.checked = on;
  });
}

document.addEventListener('click', e => {
  const row = e.target.closest('.chapter-row');
  if (row) {
    const box = row.querySelector('input[type="checkbox"]');
    // clique na linha (fora do checkbox) alterna; no checkbox, deixa o default e sincroniza
    if (e.target === box) {
      toggleItem(row.dataset.key, box.checked);
    } else {
      box.checked = !box.checked;
      toggleItem(row.dataset.key, box.checked);
    }
    return;
  }
});

$$('.tab').forEach(tab => tab.addEventListener('click', () => {
  state.activeTab = tab.dataset.tab;
  $$('.tab').forEach(t => t.classList.toggle('active', t === tab));
  chapterList.classList.toggle('hidden', state.activeTab !== 'chapters');
  volumeList.classList.toggle('hidden', state.activeTab !== 'volumes');
  renderList();
  updateSelectionUI();
}));

$('#selAll').addEventListener('click', () => {
  const items = state.activeTab === 'chapters' ? visibleChapters() : state.volumes;
  items.forEach(i => state.selected.set(itemKey(i), i));
  renderList(); updateSelectionUI();
});
$('#selNone').addEventListener('click', () => {
  const items = state.activeTab === 'chapters' ? state.chapters : state.volumes;
  items.forEach(i => state.selected.delete(itemKey(i)));
  renderList(); updateSelectionUI();
});
$('#rangeApply').addEventListener('click', () => {
  const from = parseInt($('#rangeFrom').value, 10);
  const to = parseInt($('#rangeTo').value, 10);
  if (Number.isNaN(from) && Number.isNaN(to)) { toast('Digite uma faixa válida.', 'err'); return; }
  const lo = Number.isNaN(from) ? -Infinity : from;
  const hi = Number.isNaN(to) ? Infinity : to;
  const items = state.activeTab === 'chapters' ? visibleChapters() : state.volumes;
  // limpa só o que pertence à aba ativa e aplica a faixa
  items.forEach(i => state.selected.delete(itemKey(i)));
  let added = 0;
  items.forEach(i => {
    const n = i.number ?? -1;
    if (n >= lo && n <= hi) { state.selected.set(itemKey(i), i); added++; }
  });
  renderList(); updateSelectionUI();
  toast(`${added} item(ns) na faixa.`, 'ok', 1800);
});

/* mudança de idioma recarrega as listas */
$('#langSel').addEventListener('change', async e => {
  const lang = e.target.value;
  $('#langSel').disabled = true;
  try {
    const data = await fetchJson(`/api/info?url=${encodeURIComponent(state.url)}&lang=${encodeURIComponent(lang)}`);
    applyInfo(data);
  } catch (err) { toast(err.message, 'err'); }
  finally { $('#langSel').disabled = false; }
});

/* edição "somente oficiais" afeta a lista visível */
$('#qualitySel').addEventListener('change', () => {
  if ($('#qualitySel').value === 'official') {
    [...state.selected.entries()].forEach(([key, item]) => {
      if (item.kind === 'chapter' && !item.official) state.selected.delete(key);
    });
  }
  renderList(); updateSelectionUI();
});

/* ---------------- download + progresso ---------------- */
const downloadBtn = $('#downloadBtn');
const cancelBtn = $('#cancelBtn');
const progressCard = $('#progressCard');

downloadBtn.addEventListener('click', async () => {
  if (state.selected.size === 0) return;
  const items = [...state.selected.values()];
  const payload = {
    url: state.url,
    title: state.manga?.title || '',
    cover: state.manga?.cover || '',
    dest: $('#destInput').value.trim(),
    lang: $('#langSel').value,
    resolution: $('#resSel').value,
    format: formatSel.value,
    keep_images: $('#keepImages').checked,
    official_only: $('#qualitySel').value === 'official',
    items,
  };
  downloadBtn.disabled = true;
  try {
    const data = await fetchJson('/api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    state.jobId = data.job.id;
    try { localStorage.setItem('bm_dest', $('#destInput').value.trim()); } catch { /* ignore */ }
    toast('Download na fila.', 'ok');
    showProgress(data.job);
    startEventStream();
  } catch (err) {
    toast(err.message, 'err', 6000);
    downloadBtn.disabled = false;
  }
});

cancelBtn.addEventListener('click', async () => {
  if (!state.jobId) return;
  cancelBtn.disabled = true;
  try { await fetchJson(`/api/jobs/${state.jobId}/cancel`, { method: 'POST' }); }
  catch (err) { toast(err.message, 'err'); cancelBtn.disabled = false; }
});

$('#openFolderBtn').addEventListener('click', async () => {
  try {
    await fetchJson('/api/open-folder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: $('#destInput').value.trim(), root: $('#destInput').value.trim() }),
    });
  } catch (err) { toast(err.message, 'err'); }
});

function showProgress(job) {
  $('#progressCard').classList.remove('hidden');
  renderJob(job);
  $('#progressCard').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function startEventStream() {
  if (state.es) state.es.close();
  const es = new EventSource('/api/events');
  state.es = es;
  es.onmessage = ev => {
    try {
      const jobs = JSON.parse(ev.data);
      const job = jobs.find(j => j.id === state.jobId);
      if (job) renderJob(job);
    } catch { /* snapshot inválido — ignora */ }
  };
  es.onerror = () => { /* navegador reconecta sozinho */ };
}

function renderJob(job) {
  const fmtLabels = { cbz: 'CBZ', pdf: 'PDF', epub: 'EPUB', images: 'imagens' };
  const phase = job.phase || '';
  let pct, barLabel;
  if (phase === 'upscale' || phase === 'convert') {
    pct = job.phase_total > 0 ? Math.min(100, (job.phase_done / job.phase_total) * 100) : 0;
    barLabel = `${job.phase_done}/${job.phase_total}`;
  } else {
    pct = job.pages_total > 0 ? Math.min(100, (job.pages_done / job.pages_total) * 100) : 0;
    barLabel = `${job.pages_done}/${job.pages_total || '?'}`;
  }
  $('#progBar').style.width = `${pct}%`;
  $('#progBarWrap').setAttribute('aria-valuenow', Math.round(pct));
  $('#progPct').textContent = `${Math.floor(pct)}%`;
  $('#progPages').textContent = `${barLabel} ${phase === 'upscale' ? 'página(s) sendo melhoradas' : 'páginas'}`;
  $('#progItems').textContent = `${job.items_done}/${job.items_total} itens`;
  $('#progEta').textContent = fmtEta(job.item_eta ?? job.eta);

  let title, sub;
  if (job.status === 'queued') { title = 'Na fila…'; sub = job.dest; }
  else if (job.status === 'running') {
    if (phase === 'upscale') {
      title = `Melhorando imagens — ${job.item_label || ''}`;
      sub = `${job.phase_done} de ${job.phase_total} página(s) · ${job.item_index + 1}/${job.items_total} · ${job.dest}`;
    } else if (phase === 'convert') {
      title = `Montando ${fmtLabels[job.format] || job.format} — ${job.item_label || ''}`;
      sub = `${job.phase_done} de ${job.phase_total} página(s) · ${job.item_index + 1}/${job.items_total} · ${job.dest}`;
    } else {
      title = job.item_label ? `Baixando ${job.item_label}` : 'Baixando…';
      sub = `Página ${job.item_pages_done}${job.item_pages_total ? ` de ${job.item_pages_total}` : ''} · ${job.item_index + 1}/${job.items_total} · ${job.dest}`;
    }
  }
  else if (job.status === 'done') { title = 'Download concluído'; sub = `Tudo salvo em ${job.dest}`; }
  else if (job.status === 'canceled') { title = 'Download cancelado'; sub = job.dest; }
  else { title = 'Falha no download'; sub = job.dest; }

  const failNote = job.items_failed ? ` · ${job.items_failed} com erro` : '';
  $('#progTitle').textContent = title + failNote;
  $('#progSub').textContent = sub;

  $('#logPre').textContent = (job.messages || []).slice(-40).join('\n');

  const finished = job.status === 'done' || job.status === 'canceled' || job.status === 'error';
  cancelBtn.hidden = finished;
  $('#openFolderBtn').hidden = !finished;

  if (finished && state.es) {
    state.es.close();
    state.es = null;
    if (job.status === 'done') toast('Download concluído!', 'ok', 6000);
    else if (job.status === 'canceled') toast('Download cancelado.', 'err');
    else toast(`Erro no download: ${job.error || 'veja os detalhes'}`, 'err', 6000);
    downloadBtn.disabled = state.selected.size === 0;
  }
}

/* ---------------- biblioteca ---------------- */
const libraryGrid = $('#libraryGrid');

async function loadLibrary() {
  const dest = $('#libDestInput').value.trim();
  libraryGrid.innerHTML = '<div class="empty-state">Carregando biblioteca…</div>';
  try {
    const data = await fetchJson(`/api/library?dest=${encodeURIComponent(dest)}`);
    const s = data.stats;
    const parts = [`${s.cbz} CBZ`];
    if (s.pdf) parts.push(`${s.pdf} PDF`);
    if (s.epub) parts.push(`${s.epub} EPUB`);
    $('#libStats').textContent = `${s.manga} mangá(s) · ${parts.join(' · ')} · ${fmtBytes(s.size)} no total`;
    if (!data.items.length) {
      libraryGrid.innerHTML = '';
      $('#libEmpty').classList.remove('hidden');
      return;
    }
    $('#libEmpty').classList.add('hidden');
    libraryGrid.innerHTML = data.items.map(m => {
      const breakdown = [`${m.cbz} CBZ`];
      if (m.pdf) breakdown.push(`${m.pdf} PDF`);
      if (m.epub) breakdown.push(`${m.epub} EPUB`);
      return `
      <div class="lib-card">
        <h3>${escapeHtml(m.name)}</h3>
        <div class="lib-meta"><b>${(m.archives || 0)}</b> arquivo(s) · ${breakdown.join(' · ')} · ${fmtBytes(m.size)}</div>
        <div class="lib-actions">
          <button class="ghost small open-lib" data-path="${escapeHtml(m.path)}">Abrir pasta</button>
        </div>
      </div>`;
    }).join('');
  } catch (err) {
    libraryGrid.innerHTML = '';
    toast(err.message, 'err');
  }
}

libraryGrid.addEventListener('click', async e => {
  const btn = e.target.closest('.open-lib');
  if (!btn) return;
  try {
    await fetchJson('/api/open-folder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: btn.dataset.path, root: $('#libDestInput').value.trim() }),
    });
  } catch (err) { toast(err.message, 'err'); }
});

$('#libRefresh').addEventListener('click', loadLibrary);

/* ---------------- verificar ordem de leitura ---------------- */
const verifyPanel = $('#verifyPanel');
const verifyReport = $('#verifyReport');
const fixBtn = $('#fixBtn');

function showVerifyPanel(html) { verifyPanel.classList.remove('hidden'); verifyReport.innerHTML = html; }

$('#verifyBtn').addEventListener('click', async () => {
  const root = $('#libDestInput').value.trim();
  showVerifyPanel('<em>Verificando todos os .cbz…</em>');
  fixBtn.hidden = true;
  $('#verifyTitle').textContent = 'Verificando ordem de leitura…';
  try {
    const data = await fetchJson('/api/verify', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ root }),
    });
    const problems = data.problems || [];
    const okCount = data.ok || 0;
    if (!problems.length) {
      $('#verifyTitle').textContent = 'Ordem de leitura: perfeita';
      showVerifyPanel(`<span class="verify-ok">✓ ${data.checked} CBZ verificado(s) — todos em ordem de leitura (001 → fim), sem erros.</span>`);
      fixBtn.hidden = true;
      return;
    }
    $('#verifyTitle').textContent = `${problems.length} arquivo(s) com problemas`;
    const items = problems.slice(0, 30).map(p => {
      const file = String(p.file).split(/[\\/]/).pop();
      return `<div><strong>${escapeHtml(file)}</strong> — ${escapeHtml((p.issues || []).join('; '))}</div>`;
    }).join('');
    const extra = problems.length > 30 ? `<div>…e mais ${problems.length - 30}</div>` : '';
    showVerifyPanel(`<span class="verify-bad">⚠ ${problems.length} de ${data.checked} CBZ com problemas:</span><div class="verify-list">${items}${extra}</div>`);
    fixBtn.hidden = false;
  } catch (err) {
    showVerifyPanel(`<span class="verify-bad">Erro: ${escapeHtml(err.message)}</span>`);
    fixBtn.hidden = true;
  }
});

fixBtn.addEventListener('click', async () => {
  const root = $('#libDestInput').value.trim();
  fixBtn.disabled = true;
  showVerifyPanel('<em>Corrigindo a ordem dos arquivos…</em>');
  try {
    const data = await fetchJson('/api/fix', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ root }),
    });
    const failed = (data.failed || []).length;
    showVerifyPanel(`<span class="verify-ok">✓ ${data.fixed} CBZ corrigido(s) e reordenado(s) para leitura no celular.</span>` +
      (failed ? `<div class="verify-bad">${failed} não puderam ser corrigidos (veja o log).</div>` : '') +
      `<div class="verify-report">Rode a verificação de novo para confirmar.</div>`);
    fixBtn.hidden = true;
    loadLibrary();
  } catch (err) {
    showVerifyPanel(`<span class="verify-bad">Erro: ${escapeHtml(err.message)}</span>`);
  } finally {
    fixBtn.disabled = false;
  }
});

$('#verifyClose').addEventListener('click', () => verifyPanel.classList.add('hidden'));

/* ---------------- restaura preferências ---------------- */
try {
  const savedDest = localStorage.getItem('bm_dest');
  if (savedDest) { $('#destInput').value = savedDest; $('#libDestInput').value = savedDest; }
} catch { /* localStorage desativado */ }
