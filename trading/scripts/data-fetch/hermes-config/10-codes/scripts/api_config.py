#!/usr/bin/env python3
"""Centralized API credential resolution for 10-codes scripts."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class APISpec:
    """Canonical env variable priority for one API credential."""

    name: str
    env_vars: tuple[str, ...]


API_SPECS: dict[str, APISpec] = {
    "alpaca_key_id": APISpec("alpaca_key_id", ("APCA_API_KEY_ID", "ALPACA_API_KEY_ID")),
    "alpaca_secret_key": APISpec("alpaca_secret_key", ("APCA_API_SECRET_KEY", "ALPACA_API_SECRET_KEY")),
    "mboum": APISpec("mboum", ("MBOUM_KEY", "MBOUM_API_KEY")),
    "fintel": APISpec("fintel", ("FINTEL_API_KEY",)),
    "fred": APISpec("fred", ("FRED_API_KEY",)),
    "brave": APISpec("brave", ("BRAVE_API_KEY",)),
    "digitalocean": APISpec("digitalocean", ("DO_TOKEN", "DIGITALOCEAN_TOKEN")),
    "x_bearer": APISpec("x_bearer", ("X_API_BEARER_TOKEN",)),
}


def is_missing_secret(value: str | None) -> bool:
    """Treat empty and redacted placeholders as missing values."""
    raw = (value or "").strip()
    return not raw or raw in {"***", "<redacted>", "REDACTED"}


def _load_dotenv_files() -> dict[str, str]:
    """Read key=value pairs from well-known Hermes .env file locations.
    Python-native parser: handles values with special characters (|, =, etc.).
    """
    candidates = [
        "/root/.hermes/.env",
        "/home/kcinc/.hermes/.env",
        os.path.expanduser("~/.hermes/.env"),
    ]
    result: dict[str, str] = {}
    for path in candidates:
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip()
                    # Strip surrounding quotes if present
                    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                        val = val[1:-1]
                    if key and val:
                        result[key] = val
        except OSError:
            continue
    return result


_DOTENV_CACHE: dict[str, str] | None = None


def _get_dotenv() -> dict[str, str]:
    global _DOTENV_CACHE
    if _DOTENV_CACHE is None:
        _DOTENV_CACHE = _load_dotenv_files()
    return _DOTENV_CACHE


def resolve_api_key(api_name: str) -> tuple[str, str]:
    """Return first configured key for API as (value, env_var_name).
    Checks: 1) environment variables, 2) Hermes .env files.
    """
    spec = API_SPECS.get(api_name)
    if not spec:
        raise KeyError(f"Unknown API: {api_name}")

    # 1. Try environment variables first
    for env_name in spec.env_vars:
        candidate = os.getenv(env_name)
        if not is_missing_secret(candidate):
            return (candidate or "").strip(), env_name

    # 2. Fall back to .env files (handles values with special chars like |)
    dotenv = _get_dotenv()
    for env_name in spec.env_vars:
        candidate = dotenv.get(env_name)
        if not is_missing_secret(candidate):
            return (candidate or "").strip(), env_name

    raise KeyError(f"Missing credential for {api_name}; tried {', '.join(spec.env_vars)}")


def resolve_alpaca_credentials() -> tuple[str, str, str, str]:
    """Return Alpaca credentials and resolved variable names."""
    key_id, key_env = resolve_api_key("alpaca_key_id")
    secret, secret_env = resolve_api_key("alpaca_secret_key")
    return key_id, secret, key_env, secret_env
