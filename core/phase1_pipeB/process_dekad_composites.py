import csv
import re
import shutil
import urllib.error
import urllib.request
import zipfile
import rasterio
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT

import yaml

from core.phase1_local.process_dekad_composites import (
    CompositeTask,
    DekadRecord,
    DownloadResult,
    EVENT_MISSING_COMPOSITE,
    EXPECTED_GLOBAL_TILE_LABELS,
    Phase1Config,
    Phase1Paths,
    build_composite_output_index,
    classify_raster,
    create_dekadal_raster,
    format_dekad_label,
    format_tile_count_label,
    format_tile_label,
    get_dekadal_output_path,
    load_config,
    log,
    mosaic_classified_tiles,
    parse_bool_setting,
)
from core.phase1_local.process_dekad_composites import CONFIG_PATH


EVENT_PIPEB_MISSING_COMPOSITE = "missing_pipeB_composite_for_date"
EVENT_PIPEB_MISSING_SELECTED_TILES = "missing_pipeB_selected_tiles_for_date"
EVENT_PIPEB_MISSING_GLOBAL_TILES = "missing_pipeB_global_tiles_for_date"
EVENT_PIPEB_DEKAD_NOT_PRODUCED = "missing_pipeB_dekad_after_recovery"
# Event for zero-byte/corrupt ZIPs or invalid .tif files.
EVENT_PIPEB_INVALID_ZIP = "invalid_pipeB_zip"
EVENT_PIPEB_INVALID_TIF = "invalid_pipeB_tif"

JPSS_FILENAME_PATTERN = re.compile(
    r"WATER_COM_VIIRS.*005day_(?P<tile>\d+)\.tif(?:\.zip)?$",
    re.IGNORECASE,
)

# Pipeline B tiles can arrive on a slightly different pixel size than the NOAA
# GLB grid used by Pipeline A. Normalizing them to the NOAA nominal resolution
# avoids seam drift when Pipeline A and Pipeline B outputs are compared or
# mosaicked together later.
PIPELINE_A_TARGET_RES: float = 15.0 / 4448

@dataclass
class PipeBConfig:
    phase1: Phase1Config
    availability_log: Path
    missing_log_path: Path
    output_suffix: str
    base_url: str
    listing_timeout_seconds: int
    download_timeout_seconds: int
    downloads_dirname: str
    mosaic_classified_rasters_dirname: str
    force_pipeline_b: bool
    suppress_missing_logs_for_dekad_ids: set[int]


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.links.append(value)


def load_pipeB_config(config_path: Path) -> PipeBConfig:
    config_path = config_path.resolve()
    phase1 = load_config(config_path)

    with config_path.open("r", encoding="utf-8") as handle:
        raw_config = yaml.safe_load(handle) or {}

    pipeB = raw_config.get("phase1_pipeB", {})
    source = pipeB.get("source", {})
    paths = pipeB.get("paths", {})

    availability_log = Path(pipeB.get("availability_log", "availability_issues_log.csv")).expanduser()
    if not availability_log.is_absolute():
        availability_log = phase1.data_root / availability_log

    missing_log_path = Path(pipeB.get("missing_log_filename", "availability_issues_log_pipeB.csv")).expanduser()
    if not missing_log_path.is_absolute():
        missing_log_path = phase1.data_root / missing_log_path

    output_suffix = str(pipeB.get("output_suffix", "pipeB")).strip()
    force_pipeline_b = parse_bool_setting(
        pipeB.get("force_pipeline_b", False),
        "phase1_pipeB.force_pipeline_b",
        default=False,
    )
    base_url = str(source.get("base_url", "https://jpssflood.gmu.edu/downloads/pub/")).strip().rstrip("/")
    listing_timeout_seconds = int(source.get("listing_timeout_seconds", 30))
    download_timeout_seconds = int(source.get("download_timeout_seconds", 60))
    downloads_dirname = str(paths.get("downloads_dirname", "pipeB_downloads")).strip()
    mosaic_classified_rasters_dirname = str(
        paths.get("mosaic_classified_rasters_dirname", "pipeB_mosaic_classified_rasters")
    ).strip()

    if not output_suffix:
        raise ValueError("phase1_pipeB.output_suffix must not be empty")
    if not base_url:
        raise ValueError("phase1_pipeB.source.base_url must not be empty")
    if listing_timeout_seconds <= 0:
        raise ValueError("phase1_pipeB.source.listing_timeout_seconds must be > 0")
    if download_timeout_seconds <= 0:
        raise ValueError("phase1_pipeB.source.download_timeout_seconds must be > 0")
    if not downloads_dirname:
        raise ValueError("phase1_pipeB.paths.downloads_dirname must not be empty")
    if not mosaic_classified_rasters_dirname:
        raise ValueError("phase1_pipeB.paths.mosaic_classified_rasters_dirname must not be empty")

    return PipeBConfig(
        phase1=phase1,
        availability_log=availability_log,
        missing_log_path=missing_log_path,
        output_suffix=output_suffix,
        base_url=base_url,
        listing_timeout_seconds=listing_timeout_seconds,
        download_timeout_seconds=download_timeout_seconds,
        downloads_dirname=downloads_dirname,
        mosaic_classified_rasters_dirname=mosaic_classified_rasters_dirname,
        force_pipeline_b=force_pipeline_b,
        suppress_missing_logs_for_dekad_ids=set(),
    )


