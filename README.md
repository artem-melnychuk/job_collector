# Job Collector

A Python pipeline that collects job postings from several sources, deduplicates and classifies them, shows which skills and certifications the market actually asks for, and supports the application process with a human approval gate. Built for data / business / product analyst roles, with a crypto and fintech lean.

Operational notes for contributors and AI assistants: [CLAUDE.md](CLAUDE.md).

> This is the public showcase copy. The real, accumulating dataset and personal review notes live in a private repository; here `data/processed/` holds an empty template with the same schema.

## What it does

```
SEARCH -> COLLECT -> DEDUPLICATE -> SAVE RAW -> MERGE INTO DATASET
                                                     |
        ANALYZE (rules) -> GROUP COPIES ACROSS SOURCES -> AVAILABILITY CHECK
                                                     |
        REVIEW (human) -> APPLY LIST -> APPLICATION LOG       REPORTS (skill gap, market demand)
                                                     |
        LLM STEPS (optional, paid): fit score -> draft -> separate review -> PDF + ATS check
```

1. **Collect** postings from eleven sources (three of them opt-in) into one `JobRecord` schema.
2. **Deduplicate.** Identity is `source + normalized URL`, so reruns are idempotent and hand-entered review columns survive every rerun.
3. **Group copies across sources.** The same job on two boards, or reposted under a new link, gets a shared `duplicate_group` label. Copies are labelled, not deleted.
4. **Filter** at collection time: language requirements (French, German), on-site roles in excluded countries, scam-like listings, minimum experience.
5. **Analyze** with transparent rules: role family, seniority, years of experience, skills split into required and preferred, salary converted to a rough USD figure.
6. **Check availability** of saved postings with a method per source (ATS APIs, a board's own search API, closed-page markers).
7. **Work the applications.** A person grades postings; an apply list shows open, graded postings not yet applied to, one row per job; marking a row as applied adds it to a dated application log.
8. **Report** which skills recur in near-fit postings but are missing from the candidate's toolset, joined with the share of all postings that mention them and whether any posting asks for a certificate. Actions (`learn`, `optional`, `low priority`, `verify first`) are computed from thresholds, never from hardcoded tool names.
9. **LLM steps (optional, uses an LLM API):** score fit, draft a short pitch and CV bullets, review the draft with a separate model call, assemble a PDF and check its text layer for the posting's keywords. Nothing is ever sent automatically.

## Sources

| Source | How | Notes |
|---|---|---|
| Company career boards | Greenhouse, Lever and BambooHR public JSON endpoints | Boards are listed in `config/companies.yaml` |
| Djinni | RSS | Ukrainian IT board; items outside the requested category are dropped, so a silently ignored filter shows up as an error instead of noise |
| DOU.ua | RSS | Ukrainian IT community board; company, salary and remote/office are parsed from the structured feed title |
| Jobicy | JSON API | Server-side eligibility filter (`geo`), titles matched locally |
| Himalayas | JSON API | Server-side country filter, titles matched locally; availability checked through the API because job pages sit behind a bot challenge |
| RemoteOK | JSON API | Remote-only; filtered locally by title |
| We Work Remotely | RSS | Remote-only; small rolling window |
| LinkedIn | Playwright, JSON-LD first, CSS fallback | Authwalls and markup drift are expected failure modes |
| Welcome to the Jungle | Playwright | Opt-in only (`--source wttj`) |
| Work.ua, Robota.ua | Playwright | Implemented, but blocked by anti-bot protection; opt-in only |
| Indeed | — | Checked, not addable: the RSS feed was removed (HTTP 404) and search sits behind a Cloudflare captcha (HTTP 403) |

## What the data shows

Snapshot of the private dataset on 2026-10-05: 237 postings collected between 2026-08-18 and 2026-10-05, 223 after collapsing postings with the same normalized company and title. Counts come from `scripts/certification_analysis.py`; a posting counts once per term. The sample follows one search profile (analyst roles, remote or in Europe, with a crypto and fintech lean), so the shares describe this sample, not the job market as a whole.

| Tool | Postings | Share of 223 |
|---|---:|---:|
| SQL | 129 | 57.8% |
| Python | 98 | 43.9% |
| Power BI | 61 | 27.4% |
| Tableau | 56 | 25.1% |
| Excel | 54 | 24.2% |
| Looker | 24 | 10.8% |
| BigQuery | 17 | 7.6% |
| dbt | 13 | 5.8% |
| Snowflake | 11 | 4.9% |
| GA4 / Google Analytics | 6 | 2.7% |

- **SQL is the most common requirement:** 129 of 223 postings (57.8%). Python is second with 98 (43.9%).
- **Power BI and Tableau appear at similar rates:** 61 postings (27.4%) and 56 (25.1%). Looker follows with 24 (10.8%).
- **Cloud warehouse and modeling tools are uncommon:** BigQuery 17 (7.6%), dbt 13 (5.8%), Snowflake 11 (4.9%).
- **Certificates are almost never asked for.** The script looks for nine certificate patterns (PL-300, Tableau, Google Data Analytics, Google Analytics / GA4, IBM, CompTIA Data+, GitHub Foundations, dbt, and the word "certification" within 60 characters of a tool name). One posting of 223 matched, and it lists certificates as preferred; none requires one.
- **Remote is the largest group:** 120 of 223 postings have work format Remote. The other 103 are hybrid, on-site or do not state a format: 51 of them name a European country (Ukraine included), 52 name a place outside Europe or no country.

Cross-check: `scripts/skills_gap_report.py` counts the same 223 postings with the analyzer's own skill patterns. It gives the same numbers for Power BI, Tableau, Looker, dbt and Snowflake, and 16 instead of 17 for BigQuery, because only the certification script's pattern accepts the spelling "Big Query".

## Quick start

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# validate configuration only
.venv\Scripts\python.exe -B scripts\collector.py --dry-run

# collect from all default sources
.venv\Scripts\python.exe -B scripts\collector.py --source all --limit 50

# classify, extract skills, group copies of the same job
.venv\Scripts\python.exe -B scripts\analyzer.py

# recheck whether saved postings are still open
.venv\Scripts\python.exe -B scripts\collector.py --check-availability --check-limit 500 --headless

# refresh the apply list and the application log
.venv\Scripts\python.exe -B scripts\applications_tracker.py

# what to study: skill gap + market demand + recommendations
.venv\Scripts\python.exe -B scripts\skills_gap_report.py

# how often tools and certifications appear in the collected postings
.venv\Scripts\python.exe -B scripts\certification_analysis.py

# tests
.venv\Scripts\python.exe -B -m unittest discover -s tests
```

The LLM steps read a master CV from `CV/` (not in the repository) and need `ANTHROPIC_API_KEY`. Every call is billed, so these are separate scripts with per-run limits:

```powershell
.venv\Scripts\python.exe -B scripts\fit_score.py --limit 20     # score ungraded postings
.venv\Scripts\python.exe -B scripts\cv_draft.py --limit 10      # draft pitch + CV bullets
.venv\Scripts\python.exe -B scripts\cv_review.py --limit 10     # separate reviewer call
.venv\Scripts\python.exe -B scripts\cv_pdf.py --limit 20        # PDF + ATS text-layer check (free)
```

## Working in the dataset

The processed workbook shows three tabs; pipeline data and reports are hidden.

| Tab | Purpose |
|---|---|
| `active_near_fit` | Apply list: open postings graded as a fit, not yet applied to, one row per job, with salary and link. Mark `applied` after sending. |
| `applications` | Application log with send dates, filled from the `applied` marks. |
| `manual_review` | Every collected posting with the person's grade. A repost inherits the grade of an already graded copy. |

## Design notes

- **One schema.** Every collector returns the same `JobRecord`; normalization happens after parsing, because sources give incomplete and inconsistent data.
- **Reruns are safe.** Identity is derived from the URL, merge preserves manual columns, and analysis results carry forward when a posting is re-collected.
- **Copies are grouped conservatively.** Same normalized company, nearly the same title and compatible locations. A plain company-and-title key would merge the same role posted for different cities, so named locations must overlap and a blank location never matches a specific one: a wrong merge hides a real job, while a missed one only leaves a duplicate row.
- **A closed posting stays closed.** An inconclusive recheck (authwall, rate limit) never overwrites a confirmed `closed` status.
- **Structured data over guesses.** Where an API gives a salary field, the salary is taken only from it; guessing from description text turned meal allowances into fake salaries.
- **Honesty gate in the LLM steps.** The drafting prompt may only rephrase what is in the CV, and a separate review call checks each draft for unsupported claims, wrong location claims, relevance and tone. Real failures found and fixed this way are documented in [CLAUDE.md](CLAUDE.md).
- **Human approval.** Nothing is sent by the pipeline; a person applies and marks the posting.
- **Fragile sources are labeled as such.** Browser-scraped sources can break at any time and some sites block automation outright; both facts are recorded rather than hidden.
- **Privacy by construction.** Personal contact details are read from the local CV at run time instead of living in the code, and the public copy is produced by a script that scans every file for personal identifiers and refuses to publish on a match.

## Layout

```text
config/       queries, companies and ATS boards, run settings
collectors/   one adapter per source
core/         models, storage, analyzer, cross-source grouping, skill gap and recommendations, LLM steps
scripts/      command-line entry points
data/raw/     one file per collection run (not tracked)
data/processed/  accumulating dataset (empty template in this copy)
tests/        unit and browser-integration tests (about 300)
```

## Limitations

- LinkedIn and Welcome to the Jungle have no stable public API; markup changes will break selectors.
- Skill extraction is rule-based. Short tokens (`R`) and generic words (`ML`) produce false positives; the recommendation report flags those skills instead of trusting them.
- Cross-source grouping relies on company and title text; a company renamed between boards or a heavily reworded title will not be matched.
- Market counts describe the collected sample (built around one search profile), not the whole job market.
- Portions of this project were developed with Claude Code assistance.
