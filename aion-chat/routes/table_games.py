"""Persistent, one-model-decision-per-turn API for dice and hold'em."""
import asyncio
import copy
import json
import time
import logging
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import tabletop
import tabletop_wallet as wallet
from tabletop_reports import result_card, last_active_group
from billiards.reports import save_chat_card
from config import DATA_DIR, DEFAULT_MODEL, DB_PATH
from routes import doudizhu as companion
from billiards.companionship import spoken_text, table_context_text

router = APIRouter(prefix='/api/table-games', tags=['table-games'])
_locks = {kind: asyncio.Lock() for kind in tabletop.KINDS}
_thinking = {}
_chatting = {}
_report_lock = asyncio.Lock()


class NewTable(BaseModel):
    opponent: Literal['both', 'aion', 'connor'] = 'both'
    wild: bool = True
    stake: Decimal | None = Field(default=None, ge=0, le=1000000, decimal_places=2)


class Turn(BaseModel):
    game_id: str
    revision: int
    action: str = ''
    count: int | None = None
    face: int | None = None
    amount: int | None = None
    speech: str = Field(default='', max_length=160)


class ChatMessage(BaseModel):
    game_id: str
    message_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=2000)
    target: Literal['', 'aion', 'connor'] = ''


def _check_kind(kind):
    if kind not in tabletop.KINDS:
        raise HTTPException(404, '没有这个游戏')


def _path(kind):
    return DATA_DIR / f'table_{kind}.json'


def _load(kind):
    stored = wallet.load(DB_PATH, kind)
    if not stored:
        path = _path(kind)
        stored = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
    if stored and kind == 'texas-holdem' and stored['phase'] in ('playing', 'round_over') and not stored.get('closed'):
        # Existing tables adopt the shorter limit without discarding an accepted hand.
        stored['round_limit'] = tabletop.POKER_ROUND_LIMIT
    return stored


def _save(state):
    wallet.save(DB_PATH, state)


def _response(state):
    names = companion._player_names()
    visible = tabletop.view(state) if state else None
    if visible:
        visible['busy'] = _thinking.get(state['kind']) == (state['id'], state['revision'])
        visible['chat_busy'] = _chatting.get(state['kind'], (None,))[0] == state['id']
        for p, seat in visible['seats'].items():
            seat['name'] = names[p]
    return {'ok': True, 'state': visible, 'names': names, 'wallets': wallet.balances(DB_PATH)}


def _current(kind, body):
    state = _load(kind)
    if not state or (state['id'], state['revision']) != (body.game_id, body.revision):
        raise HTTPException(409, '牌桌已经更新，请刷新后继续')
    return state


async def _commit(state):
    state['revision'] += 1
    state['updated_at'] = time.time()
    report = await _closing_report(state)
    wallet.save(DB_PATH, state, report)
    if state.get('wallet', {}).get('status') == 'settled':
        await _notify_wallets()
    if report:
        await _deliver_reports()


def _reportable(state):
    return state['phase'] == 'game_over' or (
        state['kind'] == 'texas-holdem' and state['phase'] == 'cancelled' and bool(state.get('rounds')))


async def _closing_report(state):
    if not _reportable(state) or state.get('reported_table'):
        return None
    try:
        target = await last_active_group()
    except Exception:
        target = None
    state['reported_table'] = True
    return dict(id=f"table_result_{state['id']}_final", scope='table', target=target,
                names=companion._player_names())


async def _deliver_reports():
    async with _report_lock:
        await _flush_reports()


async def _flush_reports():
    for report in wallet.pending_reports(DB_PATH):
        try:
            snapshot = report['state']
            if not _reportable(snapshot):
                wallet.update_report(DB_PATH, report, delivered=True)
                continue
            if report.get('scope') != 'table' or not report.get('target'):
                report['scope'] = 'table'
                report['target'] = await last_active_group()
                if not report['target']:
                    continue
                wallet.update_report(DB_PATH, report)
            content, attachments = result_card(report['state'], report['names'])
            if await save_chat_card(report['id'], content, attachments, report['target']):
                wallet.update_report(DB_PATH, report, delivered=True)
        except Exception:
            logging.getLogger(__name__).warning('牌桌战报等待重试', exc_info=True)


