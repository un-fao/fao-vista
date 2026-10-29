"""Phase 5 dispatcher for monthly archive statistics."""

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


_REGISTRY: dict[str, Callable[[str | None], int]] = {
    "count": lambda statistic: _run_count(statistic),
    "frequency": lambda statistic: _run_frequency(statistic),
    "dominant": lambda statistic: _run_dominant(statistic),
    "dominant_class": lambda statistic: _run_dominant(statistic),
}


def _run_count(statistic: str | None) -> int:
    from core.phase5_gee.calculate_monthly_count import main
    return main(statistic_override=statistic)


def _run_frequency(statistic: str | None) -> int:
    from core.phase5_gee.calculate_monthly_frequency import main
    return main(statistic_override=statistic)


def _run_dominant(statistic: str | None) -> int:
    from core.phase5_gee.calculate_monthly_dominant import main
    return main(statistic_override=statistic)


def parse_args() -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 5: compute statistics from the Phase 4 monthly archive. "
            "The statistic is read from config.yaml and can be overridden."
        )
    )
    parser.add_argument("--config", default=str(CONFIG_PATH), help="Path to config/config.yaml.")
    parser.add_argument(
        "--statistic",
        default=None,
        choices=sorted(_REGISTRY),
        help="Override phase5_monthly_statistics.statistic from config.",
    )
    return parser.parse_known_args()


def load_dispatch_statistic(config_path: Path) -> str:
    with config_path.open("r", encoding="utf-8") as handle:
        raw_config = yaml.safe_load(handle) or {}
    p5 = raw_config.get("phase5_monthly_statistics", {}) or {}
    statistic = str(p5.get("statistic", "")).strip().lower()
    if not statistic:
        raise ValueError("config/config.yaml is missing phase5_monthly_statistics.statistic")
    return statistic


def main() -> int:
    args, implementation_args = parse_args()
    config_path = Path(args.config).resolve()
    initialize_run_log_from_config(config_path, "phase5")

    try:
        statistic = args.statistic or load_dispatch_statistic(config_path)
    except ValueError as exc:
        log(f"ERROR: {exc}")
        return 1

    if statistic not in _REGISTRY:
        log(
            f"ERROR: Unsupported Phase 5 statistic {statistic!r}. "
            f"Valid values: {', '.join(sorted(_REGISTRY))}"
        )
        return 1

    log(f"Phase 5 dispatch: statistic='{statistic}'")
    original_argv = sys.argv[:]
    try:
        sys.argv = [original_argv[0], "--config", str(config_path), *implementation_args]
        return _REGISTRY[statistic](statistic)
    finally:
        sys.argv = original_argv


if __name__ == "__main__":
    raise SystemExit(main())

