"""Эвристический разбор SHM-шаблонов (Template Toolkit).

Из исходника шаблона достаём:
  - spool-задачи (spool.add): какой шаблон, через сколько / с каким периодом;
  - Telegram-отправки (telegram.send): метод, текст, кнопки;
  - вызовы других шаблонов (tpl.id('x').parse);
  - HTTP-запросы (http.get/post/...);
  - первый комментарий шаблона как описание.
"""
import html
import os
import re

OPENERS = ('IF', 'UNLESS', 'FOR', 'FOREACH', 'WHILE', 'BLOCK', 'SWITCH', 'WRAPPER', 'FILTER', 'MACRO', 'TRY')
DIRECTIVE_RE = re.compile(r'\{\{-?(.*?)-?\}\}', re.S)
TG_SEND_RE = re.compile(r'telegram(?:\s*\.\s*profile\s*\([^)]*\))?\s*\.\s*send\s*\(')
TG_METHODS = ('sendMessage', 'sendPhoto', 'sendDocument', 'sendVideo', 'sendAnimation', 'sendAudio',
              'sendVoice', 'sendMediaGroup', 'sendSticker', 'sendLocation', 'sendPoll', 'sendDice',
              'editMessageText', 'editMessageCaption', 'editMessageReplyMarkup', 'deleteMessage',
              'answerCallbackQuery', 'sendInvoice', 'pinChatMessage', 'copyMessage', 'forwardMessage')


def strip_comments(src):
    src = re.sub(r'\{\{-?#.*?\}\}', '', src, flags=re.S)
    src = re.sub(r'\{#.*?\}\}', '', src, flags=re.S)
    return src


def first_comment(src):
    m = re.search(r'\{\{-?#\s*(.*?)\s*-?\}\}', src, re.S)
    if m and m.start() < 200:
        return m.group(1).strip()[:300]
    return ''


def _match_bracket(s, pos, open_ch, close_ch):
    """pos указывает на open_ch; возвращает индекс парной закрывающей скобки."""
    depth, i, q = 0, pos, None
    while i < len(s):
        c = s[i]
        if q:
            if c == '\\':
                i += 2
                continue
            if c == q:
                q = None
        elif c in '"\'':
            q = c
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(s) - 1


def _stmts(directive_body):
    return [s.strip() for s in directive_body.split(';') if s.strip()]


def _is_opener(stmt):
    parts = stmt.split()
    if parts and parts[0] in OPENERS:
        return True
    return bool(re.match(r'^\w+\s*=\s*BLOCK\b', stmt))


def find_blocks(src):
    """Находит `{{ VAR = BLOCK }} ... {{ END }}` с учётом вложенности. -> [(name, start, end, body)]"""
    out = []
    for m in re.finditer(r'\{\{-?\s*(\w+)\s*=\s*BLOCK\s*-?\}\}', src):
        depth = 1
        for d in DIRECTIVE_RE.finditer(src, m.end()):
            for st in _stmts(d.group(1)):
                if _is_opener(st):
                    depth += 1
                elif st == 'END':
                    depth -= 1
            if depth <= 0:
                out.append((m.group(1), m.start(), d.end(), src[m.end():d.start()]))
                break
    return out


