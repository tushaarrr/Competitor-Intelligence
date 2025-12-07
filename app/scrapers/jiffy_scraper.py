"""Jiffy Lube scraper - Text-based HTML extraction."""
import json
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime
import re
from fuzzywuzzy import fuzz

from app.extractors.firecrawl.firecrawl_client import fetch_with_firecrawl
from app.extractors.ocr.llm_cleaner import clean_promo_text_with_llm
from app.config.constants import DATA_DIR
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__, "jiffy_scraper.log")

DATA_DIR.mkdir(parents=True, exist_ok=True)
PROMOTIONS_DIR = DATA_DIR / "promotions"
PROMOTIONS_DIR.mkdir(parents=True, exist_ok=True)


def fetch_with_fallback(url: str) -> str:
    """Fetch HTML using Firecrawl, fallback to ZenRows/ScraperAPI."""
    # Try Firecrawl first
    firecrawl_result = fetch_with_firecrawl(url, timeout=90)
    
    if firecrawl_result.get("html") and not firecrawl_result.get("error"):
        logger.info("Successfully fetched with Firecrawl")
        return firecrawl_result.get("html", "")
    
    logger.warning("Firecrawl failed, trying fallback methods...")
    
    # Fallback to ZenRows
    try:
        from app.config.constants import ZENROWS_API_KEY
        if ZENROWS_API_KEY:
            import requests
            zenrows_url = f"https://api.zenrows.com/v1/?apikey={ZENROWS_API_KEY}&url={url}"
            response = requests.get(zenrows_url, timeout=30)
            response.raise_for_status()
            logger.info("Successfully fetched with ZenRows")
            return response.text
    except Exception as e:
        logger.warning(f"ZenRows fallback failed: {e}")
    
    # Fallback to ScraperAPI
    try:
        from app.config.constants import SCRAPERAPI_KEY
        if SCRAPERAPI_KEY:
            import requests
            scraperapi_url = f"http://api.scraperapi.com?api_key={SCRAPERAPI_KEY}&url={url}"
            response = requests.get(scraperapi_url, timeout=30)
            response.raise_for_status()
            logger.info("Successfully fetched with ScraperAPI")
            return response.text
    except Exception as e:
        logger.warning(f"ScraperAPI fallback failed: {e}")
    
    logger.error("All fetch methods failed")
    return ""


def normalize_title(title: str) -> str:
    """Normalize title by removing generic phrases."""
    if not title:
        return ""
    
    # Convert to lowercase for processing
    normalized = title.lower().strip()
    
    # Remove generic phrases
    generic_phrases = [
        "get", "coupon", "off a", "expires", "barcode", "valid", "offer",
        "save", "now", "limited", "time", "only", "click", "here", "see",
        "more", "details", "terms", "apply", "conditions"
    ]
    
    # Remove generic phrases (whole words only)
    words = normalized.split()
    filtered_words = []
    for word in words:
        # Clean punctuation
        clean_word = re.sub(r'[^\w\s]', '', word)
        if clean_word not in generic_phrases:
            filtered_words.append(word)
    
    normalized = " ".join(filtered_words)
    
    # Remove extra whitespace
    normalized = " ".join(normalized.split())
    
    return normalized


