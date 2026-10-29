"""
Phase 3 implementation: step=dekadal, statistic=count
======================================================

For each dekad (e.g. "01d1" … "12d3") this module:

  1. Lists all input assets under ``archive_asset_root`` whose names contain
     the dekad string and whose embedded year falls in [year_start, year_end].
  2. Builds a 14-band count image (one uint8 band per class, plus summary
     bands N_tot / N_valid / N_flood_any / N_flood_medium_high).
  3. Exports the result to:
       • A GEE asset:  ``statistics_asset_root/vista_count_<dekad>``
         (skipped when the asset already exists)
       • A GCS object: ``gs://<export_bucket_name>/<export_prefix>/vista_count_<dekad>.tif``
         (always submitted, Cloud-Optimised GeoTIFF)

The export region is hardcoded to [-180, -75, 180, 75] in EPSG:4326 (planar),
matching the confirmed transform bounds of the VIIRS archive rasters.

Entry points
------------
``run(settings)``
    Called by the Phase 3 dispatcher when step='dekadal' and
    statistic='count'. *settings* must contain at least ``config_path``
    (a ``pathlib.Path``); all other values are loaded from config.yaml.
    Dispatcher-provided ``step`` and ``statistic`` override the matching config
    values when deriving environment-specific output paths.

``main()``
    Convenience shim for direct execution::

        python -m core.phase3_gee.calculate_viirs_archive_statistics [--config ...] [--dry-run]
            [--dekads 01d1 01d2 ...] [--year-start YYYY] [--year-end YYYY]
"""

from __future__ import annotations

import argparse
from pathlib import Path
import ee
from core.quality_control.exclusions import load_dekad_exclusions
from core.phase3_gee.config_utils import load_phase3_settings
from core.phase3_gee.constants import ALL_DEKADS, DEKAD_DAY_RANGES
from core.phase3_gee.ee_utils import (
    asset_exists,
    filter_assets_for_dekad,
    list_assets_for_folder,
    resolve_projection,
    submit_asset_export,
    submit_gcs_export,
)

from core.phase1_local.process_dekad_composites import (
    CONFIG_PATH,
    initialize_run_log_from_config,
    log,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Export region — matches confirmed VIIRS archive transform bounds.
#: ``False`` = planar (non-geodesic), required for pixel-aligned export in
#: EPSG:4326.


# ---------------------------------------------------------------------------
# Config / CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 3: compute per-dekad flood class count statistics from the "
            "fully-uploaded VIIRS archive in Google Earth Engine and export "
            "results to a GEE asset and a GCS Cloud-Optimised GeoTIFF."
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
        help="First year to include (overrides config).",
    )
    parser.add_argument(
        "--year-end",
        type=int,
        default=None,
        help="Last year to include (overrides config).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Log what would be exported without submitting any EE tasks.",
    )
    return parser.parse_args()




# ---------------------------------------------------------------------------
# Count-image builder
# ---------------------------------------------------------------------------

def build_count_image(asset_ids: list[str]) -> ee.Image:
    """
    Stack the single-band uint8 class images in *asset_ids* and produce a
    14-band count image.

    Bands
    -----
    class_0_nodata … class_11_permanent_water  — raw year counts (uint8)
    N_tot           — total observations
    N_valid         — observations excluding cloud (9) and nodata (0)
    N_flood_any     — classes 1+2+3
    N_flood_medium_high — classes 2+3
    """
    collection = ee.ImageCollection([ee.Image(aid) for aid in asset_ids])

    def count_class(val: int, name: str) -> ee.Image:
        return (
            collection
            .map(lambda img: img.select(0).byte().eq(val).rename(name))
            .sum()
            .toUint8()
        )

    c0  = count_class(0,  "class_0_nodata")
    c1  = count_class(1,  "class_1_flood_low")
    c2  = count_class(2,  "class_2_flood_medium")
    c3  = count_class(3,  "class_3_flood_high")
    c4  = count_class(4,  "class_4_snow")
    c5  = count_class(5,  "class_5_river_lake_ice")
    c6  = count_class(6,  "class_6_supra_snow_melting_ice")
    c9  = count_class(9,  "class_9_cloud")
    c10 = count_class(10, "class_10_land")
    c11 = count_class(11, "class_11_permanent_water")

    n_tot             = c0.add(c1).add(c2).add(c3).add(c4).add(c5).add(c6).add(c9).add(c10).add(c11).toUint8().rename("N_tot")
    n_valid           = n_tot.subtract(c9).subtract(c0).toUint8().rename("N_valid")
    n_flood_any       = c1.add(c2).add(c3).toUint8().rename("N_flood_any")
    n_flood_med_high  = c2.add(c3).toUint8().rename("N_flood_medium_high")

    return ee.Image([c0, c1, c2, c3, c4, c5, c6, c9, c10, c11, n_tot, n_valid, n_flood_any, n_flood_med_high])


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------

