"""聊天氛围：天气动效和叠加颜色，用户与 AI 共用一份状态。"""
import json
import re

from fastapi import APIRouter

from config import DATA_DIR
from ws import manager


STATE_PATH = DATA_DIR / 'chat_ambience.json'
AMBIENCE_COMMAND_PATTERN = re.compile(r'\[\s*小家氛围\s*[：:]\s*([^\]]*)\]')
EFFECTS = {
    'none': '无', 'rain': '下雨', 'snow': '下雪', 'wind': '刮风', 'leaves': '落叶',
    'sakura': '樱花', 'sun': '阳光', 'firefly': '萤火虫', 'bubble': '泡泡', 'heart': '飘爱心',
    'dandelion': '蒲公英', 'fog': '晨雾', 'aurora': '极光', 'hearth': '炉火',
}
EFFECT_ALIASES = {
    **{label: key for key, label in EFFECTS.items()},
    '关闭': 'none', '雨': 'rain', '雪': 'snow', '叶': 'leaves', '风': 'wind',
    '落樱': 'sakura', '爱心': 'heart', '雾': 'fog', '壁炉': 'hearth',
}
# 播十秒左右就结束的瞬间特效，不改变当前氛围
BURSTS = {'撒花': 'confetti', '烟花': 'fireworks', '流星': 'meteor'}
# 颜色编号 0~7，与前端色块一致
TINTS = ['', '#ff5a5f', '#ff9d42', '#ffd84a', '#57c96b', '#45a8ff', '#4656c9', '#a05cff']
TINT_NAMES = ['不叠加', '红', '橙', '黄', '绿', '蓝', '靛', '紫']
_TINT_PATTERN = re.compile(r'#[0-9a-f]{6}')

router = APIRouter(prefix='/api/chat-ambience', tags=['chat-ambience'])


def _clean(data) -> dict:
    """只保留合法的 effect / tint 字段。"""
    cleaned = {}
    if not isinstance(data, dict):
        return cleaned
    if data.get('effect') in EFFECTS:
        cleaned['effect'] = data['effect']
    tint = data.get('tint')
    if isinstance(tint, str) and (tint == '' or _TINT_PATTERN.fullmatch(tint.lower())):
        cleaned['tint'] = tint.lower()
    return cleaned


def load_state() -> dict | None:
    """从未保存过时返回 None，前端据此上报本机设置。"""
    try:
        saved = _clean(json.loads(STATE_PATH.read_text(encoding='utf-8')))
    except (OSError, ValueError):
        return None
    return {'effect': 'none', 'tint': '', **saved}


async def set_state(patch: dict, source: str = '', burst: str = '') -> dict:
    """保存并广播；source 让发起修改的页面忽略自己的回声，burst 是顺带播放的瞬间特效。"""
    cleaned = _clean(patch)
    state = load_state()
    if cleaned:
        state = {**(state or {'effect': 'none', 'tint': ''}), **cleaned}
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    data = {**(state or {}), 'source': source}
    if burst:
        data['burst'] = burst
    await manager.broadcast({'type': 'chat_ambience', 'data': data})
    return state or {'effect': 'none', 'tint': ''}


def parse_command(value: str) -> dict:
    """解析「效果|颜色编号」或瞬间特效名，认不出的部分保持原样。"""
    effect, _, color = re.sub(r'[｜丨]', '|', value or '').partition('|')
    patch = {}
    if effect.strip() in BURSTS:
        patch['burst'] = BURSTS[effect.strip()]
    elif effect.strip() in EFFECT_ALIASES:
        patch['effect'] = EFFECT_ALIASES[effect.strip()]
    color = color.strip()
    if color.isascii() and color.isdigit() and int(color) < len(TINTS):
        patch['tint'] = TINTS[int(color)]
    return patch


def state_text() -> str:
    """给 AI 看的当前状态，例如「当前小家氛围：下雪|红色」。"""
    state = load_state() or {'effect': 'none', 'tint': ''}
    tint = state['tint']
    if not tint:
        color = '不叠加颜色'
    elif tint in TINTS:
        color = TINT_NAMES[TINTS.index(tint)] + '色'
    else:
        color = '自定义颜色'
    return f'当前小家氛围：{EFFECTS[state["effect"]]}|{color}'


def ability_text(user_name: str) -> str:
    return (
        f'[小家氛围：效果|颜色编号] — 切换聊天界面的氛围动效和叠加的淡色，{user_name}会立刻看到。'
        '效果：' + '、'.join(EFFECTS.values()) + '；'
        '颜色编号：' + '、'.join(f'{index} {name}' for index, name in enumerate(TINT_NAMES)) + '。'
        '例如 [小家氛围：落叶|2]。'
        '聊天的情绪或场景变化时自行切换，氛围合适就不用动。'
        '另有播十秒左右就结束、不改变当前氛围的瞬间特效：' + '、'.join(BURSTS) + '，'
        '写作 [小家氛围：撒花]，留给值得庆祝或许愿的时刻。每次回复最多切换一次、放一个特效。\n'
        + state_text()
    )


async def process_ambience_commands(text: str, *, sender: str) -> str:
    from capabilities import is_capability_enabled

    matches = AMBIENCE_COMMAND_PATTERN.findall(text or '')
    cleaned = AMBIENCE_COMMAND_PATTERN.sub('', text or '').strip()
    if not matches or sender not in ('aion', 'connor') or not is_capability_enabled('chat_ambience'):
        return cleaned
    patch = {}
    for value in matches:
        patch.update(parse_command(value))
    burst = patch.pop('burst', '')
    if patch or burst:
        await set_state(patch, burst=burst)
    return cleaned


@router.get('')
async def api_get_state():
    return {'ok': True, 'state': load_state()}


@router.put('')
async def api_set_state(body: dict):
    return {'ok': True, 'state': await set_state(body, str(body.get('source') or '')[:32])}
