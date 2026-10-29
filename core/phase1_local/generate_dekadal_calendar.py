import calendar
import csv
import json
from pathlib import Path
from typing import List, Dict, Any, Union


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "data" / "calendars"


def get_dekad_records_for_year(year: int) -> List[Dict[str, Any]]:
    """
    Build a flat list of dekad records for a given year.

    Each record contains:
      - dekad_id
      - year
      - month
      - month_name
      - dekad (1, 2, or 3)
      - period_start (YYYYMMDD)
      - period_end   (YYYYMMDD)
      - composite_1, composite_2, composite_3 (YYYYMMDD or "")
      - n_composites
    """

    records: List[Dict[str, Any]] = []

    for month in range(1, 13):
        month_name = calendar.month_name[month]
        last_day = calendar.monthrange(year, month)[1]

        def dstr(day: int) -> str:
            return f"{year}{month:02d}{day:02d}"

        # Dekad 1: 1–10
        period1_start = dstr(1)
        period1_end = dstr(10)
        dek1_comps = [dstr(5), dstr(10)]

        # Dekad 2: 11–20
        period2_start = dstr(11)
        period2_end = dstr(20)
        dek2_comps = [dstr(15), dstr(20)]

        # Dekad 3: 21–end
        period3_start = dstr(21)
        period3_end = dstr(last_day)

        # Composite selection for dekad 3 depends on month length
        if last_day == 28:
            dek3_days = [25, 28]           # 8 days, 2 composites
        elif last_day == 29:
            dek3_days = [25, 29]           # 9 days, 2 composites
        elif last_day == 30:
            dek3_days = [25, 30]           # 10 days, 2 composites
        elif last_day == 31:
            dek3_days = [25, 30, 31]       # 11 days, 3 composites
        else:
            raise ValueError(f"Unexpected month length {last_day} for {year}-{month:02d}")

        dek3_comps = [dstr(d) for d in dek3_days]

        # Helper to build one record
        def make_record(
            dekad_index: int,
            period_start: str,
            period_end: str,
            composites: List[str],
        ) -> Dict[str, Any]:
            # Always expose up to three composite fields
            c1 = composites[0] if len(composites) > 0 else ""
            c2 = composites[1] if len(composites) > 1 else ""
            c3 = composites[2] if len(composites) > 2 else ""
            return {
                "year": year,
                "month": month,
                "month_name": month_name,
                "dekad": dekad_index,
                "period_start": period_start,
                "period_end": period_end,
                "composite_1": c1,
                "composite_2": c2,
                "composite_3": c3,
                "n_composites": len(composites),
            }

        # Append three dekads for this month
        records.append(make_record(1, period1_start, period1_end, dek1_comps))
        records.append(make_record(2, period2_start, period2_end, dek2_comps))
        records.append(make_record(3, period3_start, period3_end, dek3_comps))

    return records


def get_dekad_records_for_years(years: Union[int, List[int]]) -> List[Dict[str, Any]]:
    """
    Accept a single year (int) or a list of years.
    Return a combined list of dekad records.
    """
    if isinstance(years, int):
        years = [years]

    all_records: List[Dict[str, Any]] = []
    for y in years:
        all_records.extend(get_dekad_records_for_year(y))

    for index, record in enumerate(all_records, start=1):
        record["dekad_id"] = index

    return all_records


def print_dekad_calendar(records: List[Dict[str, Any]]) -> None:
    """
    Print a human readable dekadal calendar from the records list.
    """
    if not records:
        return

    current_year = None
    current_month = None

    for rec in records:
        year = rec["year"]
        month = rec["month"]
        month_name = rec["month_name"]

        if year != current_year:
            current_year = year
            print(f"\n==================== YEAR {year} ====================\n")

        if month != current_month:
            current_month = month
            print(f"--- {month_name} {year} ---")

        dekad = rec["dekad"]
        ps = rec["period_start"]
        pe = rec["period_end"]
        c1 = rec["composite_1"]
        c2 = rec["composite_2"]
        c3 = rec["composite_3"]

        # Format period as YYYY-MM-DD for readability
        ps_iso = f"{ps[0:4]}-{ps[4:6]}-{ps[6:8]}"
        pe_iso = f"{pe[0:4]}-{pe[4:6]}-{pe[6:8]}"

        comps = [c for c in [c1, c2, c3] if c]
        comps_str = ", ".join(comps)

        print(f"dekad {dekad}: {ps_iso} to {pe_iso}")
        print(f"  composites: {comps_str}")

        # Blank line after dekad 3 of each month
        if dekad == 3:
            print()


def save_dekad_records_to_csv(records: List[Dict[str, Any]], csv_path: str) -> None:
    """
    Save dekad records to a CSV file.
    """
    if not records:
        return

    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "dekad_id",
        "year",
        "month",
        "month_name",
        "dekad",
        "period_start",
        "period_end",
        "composite_1",
        "composite_2",
        "composite_3",
        "n_composites",
    ]

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


def save_dekad_records_to_json(records: List[Dict[str, Any]], json_path: str) -> None:
    """
    Save dekad records to a JSON file as a flat list of objects.
    """
    Path(json_path).parent.mkdir(parents=True, exist_ok=True)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)


# ---------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------
if __name__ == "__main__":
    # Build the full dekadal calendar needed by the pipeline.
    years_to_process = list(range(2010, 2041))

    # Build records
    dekad_records = get_dekad_records_for_years(years_to_process)

    # Print human readable calendar
    print_dekad_calendar(dekad_records)

    # Save the outputs in the project data folder so other pipeline scripts can reuse them.
    csv_file = OUTPUT_DIR / "viirs_dekads_2010_2040.csv"
    json_file = OUTPUT_DIR / "viirs_dekads_2010_2040.json"

    save_dekad_records_to_csv(dekad_records, str(csv_file))
    save_dekad_records_to_json(dekad_records, str(json_file))

    print(f"\nSaved CSV to:  {csv_file}")
    print(f"Saved JSON to: {json_file}")
