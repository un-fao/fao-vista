"""Helpers for parsing Phase 4 monthly archive assets."""

from __future__ import annotations

import re
from dataclasses import dataclass


MONTHLY_ASSET_PATTERN = re.compile(
    r"^vista_monthly_(?P<year>\d{4})(?P<month>\d{2})d(?P<dekad>[1-3])(?P<suffix>.*)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MonthlySourceAsset:
    year: int
    center_dekad: str
    asset_id: str
    basename: str
    suffix: str


def parse_monthly_source_asset(asset: dict) -> MonthlySourceAsset | None:
    asset_id = asset.get("id") or asset.get("name") or ""
    basename = asset_id.split("/")[-1]
    match = MONTHLY_ASSET_PATTERN.match(basename)
    if not match:
        return None

    month = int(match.group("month"))
    if month < 1 or month > 12:
        return None

    center_dekad = f"{month:02d}d{int(match.group('dekad'))}"
    return MonthlySourceAsset(
        year=int(match.group("year")),
        center_dekad=center_dekad,
        asset_id=asset_id,
        basename=basename,
        suffix=match.group("suffix") or "",
    )


def filter_monthly_assets_for_center(
    all_assets: list[dict],
    center_dekad: str,
    year_start: int,
    year_end: int,
) -> list[MonthlySourceAsset]:
    """Return matching Phase 4 monthly assets, deduplicated by year."""
    matched: dict[int, MonthlySourceAsset] = {}
    for asset in all_assets:
        parsed = parse_monthly_source_asset(asset)
        if parsed is None:
            continue
        if parsed.center_dekad != center_dekad:
            continue
        if parsed.year < year_start or parsed.year > year_end:
            continue
        if parsed.year not in matched:
            matched[parsed.year] = parsed
    return [matched[year] for year in sorted(matched)]

