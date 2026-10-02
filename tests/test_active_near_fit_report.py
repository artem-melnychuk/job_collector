from __future__ import annotations

import unittest

from openpyxl import Workbook

from scripts.active_near_fit_report import build_active_near_fit, copy_applied_marks, handled_postings


class ActiveNearFitReportTests(unittest.TestCase):
    def test_keeps_only_active_near_fit_not_filtered(self) -> None:
        rows = [
            {"decision": "Подходит", "should_be_filtered": "Нет", "availability_status": "active", "title": "A"},
            {"decision": "Подходит", "should_be_filtered": "Нет", "availability_status": "closed", "title": "B"},
            {"decision": "Не подходит", "should_be_filtered": "Нет", "availability_status": "active", "title": "C"},
            {"decision": "Возможно", "should_be_filtered": "Да", "availability_status": "active", "title": "D"},
            {"decision": "Возможно", "should_be_filtered": "Нет", "availability_status": "active", "title": "E"},
        ]
        kept = build_active_near_fit(rows)
        self.assertEqual([row["title"] for row in kept], ["A", "E"])

    def test_sorts_подходит_before_возможно(self) -> None:
        rows = [
            {"decision": "Возможно", "should_be_filtered": "Нет", "availability_status": "active", "title": "First", "source": "A"},
            {"decision": "Подходит", "should_be_filtered": "Нет", "availability_status": "active", "title": "Second", "source": "B"},
        ]
        kept = build_active_near_fit(rows)
        self.assertEqual([row["title"] for row in kept], ["Second", "First"])

    def test_drops_postings_already_sent_or_rejected(self) -> None:
        # The sheet is the daily "where to apply" list: anything already sent
        # or rejected in the applications sheet (by job_id, or by URL for a
        # hand-added row) must not show up again. "approved" stays.
        base = {"decision": "Подходит", "should_be_filtered": "Нет", "availability_status": "active"}
        rows = [
            {**base, "job_id": "j1", "url": "https://x.com/1", "title": "sent by id"},
            {**base, "job_id": "j2", "url": "https://x.com/2/", "title": "sent by url"},
            {**base, "job_id": "j3", "url": "https://x.com/3", "title": "approved draft"},
            {**base, "job_id": "j4", "url": "https://x.com/4", "title": "untouched"},
        ]
        applications = [
            {"job_id": "j1", "url": "https://x.com/1", "status": "sent"},
            {"job_id": "", "url": "https://x.com/2", "status": "rejected"},
            {"job_id": "j3", "url": "https://x.com/3", "status": "approved"},
        ]
        kept = build_active_near_fit(rows, *handled_postings(applications))
        self.assertEqual([row["title"] for row in kept], ["approved draft", "untouched"])


    def test_applied_mark_is_copied_to_manual_review_and_hides_the_row(self) -> None:
        # active_near_fit is rebuilt every run, so a mark typed there must be
        # copied to manual_review first or it would be lost.
        workbook = Workbook()
        near = workbook.active
        near.title = "active_near_fit"
        near.append(["applied", "title", "job_id"])
        near.append(["Да", "Data Analyst", "j1"])
        near.append([None, "BI Analyst", "j2"])
        review = workbook.create_sheet("manual_review")
        review.append(["job_id", "decision", "applied"])
        review.append(["j1", "Подходит", None])
        review.append(["j2", "Возможно", None])

        self.assertEqual(copy_applied_marks(workbook), 1)
        self.assertEqual(review["C2"].value, "Да")
        self.assertIsNone(review["C3"].value)

        base = {"should_be_filtered": "Нет", "availability_status": "active"}
        rows = [
            {**base, "decision": "Подходит", "job_id": "j1", "applied": "Да", "title": "marked"},
            {**base, "decision": "Возможно", "job_id": "j2", "applied": "", "title": "open"},
        ]
        self.assertEqual([row["title"] for row in build_active_near_fit(rows)], ["open"])


    def test_copies_of_one_job_show_once_with_other_sources(self) -> None:
        base = {"decision": "Возможно", "should_be_filtered": "Нет", "availability_status": "active",
                "duplicate_group": "g1", "title": "Data Analyst"}
        rows = [
            {**base, "job_id": "li_1", "url": "https://x.com/li", "source": "LinkedIn"},
            {**base, "job_id": "cc_1", "url": "https://x.com/cc", "source": "Company Careers"},
            {**base, "job_id": "solo", "url": "https://x.com/solo", "source": "Djinni", "duplicate_group": ""},
        ]
        kept = build_active_near_fit(rows)
        self.assertEqual([row["job_id"] for row in kept], ["cc_1", "solo"])  # company's own ATS preferred
        self.assertEqual(kept[0]["also_on"], "LinkedIn")

    def test_applying_to_one_copy_hides_the_whole_group(self) -> None:
        base = {"decision": "Подходит", "should_be_filtered": "Нет", "availability_status": "active",
                "duplicate_group": "g1"}
        rows = [
            {**base, "job_id": "a", "url": "https://x.com/a", "source": "Jobicy"},
            {**base, "job_id": "b", "url": "https://x.com/b", "source": "Himalayas"},
        ]
        applications = [{"job_id": "a", "url": "https://x.com/a", "status": "sent"}]
        self.assertEqual(build_active_near_fit(rows, *handled_postings(applications)), [])
        marked = [{**rows[0], "applied": "Да"}, rows[1]]
        self.assertEqual(build_active_near_fit(marked), [])


if __name__ == "__main__":
    unittest.main()
