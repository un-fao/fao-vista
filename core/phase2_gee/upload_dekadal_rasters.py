import argparse
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml
from google.cloud import storage

from core.quality_control.exclusions import (
    DekadExclusion,
    load_dekad_exclusions,
    record_dekad_label,
)
from core.phase1_local.process_dekad_composites import (
    CONFIG_PATH,
    DekadRecord,
    get_dekadal_output_path,
    initialize_run_log,
    load_calendar_records,
    load_config,
    log,
    log_external_output,
    parse_bool_setting,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload final dekadal rasters to Google Cloud Storage and Earth Engine."
    )
    parser.add_argument(
        "--config",
        default=str(CONFIG_PATH),
        help="Path to config/config.yaml.",
    )
    parser.add_argument(
        "--ee-upload-sleep-seconds",
        type=int,
        default=None,
        help="Optional override for the Phase 2 Earth Engine upload delay.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Report Phase 2 actions without uploading to GCS or submitting Earth Engine ingestions.",
    )
    missing_group = parser.add_mutually_exclusive_group()
    missing_group.add_argument(
        "--strict-missing",
        action="store_true",
        default=None,
        help="Fail if a selected dekad has neither a final local raster nor the expected GCS object.",
    )
    missing_group.add_argument(
        "--skip-missing",
        action="store_true",
        default=None,
        help="Skip selected dekads with neither a final local raster nor the expected GCS object and continue.",
    )
    return parser.parse_args()


def load_phase2_settings(config_path: Path) -> dict:
    with config_path.open("r", encoding="utf-8") as handle:
        raw_config = yaml.safe_load(handle) or {}

    phase2 = raw_config.get("phase2_gee", {})
    gcp = phase2.get("gcp", {})
    environments = phase2.get("environments", {})
    gcs = phase2.get("gcs", {})
    earth_engine = phase2.get("earth_engine", {})
    run_control = phase2.get("run_control", {})

    project_id = str(gcp.get("project_id", "")).strip()
    environment_settings = environments.get(project_id, {}) if isinstance(environments, dict) else {}
    environment_gcs = environment_settings.get("gcs", {}) if isinstance(environment_settings, dict) else {}
    environment_earth_engine = (
        environment_settings.get("earth_engine", {}) if isinstance(environment_settings, dict) else {}
    )

    bucket_name = str(environment_gcs.get("bucket_name", gcs.get("bucket_name", ""))).strip()
    gcs_prefix = str(environment_gcs.get("prefix", gcs.get("prefix", "vista_dekadal_rasters"))).strip().strip("/")
    upload_chunk_size_mb = int(gcs.get("upload_chunk_size_mb", 50))
    upload_timeout_seconds = int(gcs.get("upload_timeout_seconds", 1200))
    ee_asset_root = str(
        environment_earth_engine.get("asset_root", earth_engine.get("asset_root", ""))
    ).strip().rstrip("/")
    ee_upload_sleep_seconds = int(earth_engine.get("upload_sleep_seconds", 0))
    dry_run = parse_bool_setting(
        run_control.get("dry_run", False),
        "phase2_gee.run_control.dry_run",
    )
    strict_missing_rasters = parse_bool_setting(
        run_control.get("strict_missing_rasters", False),
        "phase2_gee.run_control.strict_missing_rasters",
    )

    if not project_id:
        raise ValueError("config/config.yaml is missing phase2_gee.gcp.project_id")
    if environments and project_id not in environments:
        raise ValueError(
            "config/config.yaml is missing phase2_gee.environments."
            f"{project_id} for the selected phase2_gee.gcp.project_id"
        )
    if not bucket_name:
        raise ValueError(
            "config/config.yaml is missing the Phase 2 GCS bucket name for the selected project. "
            "Set phase2_gee.environments.<project_id>.gcs.bucket_name or phase2_gee.gcs.bucket_name."
        )
    if upload_chunk_size_mb <= 0:
        raise ValueError("phase2_gee.gcs.upload_chunk_size_mb must be > 0")
    if upload_timeout_seconds <= 0:
        raise ValueError("phase2_gee.gcs.upload_timeout_seconds must be > 0")
    if not ee_asset_root:
        raise ValueError(
            "config/config.yaml is missing the Phase 2 Earth Engine asset root for the selected project. "
            "Set phase2_gee.environments.<project_id>.earth_engine.asset_root or "
            "phase2_gee.earth_engine.asset_root."
        )
    if ee_upload_sleep_seconds < 0:
        raise ValueError("phase2_gee.earth_engine.upload_sleep_seconds must be >= 0")

    return {
        "project_id": project_id,
        "bucket_name": bucket_name,
        "gcs_prefix": gcs_prefix,
        "upload_chunk_size_mb": upload_chunk_size_mb,
        "upload_timeout_seconds": upload_timeout_seconds,
        "ee_asset_root": ee_asset_root,
        "ee_upload_sleep_seconds": ee_upload_sleep_seconds,
        "dry_run": dry_run,
        "strict_missing_rasters": strict_missing_rasters,
    }


