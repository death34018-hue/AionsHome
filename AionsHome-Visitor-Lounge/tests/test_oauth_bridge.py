from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.templating import Jinja2Templates
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
import pytest

from visitor_lounge.repository import VisitorRepository
from visitor_lounge.security import KeyService
from visitor_lounge.settings import Settings


NOW = datetime(2026, 8, 9, 10, 0, tzinfo=timezone.utc)
RESOURCE = "https://visitor.aionshome.com/mcp"
SCOPE = "visitor:lounge"


def _settings(tmp_path, database) -> Settings:
    return Settings(
        root=tmp_path,
        database_path=database.path,
        visitor_host="127.0.0.1",
        visitor_port=8001,
        admin_host="127.0.0.1",
        admin_port=8002,
        max_generations=1,
        max_generations_hard_limit=2,
        max_waiting=3,
        queue_timeout_seconds=120,
        generation_timeout_seconds=120,
        key_pepper=b"oauth-key-pepper",
        master_key=Fernet.generate_key(),
        session_secret=b"oauth-session-secret",
        codex_workdir=tmp_path / "codex-workdir",
    )


def _client(client_id: str = "chatgpt-client") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="client-secret",
        client_secret_expires_at=0,
        redirect_uris=[AnyUrl("https://chatgpt.com/connector/oauth/callback")],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=SCOPE,
    )


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def test_oauth_schema_cascades_visitor_credentials(database):
    database.initialize()
    tables_expected = {
        "oauth_clients",
        "oauth_pending_authorizations",
        "oauth_authorization_codes",
        "oauth_tokens",
    }
    with database.connection() as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert tables_expected <= tables


@pytest.fixture
def oauth_harness(tmp_path, database):
    from visitor_lounge.oauth_provider import VisitorOAuthProvider

    database.initialize()
    settings = _settings(tmp_path, database)
    visitors = VisitorRepository(database)
    keys = KeyService(visitors, settings)
    visitor_id = visitors.create_unclaimed_visitor()
    raw_key = keys.create(visitor_id).value
    provider = VisitorOAuthProvider(
        database=database,
        settings=settings,
        keys=keys,
        clock=lambda: NOW,
        public_origin="https://visitor.aionshome.com",
        resource=RESOURCE,
        scope=SCOPE,
    )
    return provider, visitors, keys, visitor_id, raw_key, database


def test_oauth_code_exchange_and_refresh_rotation_share_visitor_identity(oauth_harness):
    provider, _, _, visitor_id, raw_key, _ = oauth_harness
    client = _client()
    asyncio.run(provider.register_client(client))
    assert asyncio.run(provider.get_client(client.client_id)).client_id == client.client_id

    params = AuthorizationParams(
        state="chatgpt-state",
        scopes=[SCOPE],
        code_challenge=_challenge("verifier"),
        redirect_uri=AnyUrl("https://chatgpt.com/connector/oauth/callback"),
        redirect_uri_provided_explicitly=True,
        resource=RESOURCE,
    )
    consent_url = asyncio.run(provider.authorize(client, params))
    request_token = parse_qs(urlparse(consent_url).query)["request"][0]
    redirect_url = provider.complete_authorization(request_token, raw_key)
    redirect_query = parse_qs(urlparse(redirect_url).query)
    assert redirect_query["state"] == ["chatgpt-state"]

    code = redirect_query["code"][0]
    loaded_code = asyncio.run(provider.load_authorization_code(client, code))
    issued = asyncio.run(provider.exchange_authorization_code(client, loaded_code))
    access = asyncio.run(provider.load_access_token(issued.access_token))
    assert access.subject == visitor_id
    assert access.resource == RESOURCE
    assert access.scopes == [SCOPE]

    refresh = asyncio.run(provider.load_refresh_token(client, issued.refresh_token))
    rotated = asyncio.run(provider.exchange_refresh_token(client, refresh, [SCOPE]))
    assert asyncio.run(provider.load_refresh_token(client, issued.refresh_token)) is None
    assert asyncio.run(provider.load_access_token(issued.access_token)) is None
    assert asyncio.run(provider.load_access_token(rotated.access_token)).subject == visitor_id


