"""Key-scoped visitor board endpoints; no live-chat or model dependency."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from visitor_lounge.board import (
    BoardInvalidInput, BoardRepository, BoardRequestConflict,
    BoardThreadClosed, BoardThreadNotFound,
)


class NewThread(BaseModel):
    title: str = Field(max_length=100)
    author_name: str = Field(max_length=80)
    content: str = Field(max_length=1000)
    request_id: str = Field(max_length=128)
    addressed_to: str | None = Field(default=None, max_length=80)


class NewPost(BaseModel):
    author_name: str = Field(max_length=80)
    content: str = Field(max_length=1000)
    request_id: str = Field(max_length=128)
    addressed_to: str | None = Field(default=None, max_length=80)


class CloseThread(BaseModel):
    author_name: str = Field(max_length=80)


def board_error(error: Exception) -> HTTPException:
    if isinstance(error, BoardThreadNotFound):
        return HTTPException(status_code=404, detail="留言条不存在")
    if isinstance(error, (BoardThreadClosed, BoardRequestConflict)):
        return HTTPException(status_code=409, detail="留言条已结束或请求标识冲突")
    return HTTPException(status_code=422, detail=str(error))


def visitor_board_router(board: BoardRepository, require_session, repository, settings) -> APIRouter:
    router = APIRouter(prefix="/api/board")

    def visitor_id(session=Depends(require_session)) -> str:
        if not settings.board_enabled:
            raise HTTPException(status_code=403, detail="留言板暂未开放")
        board.resume_visitor(session.visitor_id)
        visitor = repository.visitor(session.visitor_id)
        if visitor.status != "active" or visitor.display_name is None:
            raise HTTPException(status_code=403, detail="请先完成访客登记")
        return session.visitor_id

    @router.get("/threads")
    def list_threads(vid: str = Depends(visitor_id)):
        return board.list_threads(vid)

    @router.get("/threads/{thread_id}")
    def get_thread(thread_id: str, vid: str = Depends(visitor_id)):
        try:
            return board.get_thread(vid, thread_id)
        except BoardThreadNotFound as error:
            raise board_error(error) from None

    @router.post("/threads", status_code=201)
    def create_thread(body: NewThread, vid: str = Depends(visitor_id)):
        try:
            return board.create_thread(vid, **body.model_dump())
        except (BoardInvalidInput, BoardRequestConflict) as error:
            raise board_error(error) from None

    @router.post("/threads/{thread_id}/posts", status_code=201)
    def reply(thread_id: str, body: NewPost, vid: str = Depends(visitor_id)):
        try:
            return board.reply(vid, thread_id, **body.model_dump())
        except (BoardInvalidInput, BoardRequestConflict, BoardThreadClosed, BoardThreadNotFound) as error:
            raise board_error(error) from None

    @router.post("/threads/{thread_id}/close")
    def close(thread_id: str, body: CloseThread, vid: str = Depends(visitor_id)):
        try:
            return board.close_thread(vid, thread_id, **body.model_dump())
        except (BoardInvalidInput, BoardThreadNotFound) as error:
            raise board_error(error) from None

    return router
