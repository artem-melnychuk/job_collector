"""First-pass rule-based classification and skill extraction."""

from __future__ import annotations

import re
from dataclasses import replace

from core.metadata import MAX_EXPERIENCE_YEARS, normalize_salary_usd, split_city_region
from core.models import JobRecord


ROLE_RULES = (
    ("Fraud Analytics", ("fraud", "fraude", "financial crime", "aml", "anti-money")),
    (
        "Risk Analytics",
        (
            "risk analyst",
            "risk analytics",
            "risk control",
            "risk manager",
            "operational risk",
            "market risk",
            "credit risk",
            "derivatives compliance",
            "ризик-аналітик",
            "риск-аналитик",
        ),
    ),
    ("Quant / Research", ("quant", "quantitative", "research analyst", "researcher")),
    ("Trading Analytics", ("trading analyst", "trading operations", "execution analyst")),
    ("Market Data", ("market data", "pricing data", "reference data")),
    (
        "Product Analytics",
        ("product analyst", "product analytics", "growth analyst", "продуктовий аналітик", "продуктова аналітика", "продуктовая аналитика"),
    ),
    (
        "Business Analytics",
        ("business analyst", "business analytics", "strategy analyst", "бізнес-аналітик", "бизнес-аналитик"),
    ),
    ("BI / Reporting", ("bi analyst", "business intelligence", "power bi", "tableau")),
    ("Analytics Engineering", ("analytics engineer", "analytics engineering", "dbt")),
    (
        "Data Analytics",
        (
            "data analyst", "data analytics", "data analysis", "analyste de données", "analyste de donnees",
            # Ukrainian/Russian variants — Djinni.co postings are written in these
            # languages, and this bare "аналітик даних" pattern still needs to be
            # checked after the more specific Business/Product/Risk variants
            # above, same as its English/French counterparts.
            "аналітик даних", "аналитик данных", "дата-аналітик", "дата-аналитик",
        ),
    ),
    ("Software / Engineering", ("software engineer", "developer", "c++", "backend engineer")),
)


SKILL_RULES = (
    ("SQL", r"\bsql\b"),
    ("Python", r"\bpython\b"),
    ("Excel", r"\bexcel\b|microsoft excel"),
    ("Power BI", r"power\s*bi"),
    ("Tableau", r"\btableau\b"),
    ("Looker", r"\blooker\b"),
    ("dbt", r"\bdbt\b"),
    ("Snowflake", r"\bsnowflake\b"),
    ("BigQuery", r"\bbigquery\b"),
    ("Airflow", r"\bairflow\b"),
    ("VBA", r"\bvba\b"),
    ("DAX", r"\bdax\b"),
    ("R", r"(?<![a-z])r(?![a-z])|\br programming\b"),
    ("C++", r"c\+\+"),
    ("Spark", r"\bspark\b|pyspark"),
    ("Statistics", r"statistics|statistical|statistiques"),
    ("A/B testing", r"a/b testing|ab testing|experimentation"),
    # Split from a single "Machine Learning" bucket (2026-09-07): that bare
    # keyword conflated two very different asks — "familiarity with AI/LLM
    # tools for workflow automation" (learnable, low-to-medium effort) vs
    # "develop predictive models, optimize ML algorithms" (deep ML-engineering
    # background, a different job entirely). Reporting them separately keeps
    # a skills_gap count from hiding which one a posting actually wants.
    # Deliberately excludes the generic "ai-powered"/"ai-enabled" — those
    # matched marketing copy ("AI-powered job-search products") and even
    # LinkedIn's own Premium-upsell UI chrome (see
    # collectors/linkedin.py:strip_boilerplate) rather than an actual skill
    # ask. The remaining tokens are specific enough to only fire on a real
    # requirement to use LLM/generative-AI tooling.
    ("AI/LLM Tools", r"\bllm\b|large language models?|generative ai|\bgenai\b|chatgpt|claude\b|\bai/ml models\b|prompt engineering"),
    ("ML Engineering", r"predictive models?|optimi[sz]e?\s+machine learning algorithms?|train(?:ing)?\s+(?:an?\s+)?(?:ml|machine learning)\s+model|scikit-learn|tensorflow|pytorch"),
    ("Machine Learning", r"machine learning|\bml\b"),
    ("Blockchain", r"blockchain|on-chain|onchain|web3"),
    ("Crypto", r"crypto|cryptocurrency|digital assets|defi"),
    ("Market Data", r"market data|financial data|pricing data"),
    ("Risk Management", r"risk management|risk analytics|market risk|credit risk"),
    ("Fraud Detection", r"fraud detection|fraud analytics|financial crime|aml"),
    ("Financial Modeling", r"financial modeling|financial modelling|financial analysis"),
)


def _text(record: JobRecord) -> str:
    return f"{record.title}\n{record.full_text}".casefold()


def classify_role(record: JobRecord) -> tuple[str, str]:
    text = _text(record)
    for family, patterns in ROLE_RULES:
        if any(pattern in text for pattern in patterns):
            return family, family
    return "Other", "Other"


