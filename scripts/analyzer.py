"""Run the first-pass vacancy analysis on the processed workbook."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.analyzer import analyze_record
from core.cross_dedup import assign_duplicate_groups
from core.storage import load_records, save_records


def main() -> int:
    path = PROJECT_ROOT / "data" / "processed" / "crypto_jobs_clean_v1.xlsx"
    records = load_records(path)
    analyzed = [analyze_record(record) for record in records]
    # After per-record analysis: grouping compares records with each other.
    analyzed = assign_duplicate_groups(analyzed)
    save_records(analyzed, path, workbook_kind="processed")
    print(f"Analyzed records: {len(analyzed)}")
    groups = {record.duplicate_group for record in analyzed if record.duplicate_group}
    grouped = sum(1 for record in analyzed if record.duplicate_group)
    print(f"Duplicate groups: {len(groups)} ({grouped} records)")
    print(f"Processed XLSX: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
