'use strict';

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const TYPES = {
  event: { icon: '⚡', name: 'Событие', color: 'var(--c-event)' },
  template: { icon: '🧩', name: 'Шаблон', color: 'var(--c-template)' },
  tg_message: { icon: '💬', name: 'TG-сообщение', color: 'var(--c-tg_message)' },
  email: { icon: '✉️', name: 'Письмо', color: 'var(--c-email)' },
  job: { icon: '⏱', name: 'Задача', color: 'var(--c-job)' },
  push: { icon: '📱', name: 'Push', color: 'var(--c-push)' },
  bot_cmd: { icon: '🤖', name: 'Команда бота', color: 'var(--c-bot_cmd)' },
  entry: { icon: '🌐', name: 'Вход (webhook/API)', color: 'var(--c-entry)' },
  note: { icon: '📝', name: 'Заметка', color: 'var(--c-note)' },
};
const EDGE_KINDS = {
  handler: 'обработчик события', delayed: 'отложенная задача', periodic: 'периодическая задача',
  sends: 'отправляет', calls: 'вызывает шаблон', link: 'связь',
};

const api = async (method, url, body) => {
  const r = await fetch('/api/' + url, {
    method, headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401) { location.href = '/login.html'; throw new Error('нужен вход'); }
  const data = await r.json();
  if (!r.ok) {
    if (r.status === 403) toast('Только просмотр: изменения недоступны');
    throw new Error(data.error || r.statusText);
  }
  return data;
};

/* ------------------------------------------------------------ модальное окно (вместо confirm/prompt) */

function ask({ title, text = '', ok = 'OK', cancel = 'Отмена', danger = false, input = null }) {
  const dlg = $('#modal'), inp = $('#modalInput');
  $('#modalTitle').textContent = title;
  $('#modalText').textContent = text;
  $('#modalText').classList.toggle('hidden', !text);
  $('#modalOk').textContent = ok;
  $('#modalOk').className = 'btn ' + (danger ? 'danger-solid' : 'primary');
  $('#modalCancel').textContent = cancel;
  inp.classList.toggle('hidden', input === null);
  inp.value = input ?? '';
  dlg.returnValue = '';
  dlg.showModal();
  if (input !== null) setTimeout(() => { inp.focus(); inp.select(); }, 0); else $('#modalOk').focus();
  return new Promise((resolve) => {
    dlg.addEventListener('close', () => {
      if (dlg.returnValue !== 'ok') return resolve(input !== null ? null : false);
      resolve(input !== null ? inp.value.trim() : true);
    }, { once: true });
  });
}

$('#modalInput').addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') { ev.preventDefault(); $('#modal').close('ok'); }
});

const canEdit = () => S.role === 'admin';

const toast = (msg) => {
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove('show'), 2200);
};

const S = {
  topos: [], topo: null, nodes: new Map(), edges: new Map(),
  view: { x: 40, y: 40, k: 1 }, sel: null, search: '',
};

const canvas = $('#canvas'), world = $('#world'), nodesEl = $('#nodes'), edgesSvg = $('#edges');

/* ------------------------------------------------------------ топологии */

async function loadTopos() {
  S.topos = await api('GET', 'topologies');
  renderTopoList();
}

function renderTopoList() {
  const f = $('#topoFilter').value.trim().toLowerCase();
  $('#topoList').innerHTML = S.topos
    .filter((t) => !f || t.name.toLowerCase().includes(f))
    .map((t) => `<li data-id="${t.id}" class="${S.topo && S.topo.id === t.id ? 'active' : ''}">
      <span>${esc(t.name)}</span><span class="cnt">${t.nodes_count}</span></li>`).join('');
}

async function openTopo(id) {
  const d = await api('GET', 'topologies/' + id);
  S.topo = d.topology;
  S.nodes = new Map(d.nodes.map((n) => [n.id, n]));
  S.edges = new Map(d.edges.map((e) => [e.id, e]));
  S.sel = null;
  localStorage.setItem('shm-topo-last', id);
  $('#topoName').value = S.topo.name; $('#topoName').disabled = !canEdit();
  $('#empty').classList.add('hidden');
  closePanel();
  renderAll();
  renderSearchList();
  const missing = [...S.nodes.values()].filter((n) => n.x == null);
  if (missing.length === S.nodes.size && missing.length) {
    autoLayout(true);
  } else {
    if (missing.length) await placeNew(missing);
    const v = JSON.parse(localStorage.getItem('shm-view-' + id) || 'null');
    if (v) { S.view = v; applyView(); } else initialView();
  }
  renderTopoList();
}

function noTopo() {
  S.topo = null; S.nodes.clear(); S.edges.clear();
  $('#topoName').value = ''; $('#topoName').disabled = true;
  $('#empty').classList.remove('hidden');
  closePanel(); renderAll(); renderTopoList();
}

/* ------------------------------------------------------------ рендер */

function condBox(title, list, cls = '') {
  return list && list.length ? `<div class="when ${cls}"><b>${title}</b>${list.map((w) => `<div>▸ ${esc(w)}</div>`).join('')}</div>` : '';
}

function nodeHTML(n) {
  const T = TYPES[n.type] || TYPES.note;
  const m = n.meta || {};
  let body = '';
  if (m.src_changed) body += '<div class="badge warn">⚠ текст в шаблоне изменился — открой узел</div>';
  if (m.stale) body += '<div class="badge bad">✖ больше нет в шаблоне</div>';
  if (m.unused_event) body += `<div class="badge muted-b">⚪ событие ${esc(m.unused_event)} не вызывает этот шаблон — сообщение не уходит</div>`;
  body += condBox('Выполняется, если:', m.guards, 'guard');
  body += condBox('Когда:', m.when);
  if ((m.when_tech || []).length) body += `<div class="tech">+ ${m.when_tech.length} тех. ${plural(m.when_tech.length, 'проверка', 'проверки', 'проверок')}</div>`;
  const text = n.content || '';
  if (text) body += `<div class="txt${text.split('\n').length > 9 || text.length > 420 ? ' clip' : ''}">${esc(text.slice(0, 900))}</div>`;
  if (m.buttons && m.buttons.length) {
    body += '<div class="kb">' + m.buttons.map((row) => '<div class="row">' + row.map((b) =>
      `<span class="b ${esc(b.type || '')}" title="${esc((b.type || '') + ': ' + (b.action || ''))}">${esc(b.text)}</span>`).join('') + '</div>').join('') + '</div>';
  }
  if (n.type === 'push' && m.link) body += `<div class="kb"><div class="row"><span class="b url" title="${esc(m.link)}">открыть ссылку</span></div></div>`;
  if ((m.actions || []).length) body += `<div class="acts">${m.actions.map((a) => `<div>⚙ ${esc(a)}</div>`).join('')}</div>`;
  let sub = n.subtitle || '';
  if (n.type === 'email' && m.subject && m.subject !== n.title) sub = 'Тема: ' + m.subject;
  const flags = [];
  if (m.missing) flags.push(n.type === 'bot_cmd' ? 'CASE не найден в шаблоне бота' : 'нет исходника в библиотеке');
  if (m.http && m.http.length) flags.push('HTTP×' + m.http.length);
  if (m.stops && m.stops.length) flags.push('⛔ STOP×' + m.stops.length);
  if (m.public) flags.push('🌐 публичный');
  const tag = n.type === 'tg_message' ? 'TG-сообщение' : T.name;
  return `<div class="hd"><span class="ic">${T.icon}</span><div><div class="tag">${tag}</div><div class="tt">${esc(n.title || n.key || '(без названия)')}</div></div></div>
    ${sub ? `<div class="st">${esc(sub)}</div>` : ''}
    ${body ? `<div class="bd">${body}</div>` : ''}
    ${flags.length ? `<div class="flag">${esc(flags.join(' · '))}</div>` : ''}
    <div class="port" title="Потяните, чтобы связать"></div>`;
}

