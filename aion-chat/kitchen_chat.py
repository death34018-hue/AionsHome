"""Small, on-demand bridge between ordinary chat and the shared kitchen."""
from __future__ import annotations

import json
import re
from datetime import date

import kitchen
from config import load_worldbook
from model_json import extract_json_object
from stream_safety import StreamActivity

ABILITY = '[KITCHEN:查看] — 需要核对库存或按现有食材配餐时查询，等回执再推荐。日常报餐照常聊，食材记账交给厨房哨兵。'
_QUERY = re.compile(r'吃(?:些|点儿?)?(?:什么|啥)|(?:能|可以)做(?:点儿?)?(?:什么|啥)|'
                    r'(?:安排|推荐|更换|换|来份|准备|给我出|出一份|各出|看看?|查看|发我|再发|打开|重发|调出|列出).{0,16}(?:菜单|食谱|菜谱|午餐|晚餐|午饭|晚饭|道菜)|'
                    r'(?:菜单|食谱|菜谱|午餐|晚餐|午饭|晚饭).{0,10}(?:建议|推荐|安排|发我|再发|看看|发一下|发一遍|换一)|换一道|^(?:今天的?)?菜单[？?！!。]*$')
_MENU_FOLLOWUP = re.compile(r'再来一份|换一份|帮我安排一下|再发(?:我|一次|一遍)|再给我看看|重新排|想个菜|给我搭配')
_REPLAN = re.compile(r'重新(?:出|排|安排|推荐|想)|再(?:给我)?出|重出|换一[批份]|换两[个道份]|再来两[个道份]')
_FOOD_CONTEXT = re.compile(r'菜单|食谱|菜谱|备选[一二菜]|午[饭餐]|晚[饭餐]|吃什么|吃啥|小厨房')


def actor_name(actor):
    if actor == 'connor':
        from chatroom import get_chatroom_names
        return get_chatroom_names()[2]
    return load_worldbook().get('ai_name') or 'AI'


class KitchenProtocolFilter:
    """Hold only kitchen controls, including blocks split across stream chunks."""
    prefixes = ('[KITCHEN_MENU]', '[KITCHEN:', '[KITCHEN：', '[KITCHEN_CARD:', '[KITCHEN_CARD：')

    def __init__(self, state):
        self.state = state
        self.buffer = ''

    def feed(self, chunk):
        self.buffer += chunk
        visible = []
        while self.buffer:
            start = self.buffer.find('[')
            if start < 0:
                visible.append(self.buffer)
                self.buffer = ''
                break
            if start:
                visible.append(self.buffer[:start])
                self.buffer = self.buffer[start:]
            upper = self.buffer.upper()
            if upper.startswith('[KITCHEN_MENU]'):
                end = upper.find('[/KITCHEN_MENU]')
                if end < 0:
                    if len(self.buffer) > 16000:
                        raise ValueError('菜单数据过长，请重试')
                    break
                payload = extract_json_object(self.buffer[len('[KITCHEN_MENU]'):end])
                if payload:
                    self.state['payloads'].append(payload)
                else:
                    self.state['protocol_error'] = True
                self.buffer = self.buffer[end+len('[/KITCHEN_MENU]'):]
            elif any(upper.startswith(p) for p in self.prefixes[1:]):
                end = self.buffer.find(']')
                if end < 0:
                    if len(self.buffer) > 300:
                        raise ValueError('厨房指令不完整，请重试')
                    break
                if upper.startswith(('[KITCHEN_CARD:', '[KITCHEN_CARD：')):
                    value = re.split('[:：]', self.buffer[:end], maxsplit=1)[1].strip()
                    if re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
                        self.state['cards'].append(value)
                elif re.split('[:：]', self.buffer[:end], maxsplit=1)[1].strip() == '查看':
                    self.state['requested'] = True
                self.buffer = self.buffer[end+1:]
            elif any(p.startswith(upper) for p in self.prefixes):
                break
            else:
                visible.append(self.buffer[0])
                self.buffer = self.buffer[1:]
        return ''.join(visible)

    def finish(self):
        tail = self.buffer
        self.buffer = ''
        if tail.upper().startswith('[KITCHEN'):
            self.state['protocol_error'] = True
            return ''
        return tail


