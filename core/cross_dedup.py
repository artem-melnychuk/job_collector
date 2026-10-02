"""Cross-source duplicate detection: one posting listed under several URLs.

`core/deduplication.py` keys identity on source + normalized URL, so a re-run
never creates a second row for the same link. It cannot see the same job
under two different links: on two boards (Jobicy and Himalayas both carry
Canonical's posting), or reposted on one board under a new URL (LinkedIn
reposts of Jobright.ai roles). Measured 2026-10-02 on 199 rows: 1 group
across sources, 8 within one source.

Those rows are not deleted: each keeps its own job_id, manual_review row and
availability status. This module only labels them with a shared
`duplicate_group` (the smallest job_id in the group), which
scripts/active_near_fit_report.py uses to show one row per job.

Two postings are the same job when all three hold:
- company: equal after normalization (case, punctuation, CamelCase split,
  legal suffixes such as Inc/LLC/GmbH and parenthesized parents such as
  "(DRW)" dropped), or nearly equal (difflib ratio >= 0.92);
- title: equal after normalization, or nearly equal (ratio >= 0.93);
- location: compatible. A company + title key alone would wrongly merge the
  same role posted for different cities - Coinbase "Payments Risk Analyst
  II" exists for both "Remote - USA" and "Hyderabad, India". So when both
  postings name a specific place, they must share one. Region words
  (Remote, Worldwide, Europe, EMEA...) match anything. A posting with no
  location at all does not match one that names a place: blank means
  unknown, not "anywhere", and a wrong merge (a real job hidden from the
  to-do list) costs more than a missed one (a duplicate row stays visible).

Clustering is greedy and conservative: a posting joins a group only if it
matches every member, so a vague posting cannot chain two incompatible
ones together.
"""

from __future__ import annotations

import re
from dataclasses import replace
from difflib import SequenceMatcher

from core.models import JobRecord

COMPANY_RATIO = 0.92
TITLE_RATIO = 0.93
LEGAL_SUFFIXES = {
    "inc", "llc", "ltd", "limited", "gmbh", "sa", "sas", "sarl", "bv", "nv", "corp", "corporation",
    "co", "plc", "ag", "oy", "ab", "srl", "sro", "spa", "pte", "pty", "lp", "llp",
}
# Location words that name a region or "anywhere" rather than a place; they
# never make two postings incompatible.
REGION_WORDS = {
    "remote", "worldwide", "anywhere", "global", "international", "europe", "european", "emea",
    "apac", "latam", "americas", "union", "more", "and", "the", "hybrid", "onsite", "site",
}
TITLE_NOISE = {"remote", "hybrid"}


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def normalize_company(name: str) -> str:
    text = re.sub(r"\([^)]*\)", " ", str(name or ""))  # "Cumberland (DRW)" -> "Cumberland"
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)  # "CollectlyInc" -> "Collectly Inc"
    tokens = re.findall(r"[a-z0-9]+", text.casefold())
    kept = [token for token in tokens if token not in LEGAL_SUFFIXES]
    return " ".join(kept or tokens)


def normalize_title(title: str) -> str:
    text = str(title or "").casefold().replace("&", " and ")
    tokens = [token for token in re.findall(r"[\w+#]+", text) if token not in TITLE_NOISE]
    return " ".join(tokens)


def location_tokens(record: JobRecord) -> set[str]:
    text = f"{record.city_region} {record.country}".casefold()
    return {token for token in re.findall(r"[^\W\d_]{3,}", text) if token not in REGION_WORDS}


def locations_compatible(a: set[str], b: set[str], a_blank: bool = False, b_blank: bool = False) -> bool:
    """`a`/`b`: specific place tokens; `*_blank`: no location text at all."""
    if (a_blank and b) or (b_blank and a):
        return False
    return not a or not b or bool(a & b)


class _Prepared:
    __slots__ = ("record", "company", "title", "places", "blank_location")

    def __init__(self, record: JobRecord) -> None:
        self.record = record
        self.company = normalize_company(record.company)
        self.title = normalize_title(record.title)
        self.places = location_tokens(record)
        self.blank_location = not (str(record.city_region or "").strip() or str(record.country or "").strip())


def _same_posting(a: _Prepared, b: _Prepared) -> bool:
    if not a.company or not b.company or not a.title or not b.title:
        return False
    if a.company != b.company and _ratio(a.company, b.company) < COMPANY_RATIO:
        return False
    if a.title != b.title and _ratio(a.title, b.title) < TITLE_RATIO:
        return False
    return locations_compatible(a.places, b.places, a.blank_location, b.blank_location)


def assign_duplicate_groups(records: list[JobRecord]) -> list[JobRecord]:
    """Return the records with `duplicate_group` set (blank for unique ones)."""
    prepared = sorted((_Prepared(record) for record in records), key=lambda item: item.record.job_id)
    groups: list[list[_Prepared]] = []
    for item in prepared:
        for group in groups:
            if all(_same_posting(item, member) for member in group):
                group.append(item)
                break
        else:
            groups.append([item])

    group_of: dict[int, str] = {}
    for group in groups:
        label = group[0].record.job_id if len(group) > 1 else ""
        for member in group:
            group_of[id(member.record)] = label
    return [replace(record, duplicate_group=group_of.get(id(record), "")) for record in records]
