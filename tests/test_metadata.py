from __future__ import annotations

import unittest

from datetime import date, datetime

from core.metadata import (
    parse_date,
    resolve_relative_date,
    extract_location_from_text,
    extract_salary,
    has_disallowed_work_format,
    infer_country,
    infer_work_format,
    is_low_quality_listing,
    is_overqualified,
    matches_context,
    normalize_salary_usd,
    query_matches,
    requires_french,
    requires_german,
    requires_onsite_in_disallowed_country,
    split_city_region,
)


class MetadataTests(unittest.TestCase):
    def test_work_format_supports_english_and_french(self) -> None:
        self.assertEqual(infer_work_format("Poste hybride avec télétravail partiel"), "Hybrid")
        self.assertEqual(infer_work_format("Télétravail possible"), "Remote")
        self.assertEqual(infer_work_format("Office-based role"), "On-site")

    def test_work_format_detects_partial_office_attendance_phrasing(self) -> None:
        self.assertEqual(infer_work_format("2 to 3 days at the office for this role"), "Hybrid")
        self.assertEqual(infer_work_format("3 days a week in office"), "Hybrid")
        self.assertEqual(infer_work_format("2 jours par semaine au bureau"), "Hybrid")

    def test_work_format_detects_remote_first_with_mandatory_in_person_gatherings(self) -> None:
        text = (
            "We are a remote-first, but not remote-only company. Expect to get "
            "together quarterly for intense in-person working sessions called “surges”."
        )
        self.assertEqual(infer_work_format(text), "Hybrid")
        self.assertEqual(infer_work_format("We are a fully remote-first company"), "Remote")

    def test_work_format_detects_office_anchored_phrasing_without_a_remote_hybrid_word(self) -> None:
        # Regression: DRW-style postings that never say "remote"/"hybrid" but
        # anchor the role to a physical office used to fall through to
        # "Unknown" and slip past the disallowed-work-format filter.
        self.assertEqual(
            infer_work_format("Our Real-time Strategy team at the Montreal office is looking for a candidate."),
            "On-site",
        )
        self.assertEqual(infer_work_format("Join our team at our London office."), "On-site")
        # "home office" describes remote-work equipment, not a company site.
        self.assertEqual(infer_work_format("Set up your home office and get to work."), "Unknown")

    def test_work_format_detects_ukrainian_and_russian_phrasing(self) -> None:
        self.assertEqual(infer_work_format("Формат роботи: віддалено"), "Remote")
        self.assertEqual(infer_work_format("Можлива дистанційна робота"), "Remote")
        self.assertEqual(infer_work_format("Удалённая работа по всей Украине"), "Remote")
        self.assertEqual(infer_work_format("Гібридний формат: 2 дні в офісі"), "Hybrid")
        self.assertEqual(infer_work_format("Робота в офісі, Київ"), "On-site")

    def test_work_format_detects_bare_ukrainian_office_hybrid_nouns(self) -> None:
        # Regression: djinni_e11a272fdef1 ("Data Analyst" at VSK, "Київ, офіс
        # / гібрид") slipped past requires_onsite_in_disallowed_country because
        # the old patterns only matched the adjective "гібридний"/phrases like
        # "в офісі", not the bare nouns "офіс"/"гібрид" used here.
        self.assertEqual(infer_work_format("Київ, офіс / гібрид"), "Hybrid")
        self.assertEqual(infer_work_format("Київ, офіс"), "On-site")
        self.assertEqual(infer_work_format("Москва, офис"), "On-site")

    def test_salary_is_extracted_from_structured_data_and_text(self) -> None:
        self.assertEqual(extract_salary({"minValue": 45000, "maxValue": 55000, "currency": "EUR"}), "45000-55000 EUR")
        self.assertEqual(extract_salary({}, "Salary: 45 000 - 55 000 € per year"), "45 000 - 55 000 € per year")

    def test_salary_range_keeps_currency_symbol_repeated_on_each_bound(self) -> None:
        self.assertEqual(extract_salary({}, "Payout: $80 - $90/hour"), "$80 - $90/hour")

    def test_salary_extracts_ukrainian_hryvnia(self) -> None:
        self.assertEqual(extract_salary({}, "Зарплата: 30 000 – 40 000 грн"), "30 000 – 40 000 грн")
        self.assertEqual(extract_salary({}, "Оплата 25 000 ₴"), "25 000 ₴")

    def test_extract_salary_reads_djinni_thin_space_and_range_formats(self) -> None:
        # Regression (2026-09-30): real Djinni text uses a thin space (U+2009)
        # as the thousands separator, a Unicode minus (U+2212) or "від ... до"
        # for ranges. All of these used to come out as "000 грн".
        self.assertEqual(extract_salary({}, "Зарплата 50 000−75 000 грн"), "50 000−75 000 грн")
        self.assertEqual(extract_salary({}, "від 40 000 до\xa045 000 грн."), "40 000 до 45 000 грн.")
        self.assertEqual(extract_salary({}, "Оклад 40000 грн"), "40000 грн")
        self.assertEqual(normalize_salary_usd("50 000−75 000 грн"), "≈1,200-1,800 USD")

    def test_salary_reads_nested_quantitative_value_with_unit(self) -> None:
        structured = {"currency": "USD", "value": {"minValue": 80, "maxValue": 90, "unitText": "HOUR"}}
        self.assertEqual(extract_salary(structured), "80-90 USD/hour")

    def test_salary_extracts_comma_thousands_separator(self) -> None:
        # Regression: US-style "$80,000" used to cut short at "$80,00" because
        # comma was only recognized as a *decimal* separator (1-2 digits), not
        # a thousands separator (3 digits) - found 2026-09-08 while writing a
        # Greenhouse fixture test.
        self.assertEqual(extract_salary({}, "Salary: $80,000 - $95,000 per year"), "$80,000 - $95,000 per year")
        self.assertEqual(extract_salary({}, "Base pay: 120,000 USD"), "120,000 USD")

    def test_salary_comma_as_decimal_separator_still_works(self) -> None:
        # A trailing 1-2 digit comma group is still read as a European-style
        # decimal, not misparsed as a partial/invalid thousands group.
        self.assertEqual(extract_salary({}, "Taux horaire: 45,50 € par heure"), "45,50 € par heure")

    def test_matches_context_finds_any_configured_term_case_insensitively(self) -> None:
        terms = ["crypto", "blockchain", "market data"]
        self.assertTrue(matches_context("Senior Crypto Data Analyst", terms))
        self.assertTrue(matches_context("Works with on-chain Market Data feeds", terms))
        self.assertFalse(matches_context("Contrôleur de Gestion & Analyste Data", terms))
        self.assertFalse(matches_context("", terms))

    def test_overqualified_detects_senior_title_phd_and_years(self) -> None:
        exclusion = {
            "title_markers": ["senior", "lead"],
            "text_markers": ["phd", "advanced python"],
            "min_years_experience": 5,
        }
        self.assertTrue(is_overqualified("Senior Data Analyst", "", exclusion))
        self.assertTrue(is_overqualified("Statistician", "PhD in Statistics required", exclusion))
        self.assertTrue(is_overqualified("Quant", "Advanced Python coding skills required", exclusion))
        self.assertTrue(is_overqualified("Analyst", "Minimum 7+ years of experience", exclusion))
        self.assertFalse(is_overqualified("Junior Data Analyst", "3+ years is a plus", exclusion))
        self.assertFalse(is_overqualified("Data Analyst", "Works closely with the senior manager", exclusion))

    def test_overqualified_catches_year_ranges_and_plain_phrasing(self) -> None:
        exclusion = {"min_years_experience": 5}
        self.assertTrue(is_overqualified("Data Analyst", "Requires 5-7 years of similar experience", exclusion))
        self.assertTrue(is_overqualified("Data Analyst", "5 years of experience in a similar role", exclusion))
        self.assertFalse(is_overqualified("Junior Data Analyst", "2-3 years of experience welcome", exclusion))

    def test_overqualified_uses_the_lower_bound_of_a_year_range(self) -> None:
        # Regression: "3-5 years" was flagged as needing 5 years (the range's
        # upper bound), rejecting postings whose real, lower bar (3) was
        # actually within reach.
        exclusion = {"min_years_experience": 5}
        self.assertFalse(is_overqualified("Data Analyst", "Requires 3-5 years of relevant experience", exclusion))

    def test_overqualified_detects_manager_title(self) -> None:
        exclusion = {"title_markers": ["manager"]}
        self.assertTrue(is_overqualified("Sr. Business Analyst Manager", "", exclusion))
        self.assertFalse(is_overqualified("Data Analyst", "Works with the account manager", exclusion))

    def test_overqualified_ignores_unrelated_years_mentions(self) -> None:
        exclusion = {"min_years_experience": 5}
        self.assertFalse(is_overqualified("Data Analyst", "Societe Generale, founded over 50 years ago", exclusion))
        self.assertFalse(is_overqualified("Data Analyst", "52 people clicked apply, 10 years running this event", exclusion))

    def test_overqualified_ignores_calendar_years(self) -> None:
        # Phrases from jobs_master (2026-10-05) that the analyzer read as years
        # of experience. This filter needs the word "years" after the number,
        # so it never read them; kept as a guard.
        exclusion = {"min_years_experience": 3}
        for text in (
            "Wintermute was founded in 2017 and has successfully navigated",
            "founded in 2012 and headquartered in",
            "Best Workplaces in 2025 and recognized",
            "Winter Intern 2027 - Analytics & Reporting",
            "beginning in May 2027 and ending in August 2027",
            "our Global 2000 and Fortune 500 customers",
            "Founded in 2019 and fully distributed",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_overqualified("Data Analyst", text, exclusion))

    def test_overqualified_ignores_implausible_year_counts(self) -> None:
        # Regression: a bare \d{1,2} read the tail of "150" as 50 years, and
        # nothing capped a company's age, so both rejected the posting.
        exclusion = {"min_years_experience": 3}
        self.assertFalse(is_overqualified("Data Analyst", "Our team has 150+ years of combined experience", exclusion))
        self.assertFalse(is_overqualified("Data Analyst", "35 years of experience serving banks", exclusion))
        self.assertTrue(
            is_overqualified("Data Analyst", "150+ years of combined experience. You have 4+ years of experience", exclusion)
        )

    def test_requires_french_detects_explicit_phrase_and_language_density(self) -> None:
        exclusion = {
            "french_phrases": ["français courant", "fluent in french"],
            "french_word_markers": ["vous", "notre", "poste", "équipe", "société"],
            "french_word_marker_threshold": 3,
        }
        self.assertTrue(requires_french("Data Analyst", "Ce poste requiert un français courant", exclusion))
        self.assertTrue(requires_french(
            "Analyste Data",
            "Notre société recherche un candidat pour rejoindre notre équipe. Vous serez en charge du poste.",
            exclusion,
        ))
        self.assertFalse(requires_french("Data Analyst", "Fully remote role, English only, join our team", exclusion))

    def test_requires_french_gender_marker_does_not_match_inside_english_slash_french(self) -> None:
        exclusion = {
            "french_phrases": ["h/f", "f/h"],
            "french_word_markers": ["vous", "notre", "poste"],
            "french_word_marker_threshold": 3,
        }
        self.assertFalse(requires_french("Process Analyst", "Bilingual (English/French) preferred", exclusion))
        self.assertTrue(requires_french("Data Analyst (H/F)", "", exclusion))

    def test_requires_german_detects_explicit_phrase_and_language_density(self) -> None:
        exclusion = {
            "german_phrases": ["fließend deutsch", "fluent in german"],
            "german_word_markers": ["und", "wir", "unsere", "mitarbeiter", "unternehmen"],
            "german_word_marker_threshold": 3,
        }
        self.assertTrue(requires_german("Data Analyst", "Fließend Deutsch und Englisch erforderlich", exclusion))
        self.assertTrue(requires_german(
            "Analyst (m/w/d)",
            "Unser Unternehmen sucht Mitarbeiter für unsere Abteilung und unser Team.",
            exclusion,
        ))
        self.assertFalse(requires_german("Data Analyst", "Fully remote role, English only, join our team", exclusion))

    def test_requires_german_gender_marker_requires_exact_token(self) -> None:
        exclusion = {
            "german_phrases": ["m/w/d"],
            "german_word_markers": ["und", "wir", "unsere"],
            "german_word_marker_threshold": 3,
        }
        self.assertTrue(requires_german("Data Analyst (m/w/d)", "", exclusion))
        # "wire"/"wird" contain "wir" as a substring but not as a whole word —
        # the word-boundary check must not count them as a marker hit.
        self.assertFalse(requires_german("Data Analyst", "We wire funds and the process wird abgeschlossen", exclusion))

    def test_disallowed_work_format_blocks_hybrid_and_onsite_only(self) -> None:
        exclusion = {"disallowed_work_formats": ["Hybrid", "On-site"]}
        self.assertTrue(has_disallowed_work_format("Hybrid", exclusion))
        self.assertTrue(has_disallowed_work_format("On-site", exclusion))
        self.assertFalse(has_disallowed_work_format("Remote", exclusion))
        self.assertFalse(has_disallowed_work_format("", exclusion))
        self.assertFalse(has_disallowed_work_format(None, exclusion))

    def test_onsite_in_disallowed_country_blocks_hybrid_and_onsite_only(self) -> None:
        exclusion = {"disallowed_onsite_countries": ["Ukraine", "India"]}
        self.assertTrue(requires_onsite_in_disallowed_country("Hybrid", "Ukraine", exclusion))
        self.assertTrue(requires_onsite_in_disallowed_country("On-site", "India", exclusion))
        self.assertFalse(requires_onsite_in_disallowed_country("Remote", "Ukraine", exclusion))

    def test_onsite_in_disallowed_country_catches_ukrainian_office_hybrid_posting(self) -> None:
        # End-to-end regression for djinni_e11a272fdef1 (VSK "Data Analyst",
        # "Київ, офіс / гібрид"): infer_work_format now reads the Ukrainian
        # location text as Hybrid, and country="Ukraine" (Djinni collector
        # hardcodes it) is a disallowed onsite country, so the pair should
        # be caught together.
        exclusion = {"disallowed_onsite_countries": ["Ukraine", "India"]}
        work_format = infer_work_format("Київ, офіс / гібрид")
        self.assertEqual(work_format, "Hybrid")
        self.assertTrue(requires_onsite_in_disallowed_country(work_format, "Ukraine", exclusion))
        self.assertFalse(requires_onsite_in_disallowed_country("Hybrid", "France", exclusion))
        self.assertFalse(requires_onsite_in_disallowed_country("", "Ukraine", exclusion))
        self.assertFalse(requires_onsite_in_disallowed_country("Hybrid", "", exclusion))

    def test_onsite_in_disallowed_country_is_a_noop_when_unconfigured(self) -> None:
        self.assertFalse(requires_onsite_in_disallowed_country("Hybrid", "Ukraine", {}))

    def test_low_quality_listing_detects_scam_lead_funnel_phrasing(self) -> None:
        exclusion = {"low_quality_phrases": ["no experience needed", "learn to trade"]}
        self.assertTrue(is_low_quality_listing("Trader", "No experience needed, we train you from scratch", exclusion))
        self.assertTrue(is_low_quality_listing("Analyst", "Learn to Trade with our mentors", exclusion))
        self.assertFalse(is_low_quality_listing("Data Analyst", "3+ years of SQL and Python experience", exclusion))

    def test_low_quality_listing_is_a_noop_when_unconfigured(self) -> None:
        self.assertFalse(is_low_quality_listing("Trader", "No experience needed", {}))

    def test_low_quality_listing_catches_real_scam_style_posting(self) -> None:
        # Regression: a real RemoteOK posting ("Empire Assets") slipped past
        # the initial phrase list (2026-09-08) because it said "Learning from
        # scratch" and "curiosity is more important than prior experience"
        # rather than the exact phrases first seeded.
        exclusion = {
            "low_quality_phrases": [
                "learning from scratch",
                "curiosity is more important than",
                "convenient to combine with study",
            ]
        }
        text = (
            "Learning from scratch â all processes and trading tools are mastered with a mentor. "
            "Genuine interest in crypto markets is enough, curiosity is more important than prior experience. "
            "Flexible schedule, convenient to combine with study or main activity."
        )
        self.assertTrue(is_low_quality_listing("Junior Crypto Analyst & Trader", text, exclusion))

    def test_query_matches_phrase_or_all_words(self) -> None:
        self.assertTrue(query_matches("Trading Analyst", "Senior Trading Analyst"))
        self.assertTrue(query_matches("Market Data", "Analyst, market data platform"))
        self.assertFalse(query_matches("Risk Analyst", "Product Manager"))

    def test_query_matches_only_the_title_not_the_full_description(self) -> None:
        # Regression: a long JD mentioning "research" and "analyst" somewhere
        # unrelated used to falsely match the "Research Analyst" query and
        # mis-tag e.g. a Coinbase "Derivative Sales Analyst" as category "data".
        self.assertFalse(query_matches("Research Analyst", "Derivative Sales Analyst"))
        self.assertFalse(query_matches("Fraud Analyst", "Internal Audit Analyst"))

    def test_query_matches_rejects_unrelated_linkedin_recommended_titles(self) -> None:
        # Regression: LinkedIn's search results page mixes in "similar jobs"
        # unrelated to the searched query (e.g. searching "Business Analyst"
        # returned "Medical Writer (Remote)", "Solution Architect", "Quantum
        # Machine Learning Engineer") and nothing checked their title before.
        self.assertFalse(query_matches("Business Analyst", "Medical Writer (Remote)"))
        self.assertFalse(query_matches("Data Analyst", "Quantum Machine Learning Engineer"))
        self.assertFalse(query_matches("Research Analyst", "Solution Architect (Europe - Remote)"))

    def test_country_is_extracted_from_ats_location_labels(self) -> None:
        self.assertEqual(infer_country("Remote - USA"), "USA")
        self.assertEqual(infer_country("London/New York"), "United Kingdom / United States")
        self.assertEqual(infer_country("Taiwan (Remote)"), "Taiwan")

    def test_country_is_extracted_from_bare_country_name(self) -> None:
        # Regression: LinkedIn often shows just a country name with no city
        # for remote postings ("Spain", "Colombia") - these used to leave
        # `country` empty since the old logic only split on a comma.
        self.assertEqual(infer_country("Spain"), "Spain")
        self.assertEqual(infer_country("Colombia"), "Colombia")
        self.assertEqual(infer_country("United Arab Emirates"), "United Arab Emirates")

    def test_extract_location_from_text_recovers_a_stated_location_line(self) -> None:
        self.assertEqual(
            extract_location_from_text("Some intro.\n🌐 Location: Remote\nMore text."),
            "Remote",
        )
        self.assertEqual(
            extract_location_from_text("📍 Location: Paris, France"),
            "Paris, France",
        )
        self.assertEqual(extract_location_from_text("No location line here."), "")

    def test_normalize_salary_usd_converts_known_currencies(self) -> None:
        self.assertEqual(normalize_salary_usd("80000-95000 EUR/year"), "≈86,400-102,600 USD")
        self.assertEqual(normalize_salary_usd("30 000 – 40 000 грн"), "≈720-960 USD")
        self.assertEqual(normalize_salary_usd("£50,000/year"), "≈63,500 USD")

    def test_normalize_salary_usd_reads_european_decimal_comma(self) -> None:
        # Regression (2026-10-05): a posting by Example Italia (name made up)
        # gave "30.000,00 €", stored as "≈3,240,000 USD" because every
        # separator was stripped as thousands.
        self.assertEqual(normalize_salary_usd("30.000,00 €"), "≈32,400 USD")
        self.assertEqual(normalize_salary_usd("€11,50/hour"), "≈12 USD")
        # Dot-thousands without decimals and US-style decimals still parse.
        self.assertEqual(normalize_salary_usd("120.000 EUR"), "≈129,600 USD")
        self.assertEqual(normalize_salary_usd("€5.000–€7.000"), "≈5,400-7,600 USD")
        self.assertEqual(normalize_salary_usd("35.000€"), "≈37,800 USD")
        self.assertEqual(normalize_salary_usd("45,000.00 EUR"), "≈48,600 USD")

    def test_normalize_salary_usd_keeps_small_amounts_meaningful(self) -> None:
        # Regression (2026-10-05): rounding to hundreds stored "30 EUR/month"
        # as "≈0 USD" and "500€" as "≈500 USD".
        self.assertEqual(normalize_salary_usd("30 EUR/month"), "≈32 USD")
        self.assertEqual(normalize_salary_usd("80 EUR"), "≈86 USD")
        self.assertEqual(normalize_salary_usd("25 GBP"), "≈32 USD")
        self.assertEqual(normalize_salary_usd("500€"), "≈540 USD")
        self.assertEqual(normalize_salary_usd("EUR 600/month"), "≈650 USD")
        # Both ends of a range share the smaller bound's rounding step.
        self.assertEqual(normalize_salary_usd("40 000 до 45 000 грн."), "≈960-1,080 USD")

    def test_normalize_salary_usd_skips_already_usd_or_unrecognized(self) -> None:
        self.assertEqual(normalize_salary_usd("$80,000 - $95,000"), "")
        self.assertEqual(normalize_salary_usd(""), "")
        self.assertEqual(normalize_salary_usd("Competitive salary"), "")

    def test_split_city_region_drops_trailing_country_duplicate(self) -> None:
        self.assertEqual(split_city_region("Paris, Ile-de-France, FR", "FR"), ("Paris", "Ile-de-France"))

    def test_split_city_region_handles_country_only_value(self) -> None:
        self.assertEqual(split_city_region("Ukraine", "Ukraine"), ("", ""))

    def test_split_city_region_handles_single_segment(self) -> None:
        self.assertEqual(split_city_region("Remote", ""), ("Remote", ""))

    def test_split_city_region_handles_empty_value(self) -> None:
        self.assertEqual(split_city_region("", "France"), ("", ""))



