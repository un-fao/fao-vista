"""
Shared constants for Phase 3
=============================

Calendar and classification constants used across all Phase 3 implementation
modules. No EE dependency — safe to import before ``ee.Initialize()``.
"""

from __future__ import annotations

#: All 36 canonical dekad keys in calendar order.
ALL_DEKADS: list[str] = [
    f"{m:02d}d{d}"
    for m in range(1, 13)
    for d in range(1, 4)
]

#: Day-of-year windows per dekad (calendar-average, non-leap basis).
DEKAD_DAY_RANGES: dict[str, dict] = {
    "01d1": {"day_start": "01-01", "day_end": "01-10", "doy_start":   1, "doy_end":  10},
    "01d2": {"day_start": "01-11", "day_end": "01-20", "doy_start":  11, "doy_end":  20},
    "01d3": {"day_start": "01-21", "day_end": "01-31", "doy_start":  21, "doy_end":  31},
    "02d1": {"day_start": "02-01", "day_end": "02-10", "doy_start":  32, "doy_end":  41},
    "02d2": {"day_start": "02-11", "day_end": "02-20", "doy_start":  42, "doy_end":  51},
    "02d3": {"day_start": "02-21", "day_end": "02-29", "doy_start":  52, "doy_end":  60},
    "03d1": {"day_start": "03-01", "day_end": "03-10", "doy_start":  60, "doy_end":  69},
    "03d2": {"day_start": "03-11", "day_end": "03-20", "doy_start":  70, "doy_end":  79},
    "03d3": {"day_start": "03-21", "day_end": "03-31", "doy_start":  80, "doy_end":  90},
    "04d1": {"day_start": "04-01", "day_end": "04-10", "doy_start":  91, "doy_end": 100},
    "04d2": {"day_start": "04-11", "day_end": "04-20", "doy_start": 101, "doy_end": 110},
    "04d3": {"day_start": "04-21", "day_end": "04-30", "doy_start": 111, "doy_end": 120},
    "05d1": {"day_start": "05-01", "day_end": "05-10", "doy_start": 121, "doy_end": 130},
    "05d2": {"day_start": "05-11", "day_end": "05-20", "doy_start": 131, "doy_end": 140},
    "05d3": {"day_start": "05-21", "day_end": "05-31", "doy_start": 141, "doy_end": 151},
    "06d1": {"day_start": "06-01", "day_end": "06-10", "doy_start": 152, "doy_end": 161},
    "06d2": {"day_start": "06-11", "day_end": "06-20", "doy_start": 162, "doy_end": 171},
    "06d3": {"day_start": "06-21", "day_end": "06-30", "doy_start": 172, "doy_end": 181},
    "07d1": {"day_start": "07-01", "day_end": "07-10", "doy_start": 182, "doy_end": 191},
    "07d2": {"day_start": "07-11", "day_end": "07-20", "doy_start": 192, "doy_end": 201},
    "07d3": {"day_start": "07-21", "day_end": "07-31", "doy_start": 202, "doy_end": 212},
    "08d1": {"day_start": "08-01", "day_end": "08-10", "doy_start": 213, "doy_end": 222},
    "08d2": {"day_start": "08-11", "day_end": "08-20", "doy_start": 223, "doy_end": 232},
    "08d3": {"day_start": "08-21", "day_end": "08-31", "doy_start": 233, "doy_end": 243},
    "09d1": {"day_start": "09-01", "day_end": "09-10", "doy_start": 244, "doy_end": 253},
    "09d2": {"day_start": "09-11", "day_end": "09-20", "doy_start": 254, "doy_end": 263},
    "09d3": {"day_start": "09-21", "day_end": "09-30", "doy_start": 264, "doy_end": 273},
    "10d1": {"day_start": "10-01", "day_end": "10-10", "doy_start": 274, "doy_end": 283},
    "10d2": {"day_start": "10-11", "day_end": "10-20", "doy_start": 284, "doy_end": 293},
    "10d3": {"day_start": "10-21", "day_end": "10-31", "doy_start": 294, "doy_end": 304},
    "11d1": {"day_start": "11-01", "day_end": "11-10", "doy_start": 305, "doy_end": 314},
    "11d2": {"day_start": "11-11", "day_end": "11-20", "doy_start": 315, "doy_end": 324},
    "11d3": {"day_start": "11-21", "day_end": "11-30", "doy_start": 325, "doy_end": 334},
    "12d1": {"day_start": "12-01", "day_end": "12-10", "doy_start": 335, "doy_end": 344},
    "12d2": {"day_start": "12-11", "day_end": "12-20", "doy_start": 345, "doy_end": 354},
    "12d3": {"day_start": "12-21", "day_end": "12-31", "doy_start": 355, "doy_end": 365},
}
