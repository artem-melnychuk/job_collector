"""Review already-drafted CV pitches/bullets for honesty and relevance
(plan item 8.3).

A separate, explicitly-run script from `scripts/cv_draft.py` - the reviewer
is a fresh, stateless API call per draft, not the drafter checking its own
work. Scans `CV/drafts/*.md`, appends a "## Ревью" section to each with a
verdict (`approved` / `needs_revision`) and a list of concrete issues, if
any. Reviews the file exactly as it is on disk, including any hand edits.

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

from core.cv_draft import load_master_cv_text
from core.cv_review import DEFAULT_MODEL, append_review_section, generate_review, has_review
from core.storage import load_records

DATA_PATH = PROJECT_ROOT / "data" / "processed" / "jobs_clean_v1.xlsx"
DEFAULT_CV_PATH = PROJECT_ROOT / "CV" / "resume_improved.html"
DEFAULT_DRAFTS_DIR = PROJECT_ROOT / "CV" / "drafts"


def select_draft_files(drafts_dir: Path, regenerate: bool) -> list[Path]:
    if not drafts_dir.exists():
        return []
    files = sorted(drafts_dir.glob("*.md"))
    if regenerate:
        return files
    return [path for path in files if not has_review(path.read_text(encoding="utf-8"))]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10, help="Max API calls this run (cost safety cap)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--cv", type=Path, default=DEFAULT_CV_PATH, help="Path to the master CV (HTML)")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DRAFTS_DIR, help="Directory of draft .md files")
    parser.add_argument("--regenerate", action="store_true", help="Re-review drafts that already have a review section")
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

    records_by_id = {record.job_id: record for record in load_records(DATA_PATH)}
    cv_text = load_master_cv_text(args.cv)

    targets = select_draft_files(args.dir, args.regenerate)[: args.limit]
    print(f"Draft files: {len(list(args.dir.glob('*.md'))) if args.dir.exists() else 0}")
    print(f"Not yet reviewed this run: {len(targets)} (limit {args.limit})")

    reviewed = 0
    errors = 0
    for path in targets:
        job_id = path.stem
        record = records_by_id.get(job_id)
        if record is None:
            errors += 1
            print(f"  ERROR reviewing {job_id}: no matching record in the dataset (posting removed?)")
            continue
        draft_markdown = path.read_text(encoding="utf-8")
        try:
            review = generate_review(client, record, cv_text, draft_markdown, model=args.model)
        except Exception as error:  # noqa: BLE001 - report and keep going, one bad call shouldn't kill the batch
            errors += 1
            print(f"  ERROR reviewing {job_id} ({record.title}): {error}")
            time.sleep(args.delay)
            continue
        path.write_text(append_review_section(draft_markdown, review), encoding="utf-8")
        reviewed += 1
        marker = "✅" if review.verdict == "approved" else "⚠️"
        print(f"  {marker} {record.title} — {record.company} ({len(review.issues)} issues) -> {path}")
        time.sleep(args.delay)

    print(f"Reviewed: {reviewed}, Errors: {errors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
