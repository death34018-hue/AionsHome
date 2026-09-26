"""Owner-only LAN bridge to the existing loopback Visitor Lounge admin."""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from visitor_lounge.board_owner_auth import COOKIE, valid_owner_cookie


ADMIN_ORIGIN = "http://127.0.0.1:8002"
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _local_request(request: Request) -> bool:
    """Accept direct LAN traffic, or a browser on this same computer."""
    if any(name in request.headers for name in
           ("forwarded", "x-forwarded-for", "x-real-ip", "cf-connecting-ip")):
        return False
    try:
        peer = ip_address(request.client.host if request.client else "")
        hostname = request.url.hostname or ""
        host = ip_address("127.0.0.1" if hostname == "localhost" else hostname)
    except ValueError:
        return False
    if host.is_loopback:
        return peer.is_loopback
    return (host.is_private and peer.is_private and not host.is_link_local
            and not peer.is_link_local and not peer.is_loopback)


def create_mobile_admin_router() -> APIRouter:
    router = APIRouter(tags=["lounge-board"])

    @router.api_route("/admin", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    @router.api_route("/admin/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def admin_bridge(request: Request, rest: str = ""):
        if not _local_request(request):
            raise HTTPException(status_code=403, detail="请从家里的 Wi-Fi 地址打开后台")
        if not valid_owner_cookie(request.cookies.get(COOKIE)):
            if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                return RedirectResponse("/lounge-board", status_code=303)
            raise HTTPException(status_code=401, detail="请先解锁家里的留言板")
        if request.method not in {"GET", "HEAD"}:
            origin = request.headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                if (parsed.scheme.casefold() != request.url.scheme.casefold()
                        or parsed.netloc.casefold() != request.headers.get("host", "").casefold()):
                    raise HTTPException(status_code=403, detail="拒绝跨站管理操作")
            if request.headers.get("sec-fetch-site") == "cross-site":
                raise HTTPException(status_code=403, detail="拒绝跨站管理操作")

        path = request.url.path
        query = request.url.query
        target = ADMIN_ORIGIN + path + ("?" + query if query else "")
        forwarded_headers = {name: request.headers[name] for name in ("content-type", "accept")
                             if name in request.headers}
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                upstream = await client.request(request.method, target,
                                                headers=forwarded_headers,
                                                content=await request.body())
        except httpx.HTTPError:
            raise HTTPException(status_code=503, detail="会客室后台尚未启动") from None

        body = upstream.content
        content_type = upstream.headers.get("content-type", "")
        if "text/html" in content_type:
            body = body.replace(b"http://127.0.0.1:8080/", b"/")
        headers = {**NO_STORE}
        for name in ("content-type", "content-disposition", "set-cookie", "location"):
            if name in upstream.headers:
                headers[name] = upstream.headers[name]
        return Response(content=body, status_code=upstream.status_code, headers=headers)

    return router
