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
    has_more_pages,
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


class ErrorResponse:
    ok = False

    def __init__(self, status: int) -> None:
        self.status = status


def payload(jobs: list, total: int | None) -> dict:
    return {"jobs": jobs} if total is None else {"jobs": jobs, "totalCount": total}


class FakeRequest:
    """Serves pages keyed by (q, page); unknown pages are empty. ``total`` is
    sent as totalCount when given."""

    def __init__(self, pages: dict, total: int | None = None) -> None:
        self.pages = pages
        self.total = total
        self.urls: list[str] = []

    async def get(self, url: str, timeout: int) -> FakeResponse:
        self.urls.append(url)
        params = parse_qs(urlsplit(url).query)
        return FakeResponse(payload(self.pages.get((params["q"][0], int(params["page"][0])), []), self.total))


class FakeContext:
    def __init__(self, pages: dict, total: int | None = None) -> None:
        self.request = FakeRequest(pages, total)


class CompanyRequest:
    """Company search: page N holds ``sizes[N - 1]`` jobs (later pages are
    empty); a page in ``fail`` answers that HTTP status instead."""

    def __init__(self, sizes: list[int], total: int | None = None, fail: dict | None = None) -> None:
        self.sizes = sizes
        self.total = total
        self.fail = fail or {}
        self.urls: list[str] = []

    async def get(self, url: str, timeout: int):
        self.urls.append(url)
        page = int(parse_qs(urlsplit(url).query)["page"][0])
        if page in self.fail:
            return ErrorResponse(self.fail[page])
        count = self.sizes[page - 1] if page <= len(self.sizes) else 0
        return FakeResponse(payload([job("X", f"p{page}-{i}") for i in range(count)], self.total))


def fetch_listing(request: CompanyRequest, **kwargs):
    context = FakeContext({})
    context.request = request
    return asyncio.run(fetch_company_job_urls(context, "flex", timeout_ms=1000,
                                              delay_min_seconds=0, delay_max_seconds=0, **kwargs))


QUERIES = [{"query": "Data Analyst", "category": "data"}]
BI_QUERY = [{"query": "BI Analyst", "category": "data"}]
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

    def test_has_more_pages_follows_total_count_not_page_size(self) -> None:
        self.assertTrue(has_more_pages({"totalCount": 308}, 1, 2))
        self.assertFalse(has_more_pages({"totalCount": 308}, 16, 5))
        self.assertFalse(has_more_pages({"totalCount": 40}, 2, PAGE_SIZE))
        self.assertTrue(has_more_pages({}, 1, PAGE_SIZE))
        self.assertFalse(has_more_pages({}, 1, 3))

    def test_fetch_company_job_urls_reads_past_short_pages_until_total_count(self) -> None:
        # Availability check: the company's open postings come from the API
        # (job pages are behind Cloudflare). Regression: Canonical's pages
        # 1-3 held 2, 17 and 15 of 308 on 2026-10-06; the old walk stopped
        # after page 1 and marked its saved posting (on page 12) closed.
        request = CompanyRequest([2, 17, 5], total=45)
        listing = fetch_listing(request)
        self.assertEqual(len(request.urls), 3)
        self.assertEqual(len(listing.urls), 24)
        self.assertTrue(listing.complete)
        self.assertEqual(listing.status_of("https://himalayas.app/companies/flex/jobs/p3-4"), "active")
        self.assertEqual(listing.status_of("https://himalayas.app/companies/flex/jobs/gone"), "closed")

    def test_fetch_company_job_urls_page_limit_leaves_listing_incomplete(self) -> None:
        # Regression: micro1 had 284 postings and the old 10-page cap read
        # 200, so a posting past them came back "closed" while still open.
        request = CompanyRequest([PAGE_SIZE] * 15, total=284)
        listing = fetch_listing(request, max_pages=10)
        self.assertEqual(len(request.urls), 10)
        self.assertEqual(len(listing.urls), 200)
        self.assertFalse(listing.complete)
        self.assertEqual(listing.status_of("https://himalayas.app/companies/flex/jobs/p10-0"), "active")
        self.assertEqual(listing.status_of("https://himalayas.app/companies/flex/jobs/p12-0"), "unknown")

    def test_fetch_company_job_urls_default_limit_covers_a_large_company(self) -> None:
        request = CompanyRequest([PAGE_SIZE] * 14 + [3], total=284)
        listing = fetch_listing(request)
        self.assertEqual(len(request.urls), 15)
        self.assertTrue(listing.complete)

    def test_fetch_company_job_urls_rate_limit_keeps_pages_read(self) -> None:
        request = CompanyRequest([PAGE_SIZE] * 3, total=60, fail={2: 429})
        listing = fetch_listing(request)
        self.assertEqual(len(request.urls), 2)  # page 3 is not asked after the 429
        self.assertEqual(len(listing.urls), PAGE_SIZE)
        self.assertFalse(listing.complete)
        self.assertTrue(listing.rate_limited)
        self.assertIn("429", listing.error)
        self.assertEqual(listing.status_of("https://himalayas.app/companies/flex/jobs/p1-0"), "active")
        self.assertEqual(listing.status_of("https://himalayas.app/companies/flex/jobs/p3-0"), "unknown")

    def test_fetch_company_job_urls_other_failure_is_not_a_rate_limit(self) -> None:
        listing = fetch_listing(CompanyRequest([PAGE_SIZE], total=40, fail={1: 500}))
        self.assertFalse(listing.complete)
        self.assertFalse(listing.rate_limited)
        self.assertEqual(listing.urls, set())

    def test_fetch_company_job_urls_without_total_count_is_incomplete(self) -> None:
        # A short page is only a guess at the end when totalCount is missing.
        request = CompanyRequest([PAGE_SIZE, 3])
        listing = fetch_listing(request)
        self.assertEqual(len(request.urls), 2)
        self.assertEqual(len(listing.urls), PAGE_SIZE + 3)
        self.assertFalse(listing.complete)

    def test_collect_reads_past_short_page_when_total_count_says_more(self) -> None:
        pages = {("analyst", 1): [job("Data Analyst", "p1")], ("analyst", 2): [job("BI Analyst", "p2")]}
        context = FakeContext(pages, total=25)
        result = asyncio.run(HimalayasCollector(SETTINGS).collect(context, QUERIES + BI_QUERY, limit=10))
        self.assertEqual([r.title for r in result.records], ["Data Analyst", "BI Analyst"])
        self.assertEqual(len(context.request.urls), 2)


if __name__ == "__main__":
    unittest.main()
