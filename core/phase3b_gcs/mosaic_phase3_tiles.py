from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import yaml
from google.cloud import storage

from core.phase1_local.process_dekad_composites import (
    CONFIG_PATH,
    get_config_data_root,
    initialize_run_log_from_config,
    log,
    log_external_output,
    parse_bool_setting,
)
from core.phase3_gee.constants import ALL_DEKADS
from core.phase3_gee.config_utils import load_phase3_settings

import rasterio


PRODUCT_STEM_PATTERN = re.compile(
    r"^(?P<stem>vista_.+?_(?P<period_key>\d{2}d[1-3]))",
    re.IGNORECASE,
)
PERIOD_KEY_PATTERN = re.compile(r"^(?P<month>\d{1,2})d(?P<dekad>[1-3])$", re.IGNORECASE)


@dataclass(frozen=True)
class Phase3BMosaicSettings:
    project_id: str
    step: str
    statistic: str
    bucket_name: str
    tiles_prefix: str
    final_prefix: str
    work_dir: Path
    selected_period_keys: tuple[str, ...]
    dry_run: bool
    overwrite_final: bool
    stop_on_error: bool
    upload_chunk_size_mb: int
    upload_timeout_seconds: int


@dataclass(frozen=True)
class ProductTileGroup:
    product_stem: str
    period_key: str
    tile_blob_names: tuple[str, ...]

    @property
    def final_filename(self) -> str:
        return f"{self.product_stem}.tif"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 3B: mosaic Phase 3 Earth Engine GCS export tiles into one "
            "Cloud-Optimised GeoTIFF per statistics product."
        )
    )
    parser.add_argument(
        "--config",
        default=str(CONFIG_PATH),
        help="Path to config/config.yaml.",
    )
    parser.add_argument(
        "--step",
        default=None,
        choices=["dekadal", "monthly", "annual"],
        help="Override phase3_gee.step from config.",
    )
    parser.add_argument(
        "--statistic",
        default=None,
        choices=["count", "frequency", "dominant"],
        help="Override phase3_gee.statistic from config.",
    )
    parser.add_argument(
        "--products",
        default=None,
        help=(
            "Override phase3b_gcs.products.selection, for example '07d1' "
            "or '01d1-03d3,05d1'."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="List Phase 3B mosaic actions without downloading, mosaicking, or uploading.",
    )
    parser.add_argument(
        "--overwrite-final",
        action="store_true",
        default=None,
        help="Overwrite existing final mosaic files in GCS.",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        default=None,
        help="Stop on the first product failure instead of logging and continuing.",
    )
    return parser.parse_args()


def _derive_final_prefix(tiles_prefix: str) -> str:
    clean_prefix = tiles_prefix.strip("/")
    suffix = "/tiles"
    if not clean_prefix.endswith(suffix):
        raise ValueError(
            "Phase 3B expects Phase 3 GCS exports to end in '/tiles'. "
            f"Got: {tiles_prefix!r}"
        )
    return clean_prefix[: -len(suffix)]


def _normalise_dekad_key(value: object, field_name: str) -> str:
    raw_value = str(value).strip()
    match = PERIOD_KEY_PATTERN.match(raw_value)
    if not match:
        raise ValueError(f"{field_name} contains invalid dekad key: {value!r}")

    month = int(match.group("month"))
    dekad = int(match.group("dekad"))
    key = f"{month:02d}d{dekad}"
    if key not in ALL_DEKADS:
        raise ValueError(f"{field_name} contains out-of-range dekad key: {value!r}")
    return key


def _expand_dekad_selection_token(token: str, field_name: str) -> list[str]:
    clean_token = token.strip()
    if not clean_token:
        return []

    if "-" in clean_token:
        start_raw, end_raw = (part.strip() for part in clean_token.split("-", 1))
        start = _normalise_dekad_key(start_raw, field_name)
        end = _normalise_dekad_key(end_raw, field_name)
        start_index = ALL_DEKADS.index(start)
        end_index = ALL_DEKADS.index(end)
        if start_index > end_index:
            raise ValueError(f"{field_name} range start must be before range end: {clean_token!r}")
        return list(ALL_DEKADS[start_index : end_index + 1])

    return [_normalise_dekad_key(clean_token, field_name)]


def parse_dekad_product_selection(raw_selection: object) -> tuple[str, ...]:
    field_name = "phase3b_gcs.products.selection"
    if raw_selection in (None, ""):
        raw_selection = "all"

    if isinstance(raw_selection, str):
        if raw_selection.strip().lower() == "all":
            return tuple(ALL_DEKADS)
        tokens = raw_selection.split(",")
    elif isinstance(raw_selection, list):
        tokens = [str(item) for item in raw_selection]
    else:
        raise ValueError(
            f"{field_name} must be 'all', a comma-separated string, or a YAML list; "
            f"got: {raw_selection!r}"
        )

    selected: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        for key in _expand_dekad_selection_token(str(token), field_name):
            if key not in seen:
                selected.append(key)
                seen.add(key)

    if not selected:
        raise ValueError(f"{field_name} did not resolve to any dekads")
    return tuple(selected)


def load_phase3b_settings(
    config_path: Path,
    step_override: str | None = None,
    statistic_override: str | None = None,
) -> Phase3BMosaicSettings:
    with config_path.open("r", encoding="utf-8") as handle:
        raw_config = yaml.safe_load(handle) or {}

    phase3_settings = load_phase3_settings(
        config_path,
        step_override=step_override,
        statistic_override=statistic_override,
    )
    p3b = raw_config.get("phase3b_gcs", {})
    paths = p3b.get("paths", {}) or {}
    gcs = p3b.get("gcs", {}) or {}
    products = p3b.get("products", {}) or {}
    run_control = p3b.get("run_control", {}) or {}

    work_dirname = str(paths.get("work_dirname", "phase3b_mosaic_work")).strip()
    if not work_dirname:
        raise ValueError("phase3b_gcs.paths.work_dirname must not be empty")

    upload_chunk_size_mb = int(gcs.get("upload_chunk_size_mb", 50))
    upload_timeout_seconds = int(gcs.get("upload_timeout_seconds", 1200))
    if upload_chunk_size_mb <= 0:
        raise ValueError("phase3b_gcs.gcs.upload_chunk_size_mb must be > 0")
    if upload_timeout_seconds <= 0:
        raise ValueError("phase3b_gcs.gcs.upload_timeout_seconds must be > 0")

    tiles_prefix = str(phase3_settings["export_prefix"]).strip().strip("/")
    return Phase3BMosaicSettings(
        project_id=phase3_settings["project_id"],
        step=phase3_settings["step"],
        statistic=phase3_settings["statistic"],
        bucket_name=phase3_settings["export_bucket_name"],
        tiles_prefix=tiles_prefix,
        final_prefix=_derive_final_prefix(tiles_prefix),
        work_dir=get_config_data_root(config_path) / work_dirname,
        selected_period_keys=parse_dekad_product_selection(products.get("selection", "all")),
        dry_run=parse_bool_setting(
            run_control.get("dry_run", True),
            "phase3b_gcs.run_control.dry_run",
        ),
        overwrite_final=parse_bool_setting(
            run_control.get("overwrite_final", False),
            "phase3b_gcs.run_control.overwrite_final",
        ),
        stop_on_error=parse_bool_setting(
            run_control.get("stop_on_error", False),
            "phase3b_gcs.run_control.stop_on_error",
        ),
        upload_chunk_size_mb=upload_chunk_size_mb,
        upload_timeout_seconds=upload_timeout_seconds,
    )


def resolve_gdal_command(command_name: str) -> str:
    candidates: list[Path] = []
    executable_dir = Path(sys.executable).resolve().parent
    executable_name = f"{command_name}.exe" if sys.platform.startswith("win") else command_name
    candidates.extend(
        [
            executable_dir / executable_name,
            executable_dir / "Scripts" / executable_name,
            executable_dir.parent / "Library" / "bin" / executable_name,
            executable_dir / "Library" / "bin" / executable_name,
        ]
    )

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    resolved = shutil.which(command_name)
    if resolved:
        return resolved

    raise FileNotFoundError(
        f"{command_name} not found in the active environment or on PATH. "
        "Install GDAL in the selected Python environment."
    )


def product_identity_from_blob_name(blob_name: str) -> tuple[str, str] | None:
    filename = Path(blob_name).name
    if not filename.lower().endswith((".tif", ".tiff")):
        return None
    match = PRODUCT_STEM_PATTERN.match(filename)
    if not match:
        return None
    return match.group("stem"), match.group("period_key").lower()


def list_product_tile_groups(bucket: storage.Bucket, tiles_prefix: str) -> list[ProductTileGroup]:
    prefix = tiles_prefix.strip("/") + "/"
    grouped: dict[str, list[str]] = {}
    period_keys: dict[str, str] = {}
    ignored_count = 0

    for blob in bucket.list_blobs(prefix=prefix):
        product_identity = product_identity_from_blob_name(blob.name)
        if product_identity is None:
            ignored_count += 1
            continue
        stem, period_key = product_identity
        grouped.setdefault(stem, []).append(blob.name)
        period_keys[stem] = period_key

    groups = [
        ProductTileGroup(
            product_stem=stem,
            period_key=period_keys[stem],
            tile_blob_names=tuple(sorted(blob_names)),
        )
        for stem, blob_names in sorted(grouped.items())
    ]
    log(f"Tile groups found: {len(groups)}")
    if ignored_count:
        log(f"Ignored non-product objects under tiles prefix: {ignored_count}")
    return groups


def filter_product_tile_groups(
    groups: list[ProductTileGroup],
    selected_period_keys: tuple[str, ...],
) -> list[ProductTileGroup]:
    selected = set(selected_period_keys)
    filtered = [group for group in groups if group.period_key in selected]
    found = {group.period_key for group in filtered}
    missing = [key for key in selected_period_keys if key not in found]

    log(f"Selected dekads       : {', '.join(selected_period_keys)}")
    log(f"Tile groups selected  : {len(filtered)} of {len(groups)}")
    if missing:
        log(f"Selected dekads with no tile group: {', '.join(missing)}")
    return filtered


def list_existing_final_mosaics(bucket: storage.Bucket, final_prefix: str) -> set[str]:
    prefix = final_prefix.strip("/") + "/"
    existing = {
        blob.name
        for blob in bucket.list_blobs(prefix=prefix)
        if blob.name.lower().endswith((".tif", ".tiff"))
    }
    log(f"Existing final mosaics: {len(existing)} under gs://{bucket.name}/{prefix}")
    return existing


def split_groups_by_final_status(
    groups: list[ProductTileGroup],
    settings: Phase3BMosaicSettings,
    existing_final_names: set[str],
) -> tuple[list[ProductTileGroup], list[ProductTileGroup]]:
    existing: list[ProductTileGroup] = []
    pending: list[ProductTileGroup] = []
    for group in groups:
        if final_object_name(settings, group) in existing_final_names:
            existing.append(group)
        else:
            pending.append(group)

    if existing:
        log(
            "Selected final mosaics already present: "
            f"{len(existing)} ({', '.join(group.period_key for group in existing)})"
        )
    if pending:
        log(
            "Selected final mosaics missing: "
            f"{len(pending)} ({', '.join(group.period_key for group in pending)})"
        )
    return existing, pending


def final_object_name(settings: Phase3BMosaicSettings, group: ProductTileGroup) -> str:
    return f"{settings.final_prefix}/{group.final_filename}"


def prepare_product_work_dir(settings: Phase3BMosaicSettings, product_stem: str) -> Path:
    product_work_dir = settings.work_dir / product_stem
    if product_work_dir.exists():
        shutil.rmtree(product_work_dir)
    (product_work_dir / "tiles").mkdir(parents=True, exist_ok=True)
    return product_work_dir


def download_tiles(
    bucket: storage.Bucket,
    group: ProductTileGroup,
    product_work_dir: Path,
) -> list[Path]:
    local_paths: list[Path] = []
    tiles_dir = product_work_dir / "tiles"
    for index, blob_name in enumerate(group.tile_blob_names, start=1):
        local_path = tiles_dir / Path(blob_name).name
        log(f"[{group.product_stem}] Downloading tile {index}/{len(group.tile_blob_names)}: {Path(blob_name).name}")
        bucket.blob(blob_name).download_to_filename(str(local_path))
        local_paths.append(local_path)
    return local_paths


def validate_tiles(local_paths: list[Path], product_stem: str) -> None:
    if not local_paths:
        raise ValueError(f"[{product_stem}] No local tiles to mosaic")

    expected_crs = None
    expected_count = None
    expected_dtype = None
    for local_path in local_paths:
        with rasterio.open(local_path) as dataset:
            if expected_crs is None:
                expected_crs = dataset.crs
                expected_count = dataset.count
                expected_dtype = dataset.dtypes
                continue
            if dataset.crs != expected_crs:
                raise ValueError(f"[{product_stem}] CRS mismatch in {local_path.name}")
            if dataset.count != expected_count:
                raise ValueError(f"[{product_stem}] Band-count mismatch in {local_path.name}")
            if dataset.dtypes != expected_dtype:
                raise ValueError(f"[{product_stem}] Data-type mismatch in {local_path.name}")


def run_command(command: list[str]) -> None:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.stdout:
        log_external_output(result.stdout)
    if result.stderr:
        log_external_output(result.stderr)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, command)


