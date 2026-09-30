"""Himalayas collector — remote job board, via its public JSON search API.

Plain HTTP GET + JSON (https://himalayas.app/jobs/api/search), no browser
rendering, same reliability tier as remoteok.py. Added 2026-09-30.

Verified live 2026-09-30 against the API docs
(https://himalayas.app/docs/remote-jobs-api):
- ``country=France`` returns postings open to applicants in France plus
  worldwide ones (an empty ``locationRestrictions`` list means worldwide),
  which is exactly the candidate's eligibility.
- ``q`` is a loose full-text match: "data analyst" also returned "Board
  Certified Behavior Analyst" and "Behavioral Scientist". So, like
  remoteok.py, the collector searches a few broad terms (default "analyst",
  "analytics") and keeps only titles that match a configured query via
  query_matches.
- Pages hold at most 20 jobs; ``page`` is 1-based. The API is rate limited
  (HTTP 429) and its data is refreshed once a day, so the collector reads a
  few pages per term with the usual request delay and no more.

Salary is structured (``minSalary``/``maxSalary``/``currency``/
``salaryPeriod``) on part of the postings. The docs ask anyone displaying
the data to link back to himalayas.app; records keep the Himalayas posting
URL and name the source in ``note``.
"""

from __future__ import annotations

import asyncio
import html
import random
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext

from collectors.base import BaseCollector, CollectorResult, get_with_retry, setting
from core.ids import build_job_id
from core.metadata import query_matches
from core.models import JobRecord


HIMALAYAS_SEARCH_URL = "https://himalayas.app/jobs/api/search"
DEFAULT_TERMS = ("analyst", "analytics")
DEFAULT_COUNTRY = "France"
DEFAULT_MAX_PAGES = 5
PAGE_SIZE = 20
PERIOD_SUFFIX = {"annual": "/year", "yearly": "/year", "monthly": "/month", "hourly": "/hour", "weekly": "/week", "daily": "/day"}
# A posting can list dozens of eligible countries; keep the stored location readable.
MAX_LOCATIONS_SHOWN = 6


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def build_search_url(term: str, country: str = DEFAULT_COUNTRY, page: int = 1) -> str:
    params = {"q": term, "sort": "recent", "page": page}
    if country:
        params["country"] = country
    return f"{HIMALAYAS_SEARCH_URL}?{urlencode(params)}"


def parse_jobs(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    jobs = payload.get("jobs")
    return [job for job in jobs if isinstance(job, dict) and job.get("title")] if isinstance(jobs, list) else []


def format_salary(job: dict[str, Any]) -> str:
    minimum, maximum = job.get("minSalary"), job.get("maxSalary")
    if not minimum and not maximum:
        return ""
    amount = f"{minimum}-{maximum}" if minimum and maximum and minimum != maximum else f"{minimum or maximum}"
    currency = clean_text(job.get("currency"))
    period = PERIOD_SUFFIX.get(str(job.get("salaryPeriod") or "").casefold(), "")
    return f"{amount} {currency}{period}".strip() if currency else f"{amount}{period}"


def format_locations(restrictions: Any) -> tuple[str, str]:
    """Return (city_region, country) from ``locationRestrictions``.

    An empty list means the posting is open worldwide. A single country is
    also the record's country; a longer list is shortened for readability.
    """
    places = [clean_text(place) for place in restrictions or [] if clean_text(place)]
    if not places:
        return "Worldwide", ""
    if len(places) == 1:
        return places[0], places[0]
    shown = ", ".join(places[:MAX_LOCATIONS_SHOWN])
    extra = len(places) - MAX_LOCATIONS_SHOWN
    return (f"{shown} (+{extra} more)" if extra > 0 else shown), ""


def format_pub_date(value: Any) -> str:
    """Himalayas sends Unix seconds; store ISO 8601 like the other sources."""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return clean_text(value)


def record_from_job(job: dict[str, Any], query: dict[str, str]) -> JobRecord:
    city_region, country = format_locations(job.get("locationRestrictions"))
    description = clean_text(job.get("description"))
    record = JobRecord(
        source="Himalayas",
        date_collected=datetime.now().astimezone().isoformat(timespec="seconds"),
        date_published=format_pub_date(job.get("pubDate")),
        title=clean_text(job.get("title")),
        company=clean_text(job.get("companyName")),
        city_region=city_region,
        country=country,
        work_format="Remote",
        source_work_format="Remote",
        contract_type=clean_text(job.get("employmentType")),
        # Structured fields only. A text fallback (as remoteok.py has) turned a
        # "10 EUR" meal allowance and a "USD 2,000" budget line in two
        # descriptions into fake salaries on the first live run (2026-09-30).
        salary=format_salary(job),
        url=clean_text(job.get("guid") or job.get("applicationLink")),
        full_text=description,
        status="collected",
        availability_status="active",
        note="Source: Himalayas public API (himalayas.app)",
        search_query=query["query"],
        job_category=query["category"],
    )
    record.job_id = build_job_id(record)
    return record


class HimalayasCollector(BaseCollector):
    """Collect matching postings from Himalayas' country-filtered search API."""

    source_name = "Himalayas"

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or {}

    async def collect(self, context: BrowserContext, queries: list[dict[str, str]], limit: int) -> CollectorResult:
        result = CollectorResult(source=self.source_name, queries=[item["query"] for item in queries])
        timeout_ms = int(setting(self.settings, "request", "timeout_seconds", default=60) * 1000)
        retry_attempts = int(setting(self.settings, "request", "retry_attempts", default=0))
        delay_min = float(setting(self.settings, "request", "delay_min_seconds", default=1.0))
        delay_max = float(setting(self.settings, "request", "delay_max_seconds", default=delay_min))
        terms = setting(self.settings, "sources", "himalayas", "terms", default=list(DEFAULT_TERMS))
        country = str(setting(self.settings, "sources", "himalayas", "country", default=DEFAULT_COUNTRY) or "")
        max_pages = int(setting(self.settings, "sources", "himalayas", "max_pages", default=DEFAULT_MAX_PAGES))
        seen_urls: set[str] = set()
        first_request = True

        for term in terms:
            for page in range(1, max_pages + 1):
                if len(result.records) >= limit:
                    break
                if not first_request:
                    await asyncio.sleep(random.uniform(delay_min, max(delay_min, delay_max)))
                first_request = False
                try:
                    response = await get_with_retry(
                        context, build_search_url(term, country, page), timeout_ms=timeout_ms,
                        retry_attempts=retry_attempts, delay_min_seconds=delay_min, delay_max_seconds=delay_max,
                    )
                    jobs = parse_jobs(await response.json())
                except Exception as error:
                    result.errors += 1
                    result.error_messages.append(f"Himalayas {term!r} page {page}: {error}")
                    break

                for job in jobs:
                    if len(result.records) >= limit:
                        break
                    title = clean_text(job.get("title"))
                    query = next((item for item in queries if query_matches(item["query"], title)), None)
                    if query is None:
                        continue
                    record = record_from_job(job, query)
                    if record.url and record.url not in seen_urls:
                        seen_urls.add(record.url)
                        result.records.append(record)
                if len(jobs) < PAGE_SIZE:
                    break  # last page of results for this term

        result.found = len(result.records)
        return result