class PublishDateTests(unittest.TestCase):
    def test_relative_linkedin_age_is_pinned_to_a_date(self) -> None:
        now = datetime(2026, 10, 5, 12, 0)
        self.assertEqual(resolve_relative_date("2 days ago", now), "2026-10-03")
        self.assertEqual(resolve_relative_date("1 week ago", now), "2026-09-28")
        # An hour before 00:30 is the previous day.
        self.assertEqual(resolve_relative_date("1 hour ago", datetime(2026, 10, 5, 0, 30)), "2026-10-04")
        self.assertEqual(resolve_relative_date("Reposted 11 minutes ago", now), "Reposted 2026-10-05")

    def test_text_without_an_age_is_left_alone(self) -> None:
        now = datetime(2026, 10, 5)
        self.assertEqual(resolve_relative_date("2026-08-01T10:00:00Z", now), "2026-08-01T10:00:00Z")
        self.assertEqual(resolve_relative_date("", now), "")

    def test_parse_date_reads_every_source_format(self) -> None:
        self.assertEqual(parse_date("2026-09-19T04:02:55+00:00"), date(2026, 9, 19))
        self.assertEqual(parse_date("Fri, 02 Oct 2026 15:31:05"), date(2026, 10, 2))
        self.assertEqual(parse_date("Reposted 2026-10-03"), date(2026, 10, 3))
        self.assertEqual(parse_date("2026-10-05 00:00:00"), date(2026, 10, 5))

    def test_parse_date_gives_none_for_unknown_text(self) -> None:
        for value in ("", None, "3 days ago", "soon", "2026-13-45"):
            self.assertIsNone(parse_date(value), value)


if __name__ == "__main__":
    unittest.main()
