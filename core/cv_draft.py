"""LLM-drafted CV bullets + short application pitch (plan item 8.2).

Complements `core/fit_score.py` in the same "billed, explicitly-invoked
script" family - not part of the free collect/analyze pass. Deliberately
NOT a full formal cover letter: the candidate's own existing hand-written
drafts (`CV/archive/application_drafts/2026-09-05_manual_rounds/`) are short, dry, factual
first-person paragraphs naming concrete tools/employers and relocation
stance, not a generic "Dear Hiring Manager" letter - this generates in
that same established style, not a new one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

from core.fit_score import load_master_cv_text  # re-exported for callers
from core.models import JobRecord

# A stronger model than fit_score's Haiku tier is deliberate here: scoring is
# a classification task, this is generation that gets read by a real human
# recruiter - worth the extra cost per call, and the volume is much lower
# (only "Подходит" + still-active postings, not every undecided one).
DEFAULT_MODEL = "claude-sonnet-5"
# Confirmed live 2026-09-14: claude-sonnet-5 spends part of this same budget
# on an internal "thinking" content block before writing the actual JSON
# reply. At 700 this consistently starved the real output - one run got a
# truncated ("Unterminated string") JSON body, another got zero text at all
# (stop_reason "max_tokens" with only a "thinking" block). Sized generously
# so thinking always leaves enough room for a full reply.
MAX_OUTPUT_TOKENS = 4096
MAX_DESCRIPTION_CHARS = 6000

SYSTEM_PROMPT = (
    "You help a real candidate draft application material for a real job "
    "posting, from their real CV. You will be given the CV and one job "
    "posting.\n\n"
    "HONESTY - non-negotiable: only rephrase, reorder, or re-emphasize "
    "experience that is ALREADY in the CV. Never invent an employer, job "
    "title, tool, skill, degree, certification, date, or achievement that "
    "isn't there. If the posting wants something the CV doesn't show, leave "
    "it out rather than implying the candidate has it.\n\n"
    "LOCATION - never claim or imply the candidate physically lives or is "
    "'based' anywhere other than the CV header's stated current location. "
    "Never write 'based in [the posting's country]' or 'originally from "
    "[a past employer's country]' - a past employer's country is work "
    "history, not a claim about where the candidate lives now. A genuine, "
    "verifiable time-zone-overlap fact IS fine to state (e.g. the "
    "candidate's real country is only ~1 hour from Ukraine's time zone) "
    "since it's true and relevant to a remote posting - just don't dress it "
    "up as physical presence ('in Ukraine's time zone' is fine; 'based in "
    "Ukraine' or 'currently in Ukraine' is not).\n\n"
    "STYLE - dry and factual, not a sales pitch: plain concrete verbs "
    "(built, analyzed, collected, wrote) over qualifiers that imply mastery "
    "or growth (fluency, expertise, deep understanding, passionate). Fewer, "
    "tighter lines beat exhaustive detail - cut rather than pad. Match the "
    "tone of a candidate stating facts, not a marketer selling them.\n\n"
    "OUTPUT - respond with ONLY a JSON object, nothing else - no markdown, "
    "no code fences, no commentary before or after:\n"
    '{"cv_bullets": ["<3-5 short bullets, adapted from real CV lines to '
    'this posting\'s wording/priorities>"], "pitch": "<a short first-person '
    "paragraph, 2-4 sentences, English, naming concrete tools/employers "
    "from the CV and this candidate's relocation/remote stance where it's "
    'relevant to this posting - the same style as a brief application note, '
    'not a formal cover letter>"}'
)


@dataclass
class CvDraft:
    cv_bullets: list[str]
    pitch: str


def build_cv_draft_prompt(record: JobRecord, cv_text: str) -> str:
    posting = (
        f"Title: {record.title}\n"
        f"Company: {record.company}\n"
        f"Location: {record.city_region} ({record.country})\n"
        f"Work format: {record.work_format}\n"
        f"Description:\n{record.full_text[:MAX_DESCRIPTION_CHARS]}"
    )
    return f"CANDIDATE CV:\n{cv_text}\n\n---\n\nJOB POSTING:\n{posting}"


def parse_cv_draft_response(text: str) -> CvDraft:
    """Parse the model's JSON reply, tolerant of stray markdown code fences."""
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    data = json.loads(cleaned)
    bullets = [str(item).strip() for item in data.get("cv_bullets", []) if str(item).strip()]
    pitch = str(data.get("pitch", "")).strip()
    return CvDraft(cv_bullets=bullets, pitch=pitch)


def generate_draft(client, record: JobRecord, cv_text: str, model: str = DEFAULT_MODEL) -> CvDraft:
    """Call the Anthropic API once and return the parsed draft."""
    response = client.messages.create(
        model=model,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_cv_draft_prompt(record, cv_text)}],
    )
    text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
    try:
        return parse_cv_draft_response(text)
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        # A bare JSONDecodeError ("Expecting value: line 1 column 1") gives no
        # clue why - usually an empty `text` because the model stopped for a
        # reason other than finishing normally (e.g. stop_reason "refusal" on
        # newer Claude models, or hitting max_tokens before writing anything).
        # Surface that instead of letting the caller guess.
        stop_reason = getattr(response, "stop_reason", "unknown")
        block_types = [getattr(block, "type", "?") for block in response.content]
        raise RuntimeError(
            f"could not parse model reply (stop_reason={stop_reason!r}, "
            f"content_block_types={block_types!r}, raw_text={text[:200]!r}): {error}"
        ) from error


def render_draft_markdown(record: JobRecord, draft: CvDraft) -> str:
    bullets_block = "\n".join(f"- {bullet}" for bullet in draft.cv_bullets)
    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    # city_region is often blank on sources that only give a country (e.g.
    # Djinni) - joining unconditionally left a stray leading "(Country) ·"
    # with a leading space where the city would have gone.
    location = ", ".join(part for part in (record.city_region, record.country) if part)
    return (
        f"# {record.title}\n"
        f"**{record.company}** — {record.url}\n\n"
        f"{location} · {record.work_format}\n\n"
        f"> {draft.pitch}\n\n"
        f"## Адаптированные пункты для CV\n\n"
        f"{bullets_block}\n\n"
        f"---\n"
        f"Черновик сгенерирован LLM ({generated_at}, plan item 8.2). "
        f"Требует вычитки перед отправкой — не отправлять как есть "
        f"(проверка ревьюером/человеком — пункты 8.3/8.5 дорожной карты).\n"
    )
