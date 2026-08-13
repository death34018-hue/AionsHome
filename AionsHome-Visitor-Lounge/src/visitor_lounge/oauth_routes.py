"""Browser consent routes for one-time Visitor Key OAuth linking."""

from __future__ import annotations

from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from visitor_lounge.oauth_provider import InvalidOAuthConsent, VisitorOAuthProvider
from visitor_lounge.security import RateLimitExceeded


def register_oauth_routes(
    app: FastAPI,
    provider: VisitorOAuthProvider,
    *,
    templates: Jinja2Templates,
    host_display_name: str,
) -> None:
    def render(
        request: Request,
        request_token: str,
        *,
        error: str = "",
        status_code: int = 200,
    ):
        return templates.TemplateResponse(
            request=request,
            name="oauth_consent.html",
            context={
                "request_token": request_token,
                "host_name": host_display_name,
                "error": error,
            },
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/oauth/consent", include_in_schema=False)
    async def oauth_consent_page(request: Request):
        request_token = request.query_params.get("request", "")
        if not 1 <= len(request_token) <= 256 or not provider.pending_authorization(
            request_token
        ):
            return render(
                request,
                "",
                error="授权请求无效或已过期，请返回 ChatGPT 重新连接。",
                status_code=400,
            )
        return render(request, request_token)

    @app.post("/oauth/consent", include_in_schema=False)
    async def oauth_consent_submit(request: Request):
        raw_body = await request.body()
        values = parse_qs(raw_body.decode("utf-8", errors="replace"))
        request_token = (values.get("request") or [""])[0]
        visitor_key = (values.get("visitor_key") or [""])[0]
        if not 1 <= len(request_token) <= 256 or not 1 <= len(visitor_key) <= 256:
            return render(
                request,
                request_token if len(request_token) <= 256 else "",
                error="授权请求或 Visitor Key 无效。",
                status_code=400,
            )
        try:
            redirect_uri = provider.complete_authorization(
                request_token,
                visitor_key,
            )
        except (InvalidOAuthConsent, RateLimitExceeded):
            return render(
                request,
                request_token,
                error="授权请求或 Visitor Key 无效，请检查后再试。",
                status_code=400,
            )
        return RedirectResponse(
            redirect_uri,
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )
