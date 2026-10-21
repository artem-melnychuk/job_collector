"""Count how often analyst postings mention tools and certifications.

Read-only over the processed dataset (`jobs_master`); writes only
`reports/certifications.csv` and `reports/certifications.md`. One posting
counts once per term, and postings duplicated across sources/reruns
(same normalized company + title) are collapsed to a single posting first.

Certificate rows additionally split into required / preferred / merely
mentioned, decided by whichever requirement/preference marker sits closest
to the match within +-150 characters.
"""

from __future__ import annotations

import argparse
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.skill_recommendations import normalize_key  # noqa: E402
DATA_PATH = PROJECT_ROOT / "data" / "processed" / "jobs_clean_v1.xlsx"
REPORTS_DIR = PROJECT_ROOT / "reports"
MIN_POSTINGS = 50
CONTEXT_WINDOW = 150
QUOTE_WIDTH = 160
QUOTE_SAMPLES = 3

_TOOL_NAMES = (
    r"power\s?bi|tableau|looker|snowflake|bigquery|big\s+query|dbt|google\s+analytics|ga4|"
    r"sql|python|excel|metabase"
)
_CERT_WORD = r"(?:certif\w*|сертиф\w*)"


@dataclass(frozen=True)
class Term:
    name: str
    group: str
    pattern: str
    flags: int = re.IGNORECASE


CERTIFICATES = [
    Term("PL-300 / Power BI Data Analyst Associate / Microsoft Certified", "certificate",
         r"\bpl-?300\b|power\s?bi\s+data\s+analyst\s+associate|microsoft\s+certified"),
    Term("Tableau Desktop Specialist / Certified Data Analyst", "certificate",
         r"tableau\s+desktop\s+specialist|tableau\s+certified(?:\s+data\s+analyst)?|tableau\s+(?:desktop\s+)?certification"),
    Term("Google Data Analytics Certificate", "certificate",
         r"google\s+data\s+analytics\s+(?:professional\s+)?certificat\w*"),
    Term("Google Analytics / GA4 certification", "certificate",
         r"(?:google\s+analytics|ga4)\s+(?:certification|certificate|qualification|iq)|\bgaiq\b"),
    Term("IBM Data Analyst", "certificate", r"\bibm\s+data\s+analyst"),
    Term("CompTIA Data+", "certificate", r"\bcomptia\s+data\+?|data\+\s+certif\w*"),
    Term("GitHub Foundations", "certificate", r"github\s+foundations"),
    Term("dbt certification", "certificate", r"\bdbt\s+(?:analytics\s+engineering\s+)?certif\w*"),
    Term("certification near a tool name", "certificate",
         rf"{_CERT_WORD}[^\n]{{0,60}}?\b(?:{_TOOL_NAMES})\b|\b(?:{_TOOL_NAMES})\b[^\n]{{0,60}}?{_CERT_WORD}"),
]

TOOLS = [
    Term("SQL", "tool", r"\b(?:sql|mysql|postgresql|postgres)\b"),
    Term("Python", "tool", r"\bpython\b"),
    # "excel" is also an English verb: skip "to excel" and "excel in/at/as/with...".
    Term("Excel", "tool",
         r"(?<![Tt]o )\bexcel\b(?!\s+(?:in|at|as|with|when|under|on|by|while|through|and\s+(?:grow|thrive)))"),
    Term("Power BI", "tool", r"power\s?bi"),
    Term("Tableau", "tool", r"\btableau\b"),
    Term("Looker", "tool", r"\blooker\b|\blookml\b"),
    Term("GA4 / Google Analytics", "tool", r"\bga4\b|google\s+analytics|universal\s+analytics"),
    Term("dbt", "tool", r"\bdbt\b"),
    Term("BigQuery", "tool", r"\bbig\s?query\b"),
    Term("Snowflake", "tool", r"\bsnowflake\b"),
    # Bare "Dune" only counts capitalized: lowercase "dune" is a landform.
    Term("Dune", "tool", r"(?i:dune\s+analytics|dune\.com)|\bDune\b", flags=0),
    Term("Metabase", "tool", r"\bmetabase\b"),
]

