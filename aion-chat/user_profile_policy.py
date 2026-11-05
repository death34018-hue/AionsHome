"""Lifecycle and bounded presentation of the shared profile (no model or I/O)."""
from datetime import datetime
import math
import re
import time

CATEGORIES = {'recent': '近期生活与待办'}
SUBCATEGORIES = {'recent': {'plans': '待办与安排', 'projects': '正在进行', 'state': '近期状态',
                            'preferences': '近期偏好', 'ideas': '想做的事'}}
KINDS = {'context': '近期情况', 'project': '正在进行', 'task': '明确待办', 'idea': '尚未着手'}
STATUSES = {'active': '当前关注', 'stale': '久未确认', 'unknown': '结果未确认',
            'done': '已完成', 'cancelled': '已取消', 'retired': '已淡出'}
PHASES = {'planned': '计划', 'ongoing': '进行中', 'finished': '已结束', 'unknown': '未确认'}
STATE_MAX_CHARS = 160
PROMPT_LIMIT = 2000
MAX_ENTRIES = 24
MAX_SAVED_CHARS = 1600
DAY = 86400
LEGACY_CATEGORIES = {}
SUBJECT_RE = re.compile(r'(?<!其他)(?<!其它)(?<!每个)(?<!每位)(?<!所有)(?<!多)用户'
                        r'(?!画像|体验|界面|账号|账户|权限|认证|登录|注册|管理|数据|设置|配置|消息|名称|姓名)')


def personalize_text(text, user_name):
    """Name the profile subject while preserving product terms and other people."""
    name = user_name.strip() or '你'
    # Preserve an already configured name even if it contains the word 用户.
    return name.join(SUBJECT_RE.sub(lambda _: name, part) for part in text.split(name))


def personalize_document(document, user_name):
    """Cosmetic cleanup only: do not advance confirmation times or change locks."""
    count = 0
    for entry in document['entries'].values():
        if entry.get('deleted'):
            continue
        changed = False
        for field in ('content', 'topic'):
            text = entry.get(field, '')
            renamed = personalize_text(text, user_name)
            if renamed != text:
                entry[field] = renamed
                changed = True
        count += changed
    state = document.get('current_state')
    state_changed = False
    if state:
        renamed = personalize_text(state['text'], user_name)
        state_changed = renamed != state['text']
        state['text'] = renamed
    return count, state_changed


def remove_manual_protection(document):
    """Keep deletion barriers, but every visible fact and activity can evolve."""
    changed = False
    values = [e for e in document['entries'].values() if not e.get('deleted')]
    if document.get('current_state'):
        values.append(document['current_state'])
    for value in values:
        if value.get('locked'):
            value['locked'] = False
            changed = True
    return changed


def unfinished_task(entry):
    return entry.get('kind') == 'task' and entry.get('status', 'active') not in ('done', 'cancelled', 'retired')


def category(value):
    if value in LEGACY_CATEGORIES:
        return LEGACY_CATEGORIES[value]
    if value not in CATEGORIES:
        raise ValueError('画像分类无效')
    return value


def timestamp(value):
    if value in (None, ''):
        return None
    try:
        result = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError, OverflowError):
        raise ValueError('时间请使用有效日期或时间戳') from None
    if not math.isfinite(result):
        raise ValueError('时间无效')
    return result


def normalize(entry):
    result = dict(entry)
    result['category'] = category(entry.get('category'))
    result.setdefault('topic', '')
    result.setdefault('kind', 'context')
    result.setdefault('status', 'active')
    result.setdefault('sustained', False)
    result['subcategory'] = subcategory(result)
    result.setdefault('importance', 0.6)
    result.setdefault('certainty', 'fact')
    result.setdefault('keywords', [])
    return result


def subcategory(entry):
    value = entry.get('subcategory')
    if value in SUBCATEGORIES['recent']:
        return value
    return {'task': 'plans', 'project': 'projects', 'idea': 'ideas'}.get(entry.get('kind'), 'state')


def estimate_tokens(text):
    """Conservative, dependency-free estimate; deliberately labelled approximate in the UI."""
    return math.ceil(sum(0.3 if ord(c) < 128 else 1.5 for c in text))


