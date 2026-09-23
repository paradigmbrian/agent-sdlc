from __future__ import annotations

import base64
import os
import subprocess

ADO_PAT = ("laya-sdlc-ado-pat", "LAYA_SDLC_ADO_PAT")
CLAUDE_TOKEN = ("laya-sdlc-claude-token", "CLAUDE_CODE_OAUTH_TOKEN")
ANTHROPIC_KEY = ("laya-sdlc-anthropic-key", "ANTHROPIC_API_KEY")


class SecretNotFound(Exception):
    pass


def get_secret(service: str, env_var: str) -> str:
    """Env var first, then the macOS keychain (`security add-generic-password -s <service>`)."""
    if value := os.environ.get(env_var):
        return value
    r = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                       capture_output=True, text=True, check=False)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    raise SecretNotFound(f"set {env_var} or add keychain item '{service}'")


def basic_auth_header(pat: str) -> str:
    token = base64.b64encode(f":{pat}".encode()).decode()
    return f"Authorization: Basic {token}"