function plural(n, one, few, many) {
  const a = n % 10, b = n % 100;
  return a === 1 && b !== 11 ? one : a >= 2 && a <= 4 && (b < 10 || b >= 20) ? few : many;
}

function renderNode(n) {
  let el = nodesEl.querySelector(`[data-id="${n.id}"]`);
  if (!el) {
    el = document.createElement('div');
    el.dataset.id = n.id;
    nodesEl.appendChild(el);
  }
  const m = n.meta || {};
  el.className = `node t-${n.type}${m.missing ? ' missing' : ''}${m.stale ? ' stale' : ''}${m.recipient === 'group' ? ' group' : ''}${m.unused_event ? ' unused' : ''}`;
  el.style.setProperty('--c', (TYPES[n.type] || TYPES.note).color);
  el.style.left = (n.x || 0) + 'px';
  el.style.top = (n.y || 0) + 'px';
  el.innerHTML = nodeHTML(n);
  n._el = el;
}

function renderAll() {
  nodesEl.innerHTML = '';
  S.nodes.forEach(renderNode);
  renderEdges();
  applyHighlight();
}

function box(n) {
  const el = n._el;
  return { x: n.x ?? 0, y: n.y ?? 0, w: el ? el.offsetWidth : 260, h: el ? el.offsetHeight : 60 };
}

function edgeGeom(e) {
  const s = S.nodes.get(e.source_id), t = S.nodes.get(e.target_id);
  if (!s || !t) return null;
  const a = box(s), b = box(t);
  let x1, y1, x2, y2, c1x, c1y, c2x, c2y;
  const ay = a.y + Math.min(a.h / 2, 22), by = b.y + Math.min(b.h / 2, 22);
  if (b.x >= a.x + a.w + 20) { // цель справа
    x1 = a.x + a.w; y1 = ay; x2 = b.x; y2 = by;
    const dx = Math.max(40, (x2 - x1) / 2); c1x = x1 + dx; c1y = y1; c2x = x2 - dx; c2y = y2;
  } else if (b.x + b.w + 20 <= a.x) { // цель слева
    x1 = a.x; y1 = ay; x2 = b.x + b.w; y2 = by;
    const dx = Math.max(40, (x1 - x2) / 2); c1x = x1 - dx; c1y = y1; c2x = x2 + dx; c2y = y2;
  } else if (b.y > a.y) { // снизу
    x1 = a.x + a.w / 2; y1 = a.y + a.h; x2 = b.x + b.w / 2; y2 = b.y;
    const dy = Math.max(30, (y2 - y1) / 2); c1x = x1; c1y = y1 + dy; c2x = x2; c2y = y2 - dy;
  } else { // сверху
    x1 = a.x + a.w / 2; y1 = a.y; x2 = b.x + b.w / 2; y2 = b.y + b.h;
    const dy = Math.max(30, (y1 - y2) / 2); c1x = x1; c1y = y1 - dy; c2x = x2; c2y = y2 + dy;
  }
  const bz = (p0, p1, p2, p3, t) => (1 - t) ** 3 * p0 + 3 * (1 - t) ** 2 * t * p1 + 3 * (1 - t) * t * t * p2 + t ** 3 * p3;
  return {
    d: `M${x1},${y1} C${c1x},${c1y} ${c2x},${c2y} ${x2},${y2}`,
    mx: bz(x1, c1x, c2x, x2, 0.72), my: bz(y1, c1y, c2y, y2, 0.72),
  };
}

// короткая подпись условия для линии: самые конкретные (последние) условия
function whenShort(meta) {
  const w = ((meta && meta.when) || []).filter((x) => !x.startsWith('для каждого'));
  if (!w.length) return '';
  // самое информативное: про статус услуги, иначе последнее
  let s = w.find((x) => /услуг/.test(x) && /(активна|заблокирована|удалена|оплачена|создана)/.test(x)) || w[w.length - 1];
  if (s.length > 56) s = s.slice(0, 55) + '…';
  const alt = (meta.alt || []).length;
  return 'если ' + s + (w.length > 1 ? ` (+${w.length - 1})` : '') + (alt ? ` · вариантов ${alt + 1}` : '');
}

function whenFull(meta) {
  if (!meta) return '';
  const all = [meta.when || [], ...(meta.alt || [])].filter((x) => x.length);
  return all.map((w) => 'Если: ' + w.join('; ')).join('\nИЛИ\n');
}

function renderEdges() {
  const defs = edgesSvg.querySelector('defs').outerHTML;
  let out = '';
  S.edges.forEach((e) => {
    const g = edgeGeom(e);
    if (!g) return;
    const sel = S.sel && S.sel.kind === 'edge' && S.sel.id === e.id ? ' sel' : '';
    const w = e.kind === 'sends' && (S.nodes.get(e.target_id)?.meta?.when || []).length ? '' : whenShort(e.meta);
    let lbl = '';
    if (e.label && w) lbl = `<text class="elabel" x="${g.mx}" y="${g.my - 16}" text-anchor="middle">${esc(e.label)}<tspan class="ewhen" x="${g.mx}" dy="13">${esc(w)}</tspan></text>`;
    else if (e.label || w) lbl = `<text class="elabel${e.label ? '' : ' only'}" x="${g.mx}" y="${g.my - 5}" text-anchor="middle">${esc(e.label || w)}</text>`;
    out += `<g class="edge ${esc(e.kind || 'link')}${sel}${e.meta && e.meta.stale ? ' stale' : ''}" data-eid="${e.id}">
      <path class="vis" d="${g.d}"/><path class="hit" d="${g.d}"/>${lbl}<title>${esc(whenFull(e.meta))}</title></g>`;
  });
  edgesSvg.innerHTML = defs + out;
}

/* ------------------------------------------------------------ вид */

function applyView() {
  const { x, y, k } = S.view;
  world.style.transform = `translate(${x}px,${y}px) scale(${k})`;
  canvas.style.backgroundSize = `${22 * k}px ${22 * k}px`;
  canvas.style.backgroundPosition = `${x}px ${y}px`;
  $('#zoomVal').textContent = Math.round(k * 100) + '%';
  if (S.topo) {
    clearTimeout(applyView.t);
    applyView.t = setTimeout(() => localStorage.setItem('shm-view-' + S.topo.id, JSON.stringify(S.view)), 300);
  }
}

function zoomAt(k, cx, cy) {
  k = Math.min(2.5, Math.max(0.08, k));
  const v = S.view;
  v.x = cx - (cx - v.x) * (k / v.k);
  v.y = cy - (cy - v.y) * (k / v.k);
  v.k = k;
  applyView();
}

