from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from pathlib import Path

from playwright.async_api import async_playwright

from collectors.linkedin import (
    DEFAULT_LOCATION,
    build_search_url,
    clean_text,
    detect_availability_status,
    page_requires_login,
    parse_json_ld,
    parse_title_tag,
    record_from_page,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class LinkedInCollectorTests(unittest.TestCase):
    def test_search_url_contains_query_and_location(self) -> None:
        url = build_search_url("Market Data Analyst", "France")

        self.assertIn("keywords=Market+Data+Analyst", url)
        self.assertIn("location=France", url)
        self.assertIn("sortBy=DD", url)

    def test_search_url_has_no_work_type_filter(self) -> None:
        # Regression: was hardcoded to f_WT=2 (Remote only). The candidate is
        # now open to office/hybrid and relocation, so search should not
        # exclude non-remote postings before a human ever sees them.
        url = build_search_url("Data Analyst")

        self.assertNotIn("f_WT", url)
        self.assertIn(f"location={DEFAULT_LOCATION}", url)

    def test_json_ld_parser_accepts_job_posting(self) -> None:
        raw = '{"@type":"JobPosting","title":"Trading Analyst"}'

        self.assertEqual(parse_json_ld(raw)["title"], "Trading Analyst")

    def test_json_ld_parser_ignores_other_schema(self) -> None:
        raw = '{"@type":"Organization","name":"Example"}'

        self.assertIsNone(parse_json_ld(raw))

    def test_login_page_is_detected(self) -> None:
        self.assertTrue(page_requires_login("https://www.linkedin.com/login", ""))
        self.assertTrue(page_requires_login("https://www.linkedin.com/jobs/search", "Sign in to see more jobs"))
        self.assertTrue(page_requires_login("https://www.linkedin.com/connect-services/", "Keep your LinkedIn services connected?"))
        self.assertTrue(page_requires_login("https://www.linkedin.com/connect-services/ads_experience/", "Choose how we use your data for personalized ads"))
        self.assertFalse(page_requires_login("https://www.linkedin.com/jobs/search", "Job results"))

    def test_availability_status_distinguishes_closed_and_active_pages(self) -> None:
        self.assertEqual(
            detect_availability_status(
                "https://www.linkedin.com/jobs/view/123/",
                "This job is no longer available",
            ),
            "closed",
        )
        self.assertEqual(
            detect_availability_status(
                "https://www.linkedin.com/jobs/view/123/",
                "About the job\nApply",
            ),
            "active",
        )
        self.assertEqual(
            detect_availability_status("https://www.linkedin.com/login", "Sign in"),
            "unknown",
        )

    def test_parse_title_tag_splits_job_title_and_company(self) -> None:
        self.assertEqual(parse_title_tag("Statistician | Alimentiv | LinkedIn"), ("Statistician", "Alimentiv"))
        self.assertEqual(parse_title_tag("Data Analyst | LinkedIn"), ("Data Analyst", ""))
        self.assertEqual(parse_title_tag(""), ("", ""))
        self.assertEqual(parse_title_tag("LinkedIn"), ("", ""))

    def test_clean_text_strips_linkedin_premium_upsell_boilerplate(self) -> None:
        # Regression: this widget renders inside the same container the
        # description selectors target and used to leak into full_text,
        # falsely tripping AI/LLM-related skill matches on postings that
        # never mentioned AI at all.
        raw = (
            "About the job\nWe need a Data Analyst.\n"
            "Get AI-powered advice on this job and more exclusive features "
            "with Premium. Try Premium for €0\n"
            "Requirements: SQL, Excel."
        )
        cleaned = clean_text(raw)
        self.assertNotIn("AI-powered advice", cleaned)
        self.assertNotIn("Premium", cleaned)
        self.assertIn("We need a Data Analyst.", cleaned)
        self.assertIn("Requirements: SQL, Excel.", cleaned)

    def test_removed_job_error_page_is_detected_as_closed(self) -> None:
        self.assertEqual(
            detect_availability_status(
                "https://www.linkedin.com/jobs/view/4455407711/",
                "Unable to load the page\nJob id provided may not be valid or the job posting has been removed.",
            ),
            "closed",
        )


class RecordFromPageIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Exercises record_from_page against a real (headless) rendered page.

    Unlike the string/dict-level tests above, these catch breakage in the
    Playwright locator/selector plumbing itself (first_text, json_ld_job),
    which plain unit tests can't reach since they need a live DOM to query.
    """

    async def asyncSetUp(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch()
        self._page = await self._browser.new_page()

    async def asyncTearDown(self) -> None:
        await self._browser.close()
        await self._playwright.stop()

    async def test_record_from_page_uses_json_ld_when_present(self) -> None:
        html = (FIXTURES_DIR / "linkedin_json_ld.html").read_text(encoding="utf-8")
        await self._page.set_content(html)
        record = await record_from_page(
            self._page,
            query={"query": "Data Analyst", "category": "data"},
            url="https://www.linkedin.com/jobs/view/1/",
        )
        self.assertEqual(record.title, "Data Analyst")
        self.assertEqual(record.company, "Acme Corp")
        self.assertEqual(record.city_region, "Berlin, Berlin, Germany")
        self.assertEqual(record.country, "Germany")
        self.assertEqual(record.work_format, "Remote")
        self.assertEqual(record.salary, "45000-55000 EUR/year")
        self.assertIn("We are looking for a Data Analyst.", record.full_text)
        # The Premium upsell boilerplate is embedded in the JSON-LD description
        # itself on real pages, not just the DOM - must be stripped here too.
        self.assertNotIn("Premium", record.full_text)
        self.assertTrue(record.job_id)

    async def test_record_from_page_falls_back_to_css_selectors_without_json_ld(self) -> None:
        html = (FIXTURES_DIR / "linkedin_css_fallback.html").read_text(encoding="utf-8")
        await self._page.set_content(html)
        record = await record_from_page(
            self._page,
            query={"query": "Risk Analyst", "category": "risk"},
            url="https://www.linkedin.com/jobs/view/2/",
        )
        self.assertEqual(record.title, "Risk Analyst")
        self.assertEqual(record.company, "Beta LLC")
        self.assertEqual(record.city_region, "Warsaw, Poland")
        self.assertEqual(record.country, "Poland")
        self.assertEqual(record.work_format, "Hybrid")
        self.assertEqual(record.salary, "80 000 - 95 000 EUR per year")
        self.assertIn("We need a Risk Analyst with strong SQL skills.", record.full_text)
        # "2 days ago" on the page, stored as the date it stands for.
        self.assertEqual(record.date_published, (datetime.now() - timedelta(days=2)).date().isoformat())
