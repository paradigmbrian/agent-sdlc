from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx
import jwt

from agent_sdlc.adapters.errors import ForgeError

API_VERSION = "2026-03-10"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}
_REFRESH_BEFORE_S = 300   # refresh when fewer than 5 minutes remain (spec §3.3)


class GitHubAppAuth:
    """Installation tokens for one GitHub App installation, minted from the App's private key.
    Thread-safe; the key and tokens never appear in exceptions."""

    def __init__(self, *, app_id: int, private_key: str, owner: str, repo: str,
                 http: httpx.Client, installation_id: int | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._app_id = app_id
        self._key = private_key
        self._owner, self._repo = owner, repo
        self._http = http
        self._installation_id = installation_id
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires = 0.0
        self._bot: str | None = None

    def app_jwt(self) -> str:
        now = int(self._clock())
        try:
            return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(self._app_id)},
                              self._key, algorithm="RS256")
        except Exception as e:  # malformed key: never echo it
            raise ForgeError(f"GitHub App private key could not be used ({type(e).__name__})"
                             ) from None

    def _app_request(self, method: str, path: str) -> Any:
        r = self._http.request(method, path,
                               headers={**HEADERS, "Authorization": f"Bearer {self.app_jwt()}"})
        if r.status_code >= 400:
            raise ForgeError(f"GitHub App auth {method} {path} failed: HTTP {r.status_code}")
        return r.json()

    def _installation(self) -> int:
        if self._installation_id is None:
            res = self._app_request("GET", f"/repos/{self._owner}/{self._repo}/installation")
            self._installation_id = int(res["id"])
        return self._installation_id

    def token(self) -> str:
        with self._lock:
            if self._token is None or self._expires - self._clock() < _REFRESH_BEFORE_S:
                res = self._app_request(
                    "POST", f"/app/installations/{self._installation()}/access_tokens")
                self._token = str(res["token"])
                self._expires = datetime.fromisoformat(
                    str(res["expires_at"]).replace("Z", "+00:00")).timestamp()
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None

    def bot_login(self) -> str:
        if self._bot is None:
            self._bot = f"{self._app_request('GET', '/app')['slug']}[bot]"
        return self._bot
