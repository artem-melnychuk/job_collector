"""Jobicy collector — remote job board, via its public JSON API.

Plain HTTP GET + JSON (https://jobicy.com/api/v2/remote-jobs), no browser
rendering, same reliability tier as remoteok.py. Added 2026-09-30.

Unlike RemoteOK, Jobicy filters server-side, verified live 2026-09-30:
- ``tag`` is a keyword search over the job content (3-50 characters);
- ``geo`` restricts to postings open to applicants from one location, and
  ``geo=france`` returns exactly the postings whose eligibility includes
  France (``Europe``, ``EMEA``, ``Anywhere``, lists that name France). Without
  it, 27 of 50 "data analyst" results were USA-only, which needs US work
  authorization. ``geo=europe`` is broader but pulls in single-country
  postings (UK-only, Poland-only) that usually need local residency.

So the collector sends a few broad tags (default "analyst", "analytics")
with ``geo`` and ``count`` from settings.yaml, then keeps only titles that
match a configured query via query_matches, like remoteok.py. That is 2
requests per run instead of one per query (49): Jobicy's README asks not to
poll more often than once an hour, and the tag search is broad enough that
per-query requests would mostly return the same postings.

Terms (API response ``friendlyNotice`` and README): credit Jobicy with a
link to the source and send applicants to the original job URL. Records
keep Jobicy's canonical ``url`` and say where they came from in ``note``.
Salary is structured (``salaryMin``/``salaryMax``/``salaryCurrency``/
``salaryPeriod``) on about half the postings.
"""

from __future__ import annotations

import asyncio
import html
import random
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext

from collectors.base import BaseCollector, CollectorResult, get_with_retry, setting
from core.ids import build_job_id
from core.metadata import infer_country, query_matches
from core.models import JobRecord


JOBICY_API_URL = "https://jobicy.com/api/v2/remote-jobs"
DEFAULT_TAGS = ("analyst", "analytics")
DEFAULT_GEO = "france"
DEFAULT_COUNT = 100
PERIOD_SUFFIX = {"yearly": "/year", "monthly": "/month", "hourly": "/hour", "weekly": "/week", "daily": "/day"}


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def build_api_url(tag: str, geo: str = DEFAULT_GEO, count: int = DEFAULT_COUNT) -> str:
    params = {"count": count, "tag": tag}
    if geo:
        params["geo"] = geo
    return f"{JOBICY_API_URL}?{urlencode(params)}"


def parse_jobs(payload: Any) -> list[dict[str, Any]]:
    """Return the ``jobs`` list; an empty or malformed payload yields []."""
    if not isinstance(payload, dict):
        return []
    jobs = payload.get("jobs")
    return [job for job in jobs if isinstance(job, dict) and job.get("jobTitle")] if isinstance(jobs, list) else []


def format_salary(job: dict[str, Any]) -> str:
    minimum, maximum = job.get("salaryMin"), job.get("salaryMax")
    if not minimum and not maximum:
        return ""
    amount = f"{minimum}-{maximum}" if minimum and maximum and minimum != maximum else f"{minimum or maximum}"
    currency = clean_text(job.get("salaryCurrency"))
    period = PERIOD_SUFFIX.get(str(job.get("salaryPeriod") or "").casefold(), "")
    return f"{amount} {currency}{period}".strip() if currency else f"{amount}{period}"


def record_from_job(job: dict[str, Any], query: dict[str, str]) -> JobRecord:
    location = clean_text(job.get("jobGeo"))
    description = clean_text(job.get("jobDescription"))
    record = JobRecord(
        source="Jobicy",
        date_collected=datetime.now().astimezone().isoformat(timespec="seconds"),
        date_published=clean_text(job.get("pubDate")),
        title=clean_text(job.get("jobTitle")),
        company=clean_text(job.get("companyName")),
        city_region=location,
        country=infer_country(location),
        work_format="Remote",
        source_work_format="Remote",
        # Structured fields only. A text fallback (as remoteok.py has) turned a
        # "10 EUR" meal allowance and a "USD 2,000" budget line in two
        # descriptions into fake salaries on the first live run (2026-09-30).
        salary=format_salary(job),
        url=clean_text(job.get("url")),
        full_text=description,
        status="collected",
        availability_status="active",
        note="Source: Jobicy public API (jobicy.com); apply via the original posting",
        search_query=query["query"],
        job_category=query["category"],
    )
    record.job_id = build_job_id(record)
    return record


class JobicyCollector(BaseCollector):
    """Collect matching postings from Jobicy's filtered public API."""

    source_name = "Jobicy"

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or {}

    async def collect(self, context: BrowserContext, queries: list[dict[str, str]], limit: int) -> CollectorResult:
        result = CollectorResult(source=self.source_name, queries=[item["query"] for item in queries])
        timeout_ms = int(setting(self.settings, "request", "timeout_seconds", default=60) * 1000)
        retry_attempts = int(setting(self.settings, "request", "retry_attempts", default=0))
        delay_min = float(setting(self.settings, "request", "delay_min_seconds", default=1.0))
        delay_max = float(setting(self.settings, "request", "delay_max_seconds", default=delay_min))
        tags = setting(self.settings, "sources", "jobicy", "tags", default=list(DEFAULT_TAGS))
        geo = str(setting(self.settings, "sources", "jobicy", "geo", default=DEFAULT_GEO) or "")
        count = int(setting(self.settings, "sources", "jobicy", "count", default=DEFAULT_COUNT))
        seen_urls: set[str] = set()

        for index, tag in enumerate(tags):
            if len(result.records) >= limit:
                break
            if index:
                await asyncio.sleep(random.uniform(delay_min, max(delay_min, delay_max)))
            try:
                response = await get_with_retry(
                    context, build_api_url(tag, geo, count), timeout_ms=timeout_ms,
                    retry_attempts=retry_attempts, delay_min_seconds=delay_min, delay_max_seconds=delay_max,
                )
                jobs = parse_jobs(await response.json())
            except Exception as error:
                result.errors += 1
                result.error_messages.append(f"Jobicy tag {tag!r}: {error}")
                continue

            for job in jobs:
                if len(result.records) >= limit:
                    break
                title = clean_text(job.get("jobTitle"))
                query = next((item for item in queries if query_matches(item["query"], title)), None)
                if query is None:
                    continue
                record = record_from_job(job, query)
                if record.url and record.url not in seen_urls:
                    seen_urls.add(record.url)
                    result.records.append(record)

        result.found = len(result.records)
        return result
