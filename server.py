#!/usr/bin/env python3
"""SHM Topology — редактор связей событий, задач и шаблонов SHM.

Запуск:  python3 server.py [--port 8765] [--db topology.db]
Только стандартная библиотека Python.
"""
import argparse
import json
import mimetypes
import os
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

import sqldump
import tplparse

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, 'static')
LOCK = threading.Lock()
DB_PATH = os.environ.get('SHM_TOPO_DB') or os.path.join(BASE, 'data', 'topology.db')

SCHEMA = """
CREATE TABLE IF NOT EXISTS topologies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS nodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topology_id INTEGER NOT NULL REFERENCES topologies(id) ON DELETE CASCADE,
  type TEXT NOT NULL DEFAULT 'template',   -- event | template | tg_message | email | job | note
  key TEXT DEFAULT '',                     -- template_id / имя события
  title TEXT DEFAULT '',
  subtitle TEXT DEFAULT '',
  content TEXT DEFAULT '',
  meta TEXT DEFAULT '{}',                  -- JSON: buttons, subject, http, missing ...
  x REAL, y REAL
);
CREATE TABLE IF NOT EXISTS edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topology_id INTEGER NOT NULL REFERENCES topologies(id) ON DELETE CASCADE,
  source_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  target_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  kind TEXT DEFAULT 'link',                -- handler | delayed | periodic | sends | calls | link
  label TEXT DEFAULT '',
  meta TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS templates (
  name TEXT PRIMARY KEY,
  content TEXT DEFAULT '',
  settings TEXT DEFAULT '{}',
  updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_nodes_topo ON nodes(topology_id);
CREATE INDEX IF NOT EXISTS idx_edges_topo ON edges(topology_id);
"""

EVENT_NAMES = {
    'registered': 'Регистрация пользователя',
    'create': 'Создание услуги',
    'activate': 'Активация услуги',
    'block': 'Блокировка услуги',
    'remove': 'Удаление услуги',
    'prolongate': 'Продление услуги',
    'payment': 'Поступление платежа',
    'bonus': 'Начисление бонуса',
    'changed': 'Изменение статуса услуги',
    'changed_tariff': 'Смена тарифа',
    'forecast': 'Прогноз оплаты',
    'not_enough_money': 'Недостаточно средств',
    'user_password_reset': 'Сброс пароля',
    'touch': 'Touch услуги',
}

NODE_COLS = ('type', 'key', 'title', 'subtitle', 'content', 'meta', 'x', 'y')
EDGE_COLS = ('source_id', 'target_id', 'kind', 'label', 'meta')


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    return con


def init_db():
    with db() as con:
        con.executescript(SCHEMA)


def row(r):
    d = dict(r)
    if 'meta' in d:
        try:
            d['meta'] = json.loads(d['meta'] or '{}')
        except ValueError:
            d['meta'] = {}
    return d


def _meta(v):
    return v if isinstance(v, str) else json.dumps(v or {}, ensure_ascii=False)


def touch(con, tid):
    con.execute("UPDATE topologies SET updated_at = datetime('now') WHERE id = ?", (tid,))


# ---------------------------------------------------------------- импорт

