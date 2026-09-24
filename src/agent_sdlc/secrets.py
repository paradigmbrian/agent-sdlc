from __future__ import annotations

import base64
import getpass
import os

import keyring
from keyring.errors import KeyringError

ADO_PAT = ("agent-sdlc-ado-pat", "AGENT_SDLC_ADO_PAT")
CLAUDE_TOKEN = ("agent-sdlc-claude-token", "CLAUDE_CODE_OAUTH_TOKEN")
ANTHROPIC_KEY = ("agent-sdlc-anthropic-key", "ANTHROPIC_API_KEY")


class SecretNotFound(Exception):
    pass


def get_secret(service: str, env_var: str) -> str:
    """Env var first, then the macOS keychain item `<service>` for account `$USER`.

    The keychain is read in-process (keyring → Security framework), so the item's access list
    is checked against this Python interpreter rather than /usr/bin/security.
    """
    if value := os.environ.get(env_var):
        return value
    try:
        stored = keyring.get_password(service, getpass.getuser())
    except KeyringError as e:
        raise SecretNotFound(f"set {env_var}, or allow access to keychain item "
                             f"'{service}': {e}") from e
    if stored and stored.strip():
        return stored.strip()
    raise SecretNotFound(f"set {env_var} or add keychain item '{service}'")


def basic_auth_header(pat: str) -> str:
    token = base64.b64encode(f":{pat}".encode()).decode()
    return f"Authorization: Basic {token}"
