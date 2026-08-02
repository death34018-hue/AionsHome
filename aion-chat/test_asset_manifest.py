import hashlib

from asset_manifest import get_client_asset_manifest
from config import BASE_DIR, PUBLIC_DIR


def test_client_asset_manifest_is_content_addressed():
    manifest = get_client_asset_manifest()
    assert manifest["schema"] == 1
    assert len(manifest["version"]) == 20
    assert "/static/chat.js" in manifest["files"]
    assert "/public/AIIcon.png" in manifest["files"]

    chat_js = BASE_DIR / "static" / "chat.js"
    expected = hashlib.sha256(chat_js.read_bytes()).hexdigest()
    assert manifest["files"]["/static/chat.js"]["sha256"] == expected


def test_client_asset_manifest_excludes_large_or_navigable_content():
    manifest = get_client_asset_manifest()
    paths = set(manifest["files"])
    assert not any(path.startswith("/public/wallpaper/") for path in paths)
    assert not any(path.endswith(".html") for path in paths)
    assert all((PUBLIC_DIR / path.removeprefix("/public/")).is_file()
               for path in paths if path.startswith("/public/"))