function bounds(list) {
  let x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
  list.forEach((n) => { const b = box(n); x1 = Math.min(x1, b.x); y1 = Math.min(y1, b.y); x2 = Math.max(x2, b.x + b.w); y2 = Math.max(y2, b.y + b.h); });
  return { x1, y1, x2, y2 };
}

function fit(list) {
  list = list || [...S.nodes.values()];
  if (!list.length) { S.view = { x: 40, y: 40, k: 1 }; return applyView(); }
  const b = bounds(list), W = canvas.clientWidth, H = canvas.clientHeight, pad = 60;
  const k = Math.min(1.2, Math.max(0.08, Math.min((W - pad * 2) / (b.x2 - b.x1 || 1), (H - pad * 2) / (b.y2 - b.y1 || 1))));
  S.view = { k, x: (W - (b.x2 - b.x1) * k) / 2 - b.x1 * k, y: (H - (b.y2 - b.y1) * k) / 2 - b.y1 * k };
  applyView();
}

// при открытии: если всё влезает только мельче 45% — показываем начало графа в читаемом масштабе
function initialView() {
  fit();
  if (S.view.k >= 0.45 || !S.nodes.size) return;
  const b = bounds([...S.nodes.values()]), W = canvas.clientWidth, k = 0.6;
  S.view = { k, x: Math.max(40, (W - (b.x2 - b.x1) * k) / 2) - b.x1 * k, y: 60 - b.y1 * k };
  applyView();
}

function centerOn(n) {
  const b = box(n), W = canvas.clientWidth, H = canvas.clientHeight;
  const k = Math.max(S.view.k, 0.8);
  S.view = { k, x: W / 2 - (b.x + b.w / 2) * k, y: H / 2 - (b.y + b.h / 2) * k };
  applyView();
}

function toWorld(cx, cy) {
  const r = canvas.getBoundingClientRect();
  return { x: (cx - r.left - S.view.x) / S.view.k, y: (cy - r.top - S.view.y) / S.view.k };
}

/* ------------------------------------------------------------ раскладка */

async function autoLayout(andFit) {
  if (!S.nodes.size) return;
  const g = new dagre.graphlib.Graph({ multigraph: true });
  g.setGraph({ rankdir: 'LR', nodesep: 26, ranksep: 120, edgesep: 14, marginx: 20, marginy: 20 });
  g.setDefaultEdgeLabel(() => ({}));
  S.nodes.forEach((n) => { const b = box(n); g.setNode(String(n.id), { width: b.w, height: b.h }); });
  S.edges.forEach((e) => {
    if (S.nodes.has(e.source_id) && S.nodes.has(e.target_id)) g.setEdge(String(e.source_id), String(e.target_id), { minlen: 1 }, String(e.id));
  });
  dagre.layout(g);
  const items = [];
  S.nodes.forEach((n) => {
    const p = g.node(String(n.id));
    n.x = Math.round(p.x - p.width / 2); n.y = Math.round(p.y - p.height / 2);
    n._el.style.left = n.x + 'px'; n._el.style.top = n.y + 'px';
    items.push({ id: n.id, x: n.x, y: n.y });
  });
  renderEdges();
  if (andFit === true) initialView(); else if (andFit !== false) fit();
  await api('POST', `topologies/${S.topo.id}/positions`, { items });
}

// новые узлы после повторного импорта ставим справа от узла, из которого они выходят
async function placeNew(list) {
  const slots = {};
  const pending = new Set(list.map((n) => n.id));
  for (let pass = 0; pass < 6 && pending.size; pass++) {
    for (const n of list) {
      if (!pending.has(n.id)) continue;
      const e = [...S.edges.values()].find((e) => e.target_id === n.id && !pending.has(e.source_id) && S.nodes.has(e.source_id))
        || [...S.edges.values()].find((e) => e.source_id === n.id && !pending.has(e.target_id) && S.nodes.has(e.target_id));
      if (!e) continue;
      const anchor = S.nodes.get(e.target_id === n.id ? e.source_id : e.target_id), b = box(anchor);
      const k = slots[anchor.id] = (slots[anchor.id] || 0) + 1;
      n.x = e.target_id === n.id ? b.x + b.w + 120 : b.x - 380;
      n.y = b.y + (k - 1) * 160;
      pending.delete(n.id);
    }
  }
  if (pending.size) {
    const b = bounds([...S.nodes.values()].filter((n) => n.x != null));
    let y = isFinite(b.y2) ? b.y2 + 80 : 0;
    list.filter((n) => pending.has(n.id)).forEach((n) => { n.x = isFinite(b.x1) ? b.x1 : 0; n.y = y; y += 180; });
  }
  list.forEach((n) => { n._el.style.left = n.x + 'px'; n._el.style.top = n.y + 'px'; });
  renderEdges();
  await api('POST', `topologies/${S.topo.id}/positions`, { items: list.map((n) => ({ id: n.id, x: n.x, y: n.y })) });
  toast(`Новых узлов: ${list.length} — поставлены рядом со связанными`);
}

/* ------------------------------------------------------------ поиск */

function searchFields(n) {
  const m = n.meta || {};
  return [
    ['название', n.title], ['ключ', n.key], ['описание', n.subtitle], ['текст', n.content],
    ['кнопка', (m.buttons || []).flat().map((b) => b.text).join(' · ')],
    ['условие', (m.when || []).concat(m.guards || []).join(' · ')],
    ['тема', m.subject], ['ссылка', m.link],
  ];
}

// -> [поле, текст] первого совпадения или null
function searchMatch(n, q) {
  for (const [label, v] of searchFields(n)) if (v && String(v).toLowerCase().includes(q)) return [label, String(v)];
  return null;
}

function snippet(text, q) {
  const flat = text.replace(/\s+/g, ' ');
  const i = flat.toLowerCase().indexOf(q);
  const from = Math.max(0, i - 30), to = Math.min(flat.length, i + q.length + 50);
  return (from ? '…' : '') + esc(flat.slice(from, i)) + '<mark>' + esc(flat.slice(i, i + q.length)) + '</mark>'
    + esc(flat.slice(i + q.length, to)) + (to < flat.length ? '…' : '');
}

const SR = { items: [], active: -1 };

// что показать строкой в списке: у сообщений — первая содержательная строка текста
function listTitle(n) {
  if (['tg_message', 'email', 'push'].includes(n.type) && n.content) {
    const line = n.content.split('\n').map((l) => l.replace(/\[[^\]]*\]/g, '').trim()).find((l) => l.length > 2);
    if (line) return line.length > 70 ? line.slice(0, 69) + '…' : line;
  }
  return n.title || n.key || '(без названия)';
}

