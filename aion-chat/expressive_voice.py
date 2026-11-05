"""Optional directed speech, invoked only for a saved companion message."""
import asyncio
import json
import logging
import re
import shutil
import time

from config import EXPRESSIVE_VOICE_DIR, TTS_CACHE_DIR, get_key
from expressive_voice_markup import BRACKETS, VOICE_SPAN, strip_voice_blocks
from database import get_db
from generation_control import current_generation, spawn_generation_task
import elevenlabs_tts

log = logging.getLogger(__name__)
_tasks = {}


def archive_legacy_audio():
    """Preserve existing recordings; keep old copies usable by a running server."""
    copied = 0
    for source in TTS_CACHE_DIR.glob('*_expressive.mp3'):
        target = EXPRESSIVE_VOICE_DIR / source.name
        if target.exists():
            continue
        try:
            shutil.copy2(source, target)
            copied += 1
        except OSError:
            log.exception('Could not archive expressive voice: %s', source.name)
    return copied

LIMIT = 500
PROMPT = (
    '想亲口说时，用 [语音]…[/语音]，台词优先用英语，正文不重复。在 [] 里自由写英文指导，可在同一条里随台词换。'
    '[breathy] 修饰接下来的说话声。轻笑 [chuckles] 或亲近时的 [soft moan] 单独放在实际出声处，可接 Mm… 再说。'
    f'自然说可不加标签。每条语音的指导和台词合计尽量控制在 300 字符以内，最多 {LIMIT} 字符。'
)


def enabled():
    from capabilities import is_capability_enabled
    return is_capability_enabled('expressive_voice')


def extract(text):
    clips = []
    for match in VOICE_SPAN.finditer(text):
        if match['close']:
            script = match['script'].strip().translate(BRACKETS)
        elif match['legacy_close']:
            # Old models/history may still use direction=...|content=... .
            fields = re.fullmatch(r'\s*方向\s*=\s*[^|｜]+[|｜]\s*内容\s*=\s*(.+)', match['legacy'], re.S)
            if not fields:
                continue
            script = fields[1].strip()
        else:
            continue
        spoken = re.sub(r'\[[^\[\]]*\]', '', script).strip()
        clip = {'text': spoken, 'source_command': match[0], 'synthesis_text': script}
        if len(script) > LIMIT:
            clip['error'] = f'表演指导和台词超过 {LIMIT} 字符，已跳过合成'
        elif not spoken or '[' in spoken or ']' in spoken:
            clip['error'] = '缺少台词或表演指导括号未配对，已跳过合成'
        clips.append(clip)
        break
    return strip_voice_blocks(text).strip(), clips


def _identity(event):
    data = event.get('data') or {}
    if event.get('type') == 'msg_created' and data.get('role') == 'assistant':
        return 'messages', 'aion'
    if event.get('type') == 'chatroom_msg_created' and data.get('sender') in ('aion', 'connor'):
        return 'chatroom_messages', data['sender']
    return None


def _attachments(raw):
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw or '[]')
    except (ValueError, TypeError):
        return []


def transcript_for_context(attachments):
    """Restore speech directives so history shows the same format the AI emits."""
    commands = []
    for attachment in _attachments(attachments):
        if not isinstance(attachment, dict) or attachment.get('type') != 'expressive_voice':
            continue
        command = attachment.get('source_command')
        if not command:
            direction = attachment.get('direction') or '自然'
            text = attachment.get('text') or ''
            command = f'[语音|方向={direction}|内容={text}]'
        commands.append(command)
    return '\n'.join(commands)


async def prepare_message(event):
    """Runs before message sync/push, after DB insert. Mutates the shared message."""
    identity = _identity(event)
    if not identity:
        return
    data = event['data']
    text = str(data.get('content') or '')
    if not VOICE_SPAN.search(text):
        return
    clean, clips = extract(text)
    table, sender = identity
    async with get_db() as db:
        cur = await db.execute(f'SELECT content, attachments FROM {table} WHERE id=?', (data['id'],))
        row = await cur.fetchone()
        if not row:
            return
        attachments = _attachments(row[1])
        if any(a.get('type') == 'expressive_voice' for a in attachments if isinstance(a, dict)):
            data['content'], data['attachments'] = row[0], attachments
            return
        if clips:
            clip = clips[0]
            ready = enabled() and bool(get_key('elevenlabs')) and bool(elevenlabs_tts.voice_for(sender))
            status = 'pending' if ready else 'unavailable'
            if clip.get('error'):
                status = 'failed'
            attachments.append({'type': 'expressive_voice', 'msg_id': data['id'], 'status': status,
                'error': '' if ready else '主动语音未开启，或尚未配置该角色的 ElevenLabs 音色', **clip})
        data['content'], data['attachments'] = clean, attachments
        await db.execute(f'UPDATE {table} SET content=?, attachments=? WHERE id=?',
                         (clean, json.dumps(attachments, ensure_ascii=False), data['id']))
        await db.commit()


