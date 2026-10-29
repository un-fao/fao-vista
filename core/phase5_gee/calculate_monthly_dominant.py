"""Phase 5 implementation: monthly archive dominant-class statistics."""

from __future__ import annotations

import argparse
from pathlib import Path

import ee
from core.phase1_local.process_dekad_composites import (
    CONFIG_PATH,
    initialize_run_log_from_config,
    log,
)
from core.phase3_gee.calculate_dekadal_dominant import (
    DOMINANT_PRIORITY,
    MIN_CLOUD_FALLBACK_FREQUENCY,
    REAL_FALLBACK_PRIORITY,
    REQUIRED_COUNT_BANDS,
    build_dominant_image,
)
from core.phase3_gee.constants import ALL_DEKADS, DEKAD_DAY_RANGES
from core.phase3_gee.ee_utils import (
    asset_exists,
    resolve_projection,
    submit_asset_export,
    submit_gcs_export,
)
from core.phase5_gee.config_utils import load_phase5_settings, parse_center_dekads


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 5: compute dominant-class statistics from existing Phase 5 "
            "monthly count assets."
        )
    )
    parser.add_argument("--config", default=str(CONFIG_PATH), help="Path to config/config.yaml.")
    parser.add_argument(
        "--statistic",
        default=None,
        choices=["dominant", "dominant_class"],
        help="Statistic to run; this module supports dominant.",
    )
    parser.add_argument(
        "--center-dekads",
        nargs="+",
        default=None,
        metavar="CENTER",
        help="Center dekads to process, e.g. 01d1 01d2, all, or 01d1-03d3.",
    )
    parser.add_argument(
        "--year-start",
        type=int,
        default=None,
        help="Accepted for CLI compatibility; dominant reads existing count assets.",
    )
    parser.add_argument(
        "--year-end",
        type=int,
        default=None,
        help="Accepted for CLI compatibility; dominant reads existing count assets.",
    )
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


def _validate_count_bands(count_image: ee.Image, count_asset_id: str) -> None:
    band_names = count_image.bandNames().getInfo()
    missing = [band for band in REQUIRED_COUNT_BANDS if band not in band_names]
    if missing:
        raise ValueError(
            f"count asset {count_asset_id} is missing required band(s): "
            + ", ".join(missing)
        )


def annotate_image(
    image: ee.Image,
    center_dekad: str,
    count_asset_id: str,
) -> ee.Image:
    dr = DEKAD_DAY_RANGES[center_dekad]
    return image.set({
        "phase": 5,
        "product": "vista_monthly_statistics",
        "statistic": "dominant_class",
        "source_product": "vista_monthly_statistics_count",
        "center_dekad": center_dekad,
        "center_month": int(center_dekad[:2]),
        "center_dekad_number": int(center_dekad[3]),
        "center_day_start": dr["day_start"],
        "center_day_end": dr["day_end"],
        "source_count_asset": count_asset_id,
        "value_type": "dominant_class",
        "dominant_priority": ",".join(str(value) for value in DOMINANT_PRIORITY),
        "cloud_fallback_priority": ",".join(str(value) for value in REAL_FALLBACK_PRIORITY),
        "cloud_fallback_min_frequency": MIN_CLOUD_FALLBACK_FREQUENCY,
        "cloud_fallback_denominator": "N_tot",
        "aggregated_class_12": "flood_any",
        "aggregated_class_12_source": "N_flood_any",
        "scale_note": (
            "Single uint8 dominant_class band. Flood low, medium, and high "
            "are aggregated into output class 12 using N_flood_any before "
            "dominance is computed. Highest count wins; ties use priority "
            "11,12,6,5,4,10,9,0. If cloud is dominant, the best real "
            "fallback class is promoted only when fallback_count/N_tot is "
            "at least 0.30."
        ),
        "system:description": (
            "VISTA monthly archive dominant-class statistics derived from "
            f"count asset. Center dekad {center_dekad}."
        ),
    })


