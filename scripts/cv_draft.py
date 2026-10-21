"""Draft CV bullets + a short application pitch for near-fit postings (plan
item 8.2).

Each call costs real money, so this is a deliberately separate, explicitly
run script - not part of the free collect/analyze pass. Scope is narrow on
purpose: only postings marked `Подходит` in `manual_review` AND still
`active` per the last `--check-availability` run - the same "worth acting
on right now" set `scripts/active_near_fit_report.py` surfaces, restricted
further to the strongest tier. One markdown file per posting under
`CV/drafts/<job_id>.md`; a rerun skips postings that already have a file
unless `--regenerate` is passed, so it only pays for genuinely new drafts.

Requires ANTHROPIC_API_KEY in the environment.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.cv_draft import DEFAULT_MODEL, generate_draft, load_master_cv_text, render_draft_markdown
from core.storage import load_manual_review, load_records

DATA_PATH = PROJECT_ROOT / "data" / "processed" / "jobs_clean_v1.xlsx"
DEFAULT_CV_PATH = PROJECT_ROOT / "CV" / "resume_improved.html"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "CV" / "drafts"


def draft_path(output_dir: Path, job_id: str) -> Path:
    return output_dir / f"{job_id}.md"


def select_candidates(records, manual_review: dict, output_dir: Path, regenerate: bool) -> list:
    candidates = []
    for record in records:
        review = manual_review.get(record.job_id, {})
        decision = str(review.get("decision", "")).strip()
        if decision != "Подходит":
            continue
        if str(record.availability_status or "").strip().casefold() != "active":
            continue
        if not regenerate and draft_path(output_dir, record.job_id).exists():
            continue
        candidates.append(record)
    return candidates


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10, help="Max API calls this run (cost safety cap)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--cv", type=Path, default=DEFAULT_CV_PATH, help="Path to the master CV (HTML)")
    parser.add_argument("--dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Output directory for draft .md files")
    parser.add_argument("--regenerate", action="store_true", help="Overwrite postings that already have a draft file")
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds between API calls")
    return parser.parse_args(argv)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY is not set - export it before running this script.")
        return 1
    if not args.cv.exists():
        print(f"Master CV not found: {args.cv}")
        return 1

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    records = load_records(DATA_PATH)
    manual_review = load_manual_review(DATA_PATH)
    cv_text = load_master_cv_text(args.cv)

    candidates = select_candidates(records, manual_review, args.dir, args.regenerate)[: args.limit]
    print(f"Total records: {len(records)}")
    print(f"Подходит + active + no existing draft this run: {len(candidates)} (limit {args.limit})")

    args.dir.mkdir(parents=True, exist_ok=True)
    drafted = 0
    errors = 0
    for record in candidates:
        try:
            draft = generate_draft(client, record, cv_text, model=args.model)
        except Exception as error:  # noqa: BLE001 - report and keep going, one bad call shouldn't kill the batch
            errors += 1
            print(f"  ERROR drafting {record.job_id} ({record.title}): {error}")
            time.sleep(args.delay)
            continue
        path = draft_path(args.dir, record.job_id)
        path.write_text(render_draft_markdown(record, draft), encoding="utf-8")
        drafted += 1
        print(f"  {record.title} — {record.company} -> {path}")
        time.sleep(args.delay)

    print(f"Drafted: {drafted}, Errors: {errors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
