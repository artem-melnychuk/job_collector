from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from core.applications import DraftFile, apply_applied_marks, build_applications_rows, scan_draft_files, url_key
from core.cv_review import extract_verdict
from core.models import JobRecord


class ExtractVerdictTests(unittest.TestCase):
    def test_extracts_approved(self) -> None:
        self.assertEqual(extract_verdict("...\n**Вердикт:** ✅ Одобрено\n..."), "approved")

    def test_extracts_needs_revision(self) -> None:
        self.assertEqual(extract_verdict("...\n**Вердикт:** ⚠️ Нужна правка\n..."), "needs_revision")

    def test_no_verdict_present_returns_empty(self) -> None:
        self.assertEqual(extract_verdict("# Just a draft, never reviewed"), "")


class ScanDraftFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp_dir.name)

        (self.dir / "reviewed_with_pdf.md").write_text(
            "# Title\n\n> pitch\n\n---\n\n## Ревью (агент-проверяющий)\n\n**Вердикт:** ✅ Одобрено\n",
            encoding="utf-8",
        )
        (self.dir / "reviewed_with_pdf.pdf").write_bytes(b"%PDF-1.4 fake")

        (self.dir / "not_reviewed_no_pdf.md").write_text("# Title\n\n> pitch\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_missing_directory_returns_empty(self) -> None:
        self.assertEqual(scan_draft_files(self.dir / "nope"), [])

    def test_reads_verdict_and_pdf_presence(self) -> None:
        drafts = {draft.job_id: draft for draft in scan_draft_files(self.dir)}
        self.assertEqual(drafts["reviewed_with_pdf"].review_verdict, "approved")
        self.assertTrue(drafts["reviewed_with_pdf"].pdf_ready)
        self.assertEqual(drafts["not_reviewed_no_pdf"].review_verdict, "")
        self.assertFalse(drafts["not_reviewed_no_pdf"].pdf_ready)


class BuildApplicationsRowsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records_by_id = {
            "job_a": JobRecord(job_id="job_a", title="Data Analyst", company="Acme", url="https://example.com/a"),
        }
        self.drafts = [DraftFile(job_id="job_a", review_verdict="approved", pdf_ready=True)]

    def test_new_posting_defaults_to_draft_status_and_todays_date(self) -> None:
        rows = build_applications_rows(self.drafts, self.records_by_id, existing={}, today=date(2026, 9, 15))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["job_id"], "job_a")
        self.assertEqual(row["title"], "Data Analyst")
        self.assertEqual(row["company"], "Acme")
        self.assertEqual(row["review_verdict"], "approved")
        self.assertEqual(row["pdf_ready"], "Да")
        self.assertEqual(row["status"], "draft")
        self.assertEqual(row["date_added"], "2026-09-15")

    def test_existing_status_and_notes_are_preserved_across_reruns(self) -> None:
        existing = {"job_a": {"status": "approved", "notes": "sending Monday", "date_added": "2026-09-10"}}
        rows = build_applications_rows(self.drafts, self.records_by_id, existing, today=date(2026, 9, 15))
        row = rows[0]
        self.assertEqual(row["status"], "approved")
        self.assertEqual(row["notes"], "sending Monday")
        self.assertEqual(row["date_added"], "2026-09-10")  # not overwritten to today

    def test_derived_fields_still_refresh_even_when_status_is_preserved(self) -> None:
        # review_verdict/pdf_ready reflect the CURRENT file state, not
        # whatever was true the first time this job_id was tracked.
        existing = {"job_a": {"status": "rejected", "notes": "", "date_added": "2026-09-01"}}
        drafts = [DraftFile(job_id="job_a", review_verdict="needs_revision", pdf_ready=False)]
        rows = build_applications_rows(drafts, self.records_by_id, existing, today=date(2026, 9, 15))
        self.assertEqual(rows[0]["review_verdict"], "needs_revision")
        self.assertEqual(rows[0]["pdf_ready"], "Нет")
        self.assertEqual(rows[0]["status"], "rejected")

    def test_hand_added_row_without_draft_is_kept_and_matched_by_url(self) -> None:
        # Regression (2026-09-30): the user typed a Collectly row (company +
        # URL + status, no job_id, no draft). Rebuilding the sheet from draft
        # files used to drop it. It is kept, and re-keyed to the dataset's
        # job_id through its URL.
        records = {**self.records_by_id,
                   "job_b": JobRecord(job_id="job_b", title="Analytics Engineer", company="Collectly",
                                      url="https://jobs.lever.co/CollectlyInc/abc")}
        existing = {url_key("https://jobs.lever.co/CollectlyInc/abc/"): {
            "company": "CollectlyInc", "url": "https://jobs.lever.co/CollectlyInc/abc/",
            "status": "sent", "notes": "", "date_added": "2026-09-30 00:00:00"}}
        rows = build_applications_rows(self.drafts, records, existing, today=date(2026, 10, 1))
        manual = [row for row in rows if row["job_id"] == "job_b"]
        self.assertEqual(len(manual), 1)
        self.assertEqual(manual[0]["company"], "Collectly")
        self.assertEqual(manual[0]["status"], "sent")
        self.assertEqual(manual[0]["date_added"], "2026-09-30")
        self.assertEqual(manual[0]["pdf_ready"], "")

    def test_hand_added_row_outside_dataset_keeps_typed_fields(self) -> None:
        existing = {url_key("https://www.linkedin.com/jobs/view/123/"): {
            "title": "BI Analyst", "company": "SomeCo", "url": "https://www.linkedin.com/jobs/view/123/",
            "status": "", "notes": "Easy Apply", "date_added": ""}}
        rows = build_applications_rows([], {}, existing, today=date(2026, 10, 1))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job_id"], "")
        self.assertEqual(rows[0]["company"], "SomeCo")
        self.assertEqual(rows[0]["status"], "sent")  # a typed-in row means it was sent
        self.assertEqual(rows[0]["date_added"], "2026-10-01")

    def test_empty_date_cells_stay_empty_not_nat(self) -> None:
        # pandas reads an empty date cell as NaT; str(NaT) is "NaT".
        import pandas as pd
        existing = {url_key("https://x.com/1"): {"company": "A", "url": "https://x.com/1", "status": "sent",
                                                  "date_sent": pd.NaT, "date_added": pd.NaT}}
        rows = build_applications_rows([], {}, existing, today=date(2026, 10, 1))
        self.assertEqual(rows[0]["date_sent"], "")
        self.assertEqual(rows[0]["date_added"], "2026-10-01")

    def test_leftover_draft_row_without_file_is_dropped(self) -> None:
        existing = {"job_gone": {"status": "draft", "notes": "", "date_added": "2026-09-15"}}
        rows = build_applications_rows([], {}, existing, today=date(2026, 10, 1))
        self.assertEqual(rows, [])

    def test_missing_record_leaves_title_company_url_blank(self) -> None:
        drafts = [DraftFile(job_id="ghost_job", review_verdict="", pdf_ready=False)]
        rows = build_applications_rows(drafts, records_by_id={}, existing={}, today=date(2026, 9, 15))
        self.assertEqual(rows[0]["title"], "")
        self.assertEqual(rows[0]["company"], "")