def extract_promo_sections(html: str) -> List[Dict]:
    """Extract promotional sections from HTML - focus on actual coupon offers."""
    from bs4 import BeautifulSoup
    
    soup = BeautifulSoup(html, "html.parser")
    promo_sections = []
    
    # Look for specific coupon wrapper elements (actual coupon cards)
    # Priority: coupon-wrapper-master, shell promotions, coupon-wrapper, then individual coupon cards
    coupon_selectors = [
        "div.coupon-wrapper-master",
        "div.shellgoplus-promo-master",  # Shell Go+ promotion
        "div[class*='coupon-wrapper']",
        "div[class*='coupon-card']",
        "div[class*='coupon-item']",
        "div.coupon-cms-wrapper",  # Wrapper that contains coupon grid
    ]
    
    seen_texts = set()
    extracted_wrappers = []
    
    # First, try to find individual coupon wrapper elements
    for selector in coupon_selectors:
        elements = soup.select(selector)
        for elem in elements:
            text = elem.get_text(strip=True)
            if text and len(text) > 30:  # Minimum length for valid coupon
                # Skip terms/conditions or fine print (don't start with actual offer text)
                text_lower = text.lower()
                skip_keywords = [
                    "cannot be combined",
                    "terms and conditions",
                    "one coupon per visit",
                    "valid at participating",
                    "fine print",
                    "browser does not support"
                ]
                if any(skip in text_lower[:100] for skip in skip_keywords):
                    continue
                
                # Check if this looks like an actual coupon (has discount, code, expiry, or bonus/reward)
                has_discount = bool(re.search(r'\$(\d+)|(\d+)\s*%|free', text, re.IGNORECASE))
                has_code = bool(re.search(r'(?:code|coupon|promo)[:\s]+([A-Z0-9]{3,})', text, re.IGNORECASE))
                has_expiry = bool(re.search(r'(?:expires?|valid until|until)[:\s]+', text, re.IGNORECASE))
                has_service = bool(re.search(r'(?:oil change|synthetic|pennzoil|service)', text, re.IGNORECASE))
                has_bonus = bool(re.search(r'(?:bonus|miles|rewards?|points)', text, re.IGNORECASE))
                
                # Include if it looks like an actual coupon offer:
                # - Has discount OR
                # - Has expiry AND service OR
                # - Has bonus/rewards (e.g., Shell Go+ miles)
                if has_discount or (has_expiry and has_service) or has_bonus:
                    text_normalized = " ".join(text.lower().split())
                    if text_normalized not in seen_texts:
                        seen_texts.add(text_normalized)
                        extracted_wrappers.append({
                            "html": str(elem),
                            "text": text,
                            "selector": selector
                        })
    
    # If we found coupon wrappers, use those
    if extracted_wrappers:
        promo_sections = extracted_wrappers
    else:
        # Fallback: look for coupon-cms-wrapper and extract individual coupons within it
        cms_wrapper = soup.find("div", class_=lambda x: x and "coupon-cms" in str(x).lower())
        if cms_wrapper:
            # Look for buttons or links that say "GET COUPON" - those are likely individual coupons
            coupon_links = cms_wrapper.find_all(["a", "button"], string=re.compile(r"GET\s+COUPON|coupon", re.IGNORECASE))
            for link in coupon_links:
                # Get the parent container
                parent = link.find_parent("div")
                if parent:
                    text = parent.get_text(strip=True)
                    if text and len(text) > 30:
                        text_normalized = " ".join(text.lower().split())
                        if text_normalized not in seen_texts:
                            seen_texts.add(text_normalized)
                            promo_sections.append({
                                "html": str(parent),
                                "text": text,
                                "selector": "coupon-link-parent"
                            })
    
    # If still no sections, extract from coupon-cms-wrapper as a whole
    if not promo_sections:
        cms_wrapper = soup.find("div", class_=lambda x: x and "coupon-cms" in str(x).lower())
        if cms_wrapper:
            text = cms_wrapper.get_text(strip=True)
            if text and len(text) > 50:
                promo_sections.append({
                    "html": str(cms_wrapper),
                    "text": text,
                    "selector": "coupon-cms-wrapper"
                })
    
    logger.info(f"Extracted {len(promo_sections)} promo sections from HTML")
    return promo_sections


def extract_discount_value(text: str) -> Optional[str]:
    """Extract discount value from text."""
    text_lower = text.lower()
    
    # Try dollar amount first
    dollar_match = re.search(r'\$(\d+(?:\.\d+)?)', text)
    if dollar_match:
        return f"${dollar_match.group(1)}"
    
    # Try percentage
    percent_match = re.search(r'(\d+)\s*%', text)
    if percent_match:
        return f"{percent_match.group(1)}%"
    
    # Try "free"
    if "free" in text_lower:
        return "free"
    
    return None


