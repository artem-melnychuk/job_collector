from __future__ import annotations

import unittest

from core.cross_dedup import assign_duplicate_groups, normalize_company, normalize_title
from core.models import JobRecord


def record(job_id: str, company: str, title: str, city_region: str = "", country: str = "", source: str = "X") -> JobRecord:
    return JobRecord(job_id=job_id, source=source, company=company, title=title,
                     city_region=city_region, country=country, url=f"https://example.com/{job_id}")


def groups(records: list[JobRecord]) -> dict[str, str]:
    return {r.job_id: r.duplicate_group for r in assign_duplicate_groups(records)}


class NormalizationTests(unittest.TestCase):
    def test_company_drops_legal_suffixes_parent_and_camel_case(self) -> None:
        self.assertEqual(normalize_company("CollectlyInc"), "collectly")
        self.assertEqual(normalize_company("Collectly, Inc."), "collectly")
        self.assertEqual(normalize_company("Cumberland (DRW)"), "cumberland")
        self.assertEqual(normalize_company("Inc"), "inc")  # never normalizes a name to nothing

    def test_title_ignores_case_punctuation_and_remote_tag(self) -> None:
        self.assertEqual(normalize_title("Data Analyst ( Remote )"), normalize_title("data analyst"))
        self.assertEqual(normalize_title("BI & Data Analyst"), "bi and data analyst")


class AssignDuplicateGroupsTests(unittest.TestCase):
    def test_same_job_on_two_boards_is_grouped_under_smallest_job_id(self) -> None:
        # Real case 2026-10-02: Canonical's posting on Jobicy and Himalayas.
        result = groups([
            record("jobicy_b", "Canonical", "Python Engineer - Data", "Anywhere", source="Jobicy"),
            record("himalayas_a", "Canonical", "Python Engineer - Data", "Worldwide", source="Himalayas"),
        ])
        self.assertEqual(result, {"jobicy_b": "himalayas_a", "himalayas_a": "himalayas_a"})

    def test_same_title_in_different_cities_is_not_grouped(self) -> None:
        # Coinbase "Payments Risk Analyst II" exists for two different places.
        result = groups([
            record("a", "Coinbase", "Payments Risk Analyst II", "Remote - USA"),
            record("b", "Coinbase", "Payments Risk Analyst II", "Hyderabad, India"),
        ])
        self.assertEqual(result, {"a": "", "b": ""})

    def test_blank_location_does_not_match_a_named_place(self) -> None:
        # Blank means unknown; a wrong merge would hide a real posting.
        self.assertEqual(groups([record("a", "Acme", "Data Analyst", "Canada"), record("b", "Acme", "Data Analyst")]),
                         {"a": "", "b": ""})
        self.assertEqual(groups([record("a", "Acme", "Data Analyst"), record("b", "Acme", "Data Analyst")]),
                         {"a": "a", "b": "a"})

    def test_region_words_match_a_specific_place(self) -> None:
        result = groups([record("a", "Acme", "Data Analyst", "Europe"), record("b", "Acme", "Data Analyst", "France")])
        self.assertEqual(result, {"a": "a", "b": "a"})

    def test_a_vague_posting_cannot_chain_two_incompatible_ones(self) -> None:
        # "Europe" fits both France and Germany, but France and Germany are
        # different postings, so Germany must not join the France group.
        result = groups([
            record("a", "Acme", "Data Analyst", "Europe"),
            record("b", "Acme", "Data Analyst", "France"),
            record("c", "Acme", "Data Analyst", "Germany"),
        ])
        self.assertEqual(result["a"], "a")
        self.assertEqual(result["b"], "a")
        self.assertEqual(result["c"], "")

    def test_different_titles_or_companies_are_not_grouped(self) -> None:
        result = groups([
            record("a", "Acme", "Data Analyst"),
            record("b", "Acme", "Senior Data Analyst"),
            record("c", "Acme Labs", "Data Analyst"),
        ])
        self.assertEqual(set(result.values()), {""})


if __name__ == "__main__":
    unittest.main()
