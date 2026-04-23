#!/usr/bin/env python3
"""Shared environment loading and required-key guards for workspace scripts."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

# ============================================================================
# Phase 1 Constants: Trace ID Generation
# ============================================================================
HERMES_TRACE_ID = "HERMES_TRACE_ID"
HERMES_TASK_ID = "HERMES_TASK_ID"

# ============================================================================
# Phase 4 Constants: Env/State Ownership
# ============================================================================
HERMES_HOME_ENV = "HERMES_HOME"
HERMES_SHARED_STATE_ROOT_ENV = "HERMES_SHARED_STATE_ROOT"
HERMES_SESSION_ID_ENV = "HERMES_SESSION_ID"
HERMES_SESSION_TTL_SEC_ENV = "HERMES_SESSION_TTL_SEC"


# ============================================================================
# Legacy Functions (Backward Compatibility)
# ============================================================================

def load_env_file(env_file: Path, overwrite: bool = False) -> None:
    """Load key=value pairs from env_file without overriding existing env vars."""
    if not env_file.exists():
        return
    with env_file.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                continue
            value = value.strip().strip('"').strip("'")
            if overwrite or key not in os.environ:
                os.environ[key] = value


def load_workspace_env(root: Path) -> None:
    """Load workspace .env then fallback ~/.env for legacy compatibility."""
    load_env_file(root / ".env", overwrite=True)
    load_env_file(Path.home() / ".env", overwrite=False)


def get_shared_memory_root(root: Path) -> Path:
    """Resolve shared memory root for all agents from env or default workspace memory dir."""
    raw = (os.getenv("HERMES_SHARED_MEMORY_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return root / "memory"


def require_env(name: str, help_text: str | None = None) -> str:
    """Return env var value or raise SystemExit with a clear fix hint."""
    value = (os.getenv(name) or "").strip()
    if value:
        return value
    hint = f" {help_text}" if help_text else ""
    raise SystemExit(f"Missing required environment variable: {name}.{hint}")


# ============================================================================
# Phase 1 Functions: Trace ID Generation
# ============================================================================

def resolve_trace_id() -> str:
    """Get existing trace ID from env or create new one."""
    existing = (os.getenv(HERMES_TRACE_ID) or "").strip()
    if existing:
        return existing

    # Create new trace ID: trace_{12-hex-chars}
    unique_id = uuid.uuid4().hex[:12]
    trace_id = f"trace_{unique_id}"

    # Store in environment for this run
    os.environ[HERMES_TRACE_ID] = trace_id

    return trace_id


# ============================================================================
# Phase 4 Functions: Centralized Path/State Management
# ============================================================================

def resolve_hermes_home() -> Path:
    """Resolve HERMES_HOME from env or default to ~/.hermes-local."""
    raw = (os.getenv(HERMES_HOME_ENV) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".hermes-local"


def resolve_shared_state_root() -> Path:
    """Resolve shared state root from env or default to {hermes_home}/.state."""
    raw = (os.getenv(HERMES_SHARED_STATE_ROOT_ENV) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return resolve_hermes_home() / ".state"


def resolve_session_root() -> Path:
    """Resolve session root directory for storing per-run state."""
    hermes_home = resolve_hermes_home()
    return hermes_home / ".openclaw" / "sessions"


def session_ttl_seconds() -> int:
    """Get session TTL in seconds from env or default 86400 (24 hours)."""
    raw = (os.getenv(HERMES_SESSION_TTL_SEC_ENV) or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return 86400  # 24 hours default


def get_or_create_session_id() -> str:
    """Get existing session ID from env or create new one."""
    existing = (os.getenv(HERMES_SESSION_ID_ENV) or "").strip()
    if existing:
        return existing

    # Create new session ID: session_{YYYYMMDD_HHMMSS}_{uuid[:8]}
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    unique_id = str(uuid.uuid4())[:8]
    session_id = f"session_{timestamp}_{unique_id}"

    # Store in environment for this run
    os.environ[HERMES_SESSION_ID_ENV] = session_id

    return session_id


def resolve_session_path(session_id: str) -> Path:
    """Resolve and create session directory path."""
    session_root = resolve_session_root()
    session_path = session_root / session_id

    # Ensure directory exists
    session_path.mkdir(parents=True, exist_ok=True)

    return session_path


def cleanup_expired_sessions(root: Path | None = None) -> int:
    """Remove session directories older than TTL. Returns count of removed sessions."""
    if root is None:
        root = resolve_session_root()

    if not root.exists():
        return 0

    ttl_sec = session_ttl_seconds()
    now = time.time()
    removed = 0

    for session_dir in root.iterdir():
        if not session_dir.is_dir():
            continue

        # Check if directory is older than TTL
        mtime = session_dir.stat().st_mtime
        age_sec = now - mtime

        if age_sec > ttl_sec:
            # Remove old session directory
            try:
                import shutil
                shutil.rmtree(session_dir)
                removed += 1
            except Exception:
                pass

    return removed


def load_config(config_name: str) -> dict:
    """Load JSON config from multiple search paths: CWD -> hermes_home -> shared_state."""
    search_paths = [
        Path.cwd() / f"{config_name}.json",
        resolve_hermes_home() / f"{config_name}.json",
        resolve_shared_state_root() / f"{config_name}.json",
    ]

    for path in search_paths:
        if path.exists():
            try:
                with path.open(encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

    return {}


def save_config(
    config_name: str,
    data: dict,
    target_root: Path | None = None,
) -> Path:
    """Save JSON config to target_root (default: shared_state)."""
    if target_root is None:
        target_root = resolve_shared_state_root()

    target_root.mkdir(parents=True, exist_ok=True)
    config_path = target_root / f"{config_name}.json"

    with config_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    return config_path
