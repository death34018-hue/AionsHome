"""Text chat through the official Grok Build CLI and its subscription login."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from generation_control import own_process, terminate_process_tree

CHAT_WORKSPACE = Path(__file__).parent / 'data' / 'grok-chat'
CHAT_INSTRUCTIONS = '根据输入中的角色设定、记忆和对话继续回复，直接输出面向用户的正文。'
_requests = asyncio.Semaphore(2)


class GrokError(RuntimeError):
    pass


def find_binary():
    binary = shutil.which('grok.exe' if os.name == 'nt' else 'grok')
    if binary:
        return binary
    local = Path.home() / '.grok' / 'bin' / ('grok.exe' if os.name == 'nt' else 'grok')
    return str(local) if local.is_file() else None


def chat_environment():
    env = dict(os.environ)
    env.pop('XAI_API_KEY', None)
    env.update(NO_COLOR='1', GROK_DISABLE_AUTOUPDATER='1', GROK_MEMORY='0',
               GROK_SUBAGENTS='0', GROK_GOAL_ENABLED='0', GROK_MANAGED_MCPS_ENABLED='0',
               GROK_MANAGED_MCP_GATEWAY_TOOLS_ENABLED='0')
    for source in ('CLAUDE', 'CURSOR'):
        for kind in ('SKILLS', 'RULES', 'AGENTS', 'MCPS', 'HOOKS'):
            env[f'GROK_{source}_{kind}_ENABLED'] = '0'
    return env


def build_chat_command(binary, prompt_path, model, reasoning_effort):
    return [
        binary, '--prompt-file', str(prompt_path), '--verbatim', '--model', model,
        '--reasoning-effort', reasoning_effort, '--output-format', 'streaming-json',
        '--system-prompt-override', CHAT_INSTRUCTIONS,
        '--tools', 'none', '--disallowed-tools', 'search_tool,use_tool,Agent',
        '--deny', 'Bash', '--deny', 'Edit', '--deny', 'Write', '--deny', 'Read',
        '--deny', 'Grep', '--deny', 'WebFetch', '--deny', 'MCPTool',
        '--disable-web-search', '--no-subagents', '--no-plan', '--max-turns', '1',
    ]


async def _stderr_tail(stream):
    tail = b''
    while chunk := await stream.read(4096):
        tail = (tail + chunk)[-4096:]
    return tail.decode('utf-8', errors='replace').strip()


async def stream_chat(binary, prompt, model, *, reasoning_effort='high',
                      workspace=CHAT_WORKSPACE, timeout=600):
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    async with _requests:
        prompt_path = None
        process = None
        stderr_task = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='prompt-',
                                             suffix='.txt', dir=workspace, delete=False) as file:
                prompt_path = Path(file.name)
                file.write(prompt)
            process = own_process(await asyncio.create_subprocess_exec(
                *build_chat_command(binary, prompt_path, model, reasoning_effort),
                cwd=str(workspace), env=chat_environment(), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=8 * 1024 * 1024,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            ))
            stderr_task = asyncio.create_task(_stderr_tail(process.stderr))
            visible = False
            async with asyncio.timeout(timeout):
                async for line in process.stdout:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeDecodeError) as error:
                        raise GrokError('Grok 返回了无法解析的流式事件') from error
                    kind = event.get('type', '')
                    if kind == 'text' and event.get('data'):
                        visible = True
                        yield {'kind': 'text', 'text': event['data']}
                    elif kind == 'thought' and event.get('data'):
                        yield {'kind': 'thought', 'text': event['data']}
                    elif kind == 'error':
                        raise GrokError(str(event.get('message') or event.get('error') or event.get('data')))
                    elif kind.startswith('tool_'):
                        raise GrokError('聊天过程中出现了工具调用，请重试')
                    elif kind == 'end':
                        reason = event.get('stopReason')
                        if reason not in ('end_turn', 'refusal'):
                            raise GrokError(f'回复未正常完成：{reason}')
                        if not visible:
                            raise GrokError('Grok 没有返回回复正文')
                        yield {'kind': 'completed', 'usage': event.get('usage', {}),
                               'session_id': event.get('sessionId'),
                               'model_usage': event.get('modelUsage', {})}
                        return
                    else:
                        yield {'kind': 'activity'}
                await process.wait()
                stderr = await stderr_task
                raise GrokError(stderr or 'Grok 连接结束，未收到完整回复')
        except TimeoutError as error:
            raise GrokError('Grok 回复超时，请重试') from error
        finally:
            if process is not None:
                await terminate_process_tree(process)
            if stderr_task is not None:
                if not stderr_task.done():
                    stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
            if prompt_path is not None:
                prompt_path.unlink(missing_ok=True)
