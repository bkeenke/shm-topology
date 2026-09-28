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


# SHM включает ANYCASE => 1: ключевые слова TT можно писать строчными (`{{ end }}`, `{{ stop }}`)
TT_KEYWORDS = {'IF', 'ELSIF', 'ELSE', 'END', 'UNLESS', 'FOR', 'FOREACH', 'WHILE', 'BLOCK', 'SWITCH', 'CASE',
               'STOP', 'RETURN', 'PROCESS', 'INCLUDE', 'INSERT', 'WRAPPER', 'FILTER', 'MACRO', 'TRY', 'CATCH',
               'FINAL', 'THROW', 'PERL', 'RAWPERL', 'SET', 'GET', 'CALL', 'DEFAULT', 'USE', 'LAST', 'NEXT',
               'CLEAR', 'META', 'TAGS', 'DEBUG'}
_KW_RE = re.compile(r'\b(if|unless|in|block|perl|rawperl)\b', re.I)


def norm_stmt(st):
    """Ключевые слова — в верхний регистр: `end` -> END, `stop if x` -> STOP IF x, `t = block` -> t = BLOCK."""
    parts = st.split(None, 1)
    if not parts:
        return st
    head = parts[0].upper()
    if head in TT_KEYWORDS:
        rest = parts[1] if len(parts) > 1 else ''
        if head in ('STOP', 'RETURN', 'LAST', 'NEXT', 'CLEAR'):
            rest = re.sub(r'^(if|unless)\b', lambda m: m.group(1).upper(), rest, flags=re.I)
        elif head in ('FOR', 'FOREACH'):
            rest = re.sub(r'^(\w+\s+)in\b', lambda m: m.group(1) + 'IN', rest, flags=re.I)
        return head + (' ' + rest if rest else '')
    m = re.match(r'^(\w+\s*=\s*)(block|perl|rawperl)\b(.*)$', st, re.I | re.S)
    if m:
        return m.group(1) + m.group(2).upper() + m.group(3)
    return st


def _stmts(directive_body):
    out = []
    for st in directive_body.split(';'):
        st = re.sub(r'\s#.*$', '', st.strip(), flags=re.S).strip()  # `END #TEXT` — хвостовой комментарий
        if st:
            out.append(norm_stmt(st))
    return out


def _is_opener(stmt):
    parts = stmt.split()
    if parts and parts[0] in OPENERS:
        return True
    return bool(re.match(r'^\w+\s*=\s*BLOCK\b', stmt))


def find_blocks(src):
    """Находит `{{ VAR = BLOCK }} ... {{ END }}` с учётом вложенности. -> [(name, start, end, body)]"""
    out = []
    for m in re.finditer(r'\{\{-?\s*(\w+)\s*=\s*BLOCK\s*-?\}\}', src, re.I):
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
        body = norm_stmt(m.group(1).strip())
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


def KEY(k):
    """Ключ хеша в любом синтаксисе: `key = `, `key => `, `"key": ` (JSON)."""
    return r'["\']?\b' + k + r'\b["\']?\s*(?:=>|=|:)\s*'


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
    m = re.search(KEY('(?:inline_keyboard|keyboard)') + r'\[', region)
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
    tm = re.search(KEY('text'), obj)
    if not tm:
        return None
    text, lit = _value_at(obj, tm.end())
    btn = {'text': humanize_tt(text if lit else concat_text(text))}
    for key in ('callback_data', 'url', 'web_app', 'switch_inline_query', 'login_url', 'copy_text', 'pay'):
        km = re.search(KEY(key), obj)
        if km:
            if key == 'web_app':
                um = re.search(KEY('url'), obj[km.end():])
                val, _ = _value_at(obj[km.end():], um.end()) if um else ('', False)
            else:
                val, _ = _value_at(obj, km.end())
            btn['type'] = key
            btn['action'] = val
            break
    return btn


