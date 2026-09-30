"""Repository-wide test isolation from the operator's real home directory.

This file is imported by pytest before test-module collection. Setting HOME at
module import time ensures every Path.home()-derived cache, credential, and
artifact path is rooted in a disposable private directory before production
modules are imported. Tests must never chmod, create, or read live operator
state under ~/.cache or ~/.config.
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import shutil
import tempfile


_REPO_ROOT = Path(__file__).absolute().parent
# macOS: /tmp is a symlink to /private/tmp; use the physical directory so path
# comparisons in tests stay stable. Elsewhere use the resolved system temp dir.
_PHYSICAL_TEMP_ROOT = Path("/private/tmp")
if not _PHYSICAL_TEMP_ROOT.is_dir() or _PHYSICAL_TEMP_ROOT.is_symlink():
    _PHYSICAL_TEMP_ROOT = Path(tempfile.gettempdir()).resolve()
os.environ["TMPDIR"] = str(_PHYSICAL_TEMP_ROOT)
tempfile.tempdir = str(_PHYSICAL_TEMP_ROOT)

_OPERATOR_HOME = Path.home()
_TEST_HOME = Path(tempfile.mkdtemp(prefix="snapchat-cli-pytest-home-"))
_TEST_HOME.chmod(0o700)
os.environ["SNAPCHAT_CLI_OPERATOR_HOME"] = str(_OPERATOR_HOME)
os.environ["HOME"] = str(_TEST_HOME)
os.environ["XDG_CACHE_HOME"] = str(_TEST_HOME / ".cache")
os.environ["XDG_CONFIG_HOME"] = str(_TEST_HOME / ".config")
os.environ["SNAPCHAT_CLI_PYTEST_HOME"] = str(_TEST_HOME)
os.environ["SNAP_OPTIMIZER_CONFIG"] = str(
    _REPO_ROOT
    / "skills"
    / "snapchat-ads"
    / "tests"
    / "fixtures"
    / "optimizer.test.json"
)


@atexit.register
def _remove_isolated_test_home() -> None:
    shutil.rmtree(_TEST_HOME, ignore_errors=True)
