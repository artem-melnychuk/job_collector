"""Skill-gap and rejection-reason analysis over the reviewed vacancy dataset.

Answers a different question than core/analyzer.py: not "what does this one
posting need" but "across everything I've already judged, what keeps costing
me otherwise-plausible roles, and which of the missing skills shows up often
enough to be worth learning". Reads jobs_master (required_skills/
preferred_skills, from analyzer.py) joined with manual_review (decision,
should_be_filtered, main_reason, review_notes, from human/best-guess review).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from core.analyzer import SKILL_RULES


# From CV/resume_improved.html's own "Tools" section — the candidate's
# actual current toolset, kept in sync manually since it's sourced from a
# human-maintained document, not derived from collected postings. Updated
# 2026-09-28 for the rewritten resume: Python moved out of PARTIAL_SKILLS
# now that the CV shows two real projects (Job Collector, Nice Events
# Tracker) with automated tests, not just scripting.
KNOWN_SKILLS = {
    "SQL",
    "Excel",
    "Python",
}

# Skills the candidate has some exposure to but not at the depth postings
# usually expect. Reported separately from a flat gap so "I have some X"
# isn't conflated with "I have zero X". Empty for now — update by hand if
# the resume's Tools section gains a skill at a basic level.
PARTIAL_SKILLS: set[str] = set()

# SKILL_RULES matches these on bare keyword presence, which mostly fires on
# company-description boilerplate ("Binance, a leading blockchain ecosystem
# behind the world's largest cryptocurrency exchange...", "expand into real
# estate, venture capital and cryptoassets") rather than an actual skill ask.
# Spot-checked 2026-09-07 across every near-fit posting: 0 of 9 "Crypto" hits
# were a real requirement. Excluded from the gap/demand count so the report
# doesn't manufacture a fake "you're missing crypto" signal — job_category
# already captures the crypto/trading domain elsewhere.
NOT_A_LEARNABLE_SKILL = {"Crypto", "Blockchain"}

SKILL_NAMES = tuple(name for name, _ in SKILL_RULES)

# Rejection-reason buckets: keyword groups matched against main_reason +
# review_notes (Russian, first-person, per the manual_review convention).
# A row can land in multiple buckets — e.g. "senior + requires Mandarin" hits
# both. Buckets marked structural cannot be fixed by studying; the rest name
# a concrete, learnable gap.
REASON_BUCKETS: dict[str, tuple[str, ...]] = {
    "language_other_than_english": (
        "мандарин", "китайск", "французск", "немецк", "испанск", "japanese",
        "японск", "mandarin", "german", "french", "spanish", "арабск",
    ),
    "coding_too_deep": (
        "python", "кодинг", "coding", "programming", "software engineer",
        "алгоритм", "machine learning", " ml ", "data engineer", "engineering",
        "c++", "java", "scala",
    ),
    "domain_expertise_missing": (
        "compliance", "edd", "aml", "kyc", "quant", "трейдинг", "trading",
        "audit", "бухгалт", "sales", "underwriting", "brokerage", "cfa",
        "actuar",
    ),
    "seniority_too_high": (
        "senior", "manager", "lead", "director", "principal", "менеджер",
        "директор", "management", "managerial",
    ),
    "experience_years_short": (
        "лет опыта", "years of experience", "years in", "стаж", "не хватает опыта",
        "нет такого опыта", "3+ years", "5+ years",
    ),
    "location_or_onsite": (
        "офис", "релокейт", "переезд", "on-site", "onsite", "relocation",
        "office", "hybrid", "гибрид",
    ),
    "low_quality_listing": (
        "воронк", "скам", "фейков", "boilerplate", "шаблон", "не содержит",
        "не соответствует", "текст вакансии",
    ),
}

STRUCTURAL_BUCKETS = {"language_other_than_english", "location_or_onsite", "low_quality_listing"}


EXAMPLES_PER_SKILL = 3


@dataclass(slots=True)
class SkillGapReport:
    """Skill demand among near-fit postings, split by whether it's already known."""

    demand: Counter = field(default_factory=Counter)
    gap: Counter = field(default_factory=Counter)
    partial: Counter = field(default_factory=Counter)
    # A few example job titles per skill, so a count like "Statistics: 7" comes
    # with enough context to tell a real, specific ask apart from a generic
    # keyword match — read the titles (or dig into full_text) instead of
    # trusting the bucket label at face value.
    examples: dict[str, list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class RejectionReport:
    """How often each rejection-reason bucket appears among rejected postings."""

    counts: Counter = field(default_factory=Counter)
    total_rejected: int = 0
    # A few verbatim excerpts of main_reason/review_notes per bucket — a label
    # like "coding_too_deep: 14" is just a keyword match on the manual review
    # text; the underlying main_reason is usually already a specific, granular
    # quote from the posting (e.g. "Strong Python Skills... we will test!").
    # Surfacing it here avoids having to re-open every reviewed row by hand.
    examples: dict[str, list[str]] = field(default_factory=dict)

    def learnable_counts(self) -> Counter:
        """Buckets addressable by studying/practicing, not by nationality or geography."""
        return Counter({bucket: count for bucket, count in self.counts.items() if bucket not in STRUCTURAL_BUCKETS})


def _is_near_fit(decision: str, should_be_filtered: str) -> bool:
    return str(decision or "").strip() in {"Подходит", "Возможно"} and str(should_be_filtered or "").strip() != "Да"


def _is_rejected(decision: str, should_be_filtered: str) -> bool:
    return str(decision or "").strip() == "Не подходит" or str(should_be_filtered or "").strip() == "Да"


def build_skill_gap_report(jobs: Iterable[Mapping[str, str]]) -> SkillGapReport:
    """Count required/preferred skills across near-fit postings, joined by job_id.

    ``jobs`` is an iterable of merged rows carrying at least ``decision``,
    ``should_be_filtered``, ``required_skills``, ``preferred_skills``.
    """
    report = SkillGapReport()
    for job in jobs:
        if not _is_near_fit(job.get("decision", ""), job.get("should_be_filtered", "")):
            continue
        skills = set()
        for field_name in ("required_skills", "preferred_skills"):
            raw = str(job.get(field_name, "") or "")
            skills.update(part.strip() for part in raw.split(";") if part.strip())
        skills -= NOT_A_LEARNABLE_SKILL
        title = str(job.get("title", "") or "").strip()
        for skill in skills:
            report.demand[skill] += 1
            if skill in PARTIAL_SKILLS:
                report.partial[skill] += 1
            elif skill not in KNOWN_SKILLS:
                report.gap[skill] += 1
            if title:
                examples = report.examples.setdefault(skill, [])
                if title not in examples and len(examples) < EXAMPLES_PER_SKILL:
                    examples.append(title)
    return report


def classify_reasons(text: str) -> list[str]:
    """Return every bucket whose keywords appear in the given (lowercased) text."""
    lowered = str(text or "").casefold()
    return [bucket for bucket, keywords in REASON_BUCKETS.items() if any(keyword in lowered for keyword in keywords)]


def build_rejection_report(jobs: Iterable[Mapping[str, str]]) -> RejectionReport:
    """Bucket rejection reasons across postings marked as not a fit / filtered.

    ``jobs`` rows need ``decision``, ``should_be_filtered``, ``main_reason``,
    ``review_notes``.
    """
    report = RejectionReport()
    for job in jobs:
        decision = job.get("decision", "")
        should_be_filtered = job.get("should_be_filtered", "")
        if not _is_rejected(decision, should_be_filtered):
            continue
        report.total_rejected += 1
        main_reason = str(job.get("main_reason", "") or "").strip()
        combined = f"{main_reason}\n{job.get('review_notes', '')}"
        excerpt = re.sub(r"\s+", " ", main_reason)[:160]
        for bucket in classify_reasons(combined):
            report.counts[bucket] += 1
            if excerpt:
                examples = report.examples.setdefault(bucket, [])
                if excerpt not in examples and len(examples) < EXAMPLES_PER_SKILL:
                    examples.append(excerpt)
    return report
