"""Explicit Snap attribution windows for stats reads that carry conversions.

Snap's stats endpoint credits conversions under whatever swipe-up, view and
engaged-view windows the request names, and falls back to its own defaults
when a request names none. Spend does not depend on them; conversions,
revenue and ROAS do. Set the windows once in the env file so the warehouse
and the hourly report read the same lens, and keep one lens per table.

    SNAPCHAT_SWIPE_UP_ATTRIBUTION_WINDOW       e.g. 1_DAY, 7_DAY, 28_DAY
    SNAPCHAT_VIEW_ATTRIBUTION_WINDOW           e.g. none, 1_HOUR, 1_DAY, 7_DAY
    SNAPCHAT_ENGAGED_VIEW_ATTRIBUTION_WINDOW   e.g. none, 1_DAY, 7_DAY

An unset variable sends nothing for that window, so Snap's default applies.
"""

from __future__ import annotations

import json
import os
import re
from typing import Mapping

ENV_KEYS = {
    "swipe_up_attribution_window": "SNAPCHAT_SWIPE_UP_ATTRIBUTION_WINDOW",
    "view_attribution_window": "SNAPCHAT_VIEW_ATTRIBUTION_WINDOW",
    "engaged_view_attribution_window": "SNAPCHAT_ENGAGED_VIEW_ATTRIBUTION_WINDOW",
}

_WINDOW = re.compile(r"none|[1-9][0-9]*_(HOUR|DAY)")


class AttributionConfigError(ValueError):
    """An attribution window variable holds something Snap would reject."""


def attribution_lens(env: Mapping[str, str] | None = None) -> dict[str, str | None]:
    """The configured windows keyed by Snap parameter name (None = Snap default)."""
    source = os.environ if env is None else env
    lens: dict[str, str | None] = {}
    for param, key in ENV_KEYS.items():
        raw = str(source.get(key) or "").strip()
        if not raw:
            lens[param] = None
            continue
        value = "none" if raw.lower() == "none" else raw.upper()
        if not _WINDOW.fullmatch(value):
            raise AttributionConfigError(
                f"{key}={raw!r} is not an attribution window "
                "(expected none or a value such as 1_DAY, 7_DAY, 28_DAY)"
            )
        lens[param] = value
    return lens


def attribution_cli_args(env: Mapping[str, str] | None = None) -> list[str]:
    """`snapchat-ads report stats` arguments for the configured windows."""
    lens = attribution_lens(env)
    args: list[str] = []
    if lens["swipe_up_attribution_window"]:
        args += ["--swipe-up-attribution-window", lens["swipe_up_attribution_window"]]
    if lens["view_attribution_window"]:
        args += ["--view-attribution-window", lens["view_attribution_window"]]
    if lens["engaged_view_attribution_window"]:
        args += [
            "--params-json",
            json.dumps(
                {
                    "engaged_view_attribution_window": lens[
                        "engaged_view_attribution_window"
                    ]
                }
            ),
        ]
    return args


def describe_lens(lens: Mapping[str, str | None]) -> str:
    """One line for logs and report notes, e.g. 'swipe 7_DAY, view none'."""
    labels = {
        "swipe_up_attribution_window": "swipe",
        "view_attribution_window": "view",
        "engaged_view_attribution_window": "engaged view",
    }
    return ", ".join(
        f"{labels[param]} {lens.get(param) or 'Snap default'}" for param in labels
    )
