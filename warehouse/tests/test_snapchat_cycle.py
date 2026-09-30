from __future__ import annotations

import fcntl
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


CANDIDATE_ROOT = Path(__file__).resolve().parents[2]
if str(CANDIDATE_ROOT) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_ROOT))

from warehouse import run_snapchat_warehouse_cycle as cycle  # noqa: E402


class StageAttemptTests(unittest.TestCase):
    def test_successful_stage_runs_once(self) -> None:
        calls: list[list[str]] = []
        child_envs: list[dict[str, str]] = []

        def fake_runner(
            argv: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            child_envs.append(kwargs["env"])
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout='{"row_count": 3}',
                stderr="",
            )

        payload, attempts = cycle.run_stage(
            ["python", "metrics.py"],
            stage="metrics",
            attempts=1,
            timeout_seconds=30,
            runner=fake_runner,
        )

        self.assertEqual(attempts, 1)
        self.assertEqual(payload, {"row_count": 3})
        self.assertEqual(len(calls), 1)
        self.assertEqual(child_envs[0]["UV_OFFLINE"], "1")

    def test_failed_stage_is_not_blindly_replayed(self) -> None:
        calls = 0

        def fake_runner(
            argv: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            nonlocal calls
            del kwargs
            calls += 1
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout="",
                stderr="still unavailable",
            )

        with self.assertRaisesRegex(cycle.CycleError, "attempt 1/1"):
            cycle.run_stage(
                ["python", "metrics.py"],
                stage="metrics",
                attempts=1,
                timeout_seconds=30,
                runner=fake_runner,
            )
        self.assertEqual(calls, 1)

    def test_more_than_one_stage_attempt_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly 1"):
            cycle.run_stage(
                ["python", "metrics.py"],
                stage="metrics",
                attempts=2,
                timeout_seconds=30,
            )