def humanize_tt(text):
    """Делает текст сообщения читаемым: tg-emoji -> эмодзи, теги убраны, управляющие директивы свернуты."""
    text = re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', r'\1', text, flags=re.S)

    def repl(m):
        body = m.group(1).strip()
        head = body.split(None, 1)[0] if body else ''
        rest = body[len(head):].strip()
        if head == 'IF':
            return '[если: ' + human_cond(rest) + ']'
        if head == 'UNLESS':
            return '[если: ' + human_cond(rest, True) + ']'
        if head == 'ELSIF':
            return '[иначе если: ' + human_cond(rest) + ']'
        if head in ('FOR', 'FOREACH', 'WHILE'):
            return '[цикл: ' + re.sub(r'\s+', ' ', rest)[:80] + ']'
        if head == 'ELSE':
            return '[иначе]'
        if head == 'END':
            return '[конец]'
        if head in ('LAST', 'NEXT'):
            return ''
        if re.match(r'^\w+\s*=', body) or head in ('USE', 'SET', 'CALL', 'PROCESS', 'INCLUDE', 'STOP', 'RETURN'):
            return ''
        return '{{ ' + re.sub(r'\s+', ' ', body) + ' }}'

    text = DIRECTIVE_RE.sub(repl, text)
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'</?(b|i|u|s|code|pre|strong|em|a|blockquote|span|tg-spoiler)\b[^>]*>', '', text)
    text = html.unescape(text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def html_to_text(src):
    src = re.sub(r'<(style|script|head)\b.*?</\1>', '', src, flags=re.S | re.I)
    src = re.sub(r'<!--.*?-->', '', src, flags=re.S)
    src = re.sub(r'<(br|/tr|/p|/div|/h\d|/li)\b[^>]*>', '\n', src, flags=re.I)
    src = re.sub(r'<[^>]+>', ' ', src)
    src = humanize_tt(src)
    lines = [re.sub(r'\s+', ' ', l).strip() for l in src.split('\n')]
    out, blank = [], False
    for l in lines:
        if not l:
            if not blank and out:
                out.append('')
            blank = True
        else:
            out.append(l)
            blank = False
    return '\n'.join(out).strip()


def _value_at(s, pos):
    """Значение выражения после `key =` начиная с pos: строковый литерал или выражение до конца строки/запятой."""
    s2 = s[pos:].lstrip()
    if s2[:1] in '"\'':
        q = s2[0]
        end = s2.find(q, 1)
        while end > 0 and s2[end - 1] == '\\':
            end = s2.find(q, end + 1)
        lit = s2[1:end] if end > 0 else s2[1:]
        rest = s2[end + 1:end + 200] if end > 0 else ''
        if re.match(r'\s*_', rest):  # конкатенация
            return _expr(s2), False
        return lit, True
    return _expr(s2), False


def _expr(s):
    """Выражение до конца строки или до запятой/скобки верхнего уровня."""
    depth, q = 0, None
    for i, c in enumerate(s):
        if q:
            if c == q:
                q = None
        elif c in '"\'':
            q = c
        elif c in '([':
            depth += 1
        elif c in ')]':
            if depth == 0:
                return s[:i].strip()
            depth -= 1
        elif c == '\n' or (depth == 0 and c in ',}'):
            return s[:i].strip()
    return s.strip()


def parse_keyboard(region):
    m = re.search(r'\b(inline_keyboard|keyboard)\s*=\s*\[', region)
    if not m:
        return []
    start = m.end() - 1
    end = _match_bracket(region, start, '[', ']')
    kb = region[start + 1:end]
    rows, i = [], 0
    while True:
        j = kb.find('[', i)
        if j < 0:
            break
        k = _match_bracket(kb, j, '[', ']')
        row_src = kb[j + 1:k]
        row = []
        pos = 0
        while True:
            o = row_src.find('{', pos)
            if o < 0:
                break
            c = _match_bracket(row_src, o, '{', '}')
            obj = row_src[o + 1:c]
            btn = _parse_button(obj)
            if btn:
                row.append(btn)
            pos = c + 1
        if row:
            rows.append(row)
        i = k + 1
    if not rows:  # клавиатура собирается динамически — берём что нашлось
        btns = [b for b in (_parse_button(x) for x in re.split(r'\}\s*,?\s*\{', kb)) if b]
        rows = [[b] for b in btns]
    return rows


def _parse_button(obj):
    tm = re.search(r'\btext\s*=\s*', obj)
    if not tm:
        return None
    text, _ = _value_at(obj, tm.end())
    btn = {'text': text}
    for key in ('callback_data', 'url', 'web_app', 'switch_inline_query', 'login_url', 'copy_text', 'pay'):
        km = re.search(r'\b' + key + r'\s*=\s*', obj)
        if km:
            if key == 'web_app':
                um = re.search(r'\burl\s*=\s*', obj[km.end():])
                val, _ = _value_at(obj[km.end():], um.end()) if um else ('', False)
            else:
                val, _ = _value_at(obj, km.end())
            btn['type'] = key
            btn['action'] = val
            break
    return btn


def parse_tg_sends(src):
    blocks = find_blocks(src)
    out = []
    for m in TG_SEND_RE.finditer(src):
        p = m.end() - 1
        end = _match_bracket(src, p, '(', ')')
        region = src[p + 1:end]
        mm = re.search(r'\b(' + '|'.join(TG_METHODS) + r')\b', region)
        method = mm.group(1) if mm else 'sendMessage'
        text = ''
        tm = re.search(r'\b(text|caption)\s*=\s*', region)
        if tm:
            val, is_lit = _value_at(region, tm.end())
            if is_lit:
                text = val
            else:
                var = re.match(r'^(\w+)$', val)
                if var:
                    cands = [b for b in blocks if b[0] == var.group(1) and b[2] <= m.start()]
                    if not cands:
                        cands = [b for b in blocks if b[0] == var.group(1)]
                    text = cands[-1][3] if cands else '{{ ' + val + ' }}'
                else:
                    text = '{{ ' + val + ' }}'
        media = ''
        for key in ('photo', 'document', 'video', 'animation'):
            pm = re.search(r'\b' + key + r'\s*=\s*', region)
            if pm:
                media = key + ': ' + _value_at(region, pm.end())[0]
                break
        out.append({
            'method': method,
            'text': humanize_tt(text),
            'media': media,
            'buttons': parse_keyboard(region),
            'pos': m.start(),
        })
    return out


ASSIGN_RE = re.compile(
    r"""(?<![\w.$])["']?([A-Za-z_]\w*)["']?\s*(?<![=!<>])=(?![=>~])\s*"""
    r"""('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|[{\[]|[^\n,;}#{\[]+)""")
HASH_KEYS = ('template_id', 'delayed', 'period')


def _parse_val(raw):
    raw = raw.strip()
    if raw[:1] in '"\'':
        end = raw.find(raw[0], 1)
        return ('str', raw[1:end if end > 0 else None])
    if re.fullmatch(r'\d+', raw):
        return ('num', int(raw))
    if re.fullmatch(r'[A-Za-z_]\w*', raw):
        return ('ref', raw)
    return ('expr', raw)


class Vars:
    """Все присваивания `{{ имя = значение }}` файла с позициями — для подстановки переменных."""

    def __init__(self, src):
        self.items = [(m.start(), m.group(1), _parse_val(m.group(2))) for m in ASSIGN_RE.finditer(src)]

    def last(self, name, pos):
        """Последнее присваивание до pos; если нет (BLOCK ниже по файлу) — первое в файле."""
        before = [a for a in self.items if a[1] == name and a[0] < pos]
        if before:
            return before[-1]
        after = [a for a in self.items if a[1] == name]
        return after[0] if after else None

    def resolve(self, val, pos, depth=0, name=None):
        if not val or depth > 8:
            return None
        kind, v = val
        if kind == 'ref':
            a = self.last(v, pos)
            return self.resolve(a[2], a[0], depth + 1, a[1]) if a else None
        if kind == 'expr' and name and re.search(r'\b' + name + r'\b', v):
            # `DELAYED = (DELAYED + 1)` — берём предыдущее значение, если оно есть
            a = self.last(name, pos)
            if a and a[0] < pos:
                return self.resolve(a[2], a[0], depth + 1, name) or val
        return val


def parse_spool(src):
    """На каждый spool.add определяем шаблон, задержку и период, подставляя переменные.

    Параметры берутся из аргументов самого вызова `spool.add( template_id = … )`,
    иначе из хеша, собранного перед вызовом (`content = { … }`), иначе из TEMPLATE/DELAYED/PERIOD.
    """
    vs = Vars(src)
    adds = []
    for m in re.finditer(r'(spool\s*\.\s*add|make_custom_event)\s*\(', src):
        adds.append((m.start(), _match_bracket(src, m.end() - 1, '(', ')'), m.group(1).startswith('make')))
    in_call = lambda p: any(a < p < e for a, e, _ in adds)
    events = [(a[0], 'set', a) for a in vs.items if not in_call(a[0])] + [(a, 'add', (e, ce)) for a, e, ce in adds]
    events.sort(key=lambda e: e[0])
    pending, jobs = {}, []
    for pos, kind, a in events:
        if kind == 'set':
            if a[1] in HASH_KEYS:
                pending[a[1]] = (a[0], a[2])
            continue
        a, custom = a
        args = {x[1]: (x[0], x[2]) for x in vs.items if pos < x[0] < a and x[1] in HASH_KEYS + ('name',)}
        if custom and 'template_id' not in args:
            continue

        def get(key, var):
            for src_ in (args, pending):
                if key in src_:
                    r = vs.resolve(src_[key][1], src_[key][0])
                    if r:
                        return r
            return vs.resolve(('ref', var), pos)

        tpl = get('template_id', 'TEMPLATE')
        if tpl and tpl[0] == 'str' and tpl[1]:
            delay, period = get('delayed', 'DELAYED'), get('period', 'PERIOD')
            title = vs.resolve(('ref', 'TITLE'), pos)
            tr = [x for x in vs.items if x[1] == 'transport' and x[0] < a and x[2][0] == 'str']
            tpl_pos = (vs.last('TEMPLATE', pos) or (-1,))[0]
            tr = tr[-1] if tr and (tr[-1][0] > tpl_pos or tr[-1][0] > pos) else None
            jobs.append({
                'template': tpl[1],
                'delay': delay[1] if delay and delay[0] == 'num' else None,
                'delay_expr': delay[1] if delay and delay[0] == 'expr' else None,
                'period': period[1] if period and period[0] == 'num' else None,
                'title': title[1] if title and title[0] == 'str' else '',
                'transport': tr[2][1] if tr else None,
                'custom_event': (args['name'][1][1] if 'name' in args else True) if custom else None,
                'pos': pos,
            })
        pending = {}
    return jobs


def parse_calls(src):
    vs = Vars(src)
    names = set()
    for m in re.finditer(r"tpl\s*\.\s*id\s*\(\s*(['\"][^'\"]+['\"]|[A-Za-z_]\w*)\s*\)\s*\.\s*(?:parse|data)", src):
        r = vs.resolve(_parse_val(m.group(1)), m.start())
        if r and r[0] == 'str' and r[1]:
            names.add(r[1])
    return sorted(names)


def parse_http(src):
    out = []
    for m in re.finditer(r'\bhttp\s*\.\s*(get|post|put|patch|delete)\s*\(\s*([^,)\n]+)', src):
        out.append(m.group(1).upper() + ' ' + m.group(2).strip()[:120])
    return sorted(set(out))


def analyze(src):
    desc = first_comment(src)
    body = strip_comments(src)
    flow = Flow(body)
    tg = []
    for m in parse_tg_sends(body):
        m['when'], dead = flow.at(m['pos'])
        if not dead:
            tg.append(m)
    spool, seen = [], {}
    for j in parse_spool(body):
        j['when'], dead = flow.at(j.pop('pos'))
        if dead:
            continue
        k = (j['template'], j['delay'], j['period'], j['delay_expr'])
        if k in seen:  # та же задача в другой ветке — объединяем условия
            prev = seen[k]
            if j['when'] != prev['when']:
                prev.setdefault('alt', []).append(j['when'])
            continue
        seen[k] = j
        spool.append(j)
    return {
        'guards': flow.root_guards,
        'stops': [{'when': list(w)} for w, _ in flow.stops if w and not any(isinstance(x, tuple) for x in w)][:12],
        'description': desc,
        'tg': tg,
        'spool': spool,
        'calls': parse_calls(body),
        'http': parse_http(body),
        'mail_send': bool(re.search(r'\bmail\w*\s*\.\s*send\s*\(', body)),
        'email_text': html_to_text(body) if not tg else '',
    }


def human_seconds(sec):
    if sec is None:
        return ''
    sec = int(sec)
    for unit, name in ((86400 * 7, 'нед'), (86400, 'д'), (3600, 'ч'), (60, 'мин')):
        if sec >= unit and sec % unit == 0:
            return f'{sec // unit} {name}'
    if sec >= 3600:
        return f'{sec / 3600:.1f} ч'
    if sec >= 60:
        return f'{sec // 60} мин'
    return f'{sec} сек'


# ---------------------------------------------------------------- условия (IF / ELSIF / ELSE / UNLESS / STOP)

STATUS_RU = {
    'ACTIVE': 'активна', 'BLOCK': 'заблокирована', 'REMOVED': 'удалена', 'NOT PAID': 'не оплачена',
    'INIT': 'создана', 'PROGRESS': 'в обработке', 'ERROR': 'ошибка',
}
_OPS_NEG = {'==': '!=', '!=': '==', 'eq': 'ne', 'ne': 'eq', '>': '<=', '<': '>=', '>=': '<', '<=': '>'}


def _split_top(s, seps):
    """Делит выражение по && / || верхнего уровня (вне скобок и строк)."""
    out, depth, q, cur, i = [], 0, None, '', 0
    while i < len(s):
        c = s[i]
        if q:
            cur += c
            if c == q:
                q = None
            i += 1
            continue
        if c in '"\'':
            q = c
        elif c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
        if depth == 0:
            hit = next((sp for sp in seps if s.startswith(sp, i)), None)
            if hit:
                out.append(cur.strip())
                cur = ''
                i += len(hit)
                continue
        cur += c
        i += 1
    out.append(cur.strip())
    return [x for x in out if x]


def _strip_parens(s):
    s = s.strip()
    while s.startswith('(') and s.endswith(')') and _match_bracket(s, 0, '(', ')') == len(s) - 1:
        s = s[1:-1].strip()
    return s


def human_atom(a, neg=False):
    a = _strip_parens(a)
    while a.startswith('!') or a.upper().startswith('NOT '):
        a = _strip_parens(a[1:] if a.startswith('!') else a[4:])
        neg = not neg
    m = re.fullmatch(r'(\S+?)\s*(==|!=|>=|<=|>|<|\beq\b|\bne\b)\s*(.+)', a)
    if m:
        lhs, op, rhs = m.group(1), m.group(2), m.group(3).strip()
        if neg:
            op = _OPS_NEG.get(op, op)
        eq = op in ('==', 'eq')
        val = rhs.strip('\'"')
        if lhs in ('us.status', 'item.status', 'uss.status') and op in ('==', '!=', 'eq', 'ne'):
            return ('услуга ' + STATUS_RU.get(val, val)) if eq else f'статус услуги не «{STATUS_RU.get(val, val)}»'
        if lhs == 'us.status_before' and op in ('==', '!=', 'eq', 'ne'):
            return ('до этого была ' + STATUS_RU.get(val, val)) if eq else f'до этого статус не «{STATUS_RU.get(val, val)}»'
        tg = re.fullmatch(r'user\.settings\.telegram\.(?:\$\{?\w+\}?|\w+)\.status', lhs)
        if tg and val == 'kicked':
            return 'бот не остановлен' if not eq else 'бот остановлен пользователем'
        if tg and val == 'member':
            return 'пользователь в боте' if eq else 'пользователь не в боте'
        if re.fullmatch(r'(user\.|u\.)?us\.items\.size', lhs):
            if eq and val == '1':
                return 'у пользователя 1 услуга'
            return f'услуг у пользователя {"=" if eq else op} {val}'
        if lhs == 'us.service.category':
            return ('категория ' if eq else 'категория не ') + val
        if lhs == 'pay.money':
            return f'сумма платежа {op} {val}'
        if lhs == 'user.id' and val.isdigit():
            return ('пользователь #' if eq else 'пользователь не #') + val
        if rhs == 'NULL' or rhs == "''":
            return ('не задано ' if eq else 'задано ') + lhs
        return f'{lhs} {op} {rhs}'
    simple = {
        'user.has_payments': ('есть платежи', 'нет платежей'),
        'user.us.has_services_block': ('есть услуги в блоке', 'нет услуг в блоке'),
        'user.us.has_services_active': ('есть активные услуги', 'нет активных услуг'),
        'user.us.has_services_unpaid': ('есть неоплаченные услуги', 'нет неоплаченных услуг'),
        'user.us.has_services': ('есть услуги', 'нет услуг'),
        'user.email': ('указан email', 'нет email'),
        'user.partner_id': ('пришёл по реф. ссылке', 'без реферера'),
        'user.discount': ('есть скидка', 'нет скидки'),
        'callback_query.id': ('нажата inline-кнопка', 'не из inline-кнопки'),
        'forecast.items.size': ('есть прогноз оплаты', 'нет прогноза оплаты'),
    }
    key = re.sub(r'^u\.', 'user.', a)
    if key in simple:
        return simple[key][1 if neg else 0]
    if re.fullmatch(r'user\.settings\.telegram(\.\w+)?', a):
        return 'нет Telegram' if neg else 'есть Telegram'
    m = re.fullmatch(r'us\.service\.category\.(?:search|match)\(\s*[\'"](.+?)[\'"]\s*\)', a)
    if m:
        return ('категория не ~ ' if neg else 'категория ~ ') + m.group(1)
    if re.fullmatch(r'misc\.is_email\(\s*user\.login\s*\)', a):
        return 'логин не email' if neg else 'логин — email'
    m = re.fullmatch(r'(us|user)\.settings\.(\w+)', a)
    if m:
        who = 'у услуги' if m.group(1) == 'us' else 'у пользователя'
        return f'{who} {"нет флага" if neg else "есть флаг"} {m.group(2)}'
    if neg and re.fullmatch(r'[\w.]+', a):
        return 'нет ' + a
    return ('НЕ ' if neg else '') + re.sub(r'\s+', ' ', a)


def human_cond(c, neg=False):
    c = _strip_parens(re.sub(r'\s+', ' ', c))
    ors = _split_top(c, ('||', ' or ', ' OR '))
    if len(ors) > 1 and not neg:
        # (A && B) || (A && C)  ->  A, B или C
        parts = [[_strip_parens(x) for x in _split_top(_strip_parens(o), ('&&', ' and ', ' AND '))] for o in ors]
        common = [x for x in parts[0] if all(x in p for p in parts[1:])]
        if common:
            rest = [[x for x in p if x not in common] for p in parts]
            if all(len(r) == 1 for r in rest):
                hs = [human_atom(r[0]) for r in rest]
                pre = os.path.commonprefix(hs)
                pre = pre[:pre.rfind(' ') + 1] if ' ' in pre else ''
                tail = ' или '.join([hs[0]] + [h[len(pre):] for h in hs[1:]]) if pre else ' или '.join(hs)
                return ', '.join(human_atom(x) for x in common) + ', ' + tail
    if len(ors) > 1:
        if neg:
            return ' и '.join(human_cond(o, True) for o in ors)
        return ' ИЛИ '.join('(' + human_cond(o) + ')' if len(_split_top(o, ('&&', ' and ', ' AND '))) > 1 else human_cond(o) for o in ors)
    ands = _split_top(c, ('&&', ' and ', ' AND '))
    if len(ands) > 1:
        if neg:
            return 'НЕ (' + ', '.join(human_atom(x) for x in ands) + ')'
        return ', '.join(human_atom(x) for x in ands)
    return human_atom(c, neg)


# технические проверки, которые не несут смысла для схемы
NOISE_RE = re.compile(r'(НЕ )?(content\.settings\.user_id|ret|req|spool_add|result)')


class Flow:
    """Для любой позиции в шаблоне знает, при каких условиях туда доходит выполнение."""

    OPEN = ('IF', 'UNLESS', 'FOR', 'FOREACH', 'WHILE', 'BLOCK', 'SWITCH', 'WRAPPER', 'FILTER', 'TRY', 'PERL', 'RAWPERL')

    def __init__(self, src):
        self.snaps = [(0, ())]   # (pos, tuple условий)
        self.blocks = {}         # name -> (start, end)
        self.process = {}        # name -> [pos]
        self.stops = []          # [(условия, где)]
        stack = [{'kind': 'ROOT', 'cond': None, 'prior': [], 'guards': []}]
        for d in DIRECTIVE_RE.finditer(src):
            body = d.group(1).strip()
            if body.startswith('#'):
                continue
            for st in _stmts(body):
                w = st.split(None, 1)
                head, rest = w[0], (w[1] if len(w) > 1 else '')
                top = stack[-1]
                if head in ('IF', 'UNLESS'):
                    stack.append({'kind': head, 'cond': human_cond(rest, head == 'UNLESS'), 'prior': [rest], 'guards': []})
                elif head == 'ELSIF':
                    top['cond'] = human_cond(rest)
                    top['prior'].append(rest)
                    top['guards'] = []
                elif head == 'ELSE' and top['kind'] in ('IF', 'UNLESS'):
                    pr = top['prior']
                    if top['kind'] == 'UNLESS':
                        top['cond'] = human_cond(pr[0])
                    elif len(pr) == 1:
                        top['cond'] = human_cond(pr[0], True)
                    else:
                        top['cond'] = 'иначе (ни одно из условий выше)'
                    top['guards'] = []
                elif head in ('FOR', 'FOREACH'):
                    m = re.match(r'(\w+)\s+(?:IN|=)\s+(.+)', rest)
                    stack.append({'kind': 'FOR', 'cond': f'для каждого {m.group(1)} из {m.group(2).strip()[:60]}' if m else None,
                                  'prior': [], 'guards': []})
                elif head == 'SWITCH':
                    stack.append({'kind': 'SWITCH', 'cond': None, 'subj': rest, 'prior': [], 'guards': []})
                elif head == 'CASE' and top['kind'] == 'SWITCH':
                    top['cond'] = f'{top["subj"]} = {rest}' if rest and rest != 'DEFAULT' else f'{top["subj"]}: остальные случаи'
                elif head == 'BLOCK' and rest:
                    name = rest.split()[0]
                    stack.append({'kind': 'NBLOCK', 'cond': None, 'name': name, 'start': d.start(), 'prior': [], 'guards': []})
                elif head in self.OPEN or re.match(r'^\w+\s*=\s*(BLOCK|PERL|RAWPERL)\b', st):
                    stack.append({'kind': 'OTHER', 'cond': None, 'prior': [], 'guards': []})
                elif head == 'END':
                    if len(stack) > 1:
                        fr = stack.pop()
                        if fr['kind'] == 'NBLOCK':
                            self.blocks[fr['name']] = (fr['start'], d.end())
                elif head in ('PROCESS', 'INCLUDE') and rest:
                    self.process.setdefault(rest.split()[0].strip('\'"'), []).append(d.start())
                elif head in ('STOP', 'RETURN'):
                    m = re.match(r'(IF|UNLESS)\s+(.+)', rest)
                    if m:
                        self.stops.append((self._conds(stack) + (human_cond(m.group(2), m.group(1) == 'UNLESS'),), d.start()))
                        top['guards'].append(human_cond(m.group(2), m.group(1) == 'IF'))
                    else:
                        self.stops.append((self._conds(stack), d.start()))
            self.snaps.append((d.end(), self._conds(stack)))
        self.root_guards = list(stack[0]['guards'])

    @staticmethod
    def _conds(stack):
        out = []
        for fr in stack:
            if fr['kind'] == 'NBLOCK':
                out.append(('@block', fr['name']))
                continue
            if fr['cond'] and not NOISE_RE.fullmatch(fr['cond']):
                out.append(fr['cond'])
            out.extend(fr['guards'])
        return tuple(out)

    def at(self, pos, depth=0):
        """-> (список условий на русском, dead: bool)"""
        lo, hi = 0, len(self.snaps) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.snaps[mid][0] <= pos:
                lo = mid
            else:
                hi = mid - 1
        conds = list(self.snaps[lo][1])
        # позиция внутри именованного BLOCK — условия берём с места его PROCESS
        idx = max((i for i, c in enumerate(conds) if isinstance(c, tuple)), default=None)
        if idx is not None:
            name = conds[idx][1]
            inner = [c for c in conds[idx + 1:] if not isinstance(c, tuple)]
            sites = [p for p in self.process.get(name, []) if not (self.blocks.get(name, (0, 0))[0] <= p <= self.blocks.get(name, (0, 0))[1])]
            if not sites or depth > 5:
                return inner, True
            outer, dead = self.at(sites[0], depth + 1)
            return outer + inner, dead
        return [c for c in conds if not isinstance(c, tuple)], False


def when_label(conds, limit=70):
    s = ' · '.join(c for c in conds if not c.startswith('для каждого'))
    return s if len(s) <= limit else s[:limit - 1] + '…'
