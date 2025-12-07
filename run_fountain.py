"""Run Fountain Tire scraper."""
import json
from pathlib import Path
from app.scrapers.fountain_scraper import scrape_fountain

if __name__ == "__main__":
    # Load competitor data
    competitor_file = Path(__file__).parent / "app" / "config" / "competitor_list.json"
    competitors = json.loads(competitor_file.read_text())
    
    # Find Fountain Tire
    fountain = next((c for c in competitors if "fountain" in c.get("name", "").lower()), None)
    
    if not fountain:
        print("❌ Fountain Tire not found in competitor list")
        exit(1)
    
    print(f"🚀 Starting Fountain Tire scraper...")
    print(f"   URLs: {', '.join(fountain.get('promo_links', []))}")
    print()
    
    result = scrape_fountain(fountain)
    
    print(f"\n✅ Scraping complete!")
    print(f"   Found {result.get('count', 0)} promotions")
    print(f"   Saved to: data/promotions/")
    
    if result.get("promotions"):
        print(f"\n📊 Summary:")
        for promo in result.get("promotions", []):
            title = promo.get('promotion_title', promo.get('ad_title', 'N/A'))
            discount = promo.get('discount_value', 'N/A')
            print(f"   • {title}: {discount}")