async def read_context(*, actor, status, state, query):
    if not kitchen.enabled():
        return ''
    from kitchen_sentinel import flush_pending
    from context_builder import strip_tool_commands
    before = strip_tool_commands(state.get('visible_text','')).strip()
    state['lookup'] = {'type':'kitchen_lookup', 'actor':actor, 'actor_name':actor_name(actor),
                       'offset':len(before.encode('utf-16-le'))//2, 'before':before[-120:], 'status':'pending'}
    state['reading'] = True
    await status(f'{actor_name(actor)}正在查看冰箱…')
    try:
        await flush_pending()
        data = await kitchen.snapshot(as_cards=True)
        if not data['enabled'] or data['epoch'] != state['epoch']:
            return ''
        state['queried'] = True
        stock = [{'name': i['name'], 'amount': i['amount'], 'unit': i['unit'],
                  **({'storage': i['storage']} if i['storage'] != 'fridge' else {}),
                  **({'soon': True} if i['soon'] else {}), **({'uncertain': True} if i['uncertain'] or i['quantity'] is None else {})}
                 for i in data['items'] if i['quantity'] is None or i['quantity'] > 0]
        today = date.today().isoformat()
        current = next((m for m in data['menus'] if m['date'] == today), None)
        state['chosen_dishes'] = {m['name'] for m in current['meals']} if current else set()
        proposal = next((m for m in data['proposals'] if m['date'] == today and m['actor'] == actor), None)
        source_id = state.get('proposal_id')
        source = next((m for m in data['proposals'] + data['menus'] if m['id'] == source_id or
                       (source_id and any(v.get('source_id') == source_id for v in m['meals']))), None)
        if state.get('target_meal'):
            source = source or current or proposal
            if source and not source_id:
                state['proposal_id'] = source['id']
        def summary(menu):
            if not menu:
                return None
            if menu['kind'] == 'proposal':
                return {'id':menu['id'],'options':[m['name'] for m in menu['meals']]}
            return {'id':menu['id'],'meals':[{'meal':m['meal'],'name':m['name']} for m in menu['meals']]}
        other_options = []
        seen_actors = set()
        for option in data['proposals']:
            if option['date'] == today and option['actor'] != actor and option['actor'] not in seen_actors:
                seen_actors.add(option['actor'])
                other_options.append({'by':actor_name(option['actor']), 'options':[m['name'] for m in option['meals']]})
        reserved, replacing = [], None
        target = state.get('target_meal')
        if target:
            replacing = next((m['name'] for m in (source or {}).get('meals', []) if m['meal'] == target), None)
            for plan in (current, source, proposal):
                reserved = [{'meal':m['meal'],'ingredients':m['ingredients']} for m in (plan or {}).get('meals', []) if m['meal'] != target]
                if reserved:
                    break
        instruction = (
            '用自己的口吻简短回应。只查库存或录入食材时正常回应，不擅自排菜单。'
            '今日餐桌是用户逐餐确认后的共同决定。只有本条消息要菜单或回看时才附卡；确认、报餐、称赞或闲聊只用文字回应。'
            '还没确认时各自独立出提案和留言；只能引用自己的提案，不能拿另一位的未确认提案冒充自己的。'
            'other_options仅供参考，尽量换一种做法或搭配，食材有限时不强求差异，不虚构存货。'
            '想回看时可用[KITCHEN_CARD:菜单id]。用户说重新出时生成新提案。'
            '每次给两份不同的备选，各是一顿完整的饭。正文和卡片都不指定午晚餐，用户可以把任一份选作任一餐。只有today_menu中的餐次已确定。'
            '文字后输出JSON，options数组按顺序放两份，都用下面的食谱结构：\n'
            '[KITCHEN_MENU]{"message":"结合最近聊天，留一句此刻想对用户说的话","options":[{"name":"菜名和主食","minutes":20,'
            '"ingredients":[{"name":"库存原名","amount":0.5,"unit":"盒"}],'
            '"steps":["步骤一","步骤二"],"note":"必要的提前准备"},'
            '{"name":"另一份菜名和主食","minutes":15,"ingredients":[{"name":"库存原名","amount":1,"unit":"个"}],"steps":["做法"]}]}[/KITCHEN_MENU]\n'
            '用库存原名和日常单位，总用量不超库存，uncertain余量不算确定可用。'
            '按偏好安排简单少油的饭，总时长含洗切烹饪；提前解冻写note。'
            '仅用库存时，主食及主要食材也需有存货，调味油盐可少量；不足时说清缺项，不虚构菜单。'
            'reserved是保留那餐已占用的食材，新餐需为它留足份量。'
            'message用自己的口吻自由写，贴合上下文，不用固定祝福或重复菜名。'
            '按用户实际餐次和想法调整，不编造精确营养。只规划，不扣库存。'
        )
        return ('[小厨房本次查询]\n' + json.dumps({'date': today, 'preferences': data['preferences'],
                'stock': stock, 'today_menu': summary(current), 'proposal':summary(proposal), 'other_options':other_options,
                **({'replacing':replacing} if replacing else {}), **({'reserved':reserved} if reserved else {}),
                'pending_count': len(data['pending']),
                'record_sync_warning': bool(data['errors'])}, ensure_ascii=False, separators=(',', ':')) + '\n' + instruction)
    except Exception:
        return '[小厨房本次查询]库存同步或读取暂未完成，请如实告诉用户，不猜测库存，也不要宣称已成功记录。'


def _without_kitchen_ability(messages):
    # A switch change or creative-mode exclusion can happen after the prefix was built.
    cleaned = list(messages)
    for index, message in enumerate(messages):
        if message.get('_timeline_sender'):
            break
        content = message.get('content')
        if not isinstance(content, str) or not content.startswith('[系统能力]\n') or ABILITY not in content:
            continue
        following = messages[index + 1] if index + 1 < len(messages) else {}
        if message.get('role') == 'system' or (message.get('role') == 'user'
                and following.get('role') == 'assistant'
                and following.get('content') == '好的，需要时我会使用这些指令。'):
            line = r'\n\d+\. ' + re.escape(ABILITY) + r'(?=\n|$)'
            cleaned[index] = {**message, 'content': re.sub(line, '', content, count=1)}
            break
    return cleaned


async def stream_with_kitchen(messages, generate, *, actor, scope, parent_id, query, status, state, target_meal='', proposal_id=''):
    from ai_providers import CLI_STATUS_PREFIX
    state.clear()
    state.update(epoch=None, payloads=[], cards=[], requested=False, queried=False, protocol_error=False,
                 target_meal=target_meal, proposal_id=proposal_id, scope=scope, parent_id=parent_id)
    base = list(messages)
    direct_query = bool(_QUERY.search(query or ''))
    replan = bool(_REPLAN.search(query or '')) and (direct_query or _FOOD_CONTEXT.search(query or '') or any(
        _FOOD_CONTEXT.search(str(m.get('content',''))[-600:]) or any(isinstance(a,dict) and a.get('type') == 'kitchen_menu' for a in m.get('attachments') or [])
        for m in base[-8:] if m.get('role') != 'system'))
    fresh = scope == 'kitchen' or bool(replan)
    menu_request = fresh or direct_query or bool(_MENU_FOLLOWUP.search(query or ''))
    last_user = next((m for m in reversed(base) if m.get('_timeline_sender', 'user' if m.get('role')=='user' else '')=='user'), {})
    excluded = (scope not in ('private','chatroom','kitchen') or any(isinstance(a,dict) and a.get('type')=='kitchen_excluded' for a in last_user.get('attachments', [])))
    current = await kitchen.state() if kitchen.enabled() and not excluded else None
    if current and current['enabled']:
        state['epoch'] = current['epoch']
        active = base
    else:
        active = _without_kitchen_ability(base)
    visible_chars = 0
    separate_continuation = False
    stopped = False
    # One lookup continuation and, if needed, one menu-format repair.
    for round_index in range(3):
        if state['epoch'] is not None:
            dispatch = await kitchen.state()
            if not dispatch['enabled'] or dispatch['epoch'] != state['epoch']:
                if round_index:
                    stopped = True
                    break
                active = _without_kitchen_ability(base)
                state['epoch'] = None
        filter_ = KitchenProtocolFilter(state)
        state['requested'] = False
        round_text = []
        async for chunk in generate(active):
            if isinstance(chunk, StreamActivity):
                yield chunk
                continue
            if chunk.startswith(CLI_STATUS_PREFIX):
                # Keep the fridge label until the kitchen reply starts displaying.
                yield StreamActivity() if state.get('reading') else chunk
                continue
            round_text.append(chunk)
            visible = filter_.feed(chunk)
            if visible:
                if visible.strip():
                    state['reading'] = False
                if separate_continuation:
                    visible = '\n\n' + visible
                    separate_continuation = False
                visible_chars += len(visible)
                state['visible_text'] = state.get('visible_text','') + visible
                yield visible
        tail = filter_.finish()
        if tail:
            visible_chars += len(tail)
            state['visible_text'] = state.get('visible_text','') + tail
            yield tail
        if (round_index < 2 and state['requested'] and not state['queried'] and kitchen.enabled()
                and (await kitchen.state())['epoch'] == state['epoch']):
            # Only an explicit model tool request may read stock and inject its receipt.
            state['payloads'], state['cards'], state['protocol_error'] = [], [], False
            context = await read_context(actor=actor, status=status, state=state, query=query)
            if context:
                active += [{'role':'assistant','content':''.join(round_text)},
                           {'role':'system','content':context+'\n上面assistant是本条回复已经发出的内容与指令。只接着补充查询结果，不重复开场、观点、提醒或已经发出的功能指令。按用户原意决定是否附菜单，不再请求查询。'}]
                separate_continuation = visible_chars > 0
                continue
        if not menu_request and (state['cards'] or state['payloads'] or state['protocol_error']):
            # A lookup may still produce a fresh suggestion for an indirect food question.
            # An existing decision/card never needs replaying to acknowledge it in prose.
            state['cards'] = []
            chosen = state.get('chosen_dishes', set())
            def has_fresh_dish(payload):
                recipes = payload.get('options', payload.get('meals'))
                return isinstance(recipes,list) and any(isinstance(m,dict) and isinstance(m.get('name'),str)
                                                        and m['name'] not in chosen for m in recipes)
            state['payloads'] = [p for p in state['payloads'] if state['queried'] and has_fresh_dish(p)]
            state['protocol_error'] = False
            if not state['payloads'] and not visible_chars and round_index == 0 and state['epoch'] is not None:
                active += [{'role':'system','content':'用户这句话没有索要菜单。接着当前话题，用普通聊天文字回应。'}]
                continue
            if not state['payloads'] and not visible_chars and round_index and kitchen.enabled():
                yield '记得啦。'
        if state['epoch'] is not None and not target_meal and (state['payloads'] or (fresh and state['cards'])):
            complete = []
            for payload in state['payloads']:
                try:
                    recipes = kitchen.normalize_menu(payload)['meals']
                    if len(recipes) == 2 and len({m['meal'] for m in recipes}) == 2 and len({m['name'] for m in recipes}) == 2:
                        complete.append({**payload, 'options':recipes})
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
            state['payloads'], state['cards'] = complete[:1], []
            if not complete:
                state['protocol_error'] = True
                if round_index < 2 and kitchen.enabled():
                    active += [{'role':'assistant','content':''.join(round_text)}, {'role':'system','content':
                               '请补上自己的完整新卡，options含两顿不同的饭，不指定午晚餐，不重发旧卡或重复正文。需要核对库存时，可主动用[KITCHEN:查看]，等查询回执再继续。'}]
                    continue
        foreign = await reject_foreign_proposals(state,actor) if state['epoch'] is not None else False
        if foreign and not state['payloads'] and not state['cards'] and round_index < 2 and kitchen.enabled():
            active += [{'role':'assistant','content':''.join(round_text)},
                       {'role':'system','content':'刚才引用的是另一位尚未被用户确认的提案，不能当作你的。请补上你自己独立的KITCHEN_MENU与留言，不重复前面的正文。需要核对库存时，可主动用[KITCHEN:查看]，等查询回执再继续。'}]
            continue
        break
    if not visible_chars and (state['payloads'] or state['cards']) and kitchen.enabled():
        yield '先看看这份菜单。'
    elif not visible_chars and stopped:
        yield '这次查看已停止。'


async def reject_foreign_proposals(state, actor):
    rejected = []
    for menu_id in state.get('cards',[]):
        menu = await kitchen.get_menu(menu_id)
        if menu and menu.get('kind')=='proposal' and menu['actor'] != actor:
            rejected.append(menu_id)
    if rejected:
        state['cards'] = [i for i in state['cards'] if i not in rejected]
        state['protocol_error'] = True
    return bool(rejected)


async def finish_reply(text, *, actor, state):
    attachments = []
    lookup = state.get('lookup')
    if not kitchen.enabled() or state.get('epoch') is None:
        return text.strip(), [{**lookup,'status':'stopped'}] if lookup else attachments
    current = await kitchen.state()
    if current['epoch'] != state['epoch']:
        return text.strip(), [{**lookup,'status':'stopped'}] if lookup else attachments
    await reject_foreign_proposals(state,actor)
    menu_ids = list(state.get('cards', []))
    for payload in state.get('payloads', []):
        try:
            from context_builder import strip_tool_commands
            payload = {**payload, 'message':payload.get('message') or strip_tool_commands(text)[:400]}
            saved = await kitchen.save_menu(payload, actor=actor, epoch=state['epoch'], target_meal=state.get('target_meal',''),
                        proposal_id=state.get('proposal_id',''),scope=state.get('scope',''),parent_id=state.get('parent_id',''))
            if saved:
                menu_ids.append(saved['id'])
        except kitchen.MenuChanged:
            raise
        except (ValueError, TypeError, KeyError):
            state['protocol_error'] = True
    for menu_id in dict.fromkeys(menu_ids):
        menu = await kitchen.get_menu(menu_id, as_card=True)
        if menu:
            attachments.append({'type': 'kitchen_menu', 'menu_id': menu_id,
                                'summary': '；'.join(m['name'] for m in menu['meals'])[:160]})
    if attachments:
        await kitchen.notify()
    if state.get('protocol_error') and not attachments:
        text = text.rstrip() + '\n\n菜单卡还没整理完整，可以再让我安排一次。'
    if lookup:
        attachments.append({**lookup,'status':'done' if state.get('queried') else 'failed'})
    return text.strip(), attachments


async def chat_stream(messages, generate, *, actor, scope, parent_id, query, queue, state):
    async def status(text):
        await queue.put({'type': 'cli_status' if scope == 'private' else actor + '_status', 'text': text,
                         'kitchen_lookup':dict(state['lookup'])})
    async for chunk in stream_with_kitchen(messages, generate, actor=actor, scope=scope,
            parent_id=parent_id, query=query, status=status, state=state):
        yield chunk


async def generate_plan(*, actor='aion', meal='', proposal_id=''):
    from ai_providers import stream_ai, CLI_STATUS_PREFIX
    from config import DEFAULT_MODEL
    from chatroom import load_chatroom_config, _read_connor_persona
    if not kitchen.enabled():
        raise ValueError('请先开启小厨房 AI 功能')
    async with kitchen.get_db() as db:
        available = await (await db.execute('SELECT 1 FROM kitchen_items WHERE quantity IS NULL OR quantity>0 LIMIT 1')).fetchone()
    if not available:
        raise ValueError('冰箱清单还是空的，先记下几样食材吧')
    wb, cfg = load_worldbook(), load_chatroom_config()
    persona = _read_connor_persona() if actor == 'connor' else wb.get('ai_persona', '')
    query = '今天吃什么？请重新安排' + ({'lunch': '午餐', 'dinner': '晚餐'}.get(meal, '两份不同的备选，让我自己选哪顿吃'))
    from context_builder import fetch_merged_timeline, render_merged_timeline
    recent = await fetch_merged_timeline(actor, 12)
    # Reuse each actor's visibility rules and text history; no image work for menu planning.
    recent = [{**m, 'content':str(m.get('content') or '')[:800]} for m in recent]
    messages = ([{'role': 'system', 'content': persona}] if persona else [])
    if wb.get('user_persona'):
        messages.append({'role':'system', 'content':wb['user_persona']})
    from capabilities import format_ability_block
    messages.append({'role':'system', 'content':format_ability_block([ABILITY])})
    messages.extend(render_merged_timeline(recent, actor, include_image_attachments=False))
    messages.append({'role':'user', 'content':query})
    async def generate(prompts):
        if actor == 'connor':
            from routes.chatroom import _stream_connor_model
            source = _stream_connor_model(prompts, cfg.get('connor_model'))
        else:
            source = stream_ai(prompts, cfg.get('aion_model') or DEFAULT_MODEL)
        async for chunk in source:
            yield chunk
    async def status(_):
        pass
    state, chunks = {}, []
    async for chunk in stream_with_kitchen(messages, generate, actor=actor, scope='kitchen',
            parent_id='', query=query, status=status, state=state, target_meal=meal, proposal_id=proposal_id):
        if isinstance(chunk, str) and not chunk.startswith(CLI_STATUS_PREFIX):
            chunks.append(chunk)
    if meal:
        if not state['payloads']:
            raise ValueError('这次没有生成替换的菜，可以再点一次换菜')
        state['cards'] = []  # A swap must return the new recipe, even if the model referenced the old card.
    text, attachments = await finish_reply(''.join(chunks), actor=actor, state=state)
    menus = [a for a in attachments if a['type']=='kitchen_menu']
    if not menus:
        raise ValueError(text[:300] or '这次没有生成完整菜单，请再试一次')
    return {'reply': text, 'menu': await kitchen.get_menu(menus[0]['menu_id'], as_card=True)}
