#!/usr/bin/env python3
"""SHM Topology — редактор связей событий, задач и шаблонов SHM.

Запуск:  python3 server.py [--port 8765] [--db topology.db]
Только стандартная библиотека Python.
"""
import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import sqlite3
import threading
import time
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


def load_env(path):
    """Минимальный парсер .env: KEY=value, # комментарии, кавычки. Переменные окружения важнее файла."""
    if not os.path.isfile(path):
        return
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in '"\'':
                v = v[1:-1]
            os.environ.setdefault(k, v)


def env(*names, default=''):
    for n in names:  # допускаем и ADMIN_PASS, и admin_pass
        for k in (n.upper(), n.lower()):
            if os.environ.get(k):
                return os.environ[k]
    return default


class Auth:
    COOKIE = 'shm_topo'
    TTL = 30 * 86400

    def __init__(self):
        self.users = {}
        admin_login, admin_pass = env('ADMIN_LOGIN', default='admin'), env('ADMIN_PASS')
        user_login, user_pass = env('USER_LOGIN'), env('USER_PASS')
        if admin_pass:
            self.users[admin_login] = (admin_pass, 'admin')
        if user_login and user_pass:
            self.users[user_login] = (user_pass, 'viewer')
        # ключ подписи сессий: SESSION_SECRET или производный от паролей (смена пароля = разлогин всех)
        seed = env('SESSION_SECRET') or json.dumps(sorted((k, v[0]) for k, v in self.users.items()))
        self.key = hashlib.sha256(('shm-topology:' + seed).encode()).digest()
        self.fails = {}

    def check(self, login, password):
        u = self.users.get(login)
        ok = bool(u) and hmac.compare_digest(u[0].encode(), (password or '').encode())
        return (u[1] if ok else None)

    def sign(self, login, role):
        exp = int(time.time()) + self.TTL
        payload = f'{login}|{role}|{exp}'
        sig = hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()
        return f'{payload}|{sig}'

    def verify(self, token):
        try:
            login, role, exp, sig = token.rsplit('|', 3)
        except (AttributeError, ValueError):
            return None
        good = hmac.new(self.key, f'{login}|{role}|{exp}'.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig) or int(exp) < time.time():
            return None
        u = self.users.get(login)
        if not u or u[1] != role:  # пользователя убрали из .env или сменили роль
            return None
        return {'login': login, 'role': role}


AUTH = None


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    return con


def init_db():
    with db() as con:
        con.executescript(SCHEMA)
        # миграции: колонки для повторного импорта
        for table, col, ddl in (('topologies', 'source', 'TEXT'), ('nodes', 'uid', 'TEXT'),
                                ('nodes', 'auto', 'INTEGER DEFAULT 0'), ('nodes', 'edited', 'INTEGER DEFAULT 0'),
                                ('edges', 'uid', 'TEXT'), ('edges', 'auto', 'INTEGER DEFAULT 0')):
            cols = [r[1] for r in con.execute(f'PRAGMA table_info({table})')]
            if col not in cols:
                con.execute(f'ALTER TABLE {table} ADD COLUMN {col} {ddl}')


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

USER_PROFILES = {p.strip() for p in (os.environ.get('SHM_TOPO_USER_PROFILES') or 'telegram_bot').split(',') if p.strip()}
# поля, которые пользователь мог поправить руками — при повторном импорте их не трогаем
USER_FIELDS = ('title', 'subtitle', 'content')
USER_META = ('buttons', 'subject')


def recipient(profile):
    if not profile or profile in USER_PROFILES:
        return 'user', '👤 Пользователю'
    return 'group', f'👥 Группа {profile}'


