from __future__ import annotations

import asyncio
import unittest

from collectors.company_careers import (
    CompanyCareersCollector,
    build_bamboohr_detail_url,
    build_bamboohr_url,
    build_board_url,
    build_greenhouse_url,
    build_lever_url,
    configured_career_boards,
    max_jobs_per_board,
    parse_bamboohr_jobs,
    parse_greenhouse_jobs,
    parse_lever_jobs,
    _bamboohr_record,
    _greenhouse_record,
    _lever_description,
    _lever_record,
    _lever_work_format,
)


class FakeJsonResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = payload
        self.status = status
        self.ok = 200 <= status < 300

    async def json(self) -> object:
        return self._payload


class FakeRequest:
    def __init__(self, routes: dict[str, FakeJsonResponse]) -> None:
        self.routes = routes
        self.urls: list[str] = []

    async def get(self, url: str, timeout: int) -> FakeJsonResponse:
        self.urls.append(url)
        return self.routes.get(url, FakeJsonResponse({}, status=404))


class FakeContext:
    def __init__(self, routes: dict[str, FakeJsonResponse]) -> None:
        self.request = FakeRequest(routes)
        self.pages: list[object] = []


NO_DELAY = {"request": {"delay_min_seconds": 0, "delay_max_seconds": 0, "retry_attempts": 0}}
IWCONNECT = {"company": "IWConnect", "ats": "bamboohr", "slug": "iwconnect", "host": ""}
BUSINESS_ANALYST = {"query": "Business Analyst", "category": "data"}

# Shaped after the live iwconnect.bamboohr.com responses of 2026-10-05.
BAMBOOHR_REMOTE_LIST_ITEM = {
    "id": "163",
    "jobOpeningName": "B2B Technical Business Analyst*",
    "departmentLabel": "Project and Delivery Management Team",
    "employmentStatusLabel": "Contractor",
    "location": {"city": None, "state": None},
    "atsLocation": {"country": None, "state": None, "province": None, "city": None},
    "isRemote": None,
    "locationType": "1",
}
BAMBOOHR_REMOTE_OPENING = {
    "jobOpeningShareUrl": "https://iwconnect.bamboohr.com/careers/163",
    "jobOpeningName": "B2B Technical Business Analyst*",
    "jobOpeningStatus": "Open",
    "employmentStatusLabel": "Contractor",
    "location": {"city": None, "state": None, "postalCode": None, "addressCountry": None},
    "atsLocation": {"country": None, "countryId": None, "state": None, "city": None},
    "description": "<p>Experience in <strong>SQL</strong>, data mapping and BI tools.</p>",
    "compensation": None,
    "datePosted": "2026-05-11",
    "locationType": 1,
}