function renderSearchList() {
  const box = $('#searchList'), q = S.search.trim().toLowerCase();
  if (!q || !S.topo) { box.classList.add('hidden'); SR.items = []; return; }
  SR.items = [...S.nodes.values()].map((n) => ({ n, m: searchMatch(n, q) })).filter((x) => x.m)
    .sort((a, b) => (a.m[0] !== 'название') - (b.m[0] !== 'название') || (a.n.y ?? 0) - (b.n.y ?? 0));
  SR.active = Math.min(SR.active, SR.items.length - 1);
  const r = $('#search').getBoundingClientRect();
  box.style.left = r.left + 'px';
  box.style.top = r.bottom + 4 + 'px';
  box.style.width = Math.max(360, r.width) + 'px';
  const LIMIT = 60;
  box.innerHTML = `<div class="sl-head">${SR.items.length ? `Найдено: <b>${SR.items.length}</b> <span class="muted">· ↑↓ Enter — перейти</span>` : 'Ничего не найдено'}</div>`
    + SR.items.slice(0, LIMIT).map(({ n, m }, i) => {
      const T = TYPES[n.type] || TYPES.note;
      const lt = listTitle(n);
      const title = lt.toLowerCase().includes(q) ? snippet(lt, q) : esc(lt);
      const ctx = [n.type === 'tg_message' ? n.title : '', n.key && n.key !== lt ? n.key : ''].filter(Boolean).join(' · ');
      const showSnip = !lt.toLowerCase().includes(q);
      return `<div class="sl-item${i === SR.active ? ' active' : ''}" data-i="${i}">
        <span class="sl-ic" style="color:${T.color}">${T.icon}</span>
        <div class="sl-body"><div class="sl-title">${title}</div>
        <div class="sl-ctx">${esc(T.name)}${ctx ? ' · ' + esc(ctx) : ''}</div>
        ${showSnip ? `<div class="sl-snip"><span class="sl-field">${m[0]}:</span> ${snippet(m[1], q)}</div>` : ''}</div></div>`;
    }).join('')
    + (SR.items.length > LIMIT ? `<div class="sl-more">и ещё ${SR.items.length - LIMIT}… уточните запрос</div>` : '');
  box.classList.remove('hidden');
  box.querySelector('.sl-item.active')?.scrollIntoView({ block: 'nearest' });
}

function goSearchItem(i) {
  const it = SR.items[i];
  if (!it) return;
  centerOn(it.n);
  select({ kind: 'node', id: it.n.id });
  SR.active = i;
  renderSearchList();
}

$('#searchList').addEventListener('mousedown', (ev) => {
  ev.preventDefault(); // не терять фокус поля
  const el = ev.target.closest('.sl-item');
  if (el) goSearchItem(+el.dataset.i);
});

/* ------------------------------------------------------------ подсветка связей */

function reach(start, dir) {
  const seen = new Set([start]), q = [start];
  while (q.length) {
    const id = q.shift();
    S.edges.forEach((e) => {
      const [from, to] = dir === 'down' ? [e.source_id, e.target_id] : [e.target_id, e.source_id];
      if (from === id && !seen.has(to)) { seen.add(to); q.push(to); }
    });
  }
  return seen;
}

function applyHighlight() {
  const q = S.search.trim().toLowerCase();
  let keep = null;
  if (S.sel && S.sel.kind === 'node') {
    keep = new Set([...reach(S.sel.id, 'down'), ...reach(S.sel.id, 'up')]);
  }
  S.nodes.forEach((n) => {
    if (!n._el) return;
    const hit = q && !!searchMatch(n, q);
    n._el.classList.toggle('hit', !!hit);
    n._el.classList.toggle('dim', keep ? !keep.has(n.id) : (!!q && !hit));
    n._el.classList.toggle('sel', !!(S.sel && S.sel.kind === 'node' && S.sel.id === n.id));
  });
  edgesSvg.querySelectorAll('.edge').forEach((g) => {
    const e = S.edges.get(+g.dataset.eid);
    g.classList.toggle('dim', !!(keep && e && !(keep.has(e.source_id) && keep.has(e.target_id))));
    g.classList.toggle('sel', !!(S.sel && S.sel.kind === 'edge' && S.sel.id === +g.dataset.eid));
  });
}

/* ------------------------------------------------------------ мышь */

let drag = null;

canvas.addEventListener('mousedown', (ev) => {
  if (ev.button !== 0 || !S.topo) return;
  const port = ev.target.closest('.port');
  const nodeEl = ev.target.closest('.node');
  const edgeEl = ev.target.closest('.edge');
  if (port && nodeEl && canEdit()) {
    drag = { mode: 'link', from: +nodeEl.dataset.id };
    ev.preventDefault();
    return;
  }
  if (nodeEl) {
    const n = S.nodes.get(+nodeEl.dataset.id);
    if (ev.target.closest('.txt') && S.sel && S.sel.id === n.id && ev.detail > 1) return;
    drag = { mode: 'node', n, sx: ev.clientX, sy: ev.clientY, ox: n.x || 0, oy: n.y || 0, moved: false };
    ev.preventDefault();
    return;
  }
  if (edgeEl) {
    select({ kind: 'edge', id: +edgeEl.dataset.eid });
    return;
  }
  drag = { mode: 'pan', sx: ev.clientX, sy: ev.clientY, ox: S.view.x, oy: S.view.y, moved: false };
  canvas.classList.add('panning');
});

window.addEventListener('mousemove', (ev) => {
  if (!drag) return;
  if (drag.mode === 'pan') {
    drag.moved = drag.moved || Math.abs(ev.clientX - drag.sx) + Math.abs(ev.clientY - drag.sy) > 3;
    S.view.x = drag.ox + ev.clientX - drag.sx; S.view.y = drag.oy + ev.clientY - drag.sy;
    applyView();
  } else if (drag.mode === 'node') {
    const dx = (ev.clientX - drag.sx) / S.view.k, dy = (ev.clientY - drag.sy) / S.view.k;
    if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 3) return;
    drag.moved = true;
    const n = drag.n;
    n.x = Math.round(drag.ox + dx); n.y = Math.round(drag.oy + dy);
    n._el.style.left = n.x + 'px'; n._el.style.top = n.y + 'px';
    renderEdges(); applyHighlight();
  } else if (drag.mode === 'link') {
    const s = S.nodes.get(drag.from), b = box(s), r = canvas.getBoundingClientRect();
    const x1 = (b.x + b.w) * S.view.k + S.view.x, y1 = (b.y + Math.min(b.h / 2, 22)) * S.view.k + S.view.y;
    const x2 = ev.clientX - r.left, y2 = ev.clientY - r.top, dx = Math.max(40, Math.abs(x2 - x1) / 2);
    $('#ghostPath').setAttribute('d', `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`);
  }
});

window.addEventListener('mouseup', async (ev) => {
  if (!drag) return;
  const d = drag; drag = null;
  canvas.classList.remove('panning');
  if (d.mode === 'pan' && !d.moved) select(null);
  if (d.mode === 'node') {
    if (d.moved) {
      await api('POST', `topologies/${S.topo.id}/positions`, { items: [{ id: d.n.id, x: d.n.x, y: d.n.y }] });
    } else {
      select({ kind: 'node', id: d.n.id });
    }
  }
  if (d.mode === 'link') {
    $('#ghostPath').setAttribute('d', '');
    const el = document.elementFromPoint(ev.clientX, ev.clientY);
    const t = el && el.closest('.node');
    if (t && +t.dataset.id !== d.from) {
      const src = S.nodes.get(d.from), dst = S.nodes.get(+t.dataset.id);
      let kind = 'link';
      if (src.type === 'event') kind = 'handler';
      else if (['tg_message', 'email'].includes(dst.type)) kind = 'sends';
      else if (dst.type === 'job') kind = 'delayed';
      const e = await api('POST', `topologies/${S.topo.id}/edges`, { source_id: d.from, target_id: dst.id, kind });
      S.edges.set(e.id, e);
      renderEdges();
      select({ kind: 'edge', id: e.id });
    }
  }
});

