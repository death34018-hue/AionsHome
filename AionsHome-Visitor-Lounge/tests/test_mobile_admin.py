"""The phone admin bridge is available only to an unlocked owner on the LAN."""

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from visitor_lounge import board_owner_auth, home_board, mobile_admin


def test_lan_owner_can_use_admin_and_other_origins_cannot(tmp_path, monkeypatch):
    monkeypatch.setattr(board_owner_auth, "CODE_PATH", tmp_path / "owner-code.txt")
    monkeypatch.setattr(home_board, "_require_enabled", lambda: None)
    upstream = []

    def handle(request):
        upstream.append(request)
        assert request.url.host == "127.0.0.1"
        assert request.url.port == 8002
        if request.url.path == "/admin":
            return httpx.Response(200, text="<a href='http://127.0.0.1:8080/'>回家</a>",
                                  headers={"content-type": "text/html; charset=utf-8"})
        return httpx.Response(201, json={"key": "test-key"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(mobile_admin.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handle), **kwargs))
    app = FastAPI()
    app.include_router(home_board.create_home_board_router())

    with TestClient(app, base_url="http://192.168.1.10:8080",
                    client=("192.168.1.20", 50000)) as phone:
        locked = phone.get("/admin", headers={"accept": "text/html"}, follow_redirects=False)
        assert locked.status_code == 303
        assert locked.headers["location"] == "/lounge-board"
        assert phone.post("/api/lounge-board/unlock", json={
            "code": board_owner_auth.owner_code(),
        }).status_code == 200
        page = phone.get("/admin")
        assert page.status_code == 200
        assert "href='/'" in page.text
        assert page.headers["cache-control"] == "no-store"
        assert phone.post("/admin/api/invitations", headers={"origin": "https://other.example"},
                          json={"visitor_kind": "human"}).status_code == 403
        created = phone.post("/admin/api/invitations", headers={"origin": "http://192.168.1.10:8080"},
                             json={"visitor_kind": "human"})
        assert created.status_code == 201
        assert created.json()["key"] == "test-key"
        assert phone.get("/admin", headers={"x-forwarded-for": "192.168.1.20"}).status_code == 403
        assert len(upstream) == 2

    with TestClient(app, base_url="https://home.example",
                    client=("127.0.0.1", 50000)) as tunnel:
        tunnel.cookies.set(board_owner_auth.COOKIE, board_owner_auth.owner_cookie())
        assert tunnel.get("/admin").status_code == 403
        assert len(upstream) == 2
