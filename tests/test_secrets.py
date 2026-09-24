import getpass
import subprocess

import pytest
from keyring.errors import KeyringError

from laya_sdlc import secrets
from laya_sdlc.secrets import SecretNotFound, get_secret


@pytest.fixture(autouse=True)
def no_security_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Secrets must be read in-process, never by spawning /usr/bin/security."""
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"subprocess used for secret lookup: {args}")
    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.delenv("LAYA_TEST_SECRET", raising=False)


def test_reads_keychain_in_process_with_service_and_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_get(service: str, username: str) -> str:
        calls.append((service, username))
        return "  the-pat\n"

    monkeypatch.setattr(secrets.keyring, "get_password", fake_get)
    assert get_secret("laya-sdlc-ado-pat", "LAYA_TEST_SECRET") == "the-pat"
    assert calls == [("laya-sdlc-ado-pat", getpass.getuser())]


def test_env_var_wins_over_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAYA_TEST_SECRET", "from-env")
    monkeypatch.setattr(secrets.keyring, "get_password",
                        lambda s, u: pytest.fail("keychain read despite env var"))
    assert get_secret("laya-sdlc-ado-pat", "LAYA_TEST_SECRET") == "from-env"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_or_empty_item_raises(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    monkeypatch.setattr(secrets.keyring, "get_password", lambda s, u: value)
    with pytest.raises(SecretNotFound, match="LAYA_TEST_SECRET"):
        get_secret("laya-sdlc-ado-pat", "LAYA_TEST_SECRET")


def test_keychain_denied_raises_secret_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(service: str, username: str) -> str:
        raise KeyringError("Can't get password from keychain: denied")

    monkeypatch.setattr(secrets.keyring, "get_password", denied)
    with pytest.raises(SecretNotFound, match="denied"):
        get_secret("laya-sdlc-ado-pat", "LAYA_TEST_SECRET")
