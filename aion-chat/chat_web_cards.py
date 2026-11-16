"""Separate a single self-contained HTML attachment from a chat reply."""
import html
import re
from uuid import uuid4

MAX_HTML = 24_000
OPEN, CLOSE = '[WEB]', '[/WEB]'
DELIMITERS = {token: re.compile(re.escape(token), re.I | re.ASCII) for token in (OPEN, CLOSE)}
PAGE_GUIDE = (f'用 <title> 给作品起一个贴合内容的短标题，内联 CSS/JS，最多 {MAX_HTML:,} 字符。'
              '视口固定 360px 宽、约 460px 高（随设备略有变化），整体等比缩放进聊天卡片。'
              '布局用 100vh 填满视口，主视觉随可用高度缩放，文字可换行。'
              '每一步的状态和操作按钮须在同一屏完整可见，内容多时分步展示，无需滚动。')
PROMPT = '可偶尔在正文中插入[WEB]自含HTML[/WEB]，即兴做一个手机单屏小互动。' + PAGE_GUIDE
BLOCK = re.compile(r'\[WEB\].*?(?:\[/WEB\]|\Z)', re.S | re.I)
EDIT_REQUEST = re.compile(r'(?:改|修|换|调|加|变成|改成|点不动|打不开|没反应|不能玩|玩不了|动不了)', re.I)


def make_card(source):
    source = source.strip()
    fence = re.fullmatch(r'```(?:html)?\s*(.*?)\s*```', source, re.I | re.S)
    if fence:
        source = fence[1].strip()
    if not (0 < len(source) <= MAX_HTML and re.search(r'<[a-z][\w:-]*(?:\s|/?>)', source, re.I)):
        return None
    title = re.search(r'<title\b[^>]*>(.*?)</title\s*>', source, re.I | re.S)
    heading = re.search(r'<h[12]\b[^>]*>(.*?)</h[12]\s*>', source, re.I | re.S)
    label = next((text for match in (title, heading) if match
                  if (text := html.unescape(re.sub(r'<[^>]*>', '', match[1])).strip())), '')
    return {'type': 'web_card', 'id': uuid4().hex, 'title': label[:60] or '互动小网页',
            'html': source, 'revision': 1}


class WebCardFilter:
    """Only retain a potential delimiter and at most one page worth of HTML."""
    def __init__(self, prefix=''):
        from safe_live_stream import KnownCommandStreamFilter
        self.position_filter = KnownCommandStreamFilter()
        self.tail = ''
        self.inside = False
        self.parts = []
        self.size = 0
        self.card = None
        self.failure = None
        prefix = self.position_filter.feed(prefix)
        self.offset = len(prefix.encode('utf-16-le', errors='surrogatepass')) // 2
        self.before = prefix[-120:]
        self.order = None

    def feed(self, chunk):
        buf, self.tail = self.tail + chunk, ''
        visible = []
        while buf:
            token = CLOSE if self.inside else OPEN
            upper = buf.upper()
            # Use an ASCII-only case-insensitive search so Unicode expansion
            # (e.g. ß → SS) cannot shift a delimiter's offset in the original.
            match = DELIMITERS[token].search(buf)
            if match:
                part, buf = buf[:match.start()], buf[match.end():]
            else:
                keep = next((i for i in range(min(len(buf), len(token)-1), 0, -1)
                             if upper.endswith(token[:i])), 0)
                self.tail = buf[-keep:] if keep else ''
                part, buf = (buf[:-keep] if keep else buf), ''
            if self.inside:
                self.size += len(part)
                if self.size <= MAX_HTML and part and not self.card:
                    self.parts.append(part)
            else:
                visible.append(part)
                anchor = self.position_filter.feed(part)
                self.offset += len(anchor.encode('utf-16-le', errors='surrogatepass')) // 2
                self.before = (self.before + anchor)[-120:]
                if self.order:
                    self.order['after'] = (self.order['after'] + anchor)[:120]
            if match:
                if self.inside:
                    if not self.card:
                        if self.size <= MAX_HTML:
                            self.card = make_card(''.join(self.parts))
                        if self.card:
                            self.card['inline_order'] = self.order
                        else:
                            self.failure = self.incomplete('invalid_html')
                    self.parts.clear()
                    self.size = 0
                elif not self.card:
                    self.order = {'offset': self.offset, 'before': self.before, 'after': ''}
                self.inside = not self.inside
        return ''.join(visible)

    def finish(self):
        # A cut-off opener/content is never released to speech.
        return '' if self.inside or len(self.tail) > 1 else self.tail

    def incomplete(self, reason):
        size = self.size + (len(self.tail) if self.inside else 0)
        item = {'type': 'web_card_incomplete', 'reason': 'too_large' if size > MAX_HTML else reason,
                'chars': size, 'inline_order': self.order}
        if size > MAX_HTML:
            item['limit'] = MAX_HTML
        return item

    def attachment(self):
        return self.card or (self.incomplete('unclosed') if self.inside else self.failure)


