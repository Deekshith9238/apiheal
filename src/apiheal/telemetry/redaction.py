"""Conservative redaction applied before output and persistence."""

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"
_SENSITIVE = re.compile(
    r"(?:api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|"
    r"private[_-]?key|client[_-]?secret|password|credential)",
    re.I,
)
_PATTERNS = [
    (re.compile(r"(?i)([?&](?:key|token|api_key|access_token)=)[^\s&#\"']+"), r"\1" + REDACTED),
    (
        re.compile(
            r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:basic|bearer)\s+)"
            r"[^\s,\"'}]+"
        ),
        r"\1" + REDACTED,
    ),
    (
        re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S),
        REDACTED,
    ),
    (
        re.compile(
            r"\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{20,}|"
            r"gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,})\b"
        ),
        REDACTED,
    ),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1" + REDACTED),
    (re.compile(r"(?i)(https?://)[^\s/@]+(?::[^\s/@]*)?@"), r"\1" + REDACTED + "@"),
    (
        re.compile(
            r"(?i)([\w-]*(?:api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|"
            r"client[_-]?secret|password|credential)[\w-]*[\"']?\s*[:=]\s*)"
            r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&}\r\n]+)"
        ),
        r"\1" + REDACTED,
    ),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), REDACTED),
]


def redact_text(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if _SENSITIVE.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value
