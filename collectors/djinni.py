"""Djinni.co collector — Ukraine's largest IT job board, via its RSS feed.

No browser page rendering needed: djinni.co/jobs/rss/ accepts a
``primary_keyword`` query parameter that filters the feed server-side, so
this collector is a plain HTTP GET + XML parse, same shape as the Greenhouse/
Lever adapters in company_careers.py — far more stable than the LinkedIn/WTTJ
HTML-scraping path.

IMPORTANT: an unrecognized ``primary_keyword`` value does NOT error — Djinni
silently falls back to the unfiltered "all jobs" feed (mostly software-
engineering roles, verified 2026-09-04). Only add values to
config/job_queries.yaml's ``djinni_categories`` after confirming
https://djinni.co/jobs/?primary_keyword=<value> actually filters results —
the same trap already documented for WTTJ's topic page.

A value that used to filter can also stop working: "Data Analytics" did on
2026-09-29. Guard: every RSS ``<item>`` carries a ``<category>`` tag, and in a
correctly filtered feed all items have ``<category>`` equal to the keyword
(verified 100/100 for "Data Analyst" and "Business Analyst"), while the
fallback feed mixes Marketing/Support/DevOps/etc. ``matches_primary_keyword``
drops items from other categories, and a non-empty feed with zero matching
items is reported as an error rather than an empty result.

Company name: the RSS ``<description>`` has no structured company field, only
free-text HTML — the old heuristic (first ``<strong>`` tag) was frequently
wrong, since postings often open with a bolded restatement of the role
("Ми шукаємо **Data Analyst**...") or a bolded section header ("**Вимоги**"),
not the company name. Confirmed 2026-09-09: the actual job detail page (a
plain server-rendered HTML page, not a JS SPA) has a stable
``<a href="/jobs/company-{slug}/">{Name}</a>`` link. ``fetch_company_name``
does one extra plain GET per record to read it, falling back to the old
``<strong>``-tag heuristic if that fetch or match fails. This trades away
some of Djinni's collection speed (previously RSS-only, no per-record
requests) for correct company names — worth it since `company` is a field
the user actually reads when deciding where to apply.
"""

from __future__ import annotations

import asyncio
import html
import random
import re
from dataclasses import replace
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from xml.etree import ElementTree

from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext

from collectors.base import BaseCollector, CollectorResult, get_with_retry, setting
from core.metadata import extract_salary, infer_work_format
from core.ids import build_job_id
from core.models import JobRecord


DJINNI_RSS_URL = "https://djinni.co/jobs/rss/"
# A detail page links to the company's profile at least twice: a logo link
# with no text content (class="picture", just wraps an <img>/<div>) and a
# text link with the visible name - and the text link's href is absolute
# (https://djinni.co/jobs/company-...) while the logo link's is relative
# (/jobs/company-...). Confirmed 2026-09-09 after the first version of this
# pattern (relative-only) silently missed the text link and fell through to
# the unreliable <strong>-tag heuristic. Matching both href forms and taking
# the first match with actual (non-whitespace) text skips the empty logo
# link regardless of which one appears first in the HTML.
COMPANY_LINK_PATTERN = re.compile(r'<a\s+href="(?:https?://djinni\.co)?/jobs/company-[^"]+/"[^>]*>([^<]{1,120})</a>')


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def build_rss_url(primary_keyword: str) -> str:
    return f"{DJINNI_RSS_URL}?{urlencode({'primary_keyword': primary_keyword})}"


