"""Official agy subprocess transport and a minimal companion profile."""

import asyncio
import io
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from antigravity_image_hook import is_allowed_attachment_read


AGENT_NAME = 'home-companion'
VISION_AGENT_NAME = 'home-companion-vision'
CHAT_WORKSPACE = Path(__file__).parent / 'data' / 'antigravity-login'
PROFILE_SOURCE = Path(__file__).parent / 'prompts' / 'antigravity_companion.md'
VISION_PROFILE_SOURCE = PROFILE_SOURCE.with_name('antigravity_companion_vision.md')
CLI_SETTINGS = Path.home() / '.gemini' / 'antigravity-cli' / 'settings.json'
CHAT_DENIALS = ('write_file(*)', 'command(*)', 'unsandboxed(*)', 'mcp(*)', 'execute_url(*)')
_requests = asyncio.Semaphore(1)
_catalog_lock = asyncio.Lock()
_catalog = []
_catalog_time = 0.0
_ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


class AntigravityError(RuntimeError):
    pass


def require_chat_permissions(settings_path=CLI_SETTINGS):
    """Refuse to launch unless agy's official policy forbids modifying host files."""
    try:
        settings = json.loads(Path(settings_path).read_text(encoding='utf-8-sig'))
        permissions = settings.get('permissions') if isinstance(settings, dict) else None
        denied = permissions.get('deny', []) if isinstance(permissions, dict) else []
        # agy omits request-review when saving because it is the default preset.
        if (settings.get('toolPermission') or 'request-review') in {'strict', 'request-review'} and isinstance(denied, list) and all(
            rule in denied for rule in CHAT_DENIALS
        ):
            return
    except (OSError, ValueError, AttributeError):
        pass
    raise AntigravityError('AGY 禁写/禁执行权限配置缺失，已停止调用；请恢复官方 CLI settings.json 中的审查权限与拒绝规则')


def find_binary():
    binary = shutil.which('agy') or shutil.which('agy.exe')
    if binary:
        return binary
    local = os.environ.get('LOCALAPPDATA')
    candidate = Path(local) / 'agy/bin/agy.exe' if local else None
    return str(candidate) if candidate and candidate.is_file() else None


def prepare_chat_workspace(workspace=CHAT_WORKSPACE, *, vision=False):
    workspace = Path(workspace)
    source = VISION_PROFILE_SOURCE if vision else PROFILE_SOURCE
    agent = VISION_AGENT_NAME if vision else AGENT_NAME
    text = source.read_text(encoding='utf-8')
    if vision:
        hook_source = (workspace / '.agents/hooks.json').resolve().as_posix().replace("'", "''")
        text = text.replace('hooks: [home-image-reader]', "hooks: ['" + hook_source + "']")
    target = workspace / '.agents' / 'agents' / f'{agent}.md'
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file() or target.read_text(encoding='utf-8') != text:
        target.write_text(text, encoding='utf-8')
    if vision:
        hook_path = workspace / '.agents' / 'hooks.json'
        command_parts = [sys.executable, str(Path(__file__).with_name('antigravity_image_hook.py').resolve()),
                         str((workspace / '.agents/image-input.json').resolve())]
        if os.name == 'nt':
            command = subprocess.list2cmdline(command_parts)
        else:
            command = shlex.join(command_parts)
        hooks = json.loads(hook_path.read_text(encoding='utf-8')) if hook_path.exists() else {}
        hooks['home-image-reader'] = {'PreToolUse': [{'matcher': '*', 'hooks': [
            {'type': 'command', 'command': command, 'timeout': 10},
        ]}]}
        hook_path.write_text(json.dumps(hooks, ensure_ascii=False, indent=2), encoding='utf-8')
    return workspace


def build_chat_command(binary, model, timeout='10m', *, agent=AGENT_NAME):
    command = [binary, '--agent', agent,
               '--input-format', 'stream-json', '--output-format', 'stream-json',
               '--disable-slash-commands', '--print-timeout', timeout]
    if model:
        command += ['--model', model]
    return command


def _timeout_seconds(value):
    match = re.fullmatch(r'(\d+)(ms|s|m|h)?', value)
    if not match:
        return 600
    return max(1, int(match[1]) * {'ms': .001, 's': 1, 'm': 60, 'h': 3600, None: 1}[match[2]])