def split_reply(text, *, enabled=True):
    if OPEN.lower() not in (text or '').lower():
        return text or '', None
    parser = WebCardFilter()
    body = parser.feed(text or '') + parser.finish()
    return body, parser.attachment() if enabled else None


async def prose_chunks(source, attachments, *, enabled, model_key=None, on_open=None, prefix=''):
    from ai_providers import CLI_STATUS_PREFIX
    from stream_safety import StreamActivity
    parser = WebCardFilter(prefix)
    spoke = False
    async for chunk in source:
        if isinstance(chunk, StreamActivity) or chunk.startswith(CLI_STATUS_PREFIX):
            yield chunk
            continue
        visible = parser.feed(chunk)
        if on_open and parser.order and enabled and not attachments:
            await on_open({'offset': parser.order['offset']})
            on_open = None
        if visible:
            spoke = spoke or bool(visible.strip())
            yield visible
        else:
            yield StreamActivity()
    tail = parser.finish()
    if tail:
        yield tail
    card = parser.attachment()
    if card and enabled and not any(item['type'] == 'web_card' for item in attachments):
        if model_key and card['type'] == 'web_card':
            card['model'] = model_key
        attachments[:] = [card]
        if not spoke and not tail.strip():
            yield '送你一个小网页。' if card['type'] == 'web_card' else '这次小网页没写完整。'


async def chat_stream(messages, generate, *, actor, scope, parent_id, query, queue, state, model_key=None):
    from capabilities import is_capability_enabled
    from kitchen_chat import chat_stream as kitchen_stream
    from routes.web_cards import revision_context

    enabled = is_capability_enabled('web_cards')
    if enabled:
        extra = await revision_context(scope, parent_id, query, actor)
        messages = [{'role':'system','content':PROMPT}, *messages]
        if extra:
            messages.append({'role':'user','content':extra})
    attachments = []
    async def on_open(order):
        await queue.put({'type': 'cli_status' if scope == 'private' else actor + '_status',
                         'text': '正在写小网页…', 'web_card': {'type': 'web_card_pending', 'actor': actor,
                                                           'inline_order': order}})

    def prose_stream(prompts):
        return prose_chunks(generate(prompts), attachments, enabled=enabled, model_key=model_key,
                            on_open=on_open, prefix=state.get('visible_text', ''))

    async for chunk in kitchen_stream(messages, prose_stream, actor=actor, scope=scope,
            parent_id=parent_id, query=query, queue=queue, state=state):
        yield chunk
    state['web_attachments'] = attachments


async def finish_reply(text, *, actor, state):
    from kitchen_chat import finish_reply as finish_kitchen_reply
    body, attachments = await finish_kitchen_reply(text, actor=actor, state=state)
    return body, attachments + state.get('web_attachments', [])
