#!/usr/bin/env python3
import sys
from pathlib import Path

skill_scripts = Path(__file__).resolve().parents[1] / "skills" / "snapchat-ads" / "scripts"
sys.path.insert(0, str(skill_scripts))

import snap_optimizer_support

raise SystemExit(snap_optimizer_support.main(["collect-weekly"]))
