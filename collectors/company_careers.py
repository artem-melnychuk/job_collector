"""Collectors for public Greenhouse and Lever company career boards."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any
from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext

from collectors.base import BaseCollector, CollectorResult, get_with_retry, setting
from core.metadata import extract_salary, infer_country, infer_work_format, query_matches
from core.ids import build_job_id
from core.models import JobRecord


GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
LEVER_API = "{host}/v0/postings/{slug}?mode=json"
DEFAULT_LEVER_HOST = "https://api.lever.co"


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def build_greenhouse_url(slug: str) -> str:
    return GREENHOUSE_API.format(slug=slug.strip())


def build_lever_url(slug: str, host: str = DEFAULT_LEVER_HOST) -> str:
    return LEVER_API.format(host=host.strip().rstrip("/"), slug=slug.strip())


def _as_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def parse_greenhouse_jobs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return _as_list(payload.get("jobs"))


def parse_lever_jobs(payload: Any) -> list[dict[str, Any]]:
    return _as_list(payload)


def _location_text(value: Any) -> str:
    if isinstance(value, dict):
        return clean_text(value.get("name") or value.get("location") or value.get("city"))
    if isinstance(value, list):
        return " / ".join(dict.fromkeys(_location_text(item) for item in value if _location_text(item)))
    return clean_text(value)


def _country(location: str) -> str:
    return infer_country(location) or (location.rsplit(",", 1)[-1].strip() if "," in location else "")


def _work_format(text: str) -> str:
    return infer_work_format(text)


def configured_career_boards(companies: dict[str, Any]) -> list[dict[str, str]]:
    """Read explicit ATS boards while keeping the old priority lists compatible."""
    boards = companies.get("career_boards", [])
    if not isinstance(boards, list):
        raise ValueError("companies.yaml: career_boards must be a list")
    result = []
    for board in boards:
        if not isinstance(board, dict):
            continue
        company = clean_text(board.get("company"))
        ats = clean_text(board.get("ats")).casefold()
        slug = clean_text(board.get("slug"))
        host = clean_text(board.get("host")) or DEFAULT_LEVER_HOST
        if company and ats in {"greenhouse", "lever"} and slug:
            result.append({"company": company, "ats": ats, "slug": slug, "host": host})
    return result


def max_jobs_per_board(companies: dict[str, Any]) -> int:
    """Return a safe per-company cap so early boards cannot exhaust the run."""
    try:
        value = int(companies.get("max_jobs_per_board", 10))
    except (TypeError, ValueError):
        value = 10
    return max(value, 1)


def _greenhouse_record(job: dict[str, Any], board: dict[str, str], query: dict[str, str]) -> JobRecord:
    location = _location_text(job.get("location"))
    description = clean_text(job.get("content"))
    url = clean_text(job.get("absolute_url"))
    record = JobRecord(
        source="Company Careers",
        date_collected=datetime.now().astimezone().isoformat(timespec="seconds"),
        # `updated_at` moves on every edit of the posting; `first_published`
        # is when it went live. Older payloads may lack it.
        date_published=clean_text(job.get("first_published") or job.get("updated_at")),
        title=clean_text(job.get("title")),
        company=board["company"],
        city_region=location,
        country=_country(location),
        work_format=_work_format(f"{location}\n{description}"),
        source_work_format=_work_format(f"{location}\n{description}"),
        salary=extract_salary({}, description),
        url=url,
        full_text=description,
        status="collected",
        availability_status="active",
        note=f"ATS: Greenhouse; board: {board['slug']}",
        search_query=query["query"],
        job_category=query["category"],
    )
    record.job_id = build_job_id(record)
    return record


def _lever_description(job: dict[str, Any]) -> str:
    """Lever splits a posting's body across descriptionPlain, lists[] sections
    (e.g. Responsibilities/Requirements) and additionalPlain; concatenate all
    of it, since descriptionPlain alone can be just a company intro blurb."""
    parts = [clean_text(job.get("descriptionPlain") or job.get("description"))]
    for item in job.get("lists") or []:
        if not isinstance(item, dict):
            continue
        heading = clean_text(item.get("text"))
        content = clean_text(item.get("content"))
        if content:
            parts.append(f"{heading}\n{content}" if heading else content)
    additional = clean_text(job.get("additionalPlain") or job.get("additional"))
    if additional:
        parts.append(additional)
    return "\n\n".join(part for part in parts if part)


_LEVER_WORKPLACE_TYPES = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}


def _lever_work_format(job: dict[str, Any], text: str) -> str:
    """Prefer Lever's own structured `workplaceType` over text-mining.

    Confirmed against a real board (Wintermute) that Lever populates this
    field on every posting with one of remote/hybrid/onsite - far more
    reliable than `infer_work_format` guessing from free text, which left
    postings "Unknown" whenever the description never states a format
    (common - many companies only convey it through this field, not prose).
    Falls back to text-mining when the field is missing/unrecognized.
    """
    mapped = _LEVER_WORKPLACE_TYPES.get(str(job.get("workplaceType") or "").strip().casefold())
    return mapped or _work_format(text)


def _lever_created_at(job: dict[str, Any]) -> str:
    """Lever gives `createdAt` as epoch milliseconds."""
    created = job.get("createdAt")
    if not isinstance(created, (int, float)) or isinstance(created, bool):
        return ""
    return datetime.fromtimestamp(created / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def _lever_record(job: dict[str, Any], board: dict[str, str], query: dict[str, str]) -> JobRecord:
    categories = job.get("categories") if isinstance(job.get("categories"), dict) else {}
    location = _location_text(categories.get("location"))
    description = _lever_description(job)
    url = clean_text(job.get("hostedUrl") or job.get("applyUrl"))
    work_format = _lever_work_format(job, f"{location}\n{description}")
    # Lever's own ISO-ish `country` field (e.g. "GB", "US") is more reliable
    # than inferring from the free-text `location` string, but isn't always
    # present - fall back to the text-based inference when it's missing.
    country = clean_text(job.get("country")) or _country(location)
    record = JobRecord(
        source="Company Careers",
        date_collected=datetime.now().astimezone().isoformat(timespec="seconds"),
        date_published=_lever_created_at(job),
        title=clean_text(job.get("text")),
        company=board["company"],
        city_region=location,
        country=country,
        work_format=work_format,
        source_work_format=work_format,
        contract_type=clean_text(categories.get("commitment")),
        salary=extract_salary({}, description),
        url=url,
        full_text=description,
        status="collected",
        availability_status="active",
        note=f"ATS: Lever; board: {board['slug']}",
        search_query=query["query"],
        job_category=query["category"],
    )
    record.job_id = build_job_id(record)
    return record


class CompanyCareersCollector(BaseCollector):
    """Collect matching jobs from explicitly configured public ATS boards."""

    source_name = "Company Careers"

    def __init__(self, companies: dict[str, Any] | None = None, settings: dict[str, Any] | None = None) -> None:
        self.companies = companies or {}
        self.settings = settings or {}

    async def collect(self, context: BrowserContext, queries: list[dict[str, str]], limit: int) -> CollectorResult:
        result = CollectorResult(source=self.source_name, queries=[item["query"] for item in queries])
        timeout_ms = int(setting(self.settings, "request", "timeout_seconds", default=60) * 1000)
        delay_ms = int(float(setting(self.settings, "request", "delay_min_seconds", default=1)) * 1000)
        retry_attempts = int(setting(self.settings, "request", "retry_attempts", default=0))
        delay_min = float(setting(self.settings, "request", "delay_min_seconds", default=1.0))
        delay_max = float(setting(self.settings, "request", "delay_max_seconds", default=delay_min))
        boards = configured_career_boards(self.companies)
        board_limit = max_jobs_per_board(self.companies)
        seen_urls: set[str] = set()

        for board in boards:
            if len(result.records) >= limit:
                break
            url = (
                build_greenhouse_url(board["slug"])
                if board["ats"] == "greenhouse"
                else build_lever_url(board["slug"], board.get("host", DEFAULT_LEVER_HOST))
            )
            try:
                response = await get_with_retry(
                    context, url, timeout_ms=timeout_ms, retry_attempts=retry_attempts,
                    delay_min_seconds=delay_min, delay_max_seconds=delay_max,
                )
                payload = await response.json()
                jobs = parse_greenhouse_jobs(payload) if board["ats"] == "greenhouse" else parse_lever_jobs(payload)
                board_records = 0
                for job in jobs:
                    if board_records >= board_limit:
                        break
                    job_title = str(job.get("title") or job.get("text") or "")
                    query = next((item for item in queries if query_matches(item["query"], job_title)), None)
                    if query is None:
                        continue
                    record = _greenhouse_record(job, board, query) if board["ats"] == "greenhouse" else _lever_record(job, board, query)
                    if record.url and record.url not in seen_urls:
                        seen_urls.add(record.url)
                        result.records.append(record)
                        board_records += 1
                        if len(result.records) >= limit:
                            break
                result.found = len(result.records)
                if context.pages:
                    await context.pages[0].wait_for_timeout(delay_ms)
            except Exception as error:
                result.errors += 1
                result.error_messages.append(f"{board['company']} ({board['ats']}): {error}")

        result.found = len(result.records)
        return result