def test_oauth_remains_usable_after_visit_ends(oauth_harness):
    provider, visitors, _, visitor_id, raw_key, _ = oauth_harness
    client = _client()
    asyncio.run(provider.register_client(client))
    params = AuthorizationParams(
        state=None,
        scopes=[SCOPE],
        code_challenge=_challenge("verifier"),
        redirect_uri=AnyUrl("https://chatgpt.com/connector/oauth/callback"),
        redirect_uri_provided_explicitly=True,
        resource=RESOURCE,
    )
    request_token = parse_qs(
        urlparse(asyncio.run(provider.authorize(client, params))).query
    )["request"][0]
    code = parse_qs(
        urlparse(provider.complete_authorization(request_token, raw_key)).query
    )["code"][0]
    issued = asyncio.run(
        provider.exchange_authorization_code(
            client, asyncio.run(provider.load_authorization_code(client, code))
        )
    )

    visitors.end_visit(visitor_id, NOW)

    assert visitors.visitor(visitor_id).status == "suspended"
    assert asyncio.run(provider.load_access_token(issued.access_token)).subject == visitor_id
    refresh = asyncio.run(provider.load_refresh_token(client, issued.refresh_token))
    rotated = asyncio.run(provider.exchange_refresh_token(client, refresh, [SCOPE]))
    assert asyncio.run(provider.load_access_token(rotated.access_token)).subject == visitor_id

    reconnect_request = parse_qs(
        urlparse(asyncio.run(provider.authorize(client, params))).query
    )["request"][0]
    reconnect_code = parse_qs(
        urlparse(provider.complete_authorization(reconnect_request, raw_key)).query
    )["code"][0]
    loaded_code = asyncio.run(provider.load_authorization_code(client, reconnect_code))
    reconnected = asyncio.run(provider.exchange_authorization_code(client, loaded_code))
    assert asyncio.run(provider.load_access_token(reconnected.access_token)).subject == visitor_id


def test_oauth_provider_keeps_static_key_and_invalidates_grant_on_key_rotation(oauth_harness):
    provider, _, keys, visitor_id, raw_key, _ = oauth_harness
    direct = asyncio.run(provider.load_access_token(raw_key))
    assert direct.subject == visitor_id
    assert direct.client_id == "visitor-key"

    client = _client()
    asyncio.run(provider.register_client(client))
    params = AuthorizationParams(
        state=None,
        scopes=[SCOPE],
        code_challenge=_challenge("verifier"),
        redirect_uri=AnyUrl("https://chatgpt.com/connector/oauth/callback"),
        redirect_uri_provided_explicitly=True,
        resource=RESOURCE,
    )
    request_token = parse_qs(
        urlparse(asyncio.run(provider.authorize(client, params))).query
    )["request"][0]
    code = parse_qs(
        urlparse(provider.complete_authorization(request_token, raw_key)).query
    )["code"][0]
    issued = asyncio.run(
        provider.exchange_authorization_code(
            client, asyncio.run(provider.load_authorization_code(client, code))
        )
    )
    assert asyncio.run(provider.load_access_token(issued.access_token)).subject == visitor_id

    keys.rotate(visitor_id)
    assert asyncio.run(provider.load_access_token(issued.access_token)) is None


def test_oauth_authorize_rejects_wrong_resource_without_creating_request(oauth_harness):
    from mcp.server.auth.provider import AuthorizeError

    provider, _, _, _, _, database = oauth_harness
    client = _client()
    asyncio.run(provider.register_client(client))
    params = AuthorizationParams(
        state=None,
        scopes=[SCOPE],
        code_challenge=_challenge("verifier"),
        redirect_uri=AnyUrl("https://chatgpt.com/connector/oauth/callback"),
        redirect_uri_provided_explicitly=True,
        resource="https://visitor.aionshome.com/not-the-lounge",
    )
    with pytest.raises(AuthorizeError) as raised:
        asyncio.run(provider.authorize(client, params))
    assert raised.value.error == "invalid_target"
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM oauth_pending_authorizations"
        ).fetchone()[0] == 0


def test_oauth_client_metadata_is_encrypted_at_rest(oauth_harness):
    provider, _, _, _, _, database = oauth_harness
    client = _client("private-client-id")
    asyncio.run(provider.register_client(client))
    with database.connection() as connection:
        stored = bytes(
            connection.execute(
                "SELECT encrypted_metadata FROM oauth_clients WHERE client_id = ?",
                (client.client_id,),
            ).fetchone()[0]
        )
    assert b"client-secret" not in stored
    assert b"chatgpt.com" not in stored


