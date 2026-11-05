"""Kitchen endpoints; manual records remain usable with the AI switch off."""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import kitchen

router = APIRouter(prefix='/api/kitchen', tags=['kitchen'])


class Toggle(BaseModel):
    enabled: bool


class PlanRequest(BaseModel):
    actor: str = Field(default='aion', pattern='^(aion|connor)$')
    meal: str = Field(default='', pattern='^(|lunch|dinner)$')
    proposal_id: str = Field(default='', max_length=80)


class ConfirmRequest(BaseModel):
    meal: str = Field(default='', pattern='^(|lunch|dinner)$')
    serve_as: str = Field(default='', pattern='^(|lunch|dinner)$')
    reselect: bool = False
    expected_updated_at: float | None = None
    expected_selection_at: float | None = None


@router.get('')
async def get_kitchen():
    from chatroom import get_chatroom_names
    names = get_chatroom_names()
    return {**await kitchen.snapshot(as_cards=True), 'names': dict(zip(('user','aion','connor'), names))}


@router.put('/enabled')
async def toggle_kitchen(body: Toggle):
    result = await kitchen.set_enabled(body.enabled)
    await kitchen.notify()
    return result


@router.get('/status')
async def kitchen_status():
    async with kitchen.get_db() as db:
        count = (await (await db.execute('SELECT count(*) FROM kitchen_pending WHERE resolved_at IS NULL')).fetchone())[0]
    return {'enabled': kitchen.enabled(), 'pending_count': count}


@router.post('/items')
async def save_item(body: dict):
    try:
        result = await kitchen.upsert_item(body)
    except ValueError as error:
        raise HTTPException(422, str(error))
    await kitchen.notify()
    return result


@router.delete('/items/{item_id}')
async def remove_item(item_id: str):
    await kitchen.delete_item(item_id)
    await kitchen.notify()
    return {'ok': True}


@router.post('/batch')
async def batch_items(body: dict):
    try:
        result = await kitchen.bulk_items(body.get('text', ''))
    except ValueError as error:
        raise HTTPException(422, str(error))
    await kitchen.notify()
    return {'items': result}


@router.put('/preferences')
async def preferences(body: dict):
    try:
        result = await kitchen.save_preferences(body)
    except (ValueError, TypeError):
        raise HTTPException(422, '请检查用餐人数和制作时间')
    await kitchen.notify()
    return result


@router.post('/pending/{pending_id}')
async def resolve(pending_id: str, body: dict):
    try:
        await kitchen.resolve_pending(pending_id, body)
    except ValueError as error:
        raise HTTPException(422, str(error))
    await kitchen.notify()
    return {'ok': True}


@router.get('/menus/{menu_id}')
async def get_menu(menu_id: str):
    menu = await kitchen.get_menu(menu_id, as_card=True)
    if not menu:
        raise HTTPException(410, '这份菜单已过期')
    from chatroom import get_chatroom_names
    names = get_chatroom_names()
    menu['names'] = dict(zip(('user','aion','connor'),names))
    menu['actor_name'] = names[2 if menu['actor'] == 'connor' else 1]
    menu['ai_enabled'] = kitchen.enabled()
    return menu


@router.post('/plan')
async def plan(body: PlanRequest):
    if not kitchen.enabled():
        raise HTTPException(409, '请先开启小厨房 AI 功能')
    from kitchen_chat import generate_plan
    try:
        return await generate_plan(actor=body.actor, meal=body.meal, proposal_id=body.proposal_id)
    except kitchen.MenuChanged as error:
        raise HTTPException(409, str(error))
    except (ValueError, RuntimeError) as error:
        raise HTTPException(422, str(error))


@router.post('/menus/{menu_id}/confirm')
async def confirm_menu(menu_id: str, body: ConfirmRequest | None = None):
    body = body or ConfirmRequest()
    try:
        menu = await kitchen.confirm_menu(menu_id,meal=body.meal,serve_as=body.serve_as,reselect=body.reselect,
                    expected_updated_at=body.expected_updated_at,expected_selection_at=body.expected_selection_at)
    except kitchen.MenuChanged as error:
        raise HTTPException(409, str(error))
    except ValueError as error:
        raise HTTPException(422, str(error))
    await kitchen.notify()
    return menu


@router.delete('/menus/{menu_id}')
async def remove_menu(menu_id: str, meal: str = Query(default='', pattern='^(|lunch|dinner)$'), expected_updated_at: float | None = None):
    try:
        result = await kitchen.remove_menu(menu_id,meal=meal,expected_updated_at=expected_updated_at)
    except kitchen.MenuChanged as error:
        raise HTTPException(409, str(error))
    await kitchen.notify()
    return result