def relevant_fact(entry, query):
    query = query.casefold().strip()
    if not query:
        return False
    terms = entry.get('keywords') or []
    topic = entry.get('topic', '').strip()
    if topic:
        terms = [*terms, topic]
    if any(len(term.strip()) >= 2 and term.casefold().strip() in query for term in terms):
        return True
    # Legacy facts without retrieval keywords remain discoverable by specific shared phrases.
    phrases = re.findall(r'[\u4e00-\u9fff]{3,}|[a-zA-Z0-9_]{3,}', query)
    text = entry.get('content', '').casefold()
    return any(p in text for p in phrases)


def lifecycle(entry, now):
    status = entry.get('status', 'active')
    if entry.get('deleted') or entry.get('category') != 'recent':
        return 'retired'
    if status in ('done', 'cancelled', 'retired'):
        return status
    confirmed = entry.get('source_ts', now)
    age = max(0, now - confirmed)
    expiry = entry.get('expires_at')
    if entry.get('kind') == 'task':
        due = entry.get('due_at')
        return 'unknown' if (due and due < now) or age >= 7*DAY else status
    if entry.get('kind') == 'project' and expiry:
        return 'unknown' if now >= expiry else status
    # Ordinary circumstances, recent tastes and unstarted ideas get one week at most.
    cutoff = min(expiry or confirmed + 7*DAY, confirmed + 7*DAY)
    return 'retired' if now >= cutoff else status


def maintain(document, now=None):
    now = time.time() if now is None else now
    changed = False
    for key, entry in list(document['entries'].items()):
        if entry.get('deleted'):
            continue
        status = lifecycle(entry, now)
        if status != entry.get('status', 'active'):
            entry['status'] = status
            if status == 'retired':
                entry['retired_at'] = now
            changed = True
        retired_at = entry.get('retired_at') or entry.get('updated_at', now)
        if status in ('done', 'cancelled', 'retired') and now-retired_at >= 7*DAY:
            del document['entries'][key]
            changed = True
    return changed


def ranked(entries, now):
    return sorted((e for e in entries if lifecycle(e, now) not in ('done', 'cancelled', 'retired')
                   and ('manual' in e.get('sources', []) or
                        (e.get('kind') != 'idea' and subcategory(e) not in ('ideas', 'preferences')))),
                  key=lambda e: (e.get('kind') != 'task', -e.get('source_ts', 0)))


def fact_line(entry, now):
    status = lifecycle(entry, now)
    ts = entry.get('source_ts')
    tag = '/'.join(filter(None, (entry.get('topic', ''),
                  datetime.fromtimestamp(ts).strftime('%m-%d') if ts else '',
                  '结果未确认' if status == 'unknown' else '')))
    text = entry['content']
    if entry.get('next_step'):
        text += ' 下一步：' + entry['next_step']
    for field, label in (('available_at', '可开始'), ('due_at', '截止')):
        if entry.get(field):
            text += '（' + label + datetime.fromtimestamp(entry[field]).strftime('%m-%d %H:%M') + '）'
    return '- ' + (tag + '：' if tag else '') + text


def render_context(document, user_name='你', now=None, *, query=''):
    """Render all valid recent items. Writes enforce the budget, not silent selection."""
    now = time.time() if now is None else now
    items = ranked(document['entries'].values(), now)
    state = document.get('current_state')
    live_state = state and state.get('expires_at', 0) > now and state.get('phase') != 'finished'
    if not items and not live_state:
        return '', set()
    lines = [f'以下是基于近期聊天记录整理的{user_name[:40]}的近期生活与待办，供参考。']
    for sub, label in SUBCATEGORIES['recent'].items():
        group = sorted((e for e in items if subcategory(e) == sub),
                       key=lambda e: -(e.get('source_ts') or e.get('updated_at', 0)))
        if group:
            lines.append(f'【{label}】')
            lines.extend(fact_line(e, now) for e in group)
    if live_state:
        lines.extend(['【此刻活动】', f'- {PHASES[state["phase"]]}：{state["text"]}'])
    return '\n'.join(lines), {e['key'] for e in items}