class Builder:
    """Строит граф в памяти: узлы и связи со стабильными uid, чтобы повторный импорт мог обновлять схему."""

    def __init__(self, lib, depth=6, prune=False):
        self.lib, self.depth, self.prune = lib, depth, prune
        self.nodes = {}          # uid -> поля
        self.edges = {}          # uid -> {s, t, kind, label, meta}
        self._info = {}

    def info(self, name):
        if name not in self._info:
            src = (self.lib.get(name) or {}).get('content')
            self._info[name] = tplparse.analyze(src) if src is not None else None
        return self._info[name]

    def settings(self, name):
        return (self.lib.get(name) or {}).get('meta') or {}

    def node(self, uid, **f):
        created = uid not in self.nodes
        if created:
            self.nodes[uid] = {'type': 'template', 'key': '', 'title': '', 'subtitle': '', 'content': '', 'meta': {}, **f}
        return uid, created

    def edge(self, s, t, kind, label='', meta=None):
        base = f'{s}|{t}|{kind}'
        uid, n = base, 1
        while uid in self.edges:
            if self.edges[uid]['label'] == label:
                return
            n += 1
            uid = f'{base}#{n}'
        self.edges[uid] = {'s': s, 't': t, 'kind': kind, 'label': label, 'meta': meta or {}}

    # --- входы
    def event(self, ev):
        name = ev.get('name') or '?'
        kind = ev.get('kind') or ''
        st = ev.get('settings') or {}
        en, _ = self.node('event:' + kind + ':' + name, type='event', key=name, title=name,
                          subtitle=EVENT_NAMES.get(name, kind), meta={'kind': kind})
        tpl = st.get('template_id')
        if not tpl:
            return
        subject = st.get('subject') or self.settings(tpl).get('subject')
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

    def public(self, name):
        """Публичный шаблон (allow_public): узлы-входы по веткам диспетчера или один вход API."""
        tn = self.template(name)
        info = self.info(name)
        ent = info and info.get('entries')
        if ent:
            for v in ent['values']:
                en, _ = self.node(f'entry:{name}:{v}', type='entry', key=v,
                                  title=tplparse.REMNA_RU.get(v, v), subtitle=f'{name} · {ent["param"]} = {v}',
                                  meta={'template': name, 'param': ent['param'], 'value': v})
                self.edge(en, tn, 'handler', '')
        else:
            en, _ = self.node(f'entry:{name}', type='entry', key=name, title=f'/shm/v1/public/{name}',
                              subtitle='публичный HTTP-вход', meta={'template': name})
            self.edge(en, tn, 'handler', '')

    # --- шаблоны
    def template(self, name, email=False, subject=None, level=0):
        info = self.info(name)
        meta = {'missing': info is None}
        st = self.settings(name)
        if st:
            meta['settings'] = st
            if st.get('allow_public'):
                meta['public'] = True
        if info:
            for k in ('http', 'guards', 'guards_tech', 'actions'):
                if info.get(k):
                    meta[k] = info[k]
            if info['stops']:
                meta['stops'] = [x['when'] for x in info['stops']]
        nid, created = self.node('tpl:' + name, type='template', key=name, title=name,
                                 subtitle=(info or {}).get('description', ''), meta=meta)
        if email and ('mail:' + name) not in self.nodes:
            mid, _ = self.node('mail:' + name, type='email', key=name, title=subject or name,
                               content=(info or {}).get('email_text', ''), meta={'subject': subject or ''})
            self.edge(nid, mid, 'sends', 'письмо')
        if created and info and level < self.depth:
            self.expand(nid, name, info, level)
        return nid

    def bot_command(self, bot, cmd, level):
        uid = f'bot:{bot}:{cmd}'
        src = (self.lib.get(bot) or {}).get('content')
        section = tplparse.bot_case(src, cmd) if src else None
        nid, created = self.node(uid, type='bot_cmd', key=bot, title=cmd, subtitle=f'команда бота {bot}',
                                 meta={'bot': bot, 'cmd': cmd, 'missing': section is None})
        if created and section is not None and level < self.depth:
            info = tplparse.analyze(section)
            if info['actions']:
                self.nodes[uid]['meta']['actions'] = info['actions']
            self.expand(nid, f'{bot}:{cmd}', info, level, in_bot=True)
        return nid

    def expand(self, nid, name, info, level, in_bot=False):
        for i, m in enumerate(info['tg']):
            kind, label = recipient(None if in_bot else m['profile'])
            mid, _ = self.node(f'tg:{name}:{i}', type='tg_message', key=name, title=label,
                               subtitle=m['method'] + (f' · {m["media"]}' if m['media'] else ''), content=m['text'],
                               meta={'buttons': m['buttons'], 'when': m['when'], 'when_tech': m['when_tech'],
                                     'profile': m['profile'], 'recipient': kind})
            self.edge(nid, mid, 'sends', '', {'when': m['when']})
        for i, m in enumerate(info['push']):
            mid, _ = self.node(f'push:{name}:{i}', type='push', key=name, title=m['title'] or 'Push',
                               subtitle='Happ push' + (f' · {m["push_type"]}' if m['push_type'] else ''),
                               content=m['text'],
                               meta={'link': m['link'], 'when': m['when'], 'when_tech': m['when_tech']})
            self.edge(nid, mid, 'sends', '', {'when': m['when']})
        for b in info['bot_calls']:
            bn = self.bot_command(b['bot'], b['cmd'], level + 1)
            self.edge(nid, bn, 'calls', 'команда бота', {'when': b['when'], 'when_tech': b['when_tech']})
        for j in info['spool']:
            j = dict(j)
            j['when'] = [w for w in j['when'] if w not in info['guards']]
            is_mail = j['transport'] == 'mail'
            tn = self.template(j['template'], email=is_mail,
                               subject=self.settings(j['template']).get('subject'), level=level + 1)
            if j.get('custom_event'):
                ce = j['custom_event']
                lbl = 'custom event' + (f' {ce}' if isinstance(ce, str) else '')
            elif j['period']:
                lbl = 'каждые ' + tplparse.human_seconds(j['period'])
            elif j['delay']:
                lbl = 'через ' + tplparse.human_seconds(j['delay'])
            elif j.get('delay_expr'):
                lbl = 'через ' + j['delay_expr'] + ' сек'
            else:
                lbl = 'сразу'
            self.edge(nid, tn, 'periodic' if j['period'] else 'delayed', lbl, j)
        for c in info['calls']:
            tn = self.template(c, level=level + 1)
            self.edge(nid, tn, 'calls', 'вызывает')

    def mark_unused_events(self):
        """Вариант сообщения «при событии X», а событие X к этому шаблону не привязано — пометить."""
        handlers = {}
        for e in self.edges.values():
            if e['kind'] == 'handler' and e['s'].startswith('event:'):
                handlers.setdefault(e['t'], set()).add(e['s'].rsplit(':', 1)[1].lower())
        for e in self.edges.values():
            if e['kind'] != 'sends' or e['s'] not in handlers:
                continue
            nd = self.nodes.get(e['t'])
            if not nd:
                continue
            want = [re.match(r'событие (\S+)', w).group(1) for w in nd['meta'].get('when', [])
                    if re.match(r'событие (?!не )(\S+)', w)]
            missing = [w for w in want if w.lower() not in handlers[e['s']]]
            if want and len(missing) == len(want):
                nd['meta']['unused_event'] = ', '.join(missing)
        if self.prune:  # схема одного события: варианты для чужих событий не нужны
            drop = {u for u, n in self.nodes.items() if n['meta'].get('unused_event')}
            for u in drop:
                del self.nodes[u]
            self.edges = {k: e for k, e in self.edges.items() if e['s'] not in drop and e['t'] not in drop}

    # --- запись в БД
    def write(self, con, tid):
        self.mark_unused_events()
        """Создаёт или обновляет узлы/связи топологии. Ручные узлы и связи не трогает."""
        stats = {'added': 0, 'updated': 0, 'stale': 0}
        old = {r['uid']: row(r) for r in con.execute('SELECT * FROM nodes WHERE topology_id=? AND uid IS NOT NULL', (tid,))}
        ids = {}
        for uid, f in self.nodes.items():
            meta = dict(f['meta'])
            meta['auto_hash'] = _hash(f['title'], f['content'], meta.get('buttons'), meta.get('subject'))
            o = old.get(uid)
            if not o:
                cur = con.execute('INSERT INTO nodes (topology_id, uid, auto, type, key, title, subtitle, content, meta) '
                                  'VALUES (?,?,1,?,?,?,?,?,?)',
                                  (tid, uid, f['type'], f['key'], f['title'], f['subtitle'], f['content'], _meta(meta)))
                ids[uid] = cur.lastrowid
                stats['added'] += 1
                continue
            ids[uid] = o['id']
            vals = {k: f[k] for k in USER_FIELDS}
            if o.get('edited'):
                # правки пользователя сохраняем; помечаем, если шаблон с тех пор изменился
                vals = {k: o[k] for k in USER_FIELDS}
                for k in USER_META:
                    if k in (o['meta'] or {}):
                        meta[k] = o['meta'][k]
                if (o['meta'] or {}).get('auto_hash') != meta['auto_hash']:
                    meta['src_changed'] = True
                    meta['src_new'] = {'title': f['title'], 'content': f['content'], 'buttons': f['meta'].get('buttons')}
                elif (o['meta'] or {}).get('src_changed'):  # флаг держится, пока пользователь не решит
                    meta['src_changed'] = True
                    meta['src_new'] = o['meta'].get('src_new')
            con.execute('UPDATE nodes SET type=?, key=?, title=?, subtitle=?, content=?, meta=? WHERE id=?',
                        (f['type'], f['key'], vals['title'], vals['subtitle'], vals['content'], _meta(meta), o['id']))
            stats['updated'] += 1
        for uid, o in old.items():
            if uid not in self.nodes and o.get('auto'):
                m = o['meta'] or {}
                if not m.get('stale'):
                    m['stale'] = True
                    con.execute('UPDATE nodes SET meta=? WHERE id=?', (_meta(m), o['id']))
                    stats['stale'] += 1
        old_e = {r['uid']: row(r) for r in con.execute('SELECT * FROM edges WHERE topology_id=? AND uid IS NOT NULL', (tid,))}
        for uid, e in self.edges.items():
            s, t = ids.get(e['s']), ids.get(e['t'])
            if not s or not t:
                continue
            o = old_e.get(uid)
            if o:
                con.execute('UPDATE edges SET source_id=?, target_id=?, kind=?, label=?, meta=? WHERE id=?',
                            (s, t, e['kind'], e['label'], _meta(e['meta']), o['id']))
            else:
                con.execute('INSERT INTO edges (topology_id, uid, auto, source_id, target_id, kind, label, meta) '
                            'VALUES (?,?,1,?,?,?,?,?)', (tid, uid, s, t, e['kind'], e['label'], _meta(e['meta'])))
        for uid, o in old_e.items():
            if uid not in self.edges and o.get('auto'):
                m = o['meta'] or {}
                m['stale'] = True
                con.execute('UPDATE edges SET meta=? WHERE id=?', (_meta(m), o['id']))
        touch(con, tid)
        return stats