def extract_coupon_code(text: str) -> Optional[str]:
    """Extract coupon code from text."""
    # Look for patterns like "CODE: ABC123", "Use code XYZ", "Promo code: ABC"
    code_patterns = [
        r'(?:code|coupon|promo)[:\s]+([A-Z0-9]{3,20})',
        r'use[:\s]+([A-Z0-9]{3,20})',
        r'code[:\s]*([A-Z0-9]{3,20})',
    ]
    
    for pattern in code_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    
    return None


def calculate_title_overlap(title1: str, title2: str) -> float:
    """Calculate word overlap percentage between two titles."""
    if not title1 or not title2:
        return 0.0
    
    words1 = set(title1.lower().split())
    words2 = set(title2.lower().split())
    
    if not words1 or not words2:
        return 0.0
    
    intersection = words1.intersection(words2)
    union = words1.union(words2)
    
    if not union:
        return 0.0
    
    return (len(intersection) / len(union)) * 100


def are_promos_similar(promo1: Dict, promo2: Dict) -> bool:
    """Check if two promotions are similar for merging."""
    discount1 = promo1.get("discount_value")
    discount2 = promo2.get("discount_value")
    code1 = promo1.get("coupon_code")
    code2 = promo2.get("coupon_code")
    
    # Use stored normalized_title if available, otherwise normalize promotion_title
    title1 = promo1.get("normalized_title") or normalize_title(promo1.get("promotion_title", ""))
    title2 = promo2.get("normalized_title") or normalize_title(promo2.get("promotion_title", ""))
    
    # Same normalized title exactly - merge them
    if title1 == title2 and title1:
        return True
    
    # Same code but different discounts - merge them
    if code1 and code2 and code1 == code2 and discount1 != discount2:
        return True
    
    # Calculate overlap
    overlap = calculate_title_overlap(title1, title2)
    
    # Same normalized title (high similarity) - merge them
    if overlap >= 80:  # High overlap means same promotion
        return True
    
    # Similar titles (60%+ overlap) and same discount/code
    if (discount1 == discount2) or (code1 and code2 and code1 == code2):
        if overlap >= 60:
            return True
    
    return False


def merge_promos(promo1: Dict, promo2: Dict) -> Dict:
    """Merge two similar promotions, keeping the best information."""
    # Start with promo1 as base
    merged = promo1.copy()
    
    # Merge discount values (prefer the higher one if both are dollar amounts)
    discount1 = promo1.get("discount_value")
    discount2 = promo2.get("discount_value")
    
    if discount1 and discount2:
        # If both are dollar amounts, keep the higher one
        val1_match = re.search(r'(\d+(?:\.\d+)?)', discount1)
        val2_match = re.search(r'(\d+(?:\.\d+)?)', discount2)
        if val1_match and val2_match:
            val1 = float(val1_match.group(1))
            val2 = float(val2_match.group(1))
            merged["discount_value"] = discount1 if val1 >= val2 else discount2
        else:
            # Otherwise keep the first one
            merged["discount_value"] = discount1
    elif discount2 and not discount1:
        merged["discount_value"] = discount2
    
    # Merge offer details (keep the longer/more complete one)
    details1 = promo1.get("offer_details", "")
    details2 = promo2.get("offer_details", "")
    merged["offer_details"] = details1 if len(details1) >= len(details2) else details2
    
    # Merge other fields (prefer non-empty values)
    for key in ["coupon_code", "expiry_date", "promotion_title"]:
        if not merged.get(key) and promo2.get(key):
            merged[key] = promo2[key]
    
    return merged


