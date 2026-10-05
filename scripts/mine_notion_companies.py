"""Extract every row of a Notion-embed company/table page via its "Load more"
pagination, and save it as a CSV.

Built for `https://agilefluent.notion.site/600-3654f3678247803192feca7cbddd7036`
("600+ companies with Russian-speaking roots"), mined by hand in an earlier
session for Greenhouse/Lever `career_boards` candidates (see CLAUDE.md /
docs/archive/PROJECT_HANDOFF_RU.md). That pass only covered ~100 of 599 rows because the
interactive browser tool's cached element reference (`ref`) kept resolving to
a stale screen position after the DOM grew/scrolled on each click, forcing a
fresh screenshot before every single click. A plain Playwright script doesn't
have that problem: it re-queries the "Load more" button from a fresh DOM
snapshot immediately before every click, in a tight loop, so it can paginate
unattended.

The row/column extraction is generic to any Notion-embed table view (the
`.notion-table-view-row` / `[data-col-index]` / `.notion-table-view-header-cell`
selectors aren't specific to this one page) — reuse this for a different
Notion/Airtable company list by passing a different `--url`/`--out`.

Usage:
    .venv\\Scripts\\python.exe -B scripts\\mine_notion_companies.py --headless
    .venv\\Scripts\\python.exe -B scripts\\mine_notion_companies.py --url <other-notion-url> --out data\\reference\\other.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import Page, async_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_URL = "https://agilefluent.notion.site/600-3654f3678247803192feca7cbddd7036"
DEFAULT_OUT = PROJECT_ROOT / "data" / "reference" / "notion_companies.csv"

# Re-queried fresh from the live DOM before every click/read below - never
# cached across a click, which is exactly what made the earlier interactive
# browser-tool attempt unreliable (stale cached element positions).
_EXTRACT_JS = """
() => {
    const headers = Array.from(document.querySelectorAll('.notion-table-view-header-cell'))
        .map(cell => cell.textContent.trim());
    const rows = Array.from(document.querySelectorAll('.notion-table-view-row')).map(row =>
        Array.from(row.querySelectorAll('[data-col-index]')).map(cell => {
            const link = cell.querySelector('a');
            return { text: cell.textContent.trim(), href: link ? link.href : null };
        })
    );
    return { headers, rows };
}
"""

_CLICK_LOAD_MORE_JS = """
() => {
    const candidates = document.querySelectorAll('div[role="button"]');
    for (const el of candidates) {
        if (el.textContent && el.textContent.trim().toLowerCase() === 'load more') {
            el.click();
            return true;
        }
    }
    return false;
}
"""


def row_from_cells(headers: list[str], cells: list[dict[str, Any]]) -> dict[str, str]:
    """Zip one row's raw {text, href} cells against column headers.

    Prefers a cell's link target over its (often visually truncated) display
    text - e.g. the "Career page" column shows "cryptocurrencyjobs.co/sta…"
    but the underlying href is the full, usable URL.
    """
    record: dict[str, str] = {}
    for index, header in enumerate(headers):
        cell = cells[index] if index < len(cells) else {}
        record[header] = cell.get("href") or cell.get("text") or ""
    return record


def dedupe_rows(records: list[dict[str, str]], key_column: str) -> list[dict[str, str]]:
    """Keep the first occurrence of each key_column value, in original order."""
    seen: set[str] = set()
    result = []
    for record in records:
        key = record.get(key_column, "")
        if key and key not in seen:
            seen.add(key)
            result.append(record)
    return result


async def extract_all_rows(page: Page, max_rows: int | None, max_stalls: int = 3) -> tuple[list[str], list[dict[str, Any]]]:
    """Click "Load more" until the row count stops growing (or max_rows is
    hit), then return (headers, raw_cells_per_row).

    Tolerates a few consecutive no-growth clicks (`max_stalls`) before
    concluding the table is fully loaded, since a slow network response can
    make one click look like a stall when it isn't.
    """
    stalls = 0
    previous_count = -1
    while True:
        snapshot = await page.evaluate(_EXTRACT_JS)
        headers, rows = snapshot["headers"], snapshot["rows"]
        current_count = len(rows)

        if max_rows is not None and current_count >= max_rows:
            return headers, rows[:max_rows]

        if current_count == previous_count:
            stalls += 1
            if stalls >= max_stalls:
                return headers, rows
        else:
            stalls = 0
        previous_count = current_count

        clicked = await page.evaluate(_CLICK_LOAD_MORE_JS)
        if not clicked:
            return headers, rows
        await page.wait_for_timeout(1200)


def write_csv(path: Path, headers: list[str], records: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(records)


async def _goto_with_rate_limit_retry(page: Page, url: str, attempts: int = 4) -> None:
    """Notion (agilefluent.notion.site, and presumably any notion.site embed)
    answers repeated automated requests with a 429 for a while - observed
    directly while developing this script, after a handful of loads in quick
    succession. A real person browsing wouldn't trigger this; a scripted
    retry loop does. Back off and retry rather than failing outright, since
    the block is temporary (tens of seconds), not a permanent ban like
    work_ua/robota_ua's anti-bot challenge.
    """
    last_status: int | None = None
    for attempt in range(1, attempts + 1):
        response = await page.goto(
            url,
            # Not "networkidle": Notion keeps a background connection (live
            # sync) open indefinitely, so networkidle never fires and this
            # would always time out. "load" plus the caller's row-selector
            # wait is enough to know the table has actually rendered.
            wait_until="load",
            timeout=60_000,
        )
        last_status = response.status if response else None
        if last_status != 429:
            return
        if attempt < attempts:
            await page.wait_for_timeout(20_000 * attempt)
    raise RuntimeError(f"Notion kept returning HTTP 429 after {attempts} attempts - try again in a few minutes.")


async def run(url: str, out_path: Path, headless: bool, max_rows: int | None) -> int:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        try:
            page = await browser.new_page(viewport={"width": 1400, "height": 1000})
            await _goto_with_rate_limit_retry(page, url)
            await page.wait_for_selector(".notion-table-view-row", timeout=60_000)
            headers, raw_rows = await extract_all_rows(page, max_rows)
        finally:
            await browser.close()

    key_column = headers[0] if headers else ""
    records = [row_from_cells(headers, cells) for cells in raw_rows]
    records = dedupe_rows(records, key_column)

    write_csv(out_path, headers, records)
    print(f"Extracted {len(records)} rows ({len(headers)} columns: {', '.join(headers)})")
    print(f"Saved: {out_path}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL, help="Notion page URL with a table view")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="CSV output path")
    parser.add_argument("--headless", action="store_true", help="Run without a visible browser window")
    parser.add_argument("--max-rows", type=int, default=None, help="Stop after this many rows (default: all)")
    return parser.parse_args(argv)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    return asyncio.run(run(args.url, args.out, args.headless, args.max_rows))


if __name__ == "__main__":
    raise SystemExit(main())
