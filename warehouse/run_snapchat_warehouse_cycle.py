#!/usr/bin/env python3
"""Orchestrate one locked Snapchat warehouse refresh cycle.

The file lock is acquired before any Snapchat API child starts and is held
until all requested stages finish. Execute mode records a durable running
receipt, then updates that same receipt to succeeded or failed. A later cycle
marks stale running receipts abandoned so interrupted work remains observable.

Usage:
    "$SNAPCHAT_WAREHOUSE_PYTHON" warehouse/run_snapchat_warehouse_cycle.py --mode closed
    "$SNAPCHAT_WAREHOUSE_PYTHON" warehouse/run_snapchat_warehouse_cycle.py --mode recent --execute
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

try:
    from warehouse.schema_config import (
        DEFAULT_WAREHOUSE_SCHEMA,
        WarehouseSchemaError,
        qualified_table,
        render_schema_sql,
        validate_warehouse_schema,
    )
    from warehouse.runtime_paths import (
        RuntimePathError,
        resolve_warehouse_python,
    )
except ModuleNotFoundError:  # Direct `python warehouse/...` execution.
    from schema_config import (  # type: ignore[no-redef]
        DEFAULT_WAREHOUSE_SCHEMA,
        WarehouseSchemaError,
        qualified_table,
        render_schema_sql,
        validate_warehouse_schema,
    )
    from runtime_paths import (  # type: ignore[no-redef]
        RuntimePathError,
        resolve_warehouse_python,
    )


REPO_ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_DIR = Path(__file__).resolve().parent
SCHEMA_SQL = WAREHOUSE_DIR / "schema.sql"
DAILY_SYNC = WAREHOUSE_DIR / "sync_snapchat_daily.py"
ENTITY_SYNC = WAREHOUSE_DIR / "sync_snapchat_entities.py"
DEFAULT_LOCK_PATH = Path(
    os.environ.get(
        "SNAPCHAT_WAREHOUSE_LOCK_PATH",
        str(
            Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
            / "snapchat-cli"
            / "snapchat-warehouse-sync.lock"
        ),
    )
)
DEFAULT_LOCK_WAIT_SECONDS = 120.0
DEFAULT_METRICS_TIMEOUT_SECONDS = 600
DEFAULT_ENTITY_TIMEOUT_SECONDS = 600
DEFAULT_STAGE_ATTEMPTS = 1
DEFAULT_STALE_AFTER_SECONDS = 2 * 60 * 60
LOCK_TIMEOUT_EXIT = 75


class CycleError(RuntimeError):
    """A bounded cycle failure safe to expose after secret scrubbing."""


class LockTimeout(CycleError):
    """The shared warehouse lock was not available within the wait budget."""


def _scrub(value: Any) -> Any:
    """Use the canonical scrubber without making it a package dependency."""
    scrubber_dir = REPO_ROOT / "skills" / "snapchat-ads" / "scripts"
    scrubber_path = str(scrubber_dir)
    if scrubber_path not in sys.path:
        sys.path.insert(0, scrubber_path)
    try:
        from secret_scrubber import scrub_sensitive_value

        return scrub_sensitive_value(value)
    except Exception:
        # Never include child stdout in the fallback. The fixed text is less
        # diagnostic, but it cannot leak credentials when the scrubber fails.
        if isinstance(value, str):
            return "redacted cycle error"
        return {"error": "redacted cycle error"}


def _safe_error(exc: BaseException) -> str:
    cleaned = _scrub(str(exc))
    text = cleaned if isinstance(cleaned, str) else json.dumps(cleaned)
    return text[:4000]


@contextlib.contextmanager
def shared_cycle_lock(
    path: Path,
    *,
    wait_seconds: float,
    poll_seconds: float = 0.1,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> Iterator[None]:
    """Hold one process-shared non-destructive file lock for the whole cycle."""
    if wait_seconds < 0:
        raise ValueError("lock wait seconds cannot be negative")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_file:
        deadline = monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(
                    lock_file.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
                break
            except BlockingIOError as exc:
                if monotonic() >= deadline:
                    raise LockTimeout(
                        f"Snapchat warehouse cycle lock timed out after "
                        f"{wait_seconds:g}s: {path}"
                    ) from exc
                sleeper(min(poll_seconds, max(0.0, deadline - monotonic())))
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _parse_json_output(stdout: str, *, stage: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise CycleError(
            f"{stage} returned non-JSON output"
        ) from exc
    if not isinstance(payload, dict):
        raise CycleError(f"{stage} returned a non-object JSON payload")
    return payload


def run_stage(
    argv: Sequence[str],
    *,
    stage: str,
    attempts: int,
    timeout_seconds: int,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[dict[str, Any], int]:
    """Run one stage once.

    Retryable HTTP requests are already bounded to one retry inside the CLI.
    Replaying a whole multi-request stage would multiply that budget and can
    repeat completed reads or writes, so production and CLI validation permit
    exactly one stage attempt.
    """
    if attempts != 1:
        raise ValueError("stage attempts must be exactly 1")
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            child_env = os.environ.copy()
            child_env["UV_OFFLINE"] = "1"
            result = runner(
                list(argv),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=REPO_ROOT,
                env=child_env,
            )
        except subprocess.TimeoutExpired:
            last_error = (
                f"{stage} timed out after {timeout_seconds}s "
                f"(attempt {attempt}/{attempts})"
            )
        else:
            if result.returncode == 0:
                return _parse_json_output(result.stdout, stage=stage), attempt
            diagnostic = (result.stderr or result.stdout or "").strip()
            last_error = (
                f"{stage} failed with exit {result.returncode} "
                f"(attempt {attempt}/{attempts}): {diagnostic}"
            )
    raise CycleError(_safe_error(last_error))


def worst_case_cycle_seconds(
    *,
    mode: str,
    lock_wait_seconds: float,
    metrics_attempts: int,
    metrics_timeout_seconds: int,
    entity_attempts: int,
    entity_timeout_seconds: int,
) -> float:
    """Return the outer-runner budget floor for one configured cycle."""
    if mode not in {"closed", "recent"}:
        raise ValueError("mode must be closed or recent")
    if metrics_attempts != 1 or entity_attempts != 1:
        raise ValueError("stage attempts must be exactly 1")
    if lock_wait_seconds < 0:
        raise ValueError("lock wait seconds cannot be negative")
    if metrics_timeout_seconds < 1 or entity_timeout_seconds < 1:
        raise ValueError("stage timeouts must be positive")
    total = lock_wait_seconds + (
        metrics_attempts * metrics_timeout_seconds
    )
    if mode == "recent":
        total += entity_attempts * entity_timeout_seconds
    return total


def _connect(database_url: str) -> Any:
    import psycopg

    return psycopg.connect(database_url)


def _receipt_table(warehouse_schema: str) -> str:
    return qualified_table(warehouse_schema, "snapchat_sync_runs")


def apply_schema(
    database_url: str,
    *,
    warehouse_schema: str,
    connect: Callable[[str], Any] = _connect,
) -> None:
    rendered = render_schema_sql(
        SCHEMA_SQL.read_text(encoding="utf-8"),
        warehouse_schema,
    )
    with connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(rendered)
        conn.commit()


def ensure_receipt_table(
    database_url: str,
    *,
    warehouse_schema: str,
    connect: Callable[[str], Any] = _connect,
) -> None:
    with connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_catalog.to_regclass(%s)",
                (f"{warehouse_schema}.snapchat_sync_runs",),
            )
            row = cur.fetchone()
    if not row or row[0] is None:
        raise CycleError(
            "snapchat_sync_runs is missing; apply the candidate schema before "
            "executing a warehouse cycle"
        )


def start_receipt(
    database_url: str,
    *,
    run_id: uuid.UUID,
    mode: str,
    account_key: str,
    warehouse_schema: str,
    started_at: datetime.datetime,
    stale_after_seconds: int,
    connect: Callable[[str], Any] = _connect,
) -> None:
    table = _receipt_table(warehouse_schema)
    with connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {table}
                   SET status = 'abandoned',
                       completed_at = pg_catalog.now(),
                       error = 'superseded stale running receipt'
                 WHERE status = 'running'
                   AND started_at < pg_catalog.now()
                       - (%s * INTERVAL '1 second')
                """,
                (stale_after_seconds,),
            )
            cur.execute(
                f"""
                INSERT INTO {table} (
                    run_id,
                    sync_kind,
                    status,
                    account_key,
                    warehouse_schema,
                    started_at,
                    metadata
                )
                VALUES (%s, %s, 'running', %s, %s, %s, %s::jsonb)
                """,
                (
                    run_id,
                    mode,
                    account_key,
                    warehouse_schema,
                    started_at,
                    json.dumps({"runner": "run_snapchat_warehouse_cycle.py"}),
                ),
            )
        conn.commit()


