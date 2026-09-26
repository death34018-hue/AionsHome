"""Verified frontend bytes must survive the external proxy unchanged."""
import ast
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response


def test_frontend_transport_keeps_cache_policy_and_disallows_proxy_rewrites():
    # Load the actual middleware without starting unrelated application services.
    tree = ast.parse(Path(__file__).with_name("main.py").read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == "NoCacheStaticMiddleware")
    scope = {"BaseHTTPMiddleware": BaseHTTPMiddleware, "Request": Request,
             "Response": Response, "_LOCAL_PREFIXES": ("127.",)}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), "main.py", "exec"), scope)
    app = FastAPI()
    app.add_middleware(scope["NoCacheStaticMiddleware"])

    @app.get("/")
    def home():
        return HTMLResponse("<html>exact frontend bytes</html>",
                            headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    @app.get("/static/app.js")
    def script():
        return Response("window.ready = true;", media_type="application/javascript")

    @app.get("/api/value")
    def value():
        return {"ok": True}

    with TestClient(app) as client:
        home = client.get("/")
        assert home.text == "<html>exact frontend bytes</html>"
        assert home.headers["cache-control"] == "no-cache, no-store, must-revalidate, no-transform"
        script = client.get("/static/app.js")
        assert script.text == "window.ready = true;"
        assert script.headers["cache-control"] == "public, max-age=0, must-revalidate, no-transform"
        assert "cache-control" not in client.get("/api/value").headers
