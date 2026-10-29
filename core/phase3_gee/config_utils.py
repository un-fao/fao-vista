"""
Shared configuration loader for Phase 3
========================================

Reads the ``phase3_gee`` block from config.yaml and returns a settings dict
used by all Phase 3 implementation modules. The selected environment is
resolved from ``phase3_gee.gcp.project_id``. No EE dependency.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from core.phase1_local.process_dekad_composites import parse_bool_setting
from core.phase3_gee.constants import ALL_DEKADS


_STATISTIC_FOLDER_NAMES = {
    "count": "count",
    "frequency": "frequency",
    "dominant": "dominant_class",
    "dominant_class": "dominant_class",
}


def _join_remote_path(*parts: str) -> str:
    return "/".join(str(part).strip().strip("/") for part in parts if str(part).strip().strip("/"))


def _resolve_phase3_product_folder(step: str, statistic: str) -> str:
    statistic_folder = _STATISTIC_FOLDER_NAMES.get(statistic)
    if statistic_folder is None:
        valid = ", ".join(sorted(_STATISTIC_FOLDER_NAMES))
        raise ValueError(f"phase3_gee.statistic must be one of: {valid}; got {statistic!r}")
    return _join_remote_path(step, statistic_folder)


def load_phase3_settings(
    config_path: Path,
    step_override: str | None = None,
    statistic_override: str | None = None,
) -> dict:
    """
    Parse the ``phase3_gee`` block from *config_path* and return a settings
    dict shared by all Phase 3 implementation modules.

    The configured environment provides product roots only. This loader appends
    ``<step>/<statistic-folder>`` for Earth Engine asset outputs and
    ``<step>/<statistic-folder>/tiles`` for GCS GeoTIFF tile exports, so future
    statistics cannot accidentally write to the current ``count`` folder. The
    ``dominant`` statistic uses the folder name ``dominant_class``.

    Keys returned
    -------------
    project_id, archive_asset_root, statistics_product_root,
    statistics_asset_root, export_bucket_name, export_product_prefix,
    export_prefix, year_start, year_end, dekads, dry_run, overwrite
    """
    with config_path.open("r", encoding="utf-8") as handle:
        raw_config = yaml.safe_load(handle) or {}

    p3 = raw_config.get("phase3_gee", {})
    rc = p3.get("run_control", {})

    step                  = str(step_override or p3.get("step", "")).strip().lower()
    statistic             = str(statistic_override or p3.get("statistic", "")).strip().lower()
    project_id            = str(p3.get("gcp", {}).get("project_id", "")).strip()
    environments          = p3.get("environments", {})
    year_start            = int(p3.get("year_start", 2012))
    year_end              = int(p3.get("year_end",   2030))
    dry_run               = parse_bool_setting(rc.get("dry_run",   False), "phase3_gee.run_control.dry_run")
    overwrite             = parse_bool_setting(rc.get("overwrite", False), "phase3_gee.run_control.overwrite")

    if not step:
        raise ValueError("config/config.yaml is missing phase3_gee.step")
    if not statistic:
        raise ValueError("config/config.yaml is missing phase3_gee.statistic")
    if not project_id:
        raise ValueError("config/config.yaml is missing phase3_gee.gcp.project_id")
    if not isinstance(environments, dict) or not environments:
        raise ValueError("config/config.yaml is missing phase3_gee.environments")
    if project_id not in environments:
        available = ", ".join(sorted(str(key) for key in environments))
        raise ValueError(
            f"phase3_gee.gcp.project_id={project_id!r} has no matching "
            f"phase3_gee.environments entry. Available: {available}"
        )

    environment = environments[project_id] or {}
    gcs = environment.get("gcs", {}) or {}
    product_folder = _resolve_phase3_product_folder(step, statistic)

    archive_asset_root = str(environment.get("archive_asset_root", "")).strip().rstrip("/")
    statistics_product_root = str(environment.get("statistics_asset_root", "")).strip().rstrip("/")
    export_bucket_name = str(gcs.get("bucket_name", "")).strip()
    export_product_prefix = str(gcs.get("prefix", "vista_products")).strip().strip("/")

    statistics_asset_root = _join_remote_path(statistics_product_root, product_folder)
    export_prefix = _join_remote_path(export_product_prefix, product_folder, "tiles")

    # Resolve dekads.  Accepted forms:
    #   dekads: all               → all 36 dekads
    #   dekads: [01d1, 07d2, …]  → explicit list
    #   (key absent)              → all 36 dekads (safe default)
    raw_dekads = p3.get("dekads", "all")
    if isinstance(raw_dekads, str) and raw_dekads.strip().lower() == "all":
        dekads = list(ALL_DEKADS)
    elif isinstance(raw_dekads, list):
        dekads = [str(d).strip() for d in raw_dekads]
    else:
        raise ValueError(
            f"phase3_gee.dekads must be the string 'all' or a list of dekad keys, "
            f"got: {raw_dekads!r}"
        )

    if not archive_asset_root:
        raise ValueError(f"config/config.yaml is missing phase3_gee.environments.{project_id}.archive_asset_root")
    if not statistics_product_root:
        raise ValueError(f"config/config.yaml is missing phase3_gee.environments.{project_id}.statistics_asset_root")
    if not export_bucket_name:
        raise ValueError(f"config/config.yaml is missing phase3_gee.environments.{project_id}.gcs.bucket_name")

    return {
        "project_id":            project_id,
        "step":                  step,
        "statistic":             statistic,
        "statistic_folder":      _STATISTIC_FOLDER_NAMES[statistic],
        "statistics_product_root": statistics_product_root,
        "archive_asset_root":    archive_asset_root,
        "statistics_asset_root": statistics_asset_root,
        "export_bucket_name":    export_bucket_name,
        "export_product_prefix": export_product_prefix,
        "export_prefix":         export_prefix,
        "year_start":            year_start,
        "year_end":              year_end,
        "dekads":                dekads,
        "dry_run":               dry_run,
        "overwrite":             overwrite,
    }
