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

    def test_reads_french_italian_and_spanish_years(self) -> None:
        self.assertEqual(infer_experience_years("3 ans d'expérience minimum"), ("3", "3"))
        self.assertEqual(infer_experience_years("Almeno 4 anni di esperienza come business analyst"), ("4", "4"))
        self.assertEqual(infer_experience_years("con al menos 5 años de experiencia en la unificación"), ("5", "5"))

    def test_reads_ranges_with_any_dash(self) -> None:
        # Regression: only "-" was a range separator, so "0–2 years" read as 2.
        self.assertEqual(infer_experience_years("experience: 3–4 years  compensation"), ("3", "4"))
        self.assertEqual(infer_experience_years("entry-level professional (0–2 years of experience)"), ("0", "2"))
        self.assertEqual(infer_experience_years("1 to 3 years of experience in business analysis"), ("1", "3"))
        self.assertEqual(infer_experience_years("досвід: від 1-2 років у сфері аналітики даних"), ("1", "2"))

    def test_reads_or_more_years(self) -> None:
        text = "engineering or a related field 3 or more years of experience in product analytics"
        self.assertEqual(infer_experience_years(text), ("3", "3"))

    def test_reads_decimal_years(self) -> None:
        # Regression: "1.5 years" read as 5, which made the posting Senior.
        self.assertEqual(infer_experience_years("1.5 years of experience with SQL"), ("1.5", "1.5"))
        self.assertEqual(infer_experience_years("досвід від 1,5 року"), ("1.5", "1.5"))
        self.assertEqual(classify_seniority(self.make_record("Data Analyst", "1.5 years of experience"))[0], "Unknown")

    def test_company_age_is_not_years_of_experience(self) -> None:
        # Phrases from jobs_master (2026-10-05) where the first "N years" was
        # the company's age and the real requirement, if any, came later.
        cases = (
            ("leading the way in technology projects for over 15 years. 4+ years of experience", ("4", "4")),
            ("one of Canada's best employers for the past 8 years committed to continuous learning", ("", "")),
            ("a leader in AI training data for over 30 years", ("", "")),
            ("7 years on the market, 500+ employees. Have 2 years in an analytical role", ("2", "2")),
            ("ми на ринку вже 13 років; game data analyst — від 2 років", ("2", "2")),
            ("4 роки на ринку — стабільна компанія. 3+ роки досвіду як marketing analyst", ("3", "3")),
            ("команда, яка вже понад 4 роки успішно працює на ринку", ("", "")),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(infer_experience_years(text), expected)

    def test_requirement_next_to_a_company_age_word_is_kept(self) -> None:
        self.assertEqual(infer_experience_years("over 5 years of experience in analytics"), ("5", "5"))
        self.assertEqual(infer_experience_years("маєш вже 2 роки досвіду в аналітиці"), ("2", "2"))
        self.assertEqual(infer_experience_years("minimum 7 years in business intelligence"), ("7", "7"))

    def test_seniority_ignores_words_about_other_people_or_the_company(self) -> None:
        # Real (title, text) pairs from jobs_master whose seniority came from a
        # word inside another word or about someone else (2026-10-05).
        cases = (
            ("Analytics Engineer, Data", "share access to internal tools"),
            ("Quant Researcher (MFT Focus)", "a ventures arm that invests in early stage DeFi projects"),
            ("Python and Kubernetes Software Engineer", "undergraduate degree in a technical subject"),
            ("Unity Catalog Business Definitions Analyst", "Rajesh K • 3rd\n\nLead - Talent Acquisition"),
            ("Associate Business Analyst", "platform configuration, and internal quality assurance"),
            ("Business Analyst", "Binance is a leading global blockchain ecosystem"),
            ("Product Data Analyst", "робота з product managers, щоб формувати метрики"),
            ("Product Analyst", "зустріч з твоїм потенційним керівником"),
            ("Business Analyst", "presentations for senior stakeholders"),
        )
        for title, text in cases:
            with self.subTest(title=title, text=text):
                self.assertEqual(classify_seniority(self.make_record(title, text))[0], "Unknown")

    def test_seniority_reads_the_title_first(self) -> None:
        self.assertEqual(classify_seniority(self.make_record("Sr. Business Analyst", ""))[0], "Senior")
        self.assertEqual(classify_seniority(self.make_record("JR/Business Analyst", ""))[0], "Junior")
        self.assertEqual(classify_seniority(self.make_record("Data Analytics Trainee", ""))[0], "Junior")
        self.assertEqual(classify_seniority(self.make_record("Stage - Data Analyst", ""))[0], "Junior")
        self.assertEqual(
            classify_seniority(self.make_record("Middle Data Analyst", "Growe is a leading business accelerator"))[0],
            "Mid-level",
        )
        self.assertEqual(
            classify_seniority(self.make_record("Senior Data Analyst", "You will mentor junior analysts"))[0], "Senior"
        )

    def test_seniority_reads_the_posting_describing_its_own_level(self) -> None:
        cases = (
            ("Analytics Engineer", "We're hiring a senior product data analyst to own", "Senior"),
            ("Data Analyst (Marketing Analytics)", "Ми шукаємо data analyst рівня middle+", "Mid-level"),
            ("Data Analyst - Global Markets", "New grads are welcome!", "Junior"),
            ("Summer Associate", "a 12-week internship program beginning in May 2027", "Junior"),
            ("Data Analyst", "stage de 6 mois à Paris", "Junior"),
        )
        for title, text, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(classify_seniority(self.make_record(title, text))[0], expected)

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