def _hash(*parts):
    import hashlib
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:16]


def extract_events(data):
    if isinstance(data, dict):
        for k in ('data', 'items', 'events', 'result'):
            if isinstance(data.get(k), list):
                return data[k]
        return [data]
    return data if isinstance(data, list) else []


def collect(payload):
    """Собирает шаблоны/настройки/события из файлов, SQL-дампов и JSON."""
    events = extract_events(payload.get('events'))
    templates = dict(payload.get('templates') or {})
    settings = dict(payload.get('settings') or {})
    sql_info = None
    for sql in payload.get('sql') or []:
        # SQL-дамп дополняет файлы; при совпадении имён приоритет у дампа (он свежее выгрузки из SHM)
        t, st, ev = sqldump.extract(sql)
        templates.update(t)
        settings.update(st)
        if ev and not events:
            events = ev
        sql_info = {'templates': len(t), 'events': len(ev)}
    return events, templates, settings, sql_info


def save_library(con, templates, settings):
    for tname, content in templates.items():
        st = settings.get(tname)
        con.execute(
            "INSERT INTO templates (name, content, settings, updated_at) VALUES (?,?,?,datetime('now')) "
            "ON CONFLICT(name) DO UPDATE SET content=excluded.content, settings=excluded.settings, updated_at=excluded.updated_at",
            (tname, content, json.dumps(st or {}, ensure_ascii=False)))
    for tname, st in settings.items():
        if tname not in templates:
            con.execute("UPDATE templates SET settings=? WHERE name=?", (json.dumps(st, ensure_ascii=False), tname))