def build_mosaic_cog(
    local_tile_paths: list[Path],
    product_work_dir: Path,
    product_stem: str,
    gdalbuildvrt: str,
    gdal_translate: str,
) -> Path:
    vrt_path = product_work_dir / f"{product_stem}.vrt"
    output_path = product_work_dir / f"{product_stem}.tif"
    tile_list_path = product_work_dir / "tiles.txt"
    tile_list_path.write_text(
        "\n".join(str(path) for path in local_tile_paths),
        encoding="utf-8",
    )

    run_command(
        [
            gdalbuildvrt,
            "-input_file_list",
            str(tile_list_path),
            str(vrt_path),
        ]
    )
    run_command(
        [
            gdal_translate,
            "-of",
            "COG",
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "BIGTIFF=YES",
            str(vrt_path),
            str(output_path),
        ]
    )

    with rasterio.open(output_path) as dataset:
        if dataset.count <= 0:
            raise ValueError(f"[{product_stem}] Final COG has no bands")
        log(
            f"[{product_stem}] Final COG validated: "
            f"{dataset.width}x{dataset.height}, bands={dataset.count}, crs={dataset.crs}"
        )
    return output_path


def upload_final_mosaic(
    bucket: storage.Bucket,
    local_path: Path,
    object_name: str,
    settings: Phase3BMosaicSettings,
) -> None:
    blob = bucket.blob(object_name)
    blob.chunk_size = settings.upload_chunk_size_mb * 1024 * 1024
    blob.upload_from_filename(str(local_path), timeout=settings.upload_timeout_seconds)
    log(f"Uploaded final mosaic: gs://{settings.bucket_name}/{object_name}")


