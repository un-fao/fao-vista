import argparse
from pathlib import Path

from core.phase1_local.process_dekad_composites import (
    CONFIG_PATH,
    build_tasks,
    cleanup_global_intermediates_for_dekad,
    initialize_run_log,
    load_calendar_records,
    load_config,
    log,
    log_phase1_plan,
    run_housekeeping_step,
)
from core.phase2_gee.upload_dekadal_rasters import (
    build_upload_plan,
    build_phase2_uploader,
    build_phase2_run_control,
    log_phase2_plan,
    log_phase2_run_control,
    log_upload_plan_summary,
    refresh_upload_plan_local_state,
    upload_record,
)
from jobs.run_phase1 import run_dekad_workflow


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Phase 1 and Phase 2 pipeline dekad by dekad: local processing followed by upload."
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
        help="Fail if a selected dekad has neither a final local raster nor the expected GCS object after Phase 1.",
    )
    missing_group.add_argument(
        "--skip-missing",
        action="store_true",
        default=None,
        help="Skip selected dekads with neither a final local raster nor the expected GCS object after Phase 1 and continue.",
    )
    return parser.parse_args()


def run_phase1_and_2() -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()
    phase1_config = load_config(config_path)
    initialize_run_log(phase1_config.data_root, "phase1_and_2")
    records = load_calendar_records(phase1_config.calendar_csv, phase1_config.dekad_ids)
    tasks = build_tasks(records)
    tasks_by_dekad_id = {}
    for task in tasks:
        tasks_by_dekad_id.setdefault(task.dekad_id, []).append(task)

    run_control = build_phase2_run_control(config_path, args)
    uploader = build_phase2_uploader(config_path, args.ee_upload_sleep_seconds)

    log("Starting Phase 1 and 2 pipeline: process one dekad, submit upload, continue with next dekad")
    log_phase1_plan(phase1_config, records, tasks)
    log_phase2_plan(uploader)
    log_phase2_run_control(run_control)
    plans = build_upload_plan(records, phase1_config, uploader)
    log_upload_plan_summary(plans)
    plans_by_dekad_id = {plan.record.dekad_id: plan for plan in plans}

    uploaded_count = 0
    skipped_phase2_count = 0
    skipped_ee_existing_count = 0
    skipped_phase1_count = 0

    for record in records:
        plan = plans_by_dekad_id[record.dekad_id]
        if plan.exclusion is not None:
            log(
                f"Skipping Phase 1 and Phase 2 for quality-control excluded dekad "
                f"{plan.exclusion.label}: {plan.exclusion.reason}"
            )
            skipped_phase2_count += 1
            skipped_phase1_count += 1
            continue
        if plan.ee_exists:
            log(
                f"Skipping Phase 1 and Phase 2 for {record.dekad_id} "
                f"({record.year}-{record.month:02d} dekad {record.dekad}): "
                f"Earth Engine asset already exists at {plan.ee_asset_id}"
            )
            skipped_ee_existing_count += 1
            skipped_phase1_count += 1
            continue

        if plan.local_exists:
            log(
                f"Skipping Phase 1 for {record.dekad_id} "
                f"({record.year}-{record.month:02d} dekad {record.dekad}): "
                f"final local raster already exists at {plan.raster_path}"
            )
            skipped_phase1_count += 1
        else:
            run_dekad_workflow(phase1_config, record, tasks_by_dekad_id.get(record.dekad_id, []))

        plan = refresh_upload_plan_local_state(plan)
        if not plan.local_exists and not plan.gcs_exists:
            message = (
                f"{record.dekad_id} ({record.year}-{record.month:02d} dekad {record.dekad}): "
                f"final dekadal raster was not produced locally and is not present in GCS at "
                f"{plan.raster_path}; {plan.gs_uri}"
            )
            if run_control.strict_missing_rasters:
                raise FileNotFoundError(message)
            log(f"Skipping Phase 2 for {message}")
            skipped_phase2_count += 1
            continue

        upload_record(
            record=record,
            raster_path=plan.raster_path,
            uploader=uploader,
            dry_run=run_control.dry_run,
            gcs_exists=plan.gcs_exists,
            ee_exists=plan.ee_exists,
        )
        uploaded_count += 1
        cleanup_global_intermediates_for_dekad(phase1_config, record)

    run_housekeeping_step(phase1_config)
    log(f"Phase 1 skipped for {skipped_phase1_count} dekad(s) already complete locally or in Earth Engine")
    log(f"Phase 2 skipped for {skipped_ee_existing_count} dekad(s) already present in Earth Engine")
    if run_control.dry_run:
        log(f"Phase 2 dry-run checked {uploaded_count} dekad(s) with local or GCS source rasters")
    else:
        log(f"Phase 2 submitted or verified for {uploaded_count} dekad(s)")
    log(f"Phase 2 skipped for {skipped_phase2_count} dekad(s) with no local/GCS source raster")
    log("Phase 1 and 2 pipeline completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_phase1_and_2())
