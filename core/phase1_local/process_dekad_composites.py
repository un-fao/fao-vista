import argparse
import calendar
import csv
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


def configure_windows_dll_path() -> None:
    """
    On Windows, geospatial packages such as rasterio depend on DLLs shipped in
    the conda environment under Library/bin. When Python is launched directly
    from an IDE, that folder is not always available on the DLL search path.
    """
    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return

    env_root = Path(sys.executable).resolve().parent
    library_bin = env_root / "Library" / "bin"
    if library_bin.is_dir():
        os.add_dll_directory(str(library_bin))

    gdal_data = env_root / "Library" / "share" / "gdal"
    if "GDAL_DATA" not in os.environ and gdal_data.is_dir():
        os.environ["GDAL_DATA"] = str(gdal_data)


configure_windows_dll_path()

import numpy as np
import rasterio
from rasterio.merge import merge
from rasterio.transform import from_origin
from rasterio.windows import bounds as window_bounds
from rasterio.windows import from_bounds as window_from_bounds
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
DEFAULT_CALENDAR_PATH = PROJECT_ROOT / "data" / "calendars" / "viirs_dekads_2010_2040.csv"
NOAA_AWS_REGION = "us-east-1"
# First calendar composite date whose NOAA AWS folder uses the first observation day.
NOAA_FIRST_DAY_FOLDER_START_COMPOSITE_DATE = datetime(2023, 8, 10)
NOAA_VIIRS_FILENAME_PATTERN = re.compile(
    r"VIIRS-Flood-5day-(?P<tile>GLB\d{3})_v[^_]+_blend_"
    r"s(?P<start>\d+)_e(?P<end>\d+)_c(?P<created>\d+)\.tif$",
    re.IGNORECASE,
)
GLOBAL_TILE_LABELS = {f"GLB{tile_id:03d}" for tile_id in range(1, 137)}
EXPECTED_MISSING_GLOBAL_TILE_LABELS = {"GLB050", "GLB051"}
EXPECTED_GLOBAL_TILE_LABELS = GLOBAL_TILE_LABELS - EXPECTED_MISSING_GLOBAL_TILE_LABELS

def resolve_aws_cli() -> str:
    cmd = shutil.which("aws")
    if cmd is None:
        raise FileNotFoundError(
            "AWS CLI not found."
        )
    return cmd

AWS_CLI = resolve_aws_cli()
ACTIVE_RUN_LOG_PATH: Path | None = None

@dataclass
class Phase1Config:
    data_root: Path
    bucket_prefix: str
    calendar_csv: Path
    dekad_ids: list[int]
    tile_ids: list[int] | None
    noaa_download_retention_days: int
    mosaic_classified_raster_retention_days: int
    cleanup_global_intermediates_after_dekad: bool


@dataclass
class DekadRecord:
    dekad_id: int
    year: int
    month: int
    dekad: int
    composite_dates: list[str]


@dataclass
class CompositeTask:
    dekad_sequence: int
    total_dekads: int
    dekad_id: int
    composite_sequence: int
    total_composites: int
    composite_date: str


@dataclass
class Phase1Paths:
    download_dir: Path
    classified_tile_dir: Path
    classified_path: Path
    target_label: str


@dataclass
class DownloadResult:
    aws_folder_date: str
    remote_files: list[str]
    local_paths: list[Path] | None
    paths: Phase1Paths | None
    requested_tiles: list[str]
    available_tiles: list[str]
    missing_tiles: list[str]
    is_partial: bool


DEKAD_AGGREGATION_PRIORITY = np.array([0, 9, 11, 10, 4, 5, 6, 1, 2, 3], dtype=np.uint8)
DEKAD_OUTPUT_COLORMAP: dict[int, tuple[int, int, int, int]] = {
    0: (0, 0, 0, 0),
    1: (255, 255, 0, 255),
    2: (255, 165, 0, 255),
    3: (255, 0, 0, 255),
    4: (255, 255, 255, 255),
    5: (173, 216, 230, 255),
    6: (0, 255, 255, 255),
    9: (160, 160, 160, 255),
    10: (34, 139, 34, 255),
    11: (0, 0, 255, 255),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download, mosaic, and classify the VIIRS composites needed for selected dekads."
    )
    parser.add_argument(
        "--config",
        default=str(CONFIG_PATH),
        help="Path to config/config.yaml.",
    )
    return parser.parse_args()