ALL_TERMS = CERTIFICATES + TOOLS

_REQ_RE = re.compile(
    r"\b(?:required|requirements?|must|mandatory|essential|needed?)\b|обязательн|обов'язков|необходим",
    re.IGNORECASE,
)
_PREF_RE = re.compile(
    r"\b(?:preferred|prefer|nice\s+to\s+have|a\s+plus|bonus|desirable|desired|advantage|asset|ideally)\b|"
    r"плюсом|бажан|перевага|вітається|желательн",
    re.IGNORECASE,
)

EUROPE = {
    "ukraine", "poland", "germany", "france", "spain", "italy", "portugal", "netherlands", "belgium",
    "ireland", "united kingdom", "uk", "switzerland", "austria", "sweden", "norway", "denmark", "finland",
    "czechia", "czech republic", "romania", "bulgaria", "cyprus", "greece", "hungary", "estonia", "latvia",
    "lithuania", "serbia", "croatia", "slovakia", "slovenia", "malta", "luxembourg", "europe",
}


def level_bucket(seniority: object) -> str:
    return {
        "Junior": "junior", "Mid-level": "middle",
        "Senior": "senior", "Lead": "senior", "Manager": "senior",
    }.get(str(seniority), "unknown")


def location_bucket(work_format: object, country: object) -> str:
    if str(work_format) == "Remote":
        return "remote"
    if str(country).strip().lower() in EUROPE:
        return "europe"
    return "other"


def classify_context(text: str, start: int, end: int) -> str:
    lo, hi = max(0, start - CONTEXT_WINDOW), min(len(text), end + CONTEXT_WINDOW)
    centre = (start + end) / 2
    best: tuple[float, str] | None = None
    for label, regex in (("required", _REQ_RE), ("preferred", _PREF_RE)):
        for hit in regex.finditer(text, lo, hi):
            distance = abs((hit.start() + hit.end()) / 2 - centre)
            if best is None or distance < best[0]:
                best = (distance, label)
    return best[1] if best else "mentioned"