async def _notify_wallets():
    from ws import manager
    try:
        await manager.broadcast({'type': 'wallet_update'})
        await manager.broadcast({'type': 'connor_wallet_update'})
    except Exception:
        logging.getLogger(__name__).warning('钱包已保存，界面等待刷新', exc_info=True)


def _prompt(state, player):
    names = companion._player_names()
    public = tabletop.view(state, player)
    public['messages'] = public['messages'][-24:]
    for p, seat in public['seats'].items():
        seat['name'] = names[p]
    if state['kind'] == 'liars-dice':
        rules = ('叫数表示全桌至少有 count 个 face 点；数量必须增加，或数量相同而点数增加。'
                 '最多叫到当前全桌骰子数。challenge 表示质疑上一家，不足叫数则上一家输，否则你输。'
                 '输一轮少一颗骰子，没有骰子淘汰。wild_active=true 时 1 可以算作其他任一点数；'
                 '有人叫 1 后本轮关闭百搭，下轮恢复。')
        if state.get('wallet'):
            rules += 'wallet 金额均为整数分；整局结束后每位输家支付 stake 给最终赢家，单轮只减骰子，不结算钱。'
        if state.get('dice_mode') == 'three_rounds':
            rules = ('全桌每人5颗骰子，共玩3轮，每轮重新摇5颗，不淘汰。叫数表示全桌至少有count个face点；'
                     '数量增加或数量相同而点数增加，不能超过全桌骰子数。challenge质疑上一家；'
                     '实际数量够则开骰者输，不够则叫数者输。输家付每轮约定金额给另一方，第三人不输不赢。'
                     'wild_active=true时1点百搭，叫过1本轮失效，下轮恢复。wallet金额均为整数分（100分=1元）。')
        schema = {'action': 'bid 或 challenge', 'count': '叫数时填整数', 'face': '叫数时填 1~6',
                  'speech': '一句符合人设的桌边发言，建议40字以内'}
    else:
        rules = ('无限注德州扑克，使用两张底牌与五张公共牌中的最佳五张。'
                 'amount 是本条街加注到的总额，不是额外加多少；只能从 legal.actions 选择动作，'
                 '加注须在 min_raise 与 max_raise 之间。call 自动补足跟注或短码全下；all_in 投入剩余筹码。'
                 '筹码不足完整加注的全下，不会给已经行动者重新加注的权利。服务端自动结算边池及平分。')
        rules += (f'金额字段及 amount 均以整数分计，100分=1元；盲注{state.get("blind", 100) // 2}/{state.get("blind", 100)}分。wallet.initial是实际带入金额，'
                  '只能下注桌上剩余金额，不能赊账或从钱包自动追加。用户入桌额度以两位伴侣中较高的钱包为上限。'
                  if state.get('wallet') else '免费练习，每人500筹码，盲注5/10。')
        rules += '每次发底牌算一手，翻牌/转牌/河牌仍属于同一手；最多3手后自动收桌结算。'
        schema = {'action': 'fold / check / call / raise / all_in', 'amount': 'raise 时填整数总额',
                  'speech': '一句符合人设的桌边发言，建议40字以内'}
    return (f'【{tabletop.KINDS[state["kind"]]}回合】你是{names[player]}（座位 {player}）。'
            '保持原本人设，接续最近聊天和本桌发言。你自己决定动作与数字，可以诈唬；没有预设策略替你出招。'
            '你只能知道自己的暗骰/底牌与公开信息。禁止调用工具、读取文件或查找其他玩家的秘密。'
            '只输出一个 JSON 对象，不要 Markdown 或思考过程。speech 是公开发言，不要无意泄露自己的暗牌。\n'
            f'规则：{rules}\nJSON 格式：{json.dumps(schema, ensure_ascii=False)}\n'
            f'你可见的桌面（含最近桌边上下文与合法动作）：{json.dumps(public, ensure_ascii=False)}')


