"""Phase 5 implementation: monthly archive count statistics."""

from __future__ import annotations

import argparse
from pathlib import Path

import ee

from core.quality_control.exclusions import (
    exclusions_affecting_monthly_statistic,
    load_dekad_exclusions,
)
from core.phase1_local.process_dekad_composites import (
    CONFIG_PATH,
    initialize_run_log_from_config,
    log,
)
from core.phase3_gee.calculate_dekadal_count import build_count_image
from core.phase3_gee.constants import ALL_DEKADS, DEKAD_DAY_RANGES
from core.phase3_gee.ee_utils import (
    asset_exists,
    list_assets_for_folder,
    resolve_projection,
    submit_asset_export,
    submit_gcs_export,
)
from core.phase5_gee.config_utils import load_phase5_settings, parse_center_dekads
from core.phase5_gee.monthly_assets import filter_monthly_assets_for_center


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 5: compute count statistics from the Phase 4 monthly "
            "archive and export results to Earth Engine and GCS."
        )
    )
    parser.add_argument("--config", default=str(CONFIG_PATH), help="Path to config/config.yaml.")
    parser.add_argument(
        "--statistic",
        default=None,
        choices=["count"],
        help="Statistic to run; this module supports count.",
    )
    parser.add_argument(
        "--center-dekads",
        nargs="+",
        default=None,
        metavar="CENTER",
        help="Center dekads to process, e.g. 01d1 01d2, all, or 01d1-03d3.",
    )
    parser.add_argument("--year-start", type=int, default=None, help="First source year to include.")
    parser.add_argument("--year-end", type=int, default=None, help="Last source year to include.")
    run_mode_group = parser.add_mutually_exclusive_group()
    run_mode_group.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="List planned exports without submitting EE tasks.",
    )
    run_mode_group.add_argument(
        "--execute",
        action="store_true",
        default=None,
        help="Submit EE tasks even when config dry_run is true.",
    )
    parser.add_argument(
        "--export-gcs",
        action="store_true",
        default=None,
        help="Export GeoTIFF tiles to GCS even when config export_gcs is false.",
    )
    return parser.parse_args()


def annotate_image(
    image: ee.Image,
    center_dekad: str,
    asset_ids: list[str],
    years: list[int],
    year_start: int,
    year_end: int,
    excluded_source_dekads: tuple[str, ...] = (),
) -> ee.Image:
    dr = DEKAD_DAY_RANGES[center_dekad]
    return image.set({
        "phase": 5,
        "product": "vista_monthly_statistics",
        "statistic": "count",
        "source_product": "vista_monthly_archive",
        "center_dekad": center_dekad,
        "center_month": int(center_dekad[:2]),
        "center_dekad_number": int(center_dekad[3]),
        "center_day_start": dr["day_start"],
        "center_day_end": dr["day_end"],
        "year_start": year_start,
        "year_end": year_end,
        "actual_year_start": min(years),
        "actual_year_end": max(years),
        "n_years": len(years),
        "years": ",".join(str(year) for year in years),
        "source_assets": ",".join(asset_ids),
        "excluded_source_dekads": ",".join(excluded_source_dekads),
        "quality_control_exclusion_count": len(excluded_source_dekads),
        "scale_note": (
            "Raw year counts per class from Phase 4 rolling monthly archive "
            "assets. Compute percentages as band.divide(N_valid).multiply(100)."
        ),
        "system:description": (
            "VISTA monthly archive count statistics. "
            f"Center dekad {center_dekad}; source years {min(years)}-{max(years)}."
        ),
    })


def process_center_dekad(
    center_dekad: str,
    all_assets: list[dict],
    settings: dict,
    dry_run: bool,
    overwrite: bool,
) -> str:
    log(f"[{center_dekad}] Filtering monthly archive assets ...")
    matched = filter_monthly_assets_for_center(
        all_assets,
        center_dekad,
        settings["year_start"],
        settings["year_end"],
    )
    if not matched:
        log(f"[{center_dekad}] No matching monthly archive assets found - skipped.")
        return "missing_source"

    asset_ids = [asset.asset_id for asset in matched]
    years = [asset.year for asset in matched]
    relevant_exclusions = exclusions_affecting_monthly_statistic(
        center_dekad,
        set(years),
        settings.get("quality_control_exclusions", {}),
    )
    excluded_source_dekads = tuple(item.label for item in relevant_exclusions)
    log(
        f"[{center_dekad}] Found {len(asset_ids)} monthly assets "
        f"({min(years)}-{max(years)})"
    )

    reference_image = ee.Image(asset_ids[0])
    crs, crs_transform = resolve_projection(reference_image)
    log(f"[{center_dekad}] Projection: {crs}  transform: {crs_transform}")

    count_image = build_count_image(asset_ids)
    count_image = annotate_image(
        count_image,
        center_dekad,
        asset_ids,
        years,
        settings["year_start"],
        settings["year_end"],
        excluded_source_dekads=excluded_source_dekads,
    )

    output_asset_id = f"{settings['statistics_asset_root']}/vista_count_monthly_{center_dekad}"
    if asset_exists(output_asset_id):
        log(f"[{center_dekad}] Asset already exists - skipping asset export.")
        log(f"       To re-export, delete it first: earthengine rm {output_asset_id}")
    else:
        submit_asset_export(
            count_image,
            output_asset_id,
            crs,
            crs_transform,
            dry_run,
            description=f"monthly_count_{center_dekad}",
            pyramiding_policy="mode",
        )

    if settings["export_gcs"]:
        submit_gcs_export(
            count_image,
            project=settings["runner_project_id"],
            bucket=settings["export_bucket_name"],
            file_name_prefix=f"{settings['export_prefix']}/vista_count_monthly_{center_dekad}",
            crs=crs,
            crs_transform=crs_transform,
            dry_run=dry_run,
            overwrite=overwrite,
            description=f"gcs_monthly_count_{center_dekad}",
        )
    return "processed"