NO_CONTENT = ('deleteMessage', 'answerCallbackQuery', 'pinChatMessage', 'editMessageReplyMarkup', 'sendChatAction')
JSON_SEND_RE = re.compile(r'"(' + '|'.join(TG_METHODS) + r')"\s*:\s*\{')
TG_API_RE = re.compile(r'\btg_api\s*\(')


def _send_regions(src):
    """-> [(pos, region, profile_expr|None)] для всех видов отправки в Telegram."""
    out = []
    for m in re.finditer(r'telegram(?:\s*\.\s*profile\s*\(([^)]*)\))?\s*\.\s*send\s*\(', src):
        end = _match_bracket(src, m.end() - 1, '(', ')')
        out.append((m.start(), src[m.end():end], (m.group(1) or '').strip() or None))
    for m in TG_API_RE.finditer(src):
        end = _match_bracket(src, m.end() - 1, '(', ')')
        out.append((m.start(), src[m.end():end], None))
    for m in JSON_SEND_RE.finditer(src):
        end = _match_bracket(src, m.end() - 1, '{', '}')
        out.append((m.start(), m.group(1) + ' ' + src[m.end():end], None))
    return sorted(out, key=lambda x: x[0])


def _resolve_str(vs, expr, pos, default=None):
    if not expr:
        return default
    r = vs.resolve(_parse_val(expr), pos)
    return r[1] if r and r[0] == 'str' else default


def _hash_of_var(src, var, pos):
    """Текст хеша/массива из последнего `var = { … }` (или `[ … ]`) перед pos."""
    found = None
    for m in re.finditer(r'(?<![\w.])' + re.escape(var) + r'\s*=\s*\(?\s*([\[{])', src[:pos]):
        found = m
    if not found:
        return None
    o = found.start(1)
    return src[o:_match_bracket(src, o, src[o], ']' if src[o] == '[' else '}') + 1]


def _directive_at(src, pos, _cache={}):
    key = id(src)
    if _cache.get('key') != key:
        _cache.clear()
        _cache.update(key=key, list=[(d.start(), d.end(), d.group(1)) for d in DIRECTIVE_RE.finditer(src)])
    for a, b, body in _cache['list']:
        if a <= pos < b:
            return a, b, body
    return None


def _dynamic_buttons(src, var, pos, flow, send_conds):
    """Кнопки из `var = [...]` и `var.push([...]) IF …` перед отправкой; у каждой — условия показа."""
    rows = []
    reset = None
    for m in re.finditer(r'(?<![\w.])' + re.escape(var) + r'\s*=\s*\[', src[:pos]):
        reset = m
    if reset:
        lit = src[reset.end() - 1:_match_bracket(src, reset.end() - 1, '[', ']') + 1]
        if lit.strip('[] \n\t'):
            rows += parse_keyboard('inline_keyboard = ' + lit)
    have = {str(c) for c in send_conds}
    for m in re.finditer(r'(?<![\w.])' + re.escape(var) + r'\s*\.\s*push\s*\(', src[:pos]):
        if reset and m.start() < reset.start():
            continue
        o = m.end() - 1
        c = _match_bracket(src, o, '(', ')')
        arg = src[o + 1:c].strip()
        inner = arg[1:].lstrip() if arg.startswith('[') else ''
        push_rows = parse_keyboard('inline_keyboard = [' + arg + ']' if inner.startswith('{') else 'inline_keyboard = ' + arg)
        when = []
        conds, _ = flow.at(m.start())
        when += [x for x in conds if str(x) not in have]
        d = _directive_at(src, m.start())
        if d:   # постфиксный `… ) IF условие` в той же директиве
            tail = src[c + 1:d[1]]
            pm = re.match(r'\s*(IF|UNLESS)\s+(.+?)\s*-?\}\}$', tail, re.I | re.S)
            if pm:
                when.append(human_cond(flow._subst(pm.group(2), m.start()), pm.group(1).upper() == 'UNLESS'))
        w = [str(x) for x in when if not getattr(x, 'tech', False)] + [str(x) for x in when if getattr(x, 'tech', False)]
        for row in push_rows:
            for b in row:
                if w:
                    b['when'] = w
            rows.append(row)
    return rows