class CycleReceiptTests(unittest.TestCase):
    def _run_success(
        self,
        lock_path: Path,
        finish_mock: mock.Mock,
    ) -> dict:
        def fake_stage(
            argv: list[str],
            *,
            stage: str,
            attempts: int,
            timeout_seconds: int,
        ) -> tuple[dict, int]:
            del argv, attempts, timeout_seconds
            if stage == "metrics":
                return (
                    {
                        "rows_upserted": 17,
                        "window": {
                            "start": "2026-07-24T00:00:00-07:00",
                            "end": "2026-07-24T14:00:00-07:00",
                        },
                        "provisional": True,
                        "account_timezone": "America/Los_Angeles",
                    },
                    1,
                )
            self.assertEqual(stage, "entities")
            return ({"states_written": 240}, 1)

        with mock.patch.object(
            cycle,
            "ensure_receipt_table",
        ) as ensure_mock, mock.patch.object(
            cycle,
            "start_receipt",
        ) as start_mock, mock.patch.object(
            cycle,
            "finish_receipt",
            finish_mock,
        ):
            result = cycle.run_cycle(
                mode="recent",
                days=14,
                execute=True,
                apply_schema_first=False,
                warehouse_schema="snapchat_ads",
                database_url="postgresql://not-used",
                lock_path=lock_path,
                lock_wait_seconds=0,
                metrics_attempts=1,
                metrics_timeout_seconds=30,
                entity_attempts=1,
                entity_timeout_seconds=30,
                stage_runner=fake_stage,
            )

        ensure_mock.assert_called_once()
        start_mock.assert_called_once()
        return result

    def test_recent_cycle_finishes_one_success_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            finish_mock = mock.Mock()
            result = self._run_success(
                Path(temp_dir) / "warehouse.lock",
                finish_mock,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["metrics_rows"], 17)
        self.assertEqual(result["entity_rows"], 240)
        self.assertEqual(result["receipt_status"], "succeeded")
        finish_mock.assert_called_once()
        kwargs = finish_mock.call_args.kwargs
        self.assertEqual(kwargs["status"], "succeeded")
        self.assertEqual(kwargs["metrics_rows"], 17)
        self.assertEqual(kwargs["entity_rows"], 240)
        self.assertIsNone(kwargs["error"])
        self.assertEqual(
            kwargs["metadata"]["snapchat_api_max_attempts"],
            "2",
        )
        self.assertEqual(
            kwargs["metadata"]["stage_attempts"],
            {"metrics": 1, "entities": 1},
        )

    def test_failed_cycle_gets_terminal_failed_receipt_and_rerun_is_safe(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "warehouse.lock"
            failed_finish = mock.Mock()

            def fail_stage(*args: object, **kwargs: object) -> tuple[dict, int]:
                del args, kwargs
                raise cycle.CycleError("temporary child failure")

            with mock.patch.object(
                cycle,
                "ensure_receipt_table",
            ), mock.patch.object(
                cycle,
                "start_receipt",
            ), mock.patch.object(
                cycle,
                "finish_receipt",
                failed_finish,
            ):
                with self.assertRaisesRegex(
                    cycle.CycleError,
                    "temporary child failure",
                ):
                    cycle.run_cycle(
                        mode="recent",
                        days=14,
                        execute=True,
                        apply_schema_first=False,
                        warehouse_schema="snapchat_ads",
                        database_url="postgresql://not-used",
                        lock_path=lock_path,
                        lock_wait_seconds=0,
                        metrics_attempts=1,
                        metrics_timeout_seconds=30,
                        entity_attempts=1,
                        entity_timeout_seconds=30,
                        stage_runner=fail_stage,
                    )

            self.assertEqual(
                failed_finish.call_args.kwargs["status"],
                "failed",
            )
            self.assertIn(
                "temporary child failure",
                failed_finish.call_args.kwargs["error"],
            )

            success_finish = mock.Mock()
            result = self._run_success(lock_path, success_finish)

        self.assertTrue(result["ok"])
        self.assertEqual(
            success_finish.call_args.kwargs["status"],
            "succeeded",
        )
        self.assertNotEqual(
            str(failed_finish.call_args.kwargs["run_id"]),
            str(success_finish.call_args.kwargs["run_id"]),
        )

    def test_lock_contention_records_terminal_timeout_without_child_call(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "warehouse.lock"
            lock_path.touch()
            with lock_path.open("a+", encoding="utf-8") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                stage_mock = mock.Mock()
                with mock.patch.object(
                    cycle,
                    "ensure_receipt_table",
                ) as ensure_mock, mock.patch.object(
                    cycle,
                    "record_lock_timeout",
                ) as timeout_mock:
                    with self.assertRaises(cycle.LockTimeout):
                        cycle.run_cycle(
                            mode="recent",
                            days=14,
                            execute=True,
                            apply_schema_first=False,
                            warehouse_schema="snapchat_ads",
                            database_url="postgresql://not-used",
                            lock_path=lock_path,
                            lock_wait_seconds=0,
                            metrics_attempts=1,
                            metrics_timeout_seconds=30,
                            entity_attempts=1,
                            entity_timeout_seconds=30,
                            stage_runner=stage_mock,
                        )
                fcntl.flock(held.fileno(), fcntl.LOCK_UN)

        stage_mock.assert_not_called()
        ensure_mock.assert_called_once()
        timeout_mock.assert_called_once()
        self.assertIn(
            "timed out",
            timeout_mock.call_args.kwargs["error"],
        )

    def test_child_commands_keep_database_url_out_of_argv(self) -> None:
        stages = cycle._stage_commands(
            mode="recent",
            days=14,
            execute=True,
            warehouse_schema="snapchat_ads",
            python_executable=Path(
                "/opt/snapchat-cli/venv/bin/python"
            ),
        )
        joined = " ".join(
            argument
            for _, argv in stages
            for argument in argv
        )
        self.assertNotIn("--database-url", joined)
        self.assertNotIn("uv run", joined)
        self.assertIn("--mode intraday", joined)
        self.assertIn("--execute", joined)

    def test_closed_cycle_only_runs_closed_metrics_stage(self) -> None:
        stages = cycle._stage_commands(
            mode="closed",
            days=14,
            execute=True,
            warehouse_schema="snapchat_ads",
            python_executable=Path(
                "/opt/snapchat-cli/venv/bin/python"
            ),
        )
        self.assertEqual([name for name, _ in stages], ["metrics"])
        self.assertIn("--mode", stages[0][1])
        self.assertIn("closed", stages[0][1])
        self.assertNotIn("--dry-run", stages[0][1])

    def test_outer_timeout_budgets_exceed_inner_worst_case(self) -> None:
        recent_worst = cycle.worst_case_cycle_seconds(
            mode="recent",
            lock_wait_seconds=120,
            metrics_attempts=1,
            metrics_timeout_seconds=600,
            entity_attempts=1,
            entity_timeout_seconds=600,
        )
        closed_worst = cycle.worst_case_cycle_seconds(
            mode="closed",
            lock_wait_seconds=120,
            metrics_attempts=1,
            metrics_timeout_seconds=600,
            entity_attempts=1,
            entity_timeout_seconds=600,
        )

        self.assertEqual(recent_worst, 1320)
        self.assertEqual(closed_worst, 720)
        self.assertGreater(1500, recent_worst)
        self.assertGreater(900, closed_worst)
        self.assertLess(1500, 2 * 60 * 60)


if __name__ == "__main__":
    unittest.main()
