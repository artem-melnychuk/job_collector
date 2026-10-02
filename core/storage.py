"""Raw and processed data storage helpers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from core.ids import build_deduplication_key, build_job_id
from core.models import JOB_RECORD_COLUMNS, JobRecord


TRANSIENT_FIELDS = {"job_id", "date_collected", "status", "note", "search_query"}

# jobs_view and jobs_text (a column subset and a long-text subset of
# jobs_master, for reading only - no code ever read them) were dropped on
# 2026-09-30 at the user's request; save_records deletes leftover copies.
OBSOLETE_SHEETS = ("jobs_view", "jobs_text")

MANUAL_REVIEW_COLUMNS = (
    "job_id",
    "title",
    "company",
    "source",
    "city_region",
    "country",
    "contract_type",
    "url",
    "job_category",
    "role_family",
    "seniority",
    "duplicate_group",
    "personal_fit",
    "decision",
    "applied",
    "should_be_filtered",
    "french_required",
    "language_notes",
    "work_mode",
    "location_fit",
    "seniority_manual",
    "coding_intensity",
    "experience_fit",
    "main_reason",
    "review_notes",
)

# `applied`: the user marks "Да" (or types the date) once they have sent an
# application; scripts/applications_tracker.py turns every mark into a
# `sent` row in the applications sheet, so the log never has to be typed.
MANUAL_INPUT_COLUMNS = (
    "decision",
    "applied",
    "should_be_filtered",
    "french_required",
    "language_notes",
    "work_mode",
    "location_fit",
    "seniority_manual",
    "coding_intensity",
    "experience_fit",
    "main_reason",
    "review_notes",
)


def compute_personal_fit(decision: str, main_reason: str) -> str:
    """Fold the two manually-filled `decision`/`main_reason` columns into one
    scannable summary string, so reading the sheet doesn't mean reading both
    columns for every row.

    Deliberately not an independent judgment - it never overrides or
    re-derives `decision`, only formats what a human already decided. A
    separate, automatically *computed* fit score was considered and
    rejected (see CLAUDE.md's "Known gaps"): it would duplicate manual
    judgment with lower confidence. This stays a pure display fold of
    existing manual input, recomputed fresh on every save so it can never
    drift from the `decision`/`main_reason` it summarizes.
    """
    decision = str(decision or "").strip()
    reason = str(main_reason or "").strip()
    if not decision:
        return ""
    if decision == "Подходит" or not reason:
        return decision
    return f"{decision} — {reason}"


def records_to_dataframe(records: Iterable[JobRecord]) -> pd.DataFrame:
    """Convert records to the canonical column order used by exports."""
    rows = [record.to_dict() for record in records]
    return pd.DataFrame(rows, columns=JOB_RECORD_COLUMNS)


def _write_sheet(dataframe: pd.DataFrame, writer: pd.ExcelWriter, sheet_name: str) -> None:
    dataframe.to_excel(writer, sheet_name=sheet_name, index=False)
    worksheet = writer.book[sheet_name]
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    for column_cells in worksheet.columns:
        values = [str(cell.value or "") for cell in column_cells[:100]]
        width = min(max(max((len(value) for value in values), default=10) + 2, 10), 42)
        worksheet.column_dimensions[column_cells[0].column_letter].width = width


def _clean_cell(value: object) -> object:
    """Turn pandas' NaN-for-a-blank-cell into a plain empty string.

    `pd.read_excel` reads an untouched (still-blank) manual_review cell as
    float `nan`, not `""` - left unconverted, `str(nan)` is the literal text
    "nan", which is truthy and non-empty. That silently broke two consumers
    of this function's output: `compute_personal_fit` folded it into
    "nan — nan" for postings nobody had reviewed yet, and
    `scripts/fit_score.py`'s undecided-postings filter treated "nan" as an
    already-made decision and skipped scoring brand-new postings entirely.
    """
    if isinstance(value, float) and value != value:  # NaN check without importing math/numpy
        return ""
    return value


def load_manual_review(output_path: Path) -> dict[str, dict[str, object]]:
    if not output_path.exists():
        return {}
    try:
        with pd.ExcelFile(output_path) as workbook:
            if "manual_review" not in workbook.sheet_names:
                return {}
            dataframe = pd.read_excel(workbook, sheet_name="manual_review")
    except (OSError, ValueError):
        return {}
    if "job_id" not in dataframe.columns:
        return {}
    result = {}
    for _, row in dataframe.iterrows():
        job_id = str(row.get("job_id", "")).strip()
        if job_id and job_id.casefold() != "nan":
            result[job_id] = {column: _clean_cell(value) for column, value in row.to_dict().items()}
    return result


def _manual_review_dataframe(
    records: Iterable[JobRecord],
    existing: dict[str, dict[str, object]],
) -> pd.DataFrame:
    rows = []
    for record in records:
        values = record.to_dict()
        previous = existing.get(str(values.get("job_id", "")), {})
        row = {
            column: values.get(column, "")
            for column in MANUAL_REVIEW_COLUMNS
            if column not in MANUAL_INPUT_COLUMNS
        }
        row.update({column: previous.get(column, "") for column in MANUAL_INPUT_COLUMNS})
        row["personal_fit"] = compute_personal_fit(row.get("decision", ""), row.get("main_reason", ""))
        rows.append(row)
    return pd.DataFrame(rows, columns=MANUAL_REVIEW_COLUMNS)


def _style_manual_review_sheet(worksheet) -> None:
    input_start = MANUAL_REVIEW_COLUMNS.index(MANUAL_INPUT_COLUMNS[0]) + 1
    input_end = len(MANUAL_REVIEW_COLUMNS)
    yellow = PatternFill("solid", fgColor="FFF2CC")
    header_yellow = PatternFill("solid", fgColor="FFD966")
    for column_index in range(input_start, input_end + 1):
        worksheet.cell(1, column_index).fill = header_yellow
        worksheet.cell(1, column_index).font = Font(bold=True)
        for row_index in range(2, worksheet.max_row + 1):
            worksheet.cell(row_index, column_index).fill = yellow

    validations = {
        "decision": '"Подходит,Возможно,Не подходит"',
        "applied": '"Да"',
        "should_be_filtered": '"Да,Нет,Неясно"',
        "french_required": '"Да,Нет,Неясно"',
        "work_mode": '"Remote,Hybrid,Office,Неясно"',
        "location_fit": '"Подходит,Не подходит,Неясно"',
        "seniority_manual": '"Junior,Mid-level,Senior,Lead,Manager,Неясно"',
        "coding_intensity": '"Низкая,Средняя,Высокая"',
        "experience_fit": '"Да,Частично,Нет"',
    }
    for column_name, formula in validations.items():
        column_index = MANUAL_REVIEW_COLUMNS.index(column_name) + 1
        column_letter = worksheet.cell(1, column_index).column_letter
        validation = DataValidation(type="list", formula1=formula, allow_blank=True)
        worksheet.add_data_validation(validation)
        validation.add(f"{column_letter}2:{column_letter}{max(worksheet.max_row, 2)}")

    # Color each row by its `decision` so the sheet is scannable at a glance
    # without reading every cell. Conditional formatting (not a static fill)
    # so it's re-evaluated live as decisions get edited, and survives being
    # rebuilt by save_records on every future collector/analyzer run.
    decision_column_letter = worksheet.cell(1, MANUAL_REVIEW_COLUMNS.index("decision") + 1).column_letter
    last_column_letter = worksheet.cell(1, len(MANUAL_REVIEW_COLUMNS)).column_letter
    data_range = f"A2:{last_column_letter}{max(worksheet.max_row, 2)}"
    decision_fills = {
        "Подходит": PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid"),
        "Возможно": PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid"),
        "Не подходит": PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid"),
    }
    for value, fill in decision_fills.items():
        worksheet.conditional_formatting.add(
            data_range,
            FormulaRule(formula=[f'${decision_column_letter}2="{value}"'], fill=fill),
        )

    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions


# The three tabs the user works in day to day, in this order: where to apply,
# the log of applications, and grading newly collected postings. Everything
# else in the processed workbook is pipeline data or occasional analysis, so
# it is hidden (not deleted - Excel's "Unhide" brings it back). Every script
# that writes this workbook calls arrange_workbook_tabs() before saving,
# because a replaced sheet comes back visible and at the end of the tab list.
WORKING_SHEETS = ("active_near_fit", "applications", "manual_review")
HIDDEN_SHEETS = ("jobs_master", "skills_gap", "skill_recommendations")


def arrange_workbook_tabs(book) -> None:
    """Put WORKING_SHEETS first, hide HIDDEN_SHEETS, open on the first tab."""
    for position, name in enumerate(name for name in WORKING_SHEETS if name in book.sheetnames):
        book.move_sheet(name, offset=position - book.sheetnames.index(name))
    visible = []
    for worksheet in book.worksheets:
        worksheet.sheet_state = "hidden" if worksheet.title in HIDDEN_SHEETS else "visible"
        worksheet.sheet_view.tabSelected = False
        if worksheet.sheet_state == "visible":
            visible.append(worksheet)
    if not visible:  # Excel refuses a workbook with no visible sheet
        book.worksheets[0].sheet_state = "visible"
        visible.append(book.worksheets[0])
    book.active = book.worksheets.index(visible[0])
    visible[0].sheet_view.tabSelected = True


def save_records(
    records: Iterable[JobRecord],
    output_path: Path,
    *,
    workbook_kind: str = "processed",
) -> tuple[Path, Path]:
    """Save records as a canonical UTF-8-BOM CSV and a structured XLSX workbook."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path = output_path.with_suffix(".csv")
    records = list(records)
    dataframe = records_to_dataframe(records)
    existing_manual = load_manual_review(output_path)

    dataframe.to_csv(csv_path, index=False, encoding="utf-8-sig")
    # Append + replace-existing-sheets mode (only possible when the file
    # already exists) preserves sheets this function never touches, e.g.
    # skills_gap_report.py's "skills_gap" sheet. The default write mode
    # rebuilds the workbook from scratch, silently deleting any such sheet
    # on every analyzer/collector run — that used to happen every time.
    writer_kwargs: dict[str, Any] = {"engine": "openpyxl"}
    if output_path.exists():
        writer_kwargs["mode"] = "a"
        writer_kwargs["if_sheet_exists"] = "replace"
    with pd.ExcelWriter(output_path, **writer_kwargs) as writer:
        if workbook_kind == "raw":
            _write_sheet(dataframe, writer, "raw_jobs")
        else:
            _write_sheet(dataframe, writer, "jobs_master")
            for name in OBSOLETE_SHEETS:
                if name in writer.book.sheetnames:
                    del writer.book[name]
            _write_sheet(_manual_review_dataframe(records, existing_manual), writer, "manual_review")
            _style_manual_review_sheet(writer.book["manual_review"])
            arrange_workbook_tabs(writer.book)

    return output_path, csv_path


def load_records(input_path: Path) -> list[JobRecord]:
    """Load canonical records from an XLSX or CSV export."""
    input_path = Path(input_path)
    if not input_path.exists():
        return []

    if input_path.suffix.casefold() == ".csv":
        dataframe = pd.read_csv(input_path)
    else:
        with pd.ExcelFile(input_path) as workbook:
            if "jobs_master" in workbook.sheet_names:
                sheet_name = "jobs_master"
            elif "Jobs" in workbook.sheet_names:
                sheet_name = "Jobs"
            else:
                sheet_name = "raw_jobs"
            dataframe = pd.read_excel(workbook, sheet_name=sheet_name)

    return [JobRecord.from_mapping(row.to_dict()) for _, row in dataframe.iterrows()]


def _content_signature(record: JobRecord) -> tuple[str, ...]:
    values = record.to_dict()
    return tuple(
        str(values[field])
        for field in JOB_RECORD_COLUMNS
        if field not in TRANSIENT_FIELDS
    )


ANALYSIS_FIELDS = (
    "role_family",
    "role_subcategory",
    "seniority",
    "years_experience_min",
    "years_experience_max",
    "skills_all",
    "required_skills",
    "preferred_skills",
    "analysis_note",
    "fit_score",
    "fit_reasoning",
    "duplicate_group",
)


def _reconcile_updated(previous: JobRecord, incoming: JobRecord) -> JobRecord:
    """Carry forward analysis/availability that a re-collect can't know about.

    A collector run only ever produces a freshly-scraped, not-yet-analyzed
    record (analyzer.py is a separate pass) with availability_status hardcoded
    to "active". Without this, re-collecting a previously-analyzed or
    previously-availability-checked posting would silently overwrite that
    work with blanks/"active" on every rerun, well before anyone notices.
    """
    updates = {
        field_name: getattr(previous, field_name)
        for field_name in ANALYSIS_FIELDS
        if not getattr(incoming, field_name) and getattr(previous, field_name)
    }
    if incoming.availability_status in {"", "active"} and previous.availability_status not in {"", "active", "unknown"}:
        updates["availability_status"] = previous.availability_status
    return replace(incoming, **updates) if updates else incoming


def merge_records(
    existing: Iterable[JobRecord],
    incoming: Iterable[JobRecord],
) -> tuple[list[JobRecord], dict[str, int]]:
    """Merge a run into the processed dataset and classify its changes."""
    merged = list(existing)
    by_key = {build_deduplication_key(record): index for index, record in enumerate(merged)}
    counts = {"new": 0, "existing": 0, "updated": 0}

    for incoming_record in incoming:
        incoming_record.job_id = incoming_record.job_id or build_job_id(incoming_record)
        key = build_deduplication_key(incoming_record)
        existing_index = by_key.get(key)

        if existing_index is None:
            merged.append(replace(incoming_record, status="new"))
            by_key[key] = len(merged) - 1
            counts["new"] += 1
            continue

        previous = merged[existing_index]
        reconciled = _reconcile_updated(previous, incoming_record)
        status = "updated" if _content_signature(previous) != _content_signature(reconciled) else "existing"
        merged[existing_index] = replace(
            reconciled,
            job_id=previous.job_id or incoming_record.job_id,
            status=status,
        )
        counts[status] += 1

    return merged, counts
