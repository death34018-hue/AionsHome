"""
斗地主牌桌：发牌、叫分、出牌校验、AI JSON 决策。

本模块刻意把游戏状态、牌型逻辑、AI 调用和 API 都收在一个新文件里，
避免把斗地主的规则侵入原有聊天/聊天室路由。
"""

import asyncio
import json
import random
import time
from collections import Counter, defaultdict
from functools import lru_cache
from itertools import combinations
from typing import Optional

import aiosqlite
from fastapi import APIRouter
from pydantic import BaseModel

from ai_providers import CLI_STATUS_PREFIX, stream_ai
from chatroom import build_aion_group_context, build_connor_group_context, get_chatroom_names, load_chatroom_config, stream_connor_cli
from config import DATA_DIR, DEFAULT_MODEL, SETTINGS
from database import get_db
from tts import TTSStreamer
from ws import manager


router = APIRouter(prefix="/api/doudizhu", tags=["doudizhu"])

STATE_PATH = DATA_DIR / "doudizhu_state.json"
PLAYERS = ["user", "aion", "connor"]
DEFAULT_PLAYER_NAMES = {"user": "用户", "aion": "AI", "connor": "Connor"}
RANKS = ["3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A", "2", "BJ", "RJ"]
SUITS = ["S", "H", "C", "D"]
RANK_VALUE = {rank: i for i, rank in enumerate(RANKS)}
SUIT_VALUE = {"S": 0, "H": 1, "C": 2, "D": 3}
AI_TIMEOUT_SECONDS = 240
_state_lock = asyncio.Lock()


def _player_names() -> dict[str, str]:
    user_name, ai_name, connor_name = get_chatroom_names()
    return {"user": user_name, "aion": ai_name, "connor": connor_name}


def _player_name(player: str) -> str:
    return _player_names().get(player, DEFAULT_PLAYER_NAMES.get(player, player))


class NewGameBody(BaseModel):
    model: str = DEFAULT_MODEL


class BidBody(BaseModel):
    bid: int


class PlayBody(BaseModel):
    action: str = "play"
    cards: list[str] = []


def _card_rank(card: str) -> str:
    return card if card in ("BJ", "RJ") else card[:-1]


def _card_sort_key(card: str) -> tuple[int, int]:
    return (RANK_VALUE[_card_rank(card)], SUIT_VALUE.get(card[-1:], 0))


def _sort_cards(cards: list[str]) -> list[str]:
    return sorted(cards, key=_card_sort_key)


def _new_deck() -> list[str]:
    return [f"{rank}{suit}" for rank in RANKS[:13] for suit in SUITS] + ["BJ", "RJ"]


def _save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_state() -> Optional[dict]:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _make_game(model: str = DEFAULT_MODEL) -> dict:
    deck = _new_deck()
    random.shuffle(deck)
    ai_order = ["aion", "connor"]
    random.shuffle(ai_order)
    turn_order = ["user", *ai_order]
    hands = {
        "user": _sort_cards(deck[:17]),
        "aion": _sort_cards(deck[17:34]),
        "connor": _sort_cards(deck[34:51]),
    }
    bottom = _sort_cards(deck[51:])
    return {
        "id": f"ddz_{int(time.time() * 1000)}",
        "phase": "preview",
        "players": turn_order,
        "hands": hands,
        "bottom": bottom,
        "bottom_revealed": False,
        "landlord": None,
        "current_player": "user",
        "bid_turns": 0,
        "current_bid": 0,
        "bids": [],
        "last_play": None,
        "passes": [],
        "played_cards": [],
        "history": [],
        "winner": None,
        "model": model,
        "created_at": time.time(),
        "updated_at": time.time(),
    }


def _turn_order(state: dict) -> list[str]:
    order = state.get("players") or PLAYERS
    return [p for p in order if p in PLAYERS] or PLAYERS


def _next_player(state: dict, player: str) -> str:
    order = _turn_order(state)
    return order[(order.index(player) + 1) % len(order)]


def _bomb_count(state: dict) -> int:
    # 完整出牌历史随牌局保存，旧牌局也可以恢复倍率；只统计成功出的炸弹。
    return sum(
        1 for entry in state.get("history", [])
        if entry.get("event") == "play"
        and _is_bomb_move(classify_cards(entry.get("cards", [])) or {})
    )


def _public_state(state: dict, for_player: Optional[str] = None) -> dict:
    bomb_count = _bomb_count(state)
    players = {}
    for p in PLAYERS:
        hand = state["hands"].get(p, [])
        players[p] = {
            "id": p,
            "name": _player_name(p),
            "role": "landlord" if state.get("landlord") == p else "farmer",
            "handCount": len(hand),
        }
        if p == (for_player or "user"):
            players[p]["hand"] = _sort_cards(hand)

    return {
        "id": state["id"],
        "phase": state["phase"],
        "players": players,
        "order": _turn_order(state),
        "currentPlayer": state.get("current_player"),
        "currentBid": state.get("current_bid", 0),
        "bombCount": bomb_count,
        "multiplier": 2 ** bomb_count,
        "bids": state.get("bids", []),
        "bottomCards": state["bottom"] if state.get("bottom_revealed") else [],
        "bottomCount": len(state.get("bottom", [])),
        "landlord": state.get("landlord"),
        "lastPlay": state.get("last_play"),
        "passes": state.get("passes", []),
        "playedCards": state.get("played_cards", []),
        "history": state.get("history", [])[-80:],
        "winner": state.get("winner"),
        "settlement": _settlement(state) if state.get("phase") == "game_over" else None,
        "walletSettlement": state.get("wallet_settlement"),
        "updatedAt": state.get("updated_at"),
    }


