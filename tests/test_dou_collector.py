from __future__ import annotations

import asyncio
import unittest
from urllib.parse import parse_qs, urlsplit

from collectors.dou import (
    DouCollector,
    build_feed_url,
    clean_link,
    country_from_places,
    parse_feed,
    parse_title,
    record_from_item,
    work_format_from_places,
)


def feed(*items: tuple[str, str]) -> str:
    body = "".join(
        f"<item><title>{title}</title><link>{link}?utm_source=jobsrss</link>"
        f"<description>&lt;p&gt;SQL and Power BI&lt;/p&gt;</description>"
        f"<pubDate>Mon, 05 Oct 2026 12:03:51 +0300</pubDate></item>"
        for title, link in items
    )
    return f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>{body}</channel></rss>'


class FakeResponse:
    ok = True
    status = 200

    def __init__(self, text: str) -> None:
        self._text = text

    async def text(self) -> str:
        return self._text


class FakeRequest:
    def __init__(self, feeds: dict[str, str]) -> None:
        self.feeds = feeds
        self.urls: list[str] = []

    async def get(self, url: str, timeout: int) -> FakeResponse:
        self.urls.append(url)
        params = parse_qs(urlsplit(url).query)
        key = (params.get("category") or params.get("search"))[0]
        return FakeResponse(self.feeds.get(key, feed()))


class FakeContext:
    def __init__(self, feeds: dict[str, str]) -> None:
        self.request = FakeRequest(feeds)


QUERIES = [{"query": "Data Analyst", "category": "data"}]
SETTINGS = {"request": {"delay_min_seconds": 0, "delay_max_seconds": 0},
            "sources": {"dou": {"categories": ["Analyst"], "searches": ["data analyst"]}}}


class ParseTitleTests(unittest.TestCase):
    def test_title_company_salary_and_places(self) -> None:
        parsed = parse_title("Senior Data Analyst в Starlight Media, $1300–2000, віддалено")
        self.assertEqual(parsed, {"title": "Senior Data Analyst", "company": "Starlight Media",
                                  "salary": "$1300–2000", "places": ["віддалено"]})

    def test_company_name_with_a_comma_is_kept_whole(self) -> None:
        # Seen live 2026-10-05: a company name containing a comma, with a
        # digit in the second part (example name is made up).
        parsed = parse_title("Data Intelligence Analyst в Delta Labs, Studio 3, $600–2800")
        self.assertEqual(parsed["company"], "Delta Labs, Studio 3")
        self.assertEqual(parsed["salary"], "$600–2800")
        self.assertEqual(parsed["places"], [])

    def test_title_containing_v_preposition_splits_on_the_last_one(self) -> None:
        parsed = parse_title("Аналітик в банк в Банк &quot;КД&quot;, Київ")
        self.assertEqual(parsed["title"], "Аналітик в банк")
        self.assertEqual(parsed["company"], 'Банк "КД"')


class PlacesTests(unittest.TestCase):
    def test_work_format(self) -> None:
        self.assertEqual(work_format_from_places(["Київ", "Львів", "віддалено"]), "Remote")
        self.assertEqual(work_format_from_places(["Київ"]), "On-site")  # office cities only
        self.assertEqual(work_format_from_places(["за кордоном"]), "Unknown")
        self.assertEqual(work_format_from_places([]), "Unknown")

    def test_country(self) -> None:
        self.assertEqual(country_from_places(["Київ", "віддалено"]), "Ukraine")
        self.assertEqual(country_from_places(["Україна", "віддалено"]), "Ukraine")
        self.assertEqual(country_from_places(["Вишневе (Київська обл.)"]), "Ukraine")  # oblast, not a country
        self.assertEqual(country_from_places(["Варшава (Польща)", "віддалено"]), "Poland")
        self.assertEqual(country_from_places(["віддалено"]), "")


class RecordTests(unittest.TestCase):
    def test_record_fields_and_clean_link(self) -> None:
        item = parse_feed(feed(("Senior Data Analyst в Uklon, $1300–2000, Київ, віддалено",
                                "https://jobs.dou.ua/companies/uklon/vacancies/1/")))[0]
        record = record_from_item(item, QUERIES[0])
        self.assertEqual(record.source, "DOU")
        self.assertEqual(record.company, "Uklon")
        self.assertEqual(record.salary, "$1300–2000/month")
        self.assertEqual(record.work_format, "Remote")
        self.assertEqual(record.country, "Ukraine")
        self.assertEqual(record.url, "https://jobs.dou.ua/companies/uklon/vacancies/1/")
        self.assertIn("Power BI", record.full_text)
        self.assertTrue(record.job_id.startswith("dou_"))
        self.assertEqual(clean_link("https://x.ua/a/?utm_source=jobsrss"), "https://x.ua/a/")

    def test_build_feed_url(self) -> None:
        self.assertEqual(parse_qs(urlsplit(build_feed_url(category="Analyst")).query), {"category": ["Analyst"]})
        self.assertEqual(parse_qs(urlsplit(build_feed_url(search="bi analyst")).query), {"search": ["bi analyst"]})


class CollectTests(unittest.TestCase):
    def test_keeps_analyst_titles_once_across_feeds(self) -> None:
        shared = ("Product Data Analyst в appflame, Київ, віддалено", "https://jobs.dou.ua/companies/a/vacancies/1/")
        feeds = {
            "Analyst": feed(shared, ("Бізнес-аналітик в Strix Air, Дніпро", "https://jobs.dou.ua/companies/s/vacancies/2/")),
            "data analyst": feed(shared, ("Financial Controller в EOS, віддалено", "https://jobs.dou.ua/companies/e/vacancies/3/")),
        }
        context = FakeContext(feeds)
        result = asyncio.run(DouCollector(SETTINGS).collect(context, QUERIES, limit=10))
        self.assertEqual([r.title for r in result.records], ["Product Data Analyst", "Бізнес-аналітик"])
        self.assertEqual(result.records[0].search_query, "Data Analyst")  # labelled by the matching query
        self.assertEqual(result.records[1].search_query, "Analyst")       # Ukrainian title: labelled by its feed
        self.assertEqual(len(context.request.urls), 2)


if __name__ == "__main__":
    unittest.main()
