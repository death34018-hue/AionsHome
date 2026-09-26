"""Independent SL278H AI protocol. No BLE access or legacy TOY commands here."""
from contextvars import ContextVar
import re
import json
import time
from time import monotonic
from uuid import uuid4
from database import get_db
from ws import manager

COMMAND_PATTERN = re.compile(r'\[SVAKOM:([^\]]*)\]', re.IGNORECASE)
# Display/TTS cleanup is deliberately broader than the executable grammar:
# an interrupted or malformed control tag must not become ordinary chat text.
DISPLAY_PATTERN = re.compile(r'\[SVAKOM\b[^\]]*(?:\]|$)', re.IGNORECASE)
_epoch = uuid4().hex
_permission = ContextVar('svakom_request_permission', default=None)
_reports = {}
STATE_TTL = 15

PROMPT = """【新玩具独立编排指南】
用户已开启 SVAKOM SL278H 的 AI 编排能力。
主体动作 1～7 是模式编号，不是强度：1 慢速旋转伸缩；2 中速旋转伸缩；3 三短一长；4 混合变速；5 单次停顿脉冲；6 快速停顿脉冲；7 连续短脉冲。
主体震动花样 1～10：1 高频连续波；2 轻柔细振波；3 渐强波；4 慢速起伏波；5 间歇脉冲；6 快速锯齿波；7 中速锯齿波；8 阶梯脉冲；9 高速方波脉冲；10 低速方波脉冲。震动力度另选 1～10，由弱到强。
上述节奏名称来自参考描述，主体动作不提供速度滑杆参数。小部件是豆豆独立拍打，档位 1～7 由弱到强，不是主体震动。
当前参考强度由控制器状态计算：拍打档位换算到 0～10
唯一编排格式：[SVAKOM:LOOP:秒数,主体模式,震动花样,震动力度,拍打档位;下一段;...]
每段必须写全五个整数。持续秒数为正整数，1～63 段，每轮总时长不超过 600 秒。无需计算起止时间。
主体模式 0～7、震动花样 0～10、拍打档位 0～7；0 表示关闭该路。震动花样为 0 时力度必须为 0，否则力度为 1～10。
允许三路全部关闭作为休息段，例 5,0,0,0,0 表示休息 5 秒。
语法示例：[SVAKOM:LOOP:3,1,0,0,1;5,3,2,2,2;4,0,0,0,0]
根据当前语境和节奏进行编排，例如刚开始可以只使用豆豆拍打[SVAKOM:LOOP:5,0,0,0,1;]
程序顺序执行并在末尾回到第一段，循环直到替换或停止。一条回复最多一份完整编排，新编排替换旧编排，不叠加。
立即终止：[SVAKOM:STOP]。用户要求停止时优先输出此指令，不在同一条回复中再安排启动。不要将停止写作全关闭的无限循环。
"""


def state():
    from capabilities import is_capability_enabled
    import toy_profiles
    return {'enabled': toy_profiles.active() == 'svakom' and is_capability_enabled('svakom'),
            'epoch': _epoch + ':' + toy_profiles.state()['epoch']}


def invalidate_permission():
    global _epoch
    _epoch = uuid4().hex
    current = state()
    if not current['enabled']:
        _reports.clear()
    return current


def report_state(report):
    """Keep transient controller telemetry only; never store it as conversation/memory."""
    if not state()['enabled']:
        return
    source = report['source']
    previous = _reports.get(source)
    if previous and previous['sequence'] >= report['sequence']:
        return
    if source not in _reports and len(_reports) >= 16:
        del _reports[min(_reports, key=lambda key: _reports[key]['updated_at'])]
    _reports[source] = {**report, 'updated_at': monotonic()}


def reference_strength_prompt():
    fresh = [r for r in _reports.values() if r['connected'] and monotonic() - r['updated_at'] <= STATE_TTL]
    # Two live controllers are ambiguous; an idle page cannot overwrite the connected one.
    if len(fresh) != 1:
        return '当前参考强度：未知（未连接、状态过期或来源不唯一）。'
    report = fresh[0]
    flap_level = int(report['flap'] * 10 / 7 + 0.5)
    vibration_level = report['vibrate_level'] if report['vibrate'] else 0
    return f'当前参考强度：{max(flap_level, vibration_level)}/10'


