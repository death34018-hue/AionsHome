"""ANKNI MX AI grammar, independent of SOSEXY and SVAKOM."""
from contextvars import ContextVar
from uuid import uuid4
import re
from ws import manager
import toy_profiles

COMMAND_PATTERN = re.compile(r'\[ANKNI:([^\]]*)\]', re.I)
DISPLAY_PATTERN = re.compile(r'\[ANKNI\b[^\]]*(?:\]|$)', re.I)
_permission = ContextVar('ankni_request_permission', default=None)
_epoch = uuid4().hex
PROMPT = """【ANKNI MX 独立控制指南】
当前选择 ANKNI MX，用户已开启 AI 控制。你只编排十种内置模式和停止模式，共十一种选择，不直接控制震动或吮吸强度。
模式编号与名称：0 停止；1 持续浅尝；2 渐入佳境；3 大开大合；4 短浅顶撞；5 蛮牛冲撞；6 愈渐愈强；7 持续挑逗；8 边缘将至；9 快速震弹；10 冲刺。
这些名称由用户根据实际体感定义，可参考名称理解节奏并选择模式。编号不代表强弱排序，不要编造具体频率或强度。
格式：[ANKNI:LOOP:持续毫秒,模式编号;下一段;...]。每组时间就是该段持续多久，按顺序执行，由程序自动累加，无需计算起止时刻。
例如 [ANKNI:LOOP:2000,5;3000,6]：模式 5 持续 2 秒，接着模式 6 持续 3 秒；每轮共 5 秒，随后从模式 5 重新循环。
每组恰好两个整数。每段至少 200 毫秒，共 1～128 段，各段时长总和不超过 600000 毫秒；时长可相同、可变长或变短。模式只能为 0～10。
手机仅在模式变化时下发经典模式，段内由玩具运行内置节奏；相邻相同模式保持连续运行，不重新启动。
模式 0 表示该时间段停止，时间到后继续下一段。例如 [ANKNI:LOOP:2000,5;3000,0;4000,6]：模式 5 持续 2 秒，停止 3 秒，模式 6 持续 4 秒，每轮共 9 秒，再循环。
立即结束整个编排用 [ANKNI:STOP]，会清空正在运行及等待的编排。用户要求停止时优先使用 STOP，同一回复不要再启动。
一次回复最多一份完整编排，循环直到替换或停止。当前编排必须完整播放至少一轮：首轮未结束时，新编排等待首轮结束再接替；已经播完一轮则可以立即接替。等待期间只保留最新的一份编排，不叠加、不积压。停止及手动接管立即生效，不等待首轮。
不输出旧的三列强度指令，不使用 TOY 或 SVAKOM 指令。
未连接时指令不执行、不积压；连接后也不会自动恢复以前的编排。
"""


def state():
    from capabilities import is_capability_enabled
    return {'enabled': toy_profiles.active() == 'ankni' and is_capability_enabled('ankni'),
            'epoch': _epoch + ':' + toy_profiles.state()['epoch']}


def invalidate_permission():
    global _epoch
    _epoch = uuid4().hex
    return state()


def capture_permission(*, excluded=False):
    current = state()
    enabled = current['enabled'] and not excluded
    _permission.set(current['epoch'] if enabled else None)
    return enabled


def validate_command(raw):
    raw = re.sub(r'\s+', '', raw).upper()
    if raw == 'STOP':
        return raw
    if not raw.startswith('LOOP:') or len(raw) > 8192:
        raise ValueError('invalid ANKNI command')
    rows = raw[5:].split(';')
    if not 1 <= len(rows) <= 128:
        raise ValueError('invalid phase count')
    total = 0
    normalized = []
    for row in rows:
        if not re.fullmatch(r'[0-9]{1,6},[0-9]{1,2}', row):
            raise ValueError('expected duration milliseconds,mode')
        duration, mode = map(int, row.split(','))
        total += duration
        if not (200 <= duration <= 600000 and total <= 600000 and 0 <= mode <= 10):
            raise ValueError('phase out of range')
        normalized.append(f'{duration},{mode}')
    return 'LOOP:' + ';'.join(normalized)


async def process_commands(text, msg_id, *, conv_id=None, room_id=None):
    matches = COMMAND_PATTERN.findall(text or '')
    cleaned = DISPLAY_PATTERN.sub('', text or '').strip()
    raw = '\n'.join(m.group(0) for m in DISPLAY_PATTERN.finditer(text or ''))
    if not raw:
        return cleaned
    current = state()
    command = None
    status = '未下发：未选择 ANKNI、能力关闭或旧回复已失效'
    if current['enabled'] and _permission.get() == current['epoch']:
        try:
            if any(re.sub(r'\s+', '', item).upper() == 'STOP' for item in matches):
                command = 'STOP'
            elif not matches or any(not COMMAND_PATTERN.fullmatch(m.group(0)) for m in DISPLAY_PATTERN.finditer(text or '')):
                raise ValueError('incomplete command')
            else:
                command = [validate_command(item) for item in matches][-1]
        except ValueError:
            status = '未下发：ANKNI 编排格式无效或不完整'
        else:
            if command == 'STOP':
                current = invalidate_permission()
            await manager.broadcast({'type': 'ankni_command', 'data': {
                'command': command, 'epoch': current['epoch'], 'msg_id': str(msg_id), 'event_id': f'ankni:{msg_id}'}})
            status = '已广播至 ANKNI 控制页面；设备执行未确认，离线不补发'
    if conv_id or room_id:
        from svakom_ai import _save_command_notice
        await _save_command_notice(msg_id, raw, command, status, conv_id=conv_id, room_id=room_id, profile='ankni')
    return cleaned