def parse_tg_sends(src, vs=None, flow=None):
    vs = vs or Vars(src)
    flow = flow or Flow(src, vs)
    blocks = find_blocks(src)

    def captures(var, pos):
        """Какие `var = BLOCK` могли выполниться перед отправкой в pos (по веткам IF)."""
        send_path = flow.path_at(pos)
        out = []
        for b in blocks:
            if b[0] != var or b[1] >= pos:
                continue
            ok, on_path = Flow.compatible(flow.path_at(b[1]), send_path)
            if not ok:
                continue
            if on_path:
                out = []
            out.append(b)
        return out

    out = []
    for pos, region, prof in _send_regions(src):
        mm = re.search(r'\b(' + '|'.join(TG_METHODS) + r')\b', region)
        method = mm.group(1) if mm else 'sendMessage'
        if method in NO_CONTENT:
            continue
        # тело отправки в переменной: sendMessage = content
        vm = re.fullmatch(r'\s*\w+\s*=?\s*(?:\(\s*)?([A-Za-z_]\w*)(?:\s*\))?\s*', region)
        if vm and not re.search(KEY('(?:text|caption)'), region):
            body = _hash_of_var(src, vm.group(1), pos)
            if body:
                region = method + ' = ' + body
        send_conds, _ = flow.at(pos)
        text, variants = '', None
        tm = re.search(KEY('(?:text|caption)'), region)
        if tm:
            val, is_lit = _value_at(region, tm.end())
            inner = re.fullmatch(r'\{\{-?\s*(\w+)[^}]*\}\}', val.strip()) if is_lit else None
            var = inner.group(1) if inner else (None if is_lit else (re.fullmatch(r'\w+', val) or [None])[0])
            caps = captures(var, pos) if var else []
            if len(caps) > 1:
                # TEXT = BLOCK задан в нескольких ветках — у каждой свой текст (и свои варианты)
                have = {str(c) for c in send_conds}
                variants = []
                for b in caps:
                    own = [c for c in flow.at(b[1])[0] if str(c) not in have]
                    for extra, vtext in (text_variants(b[3]) or [([], b[3])]):
                        variants.append((own + extra, vtext))
            elif caps:
                text = caps[0][3]
                variants = text_variants(text)
            elif is_lit:
                text = val.replace('\\n', '\n')
            else:
                text = concat_text(val)
        media = ''
        for key in ('photo', 'document', 'video', 'animation'):
            pm = re.search(KEY(key), region)
            if pm:
                media = key + ': ' + _value_at(region, pm.end())[0]
                break
        profile = None
        if prof:
            profile = _resolve_str(vs, prof, pos, prof)
        km = re.search(KEY('(?:inline_keyboard|keyboard)') + r'([A-Za-z_]\w*)\s*(?:[,}\n]|$)', region)
        buttons = _dynamic_buttons(src, km.group(1), pos, flow, send_conds) if km else parse_keyboard(region)
        for extra, vtext in (variants or [([], text)]):
            # у кнопок не повторяем условия, которые уже есть у самого варианта сообщения
            known = {str(c) for c in extra}
            vbuttons = [[{**b, 'when': [w for w in b['when'] if w not in known]} if b.get('when') else b for b in row]
                        for row in buttons]
            for row in vbuttons:
                for b in row:
                    if 'when' in b and not b['when']:
                        del b['when']
            out.append({
                'method': method,
                'text': humanize_tt(vtext),
                'media': media,
                'buttons': vbuttons,
                'profile': profile,
                'extra_when': extra,
                'variant': bool(variants),
                'pos': pos,
            })
    return out


