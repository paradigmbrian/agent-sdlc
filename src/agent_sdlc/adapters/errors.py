from __future__ import annotations

from collections.abc import Iterable


class ForgeError(Exception):
    """An ADO or GitHub call failed. Messages never contain tokens or keys."""


def redact(text: str, secrets: Iterable[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "[redacted]")
    return text