# French "an"/"ans" needs the closing \b: without it "ans?" matched the start
# of "and"/"analytics", so "founded in 2017 and" read as 2017 years. Italian
# "anni"/"anno" used to get in through that same prefix match, so it is
# listed explicitly. (?<!\d) keeps a match from starting mid-number, and
# (?!\d+-year\b) skips the adjective in "a 4-year university/degree".
_EXPERIENCE_YEARS_PATTERN = re.compile(
    r"(?<!\d)(?!\d+-year\b)(\d+)\s*(?:\+|to|-)?\s*(\d*)\s*(?:years?|ans?|ann[io])\b"
)
# Ukrainian/Russian phrasing (Djinni.co postings): "3х років", "від 3
# років", "1+ рік", "5 лет" — a separate pattern since the word for
# "years" isn't a simple suffix variant of the English/French one.
_EXPERIENCE_YEARS_PATTERN_UK_RU = re.compile(r"(?<!\d)(\d+)\s*х?\+?\s*(?:рок(?:и|ів|у)?|рік|лет|года?)")


def _plausible_years(minimum: str, maximum: str) -> bool:
    # Calendar years ("у 2015 році", "Global 2000 and") are not experience.
    return all(int(value) <= MAX_EXPERIENCE_YEARS for value in (minimum, maximum) if value)


def infer_experience_years(text: str) -> tuple[str, str]:
    lowered = text.casefold()
    matches = [match for match in _EXPERIENCE_YEARS_PATTERN.findall(lowered) if _plausible_years(*match)]
    if not matches:
        matches = [
            (value, "") for value in _EXPERIENCE_YEARS_PATTERN_UK_RU.findall(lowered) if _plausible_years(value, "")
        ]
    if not matches:
        return "", ""
    minimum, maximum = matches[0]
    return minimum, maximum or minimum


def classify_seniority(record: JobRecord) -> tuple[str, str, str]:
    text = _text(record)
    minimum, maximum = infer_experience_years(text)
    if re.search(
        r"intern|internship|stage|alternance|apprentice|graduate|junior|entry[- ]level"
        r"|джун(?:іор|иор)?|молодш(?:ий|а)\s|стажер|стажист|практикант",
        text,
    ):
        return "Junior", minimum, maximum
    if re.search(r"manager|director|head of|керівник", text):
        return "Manager", minimum, maximum
    if re.search(r"lead|principal|staff|тімлід|team\s*lead|провідний", text):
        return "Lead", minimum, maximum
    if re.search(r"senior|expert haut niveau|сеньйор|синьйор|старш(?:ий|а)", text) or (minimum and int(minimum) >= 5):
        return "Senior", minimum, maximum
    if re.search(r"confirm[ée]|мідл|миддл|\bmiddle\b", text):
        return "Mid-level", minimum, maximum
    if minimum and int(minimum) >= 3:
        return "Mid-level", minimum, maximum
    return "Unknown", minimum, maximum


def extract_skills(record: JobRecord) -> list[str]:
    text = _text(record)
    return [name for name, pattern in SKILL_RULES if re.search(pattern, text, re.IGNORECASE)]


def split_required_preferred(skills: list[str], record: JobRecord) -> tuple[list[str], list[str]]:
    """Split by where a skill FIRST appears, not just whether it appears late.

    A skill mentioned once in the requirements and again in a closing "nice
    to have: more X is a bonus" summary is still required — checking only the
    text after the preferred-marker (and bucketing anything found there as
    preferred, full stop) used to misclassify it as preferred-only.
    """
    text = _text(record)
    preferred_match = re.search(
        r"preferred|nice to have|nice-to-have|bonus|plus|желательно|souhaité"
        # Ukrainian markers — added 2026-09-08 after the Djinni category
        # expansion showed these are the common "nice to have" phrasing
        # there ("перевагою"/"буде перевагою" = "will be an advantage",
        # "плюсом"/"буде плюсом" = "will be a plus", "бажано" = "desirable"),
        # none of which the English/French/Russian markers above catch.
        r"|перевагою|плюсом|бажано",
        text,
    )
    split_at = preferred_match.start() if preferred_match else len(text)
    required_text, preferred_text = text[:split_at], text[split_at:]
    required = [skill for skill in skills if re.search(SKILL_PATTERN_BY_NAME[skill], required_text, re.IGNORECASE)]
    preferred = [
        skill for skill in skills
        if skill not in required and re.search(SKILL_PATTERN_BY_NAME[skill], preferred_text, re.IGNORECASE)
    ]
    return required, preferred


SKILL_PATTERN_BY_NAME = dict(SKILL_RULES)


def analyze_record(record: JobRecord) -> JobRecord:
    family, subcategory = classify_role(record)
    seniority, years_min, years_max = classify_seniority(record)
    skills = extract_skills(record)
    required, preferred = split_required_preferred(skills, record)
    city, region = split_city_region(record.city_region, record.country)
    return replace(
        record,
        role_family=family,
        role_subcategory=subcategory,
        seniority=seniority,
        years_experience_min=years_min,
        years_experience_max=years_max,
        skills_all="; ".join(skills),
        required_skills="; ".join(required),
        preferred_skills="; ".join(preferred),
        city=city,
        region=region,
        salary_usd_equivalent=normalize_salary_usd(record.salary),
        analysis_note="Rule-based first pass; review before using for decisions.",
    )