def parse_bool_setting(value, field_name: str, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0", ""}:
            return False
    raise ValueError(f"{field_name} must be true or false")


def build_run_log_path(data_root: Path, run_name: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return data_root / "execution_logs" / f"{run_name}_{timestamp}.log"


def initialize_run_log(data_root: Path, run_name: str) -> Path:
    global ACTIVE_RUN_LOG_PATH

    run_log_path = build_run_log_path(data_root, run_name)
    run_log_path.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_RUN_LOG_PATH = run_log_path
    log(f"Execution log: {run_log_path}")
    return run_log_path


def get_config_data_root(config_path: Path) -> Path:
    with config_path.open("r", encoding="utf-8") as handle:
        raw_config = yaml.safe_load(handle) or {}
    paths = raw_config.get("paths", {})
    return Path(paths.get("data_root", "data")).expanduser()


def initialize_run_log_from_config(config_path: Path, run_name: str) -> Path:
    return initialize_run_log(get_config_data_root(config_path), run_name)


def append_to_active_run_log(line: str) -> None:
    if ACTIVE_RUN_LOG_PATH is None:
        return
    with ACTIVE_RUN_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line)
        if not line.endswith("\n"):
            handle.write("\n")


def log_external_output(output: str) -> None:
    if not output:
        return
    print(output, end="" if output.endswith("\n") else "\n")
    append_to_active_run_log(output)


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    append_to_active_run_log(line)


def format_elapsed_time(elapsed_seconds: float) -> str:
    total_seconds = max(0, int(round(elapsed_seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h {minutes}m {seconds}s"


def get_aws_default_region() -> str | None:
    result = subprocess.run(
        [AWS_CLI, "configure", "get", "default.region"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    region = result.stdout.strip()
    return region or None


def set_aws_default_region(region: str) -> None:
    subprocess.run(
        [AWS_CLI, "configure", "set", "default.region", region],
        check=True,
    )


def unset_aws_default_region() -> None:
    subprocess.run(
        [AWS_CLI, "configure", "set", "default.region", ""],
        check=True,
    )


@contextmanager
def use_noaa_aws_region():
    original_region = get_aws_default_region()
    set_aws_default_region(NOAA_AWS_REGION)
    try:
        yield
    finally:
        if original_region:
            set_aws_default_region(original_region)
        else:
            unset_aws_default_region()


def parse_numeric_selection(value: object, field_name: str, allow_empty: bool = False) -> list[int] | None:
    if value is None:
        if allow_empty:
            return None
        raise ValueError(f"config/config.yaml is missing {field_name}")

    if isinstance(value, int):
        return [value]

    if isinstance(value, list):
        selected_ids = sorted({int(item) for item in value})
        if selected_ids:
            return selected_ids
        if allow_empty:
            return None
        raise ValueError(f"No values were parsed from {field_name}")

    if not isinstance(value, str):
        allowed = "empty, an int, a list, or a range string" if allow_empty else "an int, a list, or a range string"
        raise ValueError(f"{field_name} must be {allowed}")

    text = value.strip()
    if not text:
        if allow_empty:
            return None
        raise ValueError(f"No values were parsed from {field_name}")

    selected_ids: set[int] = set()
    for chunk in text.split(","):
        token = chunk.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", maxsplit=1)
            start_id = int(start_text.strip())
            end_id = int(end_text.strip())
            if end_id < start_id:
                raise ValueError(f"Invalid dekad range '{token}'")
            selected_ids.update(range(start_id, end_id + 1))
        else:
            selected_ids.add(int(token))

    if not selected_ids:
        if allow_empty:
            return None
        raise ValueError(f"No values were parsed from {field_name}")

    return sorted(selected_ids)


def parse_dekad_ids(value: object) -> list[int]:
    parsed = parse_numeric_selection(value, "phase1_local.processing.dekad_ids")
    assert parsed is not None
    return parsed


def parse_tile_ids(value: object) -> list[int] | None:
    return parse_numeric_selection(value, "phase1_local.processing.tile_id", allow_empty=True)


def format_tile_label(tile_id: int | None) -> str:
    return f"GLB{tile_id:03d}" if tile_id is not None else "GLOBAL"


def format_tile_count_label(tile_count: int) -> str:
    suffix = "tile" if tile_count == 1 else "tiles"
    return f"{tile_count}{suffix}"


def format_dekad_label(record: DekadRecord) -> str:
    dekad_names = {
        1: "first",
        2: "second",
        3: "third",
    }
    month_name = calendar.month_name[record.month]
    return f"{dekad_names.get(record.dekad, str(record.dekad))} dekad of {month_name} {record.year}"


def resolve_noaa_aws_folder_date(calendar_date: str) -> str:
    date_value = datetime.strptime(calendar_date, "%Y%m%d")
    if date_value >= NOAA_FIRST_DAY_FOLDER_START_COMPOSITE_DATE:
        folder_date = date_value - timedelta(days=4)
    else:
        folder_date = date_value
    return folder_date.strftime("%Y%m%d")


def load_config(config_path: Path) -> Phase1Config:
    config_path = config_path.resolve()

    with config_path.open("r", encoding="utf-8") as handle:
        raw_config = yaml.safe_load(handle) or {}

    paths = raw_config.get("paths", {})
    phase1 = raw_config.get("phase1_local", {})
    aws = phase1.get("aws", {})
    processing = phase1.get("processing", {})
    housekeeping = phase1.get("housekeeping", {})

    data_root = Path(paths.get("data_root", "data")).expanduser()
    bucket_prefix = str(aws.get("bucket_prefix", "")).rstrip("/")
    calendar_csv = Path(phase1.get("calendar_csv", DEFAULT_CALENDAR_PATH)).expanduser()
    if not calendar_csv.is_absolute():
        calendar_csv = (PROJECT_ROOT / calendar_csv).resolve()
    dekad_ids = parse_dekad_ids(processing.get("dekad_ids"))
    tile_ids = parse_tile_ids(processing.get("tile_id"))
    noaa_download_retention_days = int(housekeeping.get("noaa_download_retention_days", 15))
    mosaic_classified_raster_retention_days = int(housekeeping.get("mosaic_classified_raster_retention_days", 30))
    cleanup_global_intermediates_after_dekad = parse_bool_setting(
        housekeeping.get("cleanup_global_intermediates_after_dekad", True),
        "phase1_local.housekeeping.cleanup_global_intermediates_after_dekad",
        default=True,
    )

    if not bucket_prefix:
        raise ValueError("config/config.yaml is missing phase1_local.aws.bucket_prefix")
    if noaa_download_retention_days < 0:
        raise ValueError("phase1_local.housekeeping.noaa_download_retention_days must be >= 0")
    if mosaic_classified_raster_retention_days < 0:
        raise ValueError("phase1_local.housekeeping.mosaic_classified_raster_retention_days must be >= 0")

    return Phase1Config(
        data_root=data_root,
        bucket_prefix=bucket_prefix,
        calendar_csv=calendar_csv,
        dekad_ids=dekad_ids,
        tile_ids=tile_ids,
        noaa_download_retention_days=noaa_download_retention_days,
        mosaic_classified_raster_retention_days=mosaic_classified_raster_retention_days,
        cleanup_global_intermediates_after_dekad=cleanup_global_intermediates_after_dekad,
    )


def load_calendar_records(calendar_csv: Path, dekad_ids: list[int]) -> list[DekadRecord]:
    if not calendar_csv.is_file():
        raise FileNotFoundError(f"Dekadal calendar not found: {calendar_csv}")

    selected_ids = set(dekad_ids)
    records_by_id: dict[int, DekadRecord] = {}

    with calendar_csv.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            dekad_id = int(row["dekad_id"])
            if dekad_id not in selected_ids:
                continue

            composite_dates = [
                row["composite_1"].strip(),
                row["composite_2"].strip(),
                row["composite_3"].strip(),
            ]
            composite_dates = [value for value in composite_dates if value]

            records_by_id[dekad_id] = DekadRecord(
                dekad_id=dekad_id,
                year=int(row["year"]),
                month=int(row["month"]),
                dekad=int(row["dekad"]),
                composite_dates=composite_dates,
            )

    missing_ids = [dekad_id for dekad_id in dekad_ids if dekad_id not in records_by_id]
    if missing_ids:
        raise ValueError(f"Dekad IDs not found in calendar CSV: {missing_ids}")

    return [records_by_id[dekad_id] for dekad_id in dekad_ids]


def build_tasks(records: list[DekadRecord]) -> list[CompositeTask]:
    tasks: list[CompositeTask] = []
    seen: set[str] = set()
    total_dekads = len(records)

    for dekad_sequence, record in enumerate(records, start=1):
        total_composites = len(record.composite_dates)
        for composite_sequence, composite_date in enumerate(record.composite_dates, start=1):
            if composite_date in seen:
                continue
            tasks.append(
                CompositeTask(
                    dekad_sequence=dekad_sequence,
                    total_dekads=total_dekads,
                    dekad_id=record.dekad_id,
                    composite_sequence=composite_sequence,
                    total_composites=total_composites,
                    composite_date=composite_date,
                )
            )
            seen.add(composite_date)

    return tasks


def build_paths(config: Phase1Config, composite_date: str, tile_count: int) -> Phase1Paths:
    processing_dir = config.data_root
    filename_suffix = "" if config.tile_ids is None else format_tile_count_label(tile_count)
    target_label = "GLOBAL" if config.tile_ids is None else filename_suffix
    base_name = f"vista_5days_{composite_date}"
    if filename_suffix:
        base_name = f"{base_name}_{filename_suffix}"
    download_dir = processing_dir / "noaa_downloads" / composite_date
    classified_tile_dir = download_dir / "classified_tiles"
    classified_path = processing_dir / "mosaic_classified_rasters" / f"{base_name}.tif"

    return Phase1Paths(
        download_dir=download_dir,
        classified_tile_dir=classified_tile_dir,
        classified_path=classified_path,
        target_label=target_label,
    )


def get_missing_composites_log_path(config: Phase1Config) -> Path:
    return config.data_root / "availability_issues_log.csv"


EVENT_MISSING_COMPOSITE = "missing_composite_for_date"
EVENT_MISSING_SELECTED_TILES = "missing_selected_tiles_for_date"
EVENT_MISSING_GLOBAL_TILES = "missing_global_tiles_for_date"
EVENT_UNEQUAL_DEKAD_COVERAGE = "dekad_aggregated_with_unequal_5day_coverage"


def append_availability_issue_log(
    config: Phase1Config,
    task: CompositeTask,
    aws_folder_date: str,
    event_type: str,
    requested_tiles: list[str],
    available_tiles: list[str],
    missing_tiles: list[str],
    details: str = "",
) -> None:
    log_path = get_missing_composites_log_path(config)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = log_path.is_file()
    run_mode = "GLOBAL" if config.tile_ids is None else "SELECTED_TILES"

    with log_path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if not file_exists:
            writer.writerow(
                [
                    "logged_at",
                    "event_type",
                    "dekad_id",
                    "calendar_composite_date",
                    "noaa_aws_folder_date",
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
                task.dekad_id,
                task.composite_date,
                aws_folder_date,
                run_mode,
                ",".join(requested_tiles),
                ",".join(available_tiles),
                ",".join(missing_tiles),
                details,
            ]
        )

    log(f"Availability issue logged to: {log_path}")


def extract_tile_labels_from_paths(local_paths: list[Path]) -> list[str]:
    tile_labels: set[str] = set()
    for local_path in local_paths:
        parsed = parse_noaa_viirs_filename(local_path)
        if parsed is None:
            continue
        tile_labels.add(parsed[0])
    return sorted(tile_labels)


def list_remote_tifs(bucket_prefix: str, aws_folder_date: str, tile_ids: list[int] | None) -> list[str]:
    s3_prefix = f"{bucket_prefix}/{aws_folder_date[0:4]}/{aws_folder_date[4:6]}/{aws_folder_date[6:8]}/"
    with use_noaa_aws_region():
        result = subprocess.run(
            [AWS_CLI, "s3", "ls", "--no-sign-request", s3_prefix],
            capture_output=True,
            text=True,
            check=False,
        )

    if result.returncode != 0:
        log(f"AWS listing failed for {s3_prefix}. Treating the composite as unavailable.")
        return []

    remote_files: list[str] = []
    allowed_tile_labels = {format_tile_label(tile_id) for tile_id in tile_ids} if tile_ids is not None else None
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        filename = parts[-1]
        if not filename.lower().endswith(".tif"):
            continue
        if allowed_tile_labels and not any(tile_label in filename.upper() for tile_label in allowed_tile_labels):
            continue
        remote_files.append(f"{s3_prefix}{filename}")

    return sorted(remote_files)


def list_local_tifs(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.iterdir() if path.is_file() and path.suffix.lower() == ".tif")


def parse_noaa_viirs_filename(path: Path) -> tuple[str, str, str, str] | None:
    match = NOAA_VIIRS_FILENAME_PATTERN.match(path.name)
    if not match:
        return None
    return (
        match.group("tile").upper(),
        match.group("start"),
        match.group("end"),
        match.group("created"),
    )


def select_best_downloaded_inputs(local_paths: list[Path]) -> list[Path]:
    if len(local_paths) < 2:
        return local_paths

    selected_by_group: dict[tuple[str, str], Path] = {}
    unmatched_paths: list[Path] = []

    for local_path in local_paths:
        parsed = parse_noaa_viirs_filename(local_path)
        if parsed is None:
            unmatched_paths.append(local_path)
            continue

        tile_label, start_timestamp, end_timestamp, created_timestamp = parsed
        group_key = (tile_label, start_timestamp)
        current_best = selected_by_group.get(group_key)
        if current_best is None:
            selected_by_group[group_key] = local_path
            continue

        current_best_parsed = parse_noaa_viirs_filename(current_best)
        assert current_best_parsed is not None
        _, _, current_end_timestamp, current_created_timestamp = current_best_parsed
        if (end_timestamp, created_timestamp, local_path.name) > (
            current_end_timestamp,
            current_created_timestamp,
            current_best.name,
        ):
            selected_by_group[group_key] = local_path

    selected_paths = list(selected_by_group.values()) + unmatched_paths
    return sorted(selected_paths)


def download_remote_files(remote_files: list[str], download_dir: Path, is_global: bool) -> list[Path]:
    download_dir.mkdir(parents=True, exist_ok=True)
    if not remote_files:
        return []

    first_remote = remote_files[0]
    prefix, _ = first_remote.rsplit("/", maxsplit=1)
    s3_prefix = f"{prefix}/"
    local_paths = [download_dir / Path(remote_file).name for remote_file in remote_files]
    missing_paths = [local_path for local_path in local_paths if not local_path.is_file()]

    if not missing_paths:
        log(f"All {len(local_paths)} tile(s) already downloaded for this composite date")
        return local_paths

    missing_filenames = {path.name for path in missing_paths}
    if is_global:
        log(f"Downloading all {len(local_paths)} tile(s) with one recursive AWS copy operation")
        command = [
            AWS_CLI,
            "s3",
            "cp",
            "--recursive",
            "--no-sign-request",
            s3_prefix,
            str(download_dir),
        ]
    else:
        log(
            f"Downloading {len(missing_filenames)} missing tile(s) out of {len(local_paths)} "
            "with one recursive AWS copy operation"
        )
        command = [
            AWS_CLI,
            "s3",
            "cp",
            "--recursive",
            "--no-sign-request",
            s3_prefix,
            str(download_dir),
            "--exclude",
            "*",
        ]
        for filename in sorted(missing_filenames):
            command.extend(["--include", filename])

    with use_noaa_aws_region():
        run_command(command)
    return local_paths


def download_global_remote_files(bucket_prefix: str, aws_folder_date: str, download_dir: Path) -> list[Path]:
    download_dir.mkdir(parents=True, exist_ok=True)
    existing_paths = list_local_tifs(download_dir)
    if existing_paths:
        log(f"Global download folder already contains {len(existing_paths)} tile(s) for this composite date")
        return existing_paths

    s3_prefix = f"{bucket_prefix}/{aws_folder_date[0:4]}/{aws_folder_date[4:6]}/{aws_folder_date[6:8]}/"
    log("Downloading global NOAA date folder with one recursive AWS copy operation")
    command = [
        AWS_CLI,
        "s3",
        "cp",
        "--recursive",
        "--no-sign-request",
        s3_prefix,
        str(download_dir),
    ]
    with use_noaa_aws_region():
        run_command(command)
    return list_local_tifs(download_dir)


def run_command(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        if result.stderr:
            print(result.stderr, end="" if result.stderr.endswith("\n") else "\n")
        raise subprocess.CalledProcessError(result.returncode, command)


def run_download_step(config: Phase1Config, task: CompositeTask) -> DownloadResult:
    aws_folder_date = resolve_noaa_aws_folder_date(task.composite_date)

    log("-" * 60)
    log(f"Dekad progress: {task.dekad_sequence} out of {task.total_dekads} selected")
    log(f"Dekad ID: {task.dekad_id}")
    log(f"NOAA 5-day composite progress within dekad: {task.composite_sequence} out of {task.total_composites}")
    log(f"NOAA 5-day composite calendar date: {task.composite_date} -> NOAA AWS folder date: {aws_folder_date}")
    if config.tile_ids is None:
        log("Tile selection: GLOBAL")
    else:
        log(f"Tile selection: {[format_tile_label(tile_id) for tile_id in config.tile_ids]}")

    if config.tile_ids is None:
        provisional_paths = build_paths(config, task.composite_date, tile_count=0)
        downloaded_paths = download_global_remote_files(config.bucket_prefix, aws_folder_date, provisional_paths.download_dir)
        local_paths = select_best_downloaded_inputs(downloaded_paths)
        available_tiles = extract_tile_labels_from_paths(local_paths)
        missing_tiles = sorted(EXPECTED_GLOBAL_TILE_LABELS - set(available_tiles))
        if not local_paths:
            log("Composite not available on AWS for the derived folder date. Skipping.")
            append_availability_issue_log(
                config,
                task,
                aws_folder_date,
                event_type=EVENT_MISSING_COMPOSITE,
                requested_tiles=sorted(EXPECTED_GLOBAL_TILE_LABELS),
                available_tiles=[],
                missing_tiles=sorted(EXPECTED_GLOBAL_TILE_LABELS),
                details="No usable NOAA tiles were found after download and duplicate filtering.",
            )
            return DownloadResult(
                aws_folder_date=aws_folder_date,
                remote_files=[],
                local_paths=None,
                paths=None,
                requested_tiles=sorted(EXPECTED_GLOBAL_TILE_LABELS),
                available_tiles=[],
                missing_tiles=sorted(EXPECTED_GLOBAL_TILE_LABELS),
                is_partial=False,
            )

        if missing_tiles:
            append_availability_issue_log(
                config,
                task,
                aws_folder_date,
                event_type=EVENT_MISSING_GLOBAL_TILES,
                requested_tiles=sorted(EXPECTED_GLOBAL_TILE_LABELS),
                available_tiles=available_tiles,
                missing_tiles=missing_tiles,
                details="Global composite processed with missing non-excluded tiles.",
            )

        paths = build_paths(config, task.composite_date, len(local_paths))
        remote_files = [f"{config.bucket_prefix}/{aws_folder_date[0:4]}/{aws_folder_date[4:6]}/{aws_folder_date[6:8]}/{path.name}" for path in local_paths]
        return DownloadResult(
            aws_folder_date=aws_folder_date,
            remote_files=remote_files,
            local_paths=local_paths,
            paths=paths,
            requested_tiles=sorted(EXPECTED_GLOBAL_TILE_LABELS),
            available_tiles=available_tiles,
            missing_tiles=missing_tiles,
            is_partial=bool(missing_tiles),
        )

    requested_tiles = [format_tile_label(tile_id) for tile_id in config.tile_ids]
    remote_files = list_remote_tifs(config.bucket_prefix, aws_folder_date, config.tile_ids)
    if not remote_files:
        log("Composite not available on AWS for the derived folder date. Skipping.")
        append_availability_issue_log(
            config,
            task,
            aws_folder_date,
            event_type=EVENT_MISSING_COMPOSITE,
            requested_tiles=requested_tiles,
            available_tiles=[],
            missing_tiles=requested_tiles,
            details="No requested NOAA tiles were found on AWS for the composite date.",
        )
        return DownloadResult(
            aws_folder_date=aws_folder_date,
            remote_files=[],
            local_paths=None,
            paths=None,
            requested_tiles=requested_tiles,
            available_tiles=[],
            missing_tiles=requested_tiles,
            is_partial=False,
        )

    provisional_paths = build_paths(config, task.composite_date, tile_count=0)
    downloaded_paths = download_remote_files(remote_files, provisional_paths.download_dir, is_global=False)
    local_paths = select_best_downloaded_inputs(downloaded_paths)
    available_tiles = extract_tile_labels_from_paths(local_paths)
    missing_tiles = sorted(set(requested_tiles) - set(available_tiles))
    if not local_paths:
        log("No usable local NOAA raster remained after duplicate filtering. Skipping.")
        append_availability_issue_log(
            config,
            task,
            aws_folder_date,
            event_type=EVENT_MISSING_COMPOSITE,
            requested_tiles=requested_tiles,
            available_tiles=[],
            missing_tiles=requested_tiles,
            details="Requested NOAA tiles were listed but no usable local rasters remained after duplicate filtering.",
        )
        return DownloadResult(
            aws_folder_date=aws_folder_date,
            remote_files=[],
            local_paths=None,
            paths=None,
            requested_tiles=requested_tiles,
            available_tiles=[],
            missing_tiles=requested_tiles,
            is_partial=False,
        )

    if missing_tiles:
        append_availability_issue_log(
            config,
            task,
            aws_folder_date,
            event_type=EVENT_MISSING_SELECTED_TILES,
            requested_tiles=requested_tiles,
            available_tiles=available_tiles,
            missing_tiles=missing_tiles,
            details="Selected-tile composite processed with missing requested tiles.",
        )

    paths = build_paths(config, task.composite_date, len(local_paths))
    return DownloadResult(
        aws_folder_date=aws_folder_date,
        remote_files=remote_files,
        local_paths=local_paths,
        paths=paths,
        requested_tiles=requested_tiles,
        available_tiles=available_tiles,
        missing_tiles=missing_tiles,
        is_partial=bool(missing_tiles),
    )


def classify_block(block: np.ndarray) -> np.ndarray:
    # Convert NOAA source classes into the simplified classes used downstream.
    classified = np.zeros(block.shape, dtype=np.uint8)
    classified[block == 15] = 11
    classified[(block >= 16) & (block <= 17)] = 10
    classified[block == 20] = 4
    classified[block == 27] = 5
    classified[block == 38] = 6
    classified[block == 30] = 9
    classified[block == 99] = 11
    classified[block == 100] = 11
    classified[(block >= 101) & (block <= 149)] = 1
    classified[(block >= 150) & (block <= 179)] = 2
    classified[(block >= 180) & (block <= 200)] = 3
    return classified


def classify_raster(input_raster_path: Path, classified_path: Path) -> None:
    # Process the raster in blocks so large rasters can be handled safely.
    if classified_path.is_file():
        return

    classified_path.parent.mkdir(parents=True, exist_ok=True)

    with rasterio.open(input_raster_path) as src:
        profile = src.profile.copy()
        profile.update(dtype=rasterio.uint8, nodata=0, compress="DEFLATE")

        with rasterio.open(classified_path, "w", **profile) as dst:
            for _, window in src.block_windows(1):
                block = src.read(1, window=window)
                dst.write(classify_block(block), 1, window=window)


def build_classified_tile_path(classified_tile_dir: Path, source_tile_path: Path) -> Path:
    return classified_tile_dir / f"vista_classified_{source_tile_path.name}"


def classify_tiles(local_paths: list[Path], classified_tile_dir: Path) -> list[Path]:
    classified_tile_dir.mkdir(parents=True, exist_ok=True)
    classified_paths: list[Path] = []
    total_files = len(local_paths)

    for file_index, local_path in enumerate(local_paths, start=1):
        classified_path = build_classified_tile_path(classified_tile_dir, local_path)
        classified_paths.append(classified_path)
        if classified_path.is_file():
            log(f"Classified tile/file {file_index} out of {total_files} already exists: {classified_path.name}")
            continue
        log(f"Classifying tile/file {file_index} out of {total_files}: {local_path.name}")
        classify_raster(local_path, classified_path)

    return classified_paths


def mosaic_classified_tiles(classified_paths: list[Path], output_path: Path) -> Path:
    if not classified_paths:
        raise FileNotFoundError("No classified VIIRS tiles are available for mosaicking.")

    if output_path.is_file():
        log(f"Classified raster already exists: {output_path}")
        return output_path

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if len(classified_paths) == 1:
        log("Single classified tile available. Skipping mosaic.")
        shutil.copy2(classified_paths[0], output_path)
        log(f"Classified raster written to {output_path}")
        return output_path

    log(f"Mosaicking {len(classified_paths)} classified tiles")
    sources = [rasterio.open(local_path) for local_path in classified_paths]
    try:
        mosaic, transform = merge(sources)
        profile = sources[0].profile.copy()
    finally:
        for source in sources:
            source.close()

    profile.update(
        driver="GTiff",
        height=mosaic.shape[1],
        width=mosaic.shape[2],
        transform=transform,
        compress="DEFLATE",
    )

    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(mosaic)

    log(f"Classified raster written to {output_path}")
    return output_path


def run_classification_step(download_result: DownloadResult) -> list[Path] | None:
    if not download_result.local_paths or download_result.paths is None:
        return None
    return classify_tiles(download_result.local_paths, download_result.paths.classified_tile_dir)


def run_mosaic_step(download_result: DownloadResult, classified_paths: list[Path] | None) -> Path | None:
    if not classified_paths or download_result.paths is None:
        return None
    return mosaic_classified_tiles(classified_paths, download_result.paths.classified_path)


def cleanup_old_files(root_dir: Path, retention_days: int, label: str) -> None:
    if not root_dir.is_dir():
        log(f"Cleanup task: {label} folder does not exist. Nothing to remove.")
        return

    cutoff_time = datetime.now() - timedelta(days=retention_days)
    removed_files = 0
    removed_dirs = 0

    log(
        f"Cleanup task: removing {label} files older than {retention_days} days from {root_dir}"
    )

    for file_path in sorted(root_dir.rglob("*"), reverse=True):
        if not file_path.is_file():
            continue
        modified_time = datetime.fromtimestamp(file_path.stat().st_mtime)
        if modified_time >= cutoff_time:
            continue
        file_path.unlink()
        removed_files += 1

    for dir_path in sorted(root_dir.rglob("*"), reverse=True):
        if not dir_path.is_dir():
            continue
        try:
            dir_path.rmdir()
            removed_dirs += 1
        except OSError:
            continue

    log(f"Cleanup task: removed {removed_files} old {label} file(s)")
    log(f"Cleanup task: removed {removed_dirs} empty folder(s)")


def remove_file_if_exists(file_path: Path) -> int:
    if not file_path.is_file():
        return 0
    file_path.unlink()
    return 1


def remove_directory_if_exists(dir_path: Path) -> tuple[int, int]:
    if not dir_path.is_dir():
        return 0, 0

    file_count = sum(1 for path in dir_path.rglob("*") if path.is_file())
    dir_count = sum(1 for path in dir_path.rglob("*") if path.is_dir()) + 1
    shutil.rmtree(dir_path)
    return file_count, dir_count


def cleanup_global_intermediates_for_dekad(config: Phase1Config, record: DekadRecord) -> None:
    if not config.cleanup_global_intermediates_after_dekad:
        return
    if config.tile_ids is not None:
        return

    dekadal_output_path = get_dekadal_output_path(config, record)
    if not dekadal_output_path.is_file():
        log(
            "Global intermediate cleanup skipped: "
            f"final dekadal raster does not exist for {record.year}-{record.month:02d} dekad {record.dekad}"
        )
        return

    removed_files = 0
    removed_dirs = 0
    for composite_date in record.composite_dates:
        download_dir = config.data_root / "noaa_downloads" / composite_date
        file_count, dir_count = remove_directory_if_exists(download_dir)
        removed_files += file_count
        removed_dirs += dir_count

        classified_path = config.data_root / "mosaic_classified_rasters" / f"vista_5days_{composite_date}.tif"
        removed_files += remove_file_if_exists(classified_path)

    log(
        "Global intermediate cleanup after successful dekad: "
        f"removed {removed_files} file(s) and {removed_dirs} folder(s) for "
        f"{record.year}-{record.month:02d} dekad {record.dekad}"
    )


def run_cleanup_step(config: Phase1Config, download_result: DownloadResult, input_raster_path: Path | None) -> None:
    return


def run_housekeeping_step(config: Phase1Config) -> None:
    cleanup_old_files(config.data_root / "noaa_downloads", config.noaa_download_retention_days, "NOAA download")
    cleanup_old_files(
        config.data_root / "mosaic_classified_rasters",
        config.mosaic_classified_raster_retention_days,
        "mosaic-classified raster",
    )


def get_dekadal_output_path(config: Phase1Config, record: DekadRecord, output_suffix: str = "") -> Path:
    dekad_label = f"{record.year}{record.month:02d}d{record.dekad}"
    suffix = f"_{output_suffix}" if output_suffix else ""
    if config.tile_ids is None:
        filename = f"vista_dekad_{dekad_label}{suffix}.tif"
    else:
        filename = f"vista_dekad_{dekad_label}_{format_tile_count_label(len(config.tile_ids))}{suffix}.tif"
    return config.data_root / "dekadal_rasters" / filename


def aggregate_classified_blocks(blocks: list[np.ndarray]) -> np.ndarray:
    stacked = np.stack(blocks, axis=0)
    aggregated = np.zeros(stacked.shape[1:], dtype=np.uint8)

    for class_value in DEKAD_AGGREGATION_PRIORITY:
        aggregated = np.where(np.any(stacked == class_value, axis=0), class_value, aggregated)

    return aggregated


def create_dekadal_raster(input_paths: list[Path], output_path: Path) -> None:
    if output_path.is_file():
        log(f"Dekadal raster already exists: {output_path}")
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)

    sources = [rasterio.open(input_path) for input_path in input_paths]
    try:
        reference = sources[0]
        min_left = min(src.bounds.left for src in sources)
        min_bottom = min(src.bounds.bottom for src in sources)
        max_right = max(src.bounds.right for src in sources)
        max_top = max(src.bounds.top for src in sources)
        x_res, y_res = reference.res
        width = max(1, int(np.ceil((max_right - min_left) / x_res)))
        height = max(1, int(np.ceil((max_top - min_bottom) / abs(y_res))))
        transform = from_origin(min_left, max_top, x_res, abs(y_res))

        profile = reference.profile.copy()
        profile.update(dtype=rasterio.uint8, nodata=0, compress="DEFLATE")
        profile.update(
            height=height,
            width=width,
            transform=transform,
        )

        with rasterio.open(output_path, "w", **profile) as dst:
            for _, window in dst.block_windows(1):
                block_bounds = window_bounds(window, dst.transform)
                blocks: list[np.ndarray] = []
                for src in sources:
                    src_window = window_from_bounds(*block_bounds, transform=src.transform)
                    src_window = src_window.round_offsets().round_lengths()
                    block = src.read(
                        1,
                        window=src_window,
                        boundless=True,
                        fill_value=0,
                        out_shape=(int(window.height), int(window.width)),
                    )
                    blocks.append(block)
                dst.write(aggregate_classified_blocks(blocks), 1, window=window)
            dst.write_colormap(1, DEKAD_OUTPUT_COLORMAP)
    finally:
        for src in sources:
            src.close()

    log(f"Dekadal raster written to {output_path}")


def build_composite_output_index(composite_outputs: dict[str, Path]) -> dict[str, Path]:
    return {composite_date: path for composite_date, path in composite_outputs.items() if path.is_file()}


def run_dekadal_step(
    config: Phase1Config,
    records: list[DekadRecord],
    composite_outputs: dict[str, Path],
) -> None:
    available_outputs = build_composite_output_index(composite_outputs)

    for record in records:
        missing_dates = [composite_date for composite_date in record.composite_dates if composite_date not in available_outputs]
        if missing_dates:
            log(
                "Skipping dekadal aggregation for "
                f"{record.year}-{record.month:02d} dekad {record.dekad}: "
                f"missing classified composite raster(s) for {missing_dates}"
            )
            continue

        input_paths = [available_outputs[composite_date] for composite_date in record.composite_dates]
        output_path = get_dekadal_output_path(config, record)
        with_sources = [rasterio.open(input_path) for input_path in input_paths]
        try:
            union_bounds = (
                min(src.bounds.left for src in with_sources),
                min(src.bounds.bottom for src in with_sources),
                max(src.bounds.right for src in with_sources),
                max(src.bounds.top for src in with_sources),
            )
            partial_inputs = [
                input_path.name
                for input_path, src in zip(input_paths, with_sources)
                if (
                    src.bounds.left,
                    src.bounds.bottom,
                    src.bounds.right,
                    src.bounds.top,
                ) != union_bounds
            ]
        finally:
            for src in with_sources:
                src.close()

        if partial_inputs:
            append_availability_issue_log(
                config,
                CompositeTask(
                    dekad_sequence=records.index(record) + 1,
                    total_dekads=len(records),
                    dekad_id=record.dekad_id,
                    composite_sequence=0,
                    total_composites=len(record.composite_dates),
                    composite_date="",
                ),
                aws_folder_date="",
                event_type=EVENT_UNEQUAL_DEKAD_COVERAGE,
                requested_tiles=[],
                available_tiles=[],
                missing_tiles=[],
                details=(
                    "Dekadal aggregation used 5-day composites with unequal coverage. "
                    f"Dekad composite dates: {','.join(record.composite_dates)}. "
                    f"Composite rasters with smaller coverage than the dekad union footprint: {','.join(partial_inputs)}"
                ),
            )
        log(
            f"Aggregating dekadal raster for {record.year}-{record.month:02d} dekad {record.dekad} "
            f"from {len(input_paths)} classified 5-day raster(s)"
        )
        create_dekadal_raster(input_paths, output_path)


def log_phase1_plan(config: Phase1Config, records: list[DekadRecord], tasks: list[CompositeTask]) -> None:
    log(f"Calendar CSV: {config.calendar_csv}")
    selected_dekads = [f"{record.dekad_id} ({format_dekad_label(record)})" for record in records]
    log(f"Selected dekads: {selected_dekads}")
    if config.tile_ids is None:
        log("Tile selection: GLOBAL")
    else:
        log(f"Tile selection: {[format_tile_label(tile_id) for tile_id in config.tile_ids]}")
    log(f"NOAA download retention (days): {config.noaa_download_retention_days}")
    log(f"Mosaic-classified raster retention (days): {config.mosaic_classified_raster_retention_days}")
    log(f"Cleanup global intermediates after each successful dekad: {config.cleanup_global_intermediates_after_dekad}")
    log(f"Availability issues log: {get_missing_composites_log_path(config)}")
    log(f"This run needs {len(tasks)} NOAA 5-day composite(s) to download and process")


def process_task(config: Phase1Config, task: CompositeTask) -> None:
    download_result = run_download_step(config, task)
    if not download_result.remote_files:
        return

    classified_paths = run_classification_step(download_result)
    input_raster_path = run_mosaic_step(download_result, classified_paths)
    run_cleanup_step(config, download_result, input_raster_path)
