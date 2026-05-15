"""Secret resolution from environment variables or a secrets manager backend.

Config files store env var *names* only. This module resolves names to values
and provides a redact() function to strip secrets from text before LLM calls.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

_REDACT_PATTERNS: list[re.Pattern[str]] = []
_RESOLVED_VALUES: set[str] = set()


def resolve(env_var_name: str) -> str:
    """Return the secret value for the given environment variable name.

    Raises RuntimeError if the variable is not set, so callers fail fast
    rather than passing empty strings to MCP servers.
    """
    value = os.environ.get(env_var_name)
    if not value:
        raise RuntimeError(
            f"Required secret '{env_var_name}' is not set in the environment. "
            "Set it directly or via your secrets manager integration."
        )
    _register_for_redaction(value)
    return value


def resolve_optional(env_var_name: str) -> Optional[str]:
    """Return the secret value or None without raising."""
    value = os.environ.get(env_var_name)
    if value:
        _register_for_redaction(value)
    return value


def _register_for_redaction(value: str) -> None:
    if value and value not in _RESOLVED_VALUES:
        _RESOLVED_VALUES.add(value)
        # Escape the value and compile a pattern that matches it anywhere in text.
        _REDACT_PATTERNS.append(re.compile(re.escape(value)))


def redact(text: str) -> str:
    """Replace all known secret values in *text* with [REDACTED].

    Call this on any content before passing it to an LLM.
    """
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def redact_dict(data: dict) -> dict:  # type: ignore[type-arg]
    """Recursively redact secrets from a dict (e.g., PR diffs, Terraform state)."""
    result = {}
    for k, v in data.items():
        if isinstance(v, str):
            result[k] = redact(v)
        elif isinstance(v, dict):
            result[k] = redact_dict(v)
        elif isinstance(v, list):
            result[k] = [redact(i) if isinstance(i, str) else i for i in v]
        else:
            result[k] = v
    return result


@lru_cache(maxsize=None)
def _vault_client():  # type: ignore[return]
    """Return a HashiCorp Vault client if configured, else None."""
    vault_addr = os.environ.get("VAULT_ADDR")
    if not vault_addr:
        return None
    try:
        import hvac  # type: ignore[import]

        token = os.environ.get("VAULT_TOKEN")
        client = hvac.Client(url=vault_addr, token=token)
        if not client.is_authenticated():
            log.warning("vault.not_authenticated", addr=vault_addr)
            return None
        return client
    except ImportError:
        log.info("vault.hvac_not_installed")
        return None