# ---------------------------------------------------------------- варианты текста одного сообщения

DISPATCH_RE = re.compile(r'\b(event_name|us\.status|us\.status_before|remna_action|request\.params\.\w+|cmd)\b')
MAX_VARIANTS = 16


def _text_tree(src):
    """Дерево TEXT-блока: текст, цепочки IF/ELSIF/ELSE (с позициями), STOP. Прочие блоки — как текст."""
    root = {'kind': 'seq', 'children': []}
    stack = [root]
    pos = 0

    def add(x):
        top = stack[-1]
        (top['branches'][-1]['children'] if top['kind'] == 'chain' else top['children']).append(x)

    for d in DIRECTIVE_RE.finditer(src):
        if stack[-1]['kind'] != 'raw' and d.start() > pos:
            add(('text', src[pos:d.start()]))
        stmts = _stmts(d.group(1))
        st = stmts[0] if len(stmts) == 1 else ''
        head = st.split(None, 1)[0] if st else ''
        rest = st[len(head):].strip()
        top = stack[-1]
        if top['kind'] == 'raw':  # внутри FOR/другого блока — считаем только вложенность
            if head in Flow.OPEN or re.match(r'^\w+\s*=\s*(BLOCK|PERL)\b', st):
                top['depth'] += 1
            elif head == 'END':
                top['depth'] -= 1
                if top['depth'] == 0:
                    stack.pop()
                    add(('text', src[top['start']:d.end()]))
            pos = d.end()
            continue
        if head in ('IF', 'UNLESS'):
            stack.append({'kind': 'chain', 'start': d.start(), 'branches': [
                {'cond': rest, 'neg': head == 'UNLESS', 'children': []}]})
        elif head == 'ELSIF' and top['kind'] == 'chain':
            top['branches'].append({'cond': rest, 'neg': False, 'children': []})
        elif head == 'ELSE' and top['kind'] == 'chain':
            top['branches'].append({'cond': None, 'neg': False, 'children': []})
        elif head == 'END' and top['kind'] == 'chain':
            stack.pop()
            top['end'] = d.end()
            add(('chain', top))
        elif head in ('STOP', 'RETURN') and not rest:
            add(('stop', None))
        elif head in Flow.OPEN or re.match(r'^\w+\s*=\s*(BLOCK|PERL)\b', st):
            stack.append({'kind': 'raw', 'start': d.start(), 'depth': 1})
        else:
            add(('text', d.group(0)))
        pos = d.end()
    if pos < len(src):
        add(('text', src[pos:]))
    while len(stack) > 1:  # незакрытое — отдаём как есть
        stack.pop()
    return root, src


def _branch_cond(chain, i):
    br = chain['branches'][i]
    if br['cond'] is not None:
        return human_cond(br['cond'], br['neg'])
    prior = [b['cond'] for b in chain['branches'][:i]]
    if len(prior) == 1:
        return human_cond(prior[0], not chain['branches'][0]['neg'])
    return H('иначе (ни одно из условий выше)')


def _only_stop(children):
    items = [c for c in children if not (c[0] == 'text' and not c[1].strip())]
    return bool(items) and all(c[0] == 'stop' for c in items)


def _variants(children, src):
    """-> [(условия, текст)] ; None если вариантов слишком много."""
    out = [([], '')]
    for kind, val in children:
        if kind == 'text':
            out = [(c, t + val) for c, t in out]
        elif kind == 'stop':
            return []  # эта ветка сообщение не отправляет
        else:
            ch = val
            raw = src[ch['start']:ch.get('end', ch['start'])]
            conds = ' '.join(b['cond'] or '' for b in ch['branches'])
            # `IF x STOP END` — это условие, а не текст
            if len(ch['branches']) == 1 and _only_stop(ch['branches'][0]['children']):
                b = ch['branches'][0]
                g = human_cond(b['cond'], not b['neg'])
                out = [(c + [g], t) for c, t in out]
                continue
            dispatch = DISPATCH_RE.search(conds) or len(ch['branches']) >= 3
            if not dispatch:
                out = [(c, t + raw) for c, t in out]
                continue
            sub = []
            for i, b in enumerate(ch['branches']):
                bc = _branch_cond(ch, i)
                vs = _variants(b['children'], src)
                if vs is None:
                    return None
                sub += [([bc] + c, t) for c, t in vs]
            out = [(c1 + c2, t1 + t2) for c1, t1 in out for c2, t2 in sub]
            if len(out) > MAX_VARIANTS:
                return None
    return out