def process_group(
    bucket: storage.Bucket,
    group: ProductTileGroup,
    settings: Phase3BMosaicSettings,
    gdalbuildvrt: str,
    gdal_translate: str,
) -> str:
    final_name = final_object_name(settings, group)
    final_uri = f"gs://{settings.bucket_name}/{final_name}"
    final_blob = bucket.blob(final_name)
    final_exists = final_blob.exists()

    log(f"[{group.product_stem}] Tiles: {len(group.tile_blob_names)}")
    log(f"[{group.product_stem}] Final target: {final_uri}")

    if final_exists and not settings.overwrite_final:
        log(f"[{group.product_stem}] Final mosaic already exists - skipped.")
        return "skipped_existing"

    if settings.dry_run:
        action = "overwrite" if final_exists else "create"
        log(f"[{group.product_stem}] [DRY RUN] Would {action} final mosaic.")
        return "dry_run"

    log(f"[{group.product_stem}] Final mosaic missing - building from GCS tiles.")
    product_work_dir = prepare_product_work_dir(settings, group.product_stem)
    local_tile_paths = download_tiles(bucket, group, product_work_dir)
    validate_tiles(local_tile_paths, group.product_stem)
    local_mosaic_path = build_mosaic_cog(
        local_tile_paths,
        product_work_dir,
        group.product_stem,
        gdalbuildvrt,
        gdal_translate,
    )
    upload_final_mosaic(bucket, local_mosaic_path, final_name, settings)
    return "uploaded"


