from __future__ import annotations

import unittest

from core import skills_gap
from core.skills_gap import (
    build_rejection_report,
    build_skill_gap_report,
    classify_reasons,
)


class SkillGapReportTests(unittest.TestCase):
    def test_counts_only_near_fit_rows(self) -> None:
        jobs = [
            {"decision": "Подходит", "should_be_filtered": "Нет", "required_skills": "SQL; dbt", "preferred_skills": ""},
            {"decision": "Не подходит", "should_be_filtered": "Нет", "required_skills": "Airflow", "preferred_skills": ""},
            {"decision": "Возможно", "should_be_filtered": "Да", "required_skills": "Spark", "preferred_skills": ""},
        ]
        report = build_skill_gap_report(jobs)
        self.assertEqual(report.demand["dbt"], 1)
        self.assertNotIn("Airflow", report.demand)
        self.assertNotIn("Spark", report.demand)

    def test_known_skill_excluded_from_gap(self) -> None:
        jobs = [{"decision": "Подходит", "should_be_filtered": "Нет", "required_skills": "SQL; dbt", "preferred_skills": ""}]
        report = build_skill_gap_report(jobs)
        self.assertNotIn("SQL", report.gap)
        self.assertEqual(report.gap["dbt"], 1)

    def test_partial_skill_tracked_separately(self) -> None:
        # PARTIAL_SKILLS is empty by default (see core/skills_gap.py); patch
        # in a skill for this test rather than relying on the real resume's
        # current state, which is a human-maintained document, not test data.
        original = skills_gap.PARTIAL_SKILLS
        skills_gap.PARTIAL_SKILLS = {"Python"}
        try:
            jobs = [{"decision": "Возможно", "should_be_filtered": "Нет", "required_skills": "Python", "preferred_skills": ""}]
            report = build_skill_gap_report(jobs)
            self.assertEqual(report.partial["Python"], 1)
            self.assertNotIn("Python", report.gap)
        finally:
            skills_gap.PARTIAL_SKILLS = original

    def test_domain_context_terms_excluded_as_not_learnable(self) -> None:
        # "Crypto"/"Blockchain" mostly fire on company-description boilerplate,
        # not an actual skill ask — see core/skills_gap.py:NOT_A_LEARNABLE_SKILL.
        jobs = [{"decision": "Подходит", "should_be_filtered": "Нет", "required_skills": "Crypto; Blockchain; dbt", "preferred_skills": ""}]
        report = build_skill_gap_report(jobs)
        self.assertNotIn("Crypto", report.demand)
        self.assertNotIn("Blockchain", report.demand)
        self.assertEqual(report.gap["dbt"], 1)

    def test_gap_skills_carry_example_titles(self) -> None:
        jobs = [
            {"decision": "Подходит", "should_be_filtered": "Нет", "required_skills": "dbt", "preferred_skills": "", "title": "Data Analyst A"},
            {"decision": "Возможно", "should_be_filtered": "Нет", "required_skills": "dbt", "preferred_skills": "", "title": "Data Analyst B"},
        ]
        report = build_skill_gap_report(jobs)
        self.assertEqual(report.examples["dbt"], ["Data Analyst A", "Data Analyst B"])


class RejectionReportTests(unittest.TestCase):
    def test_classifies_multiple_buckets(self) -> None:
        buckets = classify_reasons("Требуется Senior уровень и знание Mandarin для координации")
        self.assertIn("seniority_too_high", buckets)
        self.assertIn("language_other_than_english", buckets)

    def test_only_rejected_rows_counted(self) -> None:
        jobs = [
            {"decision": "Не подходит", "should_be_filtered": "Нет", "main_reason": "Требуется Python", "review_notes": ""},
            {"decision": "Подходит", "should_be_filtered": "Нет", "main_reason": "Требуется Python", "review_notes": ""},
        ]
        report = build_rejection_report(jobs)
        self.assertEqual(report.total_rejected, 1)
        self.assertEqual(report.counts["coding_too_deep"], 1)

    def test_learnable_counts_excludes_structural_buckets(self) -> None:
        jobs = [
            {"decision": "Не подходит", "should_be_filtered": "Нет", "main_reason": "требует офис", "review_notes": ""},
            {"decision": "Не подходит", "should_be_filtered": "Нет", "main_reason": "нужен Python", "review_notes": ""},
        ]
        report = build_rejection_report(jobs)
        learnable = report.learnable_counts()
        self.assertNotIn("location_or_onsite", learnable)
        self.assertEqual(learnable["coding_too_deep"], 1)


if __name__ == "__main__":
    unittest.main()