def text_variants(raw):
    """Разбивает TEXT-блок с ветвлением по событию/статусу на отдельные сообщения."""
    root, src = _text_tree(raw)
    vs = _variants(root['children'], src)
    if not vs or len(vs) < 2:
        return None
    return vs


def concat_text(expr):
    """'Осталось: ' _ expire_str _ ' ₽'  ->  Осталось: {{ expire_str }} ₽"""
    parts = _split_top(expr, (' _ ',))
    out = []
    for p in parts:
        p = p.strip()
        if p[:1] in '"\'' and p[-1:] == p[:1]:
            out.append(p[1:-1].replace('\\n', '\n'))
        else:
            out.append('{{ ' + p + ' }}')
    return ''.join(out)


def parse_bot_calls(src, vs=None):
    """telegram.bot('telegram_bot', '/cmd', [args]) -> команда бота."""
    vs = vs or Vars(src)
    out = []
    arg = r"""(['"][^'"]*['"]|[A-Za-z_][\w.]*)"""
    for m in re.finditer(r'telegram\s*\.\s*bot\s*\(\s*' + arg + r'\s*,\s*' + arg, src):
        bot = _resolve_str(vs, m.group(1), m.start(), 'telegram_bot')
        cmd = _resolve_str(vs, m.group(2), m.start())
        if cmd:
            out.append({'bot': bot, 'cmd': cmd, 'pos': m.start()})
    return out


def bot_case(src, cmd):
    """Текст ветки `<% CASE '/cmd' %>` (или CASE [ '/a', '/b' ]) шаблона бота."""
    marks = list(re.finditer(r'<%-?\s*(CASE|END)\b(.*?)-?%>', src, re.S | re.I))
    for i, m in enumerate(marks):
        if m.group(1).upper() != 'CASE':
            continue
        vals = re.findall(r"""['"]([^'"]+)['"]""", m.group(2))
        if cmd in vals:
            end = marks[i + 1].start() if i + 1 < len(marks) else len(src)
            return src[m.end():end]
    return None


def parse_push(src, vs=None):
    """Happ push: PushNotificationForm = { title, body, link_for_open }."""
    out = []
    for m in re.finditer(KEY('PushNotificationForm') + r'\{', src):
        end = _match_bracket(src, m.end() - 1, '{', '}')
        obj = src[m.end():end]
        f = {}
        for k in ('title', 'body', 'link_for_open', 'type_push', 'expire_days'):
            km = re.search(KEY(k), obj)
            if km:
                v, lit = _value_at(obj, km.end())
                f[k] = v if lit else concat_text(v)
        out.append({'kind': 'happ', 'title': humanize_tt(f.get('title', '')), 'text': humanize_tt(f.get('body', '')),
                    'link': f.get('link_for_open', ''), 'push_type': f.get('type_push', ''), 'pos': m.start()})
    return out


