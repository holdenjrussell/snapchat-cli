from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


CANDIDATE_ROOT = Path(__file__).resolve().parents[2]
if str(CANDIDATE_ROOT) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_ROOT))

from warehouse import runtime_paths  # noqa: E402
from warehouse import sync_snapchat_daily as daily  # noqa: E402
from warehouse import sync_snapchat_entities as entities  # noqa: E402


class RuntimePathTests(unittest.TestCase):
    def test_defaults_are_the_running_python_and_the_repo_cli_venv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            cli = repo_root / "cli" / ".venv" / "bin" / "snapchat-ads"
            cli.parent.mkdir(parents=True)
            cli.write_text("#!/bin/sh\n", encoding="utf-8")
            cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
            with mock.patch.dict(
                os.environ,
                {
                    "SNAPCHAT_WAREHOUSE_PYTHON": "",
                    "SNAPCHAT_CLI_EXECUTABLE": "",
                },
            ):
                python_path = runtime_paths.resolve_warehouse_python()
                cli_path = runtime_paths.resolve_snapchat_cli(repo_root)

        self.assertEqual(python_path, Path(sys.executable))
        self.assertEqual(cli_path, cli)
        self.assertTrue(os.access(python_path, os.X_OK))

    def test_missing_python_fails_closed(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "SNAPCHAT_WAREHOUSE_PYTHON": (
                    "/definitely/missing/warehouse-python"
                )
            },
        ):
            with self.assertRaisesRegex(
                runtime_paths.RuntimePathError,
                "does not exist",
            ):
                runtime_paths.resolve_warehouse_python()

    def test_missing_cli_fails_before_subprocess(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"SNAPCHAT_CLI_EXECUTABLE": "/definitely/missing/snapchat-ads"},
        ), mock.patch.object(
            daily.subprocess,
            "run",
        ) as run_mock:
            with self.assertRaisesRegex(daily.SyncError, "does not exist"):
                daily._run_cli_json("account", "health-check")
        run_mock.assert_not_called()

    def test_metrics_and_entity_reads_use_direct_configured_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "snapchat-ads"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(
                executable.stat().st_mode
                | stat.S_IXUSR
                | stat.S_IXGRP
                | stat.S_IXOTH
            )
            calls: list[list[str]] = []

            def fake_run(
                argv: list[str],
                **kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                del kwargs
                calls.append(argv)
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout='{"timezone": "America/Los_Angeles"}',
                    stderr="",
                )

            with mock.patch.dict(
                os.environ,
                {"SNAPCHAT_CLI_EXECUTABLE": str(executable)},
            ), mock.patch.object(
                daily.subprocess,
                "run",
                side_effect=fake_run,
            ), mock.patch.object(
                entities.subprocess,
                "run",
                side_effect=fake_run,
            ):
                daily._run_cli_json("account", "health-check")
                entities.run_cli_json(
                    "default",
                    "account",
                    "health-check",
                )

        self.assertEqual(len(calls), 2)
        for argv in calls:
            self.assertEqual(argv[0], str(executable))
            self.assertNotIn("uv", argv)
            self.assertNotIn("run", argv[:2])


if __name__ == "__main__":
    unittest.main()
