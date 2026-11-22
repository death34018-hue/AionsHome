"""Separate one inline SVG animation from a chat reply."""
import html
import re
from uuid import uuid4

MAX_SVG = 16_000
GUIDE = ('用 <title> 起个短标题，设好 viewBox，画面会按比例缩放进手机聊天气泡（约 360px 宽）。'
         '动画用 animate、animateTransform、animateMotion、set 或 <style> 里的 @keyframes；'
         '互动用 begin="元素id.click" 触发，可以连锁、延时、藏彩蛋，可点的地方在画面里给个小提示。'
         f'不写脚本，不引用外部图片和字体，最多 {MAX_SVG:,} 字符。'
         '题材和玩法自由发挥，简单有趣就好：一个小场景、一两处互动，元素别太多，少用模糊滤镜。')
PROMPT = '可偶尔在正文中直接写一段 <svg>…</svg>，即兴画一个能点着玩的小动画。' + GUIDE
EDIT_PROMPT = '按要求修改这个小动画，保留原来的互动。只返回完整的 <svg>…</svg>。' + GUIDE
EDIT_REQUEST = re.compile(r'(?:改|修|换|调|加|变成|改成|点不动|打不开|没反应|不能玩|玩不了|动不了)', re.I)

SVG_OPEN = re.compile(r'<svg(?=[\s>/])', re.I)
SVG_TAG = re.compile(r'<(/?)svg(?=[\s>/])[^>]*>', re.I)
# A code fence or a partial "<svg" at the end may still open a drawing.
PENDING = re.compile(r'(?:```[\w-]*\s*|`{1,2})?(?:<(?:s(?:vg?)?)?)?\Z', re.I)
FENCE_BEFORE = re.compile(r'```[\w-]*\s*\Z')
FENCE_AFTER = re.compile(r'\s*```')
FENCE_AFTER_PENDING = re.compile(r'\s*`{0,2}')


def make_card(source):
    source = (source or '').strip()
    if not (0 < len(source) <= MAX_SVG and SVG_OPEN.match(source)
            and re.search(r'<(?!/?(?:svg|title|desc|style)[\s>/])[a-z]', source[4:], re.I)):
        return None
    title = re.search(r'<title\b[^>]*>(.*?)</title\s*>', source, re.I | re.S)
    label = html.unescape(re.sub(r'<[^>]*>', '', title[1])).strip() if title else ''
    return {'type': 'web_card', 'format': 'svg', 'id': uuid4().hex, 'title': label[:60] or '小动画',
            'html': source, 'revision': 1}


def _utf16(text):
    return len(text.encode('utf-16-le', errors='surrogatepass')) // 2


class WebCardFilter:
    """Remove <svg> drawings from streamed prose and keep the first one."""
    def __init__(self, prefix=''):
        from safe_live_stream import KnownCommandStreamFilter
        self.position_filter = KnownCommandStreamFilter()
        self.tail = ''
        self.inside = False
        self.after = False
        self.src = ''
        self.scan = self.depth = self.size = 0
        self.oversize = False
        self.card = None
        self.failure = None
        self.order = None
        prefix = self.position_filter.feed(prefix)
        self.offset = _utf16(prefix)
        self.before = prefix[-120:]

    def feed(self, chunk):
        buf, self.tail = self.tail + chunk, ''
        visible = []
        while buf:
            if self.inside:
                buf = self._consume(buf)
                continue
            if self.after:
                fence = FENCE_AFTER.match(buf)
                if fence:
                    buf = buf[fence.end():]
                elif FENCE_AFTER_PENDING.fullmatch(buf):
                    self.tail = buf
                    break
                self.after = False
                continue
            match = SVG_OPEN.search(buf)
            if match:
                self._show(FENCE_BEFORE.sub('', buf[:match.start()]), visible)
                self.inside = True
                if not self.card:
                    self.order = {'offset': self.offset, 'before': self.before, 'after': ''}
                buf = buf[match.start():]
                continue
            hold = PENDING.search(buf).start()
            self._show(buf[:hold], visible)
            self.tail = buf[hold:]
            break
        return ''.join(visible)

    def _show(self, text, visible):
        if not text:
            return
        visible.append(text)
        anchor = self.position_filter.feed(text)
        self.offset += _utf16(anchor)
        self.before = (self.before + anchor)[-120:]
        if self.order:
            self.order['after'] = (self.order['after'] + anchor)[:120]

    def _consume(self, text):
        """Track nested <svg> tags; return the text after the closing tag."""
        self.src += text
        self.size += len(text)
        for match in SVG_TAG.finditer(self.src, self.scan):
            self.scan = match.end()
            if match[1]:
                self.depth -= 1
            elif not match[0].endswith('/>'):
                self.depth += 1
            if self.depth <= 0:
                rest = self.src[match.end():]
                self.size -= len(rest)
                self.src = self.src[:match.end()]
                self._close()
                return rest
        if len(self.src) > MAX_SVG:
            # Too long to keep: only look for the closing tag from here on.
            self.oversize = True
            cut = max(self.scan, len(self.src) - 512)
            self.src, self.scan = self.src[cut:], max(0, self.scan - cut)
        return ''

    def _keep(self):
        if self.card:
            return
        if not self.oversize:
            self.card = make_card(self.src)
        if self.card:
            self.card['inline_order'] = self.order

    def _close(self):
        if not self.card:
            self._keep()
            if not self.card:
                self.failure = self.incomplete('too_large' if self.oversize else 'invalid')
        self.inside, self.after, self.oversize = False, True, False
        self.src, self.scan, self.depth, self.size = '', 0, 0, 0

    def finish(self):
        if self.inside:
            # Keep what was drawn before the reply ended; the player closes open tags.
            self._keep()
            if not self.card:
                self.failure = self.incomplete('too_large' if self.oversize else 'unclosed')
            self.inside = False
            return ''
        tail, self.tail = self.tail, ''
        return '' if self.after or re.search(r'```|<s', tail, re.I) else tail

    def incomplete(self, reason):
        item = {'type': 'web_card_incomplete', 'reason': reason, 'chars': self.size, 'inline_order': self.order}
        if reason == 'too_large':
            item['limit'] = MAX_SVG
        return item

    def attachment(self):
        return self.card or self.failure


def split_reply(text, *, enabled=True):
    if not SVG_OPEN.search(text or ''):
        return text or '', None
    parser = WebCardFilter()
    body = parser.feed(text or '') + parser.finish()
    return body, parser.attachment() if enabled else None


def strip_drawings(text):
    return split_reply(text, enabled=False)[0]


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
            yield '送你一个小动画。' if card['type'] == 'web_card' else '这次小动画没画完整。'


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
                         'text': '正在画小动画…', 'web_card': {'type': 'web_card_pending', 'actor': actor,
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
