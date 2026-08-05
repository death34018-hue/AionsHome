"""Device-bound encryption and portable cloud-route filtering."""

from __future__ import annotations

import base64
import ipaddress
import os
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .contracts import HOMECOMING_SCHEMA_VERSION, canonical_json_bytes


class InvalidDevicePublicKey(ValueError):
    pass


def load_device_public_key(public_key_spki_b64: str) -> rsa.RSAPublicKey:
    try:
        der = base64.b64decode(public_key_spki_b64, validate=True)
        public_key = serialization.load_der_public_key(der)
    except Exception as exc:
        raise InvalidDevicePublicKey("invalid DER-SPKI public key") from exc
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise InvalidDevicePublicKey("device key must be RSA")
    if public_key.key_size < 2048:
        raise InvalidDevicePublicKey("device RSA key must be at least 2048 bits")
    return public_key


def public_key_der(public_key_spki_b64: str) -> bytes:
    public_key = load_device_public_key(public_key_spki_b64)
    return public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _is_public_https(base_url: str) -> bool:
    try:
        parsed = urlparse(base_url)
        host = (parsed.hostname or "").strip().lower()
    except Exception:
        return False
    if parsed.scheme.lower() != "https" or not host:
        return False
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def _model_row(key: str, config: dict) -> dict:
    return {
        "key": key,
        "model": str(config.get("model") or key),
        "vision": bool(config.get("vision", False)),
        "audio": config.get("audio") is True,
    }


def _upsert_chat_route(
    routes: dict[str, dict],
    *,
    route_id: str,
    label: str,
    provider: str,
    base_url: str,
    api_key: str,
    model_key: str,
    model_config: dict,
) -> None:
    if not api_key or not _is_public_https(base_url):
        return
    route = routes.setdefault(route_id, {
        "route_id": route_id,
        "label": label,
        "provider": provider,
        "base_url": base_url.rstrip("/"),
        "api_key": api_key,
        "models": [],
    })
    if not any(item["key"] == model_key for item in route["models"]):
        route["models"].append(_model_row(model_key, model_config))