@dataclass
class Phase2Uploader:
    project_id: str
    bucket_name: str
    gcs_prefix: str
    upload_chunk_size_mb: int
    upload_timeout_seconds: int
    ee_asset_root: str
    ee_upload_sleep_seconds: int
    earthengine_cli: str
    bucket: storage.Bucket


@dataclass
class Phase2RunControl:
    dry_run: bool
    strict_missing_rasters: bool


@dataclass
class Phase2RecordResult:
    gcs_uploaded: bool = False
    gcs_already_exists: bool = False
    ee_submitted: bool = False
    ee_already_exists: bool = False
    dry_run_gcs_upload: bool = False
    dry_run_ee_upload: bool = False


@dataclass
class DekadUploadPlan:
    record: DekadRecord
    raster_path: Path
    gcs_object_name: str
    gs_uri: str
    ee_asset_id: str
    local_exists: bool
    gcs_exists: bool
    ee_exists: bool
    exclusion: DekadExclusion | None = None


def resolve_earthengine_cli() -> str:
    candidates = []
    executable_dir = Path(sys.executable).resolve().parent
    if sys.platform.startswith("win"):
        candidates.append(executable_dir / "Scripts" / "earthengine.exe")
    else:
        candidates.append(executable_dir / "earthengine")
        candidates.append(executable_dir / "bin" / "earthengine")

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    earthengine_path = shutil.which("earthengine")
    if earthengine_path:
        return earthengine_path

    raise FileNotFoundError(
        "Earth Engine CLI not found in the active environment or on PATH. "
        "Install earthengine-api and verify the selected Python environment."
    )


def blob_exists(bucket, object_name: str) -> bool:
    return bucket.blob(object_name).exists()


def upload_file_to_gcs(
    bucket,
    local_path: Path,
    object_name: str,
    upload_chunk_size_mb: int,
    upload_timeout_seconds: int,
) -> None:
    blob = bucket.blob(object_name)
    blob.chunk_size = upload_chunk_size_mb * 1024 * 1024
    blob.upload_from_filename(str(local_path), timeout=upload_timeout_seconds)
    log(f"Uploaded to GCS: gs://{bucket.name}/{object_name}")