def _settlement(state: dict) -> dict:
    remaining = {p: len(state.get("hands", {}).get(p, [])) for p in PLAYERS}
    bomb_count = _bomb_count(state)
    return {
        "winner": state.get("winner"),
        "winnerName": _player_name(state.get("winner")) if state.get("winner") else "未知",
        "landlord": state.get("landlord"),
        "landlordName": _player_name(state.get("landlord")) if state.get("landlord") else "未知",
        "remainingCounts": remaining,
        "bombCount": bomb_count,
        "multiplier": 2 ** bomb_count,
        "bottomCards": state.get("bottom", []),
        "wallet": state.get("wallet_settlement"),
    }


def _counts(cards: list[str]) -> Counter:
    return Counter(_card_rank(c) for c in cards)


def _is_consecutive(values: list[int]) -> bool:
    return bool(values) and all(values[i] + 1 == values[i + 1] for i in range(len(values) - 1))


def classify_cards(cards: list[str]) -> Optional[dict]:
    cards = _sort_cards(cards)
    n = len(cards)
    if n == 0:
        return None

    ranks = _counts(cards)
    groups = sorted(((RANK_VALUE[r], c, r) for r, c in ranks.items()), key=lambda x: x[0])
    values = [g[0] for g in groups]
    counts = sorted(ranks.values(), reverse=True)

    if n == 2 and set(cards) == {"BJ", "RJ"}:
        return {"type": "rocket", "rank": RANK_VALUE["RJ"], "length": 2, "label": "王炸"}
    if n == 4 and counts == [4]:
        return {"type": "bomb", "rank": values[0], "length": 4, "label": "炸弹"}
    if n == 1:
        return {"type": "single", "rank": values[0], "length": 1, "label": "单张"}
    if n == 2 and counts == [2]:
        return {"type": "pair", "rank": values[0], "length": 2, "label": "对子"}
    if n == 3 and counts == [3]:
        return {"type": "triple", "rank": values[0], "length": 3, "label": "三张"}
    if n == 4 and counts == [3, 1]:
        main = max((RANK_VALUE[r] for r, c in ranks.items() if c == 3))
        return {"type": "triple_single", "rank": main, "length": 4, "label": "三带一"}
    if n == 5 and counts == [3, 2]:
        main = max((RANK_VALUE[r] for r, c in ranks.items() if c == 3))
        return {"type": "triple_pair", "rank": main, "length": 5, "label": "三带一对"}

    no_two_or_joker = all(v < RANK_VALUE["2"] for v in values)
    if n >= 5 and len(groups) == n and no_two_or_joker and _is_consecutive(values):
        return {"type": "straight", "rank": values[-1], "length": n, "label": f"{n}顺"}
    if n >= 6 and n % 2 == 0 and all(c == 2 for c in ranks.values()) and no_two_or_joker and _is_consecutive(values):
        return {"type": "pair_straight", "rank": values[-1], "length": n, "chain": n // 2, "label": "连对"}

    triple_values = sorted(RANK_VALUE[r] for r, c in ranks.items() if c == 3 and RANK_VALUE[r] < RANK_VALUE["2"])
    if len(triple_values) >= 2 and _is_consecutive(triple_values):
        triple_count = len(triple_values)
        rest_counts = [c for r, c in ranks.items() if RANK_VALUE[r] not in triple_values]
        if n == triple_count * 3:
            return {"type": "airplane", "rank": triple_values[-1], "length": n, "chain": triple_count, "wing": "none", "label": "飞机"}
        if n == triple_count * 4 and all(c == 1 for c in rest_counts):
            return {"type": "airplane_single", "rank": triple_values[-1], "length": n, "chain": triple_count, "wing": "single", "label": "飞机带单"}
        if n == triple_count * 5 and all(c == 2 for c in rest_counts):
            return {"type": "airplane_pair", "rank": triple_values[-1], "length": n, "chain": triple_count, "wing": "pair", "label": "飞机带对"}

    if n == 6 and counts[0] == 4:
        main = max(RANK_VALUE[r] for r, c in ranks.items() if c == 4)
        return {"type": "four_two_singles", "rank": main, "length": 6, "label": "四带二"}
    if n == 8 and counts[0] == 4 and sorted([c for c in ranks.values() if c != 4]) == [2, 2]:
        main = max(RANK_VALUE[r] for r, c in ranks.items() if c == 4)
        return {"type": "four_two_pairs", "rank": main, "length": 8, "label": "四带两对"}

    return None


def can_beat(move: dict, last: Optional[dict]) -> bool:
    if not move:
        return False
    if not last:
        return True
    if move["type"] == "rocket":
        return last["type"] != "rocket"
    if move["type"] == "bomb":
        return last["type"] not in ("bomb", "rocket") or move["rank"] > last["rank"]
    if last["type"] in ("bomb", "rocket"):
        return False
    return (
        move["type"] == last["type"]
        and move.get("length") == last.get("length")
        and move.get("chain") == last.get("chain")
        and move["rank"] > last["rank"]
    )


def _has_cards(hand: list[str], cards: list[str]) -> bool:
    hand_counts = Counter(hand)
    for card, count in Counter(cards).items():
        if hand_counts[card] < count:
            return False
    return True


def _cards_for_rank(hand_by_rank: dict[str, list[str]], rank: str, count: int) -> list[str]:
    return hand_by_rank[rank][:count]


def _generate_candidate_moves(hand: list[str]) -> list[list[str]]:
    hand = _sort_cards(hand)
    by_rank: dict[str, list[str]] = defaultdict(list)
    for card in hand:
        by_rank[_card_rank(card)].append(card)

    moves: list[list[str]] = []
    for rank in RANKS:
        cards = by_rank.get(rank, [])
        if len(cards) >= 1:
            moves.append(_cards_for_rank(by_rank, rank, 1))
        if len(cards) >= 2:
            moves.append(_cards_for_rank(by_rank, rank, 2))
        if len(cards) >= 3:
            triple = _cards_for_rank(by_rank, rank, 3)
            moves.append(triple)
            for r in RANKS:
                if r != rank and by_rank.get(r):
                    moves.append(triple + _cards_for_rank(by_rank, r, 1))
            pair_ranks = [r for r in RANKS if r != rank and len(by_rank.get(r, [])) >= 2]
            for r in pair_ranks:
                moves.append(triple + _cards_for_rank(by_rank, r, 2))
        if len(cards) == 4:
            bomb = _cards_for_rank(by_rank, rank, 4)
            moves.append(bomb)
            others = [r for r in RANKS if r != rank and by_rank.get(r)]
            for a, b in combinations(others, 2):
                moves.append(bomb + by_rank[a][:1] + by_rank[b][:1])
            pairs = [r for r in others if len(by_rank[r]) >= 2]
            for r in pairs:
                moves.append(bomb + by_rank[r][:2])
            for a, b in combinations(pairs, 2):
                moves.append(bomb + by_rank[a][:2] + by_rank[b][:2])

    if by_rank.get("BJ") and by_rank.get("RJ"):
        moves.append(["BJ", "RJ"])

    straight_ranks = RANKS[:12]
    for start in range(len(straight_ranks)):
        run = []
        for rank in straight_ranks[start:]:
            if not by_rank.get(rank):
                break
            run.append(rank)
            if len(run) >= 5:
                moves.append([_cards_for_rank(by_rank, r, 1)[0] for r in run])

    for start in range(len(straight_ranks)):
        run = []
        for rank in straight_ranks[start:]:
            if len(by_rank.get(rank, [])) < 2:
                break
            run.append(rank)
            if len(run) >= 3:
                cards = []
                for r in run:
                    cards.extend(_cards_for_rank(by_rank, r, 2))
                moves.append(cards)

    for start in range(len(straight_ranks)):
        run = []
        for rank in straight_ranks[start:]:
            if len(by_rank.get(rank, [])) < 3:
                break
            run.append(rank)
            if len(run) >= 2:
                cards = []
                for r in run:
                    cards.extend(_cards_for_rank(by_rank, r, 3))
                moves.append(cards)
                others = [r for r in RANKS if r not in run and by_rank.get(r)]
                # Match the table's existing rule: single wings have distinct ranks.
                for wings in combinations(others, len(run)):
                    moves.append(cards + [by_rank[r][0] for r in wings])
                pairs = [r for r in others if len(by_rank[r]) >= 2]
                for wings in combinations(pairs, len(run)):
                    moves.append(cards + [c for r in wings for c in by_rank[r][:2]])

    seen = set()
    unique = []
    for cards in moves:
        cards = tuple(_sort_cards(cards))
        if cards not in seen:
            seen.add(cards)
            unique.append(list(cards))
    return unique


def legal_moves(state: dict, player: str) -> list[dict]:
    hand = state["hands"].get(player, [])
    last = state.get("last_play")
    moves = []
    if last and last.get("player") != player:
        moves.append({"id": "pass", "action": "pass", "cards": [], "label": "不出"})

    for cards in _generate_candidate_moves(hand):
        move_type = classify_cards(cards)
        if not move_type:
            continue
        if last and last.get("player") != player and not can_beat(move_type, last):
            continue
        moves.append({
            "id": f"m{len(moves)}",
            "action": "play",
            "cards": _sort_cards(cards),
            "type": move_type["type"],
            "label": move_type["label"],
            "rank": move_type["rank"],
        })

    moves.sort(key=lambda m: (0 if m["action"] == "pass" else len(m["cards"]), m.get("rank", -1)))
    return moves


def _is_teammate(state: dict, player: str, other: str) -> bool:
    landlord = state.get("landlord")
    return bool(landlord and player != landlord and other != landlord)


def _move_cards_left(state: dict, player: str, move: dict) -> int:
    return len(state["hands"].get(player, [])) - len(move.get("cards") or [])


def _is_bomb_move(move: dict) -> bool:
    return move.get("type") in ("bomb", "rocket")


def _opponents(state: dict, player: str) -> list[str]:
    landlord = state.get("landlord")
    if player == landlord:
        return [p for p in PLAYERS if p != player]
    return [landlord] if landlord else [p for p in PLAYERS if p != player]


def _min_count(state: dict, players: list[str]) -> int:
    counts = [len(state["hands"].get(p, [])) for p in players if p]
    return min(counts) if counts else 99


def _hand_turn_counter(hand: list[str]):
    """Minimum number of legal melds, ignoring opponents; cache only this decision."""
    patterns = set()
    for cards in _generate_candidate_moves(hand):
        if classify_cards(cards):
            counts = _counts(cards)
            patterns.add(tuple(counts[r] for r in RANKS))
    covering = [[] for _ in RANKS]
    for pattern in patterns:
        used = [(i, n) for i, n in enumerate(pattern) if n]
        for i, _ in used:
            covering[i].append((sum(pattern), used))
    for options in covering:
        options.sort(key=lambda item: (-item[0], item[1]))

    @lru_cache(maxsize=None)
    def turns(counts: tuple[int, ...]) -> int:
        total = sum(counts)
        if not total:
            return 0
        if counts in patterns:
            return 1
        pivot = next(i for i, n in enumerate(counts) if n)
        options = [(size, used) for size, used in covering[pivot]
                   if all(counts[i] >= n for i, n in used)]
        best = total
        for _, used in options:
            rest = list(counts)
            for i, n in used:
                rest[i] -= n
            best = min(best, 1 + turns(tuple(rest)))
            if best == 2:  # A one-meld hand was already handled above.
                break
        return best

    return turns


def _unseen_rank_counts(state: dict, player: str) -> Counter:
    # Only own cards and public discards. Never inspect another player's ranks.
    seen = state["hands"][player] + state.get("played_cards", [])
    return _counts(_new_deck()) - _counts(seen)


def _strategic_play_decision(state: dict, player: str, moves: list[dict]) -> dict:
    playable = [m for m in moves if m.get("action") == "play"]
    passed = {"action": "pass", "cards": [], "moveId": "pass", "speech": "这手我先不要。"}
    if not playable:
        return passed
    for move in playable:
        if _move_cards_left(state, player, move) == 0:
            return {**move, "speech": "这手能收就不拖了。"}

    hand = state["hands"][player]
    original = _counts(hand)
    turns = _hand_turn_counter(hand)
    unseen = _unseen_rank_counts(state, player)
    opponents = _opponents(state, player)
    opponent_count = _min_count(state, opponents)
    threats = {len(state["hands"].get(p, [])) for p in opponents} & {1, 2}
    teammate = next((p for p in PLAYERS if p != player and _is_teammate(state, player, p)), None)
    teammate_count = len(state["hands"].get(teammate, [])) if teammate else 99
    last = state.get("last_play")
    following = bool(last and last.get("player") != player)

    def score(move):
        used = _counts(move["cards"])
        rest = original - used
        value = -140 * turns(tuple(rest[r] for r in RANKS)) - sum(rest.values()) * 2
        value -= move.get("rank", 0) * (1.5 if following else 0.5)
        for rank, count in rest.items():
            if count == 1:
                value -= max(0, RANK_VALUE["A"] - RANK_VALUE[rank]) * 1.5
        for rank, count in used.items():
            if original[rank] == 4 and count < 4:
                value -= 180
            elif original[rank] in (2, 3) and count < original[rank]:
                value -= 18
            if RANK_VALUE[rank] >= RANK_VALUE["2"]:
                value -= count * 24
        if original["BJ"] and original["RJ"] and bool(used["BJ"]) != bool(used["RJ"]):
            value -= 180
        if _is_bomb_move(move):
            value -= 100
        return value

    def best(options):
        return max(options, key=score)

    def highest_block(options):
        normal = [m for m in options if not _is_bomb_move(m)]
        # On a reported single/pair, use the highest same-type block available.
        return max(normal, key=lambda m: (m.get("rank", 0), score(m))) if normal else best(options)

    max_opponent_count = max((len(state["hands"].get(p, [])) for p in opponents), default=0)
    for bomb in (m for m in playable if _is_bomb_move(m)):
        rest = [c for c in hand if c not in bomb["cards"]]
        if not classify_cards(rest):
            continue
        rocket_possible = max_opponent_count >= 2 and unseen["BJ"] and unseen["RJ"]
        higher_bomb_possible = max_opponent_count >= 4 and any(
            n == 4 and RANK_VALUE[r] > bomb["rank"] for r, n in unseen.items()
        )
        if bomb["type"] == "rocket" or not (rocket_possible or higher_bomb_possible):
            return {**bomb, "speech": "先拿回出牌权，下一手收尾。"}

    if following:
        if _is_teammate(state, player, last["player"]):
            landlord_next = _next_player(state, player) == state.get("landlord")
            matching_threat = last.get("length") in threats and last.get("type") in ("single", "pair")
            bigger_unseen = any(n >= last.get("length", 1) and RANK_VALUE[r] > last["rank"]
                                for r, n in unseen.items())
            if landlord_next and matching_threat and bigger_unseen:
                return {**highest_block(playable), "speech": "我来挡一下，别让地主收尾。"}
            return {**passed, "speech": "这手给队友过。"}

        urgent = opponent_count <= 2
        if urgent and last.get("type") in ("single", "pair") and last.get("length") in threats:
            return {**highest_block(playable), "speech": "这手得拦住。"}
        normal = [m for m in playable if not _is_bomb_move(m)]
        if not normal and not urgent:
            return passed
        chosen = best(normal or playable)
        remaining = original - _counts(chosen["cards"])
        if (not urgent and chosen.get("rank", 0) >= RANK_VALUE["2"]
                and turns(tuple(remaining[r] for r in RANKS)) > 1):
            return passed
        return {**chosen, "speech": "压一手，把节奏拿回来。"}

    if threats:
        controls = [m for m in playable if m.get("type") in ("single", "pair")
                    and len(m["cards"]) in threats
                    and not any(n >= len(m["cards"]) and RANK_VALUE[r] > m["rank"]
                                for r, n in unseen.items())]
        if controls:
            return {**best(controls), "speech": "先用大牌把收尾封住。"}
        # Avoid leading the exact number of cards an opponent could finish with.
        safe = [m for m in playable if len(m["cards"]) not in threats]
        chosen = best(safe) if safe else highest_block(playable)
        return {**chosen, "speech": "先封住你们的收尾。"}

    if teammate and teammate_count <= 2 and _next_player(state, player) == teammate:
        wanted = "single" if teammate_count == 1 else "pair"
        feeds = [m for m in playable if m.get("type") == wanted and m.get("rank", 99) < RANK_VALUE["A"]]
        if feeds:
            # Prefer an intact group; do not dismantle a bomb just to feed a low card.
            intact = [m for m in feeds if original[_card_rank(m["cards"][0])] == len(m["cards"])]
            if intact:
                chosen = min(intact, key=lambda m: m["rank"])
                return {**chosen, "speech": "递个小口，给队友接。"}

    return {**best(playable), "speech": "先把牌型顺出去。"}


def _append_history(state: dict, player: str, event: str, speech: str = "", cards: Optional[list[str]] = None, bid: Optional[int] = None):
    state.setdefault("history", []).append({
        "ts": time.time(),
        "player": player,
        "name": _player_name(player),
        "event": event,
        "speech": (speech or "").strip()[:160],
        "cards": cards or [],
        "bid": bid,
    })


def _finish_bidding(state: dict):
    best = max(state["bids"], key=lambda b: b["bid"])
    if best["bid"] <= 0:
        new_state = _make_game(state.get("model", DEFAULT_MODEL))
        state.clear()
        state.update(new_state)
        _append_history(state, "user", "redeal", "这一轮都不叫，重新发牌；你可以先看手牌再开局。")
        return

    landlord = best["player"]
    state["landlord"] = landlord
    state["bottom_revealed"] = True
    state["hands"][landlord] = _sort_cards(state["hands"][landlord] + state["bottom"])
    state["phase"] = "playing"
    state["current_player"] = landlord
    _append_history(state, landlord, "landlord", f"{_player_name(landlord)} 成为地主，底牌公开。", state["bottom"], best["bid"])


def apply_bid(state: dict, player: str, bid: int, speech: str = ""):
    if state["phase"] != "bidding" or state["current_player"] != player:
        raise ValueError("现在不是该玩家叫分")
    bid = max(0, min(3, int(bid)))
    if bid != 0 and bid <= state.get("current_bid", 0):
        bid = 0

    state["bids"].append({"player": player, "name": _player_name(player), "bid": bid, "speech": speech})
    state["current_bid"] = max(state.get("current_bid", 0), bid)
    state["bid_turns"] += 1
    _append_history(state, player, "bid", speech or ("不叫" if bid == 0 else f"叫 {bid} 分"), bid=bid)

    if bid == 3 or state["bid_turns"] >= 3:
        _finish_bidding(state)
    else:
        state["current_player"] = _next_player(state, player)


def apply_play(state: dict, player: str, action: str, cards: list[str], speech: str = ""):
    if state["phase"] != "playing" or state["current_player"] != player:
        raise ValueError("现在不是该玩家出牌")

    action = (action or "play").lower()
    if action == "pass":
        if not state.get("last_play") or state["last_play"].get("player") == player:
            raise ValueError("当前轮次不能不出")
        state.setdefault("passes", []).append(player)
        _append_history(state, player, "pass", speech or "不出")
        if len(set(state["passes"])) >= 2:
            starter = state["last_play"]["player"]
            state["current_player"] = starter
            state["last_play"] = None
            state["passes"] = []
            _append_history(state, starter, "trick_reset", f"一轮结束，{_player_name(starter)} 重新出牌。")
        else:
            state["current_player"] = _next_player(state, player)
        return

    cards = _sort_cards(cards)
    if not cards:
        raise ValueError("没有选择要出的牌")
    if not _has_cards(state["hands"][player], cards):
        raise ValueError("手牌中没有这些牌")
    move_type = classify_cards(cards)
    if not move_type:
        raise ValueError("牌型不合法")
    last = state.get("last_play")
    if last and last.get("player") != player and not can_beat(move_type, last):
        raise ValueError("这手牌压不过上一手")

    for card in cards:
        state["hands"][player].remove(card)
    state["played_cards"].extend(cards)
    state["last_play"] = {"player": player, "name": _player_name(player), "cards": cards, **move_type}
    state["passes"] = []
    _append_history(state, player, "play", speech, cards)

    if not state["hands"][player]:
        state["phase"] = "game_over"
        state["winner"] = player
        _append_history(state, player, "win", speech or f"{_player_name(player)} 出完了。")
    else:
        state["current_player"] = _next_player(state, player)


async def _latest_group_context() -> tuple[str, dict, list[dict]]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM chatroom_rooms WHERE type='group' ORDER BY updated_at DESC LIMIT 1")
        room_row = await cur.fetchone()
        if not room_row:
            return "", {"context_minutes": 30}, []
        room = dict(room_row)
        cur = await db.execute(
            "SELECT * FROM chatroom_messages WHERE room_id=? ORDER BY created_at DESC LIMIT ?",
            (room["id"], max(20, int(room.get("context_minutes", 30) or 30))),
        )
        rows = await cur.fetchall()
    msgs = []
    for r in reversed(rows):
        d = dict(r)
        try:
            d["attachments"] = json.loads(d.get("attachments") or "[]")
        except Exception:
            d["attachments"] = []
        msgs.append(d)
    return room["id"], room, msgs


async def _speak_ai_speech(game_id: str, player: str, speech: str, event_index: int):
    speech = (speech or "").strip()
    if player not in ("aion", "connor") or not speech:
        return
    cfg = load_chatroom_config()
    if not cfg.get("tts_enabled"):
        return
    voice = cfg.get("tts_aion_voice" if player == "aion" else "tts_connor_voice", "")
    if not voice:
        return
    try:
        tts = TTSStreamer(f"ddz_{game_id}_{player}_{event_index}", voice, manager)
        tts.feed(speech[:160])
        await tts.flush()
    except Exception:
        pass


def _settlement_result_card(state: dict) -> dict:
    settle = _settlement(state)
    names = _player_names()
    landlord_won = settle["winner"] == settle["landlord"]
    winners = [settle["landlord"]] if landlord_won else [p for p in PLAYERS if p != settle["landlord"]]
    deltas = (state.get("wallet_settlement") or {}).get("deltas", {})
    return {
        "type": "doudizhu_result",
        "game_id": state["id"],
        "outcome": ("地主方获胜" if landlord_won else "农民方获胜") + " · " + "、".join(names[p] for p in winners),
        "winnerName": settle["winnerName"],
        "landlordName": settle["landlordName"],
        "remaining": " · ".join(f"{names[p]} {settle['remainingCounts'][p]} 张" for p in PLAYERS if p != settle["winner"]),
        "walletLines": (state.get("wallet_settlement") or {}).get("lines") or [],
        "rows": [dict(actor=p, name=names[p], won=p in winners, delta=deltas.get(p),
                      net=f"{deltas[p]:+.2f} 元" if p in deltas else '—',
                      balance=f"{settle['remainingCounts'][p]} 张") for p in PLAYERS],
        "balanceLabel": "剩余手牌",
        "note": f"炸弹 {settle['bombCount']} 次 · 本局 {settle['multiplier']} 倍 · 用户金额仅记本桌",
    }


def _format_settlement_message(state: dict) -> str:
    card = _settlement_result_card(state)
    lines = [
        "【斗地主战报】",
        card["outcome"],
        f"先出完：{card['winnerName']}",
        f"地主：{card['landlordName']}",
        f"炸弹（含王炸）：{_bomb_count(state)} 次，本局 {2 ** _bomb_count(state)} 倍",
        "剩余手牌：" + card["remaining"],
    ]
    wallet_lines = card["walletLines"]
    if wallet_lines:
        lines.append("钱包结算：" + "；".join(wallet_lines))
    if state.get("bottom"):
        lines.append("底牌：" + " ".join(state["bottom"]))
    return "\n".join(lines)


async def _apply_wallet_settlement(state: dict):
    if state.get("wallet_settlement") or state.get("phase") != "game_over" or not state.get("winner"):
        return
    winner = state["winner"]
    landlord = state.get("landlord")
    if landlord not in PLAYERS:
        return
    remaining = {p: len(state.get("hands", {}).get(p, [])) for p in PLAYERS}
    now = time.time()
    bomb_count = _bomb_count(state)
    multiplier = 2 ** bomb_count
    records = []
    deltas = {p: 0.0 for p in PLAYERS}
    ai_wallet = {
        "aion": "wallet_ai",
        "connor": "connor_wallet_ai",
    }
    farmers = [p for p in PLAYERS if p != landlord]

    if winner == landlord:
        for farmer in farmers:
            penalty = float(remaining.get(farmer, 0)) * multiplier
            if penalty:
                deltas[farmer] -= penalty
                deltas[landlord] += penalty
    else:
        penalty = float(remaining.get(landlord, 0)) * multiplier
        if penalty:
            deltas[landlord] -= penalty
            share = penalty / len(farmers)
            for farmer in farmers:
                deltas[farmer] += share

    def fmt_amount(value: float) -> str:
        text = f"{value:.2f}".rstrip("0").rstrip(".")
        return text or "0"

    lines = []
    for p in PLAYERS:
        value = deltas[p]
        sign = "+" if value > 0 else ""
        lines.append(f"{_player_name(p)} {sign}{fmt_amount(value)} 元")

    for player, record_type in ai_wallet.items():
        amount = deltas[player]
        if not amount:
            continue
        verb = "入账" if amount > 0 else "扣款"
        records.append((
            f"ddz_{state['id']}_{player}_wallet",
            record_type,
            amount,
            f"斗地主阵营结算{verb}：{_player_name(player)} {fmt_amount(amount)} 元（炸弹 {bomb_count} 次，{multiplier} 倍）",
            now,
        ))

    state["wallet_settlement"] = {
        "rule": "阵营结算：地主赢则两个农民按各自剩牌扣款给地主；农民赢则地主按剩牌扣款并均分给两个农民。每出一次炸弹或王炸，整局金额乘 2，累乘叠加。用户金额只展示，不写入服务器钱包。",
        "winner": winner,
        "landlord": landlord,
        "remainingCounts": remaining,
        "deltas": deltas,
        "bombCount": bomb_count,
        "multiplier": multiplier,
        "records": [{"id": r[0], "recordType": r[1], "amount": r[2], "description": r[3]} for r in records],
        "lines": lines,
    }
    if not records:
        return
    async with get_db() as db:
        for rec_id, record_type, amount, description, created_at in records:
            await db.execute(
                "INSERT OR IGNORE INTO bookkeeping (id, record_type, amount, description, created_at) VALUES (?, ?, ?, ?, ?)",
                (rec_id, record_type, amount, description, created_at),
            )
        await db.commit()


async def _latest_group_room_id() -> str:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id FROM chatroom_rooms WHERE type='group' ORDER BY updated_at DESC LIMIT 1")
        row = await cur.fetchone()
    return row["id"] if row else ""


async def _trigger_group_replies(room_id: str):
    try:
        from routes.chatroom import _generate_group_replies, _load_room_and_messages

        room, msgs = await _load_room_and_messages(room_id)
        if not room or not msgs:
            return
        queue = asyncio.Queue()
        await _generate_group_replies(
            room_id,
            room,
            msgs,
            DEFAULT_MODEL,
            queue,
            int(room.get("context_minutes", 30) or 30),
        )
    except Exception:
        pass


def _game_prompt(state: dict, player: str, moves: Optional[list[dict]] = None, chosen_move: Optional[dict] = None) -> str:
    public = _public_state(state, for_player=player)
    private = {
        "you": player,
        "yourName": _player_name(player),
        "yourHand": state["hands"][player],
        "publicState": public,
    }
    if state["phase"] == "bidding":
        private["legalBids"] = [0] + [b for b in range(state.get("current_bid", 0) + 1, 4)]
        schema = {
            "action": "bid",
            "bid": "0/1/2/3 中的一个整数；0 表示不叫；非 0 必须大于当前最高叫分",
            "speech": "一句符合你人设的牌桌发言，最多 40 个中文字符",
        }
    else:
        recommended = chosen_move or _strategic_play_decision(state, player, moves or [])
        private["recommendedMove"] = {
            "moveId": recommended.get("id") or recommended.get("moveId"),
            "action": recommended.get("action"),
            "cards": recommended.get("cards", []),
            "label": recommended.get("label", "不出"),
            "reason": recommended.get("speech", ""),
        }
        schema = {
            "speech": "针对 recommendedMove 已确定的出牌或不出，说一句符合你人设的牌桌发言，最多 40 个中文字符",
        }

    return (
        "[斗地主牌局任务]\n"
        f"你正在和 {_player_name('user')}、{_player_name('aion')}、{_player_name('connor')} 玩一局真实斗地主。你必须保持原本人设和说话风格，但本次回复只允许输出一个 JSON 对象。\n"
        "不要输出 Markdown，不要解释，不要把 JSON 包在代码块里，不要泄露思考过程。禁止调用任何工具，直接回复。\n"
        "服务端会校验你的动作；你不知道其他玩家未出的手牌，只能根据自己的手牌和公共信息判断。\n\n"
        "出牌回合的动作已由本地策略确定为 recommendedMove，不能更改；你负责保持人设、接续聊天，并说出与该动作一致的台词。叫分回合仍由你根据自己的牌决定。\n\n"
        f"【必须符合的 JSON schema】\n{json.dumps(schema, ensure_ascii=False)}\n\n"
        f"【你的私有牌局信息】\n{json.dumps(private, ensure_ascii=False)}"
    )


def _extract_json_object(text: str) -> dict:
    text = (text or "").strip()
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    raise ValueError("AI 没有返回合法 JSON")


async def _collect_ai_text(messages: list[dict], player: str, model: str) -> str:
    text = ""
    if player == "aion":
        async for chunk in stream_ai(messages, model, temperature=SETTINGS.get("temperature"), max_tokens=700):
            if chunk.startswith(CLI_STATUS_PREFIX):
                continue
            text += chunk
    else:
        _connor_key = (load_chatroom_config().get("connor_model") or "Codex").strip() or "Codex"
        if _connor_key == "Codex":
            async for chunk in stream_connor_cli(messages=messages):
                if chunk.startswith(CLI_STATUS_PREFIX):
                    continue
                text += chunk
        else:
            async for chunk in stream_ai(messages, _connor_key, {}):
                if chunk.startswith(CLI_STATUS_PREFIX):
                    continue
                text += chunk
    return text


async def _ask_ai(state: dict, player: str, moves: Optional[list[dict]] = None, chosen_move: Optional[dict] = None) -> dict:
    room_id, room, msgs = await _latest_group_context()
    query = "斗地主牌局 JSON 决策"
    if player == "aion":
        messages, _ = await build_aion_group_context(
            room_id, msgs, room.get("context_minutes", 30), query,
        )
    else:
        messages, _ = await build_connor_group_context(
            room_id, msgs, room.get("context_minutes", 30), query,
        )
    messages.append({"role": "user", "content": _game_prompt(state, player, moves, chosen_move)})

    raw = await _collect_ai_text(messages, player, state.get("model") or DEFAULT_MODEL)
    try:
        return _extract_json_object(raw)
    except Exception:
        retry_messages = messages + [
            {"role": "assistant", "content": raw[:1000]},
            {"role": "user", "content": "上一次输出不是合法 JSON。请立刻只返回一个符合 schema 的 JSON 对象，不要任何额外文字。"},
        ]
        raw2 = await _collect_ai_text(retry_messages, player, state.get("model") or DEFAULT_MODEL)
        return _extract_json_object(raw2)


def _fallback_bid(state: dict, player: str) -> dict:
    hand = state["hands"][player]
    power = sum(1 for c in hand if _card_rank(c) in ("2", "BJ", "RJ"))
    bombs = sum(1 for count in _counts(hand).values() if count == 4)
    bid = 0
    if state.get("current_bid", 0) < 3 and (power + bombs * 2) >= 5:
        bid = min(3, max(state.get("current_bid", 0) + 1, 2))
    return {"action": "bid", "bid": bid, "speech": "我按牌面稳一点来。"}


async def _run_ai_step(state: dict) -> dict:
    player = state.get("current_player")
    if player not in ("aion", "connor"):
        raise ValueError("当前不是 AI 回合")

    if state["phase"] == "bidding":
        try:
            decision = await asyncio.wait_for(_ask_ai(state, player), timeout=AI_TIMEOUT_SECONDS)
        except Exception:
            decision = _fallback_bid(state, player)
        bid = int(decision.get("bid", 0) or 0)
        apply_bid(state, player, bid, str(decision.get("speech", "")))
        return decision

    moves = legal_moves(state, player)
    strategic = _strategic_play_decision(state, player, moves)
    try:
        dialogue = await asyncio.wait_for(_ask_ai(state, player, moves, strategic), timeout=AI_TIMEOUT_SECONDS)
    except Exception:
        dialogue = {}
    decision = {**strategic, "speech": str(dialogue.get("speech") or strategic.get("speech") or "")[:160]}
    apply_play(state, player, decision["action"], decision["cards"], decision["speech"])
    return decision


@router.post("/new")
async def new_game(body: NewGameBody):
    async with _state_lock:
        state = _make_game(body.model)
        _save_state(state)
        return {"ok": True, "state": _public_state(state)}


@router.post("/start")
async def start_game():
    async with _state_lock:
        state = _load_state()
        if not state:
            return {"ok": False, "error": "还没有牌局"}
        if state.get("phase") != "preview":
            return {"ok": True, "state": _public_state(state)}
        state["phase"] = "bidding"
        state["current_player"] = "user"
        state["bid_turns"] = 0
        state["current_bid"] = 0
        state["bids"] = []
        state["last_play"] = None
        state["passes"] = []
        state["updated_at"] = time.time()
        _append_history(state, "user", "start", f"确认开局，由 {_player_name('user')} 先叫地主。")
        _save_state(state)
        return {"ok": True, "state": _public_state(state)}


@router.get("/state")
async def get_state():
    async with _state_lock:
        state = _load_state()
        if not state:
            state = _make_game(DEFAULT_MODEL)
            _save_state(state)
        return {"ok": True, "state": _public_state(state)}


@router.post("/bid")
async def user_bid(body: BidBody):
    async with _state_lock:
        state = _load_state()
        if not state:
            return {"ok": False, "error": "还没有牌局"}
        try:
            apply_bid(state, "user", body.bid, "我叫。 " if body.bid else "我不叫。")
            state["updated_at"] = time.time()
            _save_state(state)
            return {"ok": True, "state": _public_state(state)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "state": _public_state(state)}


@router.post("/play")
async def user_play(body: PlayBody):
    async with _state_lock:
        state = _load_state()
        if not state:
            return {"ok": False, "error": "还没有牌局"}
        try:
            apply_play(state, "user", body.action, body.cards, "我出牌。" if body.action != "pass" else "不出。")
            await _apply_wallet_settlement(state)
            state["updated_at"] = time.time()
            _save_state(state)
            return {"ok": True, "state": _public_state(state)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "state": _public_state(state)}


@router.get("/legal-moves")
async def user_legal_moves():
    async with _state_lock:
        state = _load_state()
        if not state:
            return {"ok": False, "error": "还没有牌局", "moves": []}
        if state.get("phase") != "playing":
            return {"ok": True, "moves": [], "state": _public_state(state)}
        return {"ok": True, "moves": legal_moves(state, "user"), "state": _public_state(state)}


@router.post("/ai-step")
async def ai_step():
    async with _state_lock:
        state = _load_state()
        if not state:
            return {"ok": False, "error": "还没有牌局"}
        if state.get("phase") == "game_over":
            return {"ok": True, "state": _public_state(state)}
        if state.get("current_player") not in ("aion", "connor"):
            return {"ok": False, "error": "当前不是 AI 回合", "state": _public_state(state)}
        game_id = state.get("id")
        turn_snapshot = {
            "phase": state.get("phase"),
            "current_player": state.get("current_player"),
            "history_len": len(state.get("history", [])),
            "updated_at": state.get("updated_at"),
        }

    try:
        decision = await _run_ai_step(state)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "state": _public_state(state)}

    async with _state_lock:
        current = _load_state()
        if current and current.get("id") != game_id:
            return {"ok": True, "decision": decision, "state": _public_state(current)}
        if current and (
            current.get("phase") != turn_snapshot["phase"]
            or current.get("current_player") != turn_snapshot["current_player"]
            or len(current.get("history", [])) != turn_snapshot["history_len"]
            or current.get("updated_at") != turn_snapshot["updated_at"]
        ):
            return {"ok": True, "decision": decision, "state": _public_state(current)}
        try:
            state["updated_at"] = time.time()
            await _apply_wallet_settlement(state)
            _save_state(state)
            asyncio.create_task(
                _speak_ai_speech(state.get("id", ""), turn_snapshot["current_player"], str(decision.get("speech", "")), len(state.get("history", [])))
            )
            return {"ok": True, "decision": decision, "state": _public_state(state)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "state": _public_state(state)}


@router.post("/announce")
async def announce_result():
    async with _state_lock:
        state = _load_state()
        if not state:
            return {"ok": False, "error": "还没有牌局"}
        if state.get("phase") != "game_over" or not state.get("winner"):
            return {"ok": False, "error": "本局还没有结算", "state": _public_state(state)}
        content = _format_settlement_message(state)
        attachments = [_settlement_result_card(state), {"type": "system_model_context"}]

    room_id = await _latest_group_room_id()
    if not room_id:
        return {"ok": False, "error": "还没有可同步的群聊", "state": _public_state(state)}

    from routes.chatroom import _save_msg

    msg = await _save_msg(room_id, "system", content, f"cm_{int(time.time() * 1000)}_ddz", attachments=attachments)
    asyncio.create_task(_trigger_group_replies(room_id))
    return {"ok": True, "roomId": room_id, "message": msg, "state": _public_state(state)}
