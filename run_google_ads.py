"""Run Google Ads Transparency Center scraper.

Fetches competitor ad listings from adstransparency.google.com for all 9
competitors using their Canada-region domain or advertiser ID.

Usage:
    python run_google_ads.py                    # all 9 competitors
    python run_google_ads.py --smoke            # first 2 competitors only (quick test)
    python run_google_ads.py --final-deduped    # collapse per-competitor duplicates
    python run_google_ads.py --competitor Midas # single competitor by name
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.scrapers.google_ads_scraper import (  # noqa: E402
    scrape_google_ads, COMPETITORS, ADS_DIR, _build_url,
)

CSV_COLUMNS = [
    "business_name",
    "ad_title",
    "ad_description",
    "discount_value",
    "coupon_code",
    "ad_link",
    "displayed_link",
    "date_scraped",
]


def export_ads_csv(ads: list, dest: Path) -> Path:
    if not ads:
        dest.write_text("", encoding="utf-8")
        return dest
    all_keys = list(dict.fromkeys(
        CSV_COLUMNS + [k for a in ads for k in a if k not in CSV_COLUMNS]
    ))
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
        w.writeheader()
        for a in ads:
            flat = {}
            for k in all_keys:
                v = a.get(k)
                if v is None:
                    flat[k] = ""
                elif isinstance(v, (list, dict)):
                    flat[k] = json.dumps(v, ensure_ascii=False)
                else:
                    flat[k] = str(v)
            w.writerow(flat)
    return dest


def main() -> int:
    p = argparse.ArgumentParser(description="Google Ads Transparency Center scraper")
    p.add_argument("--smoke", action="store_true",
                   help="Run only the first 2 competitors (quick test).")
    p.add_argument("--final-deduped", dest="mode", action="store_const",
                   const="final_deduped", default="qa_expanded",
                   help="Collapse duplicate ads within each competitor.")
    p.add_argument("--competitor", metavar="NAME",
                   help="Scrape a single competitor by name (case-insensitive partial match).")
    args = p.parse_args()

    # Determine competitor list.
    if args.competitor:
        needle = args.competitor.lower()
        competitors = [c for c in COMPETITORS if needle in c["name"].lower()]
        if not competitors:
            print(f"ERROR: No competitor matching {args.competitor!r}")
            print("Available:", ", ".join(c["name"] for c in COMPETITORS))
            return 1
    elif args.smoke:
        competitors = COMPETITORS[:2]
    else:
        competitors = COMPETITORS

    print("=" * 70)
    print("Google Ads Transparency Center scraper")
    print("=" * 70)
    print(f"Competitors : {len(competitors)}")
    for c in competitors:
        print(f"  {c['name']:<32}  {_build_url(c)}")
    print(f"\nMode: {args.mode}\n")

    t0 = time.time()
    result = scrape_google_ads(competitors, mode=args.mode)
    runtime = time.time() - t0

    ads = result.get("ads", [])
    val = result.get("validation", {})

    print("-" * 70)
    print(f"Total ads extracted : {result.get('count', 0)}")
    print(f"Runtime             : {runtime:.1f}s")
    print(f"Competitors OK      : {val.get('competitors_with_ads', 0)} / {len(competitors)}")
    print()

    # Per-competitor table
    print("Per-competitor results:")
    header = f"  {'Competitor':<32} {'Status':<8} {'Ads':>4}  URL"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for entry in val.get("url_log", []):
        ok = entry["status"] == "ok"
        print(
            f"  {entry['competitor']:<32} "
            f"{'OK' if ok else 'FAIL':<8} "
            f"{entry['ads_found']:>4}  "
            f"{entry['url']}"
        )
    print()

    # Sample ads (up to 5)
    if ads:
        print(f"Sample ads (first 5 of {len(ads)}):")
        for a in ads[:5]:
            print(f"  [{a['business_name']:<28}]")
            print(f"     title    : {a['ad_title']!r}")
            print(f"     desc     : {(a.get('ad_description') or '')[:90]!r}")
            print(f"     discount : {a.get('discount_value','')!r}")
            print(f"     coupon   : {a.get('coupon_code','')!r}")
            print(f"     link     : {a.get('displayed_link','')!r}")
        print()

    if result.get("count", 0) == 0:
        print("NOTE: 0 ads extracted.")
        print("  The Ads Transparency Center is a JavaScript SPA — Firecrawl")
        print("  needs to fully render the page before cards appear.")
        print("  If HTML length is small (< 5000 chars), the page didn't render.")
        print("  Check data/ads/google_ads.json for raw response details.")

    # Save outputs
    json_path = ADS_DIR / "google_ads.json"
    csv_path = ADS_DIR / "google_ads.csv"
    export_ads_csv(ads, csv_path)
    print(f"Saved: {json_path}")
    print(f"CSV  : {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