async def _ask_ai(state, player):
    room_id, room, history = await companion._latest_group_context()
    builder = companion.build_aion_group_context if player == 'aion' else companion.build_connor_group_context
    messages, _ = await builder(room_id, history, room.get('context_minutes', 30),
                                f'{tabletop.KINDS[state["kind"]]}牌桌 JSON 决策')
    messages = list(messages) + [{'role': 'user', 'content': _prompt(state, player)}]
    for attempt in range(2):
        raw = await companion._collect_ai_text(messages, player, state.get('model') or DEFAULT_MODEL)
        try:
            decision = companion._extract_json_object(raw)
            decision['speech'] = spoken_text(str(decision.get('speech') or ''))[:160]
            tabletop.act(copy.deepcopy(state), player, decision)
            return decision
        except (ValueError, TypeError) as exc:
            if attempt:
                raise ValueError('AI 连续返回了不合法的动作，请重试这一回合') from exc
            messages += [{'role': 'assistant', 'content': raw[:1500]},
                         {'role': 'user', 'content': f'动作无效：{exc}。请依据 legal 重新决定，只返回合法 JSON。'}]


async def _chat_reply(state, player):
    room_id, room, history = await companion._latest_group_context()
    builder = companion.build_aion_group_context if player == 'aion' else companion.build_connor_group_context
    messages, _ = await builder(room_id, history, room.get('context_minutes', 30),
                                f'{tabletop.KINDS[state["kind"]]}桌边闲聊')
    messages = [{**m, 'content': table_context_text(m['content']) if isinstance(m.get('content'), str)
                 else m.get('content', '')} for m in messages]
    names = companion._player_names()
    visible = tabletop.view(state, player)
    visible['messages'] = visible['messages'][-24:]
    for p, seat in visible['seats'].items():
        seat['name'] = names[p]
    messages.append({'role': 'user', 'content': (
        f'【当前场景：小家娱乐室·{tabletop.KINDS[state["kind"]]}桌边聊天】你是{names[player]}。'
        '用户刚在桌边说话，请保留人设、关系与最近聊天，自然回应一到三句，可以聊游戏也可以聊日常。'
        '这是独立闲聊，不是行动回合；不要替任何人叫数、下注或改变牌局。'
        '只输出说给在场人听的普通文字，不要 JSON、工具调用、内心旁白、动作或语音标记。'
        '不知道其他人的暗骰或底牌，不要编造结果或无意泄露自己的暗牌。'
        '如果牌局包含 wallet，所有金额数字都是整数分（100分=1元），对话里请换算成元。'
        f'\n真实可见牌局与最近桌边对话：{json.dumps(visible, ensure_ascii=False)}'
    )})
    raw = await asyncio.wait_for(companion._collect_ai_text(messages, player, state.get('model') or DEFAULT_MODEL), 150)
    text = spoken_text(raw).strip()
    if not text:
        raise ValueError('没有收到可显示的回复')
    return text[:2000]


@router.post('/{kind}/chat')
async def table_chat(kind: str, body: ChatMessage):
    _check_kind(kind)
    text = body.text.strip()
    if not text:
        raise HTTPException(400, '先写点想说的话')
    token = (body.game_id, body.message_id)
    async with _locks[kind]:
        state = _load(kind)
        if not state or state['id'] != body.game_id:
            raise HTTPException(409, '已经换了一桌，请重新发送')
        targets = [p for p in state['players'] if p != 'user']
        if body.target:
            if body.target not in targets:
                raise HTTPException(400, '这位伴侣不在桌边')
            targets = [body.target]
        if _chatting.get(kind, (None,))[0] == state['id']:
            raise HTTPException(409, '正在接话，请稍候再发')
        previous = next((m for m in state.get('messages', []) if m['id'] == body.message_id), None)
        if previous and (previous['sender'] != 'user' or previous['text'] != text or previous['round'] != state['round']):
            raise HTTPException(409, '这条消息属于之前的回合，请重新发送')
        if not previous:
            tabletop.append_message(state, 'user', text, message_id=body.message_id)
        replied = {m['sender'] for m in state.get('messages', []) if m.get('reply_to') == body.message_id}
        targets = [p for p in targets if p not in replied]
        state['chat_error'] = ''
        state['chat_updated_at'] = time.time()
        _save(state)
        _chatting[kind] = token
        round_number = state['round']
    try:
        for player in targets:
            async with _locks[kind]:
                snapshot = _load(kind)
                if snapshot['id'] != body.game_id or snapshot['round'] != round_number:
                    break
            try:
                reply = await _chat_reply(snapshot, player)
                error = ''
            except Exception:
                reply, error = '', '这次没能接上话，消息已保存，可以重试。'
            async with _locks[kind]:
                current = _load(kind)
                if current['id'] != body.game_id or current['round'] != round_number:
                    break
                if reply:
                    tabletop.append_message(current, player, spoken_text(reply), reply_to=body.message_id)
                if error:
                    current['chat_error'] = error
                current['chat_updated_at'] = time.time()
                _save(current)
    finally:
        if _chatting.get(kind) == token:
            _chatting.pop(kind, None)
    async with _locks[kind]:
        return _response(_load(kind))