def process_jiffy_promotions(competitor: Dict) -> List[Dict]:
    """Process Jiffy Lube promotions using text-based HTML extraction."""
    logger.info(f"Processing promotions for {competitor.get('name')}")
    
    promo_links = competitor.get("promo_links", [])
    if not promo_links:
        logger.warning(f"No promo_links found for {competitor.get('name')}")
        return []
    
    all_promos = []
    
    for promo_url in promo_links:
        logger.info(f"Fetching {promo_url}")
        
        # Step 1: Fetch HTML with fallback
        html = fetch_with_fallback(promo_url)
        
        if not html:
            logger.error(f"Failed to fetch HTML from {promo_url}")
            continue
        
        # Step 2: Extract promo sections from HTML
        promo_sections = extract_promo_sections(html)
        
        if not promo_sections:
            logger.warning(f"No promo sections found in HTML")
            # Try extracting from entire page
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            body_text = soup.get_text(strip=True)
            if body_text and len(body_text) > 100:
                promo_sections = [{
                    "html": html,
                    "text": body_text,
                    "selector": "full_page"
                }]
        
        # Step 3: Process each promo section
        for section in promo_sections:
            section_text = section["text"]
            section_html = section["html"]
            
            # Extract basic details
            discount_value = extract_discount_value(section_text)
            coupon_code = extract_coupon_code(section_text)
            
            # Clean with LLM
            context = f"Jiffy Lube coupon/promotion. HTML: {section_html[:1000]}"
            cleaned_data = clean_promo_text_with_llm(section_text, context)
            
            # Build promotion title
            if cleaned_data and cleaned_data.get("service_name"):
                promotion_title = cleaned_data.get("service_name")
            elif cleaned_data and cleaned_data.get("promo_description"):
                first_line = cleaned_data.get("promo_description", "").split("\n")[0].strip()[:100]
                promotion_title = first_line if first_line else section_text.split("\n")[0][:100]
            else:
                # Extract first meaningful line
                lines = [l.strip() for l in section_text.split("\n") if l.strip() and len(l.strip()) > 10]
                promotion_title = lines[0][:100] if lines else "Jiffy Lube Promotion"
            
            # Use LLM cleaned data if available
            if cleaned_data:
                offer_details = cleaned_data.get("promo_description") or section_text[:1000]
                discount_value = cleaned_data.get("discount_value") or discount_value
                coupon_code = cleaned_data.get("coupon_code") or coupon_code
                expiry_date = cleaned_data.get("expiry_date")
            else:
                offer_details = section_text[:1000]
                expiry_date = None
            
            promo = {
                "website": competitor.get("domain", ""),
                "page_url": promo_url,
                "business_name": competitor.get("name", ""),
                "google_reviews": None,
                "service_name": cleaned_data.get("service_name", "oil change") if cleaned_data else "oil change",
                "promo_description": offer_details,
                "category": "oil change",
                "contact": competitor.get("address", ""),
                "location": competitor.get("address", ""),
                "offer_details": offer_details,
                "ad_title": promotion_title,
                "ad_text": section_text[:500],
                "new_or_updated": "new",
                "date_scraped": datetime.now().isoformat(),
                "discount_value": discount_value,
                "coupon_code": coupon_code,
                "expiry_date": expiry_date,
                "promotion_title": promotion_title,
                "normalized_title": normalize_title(promotion_title)
            }
            
            all_promos.append(promo)
            logger.info(f"✓ Added promo: {promotion_title} - {discount_value or 'N/A'}")
    
    # Step 4: Deduplicate using complex rules
    logger.info(f"Found {len(all_promos)} promotions before deduplication")
    
    # Group 1: By discount_value + coupon_code
    groups_by_discount_code = {}
    for promo in all_promos:
        key = (promo.get("discount_value") or "none", promo.get("coupon_code") or "none")
        if key not in groups_by_discount_code:
            groups_by_discount_code[key] = []
        groups_by_discount_code[key].append(promo)
    
    # Select best promo from each discount+code group
    deduplicated_by_discount_code = []
    for key, group in groups_by_discount_code.items():
        if len(group) == 1:
            deduplicated_by_discount_code.append(group[0])
        else:
            # Select best promo (most complete info)
            best = max(group, key=lambda p: (
                len(p.get("offer_details", "")),
                len(p.get("promotion_title", "")),
                bool(p.get("coupon_code")),
                bool(p.get("expiry_date"))
            ))
            deduplicated_by_discount_code.append(best)
            logger.info(f"Grouped {len(group)} promos by discount+code, kept best")
    
    # Group 2: By normalized title (if no discount/code)
    groups_by_title = {}
    for promo in deduplicated_by_discount_code:
        if not promo.get("discount_value") and not promo.get("coupon_code"):
            norm_title = promo.get("normalized_title", "")
            # Skip if normalized title is too short (likely not a real promo)
            if norm_title and len(norm_title.split()) >= 3:
                if norm_title not in groups_by_title:
                    groups_by_title[norm_title] = []
                groups_by_title[norm_title].append(promo)
    
    # Select best promo from each title group
    final_promos = []
    title_grouped_indices = set()
    
    for norm_title, group in groups_by_title.items():
        if len(group) > 1:
            best = max(group, key=lambda p: (
                len(p.get("offer_details", "")),
                len(p.get("promotion_title", "")),
                bool(p.get("expiry_date"))
            ))
            final_promos.append(best)
            # Mark these for removal from deduplicated list
            for p in group:
                if p != best:
                    title_grouped_indices.add(id(p))
            logger.info(f"Grouped {len(group)} promos by normalized title, kept best")
        else:
            final_promos.append(group[0])
    
    # Add promos that weren't grouped by title
    for promo in deduplicated_by_discount_code:
        if (promo.get("discount_value") or promo.get("coupon_code")) or id(promo) not in title_grouped_indices:
            if promo not in final_promos:
                final_promos.append(promo)
    
    # Final pass: Merge promos with same code but different discounts, and similar titles (60%+ overlap)
    merged_promos = []
    processed_indices = set()
    
    for i, promo1 in enumerate(final_promos):
        if i in processed_indices:
            continue
        
        merged = promo1.copy()
        processed_indices.add(i)
        
        # Look for similar promos to merge
        for j, promo2 in enumerate(final_promos[i+1:], start=i+1):
            if j in processed_indices:
                continue
            
            if are_promos_similar(merged, promo2):
                merged = merge_promos(merged, promo2)
                processed_indices.add(j)
                logger.info(f"Merged similar promos: {merged.get('promotion_title')[:50]} with {promo2.get('promotion_title')[:50]}")
        
        merged_promos.append(merged)
    
    logger.info(f"Total unique promotions found: {len(merged_promos)}")
    return merged_promos


