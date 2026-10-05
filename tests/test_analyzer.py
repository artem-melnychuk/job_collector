from __future__ import annotations

import unittest

from core.analyzer import analyze_record, classify_role, classify_seniority, extract_skills, infer_experience_years
from core.models import JobRecord


class AnalyzerTests(unittest.TestCase):
    def make_record(self, title: str, text: str) -> JobRecord:
        return JobRecord(title=title, full_text=text)

    def test_classifies_risk_role_and_seniority(self) -> None:
        record = self.make_record(
            "Senior Risk Analyst",
            "5+ years of experience. Required: SQL and Python. Nice to have: Power BI.",
        )
        self.assertEqual(classify_role(record)[0], "Risk Analytics")
        self.assertEqual(classify_seniority(record)[0], "Senior")

    def test_classifies_risk_variants(self) -> None:
        for title in ("Risk Control Analyst", "Risk Manager", "Operational Risk Analyst"):
            self.assertEqual(classify_role(self.make_record(title, ""))[0], "Risk Analytics")

    def test_classifies_french_data_and_confirmed_roles(self) -> None:
        data_record = self.make_record("Analyste de donnees", "")
        confirmed_record = self.make_record("Data Analyst confirmé", "")
        self.assertEqual(classify_role(data_record)[0], "Data Analytics")
        self.assertEqual(classify_seniority(confirmed_record)[0], "Mid-level")

    def test_extracts_normalized_skills(self) -> None:
        record = self.make_record("Product Analyst", "Use SQL, Python, Power BI and A/B testing.")
        self.assertEqual(extract_skills(record), ["SQL", "Python", "Power BI", "A/B testing"])

    def test_splits_required_and_preferred_skills(self) -> None:
        analyzed = analyze_record(
            self.make_record("Data Analyst", "Required: SQL and Python. Nice to have: Tableau.")
        )
        self.assertEqual(analyzed.required_skills, "SQL; Python")
        self.assertEqual(analyzed.preferred_skills, "Tableau")

    def test_classifies_ukrainian_and_russian_role_titles(self) -> None:
        # Real phrasing from collected Djinni.co postings (2026-09-06).
        self.assertEqual(classify_role(self.make_record("", "Шукаємо бізнес-аналітика в команду"))[0], "Business Analytics")
        self.assertEqual(classify_role(self.make_record("", "Вакансія: аналітик даних для e-commerce"))[0], "Data Analytics")
        self.assertEqual(classify_role(self.make_record("", "Потрібен ризик-аналітик у фінтех"))[0], "Risk Analytics")

    def test_classifies_ukrainian_seniority_and_years(self) -> None:
        record = self.make_record("Product Analyst", "Досвід в Product-аналітиці від 3х років")
        seniority, minimum, maximum = classify_seniority(record)
        self.assertEqual(minimum, "3")
        self.assertEqual(seniority, "Mid-level")
        self.assertEqual(classify_seniority(self.make_record("", "Шукаємо джуніора в команду"))[0], "Junior")
        self.assertEqual(classify_seniority(self.make_record("", "Потрібен старший аналітик"))[0], "Senior")

    def test_calendar_years_are_not_years_of_experience(self) -> None:
        # Regression: French "ans?" matched the start of "and"/"analytics", so
        # each of these phrases from jobs_master (2026-10-05) was stored as N
        # years of experience (2017, 2012, 2025, 2027, 2000, 2019, 00, 2).
        for text in (
            "Wintermute was founded in 2017 and has successfully navigated",
            "founded in 2012 and headquartered in",
            "Best Workplaces in 2025 and recognized",
            "Winter Intern 2027 - Analytics & Reporting",
            "beginning in May 2027 and ending in August 2027",
            "our Global 2000 and Fortune 500 customers",
            "Founded in 2019 and fully distributed",
            "meetings with the supervisor between 15:00 and 20:00",
            "#1 DSP on G2 and leader in a number of categories",
            "bachelor's degree from a 4-year university",
        ):
            with self.subTest(text=text):
                self.assertEqual(infer_experience_years(text), ("", ""))

    def test_real_requirement_after_a_calendar_year_is_found(self) -> None:
        text = "Wintermute was founded in 2017 and has successfully navigated. 3+ years of experience"
        self.assertEqual(infer_experience_years(text), ("3", "3"))
        # Synthetic Ukrainian equivalent: "з 2015 року" is "since 2015".
        self.assertEqual(infer_experience_years("Працюємо з 2015 року. Досвід від 2 років"), ("2", "2"))

    def test_calendar_year_does_not_make_a_posting_senior(self) -> None:
        # With no seniority keyword, ">= 5 years" decided; 2019 made it Senior.
        record = self.make_record("Analytics Engineer, Data", "Founded in 2019 and fully distributed")
        self.assertEqual(classify_seniority(record), ("Unknown", "", ""))

    def test_reads_french_and_italian_years(self) -> None:
        self.assertEqual(infer_experience_years("3 ans d'expérience minimum"), ("3", "3"))
        self.assertEqual(infer_experience_years("Almeno 4 anni di esperienza come business analyst"), ("4", "4"))

    def test_splits_required_and_preferred_with_ukrainian_markers(self) -> None:
        analyzed = analyze_record(
            self.make_record("Data Analyst", "Обов'язково: SQL та Excel. Буде перевагою: Power BI.")
        )
        self.assertEqual(analyzed.required_skills, "SQL; Excel")
        self.assertEqual(analyzed.preferred_skills, "Power BI")

    def test_skill_mentioned_before_and_after_marker_stays_required(self) -> None:
        # Regression: a skill required up front and mentioned again in a
        # closing "nice to have: more X" summary used to be bucketed as
        # preferred-only, since only the post-marker text was checked.
        analyzed = analyze_record(
            self.make_record(
                "Data Analyst",
                "Required: SQL and Python. Nice to have: further Python/ML experience is a bonus.",
            )
        )
        self.assertEqual(analyzed.required_skills, "SQL; Python")
        self.assertEqual(analyzed.preferred_skills, "Machine Learning")

    def test_analyze_record_fills_city_region_and_salary_usd_equivalent(self) -> None:
        analyzed = analyze_record(
            JobRecord(
                title="Data Analyst",
                city_region="Paris, Ile-de-France, FR",
                country="FR",
                salary="80000-95000 EUR/year",
            )
        )
        self.assertEqual(analyzed.city, "Paris")
        self.assertEqual(analyzed.region, "Ile-de-France")
        self.assertEqual(analyzed.salary_usd_equivalent, "≈86,400-102,600 USD")


if __name__ == "__main__":
    unittest.main()
