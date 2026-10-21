"""Assemble a cover-note PDF for each reviewed draft and ATS-check its text
layer (plan item 8.4).

No LLM call, no API key needed - this is pure mechanical assembly (Playwright
HTML->PDF) plus a keyword check against the analyzer's already-extracted
`required_skills`/`preferred_skills`. Scans `CV/drafts/*.md`, skips files
that already have both a `.pdf` and an ATS-check section unless
`--regenerate`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.fit_score import load_master_cv_text
from core.pdf_assembly import (
    build_cover_note_html,
    check_ats_keywords,
    extract_contact_line,
    extract_draft_content,
    extract_pdf_text,
    posting_keywords,
    render_ats_section,
    render_pdf,
)
from core.storage import load_records

DATA_PATH = PROJECT_ROOT / "data" / "processed" / "jobs_clean_v1.xlsx"
DEFAULT_CV_PATH = PROJECT_ROOT / "CV" / "resume_improved.html"
DEFAULT_DRAFTS_DIR = PROJECT_ROOT / "CV" / "drafts"

ATS_SECTION_MARKER = "## ATS-проверка текстового слоя"


def has_ats_check(draft_markdown: str) -> bool:
    return ATS_SECTION_MARKER in draft_markdown


def strip_existing_ats_section(draft_markdown: str) -> str:
    return draft_markdown.split(f"\n---\n\n{ATS_SECTION_MARKER}")[0].rstrip() + "\n"


def select_draft_files(drafts_dir: Path, regenerate: bool) -> list[Path]:
    if not drafts_dir.exists():
        return []
    files = sorted(drafts_dir.glob("*.md"))
    if regenerate:
        return files
    pending = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        pdf_path = path.with_suffix(".pdf")
        if not has_ats_check(text) or not pdf_path.exists():
            pending.append(path)
    return pending


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20, help="Max drafts to process this run")
    parser.add_argument("--cv", type=Path, default=DEFAULT_CV_PATH, help="Path to the master CV (HTML)")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DRAFTS_DIR, help="Directory of draft .md files")
    parser.add_argument("--regenerate", action="store_true", help="Reprocess drafts that already have a PDF+ATS check")
    return parser.parse_args(argv)


async def process_one(path: Path, record, contact_line: str) -> tuple[bool, str]:
    draft_markdown = path.read_text(encoding="utf-8")
    content = extract_draft_content(draft_markdown)
    if not content.pitch and not content.bullets:
        return False, "could not find a pitch/bullets section to assemble"

    pdf_path = path.with_suffix(".pdf")
    html = build_cover_note_html(record, content, contact_line)
    await render_pdf(html, pdf_path)

    pdf_text = extract_pdf_text(pdf_path)
    result = check_ats_keywords(pdf_text, posting_keywords(record))

    base = strip_existing_ats_section(draft_markdown) if has_ats_check(draft_markdown) else draft_markdown.rstrip() + "\n"
    path.write_text(base + render_ats_section(result, len(pdf_text.strip())), encoding="utf-8")
    return True, f"{len(result.matched)}/{len(result.matched) + len(result.missing)} keywords found"


async def run(args: argparse.Namespace) -> int:
    contact_line = extract_contact_line(load_master_cv_text(args.cv)) if args.cv.exists() else ""
    records_by_id = {record.job_id: record for record in load_records(DATA_PATH)}
    targets = select_draft_files(args.dir, args.regenerate)[: args.limit]

    print(f"Draft files: {len(list(args.dir.glob('*.md'))) if args.dir.exists() else 0}")
    print(f"Pending this run: {len(targets)} (limit {args.limit})")

    assembled = 0
    errors = 0
    for path in targets:
        job_id = path.stem
        record = records_by_id.get(job_id)
        if record is None:
            errors += 1
            print(f"  ERROR {job_id}: no matching record in the dataset (posting removed?)")
            continue
        try:
            ok, detail = await process_one(path, record, contact_line)
        except Exception as error:  # noqa: BLE001 - report and keep going, one bad file shouldn't kill the batch
            errors += 1
            print(f"  ERROR {job_id} ({record.title}): {error}")
            continue
        if ok:
            assembled += 1
            print(f"  {record.title} — {record.company}: {detail} -> {path.with_suffix('.pdf')}")
        else:
            errors += 1
            print(f"  ERROR {job_id} ({record.title}): {detail}")

    print(f"Assembled: {assembled}, Errors: {errors}")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