def main(statistic_override: str | None = None) -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()
    initialize_run_log_from_config(config_path, "phase5")

    try:
        settings = load_phase5_settings(
            config_path,
            statistic_override=statistic_override or args.statistic or "count",
        )
        settings["quality_control_exclusions"] = load_dekad_exclusions()
    except ValueError as exc:
        log(f"ERROR: {exc}")
        return 1

    if args.year_start is not None:
        settings["year_start"] = args.year_start
    if args.year_end is not None:
        settings["year_end"] = args.year_end
    if settings["year_start"] > settings["year_end"]:
        log("ERROR: year-start must be <= year-end")
        return 1
    if args.execute:
        settings["dry_run"] = False
    elif args.dry_run:
        settings["dry_run"] = args.dry_run
    if args.export_gcs is not None:
        settings["export_gcs"] = args.export_gcs

    if args.center_dekads is not None:
        center_arg = " ".join(args.center_dekads)
        centers_to_process = parse_center_dekads(center_arg.replace(" ", ","))
        centers_source = "CLI"
    else:
        centers_to_process = settings["center_dekads"]
        centers_source = "config"

    unknown = [center for center in centers_to_process if center not in ALL_DEKADS]
    if unknown:
        log(f"ERROR: Unknown center dekad key(s): {', '.join(unknown)}")
        log(f"       Valid keys: {', '.join(ALL_DEKADS)}")
        return 1

    log("=" * 60)
    log("PHASE 5 - MONTHLY ARCHIVE COUNT STATISTICS (GEE Python API)")
    log("=" * 60)
    log(f"Runner project        : {settings['runner_project_id']}")
    log(f"Source project        : {settings['source_project_id']}")
    log(f"Monthly archive root  : {settings['source_asset_root']}")
    log(f"Target project        : {settings['target_project_id']}")
    log(f"Statistics asset root : {settings['statistics_asset_root']}")
    if settings["export_gcs"]:
        log(f"GCS output            : gs://{settings['export_bucket_name']}/{settings['export_prefix']}/")
    else:
        log("GCS output            : disabled")
    log(f"Year range            : {settings['year_start']} - {settings['year_end']}")
    log(f"Center dekads         : {len(centers_to_process)}  (source: {centers_source})")
    log(f"Dry run               : {settings['dry_run']}")
    log(f"Export GCS            : {settings['export_gcs']}")
    log(f"Overwrite             : {settings['overwrite']}")
    log("")

    try:
        ee.Initialize(project=settings["runner_project_id"])
    except ee.EEException as exc:
        log(f"ERROR: Earth Engine initialisation failed: {exc}")
        log("       Run `earthengine authenticate` or set GOOGLE_APPLICATION_CREDENTIALS.")
        return 1

    log(f"Listing monthly archive assets under: {settings['source_asset_root']} ...")
    try:
        all_assets = list_assets_for_folder(settings["source_asset_root"])
    except ee.EEException as exc:
        log(f"ERROR listing monthly archive assets: {exc}")
        return 1
    log(f"Total monthly archive assets found: {len(all_assets)}\n")

    processed: list[str] = []
    missing_source: list[str] = []
    errors: list[str] = []
    for center in centers_to_process:
        try:
            status = process_center_dekad(
                center,
                all_assets,
                settings,
                settings["dry_run"],
                settings["overwrite"],
            )
            if status == "missing_source":
                missing_source.append(center)
            else:
                processed.append(center)
        except Exception as exc:  # noqa: BLE001
            msg = f"[{center}] FAILED: {exc}"
            log(msg)
            errors.append(msg)
        log("")

    log("Phase 5 count summary:")
    log(f"  Processed center dekads : {len(processed)}")
    log(f"  Skipped missing source  : {len(missing_source)}")
    if missing_source:
        log(f"  Missing source centers  : {', '.join(missing_source)}")
    log(f"  Failed center dekads    : {len(errors)}")

    if errors:
        log("Phase 5 count completed with error(s):")
        for err in errors:
            log(f"  {err}")
        return 1

    log("Phase 5 count complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