def read_missing_composite_dekad_ids(log_path: Path) -> set[int]:
    if not log_path.is_file():
        log(f"Pipeline B source availability log not found: {log_path}")
        return set()

    missing_dekad_ids: set[int] = set()
    with log_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if row.get("event_type", "").strip() != EVENT_MISSING_COMPOSITE:
                continue
            value = row.get("dekad_id", "").strip()
            if not value:
                continue
            missing_dekad_ids.add(int(value))
    return missing_dekad_ids


def filter_records_for_pipeB(
    records: list[DekadRecord],
    config: PipeBConfig,
    allow_force_pipeline_b: bool = False,
) -> list[DekadRecord]:
    missing_dekad_ids = read_missing_composite_dekad_ids(config.availability_log)
    force_pipeline_b = allow_force_pipeline_b and config.force_pipeline_b
    if force_pipeline_b:
        retained_dekad_ids = {record.dekad_id for record in records}
        config.suppress_missing_logs_for_dekad_ids = set(retained_dekad_ids)
    else:
        retained_dekad_ids = missing_dekad_ids
        config.suppress_missing_logs_for_dekad_ids = set()
    selected_records = [record for record in records if record.dekad_id in retained_dekad_ids]
    skipped_count = len(records) - len(selected_records)
    log("Pipeline B dekad selection:")
    log(f"  Config-selected dekads: {len(records)}")
    log(f"  Dekads with missing NOAA composites in availability log: {len(missing_dekad_ids)}")
    log(f"  Force Pipeline B for all config-selected dekads: {force_pipeline_b}")
    if force_pipeline_b:
        log("  Pipeline B missing-log writes are suppressed for this forced local test run")
    log(f"  Config-selected dekads retained for Pipeline B: {len(selected_records)}")
    if force_pipeline_b:
        log(f"  Config-selected dekads ignored: {skipped_count}")
    else:
        log(f"  Config-selected dekads ignored because no missing composite was logged: {skipped_count}")
    return selected_records


def get_pipeB_dekadal_output_path(config: PipeBConfig, record: DekadRecord) -> Path:
    return get_dekadal_output_path(config.phase1, record, output_suffix=config.output_suffix)


def build_pipeB_paths(config: PipeBConfig, composite_date: str, tile_count: int) -> Phase1Paths:
    filename_suffix = "" if config.phase1.tile_ids is None else format_tile_count_label(tile_count)
    target_label = "GLOBAL" if config.phase1.tile_ids is None else filename_suffix
    base_name = f"vista_5days_{composite_date}"
    if filename_suffix:
        base_name = f"{base_name}_{filename_suffix}"
    base_name = f"{base_name}_{config.output_suffix}"

    download_dir = config.phase1.data_root / config.downloads_dirname / composite_date
    classified_tile_dir = download_dir / "classified_tiles"
    classified_path = (
        config.phase1.data_root
        / config.mosaic_classified_rasters_dirname
        / f"{base_name}.tif"
    )

    return Phase1Paths(
        download_dir=download_dir,
        classified_tile_dir=classified_tile_dir,
        classified_path=classified_path,
        target_label=target_label,
    )


def parse_pipeB_tile(filename: str) -> int | None:
    match = JPSS_FILENAME_PATTERN.match(filename)
    if not match:
        return None
    return int(match.group("tile"))


def get_requested_tile_ids(config: PipeBConfig) -> list[int]:
    if config.phase1.tile_ids is not None:
        return config.phase1.tile_ids
    return sorted(
        int(tile_label.replace("GLB", ""))
        for tile_label in EXPECTED_GLOBAL_TILE_LABELS
    )


