"""One tool-free ACP session per chat request."""
import asyncio
import base64
import json
import mimetypes
from pathlib import Path

import antigravity_cli as agy
from generation_control import own_process, terminate_process_tree


# AGY 1.5.9 advertises these chat commands even for a tool-free custom agent.
_COMPANION_COMMANDS = {'plan', 'grill-me', 'learn'}


class _Session:
    def __init__(self, process):
        self.process = process
        self.id = None
        self.next_id = 0
        self.commands_checked = False
        self.early_commands = {}

    def check_commands(self, commands):
        if any(command['name'] not in _COMPANION_COMMANDS for command in commands):
            raise agy.AntigravityError('ACP 未加载精简陪伴配置，已停止调用')
        self.commands_checked = True

    async def write(self, fields):
        self.process.stdin.write((json.dumps({'jsonrpc': '2.0', **fields}, ensure_ascii=False) + '\n').encode())
        await self.process.stdin.drain()

    async def send_request(self, method, params):
        self.next_id += 1
        await self.write({'id': self.next_id, 'method': method, 'params': params})
        return self.next_id

    async def read(self):
        line = await self.process.stdout.readline()
        if not line:
            raise agy.AntigravityError('ACP 连接已关闭，请检查 agy 版本和登录状态')
        message = json.loads(line)
        method = message.get('method')
        if method and 'id' in message:
            if method == 'session/request_permission':
                await self.write({'id': message['id'], 'result': {'outcome': {'outcome': 'cancelled'}}})
            else:
                await self.write({'id': message['id'], 'error': {'code': -32601, 'message': 'Unsupported method'}})
            return {}
        if method == 'session/update':
            params = message['params']
            update = params['update']
            kind = update.get('sessionUpdate')
            if self.id is None or params.get('sessionId') != self.id:
                if self.id is None and kind == 'available_commands_update':
                    self.early_commands[params['sessionId']] = update['availableCommands']
                return {}
            if kind == 'available_commands_update':
                self.check_commands(update['availableCommands'])
            elif kind in ('tool_call', 'tool_call_update'):
                raise agy.AntigravityError('ACP 陪伴模式出现原生工具调用，已停止')
            return update
        if method:
            return {}
        if 'error' in message:
            raise agy.AntigravityError(str(message['error'].get('message') or 'ACP 请求失败'))
        return message

    async def request(self, method, params):
        req_id = await self.send_request(method, params)
        while True:
            message = await self.read()
            if message.get('id') == req_id:
                return message['result']

    async def configure(self, workspace, model, has_images):
        init = await self.request('initialize', {'protocolVersion': 1, 'clientCapabilities': {},
                                                 'clientInfo': {'name': 'AionsHome', 'version': '1'}})
        if init.get('protocolVersion') != 1:
            raise agy.AntigravityError('ACP 协议版本不兼容')
        if has_images and not init.get('agentCapabilities', {}).get('promptCapabilities', {}).get('image'):
            raise agy.AntigravityError('当前 ACP 服务不支持图片')
        new = await self.request('session/new', {'cwd': str(workspace), 'mcpServers': []})
        self.id = new['sessionId']
        if self.id in self.early_commands:
            self.check_commands(self.early_commands.pop(self.id))
        family, level = model.rsplit('-', 1)
        options = new['configOptions']
        for key, desired in [('model', family), ('thought_level', level)]:
            if next((o.get('currentValue') for o in options if o['id'] == key), None) != desired:
                response = await self.request('session/set_config_option', {
                    'sessionId': self.id, 'configId': key, 'value': desired})
                options = response['configOptions']
        selected = {o['id']: o.get('currentValue') for o in options}
        if selected.get('model') != family or selected.get('thought_level') != level:
            raise agy.AntigravityError('ACP 未确认所选模型或思考档位')
        while not self.commands_checked:
            await self.read()

    async def reply(self, prompt, image_paths):
        blocks = [{'type': 'text', 'text': prompt}]
        for filename in image_paths:
            path = Path(filename)
            blocks.append({'type': 'image', 'mimeType': mimetypes.guess_type(path.name)[0],
                           'data': base64.b64encode(path.read_bytes()).decode('ascii')})
        req_id = await self.send_request('session/prompt', {'sessionId': self.id, 'prompt': blocks})
        has_text = False
        while True:
            event = await self.read()
            if event.get('id') == req_id:
                result = event['result']
                if result.get('stopReason') != 'end_turn' or not has_text:
                    raise agy.AntigravityError(f"ACP 回复未完成（{result.get('stopReason')}）")
                yield 'usage', result.get('usage') or {}
                return
            kind = event.get('sessionUpdate')
            content = event.get('content') or {}
            text = content.get('text') if content.get('type') == 'text' else None
            if kind in ('agent_message_chunk', 'agent_thought_chunk') and text:
                if kind == 'agent_message_chunk':
                    has_text = True
                    yield 'text', text
                else:
                    yield 'thought', text
            else:
                yield 'activity', None


async def stream(prompt, model, image_paths=(), *, timeout=600):
    binary = agy.find_binary()
    if not binary:
        raise agy.AntigravityError('未找到 agy CLI')
    agy.require_chat_permissions()
    workspace = agy.prepare_chat_workspace().resolve()
    process = own_process(await asyncio.create_subprocess_exec(
        binary, 'acp', '--stdio', '--agent', agy.AGENT_NAME, '--include-default-customizations=false',
        cwd=workspace, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=8 * 1024 * 1024, **agy._process_options()))
    stderr = asyncio.create_task(agy._read_stderr(process.stderr))
    try:
        session = _Session(process)
        async with asyncio.timeout(45):
            await session.configure(workspace, model, bool(image_paths))
        async with asyncio.timeout(timeout):
            async for event in session.reply(prompt, image_paths):
                yield event
    except TimeoutError as exc:
        raise agy.AntigravityError('ACP 握手或回复超时') from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise agy.AntigravityError(f'ACP 响应格式不符合预期（{type(exc).__name__}）') from exc
    finally:
        process.stdin.close()
        await terminate_process_tree(process)
        if not stderr.done():
            stderr.cancel()
        await asyncio.gather(stderr, return_exceptions=True)