def build_portable_routes(
    settings: dict,
    models: dict,
    chatroom_config: dict,
) -> dict:
    routes: dict[str, dict] = {}
    for model_key, raw_config in models.items():
        config = raw_config if isinstance(raw_config, dict) else {}
        provider = str(config.get("provider") or "").strip().lower()
        if not provider or provider.endswith("_cli") or provider in {
            "codex_cli",
            "gemini_cli",
            "antigravity_cli",
            "local",
        }:
            continue
        if provider == "gemini":
            _upsert_chat_route(
                routes,
                route_id="gemini",
                label="Gemini",
                provider="gemini",
                base_url="https://generativelanguage.googleapis.com",
                api_key=str(settings.get("gemini_key") or settings.get("gemini_free_key") or ""),
                model_key=model_key,
                model_config=config,
            )
        elif provider == "siliconflow":
            _upsert_chat_route(
                routes,
                route_id="siliconflow",
                label="SiliconFlow",
                provider="siliconflow",
                base_url="https://api.siliconflow.cn/v1",
                api_key=str(settings.get("siliconflow_key") or ""),
                model_key=model_key,
                model_config=config,
            )
        elif provider == "custom_openai":
            _upsert_chat_route(
                routes,
                route_id=str(config.get("route_id") or model_key),
                label=str(config.get("route_name") or model_key),
                provider="custom_openai",
                base_url=str(config.get("base_url") or ""),
                api_key=str(config.get("api_key") or ""),
                model_key=model_key,
                model_config=config,
            )

    for index, raw_route in enumerate(settings.get("custom_model_routes") or [], 1):
        if not isinstance(raw_route, dict):
            continue
        route_id = str(raw_route.get("id") or f"custom-{index}")
        for raw_model in raw_route.get("models") or []:
            if isinstance(raw_model, str):
                model_key = raw_model
                config = {"model": raw_model, "vision": True, "audio": False}
            elif isinstance(raw_model, dict):
                model_id = str(raw_model.get("model") or raw_model.get("model_id") or "").strip()
                model_key = str(raw_model.get("key") or raw_model.get("name") or model_id).strip()
                if not model_id or not model_key:
                    continue
                config = {
                    "model": model_id,
                    "vision": bool(raw_model.get("vision", True)),
                    "audio": raw_model.get("audio") is True,
                }
            else:
                continue
            _upsert_chat_route(
                routes,
                route_id=route_id,
                label=str(raw_route.get("name") or f"Cloud route {index}"),
                provider="custom_openai",
                base_url=str(raw_route.get("base_url") or ""),
                api_key=str(raw_route.get("api_key") or ""),
                model_key=model_key,
                model_config=config,
            )

    services: dict[str, dict] = {}
    sentinel_base = str(settings.get("sentinel_base_url") or "").rstrip("/")
    sentinel_key = str(settings.get("sentinel_api_key") or "")
    if not sentinel_base:
        sentinel_base = "https://generativelanguage.googleapis.com"
        sentinel_key = str(settings.get("gemini_free_key") or settings.get("gemini_key") or "")
    if sentinel_key and _is_public_https(sentinel_base):
        services["sentinel"] = {
            "provider": "custom_openai" if settings.get("sentinel_base_url") else "gemini",
            "base_url": sentinel_base,
            "api_key": sentinel_key,
            "model": str(settings.get("sentinel_model") or "gemini-3.1-flash-lite"),
        }

    embedding_base = str(settings.get("embedding_base_url") or "").rstrip("/")
    embedding_key = str(settings.get("embedding_api_key") or "")
    if not embedding_base:
        embedding_base = "https://generativelanguage.googleapis.com"
        embedding_key = str(settings.get("gemini_free_key") or settings.get("gemini_key") or "")
    if embedding_key and _is_public_https(embedding_base):
        services["embedding"] = {
            "provider": "custom_openai" if settings.get("embedding_base_url") else "gemini",
            "base_url": embedding_base,
            "api_key": embedding_key,
            "model": str(settings.get("embedding_model") or "gemini-embedding-001"),
        }

    tts_key = str(settings.get("siliconflow_key") or "")
    if tts_key:
        services["tts"] = {
            "provider": "siliconflow",
            "base_url": "https://api.siliconflow.cn/v1/audio/speech",
            "api_key": tts_key,
            "model": "FunAudioLLM/CosyVoice2-0.5B",
        }

    return apply_tts_settings({
        "chat": sorted(routes.values(), key=lambda item: item["route_id"]),
        "services": services,
    }, chatroom_config)


def apply_tts_settings(route_bundle: dict, chatroom_config: dict) -> dict:
    tts = route_bundle.get("services", {}).get("tts")
    if tts is not None:
        tts.update({
            "enabled": bool(chatroom_config.get("tts_enabled")),
            "main_voice": str(chatroom_config.get("tts_aion_voice") or ""),
            "second_voice": str(chatroom_config.get("tts_connor_voice") or ""),
        })
    return route_bundle


def encrypt_route_bundle(
    route_bundle: dict,
    public_key_spki_b64: str,
    *,
    device_id: str,
    snapshot_id: str,
) -> dict:
    public_key = load_device_public_key(public_key_spki_b64)
    aes_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    aad = canonical_json_bytes({
        "schema": HOMECOMING_SCHEMA_VERSION,
        "purpose": "aionshome-homecoming-routes",
        "device_id": device_id,
        "snapshot_id": snapshot_id,
    })
    ciphertext = AESGCM(aes_key).encrypt(
        nonce,
        canonical_json_bytes(route_bundle),
        aad,
    )
    wrapped_key = public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA1()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return {
        "algorithm": "RSA-OAEP-256-MGF1-SHA1+A256GCM",
        "wrapped_key_b64": base64.b64encode(wrapped_key).decode("ascii"),
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "ciphertext_b64": base64.b64encode(ciphertext).decode("ascii"),
        "aad_b64": base64.b64encode(aad).decode("ascii"),
    }