def list_pipeB_links(config: PipeBConfig, composite_date: str) -> list[str]:
    date_url = f"{config.base_url}/{composite_date}/tif/"
    try:
        with urllib.request.urlopen(date_url, timeout=config.listing_timeout_seconds) as response:
            html = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        log(f"Pipeline B listing not available at {date_url}: {exc}")
        return []

    parser = LinkParser()
    parser.feed(html)
    return sorted(parser.links)


def list_pipeB_remote_files(config: PipeBConfig, composite_date: str) -> list[str]:
    requested_tile_ids = set(get_requested_tile_ids(config))
    date_url = f"{config.base_url}/{composite_date}/tif/"
    selected_by_tile: dict[int, str] = {}

    for link in list_pipeB_links(config, composite_date):
        filename = Path(link).name
        tile_id = parse_pipeB_tile(filename)
        if tile_id is None or tile_id not in requested_tile_ids:
            continue
        selected_by_tile[tile_id] = f"{date_url}{filename}"

    return [selected_by_tile[tile_id] for tile_id in sorted(selected_by_tile)]


def is_valid_pipeB_zip(local_path: Path) -> bool:
    return local_path.is_file() and local_path.stat().st_size > 0 and zipfile.is_zipfile(local_path)


def download_pipeB_file(remote_url: str, local_path: Path, timeout_seconds: int) -> Path:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    if local_path.is_file():
        if is_valid_pipeB_zip(local_path):
            log(f"Pipeline B zip already downloaded: {local_path.name}")
            return local_path
        log(f"Pipeline B cached zip is invalid, deleting and re-downloading: {local_path.name}")
        local_path.unlink()

    temp_path = local_path.with_suffix(local_path.suffix + ".download")
    log(f"Downloading Pipeline B source zip: {local_path.name}")
    try:
        with urllib.request.urlopen(remote_url, timeout=timeout_seconds) as response:
            with temp_path.open("wb") as handle:
                shutil.copyfileobj(response, handle)
    except (urllib.error.URLError, TimeoutError) as exc:
        if temp_path.exists():
            temp_path.unlink()
        raise RuntimeError(f"Failed to download {remote_url}: {exc}") from exc
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise

    temp_size = temp_path.stat().st_size if temp_path.exists() else 0
    if temp_size == 0 or not zipfile.is_zipfile(temp_path):
        if temp_path.exists():
            temp_path.unlink()
        raise RuntimeError(
            f"Downloaded file is not a valid zip: {remote_url} -> {local_path.name} (size={temp_size} bytes)"
        )

    temp_path.replace(local_path)
    return local_path


def unzip_pipeB_tif(zip_path: Path, extract_to_dir: Path) -> Path | None:
    try:
        with zipfile.ZipFile(zip_path, "r") as zip_handle:
            tif_names = [name for name in zip_handle.namelist() if name.lower().endswith(".tif")]
            if not tif_names:
                return None
            member_name = tif_names[0]
            output_path = extract_to_dir / Path(member_name).name
            if output_path.is_file():
                return output_path
            with zip_handle.open(member_name) as src, output_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            return output_path
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"Pipeline B zip is invalid or corrupted: {zip_path}") from exc


def validate_pipeB_tif(local_path: Path) -> tuple[bool, str]:
    # check to catch corrupt/empty inputs before rasterio open/read attempts
    
    if not local_path.is_file():
        return False, "Extracted tif is missing"
    if local_path.stat().st_size == 0:
        return False, "Extracted tif is empty (0 bytes)"

    try:
        with rasterio.open(local_path) as src:
            if src.width <= 0 or src.height <= 0:
                return False, f"Invalid raster dimensions: {src.width}x{src.height}"
            if src.count <= 0:
                return False, "Raster has no bands"
            
            src.read(1, window=((0, 1), (0, 1)))
    except Exception as exc:
        return False, f"Raster open/read failed: {exc}"

    return True, ""


