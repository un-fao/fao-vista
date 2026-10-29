"""
Phase 3 implementation: step=dekadal, statistic=dominant
=========================================================

For each dekad (for example "01d1" or "12d3") this module reads the
corresponding Phase 3 count asset and derives a single uint8 categorical band
containing the dominant class for each pixel.

The source count asset is expected at:

  ``statistics_asset_root/dekadal/count/vista_count_<dekad>``

Dominant-class outputs are written to:

  * Earth Engine asset:
    ``statistics_asset_root/dekadal/dominant_class/vista_dominant_class_<dekad>``
  * GCS GeoTIFF tiles:
    ``gs://<export_bucket_name>/<export_prefix>/vista_dominant_class_<dekad>.tif``

The dominant class is selected from semantic groups. Flood low, medium, and
high are aggregated into output class 12 (flood_any) before dominance is
computed, preventing flood presence from being split across intensity classes.
Ties are resolved with this priority, from highest to lowest:

  11, 12, 6, 5, 4, 10, 9, 0

If cloud (9) is the initial dominant class, the best real-class fallback is
promoted only when its count represents at least 30 percent of N_tot. Real
fallback candidates exclude cloud (9) and nodata (0).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import ee

from core.phase1_local.process_dekad_composites import (
    CONFIG_PATH,
    initialize_run_log_from_config,
    log,
)
from core.phase3_gee.config_utils import load_phase3_settings
from core.phase3_gee.constants import ALL_DEKADS, DEKAD_DAY_RANGES
from core.phase3_gee.ee_utils import (
    asset_exists,
    resolve_projection,
    submit_asset_export,
    submit_gcs_export,
)


DOMINANT_CLASS_BANDS: dict[int, str] = {
    0: "class_0_nodata",
    4: "class_4_snow",
    5: "class_5_river_lake_ice",
    6: "class_6_supra_snow_melting_ice",
    9: "class_9_cloud",
    10: "class_10_land",
    11: "class_11_permanent_water",
    12: "N_flood_any",
}

DOMINANT_PRIORITY: tuple[int, ...] = (11, 12, 6, 5, 4, 10, 9, 0)
REAL_FALLBACK_PRIORITY: tuple[int, ...] = (11, 12, 6, 5, 4, 10)
REQUIRED_COUNT_BANDS: tuple[str, ...] = (
    "class_0_nodata",
    "class_4_snow",
    "class_5_river_lake_ice",
    "class_6_supra_snow_melting_ice",
    "class_9_cloud",
    "class_10_land",
    "class_11_permanent_water",
    "N_flood_any",
    "N_tot",
)
MIN_CLOUD_FALLBACK_FREQUENCY = 0.30


def _join_remote_path(*parts: str) -> str:
    return "/".join(str(part).strip().strip("/") for part in parts if str(part).strip().strip("/"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 3: compute per-dekad dominant-class statistics from "
            "existing Phase 3 count assets and export results to Earth "
            "Engine and GCS."
        )
    )
    parser.add_argument(
        "--config",
        default=str(CONFIG_PATH),
        help="Path to config/config.yaml.",
    )
    parser.add_argument(
        "--dekads",
        nargs="+",
        metavar="DEKAD",
        default=None,
        help=(
            "Dekad keys to process (e.g. 01d1 07d2), or the single keyword "
            "'all' to process all 36 dekads. Overrides phase3_gee.dekads in "
            "config.yaml when provided."
        ),
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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Log what would be exported without submitting any EE tasks.",
    )
    return parser.parse_args()


def _count_asset_root(settings: dict) -> str:
    return _join_remote_path(settings["statistics_product_root"], settings["step"], "count")


def _validate_count_bands(count_image: ee.Image, count_asset_id: str) -> None:
    band_names = count_image.bandNames().getInfo()
    missing = [band for band in REQUIRED_COUNT_BANDS if band not in band_names]
    if missing:
        raise ValueError(
            f"count asset {count_asset_id} is missing required band(s): "
            + ", ".join(missing)
        )


def _best_class_and_count(
    count_image: ee.Image,
    priority_high_to_low: tuple[int, ...],
) -> tuple[ee.Image, ee.Image]:
    """
    Return (class_image, count_image) for the highest-count dominant group.

    Iterating from low priority to high priority and replacing on >= makes
    later, higher-priority classes win ties.
    """
    priority_low_to_high = tuple(reversed(priority_high_to_low))
    first_class = priority_low_to_high[0]
    best_class = ee.Image.constant(first_class).toUint8().rename("best_class")
    best_count = count_image.select(DOMINANT_CLASS_BANDS[first_class]).toFloat().rename("best_count")

    for class_value in priority_low_to_high[1:]:
        candidate_count = count_image.select(DOMINANT_CLASS_BANDS[class_value]).toFloat().rename("best_count")
        replace = candidate_count.gte(best_count)
        best_class = best_class.where(replace, class_value)
        best_count = best_count.where(replace, candidate_count)

    return best_class.toUint8(), best_count.toFloat()


def build_dominant_image(count_image: ee.Image) -> ee.Image:
    """
    Build a single-band uint8 dominant-class image from a Phase 3 count image.

    The initial dominant class uses semantic groups. Flood low, medium, and
    high are represented by class 12 using N_flood_any. If the initial class is
    cloud (9), the best real fallback class is promoted when
    fallback_count / N_tot >= 0.30. Pixels with N_tot == 0 are forced to
    nodata (0), preventing tie priority from selecting permanent water when
    all class counts are zero.
    """
    initial_class, _ = _best_class_and_count(count_image, DOMINANT_PRIORITY)
    fallback_class, fallback_count = _best_class_and_count(count_image, REAL_FALLBACK_PRIORITY)

    n_tot = count_image.select("N_tot").toFloat()
    fallback_frequency = fallback_count.divide(n_tot)
    promote_fallback = (
        initial_class.eq(9)
        .And(n_tot.gt(0))
        .And(fallback_frequency.gte(MIN_CLOUD_FALLBACK_FREQUENCY))
    )

    return (
        initial_class
        .where(promote_fallback, fallback_class)
        .where(n_tot.eq(0), 0)
        .rename("dominant_class")
        .toUint8()
    )


def annotate_image(image: ee.Image, dekad: str, count_asset_id: str) -> ee.Image:
    dr = DEKAD_DAY_RANGES[dekad]
    return image.set({
        "dekad": dekad,
        "dekad_month": int(dekad[:2]),
        "dekad_number": int(dekad[3]),
        "day_start": dr["day_start"],
        "day_end": dr["day_end"],
        "doy_start": dr["doy_start"],
        "doy_end": dr["doy_end"],
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
            f"VISTA dekadal dominant-class statistics derived from count asset. "
            f"Dekad {dekad} ({dr['day_start']} to {dr['day_end']})."
        ),
    })


def process_dekad(
    dekad: str,
    settings: dict,
    dry_run: bool,
    overwrite: bool = True,
) -> str:
    """Build and export one dekad dominant-class product."""
    count_root = _count_asset_root(settings)
    dominant_root = settings["statistics_asset_root"]
    bucket = settings["export_bucket_name"]
    gcs_prefix = settings["export_prefix"]

    count_asset_id = f"{count_root}/vista_count_{dekad}"
    output_asset_id = f"{dominant_root}/vista_dominant_class_{dekad}"

    log(f"[{dekad}] Count source: {count_asset_id}")
    if not asset_exists(count_asset_id):
        log(f"[{dekad}] Count asset missing - skipped.")
        return "missing_count"

    count_image = ee.Image(count_asset_id)
    _validate_count_bands(count_image, count_asset_id)

    crs, crs_transform = resolve_projection(count_image)
    log(f"[{dekad}] Projection: {crs}  transform: {crs_transform}")

    dominant_image = build_dominant_image(count_image)
    dominant_image = ee.Image(
        annotate_image(dominant_image, dekad, count_asset_id).copyProperties(
            count_image,
            ["excluded_source_dekads", "quality_control_exclusion_count"],
        )
    )

    if asset_exists(output_asset_id):
        log(f"[{dekad}] Asset already exists - skipping asset export.")
        log("       To re-export, delete it first:")
        log(f"       earthengine rm {output_asset_id}")
    else:
        submit_asset_export(
            dominant_image,
            output_asset_id,
            crs,
            crs_transform,
            dry_run,
            description=f"dominant_class_{dekad}",
            pyramiding_policy="mode",
        )

    submit_gcs_export(
        dominant_image,
        project=settings["project_id"],
        bucket=bucket,
        file_name_prefix=f"{gcs_prefix}/vista_dominant_class_{dekad}",
        crs=crs,
        crs_transform=crs_transform,
        dry_run=dry_run,
        overwrite=overwrite,
        description=f"gcs_dominant_class_{dekad}",
    )
    return "processed"


def run(settings: dict) -> int:
    """
    Entry point called by the Phase 3 dispatcher.

    The full Phase 3 config is re-read here so direct module execution and
    dispatcher execution share the same behavior.
    """
    import sys

    config_path = settings.get("config_path")
    if config_path is not None:
        config_str = str(config_path)
        if "--config" not in sys.argv:
            sys.argv += ["--config", config_str]
    return main(
        step_override=settings.get("step"),
        statistic_override=settings.get("statistic"),
    )


def main(step_override: str | None = None, statistic_override: str | None = None) -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()
    initialize_run_log_from_config(config_path, "phase3")
    settings = load_phase3_settings(
        config_path,
        step_override=step_override or "dekadal",
        statistic_override=statistic_override or "dominant",
    )

    if args.dry_run is not None:
        settings["dry_run"] = args.dry_run

    dry_run = settings["dry_run"]

    if args.year_start is not None or args.year_end is not None:
        log(
            "NOTE: --year-start/--year-end are ignored for dominant; "
            "the existing count assets define the source period."
        )

    if args.dekads is not None:
        cli_dekads = args.dekads
        if len(cli_dekads) == 1 and cli_dekads[0].strip().lower() == "all":
            dekads_to_process = list(ALL_DEKADS)
            dekads_source = "CLI (all)"
        else:
            dekads_to_process = cli_dekads
            dekads_source = "CLI"
    else:
        dekads_to_process = settings["dekads"]
        dekads_source = "config"

    unknown = [d for d in dekads_to_process if d not in DEKAD_DAY_RANGES]
    if unknown:
        log(f"ERROR: Unknown dekad key(s): {', '.join(unknown)}")
        log(f"       Valid keys: {', '.join(ALL_DEKADS)}")
        return 1

    log("=" * 60)
    log("PHASE 3 - VIIRS ARCHIVE DOMINANT-CLASS STATISTICS (GEE Python API)")
    log("=" * 60)
    log(f"GCP project           : {settings['project_id']}")
    log(f"Step / statistic      : {settings['step']} / {settings['statistic']}")
    log(f"Count asset root      : {_count_asset_root(settings)}")
    log(f"Statistics asset root : {settings['statistics_asset_root']}")
    log(f"GCS output            : gs://{settings['export_bucket_name']}/{settings['export_prefix']}/")
    log(f"Dekads to process     : {len(dekads_to_process)}  (source: {dekads_source})")
    log(f"Dry run               : {dry_run}")
    log(f"Overwrite             : {settings['overwrite']}")
    log(f"Cloud fallback        : best real class if count/N_tot >= {MIN_CLOUD_FALLBACK_FREQUENCY:.2f}")
    log(f"Export region         : [-180, -75, 180, 75]")
    log("")

    try:
        ee.Initialize(project=settings["project_id"])
    except ee.EEException as exc:
        log(f"ERROR: Earth Engine initialisation failed: {exc}")
        log("       Run `earthengine authenticate` or set GOOGLE_APPLICATION_CREDENTIALS.")
        return 1

    processed: list[str] = []
    missing_count: list[str] = []
    errors: list[str] = []

    for dekad in dekads_to_process:
        try:
            status = process_dekad(dekad, settings, dry_run, settings["overwrite"])
            if status == "missing_count":
                missing_count.append(dekad)
            else:
                processed.append(dekad)
        except Exception as exc:  # noqa: BLE001
            msg = f"[{dekad}] FAILED: {exc}"
            log(msg)
            errors.append(msg)
        log("")

    log("Phase 3 dominant-class summary:")
    log(f"  Processed dekads          : {len(processed)}")
    log(f"  Skipped missing count     : {len(missing_count)}")
    if missing_count:
        log(f"  Missing count dekads      : {', '.join(missing_count)}")
    log(f"  Failed dekads             : {len(errors)}")

    if errors:
        log("Phase 3 dominant-class completed with error(s):")
        for err in errors:
            log(f"  {err}")
        return 1

    log("Phase 3 dominant-class complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
