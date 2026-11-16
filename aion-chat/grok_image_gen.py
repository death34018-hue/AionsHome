"""Generate with the official Grok subscription CLI and ordered portrait anchors."""
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

from album import MAX_IMAGE_BYTES, save_generated_image
from generation_control import own_process, terminate_process_tree
from grok_cli import find_binary, chat_environment, _stderr_tail

IMAGE_WORKSPACE = Path(__file__).parent / 'data' / 'grok-images'
IMAGE_MODEL = 'grok/imagine'
REFERENCE_LIMIT = 5
_requests = asyncio.Semaphore(1)


def _read_image(path, grok_home, session):
    path = Path(path).resolve(strict=True)
    sessions = (Path(grok_home) / 'sessions').resolve()
    if (not path.is_relative_to(sessions) or path.parent.name != 'images'
            or path.parent.parent.name != session):
        raise ValueError('Grok 返回的图片不属于本次生成')
    if not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
        raise ValueError('Grok 图片超过相册大小限制或内容为空')
    return path.read_bytes()


def _failure(detail):
    text = str(detail).lower()
    if 'supergrok' in text and ('free' in text or 'upgrade' in text):
        return RuntimeError('当前 Grok 账号没有生图权限，请检查订阅')
    if any(word in text for word in ('429', 'quota', 'usage limit', 'rate limit')):
        return RuntimeError('Grok 生图额度不足或请求过于频繁，请稍后重试')
    if any(word in text for word in ('401', 'unauthorized', 'not authenticated')):
        return RuntimeError('Grok 登录已失效，请重新登录官方 CLI')
    if any(word in text for word in ('moderation', 'content_policy', 'content policy')):
        return RuntimeError('Grok 未通过这次生图的内容审核')
    status = re.search(r'http\s+(\d{3})', text)
    return RuntimeError('Grok 生图失败' + (f'（HTTP {status[1]}）' if status else '，请重试'))


async def _generate_bytes(request, paths, *, workspace=IMAGE_WORKSPACE):
    binary = find_binary()
    if not binary:
        raise RuntimeError('未找到 Grok Build，请先安装并登录官方 CLI')
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    env = chat_environment()
    grok_home = Path(env.get('GROK_HOME') or Path.home() / '.grok')
    tool = 'image_edit' if paths else 'image_gen'
    session = str(uuid.uuid4())
    process = None
    stderr_task = None
    async with _requests:
        with tempfile.TemporaryDirectory(prefix='request-', dir=workspace) as directory:
            prompt_file = Path(directory) / 'prompt.txt'
            prompt_file.write_text(f'调用 {tool} 一次，使用以下参数：\n' +
                                   json.dumps(request, ensure_ascii=False), encoding='utf-8')
            command = [
                binary, '--prompt-file', str(prompt_file), '--verbatim', '--model', 'grok-4.7',
                '--reasoning-effort', 'high', '--output-format', 'streaming-json',
                '--session-id', session, '--system-prompt-override',
                '按给定参数调用一次图片工具。保留完整提示词，以及参考图的路径、顺序和人物对应关系。',
                '--tools', tool, '--allow', tool, '--permission-mode', 'dontAsk',
                '--disallowed-tools', 'search_tool,use_tool,Agent',
                '--disable-web-search', '--no-subagents', '--no-plan', '--max-turns', '2',
            ]
            try:
                async with asyncio.timeout(600):
                    process = own_process(await asyncio.create_subprocess_exec(
                        *command, cwd=str(workspace), env=env, stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        limit=8 * 1024 * 1024,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                    ))
                    stderr_task = asyncio.create_task(_stderr_tail(process.stderr))
                    call_id = None
                    explanation = ''
                    async for line in process.stdout:
                        if not line.strip():
                            continue
                        event = json.loads(line)
                        kind = event.get('type')
                        if kind == 'tool_call':
                            if event.get('toolName') != tool or call_id is not None:
                                raise RuntimeError('Grok 未按本次生图要求调用工具')
                            call_id = event['toolCallId']
                            supplied = (event.get('rawInput') or {}).get('image', [])
                            if paths and (not isinstance(supplied, list) or
                                          [os.path.normcase(str(Path(p).resolve())) for p in supplied] !=
                                          [os.path.normcase(str(Path(p).resolve())) for p in paths]):
                                raise RuntimeError('Grok 未完整传入人物锚点，请重试')
                        elif kind == 'tool_call_update' and event.get('toolCallId') == call_id:
                            if event.get('status') == 'failed':
                                raise _failure(json.dumps(event, ensure_ascii=False))
                            if event.get('status') == 'completed':
                                output = event.get('rawOutput') or {}
                                expected = 'ImageEdit' if paths else 'ImageGen'
                                if output.get('type') != expected or not output.get('path'):
                                    raise _failure(json.dumps(event, ensure_ascii=False))
                                return _read_image(output['path'], grok_home, session)
                        elif kind == 'text':
                            explanation = (explanation + str(event.get('data') or ''))[-2000:]
                        elif kind == 'error':
                            raise _failure(json.dumps(event, ensure_ascii=False))
                        elif kind == 'end':
                            raise _failure(explanation)
                    await process.wait()
                    raise _failure(await stderr_task)
            except TimeoutError as error:
                raise TimeoutError('Grok 生图等待超时，请重试') from error
            finally:
                if process is not None:
                    await terminate_process_tree(process)
                if stderr_task is not None:
                    if not stderr_task.done():
                        stderr_task.cancel()
                    await asyncio.gather(stderr_task, return_exceptions=True)


async def generate_grok_image(prompt, is_selfie=False, source_identity='', *, references=None):
    refs = list(references or [])
    reference_bytes = None
    if is_selfie and not refs:
        from image_gen import _selfie_reference_path
        path = _selfie_reference_path(source_identity)
        reference_bytes = path.read_bytes()
        refs = [{'name': '自拍主角', 'path': path}]
    if len(refs) > REFERENCE_LIMIT:
        raise ValueError(f'Grok 生图最多支持 {REFERENCE_LIMIT} 张人物锚点')
    paths = [str(Path(ref['path']).resolve(strict=True)) for ref in refs]
    labels = [f"参考图 {i} 对应人物：{ref['name']}。保持各自的面部特征。"
              for i, ref in enumerate(refs, 1)]
    if refs:
        labels.append('参考照片仅约束各自外貌；只画场景要求的出场人物，服饰、动作和环境按场景决定。')
    request = {'prompt': '\n'.join([*labels, prompt]), 'aspect_ratio': 'auto' if paths else '1:1'}
    if paths:
        request['image'] = paths
    aspect = re.search(r'(?<!\d)(21:9|16:9|9:16|4:3|3:4|3:2|2:3|1:1|2:1|1:2)(?!\d)', prompt.replace('：', ':'))
    if aspect:
        request['aspect_ratio'] = aspect.group()
    started = time.monotonic()
    print(f'[image_gen] Grok 请求开始，参考图 {len(paths)} 张')
    data = await _generate_bytes(request, paths)
    print(f'[image_gen] Grok 已收到图片（{time.monotonic()-started:.1f}s）')
    return await save_generated_image(data, prompt=prompt, model=IMAGE_MODEL, actor=source_identity,
                                      kind='selfie' if is_selfie else 'draw', reference_bytes=reference_bytes)
