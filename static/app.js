'use strict';

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const TYPES = {
  event: { icon: '⚡', name: 'Событие', color: 'var(--c-event)' },
  template: { icon: '🧩', name: 'Шаблон', color: 'var(--c-template)' },
  tg_message: { icon: '💬', name: 'TG-сообщение', color: 'var(--c-tg_message)' },
  email: { icon: '✉️', name: 'Письмо', color: 'var(--c-email)' },
  job: { icon: '⏱', name: 'Задача', color: 'var(--c-job)' },
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
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
};

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
  $('#topoName').value = S.topo.name; $('#topoName').disabled = false;
  $('#empty').classList.add('hidden');
  closePanel();
  renderAll();
  const needLayout = [...S.nodes.values()].some((n) => n.x == null);
  if (needLayout) {
    autoLayout(true);
  } else {
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

function nodeHTML(n) {
  const T = TYPES[n.type] || TYPES.note;
  const m = n.meta || {};
  let body = '';
  const text = n.content || '';
  if (text) body += `<div class="txt${text.split('\n').length > 9 || text.length > 420 ? ' clip' : ''}">${esc(text.slice(0, 900))}</div>`;
  if (m.buttons && m.buttons.length) {
    body += '<div class="kb">' + m.buttons.map((row) => '<div class="row">' + row.map((b) =>
      `<span class="b ${esc(b.type || '')}" title="${esc((b.type || '') + ': ' + (b.action || ''))}">${esc(b.text)}</span>`).join('') + '</div>').join('') + '</div>';
  }
  const whenList = (m.when || []).filter(Boolean);
  if (whenList.length) body = `<div class="when"><b>Когда:</b>${whenList.map((w) => `<div>▸ ${esc(w)}</div>`).join('')}</div>` + body;
  if (m.guards && m.guards.length) body = `<div class="when guard"><b>Выполняется, если:</b>${m.guards.map((w) => `<div>▸ ${esc(w)}</div>`).join('')}</div>` + body;
  let sub = n.subtitle || '';
  if (n.type === 'email' && m.subject && m.subject !== n.title) sub = 'Тема: ' + m.subject;
  const flags = [];
  if (m.missing) flags.push('нет исходника в библиотеке');
  if (m.http && m.http.length) flags.push('HTTP×' + m.http.length);
  if (m.stops && m.stops.length) flags.push('⛔ STOP×' + m.stops.length);
  return `<div class="hd"><span class="ic">${T.icon}</span><div><div class="tag">${T.name}</div><div class="tt">${esc(n.title || n.key || '(без названия)')}</div></div></div>
    ${sub ? `<div class="st">${esc(sub)}</div>` : ''}
    ${body ? `<div class="bd">${body}</div>` : ''}
    ${flags.length ? `<div class="flag">${esc(flags.join(' · '))}</div>` : ''}
    <div class="port" title="Потяните, чтобы связать"></div>`;
}

function renderNode(n) {
  let el = nodesEl.querySelector(`[data-id="${n.id}"]`);
  if (!el) {
    el = document.createElement('div');
    el.dataset.id = n.id;
    nodesEl.appendChild(el);
  }
  el.className = `node t-${n.type}${n.meta && n.meta.missing ? ' missing' : ''}`;
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
  return { x: n.x || 0, y: n.y || 0, w: el ? el.offsetWidth : 260, h: el ? el.offsetHeight : 60 };
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
    out += `<g class="edge ${esc(e.kind || 'link')}${sel}" data-eid="${e.id}">
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
    const hit = q && [n.title, n.key, n.subtitle, n.content].some((v) => (v || '').toLowerCase().includes(q));
    n._el.classList.toggle('hit', !!hit);
    n._el.classList.toggle('dim', (keep && !keep.has(n.id)) || (!!q && !hit));
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
  if (port && nodeEl) {
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
  if (!S.topo || ev.target.closest('.node') || ev.target.closest('.edge')) return;
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
      <label class="field">Ключ / template_id<input class="input" data-f="key" value="${esc(n.key)}"></label>
    </div>
    <label class="field">Название<input class="input" data-f="title" value="${esc(n.title)}"></label>
    ${n.type === 'email' ? `<label class="field">Тема письма<input class="input" data-m="subject" value="${esc(m.subject || '')}"></label>` : ''}
    <label class="field">Подзаголовок / описание<input class="input" data-f="subtitle" value="${esc(n.subtitle)}"></label>
    <label class="field">${n.type === 'tg_message' ? 'Текст сообщения' : n.type === 'email' ? 'Текст письма' : 'Содержимое / заметки'}
      <textarea class="input big" data-f="content">${esc(n.content)}</textarea></label>
    ${n.type === 'tg_message' || (m.buttons && m.buttons.length) ? `<label class="field">Кнопки — строка = ряд, <code>||</code> между кнопками, <code>Текст => /callback</code> или <code>=> url:https://…</code>
      <textarea class="input" data-buttons placeholder="💰 Баланс => /balance\n📖 Меню => /menu  ||  Канал => url:https://t.me/…">${esc(buttonsToText(m.buttons))}</textarea></label>` : ''}
    ${(m.when || []).length ? `<div class="field">Когда срабатывает<ul class="conds">${m.when.map((w) => `<li>${esc(w)}</li>`).join('')}</ul></div>` : ''}
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
  body.querySelector('[data-act="del"]').addEventListener('click', () => deleteSel());
}

async function deleteSel() {
  if (!S.sel) return;
  if (S.sel.kind === 'node') {
    const n = S.nodes.get(S.sel.id);
    if (!confirm(`Удалить узел «${n.title || n.key}» и его связи?`)) return;
    await api('DELETE', 'nodes/' + n.id);
    S.nodes.delete(n.id); n._el.remove();
    [...S.edges.values()].filter((e) => e.source_id === n.id || e.target_id === n.id).forEach((e) => S.edges.delete(e.id));
  } else {
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

function updImpInfo() {
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
  $('#impDir').value = ''; $('#impEvents').value = ''; $('#impSql').value = ''; updImpInfo();
  $('#importDlg').showModal();
});

$('#importDlg').addEventListener('close', async () => {
  if ($('#importDlg').returnValue !== 'default') return;
  if (!imp.events && !Object.keys(imp.templates).length && !imp.sql.length) return toast('Нечего импортировать');
  toast('Строю связи…');
  const res = await api('POST', 'import', {
    events: imp.events, templates: imp.templates, settings: imp.settings, sql: imp.sql,
    name: $('#impName').value, split: $('#impSplit').checked,
  });
  await loadTopos();
  if (res.topologies.length) {
    toast(`Готово: топологий ${res.topologies.length}, шаблонов ${res.templates}${res.sql ? ` (из SQL ${res.sql.templates})` : ''}`);
    openTopo(res.topologies[0]);
  } else toast(`Библиотека шаблонов обновлена: ${res.templates}`);
});

/* ------------------------------------------------------------ кнопки */

$('#topoList').addEventListener('click', (ev) => { const li = ev.target.closest('li'); if (li) openTopo(+li.dataset.id); });
$('#topoFilter').addEventListener('input', renderTopoList);
$('#btnNewTopo').addEventListener('click', async () => {
  const name = prompt('Название топологии', 'Новая топология');
  if (!name) return;
  const r = await api('POST', 'topologies', { name });
  await loadTopos(); openTopo(r.id);
});
$('#topoName').addEventListener('change', async (ev) => {
  await api('PATCH', 'topologies/' + S.topo.id, { name: ev.target.value });
  S.topo.name = ev.target.value; await loadTopos();
});
$('#btnAdd').addEventListener('click', () => addNode($('#addType').value));
$('#btnLayout').addEventListener('click', () => S.topo && confirm('Переразложить все узлы автоматически? Ручные позиции будут перезаписаны.') && autoLayout());
$('#btnFit').addEventListener('click', () => fit());
$('#btnZoomIn').addEventListener('click', () => zoomAt(S.view.k * 1.2, canvas.clientWidth / 2, canvas.clientHeight / 2));
$('#btnZoomOut').addEventListener('click', () => zoomAt(S.view.k / 1.2, canvas.clientWidth / 2, canvas.clientHeight / 2));
$('#btnDelTopo').addEventListener('click', async () => {
  if (!S.topo || !confirm(`Удалить топологию «${S.topo.name}» целиком?`)) return;
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
$('#search').addEventListener('input', (ev) => { S.search = ev.target.value; applyHighlight(); });
$('#search').addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') {
    const hits = [...S.nodes.values()].filter((n) => n._el.classList.contains('hit'));
    if (hits.length) { fit(hits); if (hits.length === 1) select({ kind: 'node', id: hits[0].id }); }
  }
  if (ev.key === 'Escape') { ev.target.value = ''; S.search = ''; applyHighlight(); ev.target.blur(); }
});

window.addEventListener('keydown', (ev) => {
  if (ev.target.closest('input, textarea, select, dialog')) return;
  if ((ev.key === 'Delete' || ev.key === 'Backspace') && S.sel) { ev.preventDefault(); deleteSel(); }
  if (ev.key === 'Escape') select(null);
  if (ev.key === 'f' || ev.key === 'а') fit();
  if (ev.key === '/') { ev.preventDefault(); $('#search').focus(); }
});

window.addEventListener('resize', () => renderEdges());

(async function init() {
  applyView();
  await loadTopos();
  const last = +localStorage.getItem('shm-topo-last');
  if (S.topos.find((t) => t.id === last)) openTopo(last);
  else if (S.topos.length) openTopo(S.topos[0].id);
  else noTopo();
})();