def finish_receipt(
    database_url: str,
    *,
    run_id: uuid.UUID,
    warehouse_schema: str,
    status: str,
    window_start: str | None,
    window_end: str | None,
    metrics_rows: int,
    entity_rows: int,
    error: str | None,
    metadata: dict[str, Any],
    connect: Callable[[str], Any] = _connect,
) -> None:
    if status not in {"succeeded", "failed"}:
        raise ValueError("finish_receipt requires succeeded or failed")
    table = _receipt_table(warehouse_schema)
    with connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {table}
                   SET status = %s,
                       window_start = %s,
                       window_end = %s,
                       metrics_rows = %s,
                       entity_rows = %s,
                       completed_at = pg_catalog.now(),
                       error = %s,
                       metadata = %s::jsonb
                 WHERE run_id = %s
                   AND status = 'running'
                """,
                (
                    status,
                    window_start,
                    window_end,
                    metrics_rows,
                    entity_rows,
                    error,
                    json.dumps(_scrub(metadata), default=str),
                    run_id,
                ),
            )
            if cur.rowcount != 1:
                raise CycleError(
                    f"terminal receipt update affected {cur.rowcount} rows"
                )
        conn.commit()


def record_lock_timeout(
    database_url: str,
    *,
    run_id: uuid.UUID,
    mode: str,
    account_key: str,
    warehouse_schema: str,
    started_at: datetime.datetime,
    error: str,
    connect: Callable[[str], Any] = _connect,
) -> None:
    table = _receipt_table(warehouse_schema)
    with connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {table} (
                    run_id,
                    sync_kind,
                    status,
                    account_key,
                    warehouse_schema,
                    started_at,
                    completed_at,
                    error,
                    metadata
                )
                VALUES (
                    %s, %s, 'lock_timeout', %s, %s, %s,
                    pg_catalog.now(), %s, %s::jsonb
                )
                """,
                (
                    run_id,
                    mode,
                    account_key,
                    warehouse_schema,
                    started_at,
                    error,
                    json.dumps({"runner": "run_snapchat_warehouse_cycle.py"}),
                ),
            )
        conn.commit()