def load_library(con):
    return {r['name']: row(r) for r in con.execute('SELECT name, content, settings AS meta FROM templates')}


def do_preview(payload):
    events, templates, settings, sql_info = collect(payload)
    with LOCK, db() as con:
        lib = load_library(con)
    for n, c in templates.items():
        lib[n] = {'content': c, 'meta': settings.get(n) or (lib.get(n) or {}).get('meta') or {}}
    for n, st in settings.items():
        lib.setdefault(n, {'content': None, 'meta': {}})['meta'] = st
    public = []
    for n, t in sorted(lib.items()):
        if (t.get('meta') or {}).get('allow_public') and t.get('content') is not None:
            info = tplparse.analyze(t['content'])
            public.append({'name': n, 'actions': tplparse.has_actions(info),
                           'entries': (info['entries'] or {}).get('values', [])})
    return {'events': len(events), 'templates': len(templates), 'sql': sql_info, 'public': public}


def do_import(payload):
    events, templates, settings, sql_info = collect(payload)
    name = (payload.get('name') or 'Импорт').strip()
    split = bool(payload.get('split'))
    public = payload.get('public') or []
    target = payload.get('target')          # id топологии для обновления (без split) или None
    update = payload.get('update', True)    # split: обновлять схемы с тем же источником
    depth = int(payload.get('depth') or 6)
    result = []
    with LOCK, db() as con:
        save_library(con, templates, settings)
        lib = load_library(con)
        if split:
            groups = {}
            for ev in events:
                groups.setdefault(ev.get('name') or '?', []).append(ev)
            plan = [(f'event:{g}', g + (f' — {EVENT_NAMES[g]}' if g in EVENT_NAMES else ''), groups[g], [])
                    for g in sorted(groups)]
            plan += [(f'public:{p}', f'🌐 {p}', [], [p]) for p in public]
        else:
            plan = [('import', name, sorted(events, key=lambda e: (e.get('name') or '', e.get('id') or 0)), public)]
        for source, title, evs, pubs in plan:
            if not evs and not pubs:
                continue
            tid = None
            if not split and target:
                tid = int(target)
                con.execute('UPDATE topologies SET source=COALESCE(source, ?) WHERE id=?', (source, tid))
            elif split and update:
                r = con.execute('SELECT id FROM topologies WHERE source=? ORDER BY id DESC LIMIT 1', (source,)).fetchone()
                tid = r and r['id']
            is_new = not tid
            if is_new:
                tid = con.execute('INSERT INTO topologies (name, description, source) VALUES (?,?,?)',
                                  (title, f'Событий: {len(evs)}, публичных шаблонов: {len(pubs)}', source)).lastrowid
            b = Builder(lib, depth, prune=split)
            for ev in evs:
                b.event(ev)
            for p in pubs:
                b.public(p)
            st = b.write(con, tid)
            real = con.execute('SELECT name FROM topologies WHERE id=?', (tid,)).fetchone()
            result.append({'id': tid, 'name': real['name'] if real else title, 'new': is_new, **st})
    return {'topologies': result, 'templates': len(templates), 'sql': sql_info}


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

    def session(self):
        from http.cookies import SimpleCookie
        c = SimpleCookie(self.headers.get('Cookie') or '')
        return AUTH.verify(c[Auth.COOKIE].value) if Auth.COOKIE in c else None

    def set_cookie(self, value, max_age):
        secure = '; Secure' if (self.headers.get('X-Forwarded-Proto') == 'https') else ''
        return f'{Auth.COOKIE}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}'

    def redirect(self, to):
        self.send_response(302)
        self.send_header('Location', to)
        self.send_header('Content-Length', '0')
        self.end_headers()

    PUBLIC_STATIC = ('/login.html', '/style.css', '/login.js')

    def route(self, method):
        path = unquote(urlparse(self.path).path)
        try:
            if path == '/api/login' and method == 'POST':
                return self.login()
            if path == '/api/logout':
                self.send_response(200)
                self.send_header('Set-Cookie', self.set_cookie('', 0))
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', '2')
                self.end_headers()
                return self.wfile.write(b'{}')
            me = self.session()
            if path.startswith('/api/'):
                if not me:
                    return self.send_json({'error': 'нужен вход'}, 401)
                if path == '/api/me':
                    return self.send_json(me)
                # просмотр: читать и двигать узлы (сохранять позиции) можно, остальное — только админу
                moving = method == 'POST' and re.fullmatch(r'/api/topologies/\d+/positions', path)
                if method != 'GET' and me['role'] != 'admin' and not moving:
                    return self.send_json({'error': 'только просмотр'}, 403)
                return self.api(method, path[5:].strip('/').split('/'))
            if method == 'GET':
                if path in self.PUBLIC_STATIC:
                    return self.static(path)
                if not me:
                    return self.redirect('/login.html')
                return self.static(path)
            self.send_json({'error': 'not found'}, 404)
        except Exception as e:  # noqa
            import traceback
            traceback.print_exc()
            self.send_json({'error': str(e)}, 500)

    def login(self):
        ip = self.headers.get('X-Forwarded-For', self.client_address[0]).split(',')[0].strip()
        b = self.body()
        role = AUTH.check((b.get('login') or '').strip(), b.get('password') or '')
        if not role:
            # против перебора: пауза растёт с числом ошибок с этого IP
            n = AUTH.fails.get(ip, 0) + 1
            AUTH.fails[ip] = n
            time.sleep(min(5, 0.5 * n))
            return self.send_json({'error': 'Неверный логин или пароль'}, 401)
        AUTH.fails.pop(ip, None)
        body = json.dumps({'role': role}).encode()
        self.send_response(200)
        self.send_header('Set-Cookie', self.set_cookie(AUTH.sign(b['login'].strip(), role), Auth.TTL))
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
            if res == 'nodes' and n == 3 and p[2] in ('accept', 'keep') and m == 'POST':
                # ответ на «шаблон изменился»: принять новый текст из шаблона или оставить свой
                nd = row(con.execute('SELECT * FROM nodes WHERE id=?', (int(p[1]),)).fetchone())
                meta = nd['meta'] or {}
                new = meta.pop('src_new', None) or {}
                meta.pop('src_changed', None)
                if p[2] == 'accept':
                    if 'buttons' in new and new['buttons'] is not None:
                        meta['buttons'] = new['buttons']
                    con.execute('UPDATE nodes SET title=?, content=?, meta=?, edited=0 WHERE id=?',
                                (new.get('title', nd['title']), new.get('content', nd['content']), _meta(meta), nd['id']))
                else:
                    con.execute('UPDATE nodes SET meta=? WHERE id=?', (_meta(meta), nd['id']))
                return self.send_json(row(con.execute('SELECT * FROM nodes WHERE id=?', (nd['id'],)).fetchone()))
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
                    if table == 'nodes' and any(k in b for k in ('title', 'subtitle', 'content', 'meta', 'type')):
                        con.execute('UPDATE nodes SET edited=1 WHERE id=?', (iid,))
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
            if n == 2 and p[1] == 'preview':
                return self.send_json(do_preview(self.body()))
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
    global DB_PATH, AUTH
    load_env(os.environ.get('SHM_TOPO_ENV') or os.path.join(BASE, '.env'))
    DB_PATH = os.environ.get('SHM_TOPO_DB') or DB_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=int(os.environ.get('SHM_TOPO_PORT') or 8765))
    ap.add_argument('--host', default=os.environ.get('SHM_TOPO_HOST') or '127.0.0.1')
    ap.add_argument('--db', default=DB_PATH)
    a = ap.parse_args()
    AUTH = Auth()
    if not AUTH.users:
        raise SystemExit('Не задан ADMIN_PASS (и/или USER_LOGIN + USER_PASS) — заполните .env, см. .env.example')
    DB_PATH = os.path.abspath(a.db)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    init_db()
    roles = ', '.join(f'{k} ({v[1]})' for k, v in AUTH.users.items())
    print(f'SHM Topology: http://{a.host}:{a.port}  (db: {DB_PATH}; пользователи: {roles})')
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
