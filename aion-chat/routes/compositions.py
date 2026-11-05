"""Read saved scores and update favorites independently of the prompt switch."""

import hashlib
import json

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from compositions import get_composition, list_compositions, set_favorite


router = APIRouter(prefix='/api/compositions', tags=['compositions'])


class FavoriteBody(BaseModel):
    favorite: bool


@router.get('')
async def index(request: Request):
    from chatroom import get_chatroom_names
    _, primary, companion = get_chatroom_names()
    body = json.dumps({'compositions': await list_compositions(), 'authors': {'aion': primary, 'connor': companion}}, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')
    etag = '"' + hashlib.sha256(body).hexdigest()[:20] + '"'
    headers = {'ETag': etag, 'Cache-Control': 'private, no-cache'}
    if request.headers.get('If-None-Match') == etag:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type='application/json', headers=headers)


@router.get('/{piece_id}')
async def detail(piece_id: str):
    piece = await get_composition(piece_id)
    if piece is None:
        raise HTTPException(404, '这首小曲暂时找不到了')
    return piece


@router.patch('/{piece_id}')
async def favorite(piece_id: str, body: FavoriteBody):
    if not await set_favorite(piece_id, body.favorite):
        raise HTTPException(404, '这首小曲暂时找不到了')
    return await get_composition(piece_id)