@router.get('/{kind}/state')
async def get_state(kind: str):
    _check_kind(kind)
    await _deliver_reports()
    async with _locks[kind]:
        return _response(_load(kind))


@router.post('/{kind}/new')
async def new_table(kind: str, body: NewTable):
    _check_kind(kind)
    players = ['user', 'aion', 'connor'] if body.opponent == 'both' else ['user', body.opponent]
    async with _locks[kind]:
        try:
            stake = body.stake if body.stake is not None else Decimal('50' if kind == 'liars-dice' else '200')
            state = wallet.open_table(DB_PATH, kind, players, body.wild, int(stake * 100))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        state['model'] = companion.load_chatroom_config().get('aion_model') or DEFAULT_MODEL
        _save(state)
        if state.get('wallet'):
            await _notify_wallets()
        return _response(state)


@router.post('/{kind}/action')
async def user_action(kind: str, body: Turn):
    _check_kind(kind)
    async with _locks[kind]:
        state = _current(kind, body)
        try:
            tabletop.act(state, 'user', body.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        await _commit(state)
        return _response(state)


@router.post('/{kind}/next')
async def next_hand(kind: str, body: Turn):
    _check_kind(kind)
    async with _locks[kind]:
        state = _current(kind, body)
        try:
            tabletop.next_round(state)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        await _commit(state)
        return _response(state)


@router.post('/{kind}/ai-step')
async def ai_step(kind: str, body: Turn):
    _check_kind(kind)
    async with _locks[kind]:
        state = _current(kind, body)
        if state['phase'] != 'playing' or state['turn'] not in ('aion', 'connor'):
            raise HTTPException(400, '现在不是 AI 回合')
        token = (state['id'], state['revision'])
        if _thinking.get(kind) == token:
            raise HTTPException(409, '这一回合正在思考，请稍候')
        _thinking[kind] = token
    try:
        decision = await asyncio.wait_for(_ask_ai(state, state['turn']), timeout=240)
        async with _locks[kind]:
            current = _current(kind, body)
            tabletop.act(current, current['turn'], decision)
            await _commit(current)
            return _response(current)
    except HTTPException:
        raise
    except asyncio.TimeoutError as exc:
        raise HTTPException(504, 'AI 思考超时，牌局已保留，请重试这一回合') from exc
    except Exception as exc:
        # No automatic move: a model/network failure leaves the exact turn intact.
        raise HTTPException(502, 'AI 暂时没有给出合法动作，牌局已保留，请重试') from exc
    finally:
        if _thinking.get(kind) == token:
            _thinking.pop(kind, None)


@router.post('/{kind}/cashout')
async def cashout(kind: str, body: Turn):
    _check_kind(kind)
    async with _locks[kind]:
        state = _current(kind, body)
        try:
            wallet.cashout(DB_PATH, state)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        report = await _closing_report(state)
        if report:
            wallet.save(DB_PATH, state, report)
            await _deliver_reports()
        if state.get('wallet'):
            await _notify_wallets()
        return _response(state)
