"""
Deprecated Phase 3 stub: step=monthly, statistic=count
======================================================

Planned: compute per-month flood class counts by aggregating the dekadal
archive across all dekads within each calendar month.

This module is a stub — implementation is pending.

Entry points
------------
``run(settings)``
    Called by ``core.phase3_gee.calculate_viirs_archive_statistics`` when
    step='monthly' and statistic='count'.

``main()``
    Convenience shim for direct execution.
"""

from __future__ import annotations

from pathlib import Path

from core.phase1_local.process_dekad_composites import (
    CONFIG_PATH,
    initialize_run_log_from_config,
    log,
)


def run(settings: dict) -> int:
    """Entry point called by the dispatcher."""
    config_path = settings.get("config_path", CONFIG_PATH)
    initialize_run_log_from_config(config_path, "phase3")
    log(
        "ERROR: Phase 3 monthly count is not implemented. "
        "Use Phase 5 instead: python -m jobs.run_phase5 --statistic count"
    )
    return 1


def main() -> int:
    initialize_run_log_from_config(CONFIG_PATH, "phase3")
    log(
        "ERROR: Phase 3 monthly count is not implemented. "
        "Use Phase 5 instead: python -m jobs.run_phase5 --statistic count"
    )
    return 1


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
