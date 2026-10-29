from pathlib import Path
from datetime import datetime

from core.phase1_local.process_dekad_composites import (
    build_tasks,
    format_dekad_label,
    format_elapsed_time,
    cleanup_global_intermediates_for_dekad,
    get_dekadal_output_path,
    initialize_run_log,
    load_calendar_records,
    load_config,
    log,
    log_phase1_plan,
    parse_args,
    run_classification_step,
    run_cleanup_step,
    run_dekadal_step,
    run_download_step,
    run_housekeeping_step,
    run_mosaic_step,
)


def log_phase1_resume_plan(config, records):
    existing_count = 0
    missing_count = 0
    for record in records:
        if get_dekadal_output_path(config, record).is_file():
            existing_count += 1
        else:
            missing_count += 1

    log("Phase 1 resume inventory:")
    log(f"  Selected dekads: {len(records)}")
    log(f"  Final local dekadal rasters already present: {existing_count}")
    log(f"  Final local dekadal rasters missing: {missing_count}")


def run_composite_task(config, task):
    # Per-composite operational flow:
    # 1. Download the NOAA source raster(s) for the composite date.
    # 2. Reclassify each tile into project classes.
    # 3. Mosaic the classified tiles when more than one tile is present.
    # 4. Remove temporary intermediate outputs for that task.
    download_result = run_download_step(config, task)
    if not download_result.remote_files:
        return None

    classified_paths = run_classification_step(download_result)
    output_raster_path = run_mosaic_step(download_result, classified_paths)
    run_cleanup_step(config, download_result, output_raster_path)
    if download_result.paths is None:
        return None
    return download_result.paths.classified_path


def run_dekad_workflow(config, record, dekad_tasks):
    started_at = datetime.now()
    log(f"Starting full workflow for {format_dekad_label(record)}")

    composite_outputs = {}
    for task in dekad_tasks:
        classified_path = run_composite_task(config, task)
        if classified_path is not None:
            composite_outputs[task.composite_date] = classified_path

    run_dekadal_step(config, [record], composite_outputs)

    elapsed = datetime.now() - started_at
    log(f"Completed full workflow for {format_dekad_label(record)} in {format_elapsed_time(elapsed.total_seconds())}")


def run_phase1() -> int:
    # Phase 1 orchestration:
    # 1. Parse runtime inputs.
    # 2. Load config and dekad records.
    # 3. Build composite tasks.
    # 4. For each task: download -> classify tiles -> mosaic classified tiles -> task cleanup.
    # 5. Aggregate the classified 5-day rasters into dekadal rasters.
    # 6. Run final housekeeping for old NOAA downloads.
    args = parse_args()
    config = load_config(Path(args.config))
    initialize_run_log(config.data_root, "phase1")
    records = load_calendar_records(config.calendar_csv, config.dekad_ids)
    tasks = build_tasks(records)

    log_phase1_plan(config, records, tasks)
    log_phase1_resume_plan(config, records)

    tasks_by_dekad_id = {}
    for task in tasks:
        tasks_by_dekad_id.setdefault(task.dekad_id, []).append(task)

    for record in records:
        raster_path = get_dekadal_output_path(config, record)
        if raster_path.is_file():
            log(
                f"Skipping Phase 1 for {format_dekad_label(record)}: "
                f"final local raster already exists at {raster_path}"
            )
            continue
        run_dekad_workflow(config, record, tasks_by_dekad_id.get(record.dekad_id, []))
        cleanup_global_intermediates_for_dekad(config, record)

    run_housekeeping_step(config)
    log("Phase 1 processing completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_phase1())
