from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

import ee
import yaml

from core.quality_control.exclusions import load_dekad_exclusions
from core.phase1_local.process_dekad_composites import (
    CONFIG_PATH,
    initialize_run_log_from_config,
    log,
    parse_bool_setting,
)
from core.phase3_gee.ee_utils import (
    asset_exists,
    gcs_prefix_exists,
    list_assets_for_folder,
    resolve_projection,
    submit_asset_export,
    submit_gcs_export,
)


SOURCE_ASSET_PATTERN = re.compile(
    r"^vista_dekad_(?P<year>\d{4})(?P<month>\d{2})d(?P<dekad>[1-3])(?P<suffix>.*)$",
    re.IGNORECASE,
)
CENTER_DEKAD_PATTERN = re.compile(r"^(?P<year>\d{4})(?P<month>\d{2})d(?P<dekad>[1-3])$")

CLASS_VALUES = [0, 9, 11, 10, 4, 5, 6, 1, 2, 3]
CLASS_RANKS = list(range(len(CLASS_VALUES)))
AGGREGATION_PRIORITY = "3 > 2 > 1 > 6 > 5 > 4 > 10 > 11 > 9 > 0"


@dataclass(frozen=True)
class Phase4Settings:
    source_project_id: str
    source_asset_root: str
    target_project_id: str
    target_asset_root: str
    target_bucket_name: str
    target_gcs_prefix: str
    dry_run: bool
    overwrite: bool
    export_gcs: bool
    min_required_dekads: int
    max_products_to_create: int | None
    center_dekad_mode: str
    center_dekad_single: str | None
    center_dekad_start: str | None
    center_dekad_end: str | None


@dataclass(frozen=True)
class DekadKey:
    year: int
    month: int
    dekad: int

    @property
    def label(self) -> str:
        return f"{self.year:04d}{self.month:02d}d{self.dekad}"

    @property
    def order(self) -> int:
        return self.year * 36 + (self.month - 1) * 3 + (self.dekad - 1)


@dataclass(frozen=True)
class SourceDekadAsset:
    key: DekadKey
    asset_id: str
    basename: str
    suffix: str

    @property
    def preference(self) -> int:
        if self.suffix == "":
            return 0
        if self.suffix.lower() == "_pipeb":
            return 1
        return 2


@dataclass(frozen=True)
class MonthlyWindow:
    center: DekadKey
    assets: tuple[SourceDekadAsset, ...]
    missing_labels: tuple[str, ...]
    excluded_labels: tuple[str, ...] = ()

    @property
    def output_stem(self) -> str:
        return f"vista_monthly_{self.center.label}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 4: build centered 3-dekad monthly VISTA archive assets from "
            "the production dekadal Earth Engine archive."
        )
    )
    parser.add_argument(
        "--config",
        default=str(CONFIG_PATH),
        help="Path to config/config.yaml.",
    )
    run_mode_group = parser.add_mutually_exclusive_group()
    run_mode_group.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="List planned Phase 4 exports without submitting Earth Engine tasks.",
    )
    run_mode_group.add_argument(
        "--execute",
        action="store_true",
        default=None,
        help="Submit Earth Engine tasks even when config dry_run is true.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        default=None,
        help="Overwrite existing GCS exports. Existing EE assets are still skipped.",
    )
    parser.add_argument(
        "--export-gcs",
        action="store_true",
        default=None,
        help="Export GeoTIFF tiles to GCS even when config export_gcs is false.",
    )
    parser.add_argument(
        "--center-dekads",
        nargs="+",
        default=None,
        metavar="YYYYMMdD",
        help="Optional center dekads to process, for example 201201d3 201202d1.",
    )
    parser.add_argument(
        "--max-products",
        type=int,
        default=None,
        help="Maximum number of eligible monthly products needing action to plan or submit.",
    )
    return parser.parse_args()