class Builder:
    """Строит граф одной топологии из событий и библиотеки шаблонов."""

    def __init__(self, con, tid, depth=6):
        self.con, self.tid, self.depth = con, tid, depth
        self.nodes = {}
        self.edges = set()
        self.lib = {r['name']: row(r) for r in con.execute('SELECT name, content, settings AS meta FROM templates')}

    def node(self, uid, **f):
        if uid in self.nodes:
            return self.nodes[uid], False
        cur = self.con.execute(
            'INSERT INTO nodes (topology_id, type, key, title, subtitle, content, meta) VALUES (?,?,?,?,?,?,?)',
            (self.tid, f.get('type', 'template'), f.get('key', ''), f.get('title', ''), f.get('subtitle', ''),
             f.get('content', ''), _meta(f.get('meta'))))
        self.nodes[uid] = cur.lastrowid
        return cur.lastrowid, True

    def edge(self, s, t, kind, label='', meta=None):
        k = (s, t, kind, label)
        if k in self.edges:
            return
        self.edges.add(k)
        self.con.execute('INSERT INTO edges (topology_id, source_id, target_id, kind, label, meta) VALUES (?,?,?,?,?,?)',
                         (self.tid, s, t, kind, label, _meta(meta)))

    def event(self, ev):
        name = ev.get('name') or '?'
        kind = ev.get('kind') or ''
        st = ev.get('settings') or {}
        en, _ = self.node('event:' + kind + ':' + name, type='event', key=name, title=name,
                          subtitle=EVENT_NAMES.get(name, kind), meta={'kind': kind})
        tpl = st.get('template_id')
        if not tpl:
            return
        tpl_settings = (self.lib.get(tpl) or {}).get('meta') or {}
        subject = st.get('subject') or tpl_settings.get('subject')
        tn = self.template(tpl, email=bool(st.get('subject')), subject=subject)
        parts = []
        cat = st.get('category')
        if cat and cat != '%':
            parts.append(cat)
        if ev.get('title') and ev.get('title') != tpl:
            parts.append(ev['title'])
        self.edge(en, tn, 'handler', ' · '.join(parts),
                  {'event_id': ev.get('id'), 'category': cat, 'server_gid': ev.get('server_gid'),
                   'event_title': ev.get('title')})

    def template(self, name, email=False, subject=None, level=0):
        src = (self.lib.get(name) or {}).get('content')
        settings = (self.lib.get(name) or {}).get('meta') or {}
        info = tplparse.analyze(src) if src else None
        meta = {'missing': src is None}
        if settings:
            meta['settings'] = settings
        if info:
            if info['http']:
                meta['http'] = info['http']
            if info['guards']:
                meta['guards'] = info['guards']
            if info['stops']:
                meta['stops'] = [x['when'] for x in info['stops']]
        nid, created = self.node('tpl:' + name, type='template', key=name, title=name,
                                 subtitle=(info or {}).get('description', ''), meta=meta)
        if email and ('mail:' + name) not in self.nodes:
            text = (info or {}).get('email_text', '')
            mid, _ = self.node('mail:' + name, type='email', key=name, title=subject or name,
                               content=text, meta={'subject': subject or ''})
            self.edge(nid, mid, 'sends', 'письмо')
        if not created or not info or level >= self.depth:
            return nid
        for i, m in enumerate(info['tg']):
            mid, _ = self.node(f'tg:{name}:{i}', type='tg_message', key=name, title=m['method'],
                               subtitle=m['media'], content=m['text'],
                               meta={'buttons': m['buttons'], 'when': m['when']})
            self.edge(nid, mid, 'sends', '' if m['when'] else 'Telegram', {'when': m['when']})
        for j in info['spool']:
            j['when'] = [w for w in j['when'] if w not in info['guards']]
            tpl_settings = (self.lib.get(j['template']) or {}).get('meta') or {}
            is_mail = j['transport'] == 'mail'
            tn = self.template(j['template'], email=is_mail,
                               subject=tpl_settings.get('subject'), level=level + 1)
            if j['period']:
                self.edge(nid, tn, 'periodic', 'каждые ' + tplparse.human_seconds(j['period']), j)
            else:
                if j.get('custom_event'):
                    ce = j['custom_event']
                    lbl = 'custom event' + (f' {ce}' if isinstance(ce, str) else '')
                elif j['delay']:
                    lbl = 'через ' + tplparse.human_seconds(j['delay'])
                elif j.get('delay_expr'):
                    lbl = 'через ' + j['delay_expr'] + ' сек'
                else:
                    lbl = 'сразу'
                self.edge(nid, tn, 'delayed', lbl, j)
        for c in info['calls']:
            tn = self.template(c, level=level + 1)
            self.edge(nid, tn, 'calls', 'вызывает')
        return nid


