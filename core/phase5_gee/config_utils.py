"""
Shared configuration loader for Phase 5 monthly statistics.

Phase 5 reads the Phase 4 monthly archive and writes statistics products.
The runner project is intentionally separate from source and target projects
so operators can run tasks under one EE project while writing assets elsewhere.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from core.phase1_local.process_dekad_composites import parse_bool_setting
from core.phase3_gee.constants import ALL_DEKADS


STATISTIC_FOLDER_NAMES = {
    "count": "count",
    "frequency": "frequency",
    "dominant": "dominant_class",
    "dominant_class": "dominant_class",
}


def join_remote_path(*parts: str) -> str:
    return "/".join(str(part).strip().strip("/") for part in parts if str(part).strip().strip("/"))


def _expand_center_range(start: str, end: str) -> list[str]:
    try:
        start_index = ALL_DEKADS.index(start)
        end_index = ALL_DEKADS.index(end)
    except ValueError as exc:
        raise ValueError(
            f"Unknown center dekad in range {start!r}-{end!r}. "
            f"Valid keys: {', '.join(ALL_DEKADS)}"
        ) from exc
    if start_index > end_index:
        raise ValueError(f"Center dekad range start must be <= end: {start}-{end}")
    return ALL_DEKADS[start_index : end_index + 1]


def parse_center_dekads(raw_value: object) -> list[str]:
    """
    Resolve center-dekad selection.

    Accepted forms:
    - "all"
    - ["01d1", "01d2", "03d1-03d3"]
    - "01d1, 01d2, 03d1-03d3"
    """
    if raw_value in (None, ""):
        return list(ALL_DEKADS)
    if isinstance(raw_value, str):
        value = raw_value.strip()
        if value.lower() == "all":
            return list(ALL_DEKADS)
        tokens = [token.strip() for token in value.split(",") if token.strip()]
    elif isinstance(raw_value, list):
        tokens = [str(token).strip() for token in raw_value if str(token).strip()]
    else:
        raise ValueError(
            "phase5_monthly_statistics.center_dekads must be 'all', "
            "a comma-separated string, or a list"
        )

    resolved: list[str] = []
    for token in tokens:
        if "-" in token:
            start, end = [part.strip() for part in token.split("-", 1)]
            resolved.extend(_expand_center_range(start, end))
        else:
            if token not in ALL_DEKADS:
                raise ValueError(
                    f"Unknown center dekad {token!r}. Valid keys: {', '.join(ALL_DEKADS)}"
                )
            resolved.append(token)

    deduped: list[str] = []
    seen: set[str] = set()
    for center in resolved:
        if center not in seen:
            seen.add(center)
            deduped.append(center)
    return deduped


def load_phase5_settings(
    config_path: Path,
    statistic_override: str | None = None,
) -> dict:
    with config_path.open("r", encoding="utf-8") as handle:
        raw_config = yaml.safe_load(handle) or {}

    p5 = raw_config.get("phase5_monthly_statistics", {}) or {}
    runner = p5.get("runner", {}) or {}
    source = p5.get("source", {}) or {}
    target = p5.get("target", {}) or {}
    target_gcs = target.get("gcs", {}) or {}
    run_control = p5.get("run_control", {}) or {}

    statistic = str(statistic_override or p5.get("statistic", "")).strip().lower()
    statistic_folder = STATISTIC_FOLDER_NAMES.get(statistic)
    if statistic_folder is None:
        valid = ", ".join(sorted(STATISTIC_FOLDER_NAMES))
        raise ValueError(f"phase5_monthly_statistics.statistic must be one of: {valid}")

    runner_project_id = str(runner.get("project_id", "")).strip()
    source_project_id = str(source.get("project_id", "")).strip()
    source_asset_root = str(source.get("monthly_asset_root", "")).strip().rstrip("/")
    target_project_id = str(target.get("project_id", "")).strip()
    statistics_product_root = str(target.get("statistics_asset_root", "")).strip().rstrip("/")
    export_bucket_name = str(target_gcs.get("bucket_name", "")).strip()
    export_product_prefix = str(target_gcs.get("prefix", "vista_products")).strip().strip("/")

    year_start = int(p5.get("year_start", 2012))
    year_end = int(p5.get("year_end", 2030))
    if year_start > year_end:
        raise ValueError("phase5_monthly_statistics.year_start must be <= year_end")

    dry_run = parse_bool_setting(
        run_control.get("dry_run", True),
        "phase5_monthly_statistics.run_control.dry_run",
    )
    overwrite = parse_bool_setting(
        run_control.get("overwrite", False),
        "phase5_monthly_statistics.run_control.overwrite",
    )
    export_gcs = parse_bool_setting(
        run_control.get("export_gcs", True),
        "phase5_monthly_statistics.run_control.export_gcs",
    )

    center_dekads = parse_center_dekads(p5.get("center_dekads", "all"))

    required_fields = {
        "phase5_monthly_statistics.runner.project_id": runner_project_id,
        "phase5_monthly_statistics.source.project_id": source_project_id,
        "phase5_monthly_statistics.source.monthly_asset_root": source_asset_root,
        "phase5_monthly_statistics.target.project_id": target_project_id,
        "phase5_monthly_statistics.target.statistics_asset_root": statistics_product_root,
    }
    if export_gcs:
        required_fields["phase5_monthly_statistics.target.gcs.bucket_name"] = export_bucket_name
    missing = [name for name, value in required_fields.items() if not value]
    if missing:
        raise ValueError("config/config.yaml is missing: " + ", ".join(missing))

    product_folder = join_remote_path("monthly", statistic_folder)
    statistics_asset_root = join_remote_path(statistics_product_root, product_folder)
    export_prefix = join_remote_path(export_product_prefix, product_folder, "tiles")
    count_asset_root = join_remote_path(statistics_product_root, "monthly", "count")

    return {
        "runner_project_id": runner_project_id,
        "source_project_id": source_project_id,
        "source_asset_root": source_asset_root,
        "target_project_id": target_project_id,
        "statistic": statistic,
        "statistic_folder": statistic_folder,
        "statistics_product_root": statistics_product_root,
        "statistics_asset_root": statistics_asset_root,
        "count_asset_root": count_asset_root,
        "export_bucket_name": export_bucket_name,
        "export_product_prefix": export_product_prefix,
        "export_prefix": export_prefix,
        "year_start": year_start,
        "year_end": year_end,
        "center_dekads": center_dekads,
        "dry_run": dry_run,
        "overwrite": overwrite,
        "export_gcs": export_gcs,
    }