def download_pipeB_remote_files(
    config: PipeBConfig,
    remote_files: list[str],
    download_dir: Path,
    task: CompositeTask,
) -> list[Path]:
    extracted_dir = download_dir / "source_tiles"
    extracted_dir.mkdir(parents=True, exist_ok=True)

    local_paths: list[Path] = []
    for remote_url in remote_files:
        zip_path = download_dir / Path(remote_url).name
        try:
            download_pipeB_file(remote_url, zip_path, config.download_timeout_seconds)
        
        # Check upstream data-quality case (invalid zip)/invalid .tif files
        except RuntimeError as exc:
            if "not a valid zip" not in str(exc):
                raise
            log(f"Pipeline B skipped invalid source zip: {zip_path.name}")
            append_pipeB_availability_issue_log(
                config=config,
                task=task,
                record=None,
                event_type=EVENT_PIPEB_INVALID_ZIP,
                requested_tiles=[],
                available_tiles=[],
                missing_tiles=[],
                details=f"{zip_path.name} | {remote_url} | {exc}",
            )
            continue
        tif_path = unzip_pipeB_tif(zip_path, extracted_dir)
        if tif_path is None:
            continue

        is_valid_tif, reason = validate_pipeB_tif(tif_path)
        if not is_valid_tif:
            log(f"Pipeline B skipped invalid source tif: {tif_path.name}")
            append_pipeB_availability_issue_log(
                config=config,
                task=task,
                record=None,
                event_type=EVENT_PIPEB_INVALID_TIF,
                requested_tiles=[],
                available_tiles=[],
                missing_tiles=[],
                details=f"{tif_path.name} | {remote_url} | {reason}",
            )
            continue

        local_paths.append(tif_path)
    return sorted(local_paths)


def resample_pipeB_tiles(local_paths: list[Path]) -> None:
    """Align Pipeline B tiles to the NOAA/Pipeline A grid before classification.

    Pipeline B source tiles share the same upper-left origin and pixel dimensions
    as Pipeline A tiles, but some files arrive with a slightly different pixel
    size. That small per-pixel difference accumulates across the full tile width
    and can produce narrow nodata seams where tiles meet. We keep the original
    origin, width, and height, and rewrite only the transform using nearest-
    neighbour resampling because the raster contains categorical classes.
    """
    for src_path in local_paths:
        with rasterio.open(src_path) as src:
            if abs(src.transform.a - PIPELINE_A_TARGET_RES) < 1e-10:
                log(f"Pipeline B tile already at correct pixel size, skipping: {src_path.name}")
                continue
            log(f"Resampling Pipeline B tile to Pipeline A pixel size: {src_path.name}")

            # Preserve the original top-left origin and raster shape; only the
            # output pixel size changes so the tile lands on the NOAA grid.
            new_transform = rasterio.transform.from_origin(
                src.transform.c, src.transform.f,
                PIPELINE_A_TARGET_RES, PIPELINE_A_TARGET_RES,
            )
            tmp_path = src_path.with_suffix(".tmp.tif")
            try:
                with WarpedVRT(
                    src,
                    crs=src.crs,
                    transform=new_transform,
                    width=src.width,
                    height=src.height,
                    resampling=Resampling.nearest,
                    nodata=src.nodata,
                ) as vrt:
                    profile = src.profile.copy()
                    profile.update(transform=new_transform, compress="deflate")
                    with rasterio.open(tmp_path, "w", **profile) as dst:
                        dst.write(vrt.read())
            except Exception:
                if tmp_path.is_file():
                    tmp_path.unlink()
                raise
        shutil.move(str(tmp_path), str(src_path))


def extract_pipeB_tile_labels(paths: list[Path]) -> list[str]:
    labels: set[str] = set()
    for path in paths:
        tile_id = parse_pipeB_tile(path.name)
        if tile_id is not None:
            labels.add(format_tile_label(tile_id))
    return sorted(labels)


def build_pipeB_classified_tile_path(classified_tile_dir: Path, source_tile_path: Path) -> Path:
    return classified_tile_dir / f"vista_classified_{source_tile_path.name}"


def classify_pipeB_tiles(local_paths: list[Path], classified_tile_dir: Path) -> list[Path]:
    classified_tile_dir.mkdir(parents=True, exist_ok=True)
    classified_paths: list[Path] = []
    total_files = len(local_paths)

    for file_index, local_path in enumerate(local_paths, start=1):
        classified_path = build_pipeB_classified_tile_path(classified_tile_dir, local_path)
        classified_paths.append(classified_path)
        if classified_path.is_file():
            log(f"Pipeline B classified tile/file {file_index} out of {total_files} already exists: {classified_path.name}")
            continue
        log(f"Classifying Pipeline B tile/file {file_index} out of {total_files}: {local_path.name}")
        classify_raster(local_path, classified_path)

    return classified_paths


