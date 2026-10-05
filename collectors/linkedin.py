"""LinkedIn collector with query-level provenance and resilient parsing."""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

from collectors.base import BaseCollector, CollectorResult, per_query_share, setting
from core.metadata import (
    extract_location_from_text,
    extract_salary,
    infer_country,
    infer_work_format,
    resolve_relative_date,
)
from core.ids import build_job_id
from core.models import JobRecord


LINKEDIN_BASE_URL = "https://www.linkedin.com"
DEFAULT_LOCATION = "Worldwide"
LOGIN_MARKERS = (
    "sign in to see more jobs",
    "log in to see more jobs",
    "join linkedin",
    "войти в linkedin",
    "identifiez-vous pour voir plus d'offres d'emploi",
    "connectez-vous pour voir plus d'offres d'emploi",
    "keep your linkedin services",
    "choose how we use your data for personalized ads",
)
CLOSED_JOB_MARKERS = (
    "no longer accepting applications",
    "job is no longer available",
    "this job has expired",
    "job has been closed",
    "position has been closed",
    "doesn't exist",
    "unable to load the page",
    "may not be valid",
    "job posting has been removed",
)


# LinkedIn's own "Premium" upsell widget renders inside the same DOM
# container the description selectors target (and shows up inside the
# JSON-LD description too), so it leaks into full_text as if the poster had
# written it — polluting every downstream keyword check (skills, French-word
# density, overqualified markers) with LinkedIn UI chrome, not job content.
# Confirmed 2026-09-07: this single line falsely triggered an "AI/LLM Tools"
# skill match on 7+ unrelated postings that never mentioned AI at all.
_BOILERPLATE_PATTERN = re.compile(
    r"get ai-powered advice on this job.*?try premium for [^\n]*",
    re.IGNORECASE,
)


def strip_boilerplate(text: str) -> str:
    return _BOILERPLATE_PATTERN.sub("", text).strip()


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = BeautifulSoup(text, "html.parser").get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return strip_boilerplate(text.strip())


def build_search_url(query: str, location: str = DEFAULT_LOCATION) -> str:
    """Build a public LinkedIn jobs search URL for one configured query.

    No `f_WT` work-type filter (was hardcoded to "2" = Remote): the candidate
    is now open to office/hybrid roles and relocation, so search casts the
    wider net and `work_format`/`work_mode` are recorded for manual_review to
    weigh instead of excluding non-remote postings before a human sees them.
    """
    parameters = urlencode(
        {
            "keywords": query,
            "location": location,
            "sortBy": "DD",
        }
    )
    return f"{LINKEDIN_BASE_URL}/jobs/search/?{parameters}"


def page_requires_login(current_url: str, body_text: str) -> bool:
    """Detect common LinkedIn login/authwall states without browser access."""
    url = current_url.casefold()
    if any(marker in url for marker in ("/login", "/authwall", "/checkpoint", "/connect-services")):
        return True
    body = body_text.casefold()
    return any(marker in body for marker in LOGIN_MARKERS)


def detect_availability_status(current_url: str, body_text: str) -> str:
    """Classify a LinkedIn job page without treating login walls as closed."""
    if page_requires_login(current_url, body_text):
        return "unknown"
    lowered = body_text.casefold()
    if any(marker in lowered for marker in CLOSED_JOB_MARKERS):
        return "closed"
    return "active" if "/jobs/view/" in current_url.casefold() and body_text.strip() else "unknown"