def _stage_commands(
    *,
    mode: str,
    days: int,
    execute: bool,
    warehouse_schema: str,
    python_executable: Path,
) -> list[tuple[str, list[str]]]:
    metrics_mode = "intraday" if mode == "recent" else "closed"
    metrics = [
        str(python_executable),
        str(DAILY_SYNC),
        "--mode",
        metrics_mode,
        "--days",
        str(days),
        "--warehouse-schema",
        warehouse_schema,
    ]
    if not execute:
        metrics.append("--dry-run")
    stages: list[tuple[str, list[str]]] = [("metrics", metrics)]
    if mode == "recent":
        entities = [
            str(python_executable),
            str(ENTITY_SYNC),
            "--warehouse-schema",
            warehouse_schema,
        ]
        entities.append("--execute" if execute else "--dry-run")
        stages.append(("entities", entities))
    return stages


def run_cycle(
    *,
    mode: str,
    days: int,
    execute: bool,
    apply_schema_first: bool,
    warehouse_schema: str,
    database_url: str | None,
    lock_path: Path,
    lock_wait_seconds: float,
    metrics_attempts: int,
    metrics_timeout_seconds: int,
    entity_attempts: int,
    entity_timeout_seconds: int,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    stage_runner: Callable[..., tuple[dict[str, Any], int]] = run_stage,
) -> dict[str, Any]:
    """Run one cycle. Dependency injection keeps lock/retry tests offline."""
    if mode not in {"closed", "recent"}:
        raise ValueError("mode must be closed or recent")
    if days < 1:
        raise ValueError("days must be at least 1")
    if execute and not database_url:
        raise CycleError("DATABASE_URL is required with --execute")
    if apply_schema_first and not execute:
        raise CycleError("--apply-schema requires --execute")
    worst_case_cycle_seconds(
        mode=mode,
        lock_wait_seconds=lock_wait_seconds,
        metrics_attempts=metrics_attempts,
        metrics_timeout_seconds=metrics_timeout_seconds,
        entity_attempts=entity_attempts,
        entity_timeout_seconds=entity_timeout_seconds,
    )
    try:
        python_executable = resolve_warehouse_python()
    except RuntimePathError as exc:
        raise CycleError(str(exc)) from exc

    run_id = uuid.uuid4()
    started_at = datetime.datetime.now(datetime.timezone.utc)
    account_key = os.environ.get("SNAPCHAT_ADS_ACCOUNT", "default")
    receipt_started = False
    stage_results: dict[str, dict[str, Any]] = {}
    stage_attempt_counts: dict[str, int] = {}

    try:
        with shared_cycle_lock(
            lock_path,
            wait_seconds=lock_wait_seconds,
        ):
            if execute:
                assert database_url is not None
                if apply_schema_first:
                    apply_schema(
                        database_url,
                        warehouse_schema=warehouse_schema,
                    )
                ensure_receipt_table(
                    database_url,
                    warehouse_schema=warehouse_schema,
                )
                start_receipt(
                    database_url,
                    run_id=run_id,
                    mode=mode,
                    account_key=account_key,
                    warehouse_schema=warehouse_schema,
                    started_at=started_at,
                    stale_after_seconds=stale_after_seconds,
                )
                receipt_started = True

            for stage, argv in _stage_commands(
                mode=mode,
                days=days,
                execute=execute,
                warehouse_schema=warehouse_schema,
                python_executable=python_executable,
            ):
                stage_attempts = (
                    metrics_attempts if stage == "metrics" else entity_attempts
                )
                stage_timeout_seconds = (
                    metrics_timeout_seconds
                    if stage == "metrics"
                    else entity_timeout_seconds
                )
                payload, used_attempts = stage_runner(
                    argv,
                    stage=stage,
                    attempts=stage_attempts,
                    timeout_seconds=stage_timeout_seconds,
                )
                stage_results[stage] = payload
                stage_attempt_counts[stage] = used_attempts

            metrics = stage_results["metrics"]
            window = metrics.get("window") or {}
            metrics_rows = int(
                metrics.get("rows_upserted")
                or metrics.get("row_count")
                or 0
            )
            entities = stage_results.get("entities") or {}
            entity_rows = int(
                entities.get("states_written")
                or entities.get("entity_count")
                or 0
            )
            result = {
                "ok": True,
                "run_id": str(run_id),
                "mode": mode,
                "execute": execute,
                "warehouse_schema": warehouse_schema,
                "window": {
                    "start": window.get("start"),
                    "end": window.get("end"),
                },
                "metrics_rows": metrics_rows,
                "entity_rows": entity_rows,
                "stage_attempts": stage_attempt_counts,
                "stage_timeouts_seconds": {
                    "metrics": metrics_timeout_seconds,
                    "entities": (
                        entity_timeout_seconds
                        if mode == "recent"
                        else None
                    ),
                },
                "receipt_status": "succeeded" if execute else "not_written",
            }
            if receipt_started:
                assert database_url is not None
                finish_receipt(
                    database_url,
                    run_id=run_id,
                    warehouse_schema=warehouse_schema,
                    status="succeeded",
                    window_start=window.get("start"),
                    window_end=window.get("end"),
                    metrics_rows=metrics_rows,
                    entity_rows=entity_rows,
                    error=None,
                    metadata={
                        "stage_attempts": stage_attempt_counts,
                        "stage_timeouts_seconds": {
                            "metrics": metrics_timeout_seconds,
                            "entities": (
                                entity_timeout_seconds
                                if mode == "recent"
                                else None
                            ),
                        },
                        "snapchat_api_max_attempts": os.environ.get(
                            "SNAPCHAT_API_MAX_ATTEMPTS",
                            "2",
                        ),
                        "provisional": metrics.get("provisional"),
                        "account_timezone": metrics.get("account_timezone"),
                        "attribution": metrics.get("attribution"),
                    },
                )
            return result
    except LockTimeout as exc:
        safe_error = _safe_error(exc)
        if execute and database_url:
            try:
                ensure_receipt_table(
                    database_url,
                    warehouse_schema=warehouse_schema,
                )
                record_lock_timeout(
                    database_url,
                    run_id=run_id,
                    mode=mode,
                    account_key=account_key,
                    warehouse_schema=warehouse_schema,
                    started_at=started_at,
                    error=safe_error,
                )
            except Exception as receipt_exc:
                raise LockTimeout(
                    f"{safe_error}; lock-timeout receipt failed: "
                    f"{_safe_error(receipt_exc)}"
                ) from exc
        raise
    except BaseException as exc:
        safe_error = _safe_error(exc)
        if receipt_started and database_url:
            try:
                metrics = stage_results.get("metrics") or {}
                window = metrics.get("window") or {}
                entities = stage_results.get("entities") or {}
                finish_receipt(
                    database_url,
                    run_id=run_id,
                    warehouse_schema=warehouse_schema,
                    status="failed",
                    window_start=window.get("start"),
                    window_end=window.get("end"),
                    metrics_rows=int(
                        metrics.get("rows_upserted")
                        or metrics.get("row_count")
                        or 0
                    ),
                    entity_rows=int(
                        entities.get("states_written")
                        or entities.get("entity_count")
                        or 0
                    ),
                    error=safe_error,
                    metadata={
                        "stage_attempts": stage_attempt_counts,
                        "stage_timeouts_seconds": {
                            "metrics": metrics_timeout_seconds,
                            "entities": (
                                entity_timeout_seconds
                                if mode == "recent"
                                else None
                            ),
                        },
                        "snapchat_api_max_attempts": os.environ.get(
                            "SNAPCHAT_API_MAX_ATTEMPTS",
                            "2",
                        ),
                    },
                )
            except Exception as receipt_exc:
                raise CycleError(
                    f"{safe_error}; terminal receipt failed: "
                    f"{_safe_error(receipt_exc)}"
                ) from exc
        raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one locked, receipted Snapchat warehouse cycle."
    )
    parser.add_argument("--mode", choices=("closed", "recent"), required=True)
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="Trailing closed days. Ignored by recent intraday metrics.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Write warehouse rows and durable receipts. Default is preview.",
    )
    parser.add_argument(
        "--apply-schema",
        action="store_true",
        help="Apply idempotent schema DDL before the first execute receipt.",
    )
    parser.add_argument(
        "--warehouse-schema",
        default=os.environ.get(
            "SNAP_WAREHOUSE_SCHEMA",
            DEFAULT_WAREHOUSE_SCHEMA,
        ),
    )
    parser.add_argument(
        "--lock-path",
        type=Path,
        default=DEFAULT_LOCK_PATH,
    )
    parser.add_argument(
        "--lock-wait-seconds",
        type=float,
        default=DEFAULT_LOCK_WAIT_SECONDS,
    )
    parser.add_argument(
        "--metrics-attempts",
        type=int,
        choices=(1,),
        default=DEFAULT_STAGE_ATTEMPTS,
    )
    parser.add_argument(
        "--metrics-timeout-seconds",
        type=int,
        default=DEFAULT_METRICS_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--entity-attempts",
        type=int,
        choices=(1,),
        default=DEFAULT_STAGE_ATTEMPTS,
    )
    parser.add_argument(
        "--entity-timeout-seconds",
        type=int,
        default=DEFAULT_ENTITY_TIMEOUT_SECONDS,
    )
    args = parser.parse_args()
    try:
        warehouse_schema = validate_warehouse_schema(args.warehouse_schema)
        result = run_cycle(
            mode=args.mode,
            days=args.days,
            execute=args.execute,
            apply_schema_first=args.apply_schema,
            warehouse_schema=warehouse_schema,
            database_url=os.environ.get("DATABASE_URL"),
            lock_path=args.lock_path,
            lock_wait_seconds=args.lock_wait_seconds,
            metrics_attempts=args.metrics_attempts,
            metrics_timeout_seconds=args.metrics_timeout_seconds,
            entity_attempts=args.entity_attempts,
            entity_timeout_seconds=args.entity_timeout_seconds,
        )
    except LockTimeout as exc:
        print(json.dumps({"ok": False, "error": _safe_error(exc)}), file=sys.stderr)
        raise SystemExit(LOCK_TIMEOUT_EXIT) from exc
    except (
        CycleError,
        WarehouseSchemaError,
        ValueError,
    ) as exc:
        print(json.dumps({"ok": False, "error": _safe_error(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(_scrub(result), default=str))


if __name__ == "__main__":
    main()
