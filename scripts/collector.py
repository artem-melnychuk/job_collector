"""Command-line entry point for the crypto and trading jobs collector."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Chromium is installed inside this project's virtual environment.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")

from playwright.async_api import async_playwright

from collectors.linkedin import LinkedInCollector, detect_availability_status as detect_linkedin_availability
from collectors.company_careers import (
    CompanyCareersCollector,
    build_greenhouse_url,
    build_lever_url,
    parse_greenhouse_jobs,
    parse_lever_jobs,
)
from collectors.wttj import (
    WttjCollector,
    detect_availability_status as detect_wttj_availability,
    normalize_saved_location,
)
from collectors.base import get_with_retry
from collectors.djinni import DjinniCollector
from collectors.dou import DouCollector
from collectors.himalayas import HimalayasCollector, company_slug_from_url, fetch_company_job_urls
from collectors.jobicy import JobicyCollector
from collectors.remoteok import RemoteOkCollector
from collectors.weworkremotely import WeWorkRemotelyCollector
from collectors.work_ua import STEALTH_HEADERS, STEALTH_LOCALE, STEALTH_USER_AGENT, WorkUaCollector
from collectors.robota_ua import RobotaUaCollector
from core.config import load_configuration, select_queries
from core.deduplication import deduplicate_records
from core.ids import normalize_url
from core.logging import RunStats, write_run_log
from core.metadata import (
    extract_salary,
    has_disallowed_work_format,
    infer_country,
    infer_work_format,
    is_low_quality_listing,
    is_overqualified,
    matches_context,
    query_matches,
    requires_french,
    requires_german,
    requires_onsite_in_disallowed_country,
)
from core.storage import load_records, merge_records, save_records


# Company Careers only pulls from explicitly curated crypto/trading companies
# (config/companies.yaml) and already checks each job's title against its
# query at collection time (collectors/company_careers.py), so every record
# is relevant by construction. LinkedIn and WTTJ run broad public-site
# searches with no such guarantee — their search results pages mix in
# "similar jobs" unrelated to the query used — so their results get a
# post-fetch relevance check: the title must still match the query it was
# found under, plus a context_terms check for categories that require it.
# Djinni is deliberately NOT in this set: its primary_keyword filter is a
# server-side relevance filter (same trust role as Company Careers' company
# curation), and its titles are Ukrainian/Russian, so an English query_matches
# check against the title would wrongly reject legitimate results. RemoteOK
# and We Work Remotely are also not in this set: neither's public feed has a
# working server-side filter (see collectors/remoteok.py and
# collectors/weworkremotely.py), so both collectors already run the same
# query_matches title check themselves before a record is ever returned, just
# like Company Careers. Jobicy and Himalayas (added 2026-09-30) do the same:
# their keyword searches are deliberately broad ("analyst"), so both run
# query_matches on titles before returning a record. DOU (added 2026-10-05)
# is excluded like Djinni: its category/search feeds are the relevance
# filter and its titles are often Ukrainian. Work.ua/Robota.ua are excluded for the same reason as
# Djinni: their own free-text keyword search IS the relevance filter, and
# their titles are Ukrainian/Russian, so the English query_matches title
# check would wrongly reject legitimate results.
CONTEXT_FILTERED_SOURCES = {"LinkedIn", "Welcome to the Jungle"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect crypto, trading, risk, and analytics vacancies."
    )
    parser.add_argument(
        "--source",
        choices=["all", "linkedin", "wttj", "company_careers", "djinni", "dou", "remoteok", "weworkremotely",
                 "jobicy", "himalayas", "work_ua", "robota_ua"],
        # "all" deliberately excludes wttj (see collect_sources) — it stays
        # selectable on its own for anyone who wants to re-check it.
        default="all",
        help="Source adapter to use (default: all).",
    )
    parser.add_argument(
        "--query",
        help="Run one exact query from config/job_queries.yaml.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum number of records per source (default: 50).",
    )
    parser.add_argument(
        "--check-availability",
        action="store_true",
        help="Recheck saved vacancy URLs and update availability_status.",
    )
    parser.add_argument(
        "--check-limit",
        type=int,
        default=50,
        help="Maximum number of saved URLs to recheck (default: 50).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output XLSX path. By default a timestamped file is used in data/raw/.",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=PROJECT_ROOT / "config",
        help="Directory containing the YAML configuration files.",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        help="Persistent browser profile directory.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chromium without opening a visible window.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load and validate configuration without starting source collectors.",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Open WTTJ sign-in and save the authenticated browser profile.",
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.limit < 1 or args.limit > 500:
        raise ValueError("--limit must be between 1 and 500")
    if args.check_limit < 1 or args.check_limit > 5000:
        raise ValueError("--check-limit must be between 1 and 5000")


def resolve_path(path: Path, project_root: Path = PROJECT_ROOT) -> Path:
    """Resolve relative CLI paths against the collector project root."""
    return path if path.is_absolute() else project_root / path


def default_raw_path() -> Path:
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return PROJECT_ROOT / "data" / "raw" / f"crypto_jobs_{timestamp}.xlsx"


async def collect_linkedin(
    configuration: dict[str, Any],
    queries: list[dict[str, str]],
    limit: int,
    profile_dir: Path,
    headless: bool,
):
    settings = configuration["settings"]
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            viewport={"width": 1440, "height": 1000},
            locale=str(settings.get("browser", {}).get("locale", "en-US")),
        )
        try:
            collector = LinkedInCollector(settings=settings)
            return await collector.collect(context, queries, limit)
        finally:
            await context.close()


async def collect_sources(
    configuration: dict[str, Any],
    queries: list[dict[str, str]],
    limit: int,
    profile_dir: Path,
    headless: bool,
    source: str,
    djinni_queries: list[dict[str, str]] | None = None,
    ukrainian_queries: list[dict[str, str]] | None = None,
):
    """Run the selected adapters in one persistent browser context.

    "all" leaves out wttj: its only reachable public page without a login
    (https://www.welcometothejungle.com/fr/pages/emploi-data-analyst) is a
    French-market topic page, so every result is either French-language
    (rejected by requires_french) or Hybrid (rejected by
    has_disallowed_work_format) — confirmed 2026-09-04 by inspecting raw
    results before filtering: all 5 sampled were correctly rejected for one
    of those two reasons, not falsely. The exclusion rules are doing their
    job; the source itself cannot produce a match for a remote-only,
    non-French-speaking profile. Still selectable via --source wttj.
    """
    settings = configuration["settings"]
    profile_dir.mkdir(parents=True, exist_ok=True)
    # work_ua and robota_ua deliberately excluded from "all". robota_ua:
    # confirmed 2026-09-07 that robota.ua's own Cloudflare/Turnstile
    # verification is failing for real human visitors too (not just
    # automation) — network inspection showed its GraphQL API calls 403ing
    # and the Cloudflare PAT token endpoint itself returning 401 even after
    # solving the "confirm you're human" challenge; a site-side outage, not
    # fixable by tuning the scraper. work_ua: a dedicated "stealth" browser
    # context (see below) measurably reduces how often it gets challenged,
    # but doesn't eliminate it — the same profile got blocked again after a
    # handful more requests in a later run (see collectors/work_ua.py
    # module docstring) — not dependable enough for an unattended run.
    selected = (
        ["linkedin", "company_careers", "djinni", "dou", "remoteok", "weworkremotely", "jobicy", "himalayas"]
        if source == "all"
        else [source]
    )
    results = []
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            viewport={"width": 1440, "height": 1000},
            locale=str(settings.get("browser", {}).get("locale", "en-US")),
        )
        try:
            for name in selected:
                if name == "linkedin":
                    collector = LinkedInCollector(settings=settings)
                    results.append(await collector.collect(context, queries, limit))
                elif name == "wttj":
                    collector = WttjCollector(settings=settings)
                    results.append(await collector.collect(context, queries, limit))
                elif name == "djinni":
                    collector = DjinniCollector(settings=settings)
                    results.append(await collector.collect(context, djinni_queries or [], limit))
                elif name == "remoteok":
                    collector = RemoteOkCollector(settings=settings)
                    results.append(await collector.collect(context, queries, limit))
                elif name == "weworkremotely":
                    collector = WeWorkRemotelyCollector(settings=settings)
                    results.append(await collector.collect(context, queries, limit))
                elif name == "dou":
                    collector = DouCollector(settings=settings)
                    results.append(await collector.collect(context, queries, limit))
                elif name == "jobicy":
                    collector = JobicyCollector(settings=settings)
                    results.append(await collector.collect(context, queries, limit))
                elif name == "himalayas":
                    collector = HimalayasCollector(settings=settings)
                    results.append(await collector.collect(context, queries, limit))
                elif name == "work_ua":
                    # Dedicated context, not the shared one: work.ua challenges
                    # the shared context's default fingerprint (no explicit
                    # user_agent/locale, "en-US") but passed repeatedly with a
                    # realistic Chrome UA + Ukrainian locale + Accept-Language
                    # (see collectors/work_ua.py:STEALTH_* for verification
                    # notes). Scoped to work_ua only so LinkedIn/WTTJ keep
                    # using the fingerprint they already work with.
                    ua_profile_dir = profile_dir.parent / f"{profile_dir.name}_work_ua"
                    ua_profile_dir.mkdir(parents=True, exist_ok=True)
                    ua_context = await playwright.chromium.launch_persistent_context(
                        user_data_dir=str(ua_profile_dir),
                        headless=headless,
                        viewport={"width": 1440, "height": 900},
                        locale=STEALTH_LOCALE,
                        user_agent=STEALTH_USER_AGENT,
                        extra_http_headers=STEALTH_HEADERS,
                    )
                    try:
                        collector = WorkUaCollector(settings=settings)
                        results.append(await collector.collect(ua_context, ukrainian_queries or [], limit))
                    finally:
                        await ua_context.close()
                elif name == "robota_ua":
                    collector = RobotaUaCollector(settings=settings)
                    results.append(await collector.collect(context, ukrainian_queries or [], limit))
                else:
                    collector = CompanyCareersCollector(
                        companies=configuration["companies"],
                        settings=settings,
                    )
                    results.append(await collector.collect(context, queries, limit))
        finally:
            await context.close()
    return results


async def run_collection(args: argparse.Namespace) -> int:
    config_dir = resolve_path(args.config_dir)
    configuration = load_configuration(config_dir)
    queries = select_queries(configuration["queries"], args.query)

    # Djinni uses its own query vocabulary (djinni_categories), not the shared
    # English "queries" list, so it needs its own selection. A --query value
    # that isn't one of Djinni's own categories just means "nothing to collect
    # from Djinni this run" when running --source all, but is a real user
    # error (and should raise, like every other source) when Djinni was
    # explicitly requested.
    djinni_config = {"categories": configuration["queries"].get("djinni_categories", {})}
    try:
        djinni_queries = select_queries(djinni_config, args.query)
    except ValueError:
        if args.source == "djinni":
            raise
        djinni_queries = []

    # Work.ua and Robota.ua are free-text keyword search (unlike Djinni's
    # categorical filter), but still need Ukrainian-language query phrases —
    # the shared English "queries" list wouldn't match anything on either
    # site. Both sources use the same ukrainian_categories vocabulary since
    # their search mechanics (and target postings) are equivalent.
    ukrainian_config = {"categories": configuration["queries"].get("ukrainian_categories", {})}
    try:
        ukrainian_queries = select_queries(ukrainian_config, args.query)
    except ValueError:
        if args.source in ("work_ua", "robota_ua"):
            raise
        ukrainian_queries = []

    settings = configuration["settings"]
    profile_dir = resolve_path(
        args.profile_dir
        or Path(str(settings.get("browser", {}).get("profile_dir", ".browser_profile")))
    )
    results = await collect_sources(
        configuration,
        queries,
        args.limit,
        profile_dir,
        args.headless or bool(settings.get("browser", {}).get("headless", False)),
        args.source,
        djinni_queries=djinni_queries,
        ukrainian_queries=ukrainian_queries,
    )
    context_terms = configuration["queries"].get("context_terms", [])
    context_exempt_categories = set(configuration["queries"].get("context_exempt_categories", []))
    exclusion = configuration["queries"].get("exclusion", {})
    queries_by_category: dict[str, list[str]] = {}
    for item in queries:
        queries_by_category.setdefault(item["category"], []).append(item["query"])
    all_records = []
    statistics = RunStats()
    for result in results:
        filtered_out = 0
        kept = []
        for record in result.records:
            if result.source in CONTEXT_FILTERED_SOURCES:
                # Check against every query in the record's own category, not
                # just the one query it happened to be searched under: LinkedIn
                # returns unrelated "similar jobs" alongside real matches, but a
                # title found under "Strategy Analyst" that actually reads
                # "Business Analyst" is still a real hit for the same category.
                candidate_queries = queries_by_category.get(record.job_category) or [record.search_query]
                if not any(query_matches(candidate, record.title) for candidate in candidate_queries):
                    filtered_out += 1
                    continue
            if is_overqualified(record.title, record.full_text, exclusion):
                filtered_out += 1
                continue
            if requires_french(record.title, record.full_text, exclusion):
                filtered_out += 1
                continue
            if requires_german(record.title, record.full_text, exclusion):
                filtered_out += 1
                continue
            if has_disallowed_work_format(record.work_format, exclusion):
                filtered_out += 1
                continue
            if requires_onsite_in_disallowed_country(record.work_format, record.country, exclusion):
                filtered_out += 1
                continue
            if is_low_quality_listing(record.title, record.full_text, exclusion):
                filtered_out += 1
                continue
            needs_context_check = (
                result.source in CONTEXT_FILTERED_SOURCES
                and context_terms
                and record.job_category not in context_exempt_categories
            )
            if needs_context_check and not matches_context(f"{record.title}\n{record.full_text}", context_terms):
                filtered_out += 1
                continue
            kept.append(record)
        result.records = kept
        unique_source = deduplicate_records(result.records)
        source_stats = statistics.for_source(result.source)
        source_stats.queries = result.queries
        source_stats.found = result.found
        source_stats.unique = len(unique_source.records)
        source_stats.duplicates = unique_source.duplicates
        source_stats.filtered_out = filtered_out
        source_stats.errors = result.errors
        all_records.extend(unique_source.records)
    unique_result = deduplicate_records(all_records)
    raw_path = resolve_path(args.output) if args.output else default_raw_path()
    processed_path = resolve_path(
        Path(str(settings.get("storage", {}).get("processed_dir", "data/processed")))
        / str(settings.get("storage", {}).get("processed_filename", "crypto_jobs_clean_v1.xlsx"))
    )
    existing = load_records(processed_path)
    merged, merge_counts = merge_records(existing, unique_result.records)
    raw_path, raw_csv_path = save_records(unique_result.records, raw_path, workbook_kind="raw")
    save_records(merged, processed_path, workbook_kind="processed")

    if len(results) == 1:
        source_stats = statistics.for_source(results[0].source)
        source_stats.new = merge_counts["new"]
        source_stats.existing = merge_counts["existing"]
        source_stats.updated = merge_counts["updated"]
    else:
        # Merge counts are collection-wide; keep them visible in the report.
        total_stats = statistics.for_source("All sources")
        total_stats.new = merge_counts["new"]
        total_stats.existing = merge_counts["existing"]
        total_stats.updated = merge_counts["updated"]
    statistics.print_report()
    print(f"Raw XLSX: {raw_path}")
    print(f"Raw CSV: {raw_csv_path}")
    print(f"Processed XLSX: {processed_path}")
    error_messages = [message for result in results for message in result.error_messages]
    if error_messages:
        print("\nErrors:")
        for message in error_messages:
            print(f"- {message}")

    logs_dir = resolve_path(Path(str(settings.get("storage", {}).get("logs_dir", "logs"))))
    log_path = write_run_log(
        logs_dir,
        "collect",
        {
            "source_arg": args.source,
            "sources": statistics.to_dict(),
            "merge_counts": merge_counts,
            "raw_path": str(raw_path),
            "processed_path": str(processed_path),
            "errors": error_messages,
        },
    )
    print(f"Run log: {log_path}")
    return 0


# Sources whose salary comes only from the API's structured fields. Their
# descriptions mention unrelated amounts (a "10 EUR" meal allowance, a
# "USD 2,000" budget line), so guessing a salary from the text produced fake
# figures; see collectors/jobicy.py and collectors/himalayas.py.
STRUCTURED_SALARY_SOURCES = {"Jobicy", "Himalayas"}


def backfill_saved_metadata(record) -> None:
    """Fill gaps in an already-saved record before an availability check:
    location/country, work format and salary inferred from its own text."""
    if record.source == "Welcome to the Jungle":
        location, country = normalize_saved_location(record.city_region)
        if location:
            record.city_region = location
        if country:
            record.country = country
    elif not record.country:
        record.country = infer_country(record.city_region)
    inferred_work_format = infer_work_format(record.full_text)
    if inferred_work_format != "Unknown" and record.work_format in {"", "Unknown", None}:
        record.work_format = inferred_work_format
        record.source_work_format = inferred_work_format
    if not record.salary and record.source not in STRUCTURED_SALARY_SOURCES:
        record.salary = extract_salary({}, record.full_text)


def keep_known_closed(previous: str, current: str) -> str:
    """A posting confirmed closed does not reopen, so an inconclusive recheck
    ("unknown": an authwall, a rate limit, a timeout) must not erase that.
    Seen 2026-10-01: a second LinkedIn pass in one morning was throttled and
    turned 39 known-closed postings into "unknown"."""
    if current == "unknown" and str(previous or "").strip() == "closed":
        return "closed"
    return current


def detect_generic_availability(http_status: int | None, body_text: str) -> str:
    """Classify a career page when no source-specific detector is available."""
    lowered = body_text.casefold()
    closed_markers = (
        "job is no longer available",
        "this job has expired",
        "position has been filled",
        "page not found",
        "404 not found",
        # Djinni serves a closed posting as a normal HTTP 200 page with this
        # banner (English / Ukrainian UI). Missing until 2026-09-30, so every
        # closed Djinni posting was reported "active" - the user found 4 of 4
        # "active" drafts already closed when trying to apply.
        "the job ad is no longer active",
        "ця вакансія зараз неактивна",
    )
    if http_status in {404, 410} or any(marker in lowered for marker in closed_markers):
        return "closed"
    if http_status is not None and 200 <= http_status < 400 and body_text.strip():
        return "active"
    return "unknown"


def parse_ats_board(note: str) -> tuple[str, str] | None:
    """Extract ATS and board slug from a Company Careers record note."""
    match = re.search(r"ATS:\s*(Greenhouse|Lever);\s*board:\s*([^;]+)", note or "", re.IGNORECASE)
    if not match:
        return None
    return match.group(1).casefold(), match.group(2).strip()


def normalized_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.scheme.casefold()}://{parts.netloc.casefold()}{parts.path.rstrip('/')}"


def ats_job_is_present(record_url: str, ats: str, jobs: list[dict[str, Any]]) -> bool:
    """Match a saved hosted URL against the current public ATS job list."""
    saved_normalized = normalized_url(record_url)
    greenhouse_id_match = re.search(r"gh_jid=(\d+)", record_url)
    saved_id = greenhouse_id_match.group(1) if greenhouse_id_match else ""
    for job in jobs:
        candidates = [str(job.get("absolute_url", "")), str(job.get("hostedUrl", ""))]
        if any(candidate and normalized_url(candidate) == saved_normalized for candidate in candidates):
            return True
        if ats == "greenhouse" and saved_id and str(job.get("id", "")) == saved_id:
            return True
    return False


async def run_availability_check(args: argparse.Namespace) -> int:
    """Recheck saved URLs and persist the latest availability status."""
    config_dir = resolve_path(args.config_dir)
    configuration = load_configuration(config_dir)
    settings = configuration["settings"]
    processed_path = resolve_path(
        Path(str(settings.get("storage", {}).get("processed_dir", "data/processed")))
        / str(settings.get("storage", {}).get("processed_filename", "crypto_jobs_clean_v1.xlsx"))
    )
    records = load_records(processed_path)
    for record in records:
        backfill_saved_metadata(record)
    records_to_check = records[: args.check_limit]
    profile_dir = resolve_path(
        args.profile_dir
        or Path(str(settings.get("browser", {}).get("profile_dir", ".browser_profile")))
    )
    timeout_ms = int(settings.get("request", {}).get("timeout_seconds", 60) * 1000)
    delay_ms = int(float(settings.get("request", {}).get("delay_min_seconds", 1)) * 1000)
    retry_attempts = int(settings.get("request", {}).get("retry_attempts", 0))
    delay_min = float(settings.get("request", {}).get("delay_min_seconds", 1.0))
    delay_max = float(settings.get("request", {}).get("delay_max_seconds", delay_min))
    counts = {"active": 0, "closed": 0, "unknown": 0}
    errors = 0
    career_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
    himalayas_cache: dict[str, set[str]] = {}

    profile_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=args.headless or bool(settings.get("browser", {}).get("headless", False)),
            viewport={"width": 1440, "height": 1000},
            locale=str(settings.get("browser", {}).get("locale", "en-US")),
        )
        page = await context.new_page()
        try:
            for record in records_to_check:
                if not record.url:
                    record.availability_status = "unknown"
                    counts["unknown"] += 1
                    continue
                try:
                    if record.source == "Company Careers":
                        board = parse_ats_board(record.note)
                        if not board:
                            status = "unknown"
                        else:
                            ats, slug = board
                            cache_key = (ats, slug)
                            if cache_key not in career_cache:
                                api_url = build_greenhouse_url(slug) if ats == "greenhouse" else build_lever_url(slug)
                                ats_response = await get_with_retry(
                                    context, api_url, timeout_ms=timeout_ms, retry_attempts=retry_attempts,
                                    delay_min_seconds=delay_min, delay_max_seconds=delay_max,
                                )
                                payload = await ats_response.json()
                                career_cache[cache_key] = (
                                    parse_greenhouse_jobs(payload)
                                    if ats == "greenhouse"
                                    else parse_lever_jobs(payload)
                                )
                            status = "active" if ats_job_is_present(record.url, ats, career_cache[cache_key]) else "closed"
                    elif record.source == "Himalayas":
                        # Job pages are behind a Cloudflare challenge; ask the
                        # API which postings the company still lists instead.
                        slug = company_slug_from_url(record.url)
                        if not slug:
                            status = "unknown"
                        else:
                            if slug not in himalayas_cache:
                                himalayas_cache[slug] = await fetch_company_job_urls(
                                    context, slug, timeout_ms=timeout_ms, retry_attempts=retry_attempts,
                                    delay_min_seconds=delay_min, delay_max_seconds=delay_max,
                                )
                            status = "active" if normalize_url(record.url) in himalayas_cache[slug] else "closed"
                    else:
                        response = await page.goto(record.url, wait_until="domcontentloaded", timeout=timeout_ms)
                        await page.wait_for_timeout(delay_ms)
                        body_text = await page.locator("body").inner_text()
                        if record.source == "LinkedIn":
                            status = detect_linkedin_availability(page.url, body_text)
                        elif record.source == "Welcome to the Jungle":
                            status = detect_wttj_availability(page.url, body_text)
                        else:
                            status = detect_generic_availability(response.status if response else None, body_text)
                    status = keep_known_closed(record.availability_status, status)
                    record.availability_status = status
                    counts[status] += 1
                except Exception as error:
                    errors += 1
                    record.availability_status = keep_known_closed(record.availability_status, "unknown")
                    counts[record.availability_status] += 1
                    print(f"Availability check failed: {record.url} ({error})")
        finally:
            await page.close()
            await context.close()

    save_records(records, processed_path, workbook_kind="processed")
    print(f"Checked URLs: {len(records_to_check)}")
    print(f"Active: {counts['active']}")
    print(f"Closed: {counts['closed']}")
    print(f"Unknown: {counts['unknown']}")
    print(f"Errors: {errors}")
    print(f"Processed XLSX: {processed_path}")

    logs_dir = resolve_path(Path(str(settings.get("storage", {}).get("logs_dir", "logs"))))
    log_path = write_run_log(
        logs_dir,
        "check_availability",
        {
            "checked": len(records_to_check),
            "active": counts["active"],
            "closed": counts["closed"],
            "unknown": counts["unknown"],
            "errors": errors,
            "processed_path": str(processed_path),
        },
    )
    print(f"Run log: {log_path}")
    return 0


async def run_login(configuration: dict[str, Any], profile_dir: Path, headless: bool) -> int:
    """Open WTTJ authentication in the same persistent profile used by collectors."""
    settings = configuration["settings"]
    profile_dir.mkdir(parents=True, exist_ok=True)
    timeout_seconds = int(settings.get("browser", {}).get("manual_login_timeout_seconds", 180))
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            viewport={"width": 1440, "height": 1000},
            locale=str(settings.get("browser", {}).get("locale", "en-US")),
        )
        page = await context.new_page()
        try:
            await page.goto(
                "https://www.welcometothejungle.com/en/authenticate/signin",
                wait_until="domcontentloaded",
                timeout=int(settings.get("request", {}).get("timeout_seconds", 60) * 1000),
            )
            print(
                "[WTTJ] Complete sign-in in the Chromium window. "
                f"Waiting up to {timeout_seconds} seconds."
            )
            for _ in range(max(timeout_seconds, 1)):
                if "/authenticate/" not in page.url.casefold():
                    print(f"[WTTJ] Login saved in profile: {profile_dir}")
                    return 0
                await page.wait_for_timeout(1000)
            print("[WTTJ] Login timeout expired; profile may not be authenticated.")
            return 1
        finally:
            await context.close()


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        validate_args(args)
        configuration = load_configuration(args.config_dir)
        if args.source == "djinni":
            # Djinni's query vocabulary (djinni_categories) is separate from
            # the shared English "queries" list used by every other source.
            djinni_config = {"categories": configuration["queries"].get("djinni_categories", {})}
            queries = select_queries(djinni_config, args.query)
        elif args.source in ("work_ua", "robota_ua"):
            ukrainian_config = {"categories": configuration["queries"].get("ukrainian_categories", {})}
            queries = select_queries(ukrainian_config, args.query)
        else:
            queries = select_queries(configuration["queries"], args.query)
    except (FileNotFoundError, ValueError) as error:
        parser.error(str(error))

    print(f"Source: {args.source}")
    print(f"Queries: {len(queries)}")
    print(f"Limit per source: {args.limit}")
    if args.check_availability:
        print(f"Availability check limit: {args.check_limit}")
    print(f"Config: {args.config_dir}")

    if args.login:
        settings = configuration["settings"]
        profile_dir = resolve_path(
            args.profile_dir
            or Path(str(settings.get("browser", {}).get("profile_dir", ".browser_profile")))
        )
        try:
            return asyncio.run(
                run_login(
                    configuration,
                    profile_dir,
                    args.headless or bool(settings.get("browser", {}).get("headless", False)),
                )
            )
        except KeyboardInterrupt:
            print("\nStopped by user.")
            return 130

    if args.check_availability:
        try:
            return asyncio.run(run_availability_check(args))
        except (FileNotFoundError, ValueError) as error:
            parser.error(str(error))
        except KeyboardInterrupt:
            print("\nStopped by user.")
            return 130

    if args.dry_run:
        print("Dry run: configuration is valid; collectors were not started.")
        return 0

    try:
        return asyncio.run(run_collection(args))
    except (FileNotFoundError, ValueError) as error:
        parser.error(str(error))
    except KeyboardInterrupt:
        print("\nStopped by user.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
