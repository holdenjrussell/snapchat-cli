"""Warehouse entrypoints stay runnable with `uv run warehouse/<script>.py`.

The docs and existing schedulers call the scripts through uv, which builds
the script's environment from its inline metadata (PEP 723). Without the
block, uv runs them without psycopg and the database step fails.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

WAREHOUSE = Path(__file__).resolve().parents[1]
ENTRYPOINTS = [
    "sync_snapchat_daily.py",
    "sync_snapchat_entities.py",
    "run_snapchat_warehouse_cycle.py",
    "query.py",
]
# Reference regex from PEP 723.
BLOCK = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
)


def _metadata(path: Path) -> dict:
    match = next(
        (m for m in BLOCK.finditer(path.read_text()) if m.group("type") == "script"),
        None,
    )
    assert match, f"{path.name} has no '# /// script' block"
    content = "".join(
        line[2:] if line.startswith("# ") else line[1:]
        for line in match.group("content").splitlines(keepends=True)
    )
    return tomllib.loads(content)


@pytest.mark.parametrize("name", ENTRYPOINTS)
def test_entrypoint_declares_psycopg_for_uv(name: str) -> None:
    meta = _metadata(WAREHOUSE / name)
    assert any(dep.startswith("psycopg") for dep in meta["dependencies"])
    assert meta["requires-python"] == ">=3.11"