class ApplyAppliedMarksTests(unittest.TestCase):
    # The user marks "Да" (or a date) in manual_review / active_near_fit
    # after applying; the log is filled from those marks, never by hand.
    def test_mark_on_unlogged_posting_adds_sent_row_dated_today(self) -> None:
        merged = apply_applied_marks({}, {"job_x": "Да"}, today=date(2026, 10, 1))
        self.assertEqual(merged["job_x"]["status"], "sent")
        self.assertEqual(merged["job_x"]["date_sent"], "2026-10-01")

    def test_typed_date_is_used_as_date_sent(self) -> None:
        merged = apply_applied_marks({}, {"job_x": "2026-09-28"}, today=date(2026, 10, 1))
        self.assertEqual(merged["job_x"]["date_sent"], "2026-09-28")

    def test_draft_becomes_sent_but_decided_rows_are_left_alone(self) -> None:
        existing = {
            "job_draft": {"status": "draft", "notes": "n"},
            "job_rejected": {"status": "rejected", "notes": "closed"},
            "job_sent": {"status": "sent", "date_sent": "2026-09-20"},
        }
        marks = {"job_draft": "Да", "job_rejected": "Да", "job_sent": "Да", "job_blank": ""}
        merged = apply_applied_marks(existing, marks, today=date(2026, 10, 1))
        self.assertEqual(merged["job_draft"]["status"], "sent")
        self.assertEqual(merged["job_draft"]["notes"], "n")
        self.assertEqual(merged["job_rejected"]["status"], "rejected")
        self.assertEqual(merged["job_sent"]["date_sent"], "2026-09-20")
        self.assertNotIn("job_blank", merged)
        self.assertEqual(existing["job_draft"]["status"], "draft")  # input not mutated


if __name__ == "__main__":
    unittest.main()
