from __future__ import annotations

import asyncio
import unittest
from urllib.parse import parse_qs, urlsplit

from collectors.jobicy import JobicyCollector, build_api_url, format_salary, parse_jobs, record_from_job


def job(title: str, job_id: int = 1, **extra) -> dict:
    return {
        "id": job_id,
        "url": f"https://jobicy.com/jobs/{job_id}-slug",
        "jobTitle": title,
        "companyName": "Acme",
        "jobGeo": "Europe,  USA",
        "jobDescription": "<p>SQL and <strong>Excel</strong></p>",
        "pubDate": "2026-09-29T15:42:50+00:00",
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
    def __init__(self, payloads_by_tag: dict) -> None:
        self.payloads_by_tag = payloads_by_tag
        self.urls: list[str] = []

    async def get(self, url: str, timeout: int) -> FakeResponse:
        self.urls.append(url)
        tag = parse_qs(urlsplit(url).query)["tag"][0]
        return FakeResponse(self.payloads_by_tag.get(tag, {"jobs": []}))


class FakeContext:
    def __init__(self, payloads_by_tag: dict) -> None:
        self.request = FakeRequest(payloads_by_tag)


QUERIES = [{"query": "Data Analyst", "category": "data"}, {"query": "Analytics Engineer", "category": "data"}]
NO_DELAY = {"request": {"delay_min_seconds": 0, "delay_max_seconds": 0}}


class JobicyCollectorTests(unittest.TestCase):
    def test_build_api_url_sends_tag_geo_and_count(self) -> None:
        params = parse_qs(urlsplit(build_api_url("data analyst", "france", 50)).query)
        self.assertEqual(params, {"count": ["50"], "tag": ["data analyst"], "geo": ["france"]})

    def test_parse_jobs_tolerates_bad_payloads(self) -> None:
        self.assertEqual(parse_jobs(None), [])
        self.assertEqual(parse_jobs({"jobs": "nope"}), [])
        self.assertEqual(len(parse_jobs({"jobs": [job("Data Analyst"), {"id": 2}]})), 1)

    def test_format_salary_includes_currency_and_period(self) -> None:
        self.assertEqual(format_salary({"salaryMin": 72500, "salaryMax": 96500, "salaryCurrency": "USD",
                                        "salaryPeriod": "yearly"}), "72500-96500 USD/year")
        self.assertEqual(format_salary({"salaryMin": 3000, "salaryCurrency": "EUR", "salaryPeriod": "monthly"}),
                         "3000 EUR/month")
        self.assertEqual(format_salary({"salaryMin": None, "salaryMax": None}), "")

    def test_record_from_job_is_remote_and_names_the_source(self) -> None:
        record = record_from_job(job("Data Analyst"), QUERIES[0])
        self.assertEqual(record.source, "Jobicy")
        self.assertEqual(record.work_format, "Remote")
        self.assertEqual(record.city_region, "Europe, USA")  # double space collapsed
        self.assertIn("Excel", record.full_text)
        self.assertIn("jobicy.com", record.note)
        self.assertTrue(record.job_id.startswith("jobicy_"))

    def test_salary_is_not_guessed_from_description_text(self) -> None:
        record = record_from_job(job("Data Analyst", jobDescription="<p>Meal allowance 10 EUR per day</p>"), QUERIES[0])
        self.assertEqual(record.salary, "")

    def test_collect_keeps_matching_titles_once_across_tags(self) -> None:
        # Tag search is broad ("analyst"): "Behavior Analyst" must be dropped
        # by the title check, and a posting found under both tags kept once.
        payloads = {
            "analyst": {"jobs": [job("Senior Data Analyst", 1), job("Board Certified Behavior Analyst", 2)]},
            "analytics": {"jobs": [job("Senior Data Analyst", 1), job("Analytics Engineer", 3)]},
        }
        context = FakeContext(payloads)
        result = asyncio.run(JobicyCollector(NO_DELAY).collect(context, QUERIES, limit=10))
        self.assertEqual([r.title for r in result.records], ["Senior Data Analyst", "Analytics Engineer"])
        self.assertEqual(result.errors, 0)
        self.assertTrue(all("geo=france" in url for url in context.request.urls))

    def test_collect_records_an_error_and_continues(self) -> None:
        class BrokenRequest(FakeRequest):
            async def get(self, url: str, timeout: int):
                if "tag=analyst&" in url or url.endswith("tag=analyst"):
                    raise TimeoutError("boom")
                return await super().get(url, timeout)

        context = FakeContext({"analytics": {"jobs": [job("Analytics Engineer", 3)]}})
        context.request = BrokenRequest(context.request.payloads_by_tag)
        result = asyncio.run(JobicyCollector(NO_DELAY).collect(context, QUERIES, limit=10))
        self.assertEqual(result.errors, 1)
        self.assertEqual([r.title for r in result.records], ["Analytics Engineer"])


if __name__ == "__main__":
    unittest.main()