def _process_options():
    return {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


def parse_model_catalog(text):
    """Read model slugs from the official catalog, without inventing model IDs."""
    text = _ANSI.sub('', text)
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    result = []
    if isinstance(data, (dict, list)):
        rows = data.get('models', []) if isinstance(data, dict) else data
        for row in rows:
            if not isinstance(row, dict) or row.get('available') is False:
                continue
            slug = row.get('slug') or row.get('id') or row.get('model')
            if isinstance(slug, str) and slug.strip():
                result.append({'model': slug.strip(), 'name': row.get('name') or row.get('label') or row.get('display_name') or slug})
    else:
        # agy models prints a human-readable slug/name table.
        for line in text.splitlines():
            line = line.strip().strip('│|').strip()
            match = re.match(r'^([a-z][a-z0-9._/-]*-[a-z0-9._/-]+)\s+(.+)$', line)
            if match:
                result.append({'model': match[1], 'name': match[2].strip().strip('│|').strip()})
                continue
            match = re.match(r'^(.+?)\s{2,}([a-z][a-z0-9._/-]*-[a-z0-9._/-]+)\s*$', line)
            if match:
                result.append({'model': match[2], 'name': match[1].strip().strip('│|').strip()})
    return list({row['model']: row for row in result}.values())


async def discover_models(force=False):
    global _catalog, _catalog_time
    async with _catalog_lock:
        if not force and time.monotonic() - _catalog_time < 300:
            return list(_catalog)
        binary = find_binary()
        if not binary:
            return []
        process = await asyncio.create_subprocess_exec(
            binary, 'models', stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **_process_options(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
            if process.returncode:
                raise AntigravityError(stderr.decode('utf-8', errors='replace')[-1000:] or '无法读取 AGY 模型列表')
            rows = parse_model_catalog(stdout.decode('utf-8', errors='replace'))
            if not rows:
                raise AntigravityError('AGY 未返回可识别的模型列表，请先完成官方 CLI 登录')
            _catalog, _catalog_time = rows, time.monotonic()
            return list(rows)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


async def _read_stderr(reader):
    tail = b''
    while chunk := await reader.read(4096):
        tail = (tail + chunk)[-4096:]
    return tail.decode('utf-8', errors='replace').strip()


def _stage_image(source, target):
    """Decode attachments before sending; salvage JPEGs missing their end marker."""
    from PIL import Image

    data = source.read_bytes()
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format == 'JPEG' and not data.endswith(b'\xff\xd9'):
                # Some decoders accept a missing EOI while Gemini rejects it.
                # Re-encode only the staged copy; missing pixels cannot be restored.
                with Image.open(io.BytesIO(data + b'\xff\xd9')) as recovered:
                    recovered.load()
                    target = target.with_suffix('.png')
                    if recovered.mode == 'CMYK':
                        recovered = recovered.convert('RGB')
                    recovered.save(target, format='PNG')
                    return target
            image.load()  # JPEG verify() only checks headers, not the pixel stream.
    except OSError as error:
        raise AntigravityError('图片文件无法完整解码，请重新上传图片') from error
    target.write_bytes(data)
    return target


async def stream_chat(binary, prompt, model, workspace=CHAT_WORKSPACE, timeout='10m', *, image_paths=(), audio_paths=()):
    """Stage attachments in the sole read-allowed directory for this turn."""
    from config import antigravity_model_supports_audio

    audio_paths = audio_paths if antigravity_model_supports_audio(model) else ()
    workspace = Path(workspace)
    images_directory = workspace / 'images'
    require_chat_permissions()
    prepare_chat_workspace(workspace, vision=bool(image_paths or audio_paths))
    if not image_paths and not audio_paths:
        async for event in _stream_chat(binary, prompt, model, workspace, timeout):
            yield event
        return
    images_directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='turn-', dir=images_directory) as directory:
        staged = []
        for index, path in enumerate(image_paths, 1):
            source = Path(path)
            target = Path(directory) / f'image-{index}{source.suffix.lower()}'
            target = _stage_image(source, target)
            staged.append(str(target.resolve()))
        attachment_list = '\n'.join(f'图片{index}：{path}' for index, path in enumerate(staged, 1))
        for index, path in enumerate(audio_paths, 1):
            source = Path(path)
            target = Path(directory) / f'audio-{index}{source.suffix.lower()}'
            shutil.copyfile(source, target)
            staged.append(str(target.resolve()))
            attachment_list += f'\n音频{index}：{target.resolve()}'
        prompt = ('[System Instruction]\nThe user supplied these image/audio attachments. '
                  'Use read_file (view_file on older CLI versions) to inspect these attachments before answering; '
                  'do not read other files. '
                  'Listen to the original audio, including tone and non-speech sounds; transcription is only a reference. '
                  'Do not include internal file paths or file links in your reply.\n'
                  + attachment_list + '\n\n' + prompt)
        async for event in _stream_chat(binary, prompt, model, workspace, timeout,
                                       agent=VISION_AGENT_NAME, attachment_paths=tuple(staged)):
            yield event


async def _stream_chat(binary, prompt, model, workspace, timeout, *, agent=AGENT_NAME, attachment_paths=()):
    """Yield visible deltas and usage; never recover replies from private databases."""
    from generation_control import own_process, terminate_process_tree

    async with _requests:
        image_manifest = Path(workspace) / '.agents/image-input.json'
        process = None
        stderr_task = None
        visible = ''
        final = None
        initialized = False
        try:
            if attachment_paths:
                image_manifest.write_text(json.dumps(attachment_paths, ensure_ascii=False), encoding='utf-8')
            from system_logs import record_cli_prompt
            await record_cli_prompt(prompt, instructions_file=Path(workspace) / '.agents' / 'agents' / f'{agent}.md')
            process = own_process(await asyncio.create_subprocess_exec(
                *build_chat_command(binary, model, timeout, agent=agent), cwd=str(workspace),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=8 * 1024 * 1024,
                env={**os.environ, 'NO_COLOR': '1'}, **_process_options(),
            ))
            stderr_task = asyncio.create_task(_read_stderr(process.stderr))
            async with asyncio.timeout(_timeout_seconds(timeout) + 30):
                payload = {'event': 'user', 'message': {'content': prompt}}
                process.stdin.write((json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8'))
                await process.stdin.drain()
                process.stdin.close()
                await process.stdin.wait_closed()
                async for line in process.stdout:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError as exc:
                        raise AntigravityError('AGY 输出不是官方 stream-json 格式，请检查版本和登录状态') from exc
                    kind = event.get('event')
                    if kind == 'init':
                        config = event.get('init', {})
                        if config.get('agent') != agent:
                            raise AntigravityError('AGY 陪伴配置未生效，已停止本次调用')
                        initialized = True
                        # 1.2.16 advertises the global tool registry here, even with
                        # excludeDefaultComponents enabled; it is not the agent's toolset.
                        yield {'kind': 'activity'}
                    elif kind == 'step_update':
                        step = event.get('step_update', {})
                        if step.get('subagent_info'):
                            raise AntigravityError('AGY 陪伴模式出现原生工具调用，已停止：' + step.get('tool_name', 'unknown'))
                        if step.get('step_type') == 'tool' or step.get('tool_info'):
                            info = step.get('tool_info') or {}
                            name = step.get('tool_name') or info.get('name')
                            if not is_allowed_attachment_read(name, info.get('parameters'), attachment_paths):
                                raise AntigravityError('AGY 陪伴模式出现未授权的原生工具调用，已停止：' + (name or 'unknown'))
                            if info.get('error'):
                                raise AntigravityError('AGY 附件读取失败：' + str(info['error'].get('message', info['error'])))
                            yield {'kind': 'activity'}
                            continue
                        if step.get('step_type') == 'agent_response' and initialized:
                            text = step.get('text_delta') or ''
                            if text:
                                visible += text
                                yield {'kind': 'text', 'text': text}
                            else:
                                yield {'kind': 'activity'}
                        else:
                            yield {'kind': 'activity'}
                    elif kind == 'result':
                        final = event.get('result', {})
                        if final.get('status') != 'SUCCESS':
                            raise AntigravityError(final.get('error') or f"AGY 调用结束：{final.get('status', 'UNKNOWN')}")
                        if not initialized:
                            raise AntigravityError('AGY 缺少初始化事件，无法确认陪伴模式')
                        response = final.get('response') or ''
                        if response.startswith(visible):
                            tail = response[len(visible):]
                            if tail:
                                visible += tail
                                yield {'kind': 'text', 'text': tail}
                        elif response != visible:
                            raise AntigravityError('AGY 流式正文与最终回复不一致')
                        if not visible:
                            raise AntigravityError('AGY 未收到完整回复，请检查登录、额度和网络')
                        # The official result completes this turn. CLI shutdown and
                        # pipe EOF may lag behind; cleanup below owns the process.
                        yield {'kind': 'completed', 'usage': final.get('usage', {}),
                               'conversation_id': final.get('conversation_id', '')}
                        return
                await process.wait()
                stderr = await stderr_task
                if process.returncode or final is None or not visible:
                    raise AntigravityError(stderr or 'AGY 未收到完整回复，请检查登录、额度和网络')
        except TimeoutError as exc:
            raise AntigravityError('AGY 回复超时，请稍后重试') from exc
        finally:
            try:
                if process is not None:
                    await terminate_process_tree(process)
            finally:
                if attachment_paths:
                    image_manifest.unlink(missing_ok=True)
                if stderr_task is not None:
                    if not stderr_task.done():
                        stderr_task.cancel()
                    await asyncio.gather(stderr_task, return_exceptions=True)
