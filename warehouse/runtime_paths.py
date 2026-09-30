"""Verified, configurable runtime executables for Snapchat warehouse jobs."""

from __future__ import annotations

import os
import sys
from pathlib import Path


class RuntimePathError(RuntimeError):
    """A required stable runtime executable is missing or unsafe."""


def _require_executable(value: str | Path, *, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimePathError(f"{label} must be an absolute path: {path}")
    if not path.is_file():
        raise RuntimePathError(f"{label} does not exist: {path}")
    if not os.access(path, os.X_OK):
        raise RuntimePathError(f"{label} is not executable: {path}")
    return path


def resolve_warehouse_python() -> Path:
    """The interpreter warehouse child jobs run under.

    SNAPCHAT_WAREHOUSE_PYTHON pins a stable runtime (recommended for
    schedulers); otherwise the interpreter running the cycle is reused."""
    configured = os.environ.get("SNAPCHAT_WAREHOUSE_PYTHON")
    default = Path(sys.executable)
    return _require_executable(
        configured or default,
        label="Snapchat warehouse Python",
    )


def resolve_snapchat_cli(repo_root: Path) -> Path:
    configured = os.environ.get("SNAPCHAT_CLI_EXECUTABLE")
    default = repo_root / "cli" / ".venv" / "bin" / "snapchat-ads"
    return _require_executable(
        configured or default,
        label="Snapchat CLI executable",
    )