def start_message(event, manager, *, autoplay=False):
    identity = _identity(event)
    if not identity:
        return
    data = event['data']
    if not any(isinstance(a, dict) and a.get('type') == 'expressive_voice' and a.get('status') == 'pending'
               for a in _attachments(data.get('attachments'))):
        return
    msg_id = data['id']
    if msg_id in _tasks:
        return
    scope = current_generation()
    # Scheduled wakeups opt in explicitly; other passive publications stay manual.
    autoplay = bool(autoplay or (scope and getattr(scope, 'expressive_autoplay', False)))
    task = spawn_generation_task(_produce(identity, dict(data), manager, autoplay))
    _tasks[msg_id] = task
    task.add_done_callback(lambda _task: _tasks.pop(msg_id, None))


async def _produce(identity, data, manager, autoplay):
    table, sender = identity
    msg_id = data['id']
    path = None
    try:
        # Claim persistently: duplicate events and reconnects must never re-bill.
        async with get_db() as db:
            await db.execute('BEGIN IMMEDIATE')
            cur = await db.execute(f'SELECT attachments FROM {table} WHERE id=?', (msg_id,))
            row = await cur.fetchone()
            attachments = _attachments(row[0]) if row else []
            clip = next((a for a in attachments if isinstance(a, dict) and a.get('type') == 'expressive_voice'), None)
            if not clip or clip.get('status') != 'pending':
                return
            clip['status'] = 'working'
            await db.execute(f'UPDATE {table} SET attachments=? WHERE id=?', (json.dumps(attachments, ensure_ascii=False), msg_id))
            await db.commit()
        if not enabled():
            raise ValueError('主动语音已关闭')
        # A pending attachment created before the upgrade only has plain speech.
        directed = clip.get('synthesis_text') or clip['text']
        clip['synthesis_text'] = directed
        clip['voice_id'] = elevenlabs_tts.voice_for(sender)
        clip['tts_model'] = elevenlabs_tts.SETTINGS.get('elevenlabs_tts_model') or 'eleven_v4'
        audio = await elevenlabs_tts.request_audio(directed, clip['voice_id'])
        safe_id = re.sub(r'[^A-Za-z0-9_-]', '', msg_id) + '_expressive'
        scope = current_generation()
        if scope:
            scope.message_ids.add(safe_id)
        path = EXPRESSIVE_VOICE_DIR / f'{safe_id}.mp3'
        path.write_bytes(audio)
        clip.update(status='ready', url=f'/api/tts/audio/{safe_id}', error='')
        if not await _update_clip(table, data, clip, manager):
            path.unlink(missing_ok=True)
            return
        if autoplay and enabled():
            from tts import wait_message_tts
            await wait_message_tts(msg_id)
            if not enabled():
                return
            payload = {'msg_id': safe_id, 'parent_msg_id': msg_id, 'expressive': True,
                       'seq': 0, 'url': clip['url'], 'text': clip['text'], 'created_at': time.time()}
            await manager.send_tts_event({'type': 'tts_chunk', 'data': payload})
            await manager.send_tts_event({'type': 'tts_done', 'data': payload})
    except asyncio.CancelledError:
        if 'clip' in locals() and clip and clip.get('status') == 'ready':
            # Cancelling auto-play must not destroy the committed replay attachment.
            raise
        if path:
            path.unlink(missing_ok=True)
        if 'clip' in locals() and clip:
            clip.update(status='cancelled', error='本次语音已取消，未自动重试。')
            await _update_clip(table, data, clip, manager, notify=False)
        raise
    except Exception as exc:
        log.warning('Expressive voice skipped: %s', type(exc).__name__)
        if 'clip' in locals() and clip and clip.get('status') != 'ready':
            clip.update(status='failed', error=str(exc) if isinstance(exc, ValueError) else '语音未完成，请检查配置、网络和官方用量；未自动重试。')
            await _update_clip(table, data, clip, manager)


async def _update_clip(table, data, clip, manager, *, notify=True):
    async with get_db() as db:
        cur = await db.execute(f'SELECT content, attachments FROM {table} WHERE id=?', (data['id'],))
        row = await cur.fetchone()
        if not row:
            return False
        attachments = [a for a in _attachments(row[1]) if not isinstance(a, dict) or a.get('type') != 'expressive_voice']
        attachments.append(clip)
        await db.execute(f'UPDATE {table} SET attachments=? WHERE id=?', (json.dumps(attachments, ensure_ascii=False), data['id']))
        await db.commit()
    data.update(content=row[0], attachments=attachments)
    if notify:
        await manager.broadcast({'type': 'msg_updated' if table == 'messages' else 'chatroom_msg_updated', 'data': data})
    return True
