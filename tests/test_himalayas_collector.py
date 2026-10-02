from __future__ import annotations

import asyncio
import unittest
from urllib.parse import parse_qs, urlsplit

from collectors.himalayas import (
    PAGE_SIZE,
    HimalayasCollector,
    build_search_url,
    company_slug_from_url,
    fetch_company_job_urls,
    format_locations,
    format_pub_date,
    format_salary,
    record_from_job,
)


def job(title: str, slug: str = "a", **extra) -> dict:
    return {
        "title": title,
        "companyName": "Flex Databases",
        "guid": f"https://himalayas.app/companies/flex/jobs/{slug}",
        "description": "<p>Clinical <strong>data</strong></p>",
        "pubDate": 1790746600,
        "locationRestrictions": [],
        "employmentType": "Full Time",
        **extra,
    }


class FakeResponse:
    ok = True
    status = 200

    def __init__(self, payload) -> None:
        self._payload = payload

    async def json(self):
        return self._payload


class FakeRequest:
    """Serves pages keyed by (q, page); unknown pages are empty."""

    def __init__(self, pages: dict) -> None:
        self.pages = pages
        self.urls: list[str] = []

    async def get(self, url: str, timeout: int) -> FakeResponse:
        self.urls.append(url)
        params = parse_qs(urlsplit(url).query)
        return FakeResponse({"jobs": self.pages.get((params["q"][0], int(params["page"][0])), [])})


class FakeContext:
    def __init__(self, pages: dict) -> None:
        self.request = FakeRequest(pages)


QUERIES = [{"query": "Data Analyst", "category": "data"}]
SETTINGS = {"request": {"delay_min_seconds": 0, "delay_max_seconds": 0},
            "sources": {"himalayas": {"terms": ["analyst"], "max_pages": 3}}}


class HimalayasCollectorTests(unittest.TestCase):
    def test_build_search_url_filters_by_country_and_sorts_recent(self) -> None:
        params = parse_qs(urlsplit(build_search_url("analyst", "France", 2)).query)
        self.assertEqual(params, {"q": ["analyst"], "sort": ["recent"], "page": ["2"], "country": ["France"]})

    def test_format_locations_worldwide_single_and_long_lists(self) -> None:
        self.assertEqual(format_locations([]), ("Worldwide", ""))
        self.assertEqual(format_locations(["France"]), ("France", "France"))
        many = [f"Country {i}" for i in range(10)]
        city_region, country = format_locations(many)
        self.assertTrue(city_region.endswith("(+4 more)"))
        self.assertEqual(country, "")

    def test_format_salary_and_pub_date(self) -> None:
        self.assertEqual(format_salary({"minSalary": 2000, "maxSalary": 3000, "currency": "EUR",
                                        "salaryPeriod": "monthly"}), "2000-3000 EUR/month")
        self.assertEqual(format_salary({"minSalary": 50000, "maxSalary": 70000, "currency": "EUR",
                                        "salaryPeriod": "annual"}), "50000-70000 EUR/year")
        self.assertEqual(format_salary({"minSalary": None, "maxSalary": None, "currency": "USD"}), "")
        self.assertEqual(format_pub_date(1790746600), "2026-09-30T05:36:40+00:00")
        self.assertEqual(format_pub_date("n/a"), "n/a")

    def test_record_from_job_fields(self) -> None:
        record = record_from_job(job("Data Analyst", minSalary=2000, maxSalary=3000, currency="EUR",
                                     salaryPeriod="monthly"), QUERIES[0])
        self.assertEqual(record.source, "Himalayas")
        self.assertEqual(record.work_format, "Remote")
        self.assertEqual(record.city_region, "Worldwide")
        self.assertEqual(record.salary, "2000-3000 EUR/month")
        self.assertEqual(record.contract_type, "Full Time")
        self.assertIn("himalayas.app", record.note)
        self.assertTrue(record.job_id.startswith("himalayas_"))

    def test_collect_filters_titles_and_stops_after_short_page(self) -> None:
        # q is a loose match: "Behavior Analyst" must be dropped by the title
        # check. Page 1 is full, page 2 short, so page 3 is never requested.
        full_page = [job("Data Analyst", "p1-0")] + [job("Behavior Analyst", f"p1-{i}") for i in range(1, PAGE_SIZE)]
        pages = {("analyst", 1): full_page, ("analyst", 2): [job("Senior Data Analyst", "p2")]}
        context = FakeContext(pages)
        result = asyncio.run(HimalayasCollector(SETTINGS).collect(context, QUERIES, limit=10))
        self.assertEqual([r.title for r in result.records], ["Data Analyst", "Senior Data Analyst"])
        self.assertEqual(len(context.request.urls), 2)
        self.assertTrue(all("country=France" in url for url in context.request.urls))


    def test_company_slug_from_url(self) -> None:
        self.assertEqual(company_slug_from_url("https://himalayas.app/companies/iwconnect/jobs/b2b-analyst"), "iwconnect")
        self.assertEqual(company_slug_from_url("https://example.com/jobs/1"), "")

    def test_fetch_company_job_urls_pages_until_short_page(self) -> None:
        # Availability check: the company's open postings come from the API
        # (job pages are behind Cloudflare). Full first page, short second.
        class CompanyRequest:
            def __init__(self) -> None:
                self.urls: list[str] = []

            async def get(self, url: str, timeout: int) -> FakeResponse:
                self.urls.append(url)
                page = int(parse_qs(urlsplit(url).query)["page"][0])
                count = PAGE_SIZE if page == 1 else 3
                jobs = [job("X", f"p{page}-{i}") for i in range(count)]
                return FakeResponse({"jobs": jobs})

        context = FakeContext({})
        context.request = CompanyRequest()
        urls = asyncio.run(fetch_company_job_urls(context, "flex", timeout_ms=1000,
                                                  delay_min_seconds=0, delay_max_seconds=0))
        self.assertEqual(len(urls), PAGE_SIZE + 3)
        self.assertIn("https://himalayas.app/companies/flex/jobs/p2-0", urls)
        self.assertEqual(len(context.request.urls), 2)


if __name__ == "__main__":
    unittest.main()
