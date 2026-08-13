"""Narrow, non-semantic checks for credential-shaped visitor input."""

from __future__ import annotations

import re
import unicodedata


_CREDENTIAL_PATTERNS = (
    (
        "private_key",
        re.compile(r"-----BEGIN\s+(?:[A-Z0-9]+\s+)?PRIVATE KEY-----", re.IGNORECASE),
    ),
    (
        "bearer_token",
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b", re.IGNORECASE),
    ),
    (
        "api_token",
        re.compile(
            r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b"
        ),
    ),
    (
        "secret_assignment",
        re.compile(
            r"(?:\b(?:password|passwd|pwd|secret|token|api[ _-]?key|access[ _-]?key)\b"
            r"|密码|密钥|令牌|访问令牌)\s*[:=：]\s*[\"']?[^\s\"',;]{12,}",
            re.IGNORECASE,
        ),
    ),
)


def detect_credential_category(text: str) -> str | None:
    """Return a bounded category without retaining or returning matched text."""

    if not isinstance(text, str):
        return None
    normalized = unicodedata.normalize("NFKC", text)
    for category, pattern in _CREDENTIAL_PATTERNS:
        if pattern.search(normalized):
            return category
    return None