def parse_actions(src):
    acts = []
    for m in re.finditer(KEY('action_type') + r"""['"]([\w-]+)['"]""", src):
        acts.append('Happ: команда ' + m.group(1))
    if re.search(r'\bexternalSquadUuid\s*=', src) and re.search(r'http\s*\.\s*patch', src):
        acts.append('Remnawave: смена external squad')
    for m in re.finditer(r'\b(us|user)\s*\.\s*set_settings\s*\(\s*\{\s*[\'"]?(\w+)', src):
        acts.append(('услуга' if m.group(1) == 'us' else 'пользователь') + ': settings.' + m.group(2))
    if re.search(r'storage\s*\.\s*save\s*\(', src):
        acts.append('storage: сохранение')
    seen, out = set(), []
    for a in acts:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def parse_entries(src):
    """Публичный шаблон-диспетчер: `x = request.params.event` и ветки `x == 'значение'`."""
    for m in re.finditer(r'\b(\w+)\s*=\s*request\.params\.(\w+)\s*(?:\}\}|;|\n)', src):
        var, vals = m.group(1), []
        for v in re.findall(r'\b' + var + r'\s*==\s*[\'"]([^\'"]+)[\'"]', src):
            if v not in vals:
                vals.append(v)
        if vals:
            return {'var': var, 'param': m.group(2), 'values': vals}
    return None


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


def _split_when(conds):
    # «A ИЛИ B ИЛИ C», за которым внутри идёт конкретное «A», — лишнее
    conds = [c for i, c in enumerate(conds)
             if not (' ИЛИ ' in c and any(x in c.split(' ИЛИ ') for x in conds[i + 1:]))]
    return [c for c in conds if not getattr(c, 'tech', False)], [c for c in conds if getattr(c, 'tech', False)]


def analyze(src):
    desc = first_comment(src)
    body = strip_comments(src)
    vs = Vars(body)
    flow = Flow(body, vs)

    def place(item):
        conds, dead = flow.at(item.pop('pos'))
        conds = conds + item.pop('extra_when', [])
        item['when'], item['when_tech'] = _split_when(conds)
        return not dead

    tg = [m for m in parse_tg_sends(body, vs, flow) if place(m)]
    push = [m for m in parse_push(body, vs) if place(m)]
    bots = [m for m in parse_bot_calls(body, vs) if place(m)]
    spool, seen = [], {}
    for j in parse_spool(body):
        if not place(j):
            continue
        k = (j['template'], j['delay'], j['period'], j['delay_expr'])
        if k in seen:  # та же задача в другой ветке — объединяем условия
            prev = seen[k]
            if j['when'] != prev['when']:
                prev.setdefault('alt', []).append(j['when'])
            continue
        seen[k] = j
        spool.append(j)
    guards, guards_tech = _split_when(flow.root_guards)
    return {
        'guards': guards,
        'guards_tech': guards_tech,
        'stops': [{'when': list(w)} for w, _ in flow.stops if w and not any(isinstance(x, tuple) for x in w)][:12],
        'description': desc,
        'tg': tg,
        'push': push,
        'bot_calls': bots,
        'spool': spool,
        'calls': parse_calls(body),
        'http': parse_http(body),
        'actions': parse_actions(body),
        'entries': parse_entries(body),
        'mail_send': bool(re.search(r'\bmail\w*\s*\.\s*send\s*\(', body)),
        'email_text': html_to_text(body) if not tg else '',
    }


def has_actions(info):
    return bool(info['tg'] or info['push'] or info['bot_calls'] or info['spool'] or info['calls'] or info['mail_send'])