def scrape_jiffy(competitor: Dict) -> Dict:
    """Main entry point for Jiffy Lube scraper."""
    try:
        promos = process_jiffy_promotions(competitor)
        
        # Save results
        output_file = PROMOTIONS_DIR / f"{competitor.get('name', 'jiffy').lower().replace(' ', '_')}.json"
        result = {
            "competitor": competitor.get("name"),
            "scraped_at": datetime.now().isoformat(),
            "promotions": promos,
            "count": len(promos)
        }
        
        output_file.write_text(json.dumps(result, indent=2, default=str))
        logger.info(f"Saved {len(promos)} promotions to {output_file}")
        
        return result
        
    except Exception as e:
        logger.error(f"Error scraping Jiffy Lube: {e}", exc_info=True)
        return {
            "competitor": competitor.get("name"),
            "error": str(e),
            "promotions": [],
            "count": 0
        }


if __name__ == "__main__":
    import sys
    from pathlib import Path
    
    # Load competitor data
    competitor_file = Path(__file__).parent.parent / "config" / "competitor_list.json"
    competitors = json.loads(competitor_file.read_text())
    
    # Find Jiffy Lube
    jiffy = next((c for c in competitors if "jiffy" in c.get("name", "").lower()), None)
    
    if not jiffy:
        logger.error("Jiffy Lube not found in competitor list")
        sys.exit(1)
    
    result = scrape_jiffy(jiffy)
    print(f"\n✅ Scraping complete!")
    print(f"   Found {result.get('count', 0)} promotions")
    print(f"   Saved to: data/promotions/")
    print(f"\n📊 Summary:")
    for promo in result.get("promotions", []):
        print(f"   • {promo.get('promotion_title', 'N/A')}: {promo.get('discount_value', 'N/A')}")

