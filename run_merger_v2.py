"""Merge all v2 promotions, deduplicate, and push to Google Sheets.

Scans data/promotions/*_v2.json, fills google_reviews, deduplicates,
writes local CSVs, then pushes to the live Google Sheet.

Usage:
    python run_merger_v2.py                 # merge + push to Sheets (default)
    python run_merger_v2.py --no-push       # merge + local CSV only (no Sheets)
    python run_merger_v2.py --v2-only       # skip legacy Valvoline file
    python run_merger_v2.py --qa-expanded   # keep all per-URL duplicates (QA mode)
"""
import argparse
import sys
from pathlib import Path
from collections import Counter
from re import sub

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.mergers.promotions_reviews_merger import (  # noqa: E402
    merge_all_data, save_merged_data, split_by_city,
)


def _dedup_rows(rows: list) -> list:
    """Collapse duplicate promos: keep the first row per (city, business, title).

    Rows with the same promotion in multiple URLs are tagged with
    duplicate_group_id by the scraper.  In qa_expanded mode they're all kept;
    here we keep exactly one per group per city so the sheet isn't cluttered.
    """
    seen: set = set()
    out: list = []
    for r in rows:
        key = (
            (r.get("city") or "").strip(),
            (r.get("business_name") or "").strip().lower(),
            (r.get("normalized_title") or r.get("ad_title") or "").strip().lower()[:80],
        )
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Merge v2 promotions and push to Google Sheets")
    p.add_argument("--v2-only", action="store_true",
                   help="Load only *_v2.json files; skip legacy entries (e.g. Valvoline).")
    p.add_argument("--qa-expanded", action="store_true",
                   help="Keep all per-URL duplicates (QA mode). Default: deduplicated.")
    p.add_argument("--no-push", action="store_true",
                   help="Skip pushing to Google Sheets; write local CSVs only.")
    args = p.parse_args()

    print("=" * 70)
    print("Promotions merger v2")
    print("=" * 70)

    rows = merge_all_data(include_legacy=not args.v2_only)

    # Deduplicate unless --qa-expanded.
    if not args.qa_expanded:
        before = len(rows)
        rows = _dedup_rows(rows)
        print(f"Deduplicated: {before} → {len(rows)} rows")
    else:
        print(f"QA-expanded mode: {len(rows)} rows (duplicates kept)")

    by_biz: Counter = Counter(r.get("business_name", "?") for r in rows)
    print(f"\nRows by competitor:")
    for name, n in sorted(by_biz.items()):
        print(f"  {name:<40}: {n}")

    # Save local files.
    output_file = save_merged_data(rows)
    csv_path = output_file.with_suffix(".csv")
    print(f"\nJSON : {output_file}")
    print(f"CSV  : {csv_path}")

    # Per-city summary.
    by_city = split_by_city(rows)
    print("\nPer-city rows:")
    for city in sorted(by_city):
        slug = sub(r"[^a-z0-9]+", "_", city.lower()).strip("_")
        city_path = output_file.parent / f"{slug}_promos.csv"
        print(f"  {city:<22}: {len(by_city[city]):>3} rows  → {city_path.name}")

    # Sample rows.
    if rows:
        print("\nSample rows (first 5):")
        for i, row in enumerate(rows[:5], 1):
            print(
                f"  {i}. [{row.get('business_name','?'):<30}|{row.get('city','?'):<14}|"
                f"{row.get('service_name','?'):<18}]"
            )
            print(f"     {(row.get('promo_description') or '')[:80]!r}")

    # Push to Google Sheets.
    if args.no_push:
        print("\n[--no-push] Skipping Google Sheets upload.")
        return 0

    print("\nPushing to Google Sheets...")
    try:
        from app.sheets.google_sheets_writer import write_city_tabs, SHEET_ID, CITY_TABS
        ok = write_city_tabs(rows, sheet_id=SHEET_ID, city_tabs=CITY_TABS)
        if ok:
            print("Google Sheets updated successfully.")
            print(f"  https://docs.google.com/spreadsheets/d/{SHEET_ID}")
        else:
            print("Google Sheets update FAILED — check logs above.")
            return 1
    except Exception as exc:
        print(f"ERROR pushing to Sheets: {exc}")
        print("Run with --no-push to skip and only save CSVs.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