canvas.addEventListener('wheel', (ev) => {
  if (!S.topo) return;
  ev.preventDefault();
  const r = canvas.getBoundingClientRect();
  const f = Math.exp(-ev.deltaY * (ev.ctrlKey ? 0.01 : 0.0015));
  zoomAt(S.view.k * f, ev.clientX - r.left, ev.clientY - r.top);
}, { passive: false });

canvas.addEventListener('dblclick', async (ev) => {
  if (!S.topo || !canEdit() || ev.target.closest('.node') || ev.target.closest('.edge')) return;
  const p = toWorld(ev.clientX, ev.clientY);
  await addNode('note', p.x - 130, p.y - 20);
});

/* ------------------------------------------------------------ панель */

function select(sel) {
  S.sel = sel;
  applyHighlight();
  if (!sel) return closePanel();
  if (sel.kind === 'node') showNodePanel(S.nodes.get(sel.id));
  else showEdgePanel(S.edges.get(sel.id));
}

function closePanel() { $('#panel').classList.add('hidden'); }

function buttonsToText(rows) {
  return (rows || []).map((r) => r.map((b) => b.text + (b.action ? ` => ${b.type && b.type !== 'callback_data' ? b.type + ':' : ''}${b.action}` : '')).join('  ||  ')).join('\n');
}

function textToButtons(txt) {
  return txt.split('\n').map((line) => line.trim()).filter(Boolean).map((line) => line.split('||').map((part) => {
    const [text, act] = part.split('=>').map((s) => s.trim());
    const b = { text };
    if (act) {
      const m = act.match(/^(url|web_app|switch_inline_query|copy_text|login_url):(.*)$/);
      b.type = m ? m[1] : 'callback_data'; b.action = m ? m[2].trim() : act;
    }
    return b;
  }));
}

function linkList(n) {
  const out = [], inc = [];
  S.edges.forEach((e) => {
    if (e.source_id === n.id && S.nodes.has(e.target_id)) out.push([e, S.nodes.get(e.target_id)]);
    if (e.target_id === n.id && S.nodes.has(e.source_id)) inc.push([e, S.nodes.get(e.source_id)]);
  });
  const li = ([e, o], arrow) => `<a data-goto="${o.id}">${arrow} ${TYPES[o.type]?.icon || ''} ${esc(o.title || o.key)}${e.label ? ` <span class="muted">(${esc(e.label)})</span>` : ''}</a>`;
  return { inc: inc.map((x) => li(x, '←')).join(''), out: out.map((x) => li(x, '→')).join('') };
}

function showNodePanel(n) {
  $('#panel').classList.remove('hidden');
  $('#panelTitle').textContent = (TYPES[n.type]?.icon || '') + ' ' + (TYPES[n.type]?.name || 'Узел');
  const m = n.meta || {};
  const links = linkList(n);
  const typeOpts = Object.entries(TYPES).map(([k, v]) => `<option value="${k}"${k === n.type ? ' selected' : ''}>${v.icon} ${v.name}</option>`).join('');
  $('#panelBody').innerHTML = `
    <div class="row2">
      <label class="field">Тип<select class="input" data-f="type">${typeOpts}</select></label>
      <label class="field">${n.type === 'bot_cmd' ? 'Шаблон бота' : n.type === 'entry' ? 'Ветка / значение' : 'Ключ / template_id'}<input class="input" data-f="key" value="${esc(n.key)}"></label>
    </div>
    <label class="field">Название<input class="input" data-f="title" value="${esc(n.title)}"></label>
    ${n.type === 'email' ? `<label class="field">Тема письма<input class="input" data-m="subject" value="${esc(m.subject || '')}"></label>` : ''}
    <label class="field">Подзаголовок / описание<input class="input" data-f="subtitle" value="${esc(n.subtitle)}"></label>
    <label class="field">${n.type === 'tg_message' ? 'Текст сообщения' : n.type === 'email' ? 'Текст письма' : 'Содержимое / заметки'}
      <textarea class="input big" data-f="content">${esc(n.content)}</textarea></label>
    ${n.type === 'tg_message' || (m.buttons && m.buttons.length) ? `<label class="field"><span>Кнопки — строка = ряд, <code>||</code> между кнопками, <code>Текст => /callback</code> или <code>=> url:https://…</code></span>
      <textarea class="input" data-buttons placeholder="💰 Баланс => /balance\n📖 Меню => /menu  ||  Канал => url:https://t.me/…">${esc(buttonsToText(m.buttons))}</textarea></label>` : ''}
    ${m.src_changed ? `<div class="field changed">⚠ После твоей правки текст в шаблоне изменился. Новый вариант из шаблона:
      <pre>${esc((m.src_new || {}).title || '')}\n\n${esc((m.src_new || {}).content || '')}${(m.src_new || {}).buttons ? '\n\n' + esc(buttonsToText(m.src_new.buttons)) : ''}</pre>
      <div class="row2"><button class="btn small primary" data-act="accept">Взять из шаблона</button><button class="btn small" data-act="keep">Оставить мой</button></div></div>` : ''}
    ${m.stale ? '<div class="field changed bad">✖ При последнем импорте этого узла в шаблонах уже не было. Можно удалить.</div>' : ''}
    ${(m.when || []).length ? `<div class="field">Когда срабатывает<ul class="conds">${m.when.map((w) => `<li>${esc(w)}</li>`).join('')}</ul></div>` : ''}
    ${(m.when_tech || []).length ? `<details class="src"><summary>Технические проверки (${m.when_tech.length})</summary><ul class="conds tech">${m.when_tech.concat(m.guards_tech || []).map((w) => `<li>${esc(w)}</li>`).join('')}</ul></details>` : ''}
    ${(m.actions || []).length ? `<div class="field">Действия<ul class="conds">${m.actions.map((w) => `<li>${esc(w)}</li>`).join('')}</ul></div>` : ''}
    ${m.link ? `<div class="field">Ссылка push<div class="chips"><span class="chip">${esc(m.link)}</span></div></div>` : ''}
    ${m.profile ? `<div class="field">Профиль Telegram<div class="chips"><span class="chip">${esc(m.profile)}</span></div></div>` : ''}
    ${(m.guards || []).length ? `<div class="field">Шаблон выполняется, только если<ul class="conds">${m.guards.map((w) => `<li>${esc(w)}</li>`).join('')}</ul></div>` : ''}
    ${(m.stops || []).length ? `<div class="field">⛔ Остановка (STOP) — дальше не выполняется, если<ul class="conds stop">${m.stops.map((w) => `<li>${esc(w.join(', ') || 'всегда')}</li>`).join('')}</ul></div>` : ''}
    ${m.http && m.http.length ? `<div class="field">HTTP-запросы<div class="chips">${m.http.map((h) => `<span class="chip">${esc(h)}</span>`).join('')}</div></div>` : ''}
    ${m.settings && Object.keys(m.settings).length ? `<div class="field">Настройки шаблона (.tpls)<div class="chips"><span class="chip">${esc(JSON.stringify(m.settings))}</span></div></div>` : ''}
    ${links.inc ? `<div class="field">Входящие<div class="links">${links.inc}</div></div>` : ''}
    ${links.out ? `<div class="field">Исходящие<div class="links">${links.out}</div></div>` : ''}
    ${n.key ? `<details class="src" data-src="${esc(n.key)}"><summary>Исходник шаблона ${esc(n.key)}</summary><pre>загрузка…</pre></details>` : ''}
    <div class="panel-actions">
      <button class="btn small" data-act="center">◎ Показать</button>
      <button class="btn small ghost danger" data-act="del">Удалить узел</button>
    </div>`;
  bindPanel(n, 'nodes');
}

