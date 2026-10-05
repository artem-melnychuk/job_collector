"""Lightweight metadata inference shared by source collectors."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any


def matches_context(text: str, terms: list[str]) -> bool:
    """Check whether text mentions at least one crypto/trading context term."""
    lowered = str(text or "").casefold()
    return any(str(term).strip().casefold() in lowered for term in terms if str(term).strip())


def query_matches(query: str, title: str) -> bool:
    """Match a query by phrase or by all meaningful words in the job title.

    Deliberately checked against the title only, not the full description:
    matching against the whole job text let common words ("research",
    "analyst") that show up anywhere in a long JD falsely match unrelated
    queries, e.g. a Coinbase "Credit Risk Analyst" (sales/underwriting role)
    matching the "Research Analyst" query and getting mis-tagged as category
    "data". The same gap let LinkedIn/WTTJ "similar jobs" results (Medical
    Writer, Solution Architect, Quantum ML Engineer) slip in under whatever
    query happened to be searched, since nothing checked their title either.
    """
    normalized_query = re.sub(r"\s+", " ", str(query or "")).strip().casefold()
    normalized_title = re.sub(r"\s+", " ", str(title or "")).strip().casefold()
    if not normalized_query or not normalized_title:
        return False
    if normalized_query in normalized_title:
        return True
    words = re.findall(r"[a-z0-9]+", normalized_query)
    return bool(words) and all(re.search(rf"\b{re.escape(word)}\b", normalized_title) for word in words)


# More years than this is not an experience requirement: it is a calendar year
# ("founded in 2017"), a company's age or a combined headcount figure ("150+
# years of combined experience"). Shared with core/analyzer.py.
MAX_EXPERIENCE_YEARS = 30

# (?<!\d): with a bare \d{1,2} the regex also matched the tail of a longer
# number, so "150+ years of combined experience" read as 50 years.
_YEARS_PATTERN = re.compile(r"(?<!\d)(\d+)\s*(?:[-–—]|to)\s*(\d+)\s*years?|(?<!\d)(\d+)\+?\s*years?")
# A bare "N years" also matches unrelated mentions like "founded 50 years ago" —
# only count it as an experience requirement when a context word sits nearby.
_YEARS_CONTEXT_WORDS = ("experience", "expérience", "expertise", "background", "track record")


def _max_years_mentioned(text: str) -> int:
    """Highest experience *floor* mentioned, e.g. the 3 in "3-5 years".

    A "3-5 years" posting's real bar to clear is the lower bound (3); using
    the range's upper bound instead overstated the requirement and rejected
    postings whose actual minimum was within reach.
    """
    best = 0
    for match in _YEARS_PATTERN.finditer(text):
        window = text[max(0, match.start() - 40): match.end() + 40]
        if not any(word in window for word in _YEARS_CONTEXT_WORDS):
            continue
        range_low, _range_high, single = match.groups()
        floor = int(range_low) if range_low else (int(single) if single else None)
        if floor is not None and floor <= MAX_EXPERIENCE_YEARS:
            best = max(best, floor)
    return best


def is_overqualified(title: str, text: str, exclusion: dict[str, Any]) -> bool:
    """Flag postings whose seniority/requirements look out of reach for now."""
    title_lowered = str(title or "").casefold()
    title_markers = exclusion.get("title_markers") or []
    if any(
        re.search(rf"\b{re.escape(str(marker).strip().casefold())}\b", title_lowered)
        for marker in title_markers
        if str(marker).strip()
    ):
        return True

    text_lowered = str(text or "").casefold()
    text_markers = exclusion.get("text_markers") or []
    if any(str(marker).strip().casefold() in text_lowered for marker in text_markers if str(marker).strip()):
        return True

    min_years = exclusion.get("min_years_experience")
    if min_years is not None and _max_years_mentioned(text_lowered) >= int(min_years):
        return True
    return False


def requires_french(title: str, text: str, exclusion: dict[str, Any]) -> bool:
    """Flag postings that explicitly require French or read as French-language."""
    combined = f"{title or ''}\n{text or ''}".casefold()

    # Word-boundary matching, not a plain substring check: "h/f" as a bare
    # substring also matches inside "English/French", which is not the French
    # gender-neutral job-title marker it's meant to catch.
    phrases = exclusion.get("french_phrases") or []
    if any(
        re.search(rf"\b{re.escape(str(phrase).strip().casefold())}\b", combined)
        for phrase in phrases
        if str(phrase).strip()
    ):
        return True

    markers = exclusion.get("french_word_markers") or []
    threshold = int(exclusion.get("french_word_marker_threshold", 3))
    hits = sum(
        1 for marker in markers
        if str(marker).strip() and re.search(rf"\b{re.escape(str(marker).strip().casefold())}\b", combined)
    )
    return hits >= threshold


def requires_german(title: str, text: str, exclusion: dict[str, Any]) -> bool:
    """Flag postings that explicitly require German or read as German-language."""
    combined = f"{title or ''}\n{text or ''}".casefold()

    # Word-boundary matching, not a plain substring check: "m/w/d" (and its
    # variants) is the standard German gender-neutral job-title marker and
    # does not appear as a substring of anything else worth matching.
    phrases = exclusion.get("german_phrases") or []
    if any(
        re.search(rf"\b{re.escape(str(phrase).strip().casefold())}\b", combined)
        for phrase in phrases
        if str(phrase).strip()
    ):
        return True

    markers = exclusion.get("german_word_markers") or []
    threshold = int(exclusion.get("german_word_marker_threshold", 3))
    hits = sum(
        1 for marker in markers
        if str(marker).strip() and re.search(rf"\b{re.escape(str(marker).strip().casefold())}\b", combined)
    )
    return hits >= threshold


def has_disallowed_work_format(work_format: str, exclusion: dict[str, Any]) -> bool:
    """Flag postings whose parsed work format is explicitly excluded (e.g. Hybrid)."""
    disallowed = {str(value).strip().casefold() for value in (exclusion.get("disallowed_work_formats") or [])}
    return str(work_format or "").strip().casefold() in disallowed


def requires_onsite_in_disallowed_country(work_format: str, country: str, exclusion: dict[str, Any]) -> bool:
    """Flag Hybrid/On-site postings anchored to a country ruled out for relocation.

    Unlike ``disallowed_work_formats`` (which used to reject all Hybrid/On-site
    regardless of where), the candidate is now open to office/hybrid roles in
    general — the blocker is specific countries he won't physically relocate
    to (confirmed cases: Poland-only Novoplex posting, Coinbase Hyderabad/India,
    a Kyiv/Ukraine hybrid Djinni posting — relocating to Ukraine specifically
    is off the table). A Remote posting nominally based in one of these
    countries is unaffected; only Hybrid/On-site (physical presence required)
    postings are checked.
    """
    if str(work_format or "").strip().casefold() not in {"hybrid", "on-site"}:
        return False
    disallowed = {str(value).strip().casefold() for value in (exclusion.get("disallowed_onsite_countries") or [])}
    if not disallowed:
        return False
    country_lowered = str(country or "").casefold()
    return any(re.search(rf"\b{re.escape(value)}\b", country_lowered) for value in disallowed)


def is_low_quality_listing(title: str, text: str, exclusion: dict[str, Any]) -> bool:
    """Flag scam/lead-funnel postings dressed up as trading/analyst roles."""
    combined = f"{title or ''}\n{text or ''}".casefold()
    phrases = exclusion.get("low_quality_phrases") or []
    return any(str(phrase).strip().casefold() in combined for phrase in phrases if str(phrase).strip())


def infer_work_format(text: str) -> str:
    lowered = str(text or "").casefold()
    if re.search(
        r"\bhybrid\b|\bhybride\b|télétravail partiel|partially remote"
        r"|\d+\s*(?:to\s*\d+\s*)?days?\s*(?:a|per)?\s*(?:week|month)?\s*(?:in|at)\s*(?:the\s*)?office"
        r"|\d+\s*(?:to\s*\d+\s*)?jours?\s*(?:par\s*semaine\s*)?au\s*bureau"
        # Ukrainian/Russian: "гібридн"/"гибридн" only caught the adjective form
        # ("гібридний формат"), not the bare noun ("офіс / гібрид") seen in real
        # Djinni postings (e.g. VSK's "Київ, офіс / гібрид") — broadened to match
        # any word starting with the stem.
        r"|\bгібрид\w*|\bгибрид\w*",
        lowered,
    ):
        return "Hybrid"
    # "Remote-first" is not "remote-only" — companies using this phrasing (e.g.
    # Coinbase) still require periodic mandatory in-person attendance, which is
    # functionally a hybrid arrangement even though the text reads as remote.
    if re.search(r"remote[- ]first", lowered) and re.search(
        r"quarterly|in[- ]person|on[- ]?site|come together|offsite|surges?", lowered
    ):
        return "Hybrid"
    if re.search(
        r"\bremote\b|full[- ]remote|work from home|à distance|télétravail|distributed"
        # Ukrainian/Russian markers, added for the Djinni.co source (Ukraine's
        # largest IT job board) — its postings are written in Ukrainian/Russian,
        # not English/French, so the patterns above never match them.
        r"|віддалено|дистанційн|удал[её]нн",
        lowered,
    ):
        return "Remote"
    if re.search(
        r"on[- ]site|onsite|office[- ]based|sur site|présentiel|en présentiel"
        # Ukrainian/Russian: the phrase forms ("в офісі"/"офісний формат")
        # missed the bare noun ("Київ, офіс") common in Djinni postings that
        # just list a city and format with no surrounding phrase.
        r"|в офісі|в офисе|офісний формат|офисный формат|\bофіс\w*|\bофис\w*"
        # "at the Montreal office" / "at our London office" names a physical
        # team location with no "remote"/"hybrid" word anywhere in the text —
        # postings phrased this way (e.g. DRW's) used to fall through to
        # "Unknown" and slip past the disallowed-work-format filter entirely.
        # "home office" is excluded: that phrasing describes remote-work
        # equipment, not a company site.
        r"|\bat (?:the|our) (?!home\b)[a-z\s]{2,25}office\b",
        lowered,
    ):
        return "On-site"
    return "Unknown"


def _salary_value(value: Any) -> str:
    if not isinstance(value, dict):
        return str(value or "").strip()
    currency = str(value.get("currency") or value.get("currencyCode") or "").strip()
    nested = value.get("value")
    if isinstance(nested, dict):
        minimum = nested.get("minValue", nested.get("value"))
        maximum = nested.get("maxValue")
        unit = nested.get("unitText", "")
    else:
        minimum = value.get("minValue") or nested
        maximum = value.get("maxValue")
        unit = value.get("unitText", "")
    if minimum is None and maximum is None:
        return ""
    if minimum is not None and maximum is not None and str(minimum) != str(maximum):
        result = f"{minimum}-{maximum} {currency}".strip()
    else:
        result = f"{minimum if minimum is not None else maximum} {currency}".strip()
    return f"{result}/{str(unit).lower()}" if unit else result


def extract_salary(value: Any, text: str = "") -> str:
    """Extract an explicit salary range/value from structured data or text."""
    structured = _salary_value(value)
    if structured:
        return structured

    # Thousands separator accepts space/dot/comma ("45 000", "45.000", "45,000"
    # — comma added 2026-09-08, US format like "$80,000" used to be cut short
    # at "$80,00" because comma was only recognized as a *decimal* separator
    # below). The two groups don't collide: thousands requires exactly 3
    # digits after the separator, decimal requires 1-2, so "45,000" (thousands)
    # and "45,50" (decimal) both still resolve correctly.
    # Djinni (2026-09-30) writes "50 000−75 000 грн" with a thin space
    # (U+2009) or no-break space as the thousands separator, a Unicode minus
    # (U+2212) between the bounds, and also "від 40 000 до 45 000 грн".
    # None of these were recognized, so the regex latched onto the tail and
    # stored "000 грн" (≈0 USD). An unseparated "40000" had the same problem,
    # since the leading group only allowed 2-3 digits. The lookbehind keeps a
    # match from starting in the middle of a longer number.
    thousands_sep = r"[    .,]"
    amount = rf"(?<!\d)(?:\d{{1,3}}(?:{thousands_sep}\d{{3}})+|\d{{2,}})(?:[,.]\d{{1,2}})?"
    range_sep = r"(?:\s*[-–—−]\s*|\s+(?:до|to)\s+)"
    # грн/₴ added for Ukrainian job boards (work.ua, robota.ua) — postings there
    # quote salary as e.g. "30 000 – 40 000 грн" with no per-month/year suffix
    # (monthly pay is the unstated default convention on those sites).
    currency = r"(?:€|EUR|USD|\$|GBP|£|грн\.?|₴|UAH)"
    unit = r"(?:/\s*|\s+per\s+|\s+par\s+)(?:hour|heure|year|an|month|mois|day|jour|week|semaine)s?"
    patterns = (
        rf"{amount}(?:{range_sep}{amount})?\s*{currency}(?:\s*{unit})?",
        rf"{currency}\s*{amount}(?:\s*[-–—−]\s*{currency}?\s*{amount})?(?:\s*{unit})?",
    )
    for pattern in patterns:
        match = re.search(pattern, str(text or ""), re.IGNORECASE)
        if match:
            return re.sub(r"\s+", " ", match.group(0)).strip()
    return ""


# Approximate FX rates to USD, fixed at the time this was written (2026-09).
# For rough cross-posting comparison only ("is this range roughly
# competitive"), not for anything a financial decision should rest on.
# No live rate lookup: keeping the collector pipeline free of an extra
# network dependency (and its own failure/rate-limit modes, see the Notion
# script's 429 issues) mattered more here than staying exactly current -
# update the numbers by hand if they drift enough to matter.
_USD_FX_RATES: dict[str, float] = {
    "USD": 1.0, "$": 1.0,
    "EUR": 1.08, "€": 1.08,
    "GBP": 1.27, "£": 1.27,
    "UAH": 0.024, "ГРН": 0.024, "₴": 0.024,
}

_SALARY_CURRENCY_PATTERN = re.compile(r"€|£|₴|\$|USD|EUR|GBP|UAH|ГРН\.?", re.IGNORECASE)
_SALARY_AMOUNT_PATTERN = re.compile(r"\d[\d.,\s]*\d|\d")


def _parse_salary_amount(token: str) -> float | None:
    # Space, dot and comma can all be thousands separators, and dot or comma
    # the decimal one: European "30.000,00" and US "30,000.00" both occur.
    # Same rule as extract_salary: a last separator followed by 1-2 digits is
    # decimal, one followed by exactly 3 is thousands. Stripping every
    # separator (until 2026-10-05) read "30.000,00 €" as 3,000,000 EUR.
    compact = re.sub(r"\s", "", token)
    decimals = ""
    decimal_match = re.search(r"[.,](\d{1,2})$", compact)
    if decimal_match:
        compact, decimals = compact[: decimal_match.start()], decimal_match.group(1)
    digits = re.sub(r"[.,]", "", compact)
    try:
        return float(f"{digits}.{decimals}" if decimals else digits)
    except ValueError:
        return None


def normalize_salary_usd(salary_text: str) -> str:
    """Best-effort USD-equivalent for an already-extracted `salary` string.

    Returns "" whenever the currency isn't recognized, already USD, or no
    amount can be parsed - this is a rough approximation for comparing
    postings across currencies, not a guarantee of the real figure.
    """
    text = str(salary_text or "").strip()
    if not text:
        return ""
    currency_match = _SALARY_CURRENCY_PATTERN.search(text)
    if not currency_match:
        return ""
    currency_key = currency_match.group(0).upper().rstrip(".")
    rate = _USD_FX_RATES.get(currency_key)
    if not rate or rate == 1.0:
        return ""
    amounts = [_parse_salary_amount(m) for m in _SALARY_AMOUNT_PATTERN.findall(text)]
    amounts = [a for a in amounts if a is not None]
    if not amounts:
        return ""
    # At least two significant figures: hundreds from 1,000 up, tens from
    # 100, whole units below. Rounding everything to hundreds turned
    # "30 EUR/month" into "≈0 USD". A range uses its smallest bound's step
    # for both ends ("960-1,080", not "960-1,100").
    usd = [a * rate for a in amounts]
    smallest = min(usd)
    step = 100 if smallest >= 1000 else 10 if smallest >= 100 else 1
    converted = [round(a / step) * step for a in usd]
    if len(converted) >= 2:
        low, high = min(converted), max(converted)
        value = f"{low:,.0f}-{high:,.0f}" if low != high else f"{low:,.0f}"
    else:
        value = f"{converted[0]:,.0f}"
    return f"≈{value} USD"


def split_city_region(city_region: str, country: str = "") -> tuple[str, str]:
    """Best-effort split of a combined `city_region` string into (city, region).

    `city_region` is a free-text join with no fixed shape across sources
    (WTTJ: "Paris, Ile-de-France, FR"; RemoteOK: "Kuala Lumpur, Kuala Lumpur,
    Wilayah Persekutuan Kuala Lumpur, Malaysia"; Djinni: often just a bare
    country name). This is a heuristic comma-split, not a real geocoder:
    the trailing segment is dropped when it duplicates `country` (already a
    separate field), the first remaining segment becomes `city`, and
    everything after it becomes `region`. A single-segment (or now-empty)
    input yields a bare city with no region, never a guessed one.
    """
    parts = [part.strip() for part in str(city_region or "").split(",") if part.strip()]
    country_value = str(country or "").strip()
    if parts and country_value and parts[-1].casefold() == country_value.casefold():
        parts = parts[:-1]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], ", ".join(parts[1:])


def infer_country(location: str) -> str:
    """Extract a country or broad geography from ATS location labels."""
    value = str(location or "").strip()
    if not value:
        return ""
    known = (
        "USA", "UK", "Canada", "India", "Singapore", "Luxembourg", "Cyprus",
        "Bhutan", "Hong Kong", "Taiwan", "Poland", "France", "Germany",
        "United States", "United Kingdom", "Europe", "Asia", "EMEA",
        "European Union",
        # Added 2026-09-07: countries seen in real LinkedIn "country only, no
        # city" location labels (common for remote postings) that this list
        # didn't cover, leaving `country` empty even though the location was
        # perfectly readable.
        "Colombia", "Spain", "United Arab Emirates", "UAE", "Netherlands",
        "Italy", "Portugal", "Ireland", "Mexico", "Ukraine", "Czech Republic",
        "Romania", "Sweden", "Switzerland", "Belgium", "Austria", "Australia",
        "Japan", "Brazil", "Argentina", "Israel", "Turkey", "Greece",
        "Norway", "Denmark", "Finland", "Philippines", "Indonesia",
        "Pakistan", "South Africa", "Nigeria", "Chile", "Peru", "Egypt",
        "Saudi Arabia", "New Zealand", "Malaysia", "Thailand", "South Korea",
        "China", "Kenya", "Vietnam",
    )
    matches = [item for item in known if re.search(rf"\b{re.escape(item)}\b", value, re.IGNORECASE)]
    if matches:
        return " / ".join(dict.fromkeys(matches))
    city_countries = {
        "London": "United Kingdom",
        "Paris": "France",
        "New York": "United States",
        "Nanterre": "France",
        "Malakoff": "France",
    }
    city_matches = [
        country for city, country in city_countries.items()
        if re.search(rf"\b{re.escape(city)}\b", value, re.IGNORECASE)
    ]
    if city_matches:
        return " / ".join(dict.fromkeys(city_matches))
    return ""


_LOCATION_LINE_PATTERN = re.compile(r"(?im)^.{0,5}location\s*:\s*(.+)$")


def extract_location_from_text(text: str) -> str:
    """Recover a location when the page's own location field failed to scrape.

    Some LinkedIn postings (aggregator reposts especially) render location
    only inside the free-text description, not in the DOM element the
    collector's location selector targets — the page-level scrape returns
    nothing even though a line like "🌐 Location: Remote" is right there in
    the text. Matches at most 5 leading characters before "location" so an
    emoji/bullet prefix doesn't block the match, but body text incidentally
    mentioning "location" elsewhere is not captured.
    """
    match = _LOCATION_LINE_PATTERN.search(str(text or ""))
    return match.group(1).strip() if match else ""


_RELATIVE_AGE_PATTERN = re.compile(r"(\d+)\s+(second|minute|hour|day|week|month)s?\s+ago", re.IGNORECASE)
_AGE_UNITS = {
    "second": timedelta(seconds=1),
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
}


def resolve_relative_date(text: str, reference: datetime) -> str:
    """Turn LinkedIn's "3 days ago" into an ISO date counted back from `reference`.

    The relative text only means something next to the moment it was read,
    so it is resolved at collect time. A prefix such as "Reposted" is kept
    ("Reposted 2026-10-03"); text without an age is returned unchanged.
    """
    text = str(text or "")
    match = _RELATIVE_AGE_PATTERN.search(text)
    if not match:
        return text
    resolved = reference - int(match.group(1)) * _AGE_UNITS[match.group(2).lower()]
    return f"{text[:match.start()]}{resolved.date().isoformat()}{text[match.end():]}".strip()


def parse_date(value: object) -> date | None:
    """Calendar date out of the date strings sources give: ISO 8601
    ("2026-09-19T04:02:55+00:00", also inside "Reposted 2026-10-03") or an
    RSS pubDate ("Fri, 02 Oct 2026 15:31:05"). Anything else is None."""
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        try:
            return date.fromisoformat(match.group(0))
        except ValueError:
            return None
    try:
        return parsedate_to_datetime(text).date()
    except (TypeError, ValueError, IndexError):
        return None