def ee_asset_exists(earthengine_cli: str, project_id: str, asset_id: str) -> bool:
    result = subprocess.run(
        [earthengine_cli, "--project", project_id, "asset", "info", asset_id],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def run_earthengine_command(earthengine_cli: str, project_id: str, *args: str) -> subprocess.CompletedProcess:
    command = [earthengine_cli, "--project", project_id, *args]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        if result.stdout:
            log_external_output(result.stdout)
        if result.stderr:
            log_external_output(result.stderr)
        raise subprocess.CalledProcessError(result.returncode, command)
    return result


def list_gcs_object_names(uploader: Phase2Uploader) -> set[str]:
    search_prefix = f"{uploader.gcs_prefix}/" if uploader.gcs_prefix else ""
    return {blob.name for blob in uploader.bucket.list_blobs(prefix=search_prefix)}


def list_ee_asset_ids(uploader: Phase2Uploader) -> set[str]:
    result = run_earthengine_command(
        uploader.earthengine_cli,
        uploader.project_id,
        "ls",
        uploader.ee_asset_root,
    )
    asset_ids: set[str] = set()
    for line in result.stdout.splitlines():
        asset_id = line.strip()
        if asset_id:
            asset_ids.add(asset_id)
    return asset_ids


def run_ee_upload(earthengine_cli: str, project_id: str, asset_id: str, gs_uri: str) -> None:
    command = [
        earthengine_cli,
        "--project",
        project_id,
        "upload",
        "image",
        f"--asset_id={asset_id}",
        "--pyramiding_policy=mode",
        gs_uri,
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        if result.stdout:
            log_external_output(result.stdout)
        if result.stderr:
            log_external_output(result.stderr)
        raise subprocess.CalledProcessError(result.returncode, command)

    if result.stdout:
        log_external_output(result.stdout)
    if result.stderr and "httplib2 transport does not support per-request timeout" not in result.stderr:
        log_external_output(result.stderr)


def build_gcs_object_name(gcs_prefix: str, raster_path: Path) -> str:
    return f"{gcs_prefix}/{raster_path.name}" if gcs_prefix else raster_path.name


def build_ee_asset_id(ee_asset_root: str, raster_path: Path) -> str:
    return f"{ee_asset_root}/{raster_path.stem}"


def build_upload_plan(
    records: list[DekadRecord],
    phase1_config,
    uploader: Phase2Uploader,
    gcs_object_names: set[str] | None = None,
    ee_asset_ids: set[str] | None = None,
    output_suffix: str = "",
    exclusions: dict[str, DekadExclusion] | None = None,
) -> list[DekadUploadPlan]:
    if gcs_object_names is None:
        gcs_object_names = list_gcs_object_names(uploader)
    if ee_asset_ids is None:
        ee_asset_ids = list_ee_asset_ids(uploader)
    if exclusions is None:
        exclusions = load_dekad_exclusions()

    plans: list[DekadUploadPlan] = []
    for record in records:
        raster_path = get_dekadal_output_path(phase1_config, record, output_suffix=output_suffix)
        object_name = build_gcs_object_name(uploader.gcs_prefix, raster_path)
        plans.append(
            DekadUploadPlan(
                record=record,
                raster_path=raster_path,
                gcs_object_name=object_name,
                gs_uri=f"gs://{uploader.bucket.name}/{object_name}",
                ee_asset_id=build_ee_asset_id(uploader.ee_asset_root, raster_path),
                local_exists=raster_path.is_file(),
                gcs_exists=object_name in gcs_object_names,
                ee_exists=build_ee_asset_id(uploader.ee_asset_root, raster_path) in ee_asset_ids,
                exclusion=exclusions.get(record_dekad_label(record)),
            )
        )
    return plans


def refresh_upload_plan_local_state(plan: DekadUploadPlan) -> DekadUploadPlan:
    return DekadUploadPlan(
        record=plan.record,
        raster_path=plan.raster_path,
        gcs_object_name=plan.gcs_object_name,
        gs_uri=plan.gs_uri,
        ee_asset_id=plan.ee_asset_id,
        local_exists=plan.raster_path.is_file(),
        gcs_exists=plan.gcs_exists,
        ee_exists=plan.ee_exists,
        exclusion=plan.exclusion,
    )


def log_upload_plan_summary(plans: list[DekadUploadPlan]) -> None:
    excluded_count = sum(1 for plan in plans if plan.exclusion is not None)
    ee_complete_count = sum(1 for plan in plans if plan.ee_exists)
    local_ready_count = sum(1 for plan in plans if plan.local_exists and not plan.ee_exists)
    gcs_ready_count = sum(1 for plan in plans if plan.gcs_exists and not plan.ee_exists)
    missing_source_count = sum(
        1 for plan in plans if not plan.local_exists and not plan.gcs_exists and not plan.ee_exists
    )
    log("Phase 2 resume inventory:")
    log(f"  Selected dekads: {len(plans)}")
    log(f"  Quality-control excluded: {excluded_count}")
    log(f"  Already present in Earth Engine: {ee_complete_count}")
    log(f"  Local final raster exists, upload/check needed: {local_ready_count}")
    log(f"  GCS object exists while EE asset is missing: {gcs_ready_count}")
    log(f"  Missing both local raster and GCS object: {missing_source_count}")


def log_phase2_plan(uploader: Phase2Uploader) -> None:
    log(f"Using Earth Engine CLI: {uploader.earthengine_cli}")
    log(f"GCP project: {uploader.project_id}")
    log(f"GCS bucket: {uploader.bucket_name}")
    log(f"GCS prefix: {uploader.gcs_prefix or '(bucket root)'}")
    log(f"GCS upload chunk size (MB): {uploader.upload_chunk_size_mb}")
    log(f"GCS upload timeout (seconds): {uploader.upload_timeout_seconds}")
    log(f"Earth Engine asset root: {uploader.ee_asset_root}")


def log_phase2_run_control(run_control: Phase2RunControl) -> None:
    log(f"Phase 2 dry run: {run_control.dry_run}")
    missing_behavior = "fail" if run_control.strict_missing_rasters else "skip and continue"
    log(f"Missing local/GCS source raster behavior: {missing_behavior}")


def build_phase2_run_control(config_path: Path, args: argparse.Namespace) -> Phase2RunControl:
    phase2_settings = load_phase2_settings(config_path)
    dry_run = phase2_settings["dry_run"]
    strict_missing_rasters = phase2_settings["strict_missing_rasters"]

    if args.dry_run is True:
        dry_run = True
    if args.strict_missing is True:
        strict_missing_rasters = True
    if args.skip_missing is True:
        strict_missing_rasters = False

    return Phase2RunControl(
        dry_run=dry_run,
        strict_missing_rasters=strict_missing_rasters,
    )


def build_phase2_uploader(config_path: Path, ee_upload_sleep_seconds_override: int | None = None) -> Phase2Uploader:
    phase2_settings = load_phase2_settings(config_path)
    if ee_upload_sleep_seconds_override is not None:
        phase2_settings["ee_upload_sleep_seconds"] = ee_upload_sleep_seconds_override

    earthengine_cli = resolve_earthengine_cli()
    storage_client = storage.Client(project=phase2_settings["project_id"])
    bucket = storage_client.bucket(phase2_settings["bucket_name"])

    return Phase2Uploader(
        project_id=phase2_settings["project_id"],
        bucket_name=phase2_settings["bucket_name"],
        gcs_prefix=phase2_settings["gcs_prefix"],
        upload_chunk_size_mb=phase2_settings["upload_chunk_size_mb"],
        upload_timeout_seconds=phase2_settings["upload_timeout_seconds"],
        ee_asset_root=phase2_settings["ee_asset_root"],
        ee_upload_sleep_seconds=phase2_settings["ee_upload_sleep_seconds"],
        earthengine_cli=earthengine_cli,
        bucket=bucket,
    )


def upload_record(
    record: DekadRecord,
    raster_path: Path,
    uploader: Phase2Uploader,
    dry_run: bool = False,
    gcs_exists: bool | None = None,
    ee_exists: bool | None = None,
) -> Phase2RecordResult:
    object_name = build_gcs_object_name(uploader.gcs_prefix, raster_path)
    gs_uri = f"gs://{uploader.bucket.name}/{object_name}"
    asset_id = build_ee_asset_id(uploader.ee_asset_root, raster_path)
    result = Phase2RecordResult()

    log(f"Phase 2 target: {raster_path.name}")

    if gcs_exists is None:
        gcs_exists = blob_exists(uploader.bucket, object_name)

    if gcs_exists:
        log(f"GCS object already exists: {gs_uri}")
        result.gcs_already_exists = True
    else:
        if not raster_path.is_file():
            raise FileNotFoundError(f"Local raster required for GCS upload was not found: {raster_path}")
        if dry_run:
            log(f"[DRY RUN] Would upload raster to GCS: {gs_uri}")
            result.dry_run_gcs_upload = True
        else:
            log(f"Uploading raster to GCS: {gs_uri}")
            upload_file_to_gcs(
                uploader.bucket,
                raster_path,
                object_name,
                uploader.upload_chunk_size_mb,
                uploader.upload_timeout_seconds,
            )
            result.gcs_uploaded = True

    if ee_exists is None:
        ee_exists = ee_asset_exists(uploader.earthengine_cli, uploader.project_id, asset_id)

    if ee_exists:
        log(f"Earth Engine asset already exists: {asset_id}")
        result.ee_already_exists = True
        return result

    if dry_run:
        log(f"[DRY RUN] Would submit raster to Earth Engine: {asset_id}")
        result.dry_run_ee_upload = True
        return result

    log(f"Uploading raster to Earth Engine: {asset_id}")
    if uploader.ee_upload_sleep_seconds > 0:
        time.sleep(uploader.ee_upload_sleep_seconds)
    run_ee_upload(uploader.earthengine_cli, uploader.project_id, asset_id, gs_uri)
    log(f"Earth Engine upload submitted: {asset_id}")
    result.ee_submitted = True
    return result


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()
    phase1_config = load_config(config_path)
    initialize_run_log(phase1_config.data_root, "phase2")
    records = load_calendar_records(phase1_config.calendar_csv, phase1_config.dekad_ids)
    run_control = build_phase2_run_control(config_path, args)
    uploader = build_phase2_uploader(config_path, args.ee_upload_sleep_seconds)
    log_phase2_plan(uploader)
    log_phase2_run_control(run_control)

    uploaded_count = 0
    gcs_existing_count = 0
    ee_submitted_count = 0
    ee_existing_count = 0
    dry_run_gcs_upload_count = 0
    dry_run_ee_upload_count = 0
    skipped_missing_count = 0
    skipped_ee_existing_count = 0
    skipped_quality_control_count = 0

    plans = build_upload_plan(records, phase1_config, uploader)
    log_upload_plan_summary(plans)

    for plan in plans:
        if plan.exclusion is not None:
            log(
                f"Skipping Phase 2 for quality-control excluded dekad "
                f"{plan.exclusion.label}: {plan.exclusion.reason}"
            )
            skipped_quality_control_count += 1
            continue
        if plan.ee_exists:
            log(f"Skipping Phase 2: Earth Engine asset already exists: {plan.ee_asset_id}")
            ee_existing_count += 1
            skipped_ee_existing_count += 1
            continue

        if not plan.local_exists and not plan.gcs_exists:
            message = (
                f"Final dekadal raster not found locally or in GCS for dekad {plan.record.dekad_id} "
                f"({plan.record.year}-{plan.record.month:02d} dekad {plan.record.dekad}): "
                f"{plan.raster_path}; {plan.gs_uri}"
            )
            if run_control.strict_missing_rasters:
                raise FileNotFoundError(message)
            log(f"Skipping Phase 2: {message}")
            skipped_missing_count += 1
            continue

        result = upload_record(
            record=plan.record,
            raster_path=plan.raster_path,
            uploader=uploader,
            dry_run=run_control.dry_run,
            gcs_exists=plan.gcs_exists,
            ee_exists=plan.ee_exists,
        )
        uploaded_count += int(result.gcs_uploaded)
        gcs_existing_count += int(result.gcs_already_exists)
        ee_submitted_count += int(result.ee_submitted)
        ee_existing_count += int(result.ee_already_exists)
        dry_run_gcs_upload_count += int(result.dry_run_gcs_upload)
        dry_run_ee_upload_count += int(result.dry_run_ee_upload)

    log(f"Phase 2 selected dekads: {len(records)}")
    log(f"Phase 2 skipped existing Earth Engine asset(s): {skipped_ee_existing_count}")
    log(f"Phase 2 skipped quality-control excluded dekad(s): {skipped_quality_control_count}")
    log(f"Phase 2 skipped missing local/GCS source raster(s): {skipped_missing_count}")
    if run_control.dry_run:
        log(f"Phase 2 dry-run GCS upload(s) that would be performed: {dry_run_gcs_upload_count}")
        log(f"Phase 2 dry-run Earth Engine ingestion(s) that would be submitted: {dry_run_ee_upload_count}")
    else:
        log(f"Phase 2 GCS upload(s) performed: {uploaded_count}")
        log(f"Phase 2 Earth Engine ingestion(s) submitted: {ee_submitted_count}")
    log(f"Phase 2 GCS object(s) already present: {gcs_existing_count}")
    log(f"Phase 2 Earth Engine asset(s) already present: {ee_existing_count}")
    log("Phase 2 upload process completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