def append_pipeB_availability_issue_log(
    config: PipeBConfig,
    task: CompositeTask | None,
    record: DekadRecord | None,
    event_type: str,
    requested_tiles: list[str],
    available_tiles: list[str],
    missing_tiles: list[str],
    details: str = "",
) -> None:
    log_path = config.missing_log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = log_path.is_file()
    run_mode = "GLOBAL" if config.phase1.tile_ids is None else "SELECTED_TILES"
    dekad_id = task.dekad_id if task is not None else record.dekad_id if record is not None else ""
    composite_date = task.composite_date if task is not None else ""
    if isinstance(dekad_id, int) and dekad_id in config.suppress_missing_logs_for_dekad_ids:
        log(f"Pipeline B issue not written to missing log for forced dekad {dekad_id}: {event_type}")
        return

    with log_path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if not file_exists:
            writer.writerow(
                [
                    "logged_at",
                    "event_type",
                    "dekad_id",
                    "calendar_composite_date",
                    "pipeB_source_date",
                    "run_mode",
                    "requested_tiles",
                    "available_tiles",
                    "missing_tiles",
                    "details",
                ]
            )
        writer.writerow(
            [
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                event_type,
                dekad_id,
                composite_date,
                composite_date,
                run_mode,
                ",".join(requested_tiles),
                ",".join(available_tiles),
                ",".join(missing_tiles),
                details,
            ]
        )

    log(f"Pipeline B availability issue logged to: {log_path}")


def run_pipeB_download_step(config: PipeBConfig, task: CompositeTask) -> DownloadResult:
    log("-" * 60)
    log(f"Dekad progress: {task.dekad_sequence} out of {task.total_dekads} Pipeline B dekad(s)")
    log(f"Dekad ID: {task.dekad_id}")
    log(f"Pipeline B 5-day composite progress within dekad: {task.composite_sequence} out of {task.total_composites}")
    log(f"Pipeline B 5-day composite source date: {task.composite_date}")
    if config.phase1.tile_ids is None:
        log("Tile selection: GLOBAL")
    else:
        log(f"Tile selection: {[format_tile_label(tile_id) for tile_id in config.phase1.tile_ids]}")

    requested_tiles = [format_tile_label(tile_id) for tile_id in get_requested_tile_ids(config)]
    remote_files = list_pipeB_remote_files(config, task.composite_date)
    if not remote_files:
        log("Composite not available in Pipeline B source. Skipping.")
        append_pipeB_availability_issue_log(
            config,
            task=task,
            record=None,
            event_type=EVENT_PIPEB_MISSING_COMPOSITE,
            requested_tiles=requested_tiles,
            available_tiles=[],
            missing_tiles=requested_tiles,
            details="No requested Pipeline B tiles were found for the composite source date.",
        )
        return DownloadResult(
            aws_folder_date=task.composite_date,
            remote_files=[],
            local_paths=None,
            paths=None,
            requested_tiles=requested_tiles,
            available_tiles=[],
            missing_tiles=requested_tiles,
            is_partial=False,
        )

    provisional_paths = build_pipeB_paths(config, task.composite_date, tile_count=0)
    local_paths = download_pipeB_remote_files(config, remote_files, provisional_paths.download_dir, task)
    available_tiles = extract_pipeB_tile_labels(local_paths)
    missing_tiles = sorted(set(requested_tiles) - set(available_tiles))

    if not local_paths:
        append_pipeB_availability_issue_log(
            config,
            task=task,
            record=None,
            event_type=EVENT_PIPEB_MISSING_COMPOSITE,
            requested_tiles=requested_tiles,
            available_tiles=[],
            missing_tiles=requested_tiles,
            details="Pipeline B tiles were listed but no usable GeoTIFF was extracted.",
        )
        return DownloadResult(
            aws_folder_date=task.composite_date,
            remote_files=[],
            local_paths=None,
            paths=None,
            requested_tiles=requested_tiles,
            available_tiles=[],
            missing_tiles=requested_tiles,
            is_partial=False,
        )

    if missing_tiles:
        event_type = (
            EVENT_PIPEB_MISSING_GLOBAL_TILES
            if config.phase1.tile_ids is None
            else EVENT_PIPEB_MISSING_SELECTED_TILES
        )
        append_pipeB_availability_issue_log(
            config,
            task=task,
            record=None,
            event_type=event_type,
            requested_tiles=requested_tiles,
            available_tiles=available_tiles,
            missing_tiles=missing_tiles,
            details="Pipeline B composite processed with missing requested tiles.",
        )

    paths = build_pipeB_paths(config, task.composite_date, len(local_paths))
    return DownloadResult(
        aws_folder_date=task.composite_date,
        remote_files=remote_files,
        local_paths=local_paths,
        paths=paths,
        requested_tiles=requested_tiles,
        available_tiles=available_tiles,
        missing_tiles=missing_tiles,
        is_partial=bool(missing_tiles),
    )