function showEdgePanel(e) {
  $('#panel').classList.remove('hidden');
  $('#panelTitle').textContent = '↝ Связь';
  const s = S.nodes.get(e.source_id), t = S.nodes.get(e.target_id);
  const opts = Object.entries(EDGE_KINDS).map(([k, v]) => `<option value="${k}"${k === e.kind ? ' selected' : ''}>${v}</option>`).join('');
  const m = e.meta || {};
  const variants = [m.when || [], ...(m.alt || [])].filter((x) => x.length);
  const condsHTML = variants.length ? `<div class="field">Срабатывает, если${variants.map((w, i) =>
    `${i ? '<div class="muted small">— или —</div>' : ''}<ul class="conds">${w.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>`).join('')}</div>` : '';
  const info = Object.entries(m).filter(([k, v]) => !['when', 'alt'].includes(k) && v !== null && v !== '' && v !== undefined)
    .map(([k, v]) => `<span class="chip">${esc(k)}: ${esc(typeof v === 'object' ? JSON.stringify(v) : v)}</span>`).join('');
  $('#panelBody').innerHTML = `
    <div class="links"><a data-goto="${s.id}">← ${esc(s.title || s.key)}</a><a data-goto="${t.id}">→ ${esc(t.title || t.key)}</a></div>
    <label class="field">Тип связи<select class="input" data-f="kind">${opts}</select></label>
    <label class="field">Подпись (например «через 1 д», «category: vpn-%»)<input class="input" data-f="label" value="${esc(e.label)}"></label>
    ${condsHTML}
    ${info ? `<div class="field">Данные импорта<div class="chips">${info}</div></div>` : ''}
    <div class="panel-actions"><span></span><button class="btn small ghost danger" data-act="del">Удалить связь</button></div>`;
  bindPanel(e, 'edges');
}

function bindPanel(obj, table) {
  const body = $('#panelBody');
  if (!canEdit()) {
    body.querySelectorAll('input, textarea, select').forEach((el) => {
      el.readOnly = true; el.disabled = el.tagName === 'SELECT'; el.placeholder = '';
      if (!el.value && el.closest('label.field')) el.closest('label.field').remove();  // пустые поля в просмотре не нужны
    });
    body.querySelectorAll('[data-act="del"], [data-act="accept"], [data-act="keep"]').forEach((el) => el.remove());
  }
  const save = async (patch) => {
    const upd = await api('PATCH', `${table}/${obj.id}`, patch);
    Object.assign(obj, upd);
    if (table === 'nodes') { renderNode(obj); renderEdges(); } else renderEdges();
    applyHighlight();
  };
  const deb = {};
  body.querySelectorAll('[data-f]').forEach((el) => {
    const ev = el.tagName === 'SELECT' ? 'change' : 'input';
    el.addEventListener(ev, () => {
      clearTimeout(deb[el.dataset.f]);
      deb[el.dataset.f] = setTimeout(async () => {
        await save({ [el.dataset.f]: el.value });
        if (el.dataset.f === 'type') select(S.sel);
      }, ev === 'change' ? 0 : 350);
    });
  });
  const metaSave = () => {
    const meta = { ...(obj.meta || {}) };
    body.querySelectorAll('[data-m]').forEach((el) => { meta[el.dataset.m] = el.value; });
    const bt = body.querySelector('[data-buttons]');
    if (bt) meta.buttons = textToButtons(bt.value);
    clearTimeout(deb._meta);
    deb._meta = setTimeout(() => save({ meta }), 400);
  };
  body.querySelectorAll('[data-m],[data-buttons]').forEach((el) => el.addEventListener('input', metaSave));
  body.querySelectorAll('[data-goto]').forEach((a) => a.addEventListener('click', () => {
    const n = S.nodes.get(+a.dataset.goto); centerOn(n); select({ kind: 'node', id: n.id });
  }));
  const src = body.querySelector('details.src');
  if (src) src.addEventListener('toggle', async () => {
    if (!src.open || src.dataset.loaded) return;
    src.dataset.loaded = 1;
    try {
      const t = await api('GET', 'templates/' + encodeURIComponent(src.dataset.src));
      src.querySelector('pre').textContent = t.content;
    } catch (e) { src.querySelector('pre').textContent = 'Шаблон не найден в библиотеке. Импортируйте папку с шаблонами.'; }
  });
  body.querySelector('[data-act="center"]')?.addEventListener('click', () => centerOn(obj));
  ['accept', 'keep'].forEach((act) => body.querySelector(`[data-act="${act}"]`)?.addEventListener('click', async () => {
    Object.assign(obj, await api('POST', `nodes/${obj.id}/${act}`, {}));
    renderNode(obj); renderEdges(); applyHighlight(); select(S.sel);
  }));
  body.querySelector('[data-act="del"]')?.addEventListener('click', () => deleteSel());
}

async function deleteSel() {
  if (!S.sel) return;
  if (S.sel.kind === 'node') {
    const n = S.nodes.get(S.sel.id);
    const links = [...S.edges.values()].filter((e) => e.source_id === n.id || e.target_id === n.id).length;
    if (!await ask({ title: '🗑 Удалить узел?', text: `«${n.title || n.key}»${links ? `\nВместе с ним удалятся связи: ${links}.` : ''}`, ok: 'Удалить', danger: true })) return;
    await api('DELETE', 'nodes/' + n.id);
    S.nodes.delete(n.id); n._el.remove();
    [...S.edges.values()].filter((e) => e.source_id === n.id || e.target_id === n.id).forEach((e) => S.edges.delete(e.id));
  } else {
    const e = S.edges.get(S.sel.id), a = S.nodes.get(e.source_id), b = S.nodes.get(e.target_id);
    if (!await ask({ title: '🗑 Удалить связь?', text: `${a?.title || a?.key} → ${b?.title || b?.key}${e.label ? `\n(${e.label})` : ''}`, ok: 'Удалить', danger: true })) return;
    await api('DELETE', 'edges/' + S.sel.id);
    S.edges.delete(S.sel.id);
  }
  select(null); renderEdges(); applyHighlight(); refreshCounts();
}

async function addNode(type, x, y) {
  if (!S.topo) return;
  if (x == null) {
    const p = toWorld(canvas.getBoundingClientRect().left + canvas.clientWidth / 2, canvas.getBoundingClientRect().top + canvas.clientHeight / 2);
    x = p.x - 130; y = p.y - 40;
  }
  const n = await api('POST', `topologies/${S.topo.id}/nodes`, {
    type, title: TYPES[type].name, x: Math.round(x), y: Math.round(y), meta: type === 'tg_message' ? { buttons: [] } : {},
  });
  S.nodes.set(n.id, n); renderNode(n); applyHighlight();
  select({ kind: 'node', id: n.id });
  setTimeout(() => $('#panelBody [data-f="title"]')?.select(), 30);
  refreshCounts();
}

function refreshCounts() {
  const t = S.topos.find((x) => S.topo && x.id === S.topo.id);
  if (t) { t.nodes_count = S.nodes.size; renderTopoList(); }
}

/* ------------------------------------------------------------ импорт */

const imp = { templates: {}, settings: {}, events: null, eventsName: '', sql: [], sqlNames: [] };

function looksLikeEvents(data) {
  const arr = Array.isArray(data) ? data : (data && (data.data || data.items || data.events));
  return Array.isArray(arr) && arr.some((e) => e && e.settings && e.settings.template_id) ? data : null;
}

let previewT = null;
function schedulePreview() {
  clearTimeout(previewT);
  previewT = setTimeout(async () => {
    if (!imp.events && !Object.keys(imp.templates).length && !imp.sql.length) return;
    $('#impPublic').textContent = 'анализ…';
    try {
      const pv = await api('POST', 'import/preview', { events: imp.events, templates: imp.templates, settings: imp.settings, sql: imp.sql });
      pv.public.sort((a, b) => (b.actions - a.actions) || a.name.localeCompare(b.name));
      imp.publicList = pv.public;
      $('#impPublic').innerHTML = pv.public.length ? pv.public.map((p) => `<label class="check" title="${esc(p.entries.join(', '))}">
        <input type="checkbox" value="${esc(p.name)}"${p.actions ? ' checked' : ''}> ${esc(p.name)}
        ${p.actions ? '<span class="pill">действия</span>' : ''}${p.entries.length ? `<span class="pill">${p.entries.length} вх.</span>` : ''}</label>`).join('')
        : 'нет публичных шаблонов';
      if (pv.sql) $('#impInfo').innerHTML += `<br>Из SQL: шаблонов <b>${pv.sql.templates}</b>, событий <b>${pv.sql.events}</b>`;
    } catch (e) { $('#impPublic').textContent = 'ошибка анализа: ' + e.message; }
  }, 250);
}

function fillTargets() {
  const auto = S.topos.filter((t) => t.source);
  const cur = S.topo && S.topo.source === 'import' ? S.topo.id : '';
  $('#impTarget').innerHTML = '<option value="">➕ Новая схема</option>' + auto.map((t) =>
    `<option value="${t.id}"${t.id === cur ? ' selected' : ''}>↻ Обновить: ${esc(t.name)}</option>`).join('');
  syncImpMode();
}

function syncImpMode() {
  const split = $('#impSplit').checked;
  $('#impSingle').classList.toggle('hidden', split);
  $('#impUpdateWrap').classList.toggle('hidden', !split);
  $('#impName').disabled = !!$('#impTarget').value;
}
$('#impSplit').addEventListener('change', syncImpMode);
$('#impTarget').addEventListener('change', syncImpMode);

function updImpInfo() {
  schedulePreview();
  const nt = Object.keys(imp.templates).length;
  const ev = imp.events ? (Array.isArray(imp.events) ? imp.events : (imp.events.data || imp.events.items || imp.events.events || [])) : [];
  $('#impInfo').innerHTML = `Файлов шаблонов: <b>${nt}</b> · событий: <b>${ev.length}</b>${imp.eventsName ? ' (' + esc(imp.eventsName) + ')' : ''}`
    + (imp.sqlNames.length ? `<br>SQL: <b>${imp.sqlNames.map(esc).join(', ')}</b> (${(imp.sql.reduce((a, x) => a + x.length, 0) / 1048576).toFixed(1)} МБ)` : '');
}

$('#impDir').addEventListener('change', async (ev) => {
  imp.templates = {}; imp.settings = {};
  if (!imp.sqlFromFile) { imp.sql = []; imp.sqlNames = []; }
  const files = [...ev.target.files];
  for (const f of files) {
    const name = f.name;
    if (name.endsWith('.tpl')) imp.templates[name.slice(0, -4)] = await f.text();
    else if (name.endsWith('.tpls')) { try { imp.settings[name.slice(0, -5)] = JSON.parse(await f.text()); } catch (e) { /* пропускаем */ } }
    else if (name.endsWith('.sql') && !imp.sqlFromFile) {
      const txt = await f.text();
      if (/INSERT\s+INTO\s+`?(templates|events)`?/i.test(txt)) { imp.sql.push(txt); imp.sqlNames.push(name); }
    }
    else if (name.endsWith('.json') && !imp.eventsFromFile) {
      try { const d = looksLikeEvents(JSON.parse(await f.text())); if (d) { imp.events = d; imp.eventsName = name; } } catch (e) { /* не события */ }
    }
  }
  updImpInfo();
});

$('#impEvents').addEventListener('change', async (ev) => {
  const f = ev.target.files[0];
  if (!f) return;
  try {
    imp.events = JSON.parse(await f.text()); imp.eventsName = f.name; imp.eventsFromFile = true;
    if (!looksLikeEvents(imp.events)) toast('В файле не найдено событий с template_id');
  } catch (e) { toast('Не удалось прочитать JSON: ' + e.message); }
  updImpInfo();
});

$('#impSql').addEventListener('change', async (ev) => {
  const f = ev.target.files[0];
  if (!f) return;
  const txt = await f.text();
  if (!/INSERT\s+INTO\s+`?(templates|events)`?/i.test(txt)) toast('В дампе не найдено INSERT INTO templates / events');
  imp.sql = [txt]; imp.sqlNames = [f.name]; imp.sqlFromFile = true;
  updImpInfo();
});

$('#btnImport').addEventListener('click', () => {
  Object.assign(imp, { templates: {}, settings: {}, events: null, eventsName: '', eventsFromFile: false, sql: [], sqlNames: [], sqlFromFile: false });
  $('#impDir').value = ''; $('#impEvents').value = ''; $('#impSql').value = '';
  $('#impPublic').textContent = 'выберите источники…'; fillTargets(); updImpInfo();
  $('#importDlg').showModal();
});

$('#importDlg').addEventListener('close', async () => {
  if ($('#importDlg').returnValue !== 'default') return;
  if (!imp.events && !Object.keys(imp.templates).length && !imp.sql.length) return toast('Нечего импортировать');
  toast('Строю связи…');
  const res = await api('POST', 'import', {
    events: imp.events, templates: imp.templates, settings: imp.settings, sql: imp.sql,
    name: $('#impName').value, split: $('#impSplit').checked,
    target: $('#impSplit').checked ? null : ($('#impTarget').value || null),
    update: $('#impUpdate').checked,
    public: [...document.querySelectorAll('#impPublic input:checked')].map((i) => i.value),
  });
  await loadTopos();
  const ts = res.topologies;
  if (ts.length) {
    const sum = (k) => ts.reduce((a, t) => a + t[k], 0);
    const upd = ts.filter((t) => !t.new).length;
    toast(`Схем: новых ${ts.length - upd}, обновлено ${upd} · узлов +${sum('added')}, ~${sum('updated')}, пропало ${sum('stale')}`);
    openTopo(ts[0].id);
  } else toast(`Библиотека шаблонов обновлена: ${res.templates}`);
});

/* ------------------------------------------------------------ кнопки */

$('#topoList').addEventListener('click', (ev) => { const li = ev.target.closest('li'); if (li) openTopo(+li.dataset.id); });
$('#topoFilter').addEventListener('input', renderTopoList);
$('#btnNewTopo').addEventListener('click', async () => {
  const name = await ask({ title: '＋ Новая схема', text: 'Название схемы', input: 'Новая топология', ok: 'Создать' });
  if (!name) return;
  const r = await api('POST', 'topologies', { name });
  await loadTopos(); openTopo(r.id);
});
$('#topoName').addEventListener('change', async (ev) => {
  await api('PATCH', 'topologies/' + S.topo.id, { name: ev.target.value });
  S.topo.name = ev.target.value; await loadTopos();
});
$('#btnAdd').addEventListener('click', () => addNode($('#addType').value));
$('#btnLayout').addEventListener('click', async () => S.topo && await ask({
  title: '⌗ Авто-раскладка', text: 'Все узлы будут переставлены автоматически.\nРучные позиции перезапишутся.', ok: 'Переразложить',
}) && autoLayout());
$('#btnFit').addEventListener('click', () => fit());
$('#btnZoomIn').addEventListener('click', () => zoomAt(S.view.k * 1.2, canvas.clientWidth / 2, canvas.clientHeight / 2));
$('#btnZoomOut').addEventListener('click', () => zoomAt(S.view.k / 1.2, canvas.clientWidth / 2, canvas.clientHeight / 2));
$('#btnDelTopo').addEventListener('click', async () => {
  if (!S.topo || !await ask({
    title: '🗑 Удалить схему?', text: `«${S.topo.name}» — ${S.nodes.size} узлов и ${S.edges.size} связей.\nОтменить будет нельзя.`, ok: 'Удалить схему', danger: true,
  })) return;
  await api('DELETE', 'topologies/' + S.topo.id);
  await loadTopos(); S.topos.length ? openTopo(S.topos[0].id) : noTopo();
});
$('#btnDup').addEventListener('click', async () => {
  if (!S.topo) return;
  const r = await api('POST', `topologies/${S.topo.id}/duplicate`);
  await loadTopos(); openTopo(r.id); toast('Копия создана');
});
$('#btnExport').addEventListener('click', async () => {
  if (!S.topo) return;
  const d = await api('GET', `topologies/${S.topo.id}/export`);
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([JSON.stringify(d, null, 2)], { type: 'application/json' }));
  a.download = `topology-${S.topo.name.replace(/[^\wа-яё-]+/gi, '_')}.json`;
  a.click();
});
$('#btnRestore').addEventListener('click', () => $('#restoreFile').click());
$('#restoreFile').addEventListener('change', async (ev) => {
  const f = ev.target.files[0]; if (!f) return;
  try {
    const r = await api('POST', 'restore', JSON.parse(await f.text()));
    await loadTopos(); openTopo(r.id); toast('Топология загружена');
  } catch (e) { toast('Ошибка: ' + e.message); }
  ev.target.value = '';
});
$('#panelClose').addEventListener('click', () => select(null));
$('#search').addEventListener('input', (ev) => {
  S.search = ev.target.value; SR.active = -1;
  if (S.sel) S.sel = null, closePanel();  // иначе подсветка цепочки выбранного узла спрячет найденное
  applyHighlight(); renderSearchList();
});
$('#search').addEventListener('focus', renderSearchList);
$('#search').addEventListener('blur', () => $('#searchList').classList.add('hidden'));
$('#search').addEventListener('keydown', (ev) => {
  if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
    ev.preventDefault();
    if (!SR.items.length) return;
    const n = Math.min(SR.items.length, 60);
    SR.active = ((SR.active + (ev.key === 'ArrowDown' ? 1 : -1)) % n + n) % n;
    goSearchItem(SR.active);
  }
  if (ev.key === 'Enter') {
    if (SR.active >= 0) goSearchItem(SR.active);
    else if (SR.items.length === 1) goSearchItem(0);
    else if (SR.items.length) fit(SR.items.map((x) => x.n));
  }
  if (ev.key === 'Escape') {
    ev.target.value = ''; S.search = ''; SR.active = -1;
    applyHighlight(); renderSearchList(); ev.target.blur();
  }
});
window.addEventListener('resize', () => { if (!$('#searchList').classList.contains('hidden')) renderSearchList(); });

