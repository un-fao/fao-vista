# Changelog

## Unreleased

- First publication of the VISTA pipeline code: Phase 1 (NOAA 5-day composites
  and the Pipeline B recovery branch), Phases 2 to 5, the dekadal calendar, the
  conda environment and the quality-control tests.
- `config/config.example.yaml` documents every setting; copy it to
  `config/config.yaml` before running.
- Phase 3 now initialises Earth Engine with `phase3_gee.gcp.project_id`
  instead of the default project of the user's credentials. Asset and bucket
  paths already came from the configuration, so outputs do not change.
- New tests for the dekadal class-priority rule. The README now states that
  rule and the order in which the phases must run.