def parse_rss_items(xml_text: str) -> list[dict[str, str]]:
    """Parse a Djinni RSS document into plain dicts, tolerating malformed XML."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    items = []
    for item in root.iter("item"):
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "description": item.findtext("description") or "",
            "pub_date": (item.findtext("pubDate") or "").strip(),
            "categories": [(tag.text or "").strip() for tag in item.findall("category") if tag.text],
        })
    return items


def matches_primary_keyword(item: dict[str, Any], primary_keyword: str) -> bool:
    """Whether an RSS item belongs to the Djinni category the feed was filtered by."""
    keyword = primary_keyword.strip().casefold()
    return any(category.casefold() == keyword for category in item.get("categories", []))


def extract_company(description_html: str) -> str:
    """Fallback company-name heuristic: Djinni descriptions often open with it in <strong>.

    Not a structured field in the RSS feed (unlike Greenhouse/Lever's explicit
    company field) — this is a heuristic and can miss postings that don't lead
    with the company name, or grab a bolded role/section title instead. Used
    only when ``fetch_company_name`` (the reliable detail-page link) fails.
    """
    match = re.search(r"<strong>([^<]{2,80})</strong>", description_html)
    return clean_text(match.group(1)) if match else ""


def parse_company_from_detail_html(detail_html: str) -> str:
    """Extract the company name from a fetched job detail page's HTML.

    Takes the first company-profile-link match with actual visible text,
    skipping a logo link to the same company that has none.
    """
    for match in COMPANY_LINK_PATTERN.finditer(detail_html):
        text = clean_text(match.group(1))
        if text:
            return text
    return ""


async def fetch_company_name(
    context: BrowserContext,
    url: str,
    *,
    timeout_ms: int,
    retry_attempts: int,
    delay_min_seconds: float,
    delay_max_seconds: float,
) -> str:
    """Read the real company name off the job detail page's stable profile link.

    Best-effort: any failure (network error, unexpected markup) just returns
    "" so the caller can fall back to the RSS-description heuristic instead
    of failing the whole record.
    """
    try:
        response = await get_with_retry(
            context, url, timeout_ms=timeout_ms, retry_attempts=retry_attempts,
            delay_min_seconds=delay_min_seconds, delay_max_seconds=delay_max_seconds,
        )
        detail_html = await response.text()
    except Exception:
        return ""
    return parse_company_from_detail_html(detail_html)


def record_from_item(item: dict[str, str], query: dict[str, str]) -> JobRecord:
    description_text = clean_text(item["description"])
    work_format = infer_work_format(description_text)
    record = JobRecord(
        source="Djinni",
        date_collected=datetime.now().astimezone().isoformat(timespec="seconds"),
        date_published=item["pub_date"],
        title=clean_text(item["title"]),
        company=extract_company(item["description"]),
        city_region="",
        country="Ukraine",
        work_format=work_format,
        source_work_format=work_format,
        salary=extract_salary({}, description_text),
        url=item["link"],
        full_text=description_text,
        status="collected",
        availability_status="active",
        search_query=query["query"],
        job_category=query["category"],
    )
    record.job_id = build_job_id(record)
    return record


class DjinniCollector(BaseCollector):
    """Collect matching postings from Djinni.co's filtered RSS feed."""

    source_name = "Djinni"

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or {}

    async def collect(
        self,
        context: BrowserContext,
        queries: list[dict[str, str]],
        limit: int,
    ) -> CollectorResult:
        result = CollectorResult(source=self.source_name, queries=[item["query"] for item in queries])
        timeout_ms = int(setting(self.settings, "request", "timeout_seconds", default=60) * 1000)
        retry_attempts = int(setting(self.settings, "request", "retry_attempts", default=0))
        delay_min = float(setting(self.settings, "request", "delay_min_seconds", default=1.0))
        delay_max = float(setting(self.settings, "request", "delay_max_seconds", default=delay_min))
        seen_urls: set[str] = set()

        for query in queries:
            if len(result.records) >= limit:
                break
            url = build_rss_url(query["query"])
            try:
                response = await get_with_retry(
                    context, url, timeout_ms=timeout_ms, retry_attempts=retry_attempts,
                    delay_min_seconds=delay_min, delay_max_seconds=delay_max,
                )
                xml_text = await response.text()
                items = parse_rss_items(xml_text)
                matching = [item for item in items if matches_primary_keyword(item, query["query"])]
                if items and not matching:
                    raise ValueError(
                        f"Djinni ignored primary_keyword={query['query']!r}: none of {len(items)} feed "
                        "items is in that category (unfiltered fallback feed). Remove or replace it in "
                        "config/job_queries.yaml djinni_categories."
                    )
                for item in matching:
                    if len(result.records) >= limit:
                        break
                    if not item["link"] or item["link"] in seen_urls:
                        continue
                    seen_urls.add(item["link"])
                    record = record_from_item(item, query)
                    company = await fetch_company_name(
                        context, item["link"], timeout_ms=timeout_ms, retry_attempts=retry_attempts,
                        delay_min_seconds=delay_min, delay_max_seconds=delay_max,
                    )
                    if company:
                        record = replace(record, company=company)
                    result.records.append(record)
                    await asyncio.sleep(random.uniform(delay_min, max(delay_min, delay_max)))
            except Exception as error:
                result.errors += 1
                result.error_messages.append(f"{query['query']}: {error}")

        result.found = len(result.records)
        return result
