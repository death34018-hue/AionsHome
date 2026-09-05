import hashlib

from asset_manifest import get_client_asset_manifest
from config import BASE_DIR, PUBLIC_DIR


def test_client_asset_manifest_is_content_addressed():
    manifest = get_client_asset_manifest()
    assert manifest["schema"] == 2
    assert len(manifest["version"]) == 20
    assert "/static/chat.js" in manifest["files"]
    assert "/public/AIIcon.png" in manifest["files"]

    chat_js = BASE_DIR / "static" / "chat.js"
    expected = hashlib.sha256(chat_js.read_bytes()).hexdigest()
    assert manifest["files"]["/static/chat.js"]["sha256"] == expected


def test_client_asset_manifest_excludes_large_user_content_but_includes_app_documents():
    manifest = get_client_asset_manifest()
    paths = set(manifest["files"])
    assert not any(path.startswith("/public/wallpaper/") for path in paths)
    assert "/" in paths
    assert "/memory" in paths
    assert "/moments" in paths
    assert all((PUBLIC_DIR / path.removeprefix("/public/")).is_file()
               for path in paths if path.startswith("/public/"))


def test_english_corner_document_route_is_content_addressed():
    manifest = get_client_asset_manifest()
    entry = manifest["files"]["/english-corner"]
    document = BASE_DIR / "static" / "english-corner.html"

    assert entry["category"] == "document"
    assert entry["content_type"] == "text/html"
    assert entry["sha256"] == hashlib.sha256(document.read_bytes()).hexdigest()


def test_nested_markdown_vendor_is_in_the_verified_frontend_cache():
    manifest = get_client_asset_manifest()
    asset_path = "/static/vendor/markdown-it-15.0.1.min.js"
    entry = manifest["files"][asset_path]
    assert entry["category"] == "frontend"
    assert entry["sha256"] == hashlib.sha256(
        (BASE_DIR / asset_path.lstrip("/")).read_bytes()
    ).hexdigest()
