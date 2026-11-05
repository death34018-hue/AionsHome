"""Generate a single image through the user's authenticated FlowWall MCP."""
import asyncio
import json
import mimetypes
import re
import shutil
import subprocess
import tempfile
import tomllib
from contextlib import asynccontextmanager
from itertools import count
from pathlib import Path

import httpx

from album import MAX_IMAGE_BYTES, save_generated_image
from codex_app_server import _close_process, _request, _write_json
from codex_image_gen import _prepare_reference_paths


FLARE = 'gpt_image2_5_flare'
REFERENCE_LIMITS = {FLARE: 16, 'seedream_5_0_flash': 10, 'grok_imagine_image_2_0': 5}


@asynccontextmanager
async def _flow_session():
    # The companion chat profile deliberately omits MCPs. Use the user's registered
    # FlowWall connection here, with an ephemeral thread and no model turn.
    from ai_providers import _CODEX_HOME, _CODEX_SCRIPT
    node = shutil.which('node')
    if node and _CODEX_SCRIPT:
        command = [node, _CODEX_SCRIPT]
    elif binary := shutil.which('codex.exe') or shutil.which('codex'):
        command = [binary]
    else:
        raise ValueError('Flow AI 需要本机已登录 flowwall MCP 的 Codex CLI')
    overrides = ['-c', 'features.apps=false', '-c', 'features.plugins=false']
    config_file = Path(_CODEX_HOME) / 'config.toml'
    if config_file.is_file():
        with config_file.open('rb') as config:
            servers = tomllib.load(config).get('mcp_servers', {})
        for name in servers:
            if name != 'flowwall' and re.fullmatch(r'[\w-]+', name):
                overrides.extend(['-c', f'mcp_servers.{name}.enabled=false'])
    process = await asyncio.create_subprocess_exec(
        *command, *overrides, 'app-server', '--stdio', stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), limit=16 * 1024 * 1024)
    sequence = count(1)

    async def rpc(method, params):
        async with asyncio.timeout(60):
            return await _request(process, next(sequence), method, params, [])

    try:
        await rpc('initialize', {'clientInfo': {'name': 'aionshome_flowai', 'version': '1'},
                                 'capabilities': {'experimentalApi': True}})
        await _write_json(process.stdin, {'method': 'initialized'})
        thread = await rpc('thread/start', {'ephemeral': True, 'approvalPolicy': 'never', 'sandbox': 'read-only'})
        thread_id = thread['thread']['id']

        async def call(tool, arguments):
            result = await rpc('mcpServer/tool/call', {'threadId': thread_id, 'server': 'flowwall',
                                                       'tool': tool, 'arguments': arguments})
            if result.get('isError'):
                detail = ' '.join(str(c.get('text', '')) for c in result.get('content', [])).lower()
                if any(word in detail for word in ('unauthorized', 'not authenticated', '登录', 'oauth')):
                    raise ValueError('请在本机 Codex 重新登录 Flow AI（flowwall）')
                if 'insufficient' in detail or '余额不足' in detail:
                    raise ValueError('Flow AI 积分不足')
                raise ValueError(f'Flow AI 的 {tool} 请求失败')
            data = result.get('structuredContent')
            if data is None:
                data = json.loads(next(c['text'] for c in result['content'] if c.get('type') == 'text'))
            if not isinstance(data, dict):
                raise ValueError('Flow AI 返回了无法识别的结果')
            return data

        yield call
    finally:
        await _close_process(process, interrupt_turn=None)


async def _upload_references(call, client, paths, model):
    files = []
    for index, path in enumerate(paths, 1):
        files.append({'filename': f'anchor-{index}{path.suffix.lower()}',
                      'content_type': mimetypes.guess_type(path.name)[0] or 'image/jpeg'})
    slots = (await call('request_asset_upload', {'files': files}))['uploads']
    confirmation = []
    for path, file, slot in zip(paths, files, slots, strict=True):
        url = httpx.URL(slot['upload_url'])
        if url.scheme != 'https' or url.host != 'www.flowwall.ai' or slot.get('method') != 'POST':
            raise ValueError('Flow AI 返回了无法使用的上传地址')
        with path.open('rb') as source:
            response = await client.post(url, files={slot['form_field']: (file['filename'], source, file['content_type'])})
        response.raise_for_status()
        confirmation.append({'staging_key': slot['staging_key'], 'filename': file['filename']})
    confirmed = await call('confirm_asset_upload', {'items': confirmation, 'for_model': model})
    urls = [asset['url'] for asset in confirmed['assets']]
    if len(urls) != len(paths) or not all(urls):
        raise ValueError('Flow AI 未收到全部人物锚点')
    return urls