def bot_cases(src):
    """Команды бота: значения всех <% CASE … %>."""
    out = []
    for m in re.finditer(r'<%-?\s*CASE\b(.*?)-?%>', src or '', re.S | re.I):
        for v in re.findall(r"""['"]([^'"]+)['"]""", m.group(1)):
            if v not in out:
                out.append(v)
    return out


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
REMNA_RU = {
    'user.first_connected': 'первое подключение', 'user.not_connected': 'не подключился',
    'user.limited': 'превышен лимит трафика', 'user.bandwidth_usage_threshold_reached': 'приближается к лимиту трафика',
    'user_hwid_devices.added': 'добавлено устройство', 'user_hwid_devices.deleted': 'удалено устройство',
    'user.revoked': 'ссылка подписки сброшена', 'user.expired': 'подписка истекла', 'user.disabled': 'пользователь отключён',
    'user.enabled': 'пользователь включён', 'user.created': 'пользователь создан', 'user.deleted': 'пользователь удалён',
    'user.modified': 'пользователь изменён', 'user.expires_in_24_hours': 'истекает через 24 ч',
    'user.expires_in_48_hours': 'истекает через 48 ч', 'user.expires_in_72_hours': 'истекает через 72 ч',
}
EVENT_RU = {
    'REGISTERED': 'регистрация', 'CREATE': 'создание услуги', 'ACTIVATE': 'активация', 'BLOCK': 'блокировка',
    'REMOVE': 'удаление', 'PROLONGATE': 'продление', 'PAYMENT': 'платёж', 'BONUS': 'бонус', 'CHANGED': 'смена статуса',
    'FORECAST': 'прогноз оплаты', 'CHANGED_TARIFF': 'смена тарифа', 'NOT_ENOUGH_MONEY': 'не хватает денег',
}


class H(str):
    """Строка условия на русском; tech=True — техническая проверка (сворачивается)."""

    def __new__(cls, text, tech=False):
        o = str.__new__(cls, text)
        o.tech = tech
        return o


def T(text):
    return H(text, True)


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
    a2 = _strip_parens(re.sub(r'\s+', ' ', a))
    # скобочная группа (A || B) внутри && — это не атом
    if len(_split_top(a2, ('||', ' or ', ' OR '))) > 1 or len(_split_top(a2, ('&&', ' and ', ' AND '))) > 1:
        return human_cond(a2, neg)
    r = _atom(a, neg)
    return r if isinstance(r, H) else H(r)


def _atom(a, neg=False):
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
        if lhs == 'event_name' and op in ('==', '!=', 'eq', 'ne'):
            return ('событие ' if eq else 'событие не ') + val.lower() + (f' ({EVENT_RU[val]})' if val in EVENT_RU else '')
        if val in REMNA_RU and op in ('==', '!=', 'eq', 'ne'):
            return ('Remnawave: ' if eq else 'Remnawave не: ') + REMNA_RU[val]
        if lhs == 'user.settings.email_verified':
            return 'email подтверждён' if (eq and val == '1') or (not eq and val == '0') else 'email не подтверждён'
        m2 = re.fullmatch(r'(us|user)\.settings\.(\w+)', lhs)
        if m2 and op in ('==', '!=', 'eq', 'ne'):
            who = 'у услуги' if m2.group(1) == 'us' else 'у пользователя'
            return f'{who} {m2.group(2)} {"=" if eq else "≠"} {val}'
        if lhs in ('user.discount', 'user.balance', 'bonus.bonus'):
            return {'user.discount': 'скидка', 'user.balance': 'баланс', 'bonus.bonus': 'бонус'}[lhs] + f' {op} {val}'
        if rhs == 'NULL' or rhs == "''":
            return T(('не задано ' if eq else 'задано ') + lhs)
        return T(f'{lhs} {op} {rhs}')
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
        return T('нет ' + a)
    return T(('НЕ ' if neg else '') + re.sub(r'\s+', ' ', a))


def human_cond(c, neg=False):
    c = _strip_parens(re.sub(r'\s+', ' ', c))
    atoms = [x for o in _split_top(c, ('||', ' or ', ' OR ')) for x in _split_top(_strip_parens(o), ('&&', ' and ', ' AND '))]
    tech = all(human_atom(x).tech for x in atoms)
    return H(_human_cond(c, neg), tech)


