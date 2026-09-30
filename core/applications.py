"""Application status tracker (plan item 8.5).

Turns the per-posting drafts already sitting in `CV/drafts/*.md` (8.2's
pitch/bullets, 8.3's review verdict, 8.4's assembled PDF) into one tracking
sheet keyed by `job_id`. This is the actual gate the roadmap called for:
every posting starts life here as `draft`, and nothing in this pipeline
ever sends anything - a human moves a row to `approved` and, only after
actually sending it themselves outside this tool, to `sent`. No LLM call,
no network access; this only reads local files and the processed dataset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from core.cv_review import extract_verdict, has_review
from core.ids import normalize_url
from core.models import JobRecord

# What the user reads first comes first; job_id is internal and goes last.
APPLICATIONS_COLUMNS = (
    "company",
    "title",
    "status",
    "date_sent",
    "notes",
    "url",
    "review_verdict",
    "pdf_ready",
    "date_added",
    "job_id",
)

# Preserved across reruns, same convention as manual_review's
# MANUAL_INPUT_COLUMNS - a human fills these in, a rerun must never
# overwrite them with a freshly-derived value.
APPLICATIONS_INPUT_COLUMNS = ("status", "date_sent", "notes")

DEFAULT_STATUS = "draft"
STATUS_OPTIONS = ("draft", "approved", "rejected", "sent")


@dataclass
class DraftFile:
    job_id: str
    review_verdict: str
    pdf_ready: bool


def scan_draft_files(drafts_dir: Path) -> list[DraftFile]:
    """Read every draft's review verdict and PDF-readiness off disk."""
    if not drafts_dir.exists():
        return []
    results = []
    for path in sorted(drafts_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        verdict = extract_verdict(text) if has_review(text) else ""
        pdf_ready = path.with_suffix(".pdf").exists()
        results.append(DraftFile(job_id=path.stem, review_verdict=verdict, pdf_ready=pdf_ready))
    return results


def url_key(url: str) -> str:
    """Key for a sheet row that has a URL but no job_id (typed in by hand)."""
    return f"url:{normalize_url(url)}"


def _date_str(value: object) -> str:
    # pandas reads a hand-typed date cell back as a Timestamp ("2026-09-30
    # 00:00:00"); keep the sheet's plain ISO-date convention.
    # An empty date cell comes back as pandas' NaT, whose str() is "NaT".
    if value is None or str(value).strip() in {"", "NaT", "nan"}:
        return ""
    if hasattr(value, "date") and callable(value.date):
        return value.date().isoformat()
    return str(value).strip().removesuffix(" 00:00:00")


def _resolve_manual_keys(
    existing: dict[str, dict[str, str]],
    records_by_id: dict[str, JobRecord],
) -> dict[str, dict[str, str]]:
    """Re-key hand-added rows (known only by URL) to their job_id when the
    posting is in the dataset, so they merge with a draft or record."""
    job_id_by_url = {normalize_url(record.url): job_id for job_id, record in records_by_id.items() if record.url}
    resolved: dict[str, dict[str, str]] = {}
    for key, row in existing.items():
        if key.startswith("url:") and key[4:] in job_id_by_url:
            key = job_id_by_url[key[4:]]
        if key in resolved and str(resolved[key].get("status") or DEFAULT_STATUS) != DEFAULT_STATUS:
            continue  # keep the row that already carries a human decision
        resolved[key] = row
    return resolved


def build_applications_rows(
    drafts: list[DraftFile],
    records_by_id: dict[str, JobRecord],
    existing: dict[str, dict[str, str]],
    today: date | None = None,
) -> list[dict[str, str]]:
    """Build the applications sheet's rows: derived fields refreshed every
    run, but `status`/`notes` (and the original `date_added`) preserved from
    `existing` for any job_id already tracked - a rerun must never quietly
    reset a human's approval decision back to "draft".

    The sheet is the one log of every application, not only drafted ones:
    a row with no draft file (sent straight from the posting, or added by
    hand with just a URL) is kept as long as it carries a human status. A
    hand-added row with a blank status counts as `sent`. A leftover `draft`
    row whose file was deleted is dropped - nothing was decided there.
    """
    today_str = (today or date.today()).isoformat()
    existing = _resolve_manual_keys(existing, records_by_id)
    rows = []
    for draft in drafts:
        record = records_by_id.get(draft.job_id)
        previous = existing.get(draft.job_id, {})
        rows.append(
            {
                "job_id": draft.job_id,
                "title": record.title if record else "",
                "company": record.company if record else "",
                "url": record.url if record else "",
                "review_verdict": draft.review_verdict,
                "pdf_ready": "Да" if draft.pdf_ready else "Нет",
                "status": previous.get("status") or DEFAULT_STATUS,
                "date_sent": _date_str(previous.get("date_sent")),
                "notes": previous.get("notes", ""),
                "date_added": _date_str(previous.get("date_added")) or today_str,
            }
        )

    drafted_ids = {draft.job_id for draft in drafts}
    for key, previous in existing.items():
        if key in drafted_ids:
            continue
        status = str(previous.get("status") or "").strip() or "sent"
        if status == DEFAULT_STATUS:
            continue
        record = None if key.startswith("url:") else records_by_id.get(key)
        rows.append(
            {
                "job_id": "" if key.startswith("url:") else key,
                "title": record.title if record else str(previous.get("title") or ""),
                "company": record.company if record else str(previous.get("company") or ""),
                "url": record.url if record else str(previous.get("url") or ""),
                "review_verdict": "",
                "pdf_ready": "",
                "status": status,
                "date_sent": _date_str(previous.get("date_sent")),
                "notes": str(previous.get("notes") or ""),
                "date_added": _date_str(previous.get("date_added")) or today_str,
            }
        )
    return rows


def _mark_date(value: object, today_str: str) -> str:
    """The date an `applied` mark stands for: a typed date, otherwise today."""
    text = _date_str(value)
    return text if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else today_str


def apply_applied_marks(
    existing: dict[str, dict[str, str]],
    marks: dict[str, object],
    today: date | None = None,
) -> dict[str, dict[str, str]]:
    """Fold the user's `applied` marks (job_id -> "Да" or a date, from
    manual_review) into the applications rows as `sent`.

    A posting not yet in the log gets a new row. One that is still a draft
    or approved becomes sent. A row already sent or rejected is left alone,
    so re-running never rewrites a date or overrides a later decision.
    """
    today_str = (today or date.today()).isoformat()
    merged = {key: dict(row) for key, row in existing.items()}
    for job_id, value in marks.items():
        if not str(value or "").strip():
            continue
        sent_on = _mark_date(value, today_str)
        row = merged.get(job_id)
        if row is None:
            merged[job_id] = {"status": "sent", "date_sent": sent_on, "notes": "", "date_added": today_str}
        elif str(row.get("status") or DEFAULT_STATUS) in {DEFAULT_STATUS, "approved"}:
            row["status"] = "sent"
            row["date_sent"] = _date_str(row.get("date_sent")) or sent_on
    return merged