window.addEventListener('keydown', (ev) => {
  if (ev.target.closest('input, textarea, select, dialog')) return;
  if ((ev.key === 'Delete' || ev.key === 'Backspace') && S.sel && canEdit()) { ev.preventDefault(); deleteSel(); }
  if (ev.key === 'Escape') select(null);
  if (ev.key === 'f' || ev.key === 'а') fit();
  if (ev.key === '/') { ev.preventDefault(); $('#search').focus(); }
});

window.addEventListener('resize', () => renderEdges());

/* ------------------------------------------------------------ левое меню и ширина панели */

const store = {
  get: (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* приватный режим */ } },
};

function setSide(collapsed) {
  document.body.classList.toggle('side-collapsed', collapsed);
  store.set('shm-side-collapsed', collapsed ? '1' : '');
  setTimeout(renderEdges, 200);
}
$('#btnSide').addEventListener('click', () => setSide(!document.body.classList.contains('side-collapsed')));
window.addEventListener('keydown', (ev) => {
  if (ev.key === '[' && !ev.target.closest('input, textarea, select, dialog')) setSide(!document.body.classList.contains('side-collapsed'));
});
setSide(store.get('shm-side-collapsed') === '1');

function setPanelW(w) {
  w = Math.round(Math.max(300, Math.min(window.innerWidth * 0.75, w)));
  document.documentElement.style.setProperty('--panel-w', w + 'px');
  return w;
}
if (+store.get('shm-panel-w')) setPanelW(+store.get('shm-panel-w'));
$('#panelResize').addEventListener('mousedown', (ev) => {
  ev.preventDefault();
  document.body.classList.add('resizing');
  const move = (e) => setPanelW(window.innerWidth - e.clientX);
  const up = (e) => {
    document.body.classList.remove('resizing');
    window.removeEventListener('mousemove', move);
    window.removeEventListener('mouseup', up);
    store.set('shm-panel-w', setPanelW(window.innerWidth - e.clientX));
  };
  window.addEventListener('mousemove', move);
  window.addEventListener('mouseup', up);
});
$('#panelResize').addEventListener('dblclick', () => { store.set('shm-panel-w', setPanelW(380)); });

$('#btnLogout').addEventListener('click', async () => {
  if (!await ask({ title: 'Выйти?', ok: 'Выйти' })) return;
  await fetch('/api/logout', { method: 'POST' });
  location.href = '/login.html';
});

(async function init() {
  const me = await api('GET', 'me');
  S.role = me.role;
  document.body.classList.toggle('viewer', me.role !== 'admin');
  $('#meLogin').textContent = me.login;
  $('#meRole').textContent = me.role === 'admin' ? 'админ' : 'просмотр';
  applyView();
  await loadTopos();
  const last = +localStorage.getItem('shm-topo-last');
  if (S.topos.find((t) => t.id === last)) openTopo(last);
  else if (S.topos.length) openTopo(S.topos[0].id);
  else noTopo();
})();
