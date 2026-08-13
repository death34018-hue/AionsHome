"""FastAPI boundary for the focused, visitor-only lounge experience."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from visitor_lounge.background import BackgroundCoordinator
from visitor_lounge.container import Container
from visitor_lounge.mcp_app import create_mcp_server
from visitor_lounge.oauth_routes import register_oauth_routes
from visitor_lounge.prompts import PromptBudgetExceeded, VisitorInputTooLong
from visitor_lounge.quota import QuotaExhausted, RequestConflict, VisitorHasPendingJob
from visitor_lounge.repository import VisitorAlreadyClaimed, VisitorRepository
from visitor_lounge.scheduler import QueueFull, SchedulerShuttingDown, VisitorAlreadyQueued
from visitor_lounge.security import (
    InvalidVisitorName,
    KeyService,
    RateLimitExceeded,
    SessionService,
    normalize_visitor_name,
)
from visitor_lounge.settings import Settings
from visitor_lounge.turn_coordinator import (
    VisitorTurnCoordinator,
    VisitorTurnQueueFull,
)
from visitor_lounge.visitor_service import (
    JobAccessDenied,
    LoungeClosed,
    SensitiveCredentialInput,
    VisitorNotClaimed,
    VisitorService,
    VisitorUnavailable,
)


COOKIE_NAME = "visitor_session"
GENERIC_LOGIN_ERROR = "登录信息无效或已不可用"
LIFECYCLE_TIMEOUT_SECONDS = 5.0
MAX_PUBLIC_REQUEST_BODY_BYTES = 16 * 1024
logger = logging.getLogger(__name__)


class _RequestBodyTooLarge(Exception):
    pass


class PublicRequestBodyLimitMiddleware:
    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_content_length = headers.get(b"content-length")
        if raw_content_length is not None:
            try:
                declared_length = int(raw_content_length)
            except ValueError:
                declared_length = self.max_bytes + 1
            if declared_length > self.max_bytes:
                response = JSONResponse(
                    {"detail": "请求内容过大"}, status_code=413
                )
                await response(scope, receive, send)
                return

        received = 0
        response_started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _RequestBodyTooLarge()
            return message

        async def tracked_send(message) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLarge:
            if response_started:
                raise
            response = JSONResponse({"detail": "请求内容过大"}, status_code=413)
            await response(scope, receive, send)


def _request_is_https(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    original_scheme = forwarded_proto.split(",", 1)[0].strip().casefold()
    return request.url.scheme.casefold() == "https" or original_scheme == "https"


def _visitor_ui_context(settings: Settings) -> dict[str, str]:
    return {
        "host_name": settings.host_display_name,
        "standby_name": settings.standby_display_name,
        "host_avatar": "/static/avatars/host.jpg",
        "standby_avatar": "/static/avatars/standby.png",
    }


def _consume_task_result(task: asyncio.Task[object]) -> None:
    if not task.cancelled():
        task.exception()


async def _bounded_lifecycle(operation, name: str) -> None:
    task = asyncio.create_task(operation, name=f"visitor-lounge-{name}")
    done, _ = await asyncio.wait({task}, timeout=LIFECYCLE_TIMEOUT_SECONDS)
    if task not in done:
        task.cancel()
        task.add_done_callback(_consume_task_result)
        raise TimeoutError(f"{name} exceeded lifecycle deadline")
    task.result()


async def _stop_safely(operation, name: str) -> None:
    try:
        await _bounded_lifecycle(operation, name)
    except BaseException as error:
        logger.error(
            "visitor lounge lifecycle cleanup failed; operation=%s error_type=%s",
            name,
            type(error).__name__,
        )


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    key: str = Field(min_length=1, max_length=4096)
    device_id: str = Field(default="browser", min_length=1, max_length=128)


class ClaimBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(min_length=1, max_length=200)
    consent: bool


class SendMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    request_id: str = Field(min_length=1, max_length=128)
    text: str


def create_visitor_app(container: Container) -> FastAPI:
    repository = VisitorRepository(container.database)
    keys = KeyService(repository, container.settings)
    sessions = SessionService(repository, container.settings)
    service = container.visitor_service or VisitorService(container)
    background = container.background or BackgroundCoordinator(
        database=container.database,
        prompt_builder=service.prompt_builder,
        scheduler=service.scheduler,
        clock=container.clock,
    )
    container.visitor_service = service
    container.background = background
    coordinator = container.turn_coordinator or VisitorTurnCoordinator(service)
    container.turn_coordinator = coordinator
    mcp_server, mcp_asgi, oauth_provider = create_mcp_server(container, coordinator)
    project_root = Path(__file__).resolve().parents[2]
    templates = Jinja2Templates(directory=project_root / "templates")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        container.startup_recovery()
        service_started = False
        background_started = False
        async with mcp_server.session_manager.run():
            try:
                await _bounded_lifecycle(service.start(), "scheduler-start")
                service_started = True
                await _bounded_lifecycle(background.start(), "background-start")
                background_started = True
                yield
            finally:
                await _stop_safely(coordinator.shutdown(), "turn-coordinator-stop")
                if background_started:
                    await _stop_safely(background.stop(), "background-stop")
                if service_started:
                    await _stop_safely(service.shutdown(), "scheduler-stop")

    app = FastAPI(title="Visitor Lounge", lifespan=lifespan)
    app.add_middleware(
        PublicRequestBodyLimitMiddleware,
        max_bytes=MAX_PUBLIC_REQUEST_BODY_BYTES,
    )
    app.state.container = container
    app.state.visitor_service = service
    app.state.background = background
    app.state.turn_coordinator = coordinator
    app.state.mcp_server = mcp_server
    app.mount("/static", StaticFiles(directory=project_root / "static"), name="static")
    register_oauth_routes(
        app,
        oauth_provider,
        templates=templates,
        host_display_name=container.settings.host_display_name,
    )

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok", "service": "visitor"}

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, __: RequestValidationError):
        return JSONResponse({"detail": "请求无效"}, status_code=422)

    async def require_session(
        visitor_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    ):
        if not visitor_session:
            raise HTTPException(status_code=401, detail="需要登录")
        session = sessions.resolve(visitor_session)
        if session is None:
            raise HTTPException(status_code=401, detail="会话已失效")
        return session

    @app.get("/")
    async def visitor_page(
        request: Request,
        visitor_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    ):
        session = sessions.resolve(visitor_session) if visitor_session else None
        if session is None:
            return templates.TemplateResponse(
                request=request,
                name="visitor_login.html",
                context=_visitor_ui_context(container.settings),
            )
        visitor = repository.visitor(session.visitor_id)
        if visitor.display_name is None:
            return templates.TemplateResponse(
                request=request,
                name="visitor_claim.html",
                context={
                    **_visitor_ui_context(container.settings),
                    "disclosure": container.settings.recording_disclosure,
                    "disclosure_version": container.settings.recording_disclosure_version,
                },
            )
        return templates.TemplateResponse(
            request=request,
            name="visitor_chat.html",
            context=service.state(session.visitor_id),
        )

    @app.post("/api/login")
    async def login(body: LoginBody, request: Request):
        try:
            visitor_id = keys.authenticate(body.key)
        except RateLimitExceeded:
            raise HTTPException(status_code=429, detail=GENERIC_LOGIN_ERROR) from None
        if visitor_id is None:
            raise HTTPException(status_code=401, detail=GENERIC_LOGIN_ERROR)
        service.record_login(visitor_id)
        raw_cookie = sessions.issue(visitor_id, body.device_id)
        visitor = repository.visitor(visitor_id)
        response = JSONResponse(
            {"next": "claim" if visitor.display_name is None else "chat"}
        )
        response.set_cookie(
            COOKIE_NAME,
            raw_cookie,
            httponly=True,
            samesite="strict",
            secure=_request_is_https(request),
            path="/",
        )
        return response

    @app.post("/api/claim")
    async def claim(body: ClaimBody, session=Depends(require_session)):
        visitor = service.effective_visitor(session.visitor_id)
        if visitor.status != "active":
            raise HTTPException(status_code=403, detail="当前会话不可认领")
        if not body.consent:
            raise HTTPException(status_code=422, detail="需要同意记录说明")
        try:
            name = normalize_visitor_name(body.name, set())
            first_welcome = service.reception.get().first_welcome.replace(
                "{访客名字}", name
            ).replace("{接待人名字}", container.settings.host_display_name)
            repository.claim_name(
                session.visitor_id,
                name,
                container.settings.recording_disclosure_version,
                greeting=first_welcome,
                greeting_at=container.clock(),
            )
        except (InvalidVisitorName, VisitorAlreadyClaimed):
            raise HTTPException(status_code=422, detail="这个名字暂时不能使用") from None
        return {"next": "chat", "visitor_name": name}

    @app.get("/api/state")
    async def state(session=Depends(require_session)):
        return service.state(session.visitor_id)

    @app.post("/api/messages", status_code=202)
    async def send_message(body: SendMessage, session=Depends(require_session)):
        try:
            ticket = await coordinator.submit(
                visitor_id=session.visitor_id,
                request_id=body.request_id,
                text=body.text,
                source="web",
            )
        except VisitorInputTooLong:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "消息不能超过 500 个字符或 600 tokens",
                    "template_text": service.reception.get().input_too_long,
                },
            ) from None
        except SensitiveCredentialInput:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "请不要发送密码、密钥或访问令牌",
                    "template_text": service.reception.get().credential_detected,
                },
            ) from None
        except PromptBudgetExceeded:
            raise HTTPException(status_code=422, detail="消息暂时无法处理") from None
        except (VisitorUnavailable, VisitorNotClaimed):
            raise HTTPException(status_code=403, detail="当前会话不能发送消息") from None
        except LoungeClosed:
            raise HTTPException(
                status_code=403,
                detail={
                    "message": "会客室暂时关闭",
                    "template_text": service.reception.get().lounge_closed,
                },
            ) from None
        except RateLimitExceeded:
            raise HTTPException(status_code=429, detail="发送过于频繁，请稍后再试") from None
        except QuotaExhausted as error:
            raise HTTPException(
                status_code=429,
                detail={
                    "message": "本轮额度已用完",
                    "reset_at": error.ends_at.isoformat(),
                    "template_text": service.reception.get().quota_exhausted,
                },
            ) from None
        except (VisitorAlreadyQueued, VisitorHasPendingJob, RequestConflict):
            raise HTTPException(status_code=409, detail="已有消息正在处理") from None
        except VisitorTurnQueueFull:
            raise HTTPException(
                status_code=409,
                detail="这位访客已有三条消息等待处理",
            ) from None
        except (QueueFull, SchedulerShuttingDown):
            raise HTTPException(status_code=503, detail="当前排队已满，请稍后再试") from None
        return {"job_id": ticket.job_id, "queue_position": ticket.position}

    @app.get("/api/jobs/{job_id}/events")
    async def stream_job(
        job_id: str,
        session=Depends(require_session),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ):
        try:
            service.assert_job_owner(job_id, session.visitor_id)
        except JobAccessDenied:
            raise HTTPException(status_code=404, detail="任务不存在") from None
        try:
            replay_after = max(0, int(last_event_id or "0"))
        except ValueError:
            replay_after = 0
        return StreamingResponse(
            service.sse_events(
                job_id, session.visitor_id, after_event_id=replay_after
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/logout")
    async def logout(
        visitor_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    ):
        if visitor_session:
            sessions.revoke(visitor_session)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE_NAME, path="/", samesite="strict")
        return response

    app.mount("/", mcp_asgi, name="mcp")
    return app


def build_visitor_app() -> FastAPI:
    """Uvicorn factory for the visitor-only process."""
    root = Path(__file__).resolve().parents[2]
    return create_visitor_app(Container.build_visitor(Settings.load(root)))
