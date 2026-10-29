"""
Shared Earth Engine utilities for Phase 3
==========================================

Functions in this module are stateless EE helpers used by all Phase 3
implementation modules. They must be called after ``ee.Initialize()``.
"""

from __future__ import annotations

from typing import Any

import ee
from google.cloud import storage

from core.quality_control.exclusions import archive_basename_dekad_label
from core.phase1_local.process_dekad_composites import log


def list_assets_for_folder(folder_id: str) -> list[dict]:
    """Return all assets under *folder_id*, handling EE pagination."""
    assets: list[dict] = []
    page_token: str | None = None
    while True:
        params: dict[str, Any] = {"parent": folder_id}
        if page_token:
            params["pageToken"] = page_token
        result = ee.data.listAssets(params)
        assets.extend(result.get("assets", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return assets


def filter_assets_for_dekad(
    all_assets: list[dict],
    dekad: str,
    year_start: int,
    year_end: int,
    excluded_labels: set[str] | frozenset[str] = frozenset(),
) -> list[dict]:
    """
    Return the subset of *all_assets* whose name contains *dekad* and whose
    embedded year (characters 12–15 of the asset basename) is in
    [year_start, year_end].  Duplicate years are deduplicated (first wins).
    Result is sorted by year ascending.
    """
    matched: dict[int, dict] = {}
    for asset in all_assets:
        asset_id = asset.get("id") or asset.get("name") or ""
        basename = asset_id.split("/")[-1]
        label = archive_basename_dekad_label(basename)
        if label is None or label[4:] != dekad:
            continue
        if label in excluded_labels:
            continue
        try:
            year = int(label[:4])
        except (ValueError, IndexError):
            continue
        if year < year_start or year > year_end:
            continue
        if year not in matched:
            matched[year] = asset
    return [matched[y] for y in sorted(matched)]


def asset_exists(asset_id: str) -> bool:
    """Return True if *asset_id* is accessible in EE."""
    try:
        info = ee.data.getAsset(asset_id)
        return bool(info)
    except ee.EEException:
        return False


def resolve_projection(image: ee.Image) -> tuple[str, list[float]]:
    """
    Return (crs, crs_transform) for *image*.

    ``projection().getInfo()`` returns a dict with keys ``"crs"`` and
    ``"transform"`` (a plain 6-element list) — no WKT parsing needed.
    """
    proj = image.select(0).projection().getInfo()
    return proj["crs"], proj["transform"]


def gcs_file_exists(bucket_name: str, blob_path: str) -> bool:
    """Return True if *blob_path* already exists in *bucket_name*."""
    client = storage.Client()
    return client.bucket(bucket_name).blob(blob_path).exists()

def gcs_prefix_exists(bucket_name: str, prefix: str) -> list[str]:
    client = storage.Client()
    blobs = client.bucket(bucket_name).list_blobs(prefix=prefix)
    return [b.name for b in blobs]

def _export_region() -> "ee.Geometry":
    return ee.Geometry.Rectangle([-180, -75, 180, 75], "EPSG:4326", False)


def submit_asset_export(
    image: "ee.Image",
    asset_id: str,
    crs: str,
    crs_transform: list,
    dry_run: bool,
    description: str = "export",
    pyramiding_policy: str = "mode",
) -> None:
    """
    Submit an EE asset export task.

    *description* is the human-readable task name shown in the EE task console.
    Callers should pass something like ``"count_01d1"`` or ``"frequency_07d2"``.
    *pyramiding_policy* defaults to ``"mode"`` for categorical/count products;
    continuous products such as frequencies should pass ``"mean"``.
    """
    if dry_run:
        log(f"  [dry-run] Would submit asset export -> {asset_id}")
        return
    ee.batch.Export.image.toAsset(
        image=image,
        description=description,
        assetId=asset_id,
        crs=crs,
        crsTransform=crs_transform,
        region=_export_region(),
        maxPixels=int(1e13),
        pyramidingPolicy={".default": pyramiding_policy},
    ).start()
    log(f"  Asset task submitted  -> {asset_id}")


def submit_gcs_export(
    image: "ee.Image",
    bucket: str,
    file_name_prefix: str,
    crs: str,
    crs_transform: list,
    dry_run: bool,
    overwrite: bool = True,
    description: str = "export",
) -> None:
    """
    Submit an EE-to-GCS export task (Cloud-Optimised GeoTIFF).

    EE may split large exports into multiple files named
    ``<file_name_prefix>-00000.tif``, ``<file_name_prefix>-00001.tif``, etc.
    Existence is checked by listing all GCS blobs matching *file_name_prefix*
    as a prefix, so any previously exported chunks are detected correctly.

    *file_name_prefix* is the full GCS path without the ``.tif`` extension,
    e.g. ``"vista_products/dekadal/count/vista_count_01d1"``.
    *overwrite* — if False and any matching files already exist, the export
    is skipped and the number of existing files is logged.
    *description* is the human-readable task name shown in the EE task console.
    """
    existing = gcs_prefix_exists(bucket, file_name_prefix)
    n_existing = len(existing)
    if dry_run:
        if n_existing:
            action = "overwrite" if overwrite else "skip (already exists)"
            log(f"  [dry-run] Would submit GCS export   -> gs://{bucket}/{file_name_prefix}*.tif"
                f"  [{n_existing} file(s) exist — would {action}]")
        else:
            log(f"  [dry-run] Would submit GCS export   -> gs://{bucket}/{file_name_prefix}*.tif")
        return
    if not overwrite and n_existing:
        log(f"  GCS files already exist ({n_existing} file(s)) — skipping"
            f"  -> gs://{bucket}/{file_name_prefix}*.tif")
        return
    ee.batch.Export.image.toCloudStorage(
        image=image,
        description=description,
        bucket=bucket,
        fileNamePrefix=file_name_prefix,
        crs=crs,
        crsTransform=crs_transform,
        region=_export_region(),
        maxPixels=int(1e13),
        fileFormat="GeoTIFF",
        formatOptions={"cloudOptimized": True},
    ).start()
    log(f"  GCS task submitted    -> gs://{bucket}/{file_name_prefix}*.tif")
