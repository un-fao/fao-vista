"""
Phase 3 implementation: step=dekadal, statistic=frequency
==========================================================

For each dekad (for example "01d1" or "12d3") this module reads the
corresponding Phase 3 count asset and derives frequency bands as float32
proportions in the 0.0-1.0 range. Frequency outputs keep the same band count
and semantic order as the source count assets, with ``freq_*`` output names.

Frequency outputs are written to:

  * Earth Engine asset:
    ``statistics_asset_root/dekadal/frequency/vista_frequency_<dekad>``
  * GCS GeoTIFF tiles:
    ``gs://<export_bucket_name>/<export_prefix>/vista_frequency_<dekad>.tif``

The source count asset is expected at:

  ``statistics_asset_root/dekadal/count/vista_count_<dekad>``

Missing count assets are skipped so one incomplete dekad does not stop the
whole frequency run.
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


FREQUENCY_BAND_SPECS: tuple[tuple[str, str, str], ...] = (
    ("freq_0_nodata", "class_0_nodata", "N_tot"),
    ("freq_1_flood_low", "class_1_flood_low", "N_valid"),
    ("freq_2_flood_medium", "class_2_flood_medium", "N_valid"),
    ("freq_3_flood_high", "class_3_flood_high", "N_valid"),
    ("freq_4_snow", "class_4_snow", "N_valid"),
    ("freq_5_river_lake_ice", "class_5_river_lake_ice", "N_valid"),
    ("freq_6_supra_snow_melting_ice", "class_6_supra_snow_melting_ice", "N_valid"),
    ("freq_9_cloud", "class_9_cloud", "N_tot"),
    ("freq_10_land", "class_10_land", "N_valid"),
    ("freq_11_permanent_water", "class_11_permanent_water", "N_valid"),
    ("freq_tot", "N_tot", "N_tot"),
    ("freq_valid", "N_valid", "N_tot"),
    ("freq_flood_any", "N_flood_any", "N_valid"),
    ("freq_flood_medium_high", "N_flood_medium_high", "N_valid"),
)

_required_count_bands: list[str] = []
for _, source_band, denominator_band in FREQUENCY_BAND_SPECS:
    for required_band in (source_band, denominator_band):
        if required_band not in _required_count_bands:
            _required_count_bands.append(required_band)

REQUIRED_COUNT_BANDS: tuple[str, ...] = tuple(_required_count_bands)


def _join_remote_path(*parts: str) -> str:
    return "/".join(str(part).strip().strip("/") for part in parts if str(part).strip().strip("/"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 3: compute per-dekad frequency statistics from existing "
            "Phase 3 count assets and export results to Earth Engine and GCS."
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
        help="Accepted for CLI compatibility; frequency reads existing count assets.",
    )
    parser.add_argument(
        "--year-end",
        type=int,
        default=None,
        help="Accepted for CLI compatibility; frequency reads existing count assets.",
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


def build_frequency_image(count_image: ee.Image) -> ee.Image:
    """
    Build the configured frequency bands from a Phase 3 count image.

    Output bands are named ``freq_*`` and follow the count asset semantic
    order. Bands with denominator N_valid are masked where N_valid is 0;
    bands with denominator N_tot are masked where N_tot is 0.
    """
    frequency_bands: list[ee.Image] = []
    for output_band, count_band, denominator_band in FREQUENCY_BAND_SPECS:
        numerator = count_image.select(count_band).toFloat()
        denominator = count_image.select(denominator_band).toFloat()
        denominator_mask = denominator.gt(0)
        frequency_bands.append(
            numerator
            .divide(denominator.updateMask(denominator_mask))
            .updateMask(denominator_mask)
            .rename(output_band)
            .toFloat()
        )

    return ee.Image(frequency_bands).toFloat()


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
        "value_scale": "0.0-1.0",
        "value_type": "frequency",
        "scale_note": (
            "Float32 frequency bands named freq_* in 0.0-1.0 scale. "
            "Frequency output keeps the same semantic order as count. "
            "Nodata, cloud, freq_tot, and freq_valid use N_tot as "
            "denominator; other bands use N_valid. Bands are masked where "
            "their denominator is 0."
        ),
        "system:description": (
            f"VISTA dekadal frequency statistics derived from count asset. "
            f"Dekad {dekad} ({dr['day_start']} to {dr['day_end']})."
        ),
    })


def process_dekad(
    dekad: str,
    settings: dict,
    dry_run: bool,
    overwrite: bool = True,
) -> str:
    """Build and export one dekad frequency product."""
    count_root = _count_asset_root(settings)
    frequency_root = settings["statistics_asset_root"]
    bucket = settings["export_bucket_name"]
    gcs_prefix = settings["export_prefix"]

    count_asset_id = f"{count_root}/vista_count_{dekad}"
    output_asset_id = f"{frequency_root}/vista_frequency_{dekad}"

    log(f"[{dekad}] Count source: {count_asset_id}")
    if not asset_exists(count_asset_id):
        log(f"[{dekad}] Count asset missing - skipped.")
        return "missing_count"

    count_image = ee.Image(count_asset_id)
    _validate_count_bands(count_image, count_asset_id)

    crs, crs_transform = resolve_projection(count_image)
    log(f"[{dekad}] Projection: {crs}  transform: {crs_transform}")

    frequency_image = build_frequency_image(count_image)
    frequency_image = ee.Image(
        annotate_image(frequency_image, dekad, count_asset_id).copyProperties(
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
            frequency_image,
            output_asset_id,
            crs,
            crs_transform,
            dry_run,
            description=f"frequency_{dekad}",
            pyramiding_policy="mean",
        )

    submit_gcs_export(
        frequency_image,
        bucket=bucket,
        file_name_prefix=f"{gcs_prefix}/vista_frequency_{dekad}",
        crs=crs,
        crs_transform=crs_transform,
        dry_run=dry_run,
        overwrite=overwrite,
        description=f"gcs_frequency_{dekad}",
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
        statistic_override=statistic_override or "frequency",
    )

    if args.dry_run is not None:
        settings["dry_run"] = args.dry_run

    dry_run = settings["dry_run"]

    if args.year_start is not None or args.year_end is not None:
        log(
            "NOTE: --year-start/--year-end are ignored for frequency; "
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
    log("PHASE 3 - VIIRS ARCHIVE FREQUENCY STATISTICS (GEE Python API)")
    log("=" * 60)
    log(f"GCP project           : {settings['project_id']}")
    log(f"Step / statistic      : {settings['step']} / {settings['statistic']}")
    log(f"Count asset root      : {_count_asset_root(settings)}")
    log(f"Statistics asset root : {settings['statistics_asset_root']}")
    log(f"GCS output            : gs://{settings['export_bucket_name']}/{settings['export_prefix']}/")
    log(f"Dekads to process     : {len(dekads_to_process)}  (source: {dekads_source})")
    log(f"Dry run               : {dry_run}")
    log(f"Overwrite             : {settings['overwrite']}")
    log(f"Export region         : [-180, -75, 180, 75]")
    log("")

    try:
        ee.Initialize()
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

    log("Phase 3 frequency summary:")
    log(f"  Processed dekads          : {len(processed)}")
    log(f"  Skipped missing count     : {len(missing_count)}")
    if missing_count:
        log(f"  Missing count dekads      : {', '.join(missing_count)}")
    log(f"  Failed dekads             : {len(errors)}")

    if errors:
        log("Phase 3 frequency completed with error(s):")
        for err in errors:
            log(f"  {err}")
        return 1

    log("Phase 3 frequency complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
