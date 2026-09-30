from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from core.cv_draft import (
    SYSTEM_PROMPT,
    CvDraft,
    build_cv_draft_prompt,
    generate_draft,
    parse_cv_draft_response,
    render_draft_markdown,
)
from core.models import JobRecord


class SystemPromptGuardrailTests(unittest.TestCase):
    def test_prompt_forbids_claiming_physical_presence_not_in_the_cv(self) -> None:
        # Regression: live drafts for Ukraine-based Djinni postings invented
        # residency claims not in the CV ("I'm based in Ukraine originally")
        # even though the CV header states the candidate's actual current city -
        # the model was blurring past-employer location with current residence.
        # Guard the explicit rule that fixed it so a future prompt edit
        # can't silently drop it.
        self.assertIn("LOCATION", SYSTEM_PROMPT)
        self.assertIn("current location", SYSTEM_PROMPT)

    def test_prompt_still_allows_genuine_timezone_overlap_claims(self) -> None:
        # The user's own correction: he's fine with "Ukraine time zone"-style
        # phrasing (his real city is genuinely only ~1h from Kyiv) - only a claim of
        # physically living/being based in Ukraine is the actual problem.
        self.assertIn("time-zone-overlap", SYSTEM_PROMPT)
        self.assertIn("fine", SYSTEM_PROMPT)


class CvDraftPromptTests(unittest.TestCase):
    def test_build_cv_draft_prompt_includes_cv_and_posting_fields(self) -> None:
        record = JobRecord(
            title="Data Analyst",
            company="Acme",
            city_region="Remote",
            country="",
            work_format="Remote",
            full_text="Looking for a SQL-savvy analyst.",
        )
        prompt = build_cv_draft_prompt(record, "CV TEXT HERE")
        self.assertIn("CV TEXT HERE", prompt)
        self.assertIn("Data Analyst", prompt)
        self.assertIn("Acme", prompt)
        self.assertIn("SQL-savvy analyst", prompt)

    def test_build_cv_draft_prompt_truncates_long_descriptions(self) -> None:
        record = JobRecord(title="X", full_text="A" * 10000)
        prompt = build_cv_draft_prompt(record, "cv")
        self.assertLess(len(prompt), 7000)


class CvDraftParsingTests(unittest.TestCase):
    def test_parses_plain_json(self) -> None:
        draft = parse_cv_draft_response(
            '{"cv_bullets": ["Built SQL reports", "Wrote Python scripts"], "pitch": "I have SQL experience."}'
        )
        self.assertEqual(draft.cv_bullets, ["Built SQL reports", "Wrote Python scripts"])
        self.assertEqual(draft.pitch, "I have SQL experience.")

    def test_parses_json_wrapped_in_code_fences(self) -> None:
        draft = parse_cv_draft_response('```json\n{"cv_bullets": ["A"], "pitch": "B"}\n```')
        self.assertEqual(draft.cv_bullets, ["A"])
        self.assertEqual(draft.pitch, "B")

    def test_drops_blank_bullets(self) -> None:
        draft = parse_cv_draft_response('{"cv_bullets": ["Real bullet", "  ", ""], "pitch": "x"}')
        self.assertEqual(draft.cv_bullets, ["Real bullet"])


class GenerateDraftTests(unittest.TestCase):
    def test_generate_draft_calls_the_api_and_returns_parsed_draft(self) -> None:
        record = JobRecord(job_id="test_1", title="Data Analyst", company="Acme", full_text="SQL, Python")
        fake_block = MagicMock(type="text", text='{"cv_bullets": ["Built SQL reports"], "pitch": "I have SQL."}')
        fake_response = MagicMock(content=[fake_block])
        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_response

        draft = generate_draft(fake_client, record, "CV TEXT", model="claude-sonnet-5")

        self.assertEqual(draft.cv_bullets, ["Built SQL reports"])
        self.assertEqual(draft.pitch, "I have SQL.")
        fake_client.messages.create.assert_called_once()
        call_kwargs = fake_client.messages.create.call_args.kwargs
        self.assertEqual(call_kwargs["model"], "claude-sonnet-5")
        self.assertIn("CV TEXT", call_kwargs["messages"][0]["content"])

    def test_generate_draft_raises_a_diagnosable_error_on_empty_reply(self) -> None:
        # Regression: a bare JSONDecodeError ("Expecting value: line 1 column
        # 1") on an empty model reply gave no clue why - seen live 2026-09-14
        # on a real posting. generate_draft should surface stop_reason and
        # the raw content block types instead of a bare parse error.
        record = JobRecord(job_id="test_2", title="Product Analyst", company="Acme")
        fake_response = MagicMock(content=[], stop_reason="refusal")
        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_response

        with self.assertRaises(RuntimeError) as ctx:
            generate_draft(fake_client, record, "CV TEXT")
        self.assertIn("refusal", str(ctx.exception))


class RenderDraftMarkdownTests(unittest.TestCase):
    def test_render_includes_title_company_pitch_and_bullets(self) -> None:
        record = JobRecord(
            title="Data Analyst",
            company="Acme",
            url="https://example.com/job",
            city_region="Remote",
            country="",
            work_format="Remote",
        )
        draft = CvDraft(cv_bullets=["Built SQL reports", "Wrote Python scripts"], pitch="I have SQL experience.")
        markdown = render_draft_markdown(record, draft)
        self.assertIn("# Data Analyst", markdown)
        self.assertIn("**Acme**", markdown)
        self.assertIn("https://example.com/job", markdown)
        self.assertIn("I have SQL experience.", markdown)
        self.assertIn("- Built SQL reports", markdown)
        self.assertIn("- Wrote Python scripts", markdown)
        self.assertIn("Требует вычитки перед отправкой", markdown)

    def test_render_skips_blank_city_region_without_a_stray_separator(self) -> None:
        # Regression: Djinni postings often have no city_region, only a
        # country (e.g. "Ukraine") - the naive "{city_region} ({country})"
        # join left a stray leading space/parenthesis: " (Ukraine) · Remote".
        record = JobRecord(title="X", company="Y", city_region="", country="Ukraine", work_format="Remote")
        draft = CvDraft(cv_bullets=["A"], pitch="p")
        markdown = render_draft_markdown(record, draft)
        self.assertIn("Ukraine · Remote", markdown)
        self.assertNotIn("(Ukraine)", markdown)
        self.assertNotIn(" (Ukraine)", markdown)


if __name__ == "__main__":
    unittest.main()