async def _download_image(client, url):
    location = httpx.URL(url)
    if (location.scheme != 'https' or location.userinfo or location.port not in (None, 443)
            or not any(location.host == domain or location.host.endswith('.' + domain)
                       for domain in ('flowwall.ai', 'vlinkcloud.cn'))):
        raise ValueError('Flow AI 返回了无效的图片地址')
    data = bytearray()
    async with client.stream('GET', url) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError('Flow AI 图片超过相册大小限制')
    return bytes(data)


async def generate_flowai_image(prompt, is_selfie, source_identity, *, model, references=None,
                                flow_job_id=None, on_flow_job=None):
    limit = REFERENCE_LIMITS.get(model)
    if limit is None and not (flow_job_id and model == 'jimeng_imgref_v4.5'):
        raise ValueError('请选择支持的 Flow AI 生图模型')
    refs = list(references or [])
    reference_bytes = None
    if is_selfie and not refs:
        from image_gen import _selfie_reference_path
        path = _selfie_reference_path(source_identity)
        reference_bytes = path.read_bytes()
        refs = [{'name': '自拍主角', 'path': path}]
    if not flow_job_id and len(refs) > limit:
        raise ValueError(f'当前 Flow AI 模型最多支持 {limit} 张人物锚点')
    labels = [f"参考图 {i} 对应人物：{r['name']}。保持各自的面部特征。" for i, r in enumerate(refs, 1)]
    aspect = re.search(r'(?<!\d)(21:9|16:9|3:2|4:3|1:1|3:4|2:3|9:16)(?!\d)', prompt.replace('：', ':'))
    try:
        async with asyncio.timeout(600), _flow_session() as call, httpx.AsyncClient(timeout=60) as client:
            if not flow_job_id:
                request = {'model': model, 'prompt': '\n'.join([*labels, prompt]), 'resolution': '1K'}
                if aspect:
                    request['aspect'] = aspect.group()
                if refs:
                    with tempfile.TemporaryDirectory(prefix='aionshome-flowai-') as tmp:
                        paths = await _prepare_reference_paths([r['path'] for r in refs], tmp)
                        request['reference_image_urls'] = await _upload_references(call, client, list(map(Path, paths)), model)
                submitted = await call('generate_image', request)
                flow_job_id = submitted.get('generation_id')
                if not flow_job_id:
                    raise ValueError('Flow AI 未返回生成任务号')
                if on_flow_job:
                    await on_flow_job(flow_job_id)
            while True:
                state = await call('get_generation_status', {'generation_id': flow_job_id})
                status = state.get('status')
                if status == 'completed':
                    urls = state.get('urls') or []
                    if not urls:
                        raise ValueError('Flow AI 已完成任务，但未返回图片')
                    data = await _download_image(client, urls[0])
                    break
                if status in ('failed', 'cancelled', 'canceled', 'rejected', 'error'):
                    if on_flow_job:
                        await on_flow_job(None)
                    raise ValueError('Flow AI 生图任务失败，可重新生成')
                await asyncio.sleep(4)
    except (TimeoutError, ValueError):
        raise
    except httpx.TimeoutException:
        raise TimeoutError('Flow AI 文件传输超时') from None
    except httpx.HTTPStatusError as exc:
        raise ValueError(f'Flow AI 文件传输失败（HTTP {exc.response.status_code}）') from None
    except Exception as exc:
        # Transport exceptions can include signed URLs. Keep them out of the chat and logs.
        raise ValueError(f'Flow AI 连接或响应异常（{type(exc).__name__}），请检查本机 flowwall MCP 登录状态') from None
    return await save_generated_image(data, prompt=prompt, model=f'flowai/{model}', actor=source_identity,
                                      kind='selfie' if is_selfie else 'draw', reference_bytes=reference_bytes)
