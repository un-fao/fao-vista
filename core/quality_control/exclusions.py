"""Load and apply version-controlled exclusions of rejected dekadal observations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_QUALITY_CONTROL_PATH = PROJECT_ROOT / "config" / "quality_control.yaml"
DEKAD_LABEL_PATTERN = re.compile(
    r"^(?P<year>\d{4})(?P<month>\d{2})d(?P<dekad>[1-3])$",
    re.IGNORECASE,
)
ARCHIVE_BASENAME_PATTERN = re.compile(
    r"^vista_dekad_(?P<label>\d{4}\d{2}d[1-3])(?:$|_)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DekadExclusion:
    label: str
    status: str
    reason: str
    provider_source_period: str
    provider_folder: str


def normalise_dekad_label(value: str) -> str:
    match = DEKAD_LABEL_PATTERN.fullmatch(str(value).strip())
    if not match:
        raise ValueError(f"Invalid excluded dekad label {value!r}; expected YYYYMMdD")
    month = int(match.group("month"))
    if not 1 <= month <= 12:
        raise ValueError(f"Invalid excluded dekad month in {value!r}")
    return f"{int(match.group('year')):04d}{month:02d}d{int(match.group('dekad'))}"


def load_dekad_exclusions(
    path: Path = DEFAULT_QUALITY_CONTROL_PATH,
) -> dict[str, DekadExclusion]:
    with Path(path).open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    entries = raw.get("excluded_dekads", [])
    if not isinstance(entries, list):
        raise ValueError("quality_control.yaml excluded_dekads must be a list")

    exclusions: dict[str, DekadExclusion] = {}
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Excluded dekad entry {index} must be a mapping")
        label = normalise_dekad_label(entry.get("label", ""))
        if label in exclusions:
            raise ValueError(f"Duplicate excluded dekad label: {label}")
        status = str(entry.get("status", "")).strip().lower()
        reason = str(entry.get("reason", "")).strip()
        if status != "rejected":
            raise ValueError(f"Excluded dekad {label} must have status: rejected")
        if not reason:
            raise ValueError(f"Excluded dekad {label} is missing a reason")
        exclusions[label] = DekadExclusion(
            label=label,
            status=status,
            reason=reason,
            provider_source_period=str(entry.get("provider_source_period", "")).strip(),
            provider_folder=str(entry.get("provider_folder", "")).strip(),
        )
    return exclusions


def archive_basename_dekad_label(basename: str) -> str | None:
    match = ARCHIVE_BASENAME_PATTERN.match(basename)
    if not match:
        return None
    return normalise_dekad_label(match.group("label"))


def record_dekad_label(record: object) -> str:
    return f"{int(record.year):04d}{int(record.month):02d}d{int(record.dekad)}"


def _label_parts(label: str) -> tuple[int, int, int]:
    clean = normalise_dekad_label(label)
    return int(clean[:4]), int(clean[4:6]), int(clean[-1])


def _shift_dekad(label: str, offset: int) -> str:
    year, month, dekad = _label_parts(label)
    order = year * 36 + (month - 1) * 3 + (dekad - 1) + offset
    shifted_year, remainder = divmod(order, 36)
    shifted_month, shifted_dekad_zero = divmod(remainder, 3)
    return f"{shifted_year:04d}{shifted_month + 1:02d}d{shifted_dekad_zero + 1}"


def affected_monthly_center_labels(excluded_label: str) -> tuple[str, str, str]:
    """Return previous, same, and next centers whose windows use the label."""
    clean = normalise_dekad_label(excluded_label)
    return _shift_dekad(clean, -1), clean, _shift_dekad(clean, 1)


def exclusions_for_monthly_center(
    center_label: str,
    exclusions: dict[str, DekadExclusion],
) -> tuple[DekadExclusion, ...]:
    clean_center = normalise_dekad_label(center_label)
    return tuple(
        exclusion
        for exclusion in exclusions.values()
        if clean_center in affected_monthly_center_labels(exclusion.label)
    )


def exclusions_affecting_monthly_statistic(
    center_dekad: str,
    years: set[int] | frozenset[int],
    exclusions: dict[str, DekadExclusion],
) -> tuple[DekadExclusion, ...]:
    """Return raw-dekad exclusions represented in a monthly statistic."""
    clean_center = str(center_dekad).strip().lower()
    affected: list[DekadExclusion] = []
    for exclusion in exclusions.values():
        if int(exclusion.label[:4]) not in years:
            continue
        center_keys = {label[4:].lower() for label in affected_monthly_center_labels(exclusion.label)}
        if clean_center in center_keys:
            affected.append(exclusion)
    return tuple(sorted(affected, key=lambda item: item.label))