class CompanyCareersTests(unittest.TestCase):
    def test_builds_public_ats_urls(self) -> None:
        self.assertEqual(
            build_greenhouse_url("coinbase"),
            "https://boards-api.greenhouse.io/v1/boards/coinbase/jobs?content=true",
        )
        self.assertEqual(
            build_lever_url("wintermute"),
            "https://api.lever.co/v0/postings/wintermute?mode=json",
        )

    def test_parsers_keep_only_mapping_jobs(self) -> None:
        self.assertEqual(parse_greenhouse_jobs({"jobs": [{"title": "Risk Analyst"}, "bad"]}), [{"title": "Risk Analyst"}])
        self.assertEqual(parse_lever_jobs([{"text": "Trading Analyst"}, "bad"]), [{"text": "Trading Analyst"}])

    def test_configured_boards_validates_ats(self) -> None:
        boards = configured_career_boards({"career_boards": [
            {"company": "Coinbase", "ats": "greenhouse", "slug": "coinbase"},
            {"company": "Unknown", "ats": "other", "slug": "x"},
        ]})
        self.assertEqual(len(boards), 1)
        self.assertEqual(boards[0]["company"], "Coinbase")

    def test_builds_bamboohr_urls(self) -> None:
        self.assertEqual(build_bamboohr_url("iwconnect"), "https://iwconnect.bamboohr.com/careers/list")
        self.assertEqual(build_bamboohr_detail_url("iwconnect", "163"), "https://iwconnect.bamboohr.com/careers/163/detail")
        self.assertEqual(build_board_url(IWCONNECT), "https://iwconnect.bamboohr.com/careers/list")
        # A configured EU Lever host is used, not the default one (which 404s).
        eu_board = {"company": "Kaiko", "ats": "lever", "slug": "kaiko", "host": "https://api.eu.lever.co"}
        self.assertEqual(build_board_url(eu_board), "https://api.eu.lever.co/v0/postings/kaiko?mode=json")

    def test_bamboohr_parser_reads_the_result_list(self) -> None:
        self.assertEqual(parse_bamboohr_jobs({"meta": {}, "result": [{"id": "1"}, "bad"]}), [{"id": "1"}])
        self.assertEqual(parse_bamboohr_jobs([{"id": "1"}]), [])

    def test_configured_boards_accept_bamboohr(self) -> None:
        boards = configured_career_boards({"career_boards": [{"company": "IWConnect", "ats": "BambooHR", "slug": "iwconnect"}]})
        self.assertEqual([(board["company"], board["ats"]) for board in boards], [("IWConnect", "bamboohr")])

    def test_bamboohr_record_maps_a_remote_contractor_posting(self) -> None:
        record = _bamboohr_record(BAMBOOHR_REMOTE_LIST_ITEM, BAMBOOHR_REMOTE_OPENING, IWCONNECT, BUSINESS_ANALYST)
        self.assertEqual(record.title, "B2B Technical Business Analyst*")
        self.assertEqual(record.company, "IWConnect")
        self.assertEqual(record.work_format, "Remote")  # locationType 1
        self.assertEqual(record.contract_type, "Contractor")
        self.assertEqual(record.date_published, "2026-05-11")
        self.assertEqual(record.url, "https://iwconnect.bamboohr.com/careers/163")
        self.assertEqual((record.city_region, record.country, record.salary), ("", "", ""))
        self.assertIn("SQL", record.full_text)
        self.assertEqual(record.note, "ATS: BambooHR; board: iwconnect")

    def test_bamboohr_record_reads_hybrid_and_office_location(self) -> None:
        opening = {
            "jobOpeningName": "Senior Data Engineer (Macedonia)",
            "location": {"city": "Bitola, Skopje, Prilep, Ohrid", "state": None, "postalCode": "7000", "addressCountry": "Macedonia"},
            "description": "<p>Lakehouse platforms.</p>",
            "datePosted": "2024-07-09",
            "locationType": 2,
        }
        record = _bamboohr_record({"id": "93"}, opening, IWCONNECT, BUSINESS_ANALYST)
        self.assertEqual(record.work_format, "Hybrid")  # locationType 2
        self.assertEqual(record.city_region, "Bitola, Skopje, Prilep, Ohrid, Macedonia")
        self.assertEqual(record.country, "Macedonia")
        self.assertEqual(record.url, "https://iwconnect.bamboohr.com/careers/93")  # built when no share URL

    def test_bamboohr_record_reads_text_for_an_unseen_location_type(self) -> None:
        opening = {"jobOpeningName": "Data Analyst", "description": "Fully remote role.", "locationType": 0}
        self.assertEqual(_bamboohr_record({"id": "7"}, opening, IWCONNECT, BUSINESS_ANALYST).work_format, "Remote")

    def test_collect_fetches_bamboohr_detail_only_for_matching_titles(self) -> None:
        engineer = {"id": "93", "jobOpeningName": "Senior Data Engineer (Macedonia)", "locationType": "2"}
        broken = {"id": "200", "jobOpeningName": "Business Analyst", "locationType": "1"}
        routes = {
            "https://iwconnect.bamboohr.com/careers/list": FakeJsonResponse({"result": [engineer, BAMBOOHR_REMOTE_LIST_ITEM, broken]}),
            "https://iwconnect.bamboohr.com/careers/163/detail": FakeJsonResponse({"result": {"jobOpening": BAMBOOHR_REMOTE_OPENING}}),
        }
        context = FakeContext(routes)
        collector = CompanyCareersCollector({"career_boards": [IWCONNECT]}, NO_DELAY)
        result = asyncio.run(collector.collect(context, [BUSINESS_ANALYST], limit=10))

        self.assertEqual([record.title for record in result.records], ["B2B Technical Business Analyst*"])
        self.assertEqual(result.records[0].search_query, "Business Analyst")
        self.assertNotIn("https://iwconnect.bamboohr.com/careers/93/detail", context.request.urls)
        # A failed detail costs that one posting, not the rest of the board.
        self.assertEqual(result.errors, 1)
        self.assertIn("job 200", result.error_messages[0])

    def test_board_limit_has_safe_default(self) -> None:
        self.assertEqual(max_jobs_per_board({}), 10)
        self.assertEqual(max_jobs_per_board({"max_jobs_per_board": "3"}), 3)
        self.assertEqual(max_jobs_per_board({"max_jobs_per_board": 0}), 1)

    def test_lever_description_includes_lists_and_additional(self) -> None:
        job = {
            "descriptionPlain": "About Binance blah blah.",
            "lists": [
                {"text": "Responsibilities", "content": "Do the thing."},
                {"text": "Requirements", "content": "3+ years experience."},
                "not a dict",
            ],
            "additionalPlain": "We offer great benefits.",
        }
        description = _lever_description(job)
        self.assertIn("About Binance blah blah.", description)
        self.assertIn("Responsibilities\nDo the thing.", description)
        self.assertIn("Requirements\n3+ years experience.", description)
        self.assertIn("We offer great benefits.", description)

    def test_lever_description_falls_back_to_description_only(self) -> None:
        job = {"description": "Just a plain description, no lists."}
        self.assertEqual(_lever_description(job), "Just a plain description, no lists.")

    def test_lever_work_format_prefers_structured_workplace_type(self) -> None:
        # Confirmed against a real board (Wintermute): Lever populates
        # workplaceType on every posting even when the free-text description
        # never states a format - text-mining alone left these "Unknown".
        self.assertEqual(_lever_work_format({"workplaceType": "hybrid"}, ""), "Hybrid")
        self.assertEqual(_lever_work_format({"workplaceType": "onsite"}, ""), "On-site")
        self.assertEqual(_lever_work_format({"workplaceType": "remote"}, ""), "Remote")

    def test_lever_work_format_falls_back_to_text_when_field_missing(self) -> None:
        self.assertEqual(_lever_work_format({}, "This is a fully remote role."), "Remote")
        self.assertEqual(_lever_work_format({"workplaceType": None}, "Office-based role."), "On-site")

    def test_lever_record_uses_structured_country_field(self) -> None:
        job = {
            "text": "Trading Analyst",
            "workplaceType": "hybrid",
            "country": "GB",
            "categories": {"location": "London", "commitment": "Full-time"},
            "description": "Join our London team.",
            "hostedUrl": "https://jobs.lever.co/wintermute/abc",
        }
        record = _lever_record(job, {"company": "Wintermute", "ats": "lever", "slug": "wintermute"}, {"query": "Trading Analyst", "category": "trading"})
        self.assertEqual(record.work_format, "Hybrid")
        self.assertEqual(record.country, "GB")

    def test_lever_record_falls_back_when_structured_fields_absent(self) -> None:
        job = {
            "text": "Data Analyst",
            "categories": {"location": "Paris, France", "commitment": "Full-time"},
            "description": "This is a fully remote role.",
            "hostedUrl": "https://jobs.lever.co/example/def",
        }
        record = _lever_record(job, {"company": "Example", "ats": "lever", "slug": "example"}, {"query": "Data Analyst", "category": "data"})
        self.assertEqual(record.work_format, "Remote")
        self.assertEqual(record.country, "France")
        self.assertEqual(record.date_published, "")

    def test_lever_record_reads_created_at_as_publish_date(self) -> None:
        job = {
            "text": "Data Analyst",
            "categories": {"location": "Remote"},
            "description": "Remote role.",
            "hostedUrl": "https://jobs.lever.co/example/ghi",
            "createdAt": 1759658400000,  # epoch ms, 2025-10-05T10:00:00Z
        }
        record = _lever_record(job, {"company": "Example", "ats": "lever", "slug": "example"}, {"query": "Data Analyst", "category": "data"})
        self.assertEqual(record.date_published, "2025-10-05T10:00:00+00:00")

    def test_greenhouse_publish_date_is_first_published_not_updated_at(self) -> None:
        job = {
            "absolute_url": "https://boards.greenhouse.io/example/jobs/1",
            "location": {"name": "Remote"},
            "first_published": "2026-08-13T04:21:27-04:00",
            "updated_at": "2026-09-30T09:00:00-04:00",
            "title": "Data Analyst",
            "content": "<p>Remote role.</p>",
        }
        board = {"company": "Example", "ats": "greenhouse", "slug": "example"}
        query = {"query": "Data Analyst", "category": "data"}
        self.assertEqual(_greenhouse_record(job, board, query).date_published, "2026-08-13T04:21:27-04:00")
        del job["first_published"]
        self.assertEqual(_greenhouse_record(job, board, query).date_published, "2026-09-30T09:00:00-04:00")

    def test_greenhouse_record_maps_fields_from_a_real_shaped_payload(self) -> None:
        # Shaped after a real Coinbase boards-api response (fetched 2026-09-08):
        # Greenhouse has no structured work-format field, only a free-text
        # `location.name` (see CLAUDE.md "Resolved" 2026-09-08).
        job = {
            "absolute_url": "https://www.coinbase.com/careers/positions/8053751?gh_jid=8053751",
            "location": {"name": "Remote - Cyprus"},
            "updated_at": "2026-08-28T10:51:50-04:00",
            "title": "Accountant, Cyprus",
            "content": "<p>Join our Finance team. Salary: $80 000 - $95 000 per year.</p>",
            "metadata": [{"id": 166433, "name": "Team", "value": "Finance", "value_type": "single_select"}],
        }
        record = _greenhouse_record(
            job,
            {"company": "Coinbase", "ats": "greenhouse", "slug": "coinbase"},
            {"query": "Accountant", "category": "finance"},
        )
        self.assertEqual(record.title, "Accountant, Cyprus")
        self.assertEqual(record.company, "Coinbase")
        self.assertEqual(record.city_region, "Remote - Cyprus")
        self.assertEqual(record.country, "Cyprus")
        self.assertEqual(record.work_format, "Remote")
        self.assertEqual(record.url, "https://www.coinbase.com/careers/positions/8053751?gh_jid=8053751")
        self.assertIn("Join our Finance team.", record.full_text)
        self.assertEqual(record.salary, "$80 000 - $95 000 per year")
        self.assertEqual(record.note, "ATS: Greenhouse; board: coinbase")
        self.assertTrue(record.job_id)

    def test_greenhouse_record_reads_hybrid_from_location_name_when_description_is_silent(self) -> None:
        # Real-shaped: many Greenhouse boards mark hybrid roles only in
        # location.name ("Hybrid - Bangalore, India"), never in prose.
        job = {
            "absolute_url": "https://boards.greenhouse.io/example/jobs/1",
            "location": {"name": "Hybrid - Bangalore, India"},
            "title": "Risk Analyst",
            "content": "Join our growing risk team.",
        }
        record = _greenhouse_record(
            job,
            {"company": "Example", "ats": "greenhouse", "slug": "example"},
            {"query": "Risk Analyst", "category": "risk"},
        )
        self.assertEqual(record.work_format, "Hybrid")
        self.assertEqual(record.country, "India")

    def test_greenhouse_record_leaves_work_format_unknown_for_a_bare_office_name(self) -> None:
        # Documented gap (CLAUDE.md "Resolved", 2026-09-08): a bare office
        # location with no Remote/Hybrid qualifier is usually on-site in
        # Greenhouse's convention, but this is not inferred - left "Unknown"
        # unless the description itself states the format.
        job = {
            "absolute_url": "https://boards.greenhouse.io/example/jobs/2",
            "location": {"name": "Chicago, IL"},
            "title": "Data Analyst",
            "content": "Join our data team in Chicago.",
        }
        record = _greenhouse_record(
            job,
            {"company": "Example", "ats": "greenhouse", "slug": "example"},
            {"query": "Data Analyst", "category": "data"},
        )
        self.assertEqual(record.work_format, "Unknown")


if __name__ == "__main__":
    unittest.main()
