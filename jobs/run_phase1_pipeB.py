from datetime import datetime
from pathlib import Path

from core.phase1_local.process_dekad_composites import (
    build_tasks,
    format_elapsed_time,
    initialize_run_log,
    load_calendar_records,
    log,
)
from core.phase1_pipeB.process_dekad_composites import (
    filter_records_for_pipeB,
    get_pipeB_dekadal_output_path,
    load_pipeB_config,
    log_pipeB_plan,
    log_pipeB_resume_plan,
    parse_args,
    run_pipeB_composite_task,
    run_pipeB_dekadal_step,
)


def run_pipeB_dekad_workflow(config, record, dekad_tasks):
    started_at = datetime.now()
    log(f"Starting Pipeline B workflow for {record.dekad_id} ({record.year}-{record.month:02d} dekad {record.dekad})")

    composite_outputs = {}
    for task in dekad_tasks:
        classified_path = run_pipeB_composite_task(config, task)
        if classified_path is not None:
            composite_outputs[task.composite_date] = classified_path

    run_pipeB_dekadal_step(config, [record], composite_outputs)

    elapsed = datetime.now() - started_at
    log(
        "Completed Pipeline B workflow for "
        f"{record.dekad_id} ({record.year}-{record.month:02d} dekad {record.dekad}) "
        f"in {format_elapsed_time(elapsed.total_seconds())}"
    )


def run_phase1_pipeB() -> int:
    args = parse_args()
    config = load_pipeB_config(Path(args.config))
    initialize_run_log(config.phase1.data_root, "phase1_pipeB")

    configured_records = load_calendar_records(config.phase1.calendar_csv, config.phase1.dekad_ids)
    records = filter_records_for_pipeB(configured_records, config, allow_force_pipeline_b=True)
    tasks = build_tasks(records)

    log_pipeB_plan(config, records, len(tasks))
    log_pipeB_resume_plan(config, records)

    tasks_by_dekad_id = {}
    for task in tasks:
        tasks_by_dekad_id.setdefault(task.dekad_id, []).append(task)

    for record in records:
        raster_path = get_pipeB_dekadal_output_path(config, record)
        if raster_path.is_file():
            log(
                f"Skipping Pipeline B Phase 1 for {record.dekad_id} "
                f"({record.year}-{record.month:02d} dekad {record.dekad}): "
                f"final local raster already exists at {raster_path}"
            )
            continue
        run_pipeB_dekad_workflow(config, record, tasks_by_dekad_id.get(record.dekad_id, []))

    log("Pipeline B Phase 1 processing completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_phase1_pipeB())