def test_oauth_tokens_store_hashes_not_raw_values(oauth_harness):
    provider, _, _, _, raw_key, database = oauth_harness
    client = _client()
    asyncio.run(provider.register_client(client))
    params = AuthorizationParams(
        state=None,
        scopes=[SCOPE],
        code_challenge=_challenge("verifier"),
        redirect_uri=AnyUrl("https://chatgpt.com/connector/oauth/callback"),
        redirect_uri_provided_explicitly=True,
        resource=RESOURCE,
    )
    request_token = parse_qs(
        urlparse(asyncio.run(provider.authorize(client, params))).query
    )["request"][0]
    code = parse_qs(
        urlparse(provider.complete_authorization(request_token, raw_key)).query
    )["code"][0]
    issued = asyncio.run(
        provider.exchange_authorization_code(
            client, asyncio.run(provider.load_authorization_code(client, code))
        )
    )
    with database.connection() as connection:
        encoded_rows = json.dumps(
            [tuple(str(value) for value in row) for row in connection.execute("SELECT * FROM oauth_tokens")]
        )
    assert issued.access_token not in encoded_rows
    assert issued.refresh_token not in encoded_rows


def test_oauth_consent_page_accepts_one_visitor_key(oauth_harness, project_root):
    from visitor_lounge.oauth_routes import register_oauth_routes

    provider, _, _, _, raw_key, _ = oauth_harness
    client = _client()
    asyncio.run(provider.register_client(client))
    params = AuthorizationParams(
        state="state-from-chatgpt",
        scopes=[SCOPE],
        code_challenge=_challenge("verifier"),
        redirect_uri=AnyUrl("https://chatgpt.com/connector/oauth/callback"),
        redirect_uri_provided_explicitly=True,
        resource=RESOURCE,
    )
    request_token = parse_qs(
        urlparse(asyncio.run(provider.authorize(client, params))).query
    )["request"][0]
    app = FastAPI()
    register_oauth_routes(
        app,
        provider,
        templates=Jinja2Templates(directory=project_root / "templates"),
        host_display_name="Configured Host",
    )

    with TestClient(app) as browser:
        page = browser.get(f"/oauth/consent?request={request_token}")
        assert page.status_code == 200
        assert "Configured Host" in page.text
        assert raw_key not in page.text
        response = browser.post(
            "/oauth/consent",
            data={"request": request_token, "visitor_key": raw_key},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert response.headers["location"].startswith(
        "https://chatgpt.com/connector/oauth/callback?"
    )
    assert "state=state-from-chatgpt" in response.headers["location"]


def test_mcp_server_publishes_chatgpt_oauth_metadata(tmp_path, database):
    from visitor_lounge.container import Container
    from visitor_lounge.mcp_app import create_mcp_server
    from visitor_lounge.turn_coordinator import VisitorTurnCoordinator
    from visitor_lounge.visitor_service import VisitorService

    class NoCallAdapter:
        async def generate(self, _request):
            raise AssertionError("OAuth metadata test must not call the model")
            yield

    settings = _settings(tmp_path, database)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "persona.md").write_text("Host persona", "utf-8")
    database.initialize()
    container = Container(
        settings=settings,
        database=database,
        codex_adapter=NoCallAdapter(),
        clock=lambda: NOW,
    )
    service = VisitorService(container)
    container.visitor_service = service
    coordinator = VisitorTurnCoordinator(service)

    server, mcp_asgi, _provider = create_mcp_server(container, coordinator)

    with TestClient(mcp_asgi) as client:
        auth = client.get("/.well-known/oauth-authorization-server")
        resource = client.get("/.well-known/oauth-protected-resource/mcp")
    assert auth.status_code == 200
    assert auth.json()["registration_endpoint"] == "https://visitor.aionshome.com/register"
    assert auth.json()["code_challenge_methods_supported"] == ["S256"]
    assert resource.status_code == 200
    assert resource.json()["resource"] == RESOURCE
    assert resource.json()["scopes_supported"] == [SCOPE]

    tools = server._tool_manager.list_tools()
    assert len(tools) == 6
    for tool in tools:
        assert tool.meta["securitySchemes"] == [
            {"type": "oauth2", "scopes": [SCOPE]}
        ]
    talk = next(tool for tool in tools if tool.name == "talk_to_host")
    request_id_schema = talk.parameters["properties"]["request_id"]
    assert request_id_schema["type"] == "string"
    assert request_id_schema["default"] == ""
    assert "request_id" not in talk.parameters["required"]