def annotate_image(
    image: ee.Image,
    dekad: str,
    asset_ids: list[str],
    year_start: int,
    year_end: int,
    excluded_source_dekads: tuple[str, ...] = (),
) -> ee.Image:
    """Attach metadata properties to *image*."""
    dr = DEKAD_DAY_RANGES[dekad]
    n_years = len(asset_ids)
    years_list = ",".join(str(int(aid.split("/")[-1][12:16])) for aid in asset_ids)

    return image.set({
        "dekad":        dekad,
        "dekad_month":  int(dekad[:2]),
        "dekad_number": int(dekad[3]),
        "day_start":    dr["day_start"],
        "day_end":      dr["day_end"],
        "doy_start":    dr["doy_start"],
        "doy_end":      dr["doy_end"],
        "year_start":   year_start,
        "year_end":     year_end,
        "n_years":      n_years,
        "years":        years_list,
        "excluded_source_dekads": ",".join(excluded_source_dekads),
        "quality_control_exclusion_count": len(excluded_source_dekads),
        "scale_note":   (
            "Raw year counts per class (uint8). "
            "Compute percentages as band.divide(N_valid).multiply(100)."
        ),
        "system:description": (
            f"VISTA dekadal flood class count statistics. "
            f"Dekad {dekad} ({dr['day_start']} to {dr['day_end']})."
        ),
    })


# ---------------------------------------------------------------------------
# Per-dekad orchestration
# ---------------------------------------------------------------------------

def process_dekad(
    dekad: str,
    all_assets: list[dict],
    settings: dict,
    dry_run: bool,
    overwrite: bool = True,
) -> None:
    """Full pipeline for a single dekad: filter → build → export."""
    archive_root       = settings["archive_asset_root"]
    statistics_root    = settings["statistics_asset_root"]
    bucket             = settings["export_bucket_name"]
    gcs_prefix         = settings["export_prefix"]
    year_start         = settings["year_start"]
    year_end           = settings["year_end"]
    exclusions         = settings.get("quality_control_exclusions", {})
    excluded_labels    = set(exclusions)
    relevant_exclusions = tuple(
        sorted(label for label in excluded_labels if label[4:] == dekad)
    )

    log(f"[{dekad}] Filtering assets …")
    matched = filter_assets_for_dekad(
        all_assets,
        dekad,
        year_start,
        year_end,
        excluded_labels=excluded_labels,
    )
    if relevant_exclusions:
        log(f"[{dekad}] Quality-control exclusions: {', '.join(relevant_exclusions)}")

    if not matched:
        log(f"[{dekad}] No matching assets found — skipped.")
        return

    asset_ids   = [a.get("id") or a.get("name") for a in matched]
    actual_year_start = int(asset_ids[0].split("/")[-1][12:16])
    actual_year_end   = int(asset_ids[-1].split("/")[-1][12:16])
    n_years     = len(asset_ids)
    dr          = DEKAD_DAY_RANGES[dekad]

    log(
        f"[{dekad}] Found {n_years} assets "
        f"({actual_year_start}–{actual_year_end})  "
        f"window: {dr['day_start']} → {dr['day_end']}"
    )

    # Resolve CRS from the first asset (all archive rasters share the same
    # projection, so one lookup is sufficient).
    reference_image = ee.Image(asset_ids[0])
    crs, crs_transform = resolve_projection(reference_image)
    log(f"[{dekad}] Projection: {crs}  transform: {crs_transform}")

    count_image = build_count_image(asset_ids)
    count_image = annotate_image(
        count_image,
        dekad,
        asset_ids,
        actual_year_start,
        actual_year_end,
        excluded_source_dekads=relevant_exclusions,
    )

    output_asset_id = f"{statistics_root}/vista_count_{dekad}"

    # ── Asset export ───────────────────────────────────────────────────────
    if asset_exists(output_asset_id):
        log(f"[{dekad}] Asset already exists — skipping asset export.")
        log(f"       To re-export, delete it first:")
        log(f"       earthengine rm {output_asset_id}")
    else:
        submit_asset_export(count_image, output_asset_id, crs, crs_transform, dry_run, description=f"count_{dekad}")

    # ── GCS export ─────────────────────────────────────────────────────────
    submit_gcs_export(
        count_image,
        project=settings["project_id"],
        bucket=bucket,
        file_name_prefix=f"{gcs_prefix}/vista_count_{dekad}",
        crs=crs,
        crs_transform=crs_transform,
        dry_run=dry_run,
        overwrite=overwrite,
        description=f"gcs_count_{dekad}",
    )



