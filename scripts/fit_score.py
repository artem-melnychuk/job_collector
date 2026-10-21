"""Score undecided postings for candidate fit via an LLM call (plan item 8.1).

Each call costs real money, so this is a deliberately separate, explicitly
run script - not part of the free collect/analyze pass. By default it only
scores postings nobody has manually decided on yet (skips anything with a
`decision` already in `manual_review`) and skips anything already scored,
so a rerun only pays for genuinely new postings unless told otherwise.

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

from core.fit_score import DEFAULT_MODEL, load_master_cv_text, score_record
from core.storage import load_manual_review, load_records, save_records

DATA_PATH = PROJECT_ROOT / "data" / "processed" / "jobs_clean_v1.xlsx"
DEFAULT_CV_PATH = PROJECT_ROOT / "CV" / "resume_improved.html"


def select_candidates(records, manual_review: dict, rescore: bool) -> list:
    candidates = []
    for record in records:
        decision = str(manual_review.get(record.job_id, {}).get("decision", "")).strip()
        if decision:
            continue
        if str(record.fit_score or "").strip() and not rescore:
            continue
        candidates.append(record)
    return candidates


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20, help="Max API calls this run (cost safety cap)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--cv", type=Path, default=DEFAULT_CV_PATH, help="Path to the master CV (HTML)")
    parser.add_argument("--rescore", action="store_true", help="Re-score postings that already have a fit_score")
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

    by_id = {record.job_id: record for record in records}
    candidates = select_candidates(records, manual_review, args.rescore)[: args.limit]

    print(f"Total records: {len(records)}")
    print(f"Undecided + unscored candidates this run: {len(candidates)} (limit {args.limit})")

    scored = 0
    errors = 0
    for record in candidates:
        try:
            updated = score_record(client, record, cv_text, model=args.model)
        except Exception as error:  # noqa: BLE001 - report and keep going, one bad call shouldn't kill the batch
            errors += 1
            print(f"  ERROR scoring {record.job_id} ({record.title}): {error}")
            time.sleep(args.delay)
            continue
        by_id[record.job_id] = updated
        scored += 1
        print(f"  [{updated.fit_score:>3}] {record.title} — {record.company}")
        time.sleep(args.delay)

    if scored:
        save_records(by_id.values(), DATA_PATH)
        print(f"Processed XLSX: {DATA_PATH}")
    print(f"Scored: {scored}, Errors: {errors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