def _human_cond(c, neg=False):
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
        parts, seen = [], set()
        for o in ors:
            h = '(' + human_cond(o) + ')' if len(_split_top(o, ('&&', ' and ', ' AND '))) > 1 else str(human_cond(o))
            k = re.sub(r' \(.*\)$', '', h).lower()
            if k not in seen:
                seen.add(k)
                parts.append(h)
        return ' ИЛИ '.join(parts)
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

    def __init__(self, src, vs=None):
        self.vs = vs
        self.snaps = [(0, ())]   # (pos, tuple условий)
        self.paths = [(0, ())]   # (pos, путь веток: ((id IF, № ветки), ...))
        self._ids = 0
        # секции `<% CASE … %>` шаблона бота — тоже взаимоисключающие ветки
        self.cases = [m.start() for m in re.finditer(r'<%-?\s*CASE\b', src, re.I)]
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
                if head in ('IF', 'UNLESS', 'ELSIF', 'STOP', 'RETURN'):
                    rest = self._subst(rest, d.start())
                if head in ('IF', 'UNLESS'):
                    self._ids += 1
                    stack.append({'kind': head, 'cond': human_cond(rest, head == 'UNLESS'), 'prior': [rest], 'guards': [],
                                  'id': self._ids, 'br': 0})
                elif head == 'ELSIF':
                    top['cond'] = human_cond(rest)
                    top['prior'].append(rest)
                    top['guards'] = []
                    top['br'] = top.get('br', 0) + 1
                elif head == 'ELSE' and top['kind'] in ('IF', 'UNLESS'):
                    top['br'] = top.get('br', 0) + 1
                    pr = top['prior']
                    if top['kind'] == 'UNLESS':
                        top['cond'] = human_cond(pr[0])
                    elif len(pr) == 1:
                        top['cond'] = human_cond(pr[0], True)
                    else:
                        top['cond'] = H('иначе (ни одно из условий выше)')
                    top['guards'] = []
                elif head in ('FOR', 'FOREACH'):
                    m = re.match(r'(\w+)\s+(?:IN|=)\s+(.+)', rest)
                    stack.append({'kind': 'FOR', 'cond': T(f'для каждого {m.group(1)} из {m.group(2).strip()[:60]}') if m else None,
                                  'prior': [], 'guards': []})
                elif head == 'SWITCH':
                    self._ids += 1
                    stack.append({'kind': 'SWITCH', 'cond': None, 'subj': rest, 'prior': [], 'guards': [], 'id': self._ids, 'br': -1})
                elif head == 'CASE' and top['kind'] == 'SWITCH':
                    top['br'] += 1
                    top['cond'] = H(f'{top["subj"]} = {rest}' if rest and rest != 'DEFAULT' else f'{top["subj"]}: остальные случаи')
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
            self.paths.append((d.end(), tuple((fr['id'], fr['br']) for fr in stack if 'id' in fr)))
        self.root_guards = list(stack[0]['guards'])

    def _subst(self, cond, pos):
        """status → user.settings.telegram.telegram_bot.status, если выше было `status = user.settings…status`."""
        if not self.vs:
            return cond

        def repl(m):
            a = self.vs.last(m.group(1), pos)
            v = a[2][1] if a and a[2][0] in ('expr', 'ref') else None
            return v if v and v != m.group(1) and re.fullmatch(r'[A-Za-z_][\w.]*\.[\w.]+', v) else m.group(1)
        return re.sub(r'(?<![\w.\'"$])([A-Za-z_]\w*)(?![\w(])', repl, cond)

    def path_at(self, pos):
        lo, hi = 0, len(self.paths) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.paths[mid][0] <= pos:
                lo = mid
            else:
                hi = mid - 1
        path = self.paths[lo][1]
        if self.cases:
            n = sum(1 for c in self.cases if c <= pos)
            path = (('case', n),) + path
        return path

    @staticmethod
    def compatible(cap_path, send_path):
        """-> (может выполниться перед отправкой, лежит на её пути)"""
        mine = dict(send_path)
        on_path = True
        for node, br in cap_path:
            if node not in mine:
                on_path = False
            elif mine[node] != br:
                return False, False
        return True, on_path

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
