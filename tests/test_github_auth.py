from datetime import datetime

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from agent_sdlc.adapters.errors import ForgeError
from agent_sdlc.adapters.github_auth import API_VERSION, GitHubAppAuth

API = "https://api.github.com"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption()).decode()
T0 = 1_799_700_000.0  # ~3.5 days before the default expires time


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


def _auth(clock: Clock, installation_id: int | None = None) -> GitHubAppAuth:
    return GitHubAppAuth(app_id=42, private_key=PEM, owner="o", repo="r",
                         http=httpx.Client(base_url=API), installation_id=installation_id,
                         clock=clock)


def _token_route(token: str, expires: str = "2027-01-15T08:00:00Z") -> respx.Route:
    return respx.post(f"{API}/app/installations/7/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": token, "expires_at": expires}))


def test_app_jwt_claims() -> None:
    token = _auth(Clock()).app_jwt()
    claims = jwt.decode(token, KEY.public_key(), algorithms=["RS256"],
                        options={"verify_exp": False, "verify_iat": False})
    assert claims == {"iat": int(T0) - 60, "exp": int(T0) + 540, "iss": "42"}


@respx.mock
def test_installation_lookup_token_cache_and_headers() -> None:
    lookup = respx.get(f"{API}/repos/o/r/installation").mock(
        return_value=httpx.Response(200, json={"id": 7}))
    mint = _token_route("ghs_first")
    auth = _auth(Clock())
    assert auth.token() == "ghs_first"
    assert auth.token() == "ghs_first"
    assert lookup.call_count == 1 and mint.call_count == 1
    req = mint.calls[0].request
    assert req.headers["Authorization"].startswith("Bearer ")
    assert req.headers["X-GitHub-Api-Version"] == API_VERSION


@respx.mock
def test_token_refreshes_five_minutes_before_expiry() -> None:
    clock = Clock()
    expires = "2027-01-15T08:00:00+00:00"
    exp_ts = datetime.fromisoformat(expires).timestamp()
    clock.now = exp_ts - 3600
    mint = _token_route("ghs_a", expires)
    auth = _auth(clock, installation_id=7)
    assert auth.token() == "ghs_a"
    clock.now = exp_ts - 301
    auth.token()
    assert mint.call_count == 1
    clock.now = exp_ts - 299
    auth.token()
    assert mint.call_count == 2


@respx.mock
def test_invalidate_forces_a_new_token() -> None:
    mint = _token_route("ghs_a")
    auth = _auth(Clock(), installation_id=7)
    auth.token()
    auth.invalidate()
    auth.token()
    assert mint.call_count == 2


@respx.mock
def test_mint_failure_raises_without_secrets() -> None:
    respx.post(f"{API}/app/installations/7/access_tokens").mock(
        return_value=httpx.Response(401, json={"message": "Bad credentials"}))
    with pytest.raises(ForgeError) as e:
        _auth(Clock(), installation_id=7).token()
    assert "401" in str(e.value) and "BEGIN" not in str(e.value) and "Bearer" not in str(e.value)


def test_bad_key_raises_without_key_material() -> None:
    auth = GitHubAppAuth(app_id=42, private_key="-----BEGIN nonsense-----", owner="o", repo="r",
                         http=httpx.Client(base_url=API), installation_id=7)
    with pytest.raises(ForgeError) as e:
        auth.app_jwt()
    assert "nonsense" not in str(e.value)


@respx.mock
def test_bot_login_from_app_slug() -> None:
    route = respx.get(f"{API}/app").mock(
        return_value=httpx.Response(200, json={"slug": "agent-sdlc-bot"}))
    auth = _auth(Clock(), installation_id=7)
    assert auth.bot_login() == "agent-sdlc-bot[bot]"
    assert auth.bot_login() == "agent-sdlc-bot[bot]"
    assert route.call_count == 1