def print_settings(settings: Phase3BMosaicSettings) -> None:
    log("=" * 60)
    log("PHASE 3B - MOSAIC PHASE 3 GCS TILES")
    log("=" * 60)
    log(f"GCP project           : {settings.project_id}")
    log(f"Step / statistic      : {settings.step} / {settings.statistic}")
    log(f"GCS tiles prefix      : gs://{settings.bucket_name}/{settings.tiles_prefix}/")
    log(f"GCS final prefix      : gs://{settings.bucket_name}/{settings.final_prefix}/")
    log(f"Work directory        : {settings.work_dir}")
    log(f"Product selection     : {', '.join(settings.selected_period_keys)}")
    log(f"Dry run               : {settings.dry_run}")
    log(f"Overwrite final       : {settings.overwrite_final}")
    log(f"Stop on error         : {settings.stop_on_error}")
    log("")


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()
    initialize_run_log_from_config(config_path, "phase3b")

    try:
        settings = load_phase3b_settings(
            config_path,
            step_override=args.step,
            statistic_override=args.statistic,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR loading Phase 3B settings: {exc}")
        return 1

    if args.dry_run is not None:
        settings = replace(settings, dry_run=args.dry_run)
    if args.products is not None:
        settings = replace(
            settings,
            selected_period_keys=parse_dekad_product_selection(args.products),
        )
    if args.overwrite_final is not None:
        settings = replace(settings, overwrite_final=args.overwrite_final)
    if args.stop_on_error is not None:
        settings = replace(settings, stop_on_error=args.stop_on_error)
    print_settings(settings)

    try:
        gdalbuildvrt = resolve_gdal_command("gdalbuildvrt")
        gdal_translate = resolve_gdal_command("gdal_translate")
    except FileNotFoundError as exc:
        log(f"ERROR: {exc}")
        return 1

    bucket = storage.Client(project=settings.project_id).bucket(settings.bucket_name)
    groups = list_product_tile_groups(bucket, settings.tiles_prefix)
    groups = filter_product_tile_groups(groups, settings.selected_period_keys)
    skipped_existing_groups: list[ProductTileGroup] = []
    if not settings.overwrite_final:
        existing_final_names = list_existing_final_mosaics(bucket, settings.final_prefix)
        skipped_existing_groups, groups = split_groups_by_final_status(
            groups,
            settings,
            existing_final_names,
        )

    if not groups:
        if skipped_existing_groups:
            log(
                "All selected final mosaics already exist. "
                f"skipped_existing={len(skipped_existing_groups)}"
            )
        else:
            log("No tile groups found. Nothing to mosaic.")
        return 0

    settings.work_dir.mkdir(parents=True, exist_ok=True)
    counts = {"uploaded": 0, "skipped_existing": 0, "dry_run": 0, "failed": 0}
    counts["skipped_existing"] = len(skipped_existing_groups)
    errors: list[str] = []
    for index, group in enumerate(groups, start=1):
        log(f"Product {index}/{len(groups)}: {group.product_stem}")
        try:
            status = process_group(bucket, group, settings, gdalbuildvrt, gdal_translate)
            counts[status] = counts.get(status, 0) + 1
        except Exception as exc:  # noqa: BLE001
            counts["failed"] += 1
            message = f"[{group.product_stem}] FAILED: {exc}"
            log(message)
            errors.append(message)
            if settings.stop_on_error:
                break
        log("")

    log(
        "Phase 3B summary: "
        f"uploaded={counts['uploaded']}, "
        f"skipped_existing={counts['skipped_existing']}, "
        f"dry_run={counts['dry_run']}, "
        f"failed={counts['failed']}"
    )
    if errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
