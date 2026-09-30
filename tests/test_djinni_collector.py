from __future__ import annotations

import asyncio
import unittest

from collectors.djinni import (
    DjinniCollector,
    build_rss_url,
    extract_company,
    matches_primary_keyword,
    parse_company_from_detail_html,
    parse_rss_items,
    record_from_item,
)


SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Djinni</title>
<item>
<title>Product Analyst</title>
<link>https://djinni.co/jobs/839662-product-analyst/</link>
<description>&lt;p&gt;&lt;strong&gt;Novoplex&lt;/strong&gt; is a company. This role is fully remote across Ukraine.&lt;/p&gt;</description>
<pubDate>Tue, 01 Sep 2026 15:44:22 +0300</pubDate>
<guid>https://djinni.co/jobs/839662-product-analyst/</guid>
<category>Data Analyst</category>
</item>
<item>
<title>Аналітик ринку</title>
<link>https://djinni.co/jobs/843517-analitik-rinku/</link>
<description>Офіс ефективного регулювання &lt;strong&gt;BRDO&lt;/strong&gt; шукає аналітика. Формат роботи: віддалено.</description>
<pubDate>Thu, 03 Sep 2026 11:48:47 +0300</pubDate>
<guid>https://djinni.co/jobs/843517-analitik-rinku/</guid>
<category>Support</category>
</item>
</channel></rss>"""


class FakeResponse:
    def __init__(self, body: str) -> None:
        self.ok = True
        self.status = 200
        self._body = body

    async def text(self) -> str:
        return self._body


class FakeRequest:
    async def get(self, url: str, timeout: int) -> FakeResponse:
        # RSS feed for the listing URL, an empty detail page for everything
        # else (company name then falls back to the <strong> heuristic).
        return FakeResponse(SAMPLE_RSS if "/jobs/rss/" in url else "")


class FakeContext:
    request = FakeRequest()


def run_collect(primary_keyword: str):
    collector = DjinniCollector({"request": {"delay_min_seconds": 0, "delay_max_seconds": 0}})
    query = {"query": primary_keyword, "category": "data"}
    return asyncio.run(collector.collect(FakeContext(), [query], limit=10))


class DjinniCollectorTests(unittest.TestCase):
    def test_build_rss_url_encodes_primary_keyword(self) -> None:
        self.assertEqual(
            build_rss_url("Data Analytics"),
            "https://djinni.co/jobs/rss/?primary_keyword=Data+Analytics",
        )

    def test_extract_company_reads_leading_strong_tag(self) -> None:
        self.assertEqual(extract_company("<p><strong>Novoplex</strong> is a company.</p>"), "Novoplex")
        self.assertEqual(extract_company("No bold tag here."), "")

    def test_parse_company_from_detail_html_reads_company_profile_link(self) -> None:
        # Confirmed 2026-09-09: the real job detail page (not the RSS
        # description) links to the company's Djinni profile with this exact
        # href shape - the reliable source, unlike the <strong>-tag heuristic
        # which often grabs a bolded role title or section header instead.
        html_page = (
            '<h1>Data Analyst</h1>'
            '<a href="/jobs/company-office8/">Office8</a>'
            '<p>Ми шукаємо <strong>Data Analyst</strong>...</p>'
        )
        self.assertEqual(parse_company_from_detail_html(html_page), "Office8")

    def test_parse_company_from_detail_html_returns_empty_when_no_link(self) -> None:
        self.assertEqual(parse_company_from_detail_html("<p>No company link here.</p>"), "")

    def test_parse_company_from_detail_html_skips_textless_logo_link(self) -> None:
        # Regression: a real page (SKELAR, 2026-09-09) has two links to the
        # company profile - a relative-href logo link with no text content,
        # and an absolute-href text link with the visible name - in that
        # order. The first version of this parser only matched relative
        # hrefs and required text immediately after the opening tag, so it
        # silently found nothing and fell through to the <strong> heuristic.
        html_page = (
            '<a href="/jobs/company-skelar/" class="picture">'
            '<div class="recruiter-images-container"><div>logo</div></div>'
            '</a>'
            '<a href="https://djinni.co/jobs/company-skelar/" class="text-secondary fw-medium">'
            '\n                    SKELAR\n                  '
            '</a>'
        )
        self.assertEqual(parse_company_from_detail_html(html_page), "SKELAR")

    def test_parse_rss_items_reads_all_fields(self) -> None:
        items = parse_rss_items(SAMPLE_RSS)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "Product Analyst")
        self.assertEqual(items[0]["link"], "https://djinni.co/jobs/839662-product-analyst/")
        self.assertIn("Novoplex", items[0]["description"])
        self.assertEqual(items[0]["pub_date"], "Tue, 01 Sep 2026 15:44:22 +0300")

    def test_parse_rss_items_reads_categories(self) -> None:
        items = parse_rss_items(SAMPLE_RSS)
        self.assertEqual(items[0]["categories"], ["Data Analyst"])
        self.assertEqual(items[1]["categories"], ["Support"])

    def test_matches_primary_keyword_is_case_insensitive(self) -> None:
        item = {"categories": ["Data Analyst"]}
        self.assertTrue(matches_primary_keyword(item, "data analyst"))
        self.assertFalse(matches_primary_keyword(item, "Data Analytics"))
        self.assertFalse(matches_primary_keyword({"categories": []}, "Data Analyst"))

    def test_collect_drops_items_from_other_categories(self) -> None:
        result = run_collect("Data Analyst")
        self.assertEqual(result.errors, 0)
        self.assertEqual([record.title for record in result.records], ["Product Analyst"])

    def test_collect_reports_unfiltered_fallback_feed_as_error(self) -> None:
        # Regression: on 2026-09-29 primary_keyword="Data Analytics" silently
        # returned Djinni's unfiltered feed (Support/DevOps/Marketing roles).
        # No item carries the requested category, so this must be an error,
        # not 13 off-profile records.
        result = run_collect("Data Analytics")
        self.assertEqual(result.records, [])
        self.assertEqual(result.errors, 1)
        self.assertIn("Data Analytics", result.error_messages[0])

    def test_parse_rss_items_tolerates_malformed_xml(self) -> None:
        self.assertEqual(parse_rss_items("not xml at all <<<"), [])

    def test_record_from_item_builds_job_record(self) -> None:
        items = parse_rss_items(SAMPLE_RSS)
        query = {"query": "Data Analytics", "category": "data"}

        record = record_from_item(items[0], query)
        self.assertEqual(record.source, "Djinni")
        self.assertEqual(record.title, "Product Analyst")
        self.assertEqual(record.company, "Novoplex")
        self.assertEqual(record.country, "Ukraine")
        self.assertEqual(record.work_format, "Remote")
        self.assertEqual(record.job_category, "data")
        self.assertEqual(record.search_query, "Data Analytics")
        self.assertTrue(record.job_id)

        ukrainian_record = record_from_item(items[1], query)
        self.assertEqual(ukrainian_record.company, "BRDO")
        self.assertEqual(ukrainian_record.work_format, "Remote")


if __name__ == "__main__":
    unittest.main()