def parse_json_ld(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if isinstance(value, dict) and value.get("@type") == "JobPosting":
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


async def first_text(page: Page, selectors: list[str]) -> str:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible():
                value = clean_text(await locator.inner_text())
                if value:
                    return value
        except Exception:
            continue
    return ""


async def json_ld_job(page: Page) -> dict[str, Any] | None:
    scripts = page.locator('script[type="application/ld+json"]')
    for index in range(await scripts.count()):
        raw = await scripts.nth(index).text_content()
        if raw:
            job = parse_json_ld(raw)
            if job:
                return job
    return None


async def wait_for_manual_login(page: Page, timeout_seconds: int) -> None:
    """Wait while the user completes LinkedIn login in the visible browser."""
    deadline_ms = max(timeout_seconds, 1) * 1000
    elapsed_ms = 0
    print(
        "[LinkedIn] Login required. Complete login in the open Chromium window; "
        f"waiting up to {timeout_seconds} seconds."
    )
    while elapsed_ms < deadline_ms:
        try:
            body_text = await page.locator("body").inner_text()
        except Exception:
            body_text = ""
        if not page_requires_login(page.url, body_text):
            print("[LinkedIn] Login detected, continuing collection.")
            return
        await page.wait_for_timeout(1000)
        elapsed_ms += 1000

    raise TimeoutError("LinkedIn login was not completed before the timeout")


def organization_name(job: dict[str, Any]) -> str:
    organization = job.get("hiringOrganization", "")
    if isinstance(organization, dict):
        return clean_text(organization.get("name", ""))
    return clean_text(organization)


def location_name(job: dict[str, Any]) -> str:
    locations = job.get("jobLocation", [])
    if not isinstance(locations, list):
        locations = [locations]
    values: list[str] = []
    for location in locations:
        if not isinstance(location, dict):
            continue
        address = location.get("address", location)
        if not isinstance(address, dict):
            continue
        parts = [address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry")]
        value = ", ".join(clean_text(part) for part in parts if clean_text(part))
        if value:
            values.append(value)
    return " / ".join(dict.fromkeys(values))


def parse_title_tag(raw: str) -> tuple[str, str]:
    """Split LinkedIn's "<Job Title> | <Company> | LinkedIn" page title.

    LinkedIn's job-detail markup uses hashed, frequently-changing CSS classes
    with no stable selector for the title/company text, but the <title> tag
    is server-rendered for SEO and has kept this pipe-delimited format even
    through markup rewrites, making it a much more durable fallback source.
    """
    parts = [part.strip() for part in str(raw or "").split("|")]
    parts = [part for part in parts if part and part.casefold() != "linkedin"]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def workplace_from_job(job: dict[str, Any]) -> str:
    if job.get("jobLocationType") == "TELECOMMUTE":
        return "Remote"
    return ""


async def record_from_page(
    page: Page,
    *,
    query: dict[str, str],
    url: str,
    availability_status: str = "unknown",
) -> JobRecord:
    job = await json_ld_job(page)
    if job:
        title = clean_text(job.get("title"))
        company = organization_name(job)
        location = location_name(job)
        description = clean_text(job.get("description"))
        date_published = clean_text(job.get("datePosted"))
        contract_type = clean_text(job.get("employmentType"))
        work_format = workplace_from_job(job) or infer_work_format(description)
        salary = extract_salary(job.get("baseSalary"), description)
    else:
        title = await first_text(page, ["h1", ".top-card-layout__title", ".jobs-unified-top-card__job-title"])
        company = await first_text(page, ["a[href*='/company/']", ".topcard__org-name-link"])
        location = await first_text(page, [".topcard__flavor--bullet", ".job-details-jobs-unified-top-card__tertiary-description-container"])
        # '[id^="JobDetails_AboutTheJob_"]' targets LinkedIn's current per-job
        # description component by its stable id prefix, scoped away from the
        # sibling "similar jobs" panel (its own id, JobDetailsSimilarJobsSlot_*).
        # The old BEM class names are kept as a fallback for markup that still
        # uses them; "main" is deliberately NOT in this list — it captures the
        # whole content area including that sidebar, mixing an unrelated
        # posting's title/salary/language into this record's full_text.
        description = await first_text(
            page,
            ['[id^="JobDetails_AboutTheJob_"]', ".jobs-description-content__text", ".show-more-less-html__markup"],
        )
        # The page shows "2 days ago"; pin it to a date now, while "now" is known.
        date_published = resolve_relative_date(
            await first_text(page, ["time", ".posted-time-ago__text"]), datetime.now()
        )
        contract_type = ""
        # infer_visible_metadata() is NOT used here: it assumes the old "main"
        # fallback's shape (company / title / location · date as the first few
        # lines of the whole page). `description` is now scoped precisely to
        # the job body, so that positional heuristic would misread the job's
        # own first sentence as the title/company instead.
        work_format = infer_work_format(description)
        salary = extract_salary({}, description)

        if not title or not company:
            # Last-resort fallback: the <title> tag is server-rendered for SEO
            # and survives markup rewrites that break every CSS selector above.
            tag_title, tag_company = parse_title_tag(await page.title())
            title = title or tag_title
            company = company or tag_company

    if not location:
        # The page-level location selector sometimes finds nothing (common on
        # aggregator reposts) even though the description itself states one,
        # e.g. "🌐 Location: Remote" — recover it from the text instead of
        # leaving city_region empty.
        location = extract_location_from_text(description)

    country = infer_country(location)
    if not country and "," in location:
        # infer_country only knows a fixed country/city list; keep the old
        # comma-split as a fallback for a country name outside that list.
        country = location.rsplit(",", 1)[-1].strip()

    record = JobRecord(
        source="LinkedIn",
        date_collected=datetime.now().astimezone().isoformat(timespec="seconds"),
        date_published=date_published,
        title=title,
        company=company,
        city_region=location,
        country=country,
        work_format=work_format,
        source_work_format=work_format,
        contract_type=contract_type,
        salary=salary,
        url=url,
        full_text=description,
        status="collected",
        availability_status=availability_status,
        search_query=query["query"],
        job_category=query["category"],
    )
    record.job_id = build_job_id(record)
    return record


class LinkedInCollector(BaseCollector):
    """Collect public LinkedIn job detail pages through a browser context."""

    source_name = "LinkedIn"

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or {}

    async def collect(
        self,
        context: BrowserContext,
        queries: list[dict[str, str]],
        limit: int,
    ) -> CollectorResult:
        result = CollectorResult(
            source=self.source_name,
            queries=[item["query"] for item in queries],
        )
        location = str(setting(self.settings, "collection", "location", default=DEFAULT_LOCATION))
        timeout_ms = int(setting(self.settings, "request", "timeout_seconds", default=60) * 1000)
        delay_ms = int(float(setting(self.settings, "request", "delay_min_seconds", default=1.0)) * 1000)
        login_timeout_seconds = int(
            setting(self.settings, "browser", "manual_login_timeout_seconds", default=180)
        )

        search_page = await context.new_page()
        detail_page = await context.new_page()
        links: list[tuple[str, dict[str, str]]] = []
        seen_urls: set[str] = set()

        query_cap = per_query_share(limit, len(queries))
        try:
            for query in queries:
                if len(links) >= limit:
                    break
                query_added = 0
                try:
                    search_url = build_search_url(query["query"], location)
                    await search_page.goto(
                        search_url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                    await search_page.wait_for_timeout(max(delay_ms, 2000))
                    body_text = await search_page.locator("body").inner_text()
                    if page_requires_login(search_page.url, body_text):
                        await wait_for_manual_login(search_page, login_timeout_seconds)
                        # LinkedIn often redirects to the feed after login.
                        # Return to the original search before extracting cards.
                        await search_page.goto(
                            search_url,
                            wait_until="domcontentloaded",
                            timeout=timeout_ms,
                        )
                    await search_page.wait_for_timeout(delay_ms)
                    anchors = search_page.locator('a[href*="/jobs/view/"]')
                    for index in range(await anchors.count()):
                        href = await anchors.nth(index).get_attribute("href")
                        if not href:
                            continue
                        url = urljoin(LINKEDIN_BASE_URL, href).split("?", 1)[0]
                        if url not in seen_urls:
                            seen_urls.add(url)
                            links.append((url, query))
                            query_added += 1
                            if len(links) >= limit or query_added >= query_cap:
                                break
                except Exception as error:
                    result.errors += 1
                    result.error_messages.append(f"{query['query']}: {error}")

            result.found = len(links)
            for url, query in links:
                try:
                    await detail_page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    await detail_page.wait_for_timeout(delay_ms)
                    body_text = await detail_page.locator("body").inner_text()
                    record = await record_from_page(
                        detail_page,
                        query=query,
                        url=url,
                        availability_status=detect_availability_status(detail_page.url, body_text),
                    )
                    if record.title or record.full_text:
                        result.records.append(record)
                    else:
                        result.errors += 1
                        result.error_messages.append(f"Empty job page: {url}")
                except PlaywrightTimeoutError:
                    result.errors += 1
                    result.error_messages.append(f"Timeout: {url}")
                except Exception as error:
                    result.errors += 1
                    result.error_messages.append(f"{url}: {error}")
        finally:
            await search_page.close()
            await detail_page.close()

        return result