# ---------------------------------------------------------------------------
# Public entry point for the dispatcher
# ---------------------------------------------------------------------------

def run(settings: dict) -> int:
    """
    Entry point called by the Phase 3 dispatcher.

    *settings* must contain ``config_path`` (a ``pathlib.Path``).
    All phase3 configuration is re-read from that file so that this function
    behaves identically whether invoked via the dispatcher or directly.
    """
    import sys
    # Inject --config so that parse_args() inside main() picks it up, while
    # leaving any other flags (--dekads, --dry-run, etc.) already in sys.argv
    # untouched.  Only add it if the caller has not already done so.
    config_path = settings.get("config_path")
    if config_path is not None:
        config_str = str(config_path)
        if "--config" not in sys.argv:
            sys.argv += ["--config", config_str]
    return main(
        step_override=settings.get("step"),
        statistic_override=settings.get("statistic"),
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main(step_override: str | None = None, statistic_override: str | None = None) -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()
    initialize_run_log_from_config(config_path, "phase3")
    settings = load_phase3_settings(
        config_path,
        step_override=step_override or "dekadal",
        statistic_override=statistic_override or "count",
    )
    settings["quality_control_exclusions"] = load_dekad_exclusions()

    # CLI overrides — each is applied only when explicitly passed
    if args.year_start is not None:
        settings["year_start"] = args.year_start
    if args.year_end is not None:
        settings["year_end"] = args.year_end
    if args.dry_run is not None:
        settings["dry_run"] = args.dry_run

    dry_run = settings["dry_run"]

    # Dekad selection: CLI wins over config (config already resolved 'all')
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

    # Validate dekad keys
    unknown = [d for d in dekads_to_process if d not in DEKAD_DAY_RANGES]
    if unknown:
        log(f"ERROR: Unknown dekad key(s): {', '.join(unknown)}")
        log(f"       Valid keys: {', '.join(ALL_DEKADS)}")
        return 1

    log("=" * 60)
    log("PHASE 3 — VIIRS ARCHIVE STATISTICS (GEE Python API)")
    log("=" * 60)
    log(f"GCP project           : {settings['project_id']}")
    log(f"Step / statistic      : {settings['step']} / {settings['statistic']}")
    log(f"Archive asset root    : {settings['archive_asset_root']}")
    log(f"Statistics asset root : {settings['statistics_asset_root']}")
    log(f"GCS output            : gs://{settings['export_bucket_name']}/{settings['export_prefix']}/")
    log(f"Year range            : {settings['year_start']} – {settings['year_end']}")
    log(f"Dekads to process     : {len(dekads_to_process)}  (source: {dekads_source})")
    log(f"Dry run               : {dry_run}")
    log(f"Overwrite             : {settings['overwrite']}")
    log(
        "QC excluded dekads    : "
        + (", ".join(settings["quality_control_exclusions"]) or "none")
    )
    log(f"Export region         : [-180, -75, 180, 75]")
    log("")

    # Initialise Earth Engine (uses application-default credentials)
    try:
        ee.Initialize(project=settings["project_id"])
    except ee.EEException as exc:
        log(f"ERROR: Earth Engine initialisation failed: {exc}")
        log("       Run `earthengine authenticate` or set GOOGLE_APPLICATION_CREDENTIALS.")
        return 1

    # Fetch the full archive asset listing once (avoids one API call per dekad)
    log(f"Listing assets under: {settings['archive_asset_root']} …")
    try:
        all_assets = list_assets_for_folder(settings["archive_asset_root"])
    except ee.EEException as exc:
        log(f"ERROR listing archive assets: {exc}")
        return 1
    log(f"Total assets found: {len(all_assets)}\n")

    errors: list[str] = []
    for dekad in dekads_to_process:
        try:
            process_dekad(dekad, all_assets, settings, dry_run, settings["overwrite"])
        except Exception as exc:  # noqa: BLE001
            msg = f"[{dekad}] FAILED: {exc}"
            log(msg)
            errors.append(msg)
        log("")

    if errors:
        log(f"Phase 3 completed with {len(errors)} error(s):")
        for err in errors:
            log(f"  {err}")
        return 1

    log(f"Phase 3 complete — {len(dekads_to_process)} dekad(s) processed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
