"""Generate through the logged-in Codex CLI and retain named photo references."""
import asyncio
import base64
import io
import json
import shutil
import tempfile
import time
from contextlib import aclosing, asynccontextmanager
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageOps

from ai_providers import (
    _CODEX_SCRIPT, _build_codex_chat_command, _build_codex_chat_environment,
)
from album import save_generated_image
from codex_app_server import stream_codex_app_server

CODEX_IMAGE_AGENT = 'gpt-6.1-sol'
CODEX_IMAGE_MODEL = 'codex/image-generation'
CODEX_IMAGE_TIMEOUT = 600
CODEX_IMAGE_FIRST_ATTEMPT_TIMEOUT = 240
CODEX_IMAGE_QUEUE_TURN = 180
REFERENCE_BYTE_LIMIT = 500_000


class _ImageQueue:
    """One active CLI attempt. A stalled attempt yields sooner if another is waiting."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.waiting = 0
        self.active = None

    def _update_deadline(self):
        if self.active is None:
            return
        timer, start, seconds = self.active
        if not timer.expired():
            duration = min(seconds, CODEX_IMAGE_QUEUE_TURN) if self.waiting else seconds
            timer.reschedule(start + duration)

    @asynccontextmanager
    async def turn(self):
        loop = asyncio.get_running_loop()
        queued = loop.time()
        self.waiting += 1
        try:
            self._update_deadline()
            await self.lock.acquire()
        finally:
            self.waiting -= 1
            self._update_deadline()
        try:
            print(f'[image_gen] Codex 本轮启动，排队 {loop.time() - queued:.1f}s')
            yield
        finally:
            self.lock.release()

    async def read(self, events, workspace, seconds):
        loop = asyncio.get_running_loop()
        start = loop.time()
        async with aclosing(events):
            timer = asyncio.timeout_at(start + seconds)
            try:
                async with timer:
                    self.active = (timer, start, seconds)
                    self._update_deadline()
                    return await _read_generated_image(events, workspace, start)
            except TimeoutError:
                if not timer.expired():
                    raise
                print(f'[image_gen] Codex 本轮 {loop.time() - start:.1f}s 未返回图片')
                return None
            finally:
                self.active = None


@lru_cache(maxsize=1)
def _queue_for_loop(loop):
    return _ImageQueue()


def _compress_reference(path):
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert('RGBA')
    transparent = image.getchannel('A').getextrema()[0] < 255
    if transparent:
        fmt, suffix, options = 'WEBP', '.webp', {}
    else:
        image = image.convert('RGB')
        fmt, suffix, options = 'JPEG', '.jpg', {'subsampling': 0}
    while True:
        output = io.BytesIO()
        image.save(output, fmt, quality=94, **options)
        size = output.tell()
        if size < REFERENCE_BYTE_LIMIT:
            return suffix, output.getvalue()
        scale = 0.95 * (REFERENCE_BYTE_LIMIT / size) ** 0.5
        image = image.resize(tuple(max(1, int(n * scale)) for n in image.size), Image.Resampling.LANCZOS)


async def _prepare_reference_paths(paths, directory):
    prepared = []
    for index, original in enumerate(paths, 1):
        size = Path(original).stat().st_size
        if size < REFERENCE_BYTE_LIMIT:
            prepared.append(original)
            continue
        suffix, data = await asyncio.to_thread(_compress_reference, original)
        copy = Path(directory) / f'reference-{index}{suffix}'
        copy.write_bytes(data)
        prepared.append(str(copy))
        print(f'[image_gen] 参考图 {index}：{size} → {len(data)} 字节')
    return prepared


def image_command(workspace):
    command = _build_codex_chat_command(
        shutil.which('node') or 'node', _CODEX_SCRIPT, workspace, CODEX_IMAGE_AGENT)
    # Reuse the small companion profile, but replace its chat-only instructions.
    filtered = []
    index = 0
    while index < len(command):
        if command[index] == '-c' and command[index + 1].split('=', 1)[0] in {
            'model_instructions_file', 'developer_instructions', 'features.image_generation',
        }:
            index += 2
        else:
            filtered.append(command[index])
            index += 1
    instructions = (
        'You are the AionsHome image generator. Generate exactly one image using the built-in '
        'image generation tool. Treat the supplied local images as ordered visual references. '
        'Preserve the explicit mapping between each reference number and character name. '
        'Never substitute a text description for generating an image. Do not use APIs, shell '
        'commands, or external services. The user has requested and authorized this generation.'
    )
    return filtered[:-2] + [
        '-c', 'features.image_generation=true',
        '-c', 'forced_login_method="chatgpt"',
        '-c', 'project_doc_max_bytes=0',
        '-c', 'model_reasoning_effort="low"',
        '-c', 'developer_instructions=' + json.dumps(instructions),
    ] + filtered[-2:]


async def _read_generated_image(events, workspace, started):
    explanation = ''
    async for event in events:
        if event.kind == 'text_delta':
            explanation = (explanation + event.text)[:1000]
        elif event.kind == 'image_started':
            print(f'[image_gen] Codex 已进入生图阶段（{time.monotonic()-started:.1f}s）')
        elif event.kind == 'image':
            item = event.image or {}
            if item.get('status') != 'completed' or item.get('failure'):
                failure = item.get('failure')
                detail = json.dumps(failure, ensure_ascii=False) if isinstance(failure, dict) else str(failure or item.get('status') or '')
                raise RuntimeError('Codex 生图未完成：' + detail[:500])
            if item.get('result'):
                return base64.b64decode(item['result'], validate=True)
            if item.get('savedPath'):
                path = Path(item['savedPath']).resolve(strict=True)
                if not path.is_relative_to(Path(workspace).resolve() / 'generated_images'):
                    raise ValueError('Codex 返回了非生图目录的文件')
                image_bytes = path.read_bytes()
                if image_bytes:
                    return image_bytes
    raise RuntimeError('Codex 未返回生成图片' + ('：' + explanation if explanation else ''))


async def generate_codex_image(prompt, is_selfie=False, source_identity='', *, references=None):
    if not _CODEX_SCRIPT:
        raise RuntimeError('未安装 Codex CLI')
    refs = list(references or [])
    reference_bytes = None
    if is_selfie and not references:
        from image_gen import _selfie_reference_path
        path = _selfie_reference_path(source_identity)
        # A missing anchor must not silently turn a requested portrait into a new face.
        reference_bytes = path.read_bytes()
        refs.append({'name': '自拍主角', 'path': str(path)})
    paths = []
    labels = []
    for index, ref in enumerate(refs, 1):
        path = Path(ref['path']).resolve(strict=True)
        paths.append(str(path))
        labels.append(f"参考图 {index} 对应人物：{ref['name']}。保留此人的面部特征，不与其他角色混淆。")
    request = '\n'.join([
        '请使用内置生图工具生成一张图片。',
        *labels,
        '参考照片仅约束各自人物的外貌；服饰、场景、动作按下方要求。只画场景要求的出场人物。',
        prompt,
    ])
    started = time.monotonic()
    print(f'[image_gen] Codex 请求开始，参考图 {len(paths)} 张')
    env = _build_codex_chat_environment()
    # Never inherit an API key into this subscription-only route.
    env.pop('OPENAI_API_KEY', None)
    env.pop('CODEX_API_KEY', None)
    workspace = str(Path(env['CODEX_HOME']))
    queue = _queue_for_loop(asyncio.get_running_loop())
    with tempfile.TemporaryDirectory(prefix='aionshome-image-refs-') as directory:
        first = min(CODEX_IMAGE_FIRST_ATTEMPT_TIMEOUT, CODEX_IMAGE_TIMEOUT)
        for attempt, seconds in enumerate((first, CODEX_IMAGE_TIMEOUT - first), 1):
            if seconds <= 0:
                continue
            async with queue.turn():
                if attempt == 1:
                    paths = await _prepare_reference_paths(paths, directory)
                events = stream_codex_app_server(
                    image_command(workspace), env=env, cwd=workspace, model=CODEX_IMAGE_AGENT,
                    prompt=request, image_paths=paths, reasoning_summary='none', image_results=True,
                )
                image_bytes = await queue.read(events, workspace, seconds)
            if image_bytes is not None:
                break
        else:
            raise TimeoutError('Codex 生图等待超时')
    print(f'[image_gen] Codex 已收到图片（{time.monotonic()-started:.1f}s）')
    return await save_generated_image(
        image_bytes, prompt=prompt, model=CODEX_IMAGE_MODEL, actor=source_identity,
        kind='selfie' if is_selfie else 'draw', reference_bytes=reference_bytes,
    )