def run_pipeB_classification_step(download_result: DownloadResult) -> list[Path] | None:
    if not download_result.local_paths or download_result.paths is None:
        return None
    return classify_pipeB_tiles(download_result.local_paths, download_result.paths.classified_tile_dir)


def run_pipeB_mosaic_step(download_result: DownloadResult, classified_paths: list[Path] | None) -> Path | None:
    if not classified_paths or download_result.paths is None:
        return None
    return mosaic_classified_tiles(classified_paths, download_result.paths.classified_path)


def run_pipeB_composite_task(config: PipeBConfig, task: CompositeTask) -> Path | None:
    download_result = run_pipeB_download_step(config, task)
    if not download_result.remote_files:
        return None

    resample_pipeB_tiles(download_result.local_paths)

    classified_paths = run_pipeB_classification_step(download_result)
    output_raster_path = run_pipeB_mosaic_step(download_result, classified_paths)
    if output_raster_path is None or download_result.paths is None:
        return None
    return download_result.paths.classified_path


def run_pipeB_dekadal_step(
    config: PipeBConfig,
    records: list[DekadRecord],
    composite_outputs: dict[str, Path],
) -> None:
    available_outputs = build_composite_output_index(composite_outputs)

    for record in records:
        missing_dates = [
            composite_date
            for composite_date in record.composite_dates
            if composite_date not in available_outputs
        ]
        if missing_dates:
            log(
                "Skipping Pipeline B dekadal aggregation for "
                f"{record.year}-{record.month:02d} dekad {record.dekad}: "
                f"missing classified composite raster(s) for {missing_dates}"
            )
            append_pipeB_availability_issue_log(
                config,
                task=None,
                record=record,
                event_type=EVENT_PIPEB_DEKAD_NOT_PRODUCED,
                requested_tiles=[],
                available_tiles=[],
                missing_tiles=[],
                details=(
                    "Pipeline B dekadal raster was not produced because required "
                    f"5-day classified composite rasters were missing: {','.join(missing_dates)}"
                ),
            )
            continue

        input_paths = [available_outputs[composite_date] for composite_date in record.composite_dates]
        output_path = get_pipeB_dekadal_output_path(config, record)
        log(
            f"Aggregating Pipeline B dekadal raster for {record.year}-{record.month:02d} dekad {record.dekad} "
            f"from {len(input_paths)} classified 5-day raster(s)"
        )
        create_dekadal_raster(input_paths, output_path)


def log_pipeB_plan(config: PipeBConfig, records: list[DekadRecord], task_count: int) -> None:
    log(f"Pipeline B source URL: {config.base_url}")
    log(f"Pipeline B source availability log: {config.availability_log}")
    log(f"Pipeline B missing log: {config.missing_log_path}")
    log(f"Pipeline B output suffix: {config.output_suffix}")
    log(f"Pipeline B downloads folder: {config.phase1.data_root / config.downloads_dirname}")
    log(
        "Pipeline B mosaic-classified folder: "
        f"{config.phase1.data_root / config.mosaic_classified_rasters_dirname}"
    )
    selected_dekads = [f"{record.dekad_id} ({format_dekad_label(record)})" for record in records]
    log(f"Pipeline B selected dekads after log filtering: {selected_dekads}")
    log(f"Pipeline B needs {task_count} 5-day composite(s) to download and process")


def log_pipeB_resume_plan(config: PipeBConfig, records: list[DekadRecord]) -> None:
    existing_count = 0
    missing_count = 0
    for record in records:
        if get_pipeB_dekadal_output_path(config, record).is_file():
            existing_count += 1
        else:
            missing_count += 1

    log("Pipeline B local resume inventory:")
    log(f"  Selected dekads: {len(records)}")
    log(f"  Final local Pipeline B dekadal rasters already present: {existing_count}")
    log(f"  Final local Pipeline B dekadal rasters missing: {missing_count}")


def parse_args():
    import argparse

    parser = argparse.ArgumentParser(
        description="Run Pipeline B local recovery for NOAA-missing dekads from the GMU/JPSS repository."
    )
    parser.add_argument(
        "--config",
        default=str(CONFIG_PATH),
        help="Path to config/config.yaml.",
    )
    return parser.parse_args()
