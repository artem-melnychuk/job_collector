"""DOU.ua collector — Ukraine's main IT community job board, via its RSS feeds.

Plain HTTP GET + XML (https://jobs.dou.ua/vacancies/feeds/), no browser
rendering and no bot challenge, same tier as djinni.py. Added 2026-10-05.

Verified live 2026-10-05:
- Feeds exist per category (``?category=Analyst``) and per search
  (``?search=data analyst``); each returns the 25 newest postings, with the
  full description in ``<description>``. There is no archive, so regular
  runs are what builds coverage.
- The ``<title>`` carries structured fields:
  ``<Title> в <Company>[, $<min>–<max>][, <place>, ...]``, for example
  ``Senior Data Analyst в Starlight Media, $1300–2000, віддалено``. Places
  are cities ("Київ", "Варшава (Польща)"), "Україна", "за кордоном"
  (abroad) and "віддалено" (remote). A company name can itself contain a
  comma ("Delta Labs, Studio 3"), so the title is read left to right: the
  company continues until the first salary or place token.
- The salary in the title is the board's own field, monthly by DOU
  convention, so it is stored as given with "/month"; nothing is guessed
  from description text (see the fake-salary note in jobicy.py).

Work format comes from the place list: "віддалено" -> Remote; only cities
-> On-site (DOU lists the office cities), so office-only postings in
Ukraine are dropped by the existing disallowed_onsite_countries filter.
Country is "Ukraine" when a Ukrainian city or "Україна" is listed, the
named foreign country when only foreign cities are, otherwise blank.

Relevance: the category and search feeds are already filtered by DOU,
but search is full-text, so a title must still name an analyst role
(English or Ukrainian/Russian) to be kept. Titles are mixed-language, so
the English query_matches check is used only to label search_query and
category, not to reject. Not in CONTEXT_FILTERED_SOURCES, like djinni.py.
"""

from __future__ import annotations

import asyncio
import html
import random
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext

from collectors.base import BaseCollector, CollectorResult, get_with_retry, setting
from core.ids import build_job_id, normalize_url
from core.metadata import query_matches
from core.models import JobRecord


DOU_FEED_URL = "https://jobs.dou.ua/vacancies/feeds/"
DEFAULT_CATEGORIES = ("Analyst",)
DEFAULT_SEARCHES = ("data analyst", "product analyst", "business analyst", "bi analyst")
REMOTE = "віддалено"
ABROAD = "за кордоном"
UKRAINE = "Україна"
ANALYST_TITLE = re.compile(r"analy|аналіт|аналит", re.IGNORECASE)
SALARY_TOKEN = re.compile(r"^(?:\$|€|£)?\s?\d[\d\s]*(?:[–-]\s?\d[\d\s]*)?\s?(?:\$|€|£|грн|USD|EUR)?$", re.IGNORECASE)
FOREIGN_CITY = re.compile(r"^(?P<city>[^()]+?)\s*\((?P<country>[^()]+)\)$")
# Country names as DOU writes them (Ukrainian) -> the English names the rest
# of the pipeline uses. Unknown ones are kept as written.
COUNTRY_NAMES = {
    "Польща": "Poland", "Португалія": "Portugal", "Німеччина": "Germany", "Іспанія": "Spain",
    "Кіпр": "Cyprus", "Болгарія": "Bulgaria", "Чехія": "Czechia", "Румунія": "Romania",
    "Естонія": "Estonia", "Литва": "Lithuania", "Латвія": "Latvia", "Нідерланди": "Netherlands",
    "Великобританія": "United Kingdom", "США": "USA", "ОАЕ": "UAE", "Грузія": "Georgia",
    "Молдова": "Moldova", "Словаччина": "Slovakia", "Угорщина": "Hungary", "Франція": "France",
    "Італія": "Italy", "Австрія": "Austria", "Ізраїль": "Israel", "Канада": "Canada",
    "Швейцарія": "Switzerland", "Мальта": "Malta", "Сербія": "Serbia", "Хорватія": "Croatia",
}


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def build_feed_url(*, category: str = "", search: str = "") -> str:
    params = {"category": category} if category else {"search": search}
    return f"{DOU_FEED_URL}?{urlencode(params)}"


def clean_link(link: str) -> str:
    """Drop the feed's ?utm_source=jobsrss so the stored URL is the plain posting URL."""
    parts = urlsplit(link.strip())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _is_place(token: str) -> bool:
    if token in {REMOTE, ABROAD, UKRAINE}:
        return True
    # Cities: capitalized words, no digits ("Київ", "Вишневе (Київська обл.)").
    return bool(token) and token[0].isupper() and not re.search(r"\d", token) and len(token.split()) <= 4