async def _save_command_notice(msg_id, raw, command, status, *, conv_id=None, room_id=None, profile='svakom'):
    if not conv_id and not room_id:
        return
    if command == 'STOP':
        summary = '新玩具编排 · 停止'
    elif command and command.startswith('LOOP:'):
        summary = f'新玩具编排 · {command.count(";") + 1} 段循环'
    else:
        summary = '新玩具编排 · 未下发'
    if profile == 'ankni':
        summary = summary.replace('新玩具', 'ANKNI')
    notice_id = f'{profile}_notice_{msg_id}'
    attachments = [
        {'type': f'{profile}_command_notice', 'raw': raw, 'status': status},
        {'type': 'system_notice_order', 'after_msg_id': str(msg_id)},
    ]
    if profile == 'ankni':
        attachments[0]['format'] = 'duration_modes_v1'
    now = time.time()
    table, scope_key, role_key = ('messages', 'conv_id', 'role') if conv_id else ('chatroom_messages', 'room_id', 'sender')
    scope_id = conv_id or room_id
    async with get_db() as db:
        await db.execute(
            f'INSERT OR REPLACE INTO {table} (id, {scope_key}, {role_key}, content, created_at, attachments) VALUES (?,?,?,?,?,?)',
            (notice_id, scope_id, 'system', summary, now, json.dumps(attachments, ensure_ascii=False)),
        )
        await db.commit()
    await manager.broadcast({'type': 'msg_created' if conv_id else 'chatroom_msg_created', 'data': {
        'id': notice_id, scope_key: scope_id, role_key: 'system', 'content': summary,
        'created_at': now, 'attachments': attachments,
    }})


def capture_permission(*, excluded=False):
    current = state()
    enabled = current['enabled'] and not excluded
    _permission.set(current['epoch'] if enabled else None)
    return enabled


def validate_command(raw):
    raw = re.sub(r'\s+', '', raw).upper()
    if raw == 'STOP':
        return raw
    if not raw.startswith('LOOP:') or len(raw) > 4096:
        raise ValueError('invalid SVAKOM command')
    rows = raw[5:].split(';')
    if not 1 <= len(rows) <= 63:
        raise ValueError('invalid phase count')
    total = 0
    normalized = []
    for row in rows:
        if not re.fullmatch(r'[0-9]{1,4}(?:,[0-9]{1,4}){4}', row):
            raise ValueError('expected five integer fields')
        duration, stretch, vibrate, level, flap = map(int, row.split(','))
        total += duration
        if not (duration >= 1 and total <= 3600 and 0 <= stretch <= 7 and 0 <= vibrate <= 10
                and 0 <= flap <= 7 and (level == 0 if vibrate == 0 else 1 <= level <= 10)):
            raise ValueError('phase out of range')
        normalized.append(','.join(map(str, (duration, stretch, vibrate, level, flap))))
    return 'LOOP:' + ';'.join(normalized)


async def process_commands(text, msg_id, *, conv_id=None, room_id=None):
    matches = COMMAND_PATTERN.findall(text or '')
    raw = '\n'.join(m.group(0) for m in DISPLAY_PATTERN.finditer(text or ''))
    cleaned = DISPLAY_PATTERN.sub('', text or '').strip()
    if not raw:
        return cleaned
    command = None
    current = state()
    if not current['enabled']:
        status = '未下发：AI 控制开关已关闭'
    elif _permission.get() != current['epoch']:
        status = '未下发：控制权已撤销，旧回复失效'
    else:
        try:
            # STOP wins even if the model mistakenly includes a plan in the same reply.
            if any(re.sub(r'\s+', '', item).upper() == 'STOP' for item in matches):
                command = 'STOP'
            elif matches:
                if any(not COMMAND_PATTERN.fullmatch(item.group(0)) for item in DISPLAY_PATTERN.finditer(text or '')):
                    raise ValueError('incomplete control tag')
                command = [validate_command(item) for item in matches][-1]
            else:
                raise ValueError('incomplete control tag')
        except ValueError:
            status = '未下发：编排格式无效或不完整'
        else:
            if command == 'STOP':
                current = invalidate_permission()
            await manager.broadcast({'type': 'svakom_command', 'data': {
                'command': command, 'epoch': current['epoch'], 'msg_id': str(msg_id),
                'event_id': f'svakom:{msg_id}',
            }})
            status = '已广播至控制页面；设备执行未确认。未连接的页面不会执行，也不会缓存补发。'
    await _save_command_notice(msg_id, raw, command, status, conv_id=conv_id, room_id=room_id)
    return cleaned
