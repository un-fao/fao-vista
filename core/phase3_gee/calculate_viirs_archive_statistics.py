"""
Phase 3 dispatcher
==================

Reads ``phase3_gee.step`` and ``phase3_gee.statistic`` from config and routes
to the matching implementation module.

Supported combinations
----------------------
+------------+-----------+-----------------------------------------------+
| step       | statistic | module                                        |
+============+===========+===============================================+
| dekadal    | count     | core.phase3_gee.calculate_dekadal_count       |
+------------+-----------+-----------------------------------------------+
| dekadal    | frequency | core.phase3_gee.calculate_dekadal_frequency   |
+------------+-----------+-----------------------------------------------+
| monthly    | count     | core.phase3_gee.calculate_monthly_count       |
+------------+-----------+-----------------------------------------------+
| monthly    | frequency | core.phase3_gee.calculate_monthly_frequency   |
+------------+-----------+-----------------------------------------------+
| dekadal    | dominant  | core.phase3_gee.calculate_dekadal_dominant    |
+------------+-----------+-----------------------------------------------+
| monthly    | dominant  | core.phase3_gee.calculate_monthly_dominant    |
+------------+-----------+-----------------------------------------------+

"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Callable

import yaml

from core.phase1_local.process_dekad_composites import (
    CONFIG_PATH,
    initialize_run_log_from_config,
    log,
)

# ---------------------------------------------------------------------------
# Registry  —  (step, statistic) → callable that accepts a settings dict
# ---------------------------------------------------------------------------
# Imports are deferred inside lambdas so that heavy EE initialisation only
# happens for the selected flavor.

_REGISTRY: dict[tuple[str, str], Callable[[dict], int]] = {
    ("dekadal", "count"):     lambda settings: _run_dekadal_count(settings),
    ("dekadal", "frequency"): lambda settings: _run_dekadal_frequency(settings),
    ("monthly", "count"):     lambda settings: _run_monthly_count(settings),
    ("monthly", "frequency"): lambda settings: _run_monthly_frequency(settings),
    ("dekadal", "dominant"):  lambda settings: _run_dekadal_dominant(settings),
    ("monthly", "dominant"):  lambda settings: _run_monthly_dominant(settings),
}

_VALID_STEPS      = sorted({k[0] for k in _REGISTRY})
_VALID_STATISTICS = sorted({k[1] for k in _REGISTRY})


def _run_dekadal_count(settings: dict) -> int:
    from core.phase3_gee.calculate_dekadal_count import run
    return run(settings)


def _run_dekadal_frequency(settings: dict) -> int:
    from core.phase3_gee.calculate_dekadal_frequency import run
    return run(settings)


def _run_monthly_count(settings: dict) -> int:
    from core.phase3_gee.calculate_monthly_count import run
    return run(settings)


def _run_monthly_frequency(settings: dict) -> int:
    from core.phase3_gee.calculate_monthly_frequency import run
    return run(settings)


def _run_dekadal_dominant(settings: dict) -> int:
    from core.phase3_gee.calculate_dekadal_dominant import run
    return run(settings)


def _run_monthly_dominant(settings: dict) -> int:
    from core.phase3_gee.calculate_monthly_dominant import run
    return run(settings)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 3: compute VIIRS archive statistics in Google Earth Engine. "
            "The step and statistic are read from config.yaml "
            "(phase3_gee.step / phase3_gee.statistic) and can be overridden "
            "on the command line."
        )
    )
    parser.add_argument(
        "--config",
        default=str(CONFIG_PATH),
        help="Path to config/config.yaml.",
    )
    parser.add_argument(
        "--step",
        default=None,
        choices=_VALID_STEPS,
        help="Override phase3_gee.step from config.",
    )
    parser.add_argument(
        "--statistic",
        default=None,
        choices=_VALID_STATISTICS,
        help="Override phase3_gee.statistic from config.",
    )
    # Pass remaining args through to the implementation module unchanged.
    # Each implementation's parse_args() re-parses sys.argv independently,
    # so extra flags (--dekads, --dry-run, etc.) are handled there.
    return parser.parse_known_args()


# ---------------------------------------------------------------------------
# Config loading (dispatcher-level only)
# ---------------------------------------------------------------------------

def load_dispatch_settings(config_path: Path) -> dict:
    """Return step and statistic from the phase3_gee config block."""
    with config_path.open("r", encoding="utf-8") as handle:
        raw_config = yaml.safe_load(handle) or {}

    p3 = raw_config.get("phase3_gee", {})
    step      = str(p3.get("step",      "")).strip().lower()
    statistic = str(p3.get("statistic", "")).strip().lower()

    if not step:
        raise ValueError(
            "config/config.yaml is missing phase3_gee.step. "
            f"Valid values: {', '.join(_VALID_STEPS)}"
        )
    if not statistic:
        raise ValueError(
            "config/config.yaml is missing phase3_gee.statistic. "
            f"Valid values: {', '.join(_VALID_STATISTICS)}"
        )

    return {"step": step, "statistic": statistic}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args, implementation_args = parse_args()
    config_path = Path(args.config).resolve()
    initialize_run_log_from_config(config_path, "phase3")

    try:
        dispatch_settings = load_dispatch_settings(config_path)
    except ValueError as exc:
        log(f"ERROR: {exc}")
        return 1

    # CLI overrides
    step      = args.step      or dispatch_settings["step"]
    statistic = args.statistic or dispatch_settings["statistic"]

    key = (step, statistic)
    if key not in _REGISTRY:
        log(
            f"ERROR: Unsupported combination step='{step}', statistic='{statistic}'. "
            f"Registered combinations: "
            + ", ".join(f"({s}, {st})" for s, st in sorted(_REGISTRY))
        )
        return 1

    log(f"Phase 3 dispatch: step='{step}'  statistic='{statistic}'")

    # Delegate — the implementation re-reads the full config itself
    runner = _REGISTRY[key]
    original_argv = sys.argv[:]
    try:
        sys.argv = [original_argv[0], *implementation_args]
        return runner({"config_path": config_path, "step": step, "statistic": statistic})
    finally:
        sys.argv = original_argv


if __name__ == "__main__":
    raise SystemExit(main())