def parse_title(raw_title: str) -> dict[str, Any]:
    """Split a DOU feed title into title, company, salary and places."""
    text = clean_text(raw_title)
    if " в " not in text:
        return {"title": text, "company": "", "salary": "", "places": []}
    title, rest = text.rsplit(" в ", 1)
    tokens = [token.strip() for token in rest.split(",") if token.strip()]
    company_tokens = tokens[:1]
    index = 1
    while index < len(tokens) and not SALARY_TOKEN.match(tokens[index]) and not _is_place(tokens[index]):
        company_tokens.append(tokens[index])
        index += 1
    salary = ""
    places = []
    for token in tokens[index:]:
        if not salary and SALARY_TOKEN.match(token):
            salary = token
        else:
            places.append(token)
    return {"title": title.strip(), "company": ", ".join(company_tokens), "salary": salary, "places": places}


def work_format_from_places(places: list[str]) -> str:
    if REMOTE in places:
        return "Remote"
    if any(place not in {ABROAD, UKRAINE} for place in places):
        return "On-site"
    return "Unknown"


def country_from_places(places: list[str]) -> str:
    foreign = []
    for place in places:
        if place in {REMOTE, ABROAD}:
            continue
        match = FOREIGN_CITY.match(place)
        if match and not match.group("country").endswith("обл."):
            foreign.append(COUNTRY_NAMES.get(match.group("country").strip(), match.group("country").strip()))
        else:
            return "Ukraine"  # a Ukrainian city, an oblast town, or "Україна"
    return foreign[0] if foreign else ""


def format_salary(salary: str) -> str:
    return f"{salary}/month" if salary else ""


def parse_feed(xml_text: str) -> list[dict[str, str]]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    return [
        {
            "title": item.findtext("title") or "",
            "link": item.findtext("link") or "",
            "description": item.findtext("description") or "",
            "pub_date": (item.findtext("pubDate") or "").strip(),
        }
        for item in root.iter("item")
    ]


def record_from_item(item: dict[str, str], query: dict[str, str]) -> JobRecord:
    parsed = parse_title(item["title"])
    places = parsed["places"]
    work_format = work_format_from_places(places)
    record = JobRecord(
        source="DOU",
        date_collected=datetime.now().astimezone().isoformat(timespec="seconds"),
        date_published=item["pub_date"],
        title=parsed["title"],
        company=parsed["company"],
        city_region=", ".join(places),
        country=country_from_places(places),
        work_format=work_format,
        source_work_format=work_format,
        salary=format_salary(parsed["salary"]),
        url=clean_link(item["link"]),
        full_text=clean_text(item["description"]),
        status="collected",
        availability_status="active",
        note="Source: DOU.ua RSS (jobs.dou.ua)",
        search_query=query["query"],
        job_category=query["category"],
    )
    record.job_id = build_job_id(record)
    return record


class DouCollector(BaseCollector):
    """Collect analyst postings from DOU.ua's category and search RSS feeds."""

    source_name = "DOU"

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or {}

    async def collect(self, context: BrowserContext, queries: list[dict[str, str]], limit: int) -> CollectorResult:
        result = CollectorResult(source=self.source_name, queries=[item["query"] for item in queries])
        timeout_ms = int(setting(self.settings, "request", "timeout_seconds", default=60) * 1000)
        retry_attempts = int(setting(self.settings, "request", "retry_attempts", default=0))
        delay_min = float(setting(self.settings, "request", "delay_min_seconds", default=1.0))
        delay_max = float(setting(self.settings, "request", "delay_max_seconds", default=delay_min))
        categories = setting(self.settings, "sources", "dou", "categories", default=list(DEFAULT_CATEGORIES))
        searches = setting(self.settings, "sources", "dou", "searches", default=list(DEFAULT_SEARCHES))
        feeds = [("category", value) for value in categories] + [("search", value) for value in searches]
        seen: set[str] = set()

        for index, (kind, value) in enumerate(feeds):
            if len(result.records) >= limit:
                break
            if index:
                await asyncio.sleep(random.uniform(delay_min, max(delay_min, delay_max)))
            url = build_feed_url(**{kind: value})
            try:
                response = await get_with_retry(
                    context, url, timeout_ms=timeout_ms, retry_attempts=retry_attempts,
                    delay_min_seconds=delay_min, delay_max_seconds=delay_max,
                )
                items = parse_feed(await response.text())
            except Exception as error:
                result.errors += 1
                result.error_messages.append(f"DOU {kind} {value!r}: {error}")
                continue

            for item in items:
                if len(result.records) >= limit:
                    break
                title = parse_title(item["title"])["title"]
                key = normalize_url(clean_link(item["link"]))
                if not key or key in seen or not ANALYST_TITLE.search(title):
                    continue
                seen.add(key)
                matched = next((query for query in queries if query_matches(query["query"], title)), None)
                query = matched or {"query": value, "category": "data"}
                result.records.append(record_from_item(item, query))

        result.found = len(result.records)
        return result