def process_center_dekad(center_dekad: str, settings: dict, dry_run: bool, overwrite: bool) -> str:
    count_asset_id = f"{settings['count_asset_root']}/vista_count_monthly_{center_dekad}"
    output_asset_id = f"{settings['statistics_asset_root']}/vista_dominant_class_monthly_{center_dekad}"

    log(f"[{center_dekad}] Count source: {count_asset_id}")
    if not asset_exists(count_asset_id):
        log(f"[{center_dekad}] Count asset missing - skipped.")
        return "missing_count"

    count_image = ee.Image(count_asset_id)
    _validate_count_bands(count_image, count_asset_id)
    crs, crs_transform = resolve_projection(count_image)
    log(f"[{center_dekad}] Projection: {crs}  transform: {crs_transform}")

    dominant_image = ee.Image(
        annotate_image(
            build_dominant_image(count_image),
            center_dekad,
            count_asset_id,
        ).copyProperties(
            count_image,
            ["excluded_source_dekads", "quality_control_exclusion_count"],
        )
    )

    if asset_exists(output_asset_id):
        log(f"[{center_dekad}] Asset already exists - skipping asset export.")
        log(f"       To re-export, delete it first: earthengine rm {output_asset_id}")
    else:
        submit_asset_export(
            dominant_image,
            output_asset_id,
            crs,
            crs_transform,
            dry_run,
            description=f"monthly_dominant_class_{center_dekad}",
            pyramiding_policy="mode",
        )

    if settings["export_gcs"]:
        submit_gcs_export(
            dominant_image,
            project=settings["runner_project_id"],
            bucket=settings["export_bucket_name"],
            file_name_prefix=f"{settings['export_prefix']}/vista_dominant_class_monthly_{center_dekad}",
            crs=crs,
            crs_transform=crs_transform,
            dry_run=dry_run,
            overwrite=overwrite,
            description=f"gcs_monthly_dominant_class_{center_dekad}",
        )
    return "processed"


def main(statistic_override: str | None = None) -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()
    initialize_run_log_from_config(config_path, "phase5")

    try:
        settings = load_phase5_settings(
            config_path,
            statistic_override=statistic_override or args.statistic or "dominant",
        )
    except ValueError as exc:
        log(f"ERROR: {exc}")
        return 1

    if args.execute:
        settings["dry_run"] = False
    elif args.dry_run:
        settings["dry_run"] = args.dry_run
    if args.export_gcs is not None:
        settings["export_gcs"] = args.export_gcs
    if args.year_start is not None or args.year_end is not None:
        log(
            "NOTE: --year-start/--year-end are ignored for dominant; "
            "the existing monthly count assets define the source period."
        )

    if args.center_dekads is not None:
        center_arg = " ".join(args.center_dekads)
        centers_to_process = parse_center_dekads(center_arg.replace(" ", ","))
        centers_source = "CLI"
    else:
        centers_to_process = settings["center_dekads"]
        centers_source = "config"

    log("=" * 60)
    log("PHASE 5 - MONTHLY ARCHIVE DOMINANT-CLASS STATISTICS (GEE Python API)")
    log("=" * 60)
    log(f"Runner project        : {settings['runner_project_id']}")
    log(f"Count asset root      : {settings['count_asset_root']}")
    log(f"Statistics asset root : {settings['statistics_asset_root']}")
    if settings["export_gcs"]:
        log(f"GCS output            : gs://{settings['export_bucket_name']}/{settings['export_prefix']}/")
    else:
        log("GCS output            : disabled")
    log(f"Center dekads         : {len(centers_to_process)}  (source: {centers_source})")
    log(f"Dry run               : {settings['dry_run']}")
    log(f"Export GCS            : {settings['export_gcs']}")
    log(f"Overwrite             : {settings['overwrite']}")
    log(f"Cloud fallback        : best real class if count/N_tot >= {MIN_CLOUD_FALLBACK_FREQUENCY:.2f}")
    log("")

    unknown = [center for center in centers_to_process if center not in ALL_DEKADS]
    if unknown:
        log(f"ERROR: Unknown center dekad key(s): {', '.join(unknown)}")
        return 1

    try:
        ee.Initialize(project=settings["runner_project_id"])
    except ee.EEException as exc:
        log(f"ERROR: Earth Engine initialisation failed: {exc}")
        log("       Run `earthengine authenticate` or set GOOGLE_APPLICATION_CREDENTIALS.")
        return 1

    processed: list[str] = []
    missing_count: list[str] = []
    errors: list[str] = []
    for center in centers_to_process:
        try:
            status = process_center_dekad(
                center,
                settings,
                settings["dry_run"],
                settings["overwrite"],
            )
            if status == "missing_count":
                missing_count.append(center)
            else:
                processed.append(center)
        except Exception as exc:  # noqa: BLE001
            msg = f"[{center}] FAILED: {exc}"
            log(msg)
            errors.append(msg)
        log("")

    log("Phase 5 dominant-class summary:")
    log(f"  Processed center dekads : {len(processed)}")
    log(f"  Skipped missing count   : {len(missing_count)}")
    if missing_count:
        log(f"  Missing count centers   : {', '.join(missing_count)}")
    log(f"  Failed center dekads    : {len(errors)}")

    if errors:
        return 1
    log("Phase 5 dominant-class complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