def _parse_optional_positive_int(value: object, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{field_name} must be empty or > 0")
    return parsed


def _join_remote_path(*parts: str) -> str:
    return "/".join(str(part).strip().strip("/") for part in parts if str(part).strip().strip("/"))


def _parse_optional_center_dekad(value: object, field_name: str) -> DekadKey | None:
    if value in (None, ""):
        return None
    return parse_center_dekad_label(str(value))


def load_phase4_settings(config_path: Path) -> Phase4Settings:
    with config_path.open("r", encoding="utf-8") as handle:
        raw_config = yaml.safe_load(handle) or {}

    p4 = raw_config.get("phase4_monthly_archive", {}) or {}
    source = p4.get("source", {}) or {}
    target = p4.get("target", {}) or {}
    target_gcs = target.get("gcs", {}) or {}
    run_control = p4.get("run_control", {}) or {}
    center_dekads = p4.get("center_dekads", {}) or {}

    source_project_id = str(source.get("project_id", "")).strip()
    source_asset_root = str(source.get("asset_root", "")).strip().rstrip("/")
    target_project_id = str(target.get("project_id", "")).strip()
    target_asset_root = str(target.get("asset_root", "")).strip().rstrip("/")
    target_bucket_name = str(target_gcs.get("bucket_name", "")).strip()
    target_gcs_prefix = str(target_gcs.get("prefix", "vista_monthly_archive")).strip().strip("/")
    dry_run = parse_bool_setting(
        run_control.get("dry_run", True),
        "phase4_monthly_archive.run_control.dry_run",
    )
    overwrite = parse_bool_setting(
        run_control.get("overwrite", False),
        "phase4_monthly_archive.run_control.overwrite",
    )
    export_gcs = parse_bool_setting(
        run_control.get("export_gcs", True),
        "phase4_monthly_archive.run_control.export_gcs",
    )
    min_required_dekads = int(run_control.get("min_required_dekads", 2))
    max_products_to_create = _parse_optional_positive_int(
        run_control.get("max_products_to_create"),
        "phase4_monthly_archive.run_control.max_products_to_create",
    )
    center_dekad_mode = str(center_dekads.get("mode", "all")).strip().lower()
    if center_dekad_mode not in {"all", "single", "range"}:
        raise ValueError(
            "phase4_monthly_archive.center_dekads.mode must be one of: all, single, range"
        )
    center_dekad_single = _parse_optional_center_dekad(
        center_dekads.get("single"),
        "phase4_monthly_archive.center_dekads.single",
    )
    center_dekad_start = _parse_optional_center_dekad(
        center_dekads.get("start"),
        "phase4_monthly_archive.center_dekads.start",
    )
    center_dekad_end = _parse_optional_center_dekad(
        center_dekads.get("end"),
        "phase4_monthly_archive.center_dekads.end",
    )

    if not source_project_id:
        raise ValueError("config/config.yaml is missing phase4_monthly_archive.source.project_id")
    if not source_asset_root:
        raise ValueError("config/config.yaml is missing phase4_monthly_archive.source.asset_root")
    if not target_project_id:
        raise ValueError("config/config.yaml is missing phase4_monthly_archive.target.project_id")
    if not target_asset_root:
        raise ValueError("config/config.yaml is missing phase4_monthly_archive.target.asset_root")
    if export_gcs and not target_bucket_name:
        raise ValueError("config/config.yaml is missing phase4_monthly_archive.target.gcs.bucket_name")
    if min_required_dekads < 2 or min_required_dekads > 3:
        raise ValueError("phase4_monthly_archive.run_control.min_required_dekads must be 2 or 3")
    if center_dekad_mode == "single" and center_dekad_single is None:
        raise ValueError("phase4_monthly_archive.center_dekads.single is required when mode is single")
    if (
        center_dekad_mode == "range"
        and center_dekad_start is not None
        and center_dekad_end is not None
        and center_dekad_start.order > center_dekad_end.order
    ):
        raise ValueError("phase4_monthly_archive.center_dekads.start must be <= end")

    return Phase4Settings(
        source_project_id=source_project_id,
        source_asset_root=source_asset_root,
        target_project_id=target_project_id,
        target_asset_root=target_asset_root,
        target_bucket_name=target_bucket_name,
        target_gcs_prefix=target_gcs_prefix,
        dry_run=dry_run,
        overwrite=overwrite,
        export_gcs=export_gcs,
        min_required_dekads=min_required_dekads,
        max_products_to_create=max_products_to_create,
        center_dekad_mode=center_dekad_mode,
        center_dekad_single=center_dekad_single.label if center_dekad_single is not None else None,
        center_dekad_start=center_dekad_start.label if center_dekad_start is not None else None,
        center_dekad_end=center_dekad_end.label if center_dekad_end is not None else None,
    )


def apply_cli_overrides(settings: Phase4Settings, args: argparse.Namespace) -> Phase4Settings:
    if args.execute:
        dry_run = False
    elif args.dry_run:
        dry_run = True
    else:
        dry_run = settings.dry_run
    overwrite = settings.overwrite if args.overwrite is None else args.overwrite
    export_gcs = settings.export_gcs if args.export_gcs is None else args.export_gcs
    max_products = settings.max_products_to_create if args.max_products is None else args.max_products
    if max_products is not None and max_products <= 0:
        raise ValueError("--max-products must be > 0")
    return Phase4Settings(
        source_project_id=settings.source_project_id,
        source_asset_root=settings.source_asset_root,
        target_project_id=settings.target_project_id,
        target_asset_root=settings.target_asset_root,
        target_bucket_name=settings.target_bucket_name,
        target_gcs_prefix=settings.target_gcs_prefix,
        dry_run=dry_run,
        overwrite=overwrite,
        export_gcs=export_gcs,
        min_required_dekads=settings.min_required_dekads,
        max_products_to_create=max_products,
        center_dekad_mode=settings.center_dekad_mode,
        center_dekad_single=settings.center_dekad_single,
        center_dekad_start=settings.center_dekad_start,
        center_dekad_end=settings.center_dekad_end,
    )


def parse_center_dekad_label(value: str) -> DekadKey:
    match = CENTER_DEKAD_PATTERN.match(value.strip())
    if not match:
        raise ValueError(f"Invalid center dekad {value!r}; expected YYYYMMdD, for example 201201d3")
    month = int(match.group("month"))
    dekad = int(match.group("dekad"))
    if month < 1 or month > 12:
        raise ValueError(f"Invalid center dekad month in {value!r}")
    return DekadKey(
        year=int(match.group("year")),
        month=month,
        dekad=dekad,
    )


def parse_source_dekad_asset(asset: dict) -> SourceDekadAsset | None:
    asset_id = asset.get("id") or asset.get("name") or ""
    basename = asset_id.split("/")[-1]
    match = SOURCE_ASSET_PATTERN.match(basename)
    if not match:
        return None

    month = int(match.group("month"))
    if month < 1 or month > 12:
        return None

    return SourceDekadAsset(
        key=DekadKey(
            year=int(match.group("year")),
            month=month,
            dekad=int(match.group("dekad")),
        ),
        asset_id=asset_id,
        basename=basename,
        suffix=match.group("suffix") or "",
    )


def build_source_index(
    all_assets: list[dict],
    excluded_labels: set[str] | frozenset[str] = frozenset(),
) -> dict[str, SourceDekadAsset]:
    candidates: dict[str, list[SourceDekadAsset]] = {}
    ignored_count = 0
    for asset in all_assets:
        parsed = parse_source_dekad_asset(asset)
        if parsed is None:
            ignored_count += 1
            continue
        if parsed.key.label in excluded_labels:
            log(f"[{parsed.key.label}] Excluding rejected source asset: {parsed.basename}")
            continue
        candidates.setdefault(parsed.key.label, []).append(parsed)

    selected: dict[str, SourceDekadAsset] = {}
    duplicate_count = 0
    for label, assets in candidates.items():
        sorted_assets = sorted(assets, key=lambda item: (item.preference, item.basename))
        selected[label] = sorted_assets[0]
        if len(sorted_assets) > 1:
            duplicate_count += len(sorted_assets) - 1
            kept = sorted_assets[0].basename
            skipped = ", ".join(item.basename for item in sorted_assets[1:])
            log(f"[{label}] Multiple source assets found; using {kept}; ignoring {skipped}")

    if ignored_count:
        log(f"Ignored {ignored_count} source asset(s) with names that do not match vista_dekad_YYYYMMdD*")
    if duplicate_count:
        log(f"Ignored {duplicate_count} duplicate source asset variant(s)")
    return selected


def previous_dekad(key: DekadKey) -> DekadKey:
    if key.dekad > 1:
        return DekadKey(key.year, key.month, key.dekad - 1)
    if key.month > 1:
        return DekadKey(key.year, key.month - 1, 3)
    return DekadKey(key.year - 1, 12, 3)


def next_dekad(key: DekadKey) -> DekadKey:
    if key.dekad < 3:
        return DekadKey(key.year, key.month, key.dekad + 1)
    if key.month < 12:
        return DekadKey(key.year, key.month + 1, 1)
    return DekadKey(key.year + 1, 1, 1)


def build_monthly_windows(
    source_index: dict[str, SourceDekadAsset],
    excluded_labels: set[str] | frozenset[str] = frozenset(),
) -> list[MonthlyWindow]:
    windows: list[MonthlyWindow] = []
    centers_by_label = {asset.key.label: asset.key for asset in source_index.values()}
    for label in excluded_labels:
        centers_by_label[label] = parse_center_dekad_label(label)
    centers = sorted(centers_by_label.values(), key=lambda item: item.order)
    for center in centers:
        source_labels = [previous_dekad(center).label, center.label, next_dekad(center).label]
        assets = tuple(source_index[label] for label in source_labels if label in source_index)
        excluded = tuple(label for label in source_labels if label in excluded_labels)
        missing = tuple(
            label
            for label in source_labels
            if label not in source_index and label not in excluded_labels
        )
        windows.append(
            MonthlyWindow(
                center=center,
                assets=assets,
                missing_labels=missing,
                excluded_labels=excluded,
            )
        )
    return windows


def filter_windows_from_config(windows: list[MonthlyWindow], settings: Phase4Settings) -> list[MonthlyWindow]:
    if settings.center_dekad_mode == "all":
        return windows
    if settings.center_dekad_mode == "single":
        return [
            window for window in windows
            if window.center.label == settings.center_dekad_single
        ]

    start = (
        parse_center_dekad_label(settings.center_dekad_start)
        if settings.center_dekad_start is not None
        else None
    )
    end = (
        parse_center_dekad_label(settings.center_dekad_end)
        if settings.center_dekad_end is not None
        else None
    )
    return [
        window for window in windows
        if (start is None or window.center.order >= start.order)
        and (end is None or window.center.order <= end.order)
    ]


def describe_config_center_dekad_filter(settings: Phase4Settings) -> str:
    if settings.center_dekad_mode == "all":
        return "all"
    if settings.center_dekad_mode == "single":
        return settings.center_dekad_single or "single unset"
    start = settings.center_dekad_start or "first available"
    end = settings.center_dekad_end or "last available"
    return f"{start} through {end}"


def build_monthly_image(asset_ids: list[str]) -> ee.Image:
    ranked_images = [
        ee.Image(asset_id).select(0).remap(CLASS_VALUES, CLASS_RANKS, 0).toUint8()
        for asset_id in asset_ids
    ]
    max_rank = ee.ImageCollection(ranked_images).max().toUint8()
    return max_rank.remap(CLASS_RANKS, CLASS_VALUES, 0).toUint8().rename("monthly_class")


def annotate_image(image: ee.Image, window: MonthlyWindow, settings: Phase4Settings) -> ee.Image:
    source_labels = ",".join(asset.key.label for asset in window.assets)
    source_assets = ",".join(asset.asset_id for asset in window.assets)
    missing_labels = ",".join(window.missing_labels)
    excluded_labels = ",".join(window.excluded_labels)
    return image.set({
        "phase": 4,
        "product": "vista_monthly_archive",
        "source_project_id": settings.source_project_id,
        "source_archive": settings.source_asset_root,
        "center_dekad": window.center.label,
        "center_year": window.center.year,
        "center_month": window.center.month,
        "center_dekad_number": window.center.dekad,
        "source_dekad_count": len(window.assets),
        "source_dekads": source_labels,
        "source_assets": source_assets,
        "missing_neighbor_dekads": missing_labels,
        "excluded_source_dekads": excluded_labels,
        "quality_control_exclusion_count": len(window.excluded_labels),
        "aggregation_rule": "priority_max",
        "aggregation_priority": AGGREGATION_PRIORITY,
        "system:description": (
            "VISTA centered 3-dekad monthly archive product. "
            f"Center dekad {window.center.label}; source dekads {source_labels}."
        ),
    })


def output_asset_id(settings: Phase4Settings, window: MonthlyWindow) -> str:
    return _join_remote_path(settings.target_asset_root, window.output_stem)


def output_gcs_prefix(settings: Phase4Settings, window: MonthlyWindow) -> str:
    return _join_remote_path(settings.target_gcs_prefix, window.output_stem)


def process_window(window: MonthlyWindow, settings: Phase4Settings) -> bool:
    asset_id = output_asset_id(settings, window)
    gcs_prefix = output_gcs_prefix(settings, window)
    existing_asset = asset_exists(asset_id)
    existing_gcs_files = (
        gcs_prefix_exists(settings.target_bucket_name, gcs_prefix, project=settings.target_project_id)
        if settings.export_gcs
        else []
    )

    if existing_asset:
        log(f"[{window.center.label}] Target asset already exists; skipping asset export.")
        log(f"       To regenerate it, delete first: earthengine rm {asset_id}")

    gcs_done = (not settings.export_gcs) or (bool(existing_gcs_files) and not settings.overwrite)
    if existing_gcs_files and not settings.overwrite:
        log(
            f"[{window.center.label}] GCS export already exists "
            f"({len(existing_gcs_files)} file(s)); skipping GCS export."
        )

    needs_asset = not existing_asset
    needs_gcs = not gcs_done
    if not needs_asset and not needs_gcs:
        return False

    source_asset_ids = [asset.asset_id for asset in window.assets]
    source_labels = ", ".join(asset.key.label for asset in window.assets)
    if window.missing_labels or window.excluded_labels:
        unavailable = []
        if window.missing_labels:
            unavailable.append(f"missing: {', '.join(window.missing_labels)}")
        if window.excluded_labels:
            unavailable.append(f"quality-control excluded: {', '.join(window.excluded_labels)}")
        log(
            f"[{window.center.label}] Building from {len(window.assets)} source dekads "
            f"({source_labels}); {'; '.join(unavailable)}"
        )
    else:
        log(f"[{window.center.label}] Building from 3 source dekads ({source_labels})")

    reference_asset = next(
        (asset for asset in window.assets if asset.key.label == window.center.label),
        window.assets[0],
    )
    reference_image = ee.Image(reference_asset.asset_id)
    crs, crs_transform = resolve_projection(reference_image)
    image = annotate_image(build_monthly_image(source_asset_ids), window, settings)

    if needs_asset:
        submit_asset_export(
            image,
            asset_id,
            crs,
            crs_transform,
            settings.dry_run,
            description=f"monthly_{window.center.label}",
            pyramiding_policy="mode",
        )

    if needs_gcs:
        submit_gcs_export(
            image,
            project=settings.target_project_id,
            bucket=settings.target_bucket_name,
            file_name_prefix=gcs_prefix,
            crs=crs,
            crs_transform=crs_transform,
            dry_run=settings.dry_run,
            overwrite=settings.overwrite,
            description=f"gcs_monthly_{window.center.label}",
        )

    return True


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()
    initialize_run_log_from_config(config_path, "phase4")

    try:
        settings = apply_cli_overrides(load_phase4_settings(config_path), args)
        selected_centers = (
            {parse_center_dekad_label(value).label for value in args.center_dekads}
            if args.center_dekads is not None
            else None
        )
    except ValueError as exc:
        log(f"ERROR: {exc}")
        return 1

    log("=" * 60)
    log("PHASE 4 - VISTA MONTHLY ARCHIVE (GEE Python API)")
    log("=" * 60)
    log(f"Source project         : {settings.source_project_id}")
    log(f"Source dekadal archive : {settings.source_asset_root}")
    log(f"Target project         : {settings.target_project_id}")
    log(f"Target monthly archive : {settings.target_asset_root}")
    if settings.export_gcs:
        log(f"GCS output             : gs://{settings.target_bucket_name}/{settings.target_gcs_prefix}/")
    else:
        log("GCS output             : disabled")
    log(f"Min required dekads    : {settings.min_required_dekads}")
    log(f"Aggregation priority   : {AGGREGATION_PRIORITY}")
    log(f"Dry run                : {settings.dry_run}")
    log(f"Export GCS             : {settings.export_gcs}")
    log(f"Overwrite GCS          : {settings.overwrite}")
    log(f"Max products           : {settings.max_products_to_create or 'all'}")
    log(f"Config center filter   : {describe_config_center_dekad_filter(settings)}")
    if selected_centers is not None:
        log(f"Center dekad filter    : {', '.join(sorted(selected_centers))}")
    log("")

    try:
        ee.Initialize(project=settings.target_project_id)
    except ee.EEException as exc:
        log(f"ERROR: Earth Engine initialisation failed: {exc}")
        log("       Run `earthengine authenticate` or set GOOGLE_APPLICATION_CREDENTIALS.")
        return 1

    log(f"Listing source assets under: {settings.source_asset_root}")
    try:
        source_assets = list_assets_for_folder(settings.source_asset_root)
    except ee.EEException as exc:
        log(f"ERROR listing source archive assets: {exc}")
        return 1
    log(f"Total source assets found: {len(source_assets)}")

    quality_control_exclusions = load_dekad_exclusions()
    excluded_labels = set(quality_control_exclusions)
    source_index = build_source_index(source_assets, excluded_labels=excluded_labels)
    windows = build_monthly_windows(source_index, excluded_labels=excluded_labels)
    if selected_centers is not None:
        windows = [window for window in windows if window.center.label in selected_centers]
    else:
        windows = filter_windows_from_config(windows, settings)

    eligible_windows = [
        window for window in windows
        if len(window.assets) >= settings.min_required_dekads
    ]
    one_dekad_windows = len(windows) - len(eligible_windows)
    log(f"Parsed source dekads   : {len(source_index)}")
    log(
        "QC excluded dekads    : "
        + (", ".join(sorted(excluded_labels)) or "none")
    )
    log(f"Candidate centers      : {len(windows)}")
    log(f"Eligible windows       : {len(eligible_windows)}")
    log(f"Skipped one-dekad windows before output checks: {one_dekad_windows}")
    log("")

    if selected_centers is not None:
        available_labels = {window.center.label for window in windows}
        missing_filter_labels = selected_centers - available_labels
        for label in sorted(missing_filter_labels):
            log(f"[{label}] Requested center dekad was not found in the source archive.")

    errors: list[str] = []
    action_count = 0
    skipped_existing_or_no_action = 0

    for window in eligible_windows:
        if (
            settings.max_products_to_create is not None
            and action_count >= settings.max_products_to_create
        ):
            log(f"Reached max product limit ({settings.max_products_to_create}); stopping.")
            break
        try:
            did_action = process_window(window, settings)
            if did_action:
                action_count += 1
            else:
                skipped_existing_or_no_action += 1
        except Exception as exc:  # noqa: BLE001
            msg = f"[{window.center.label}] FAILED: {exc}"
            log(msg)
            errors.append(msg)
        log("")

    if errors:
        log(f"Phase 4 completed with {len(errors)} error(s):")
        for error in errors:
            log(f"  {error}")
        return 1

    action_label = "planned" if settings.dry_run else "submitted or verified"
    log(f"Phase 4 complete - {action_count} monthly product(s) {action_label}.")
    log(f"Skipped with no required action: {skipped_existing_or_no_action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