def extract_events(data):
    if isinstance(data, dict):
        for k in ('data', 'items', 'events', 'result'):
            if isinstance(data.get(k), list):
                return data[k]
        return [data]
    return data if isinstance(data, list) else []


def do_import(payload):
    events = extract_events(payload.get('events'))
    templates = payload.get('templates') or {}
    settings = payload.get('settings') or {}
    sql_info = None
    for sql in payload.get('sql') or []:
        # SQL-дамп дополняет файлы; при совпадении имён приоритет у дампа (он свежее выгрузки из SHM)
        t, st, ev = sqldump.extract(sql)
        templates.update(t)
        settings.update(st)
        if ev and not events:
            events = ev
        sql_info = {'templates': len(t), 'events': len(ev)}
    name = (payload.get('name') or 'Импорт').strip()
    split = bool(payload.get('split'))
    created = []
    with LOCK, db() as con:
        for tname, content in templates.items():
            st = settings.get(tname)
            con.execute(
                "INSERT INTO templates (name, content, settings, updated_at) VALUES (?,?,?,datetime('now')) "
                "ON CONFLICT(name) DO UPDATE SET content=excluded.content, "
                "settings=COALESCE(excluded.settings, templates.settings), updated_at=excluded.updated_at",
                (tname, content, json.dumps(st, ensure_ascii=False) if st is not None else '{}'))
        for tname, st in settings.items():
            if tname not in templates:
                con.execute("UPDATE templates SET settings=? WHERE name=?", (json.dumps(st, ensure_ascii=False), tname))
        if not events:
            return {'topologies': [], 'templates': len(templates), 'sql': sql_info}
        if split:
            groups = {}
            for ev in events:
                groups.setdefault(ev.get('name') or '?', []).append(ev)
            order = sorted(groups)
            groups = [(g + (f' — {EVENT_NAMES[g]}' if g in EVENT_NAMES else ''), groups[g]) for g in order]
        else:
            groups = [(name, sorted(events, key=lambda e: (e.get('name') or '', e.get('id') or 0)))]
        for tname, evs in groups:
            cur = con.execute('INSERT INTO topologies (name, description) VALUES (?, ?)',
                              (tname, f'Импортировано событий: {len(evs)}'))
            b = Builder(con, cur.lastrowid, depth=int(payload.get('depth') or 6))
            for ev in evs:
                b.event(ev)
            created.append(cur.lastrowid)
    return {'topologies': created, 'templates': len(templates), 'sql': sql_info}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = 'SHMTopology/1.0'

    def log_message(self, fmt, *args):
        if not self.path.startswith('/static'):
            super().log_message(fmt, *args)

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(n) or b'{}') if n else {}

    def do_GET(self):
        self.route('GET')

    def do_POST(self):
        self.route('POST')

    def do_PATCH(self):
        self.route('PATCH')

    def do_DELETE(self):
        self.route('DELETE')

    def route(self, method):
        path = unquote(urlparse(self.path).path)
        try:
            if path.startswith('/api/'):
                return self.api(method, path[5:].strip('/').split('/'))
            if method == 'GET':
                return self.static(path)
            self.send_json({'error': 'not found'}, 404)
        except Exception as e:  # noqa
            import traceback
            traceback.print_exc()
            self.send_json({'error': str(e)}, 500)

    def static(self, path):
        if path in ('/', ''):
            path = '/index.html'
        full = os.path.normpath(os.path.join(STATIC, path.lstrip('/')))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            return self.send_json({'error': 'not found'}, 404)
        with open(full, 'rb') as f:
            data = f.read()
        self.send_response(200)
        ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
        if ctype.startswith('text/') or ctype.endswith('javascript'):
            ctype += '; charset=utf-8'
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(data)

    def api(self, m, p):
        res, n = p[0], len(p)
        with LOCK, db() as con:
            # --- топологии
            if res == 'topologies':
                if n == 1 and m == 'GET':
                    rows = con.execute(
                        'SELECT t.*, (SELECT COUNT(*) FROM nodes WHERE topology_id=t.id) AS nodes_count '
                        'FROM topologies t ORDER BY t.id').fetchall()
                    return self.send_json([dict(r) for r in rows])
                if n == 1 and m == 'POST':
                    b = self.body()
                    cur = con.execute('INSERT INTO topologies (name, description) VALUES (?, ?)',
                                      (b.get('name') or 'Новая топология', b.get('description') or ''))
                    return self.send_json({'id': cur.lastrowid})
                tid = int(p[1])
                if n == 2 and m == 'GET':
                    t = con.execute('SELECT * FROM topologies WHERE id=?', (tid,)).fetchone()
                    if not t:
                        return self.send_json({'error': 'not found'}, 404)
                    nodes = [row(r) for r in con.execute('SELECT * FROM nodes WHERE topology_id=?', (tid,))]
                    edges = [row(r) for r in con.execute('SELECT * FROM edges WHERE topology_id=?', (tid,))]
                    return self.send_json({'topology': dict(t), 'nodes': nodes, 'edges': edges})
                if n == 2 and m == 'PATCH':
                    b = self.body()
                    for k in ('name', 'description'):
                        if k in b:
                            con.execute(f'UPDATE topologies SET {k}=? WHERE id=?', (b[k], tid))
                    touch(con, tid)
                    return self.send_json({'ok': True})
                if n == 2 and m == 'DELETE':
                    con.execute('DELETE FROM topologies WHERE id=?', (tid,))
                    return self.send_json({'ok': True})
                if n == 3 and p[2] == 'duplicate' and m == 'POST':
                    return self.send_json({'id': self.copy_topology(con, tid)})
                if n == 3 and p[2] == 'nodes' and m == 'POST':
                    b = self.body()
                    vals = [b.get('type') or 'note', b.get('key') or '', b.get('title') or '', b.get('subtitle') or '',
                            b.get('content') or '', _meta(b.get('meta')), b.get('x'), b.get('y')]
                    cur = con.execute(f'INSERT INTO nodes (topology_id, {",".join(NODE_COLS)}) VALUES (?{",?" * len(NODE_COLS)})',
                                      [tid] + vals)
                    touch(con, tid)
                    return self.send_json(row(con.execute('SELECT * FROM nodes WHERE id=?', (cur.lastrowid,)).fetchone()))
                if n == 3 and p[2] == 'edges' and m == 'POST':
                    b = self.body()
                    cur = con.execute('INSERT INTO edges (topology_id, source_id, target_id, kind, label, meta) VALUES (?,?,?,?,?,?)',
                                      (tid, b['source_id'], b['target_id'], b.get('kind') or 'link', b.get('label') or '',
                                       _meta(b.get('meta'))))
                    touch(con, tid)
                    return self.send_json(row(con.execute('SELECT * FROM edges WHERE id=?', (cur.lastrowid,)).fetchone()))
                if n == 3 and p[2] == 'positions' and m == 'POST':
                    for it in self.body().get('items', []):
                        con.execute('UPDATE nodes SET x=?, y=? WHERE id=? AND topology_id=?', (it['x'], it['y'], it['id'], tid))
                    return self.send_json({'ok': True})
                if n == 3 and p[2] == 'export' and m == 'GET':
                    t = dict(con.execute('SELECT * FROM topologies WHERE id=?', (tid,)).fetchone())
                    nodes = [row(r) for r in con.execute('SELECT * FROM nodes WHERE topology_id=?', (tid,))]
                    edges = [row(r) for r in con.execute('SELECT * FROM edges WHERE topology_id=?', (tid,))]
                    return self.send_json({'format': 'shm-topology/1', 'topology': t, 'nodes': nodes, 'edges': edges})
            if res == 'restore' and m == 'POST':
                return self.send_json({'id': self.restore(con, self.body())})
            # --- узлы / связи
            if res in ('nodes', 'edges') and n == 2:
                table, cols = (res, NODE_COLS) if res == 'nodes' else (res, EDGE_COLS)
                iid = int(p[1])
                r = con.execute(f'SELECT topology_id FROM {table} WHERE id=?', (iid,)).fetchone()
                if not r:
                    return self.send_json({'error': 'not found'}, 404)
                if m == 'PATCH':
                    b = self.body()
                    for k, v in b.items():
                        if k in cols:
                            con.execute(f'UPDATE {table} SET {k}=? WHERE id=?', (_meta(v) if k == 'meta' else v, iid))
                    touch(con, r['topology_id'])
                    return self.send_json(row(con.execute(f'SELECT * FROM {table} WHERE id=?', (iid,)).fetchone()))
                if m == 'DELETE':
                    con.execute(f'DELETE FROM {table} WHERE id=?', (iid,))
                    touch(con, r['topology_id'])
                    return self.send_json({'ok': True})
            # --- библиотека шаблонов
            if res == 'templates':
                if n == 1 and m == 'GET':
                    return self.send_json([r[0] for r in con.execute('SELECT name FROM templates ORDER BY name')])
                if n == 2 and m == 'GET':
                    r = con.execute('SELECT * FROM templates WHERE name=?', (p[1],)).fetchone()
                    return self.send_json(row(r) if r else {'error': 'not found'}, 200 if r else 404)
        if res == 'import' and m == 'POST':
            return self.send_json(do_import(self.body()))
        self.send_json({'error': 'not found'}, 404)

    def copy_topology(self, con, tid):
        t = con.execute('SELECT * FROM topologies WHERE id=?', (tid,)).fetchone()
        nodes = [row(r) for r in con.execute('SELECT * FROM nodes WHERE topology_id=?', (tid,))]
        edges = [row(r) for r in con.execute('SELECT * FROM edges WHERE topology_id=?', (tid,))]
        return self.restore(con, {'topology': {'name': t['name'] + ' (копия)', 'description': t['description']},
                                  'nodes': nodes, 'edges': edges})

    def restore(self, con, data):
        t = data.get('topology') or {}
        cur = con.execute('INSERT INTO topologies (name, description) VALUES (?, ?)',
                          (t.get('name') or 'Восстановленная', t.get('description') or ''))
        tid, ids = cur.lastrowid, {}
        for nd in data.get('nodes', []):
            c = con.execute(f'INSERT INTO nodes (topology_id, {",".join(NODE_COLS)}) VALUES (?{",?" * len(NODE_COLS)})',
                            [tid] + [_meta(nd.get(k)) if k == 'meta' else nd.get(k) for k in NODE_COLS])
            ids[nd['id']] = c.lastrowid
        for e in data.get('edges', []):
            if e['source_id'] in ids and e['target_id'] in ids:
                con.execute('INSERT INTO edges (topology_id, source_id, target_id, kind, label, meta) VALUES (?,?,?,?,?,?)',
                            (tid, ids[e['source_id']], ids[e['target_id']], e.get('kind'), e.get('label'), _meta(e.get('meta'))))
        return tid


def main():
    global DB_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=int(os.environ.get('SHM_TOPO_PORT') or 8765))
    ap.add_argument('--host', default=os.environ.get('SHM_TOPO_HOST') or '127.0.0.1')
    ap.add_argument('--db', default=DB_PATH)
    a = ap.parse_args()
    DB_PATH = os.path.abspath(a.db)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    init_db()
    print(f'SHM Topology: http://{a.host}:{a.port}  (db: {DB_PATH})')
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
