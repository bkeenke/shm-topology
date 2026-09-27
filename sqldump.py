"""Разбор mysqldump SHM: таблицы `templates` (id, data, settings) и `events`.

Поддерживает многострочные INSERT ... VALUES (...),(...); и INSERT с явным списком колонок.
"""
import json
import re

_ESC = {'n': '\n', 'r': '\r', 't': '\t', '0': '\0', 'Z': '\x1a', 'b': '\b'}


def _columns(sql):
    """CREATE TABLE `x` (...) -> {table: [колонки]}"""
    out = {}
    for m in re.finditer(r'CREATE TABLE\s+`?(\w+)`?\s*\((.*?)\)\s*(?:ENGINE|;)', sql, re.S | re.I):
        cols = re.findall(r'^\s*`(\w+)`', m.group(2), re.M)
        out[m.group(1)] = cols
    return out


def _parse_values(s, i):
    """Парсит последовательность кортежей начиная с s[i]; возвращает (rows, конец)."""
    rows, n = [], len(s)
    while i < n:
        while i < n and s[i] in ' \t\r\n,':
            i += 1
        if i >= n or s[i] != '(':
            break
        i += 1
        row = []
        while True:
            while s[i] in ' \t\r\n':
                i += 1
            c = s[i]
            if c == "'":
                i += 1
                buf = []
                while True:
                    j = i
                    while s[i] not in "\\'":
                        i += 1
                    buf.append(s[j:i])
                    if s[i] == '\\':
                        buf.append(_ESC.get(s[i + 1], s[i + 1]))
                        i += 2
                    elif s[i + 1:i + 2] == "'":  # '' внутри строки
                        buf.append("'")
                        i += 2
                    else:
                        i += 1
                        break
                row.append(''.join(buf))
            else:
                j = i
                while s[i] not in ',)':
                    i += 1
                tok = s[j:i].strip()
                if tok.upper() == 'NULL':
                    row.append(None)
                else:
                    try:
                        row.append(int(tok))
                    except ValueError:
                        try:
                            row.append(float(tok))
                        except ValueError:
                            row.append(tok)
            while s[i] in ' \t\r\n':
                i += 1
            if s[i] == ',':
                i += 1
                continue
            if s[i] == ')':
                i += 1
                break
        rows.append(row)
    return rows, i


def tables(sql, wanted=('templates', 'events')):
    cols = _columns(sql)
    data = {}
    for m in re.finditer(r'INSERT\s+(?:IGNORE\s+)?INTO\s+`?(\w+)`?\s*(\(([^)]*)\))?\s*VALUES\s*', sql, re.I):
        table = m.group(1)
        if table not in wanted:
            continue
        names = [c.strip(' `') for c in m.group(3).split(',')] if m.group(3) else cols.get(table)
        rows, _ = _parse_values(sql, m.end())
        for r in rows:
            data.setdefault(table, []).append(dict(zip(names, r)) if names else r)
    return data


def _json(v):
    if v is None or isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


def extract(sql):
    """-> (templates{name: text}, settings{name: dict}, events[list])"""
    t = tables(sql)
    templates, settings, events = {}, {}, []
    for r in t.get('templates', []):
        if not isinstance(r, dict):
            r = dict(zip(('id', 'data', 'settings'), r))
        name = r.get('id')
        if not name:
            continue
        templates[name] = (r.get('data') or '').replace('\r\n', '\n')
        st = _json(r.get('settings'))
        if st:
            settings[name] = st
    for r in t.get('events', []):
        if isinstance(r, dict):
            r = dict(r)
            r['settings'] = _json(r.get('settings')) or {}
            events.append(r)
    return templates, settings, events