def quote_around(text: str, start: int, end: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end == -1 else line_end
    lo = max(line_start, start - QUOTE_WIDTH // 2)
    hi = min(line_end, end + QUOTE_WIDTH // 2)
    return " ".join(text[lo:hi].split())


def dedupe_postings(frame: pd.DataFrame) -> pd.DataFrame:
    keys = [normalize_key(c, t) for c, t in zip(frame["company"], frame["title"])]
    return frame.loc[~pd.Series(keys, index=frame.index).duplicated(keep="first")].copy()


def analyze(postings: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    postings = postings.reset_index(drop=True)
    levels = [level_bucket(v) for v in postings["seniority"]]
    places = [location_bucket(w, c) for w, c in zip(postings["work_format"], postings["country"])]
    texts = [f"{t}\n{b}" for t, b in zip(postings["title"].fillna(""), postings["full_text"].fillna(""))]
    total = len(postings)

    rows: list[dict] = []
    quotes: dict[str, list[str]] = {}
    for term in ALL_TERMS:
        regex = re.compile(term.pattern, term.flags)
        counters = {k: 0 for k in ("junior", "middle", "senior", "unknown", "europe", "remote", "other",
                                   "required", "preferred", "mentioned_only")}
        found, term_quotes = 0, []
        for index, text in enumerate(texts):
            match = regex.search(text)
            if not match:
                continue
            found += 1
            counters[levels[index]] += 1
            counters[places[index]] += 1
            term_quotes.append(quote_around(text, match.start(), match.end()))
            if term.group == "certificate":
                status = classify_context(text, match.start(), match.end())
                counters["mentioned_only" if status == "mentioned" else status] += 1
        rows.append({
            "term": term.name, "group": term.group, "postings": found,
            "pct_of_all": round(100 * found / total, 1) if total else 0.0,
            "junior": counters["junior"], "middle": counters["middle"],
            "senior_lead_manager": counters["senior"], "level_unknown": counters["unknown"],
            "europe": counters["europe"], "remote": counters["remote"], "other_location": counters["other"],
            "cert_required": counters["required"] if term.group == "certificate" else "",
            "cert_preferred": counters["preferred"] if term.group == "certificate" else "",
            "cert_mentioned_only": counters["mentioned_only"] if term.group == "certificate" else "",
        })
        quotes[term.name] = term_quotes
    return pd.DataFrame(rows), quotes


def render_report(result: pd.DataFrame, quotes: dict[str, list[str]], raw: int, total: int,
                  postings: pd.DataFrame, seed: int) -> str:
    rng = random.Random(seed)
    level_counts = pd.Series([level_bucket(v) for v in postings["seniority"]]).value_counts()
    place_counts = pd.Series(
        [location_bucket(w, c) for w, c in zip(postings["work_format"], postings["country"])]
    ).value_counts()
    top = result.sort_values(["postings", "term"], ascending=[False, True]).head(10)
    certs = result[(result.group == "certificate") & (result.postings > 0)]

    lines = [
        "# Сертификаты и инструменты в вакансиях аналитиков", "",
        f"Проанализировано вакансий: **{total}** (в данных {raw}, дублей по company+title убрано {raw - total}).",
        f"Проверка сумм: по уровням {int(level_counts.sum())}, по локациям {int(place_counts.sum())} — "
        f"{'совпадает' if int(level_counts.sum()) == total == int(place_counts.sum()) else 'НЕ СОВПАДАЕТ'} с {total}.", "",
        "Уровень: Junior / Mid-level→middle / Senior+Lead+Manager→senior / остальное unknown. "
        "Локация: work_format=Remote → удалёнка, иначе страна из Европы (включая Украину) → Европа, иначе прочее. "
        "Одна вакансия = одно упоминание термина.", "",
        f"Уровни: {level_counts.to_dict()}. Локации: {place_counts.to_dict()}.", "",
        "## Топ-10 терминов", "", "| Термин | Группа | Вакансий | % |", "|---|---|---|---|",
    ]
    lines += [f"| {r.term} | {r.group} | {r.postings} | {r.pct_of_all} |" for r in top.itertuples()]
    lines += ["", "## Сертификаты (всё, что нашлось)", ""]
    if certs.empty:
        lines.append("Ни один из искомых сертификатов не упомянут.")
    else:
        lines += ["| Сертификат | Вакансий | Требуется | Желателен | Просто упомянут |", "|---|---|---|---|---|"]
        lines += [f"| {r.term} | {r.postings} | {r.cert_required} | {r.cert_preferred} | {r.cert_mentioned_only} |"
                  for r in certs.itertuples()]
    lines += ["", "## Цитаты для проверки ложных срабатываний (3 случайные на термин)", ""]
    shown = list(top.term) + [t for t in certs.term if t not in set(top.term)]
    for term in shown:
        sample = rng.sample(quotes[term], min(QUOTE_SAMPLES, len(quotes[term])))
        lines.append(f"**{term}**")
        lines += [f"- {q}" for q in sample] or ["- (нет)"]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DATA_PATH)
    parser.add_argument("--out", type=Path, default=REPORTS_DIR)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)

    frame = pd.read_excel(args.data, sheet_name="jobs_master")
    needed = {"title", "company", "full_text", "seniority", "work_format", "country"}
    if missing := needed - set(frame.columns):
        print(f"Missing columns: {sorted(missing)} - stopping, not guessing.")
        return 1
    postings = dedupe_postings(frame)
    if len(postings) < MIN_POSTINGS:
        print(f"Only {len(postings)} postings after dedup (< {MIN_POSTINGS}) - stopping.")
        return 1

    result, quotes = analyze(postings)
    args.out.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.out / "certifications.csv", index=False, encoding="utf-8-sig")
    (args.out / "certifications.md").write_text(
        render_report(result, quotes, len(frame), len(postings), postings.reset_index(drop=True), args.seed),
        encoding="utf-8",
    )
    print(f"Analyzed {len(postings)} postings (raw {len(frame)}). Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
