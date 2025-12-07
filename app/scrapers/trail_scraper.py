"""Trail Tire Auto Centres scraper - Banner image OCR for tire promotions."""
import json
from pathlib import Path
from typing import List, Dict
from datetime import datetime
import hashlib
import re

from app.extractors.firecrawl.firecrawl_client import fetch_with_firecrawl
from app.extractors.html_parser import find_images_by_css_selector
from app.extractors.images.image_downloader import download_image, get_image_hash, normalize_url
from app.extractors.ocr.ocr_processor import ocr_image, detect_promo_keywords
from app.extractors.ocr.llm_cleaner import clean_promo_text_with_llm
from app.config.constants import PROMO_KEYWORDS, DATA_DIR
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__, "trail_scraper.log")

DATA_DIR.mkdir(parents=True, exist_ok=True)
PROMOTIONS_DIR = DATA_DIR / "promotions"
PROMOTIONS_DIR.mkdir(parents=True, exist_ok=True)


def normalize_title(title: str) -> str:
    """Normalize title for deduplication."""
    return " ".join(title.lower().strip().split())


def calculate_ocr_hash(ocr_text: str) -> str:
    """Calculate hash of OCR text for similarity checking."""
    normalized = " ".join(ocr_text.lower().strip().split())
    return hashlib.md5(normalized.encode()).hexdigest()


def are_texts_similar(text1: str, text2: str, threshold: int = 85) -> bool:
    """Check if two OCR texts are similar."""
    if not text1 or not text2:
        return False
    
    from fuzzywuzzy import fuzz
    similarity = fuzz.token_set_ratio(text1.lower(), text2.lower())
    return similarity >= threshold


def extract_promo_details_from_text(text: str) -> Dict:
    """Extract promotion details from OCR text."""
    text_lower = text.lower()
    
    # Extract discount value
    discount_value = None
    dollar_match = re.search(r'\$(\d+(?:\.\d+)?)', text)
    if dollar_match:
        discount_value = f"${dollar_match.group(1)}"
    else:
        percent_match = re.search(r'(\d+)\s*%', text)
        if percent_match:
            discount_value = f"{percent_match.group(1)}%"
        elif "free" in text_lower:
            discount_value = "free"
    
    # Extract coupon code (look for patterns like "CODE: ABC123" or "Use code XYZ")
    coupon_code = None
    code_patterns = [
        r'code[:\s]+([A-Z0-9]{3,20})',
        r'coupon[:\s]+([A-Z0-9]{3,20})',
        r'use[:\s]+([A-Z0-9]{3,20})',
    ]
    for pattern in code_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            coupon_code = match.group(1).upper()
            break
    
    # Extract expiry date
    expiry_date = None
    date_patterns = [
        r'(?:expires?|expiry|valid until|until|ends?)[\s:]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',
        r'(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',
    ]
    for pattern in date_patterns:
        date_match = re.search(pattern, text, re.IGNORECASE)
        if date_match:
            expiry_date = date_match.group(1).strip()
            break
    
    return {
        "discount_value": discount_value,
        "coupon_code": coupon_code,
        "expiry_date": expiry_date
    }


def process_trail_promotions(competitor: Dict) -> List[Dict]:
    """Process Trail Tire promotions using banner image OCR."""
    logger.info(f"Processing promotions for {competitor.get('name')}")
    
    promo_links = competitor.get("promo_links", [])
    if not promo_links:
        logger.warning(f"No promo_links found for {competitor.get('name')}")
        return []
    
    all_promos = []
    seen_image_urls = set()
    seen_titles = set()
    seen_ocr_hashes = set()
    seen_image_hashes = set()
    
    for promo_url in promo_links:
        logger.info(f"Fetching {promo_url}")
        
        # Step 1: Fetch with Firecrawl
        firecrawl_result = fetch_with_firecrawl(promo_url, timeout=90)
        
        if firecrawl_result.get("error"):
            logger.error(f"Firecrawl error: {firecrawl_result['error']}")
            continue
        
        html = firecrawl_result.get("html", "")
        if not html:
            logger.warning(f"No HTML content from Firecrawl for {promo_url}")
            continue
        
        # Step 2: Find banner images using CSS selector
        # Note: Divs have multiple classes: "probox" and "promotion_width"
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin
        soup = BeautifulSoup(html, "html.parser")
        
        # Find all divs that have both "probox" and "promotion_width" classes
        promo_divs = soup.find_all("div", class_=lambda x: x and "probox" in x and "promotion_width" in x)
        images = []
        seen_urls = set()
        
        for div in promo_divs:
            # Find images inside this div
            imgs = div.find_all("img")
            for img in imgs:
                # Extract image URL from multiple attributes (check in priority order)
                image_url = None
                
                # Try data-src first (common for lazy loading)
                if img.get("data-src"):
                    image_url = urljoin(promo_url, img.get("data-src"))
                # Try data-lazy-src
                elif img.get("data-lazy-src"):
                    image_url = urljoin(promo_url, img.get("data-lazy-src"))
                # Try data-original
                elif img.get("data-original"):
                    image_url = urljoin(promo_url, img.get("data-original"))
                # Try src
                elif img.get("src") and img.get("src").strip():
                    image_url = urljoin(promo_url, img.get("src"))
                # Try data-url
                elif img.get("data-url"):
                    image_url = urljoin(promo_url, img.get("data-url"))
                # Try srcset
                elif img.get("srcset"):
                    srcset = img.get("srcset")
                    first_url = srcset.split(",")[0].strip().split()[0]
                    image_url = urljoin(promo_url, first_url)
                
                if not image_url or not image_url.strip() or image_url.strip() == "/":
                    continue
                
                # Skip placeholder/empty images
                if any(skip in image_url.lower() for skip in ["placeholder", "blank", "1x1", "spacer"]):
                    continue
                
                # Normalize and deduplicate
                normalized_url = image_url.lower().strip()
                if normalized_url in seen_urls:
                    continue
                seen_urls.add(normalized_url)
                
                alt_text = img.get("alt", "")
                images.append({
                    "image_url": image_url,
                    "alt_text": alt_text
                })
        
        logger.info(f"Found {len(images)} banner images")
        
        if not images:
            logger.warning(f"No images found with selector 'div.probox_promotion_width img'")
            continue
        
        # Step 3: Process each image
        for img_data in images:
            image_url = img_data["image_url"]
            alt_text = img_data.get("alt_text", "")
            
            # Normalize image URL for deduplication
            normalized_img_url = normalize_url(promo_url, image_url).lower().strip()
            
            # Skip if we've seen this image URL before
            if normalized_img_url in seen_image_urls:
                logger.info(f"Skipping duplicate image URL: {image_url[:80]}...")
                continue
            seen_image_urls.add(normalized_img_url)
            
            # Download image
            logger.info(f"Downloading image: {image_url[:80]}...")
            img_path = download_image(normalize_url(promo_url, image_url))
            
            if not img_path:
                logger.warning(f"Failed to download image: {image_url}")
                continue
            
            # Check for duplicate image (same file content)
            img_hash = get_image_hash(img_path)
            if img_hash and img_hash in seen_image_hashes:
                logger.info(f"Skipping duplicate image content: {image_url}")
                img_path.unlink()
                continue
            seen_image_hashes.add(img_hash)
            
            # Step 4: Run OCR (Google Vision primary, Tesseract fallback)
            logger.info(f"Running OCR on {img_path.name}...")
            ocr_text = ocr_image(img_path)
            
            # Step 5: Skip if OCR text too short
            if not ocr_text or len(ocr_text.strip()) < 10:
                logger.warning(f"OCR text too short (< 10 chars) from {image_url}")
                img_path.unlink()
                continue
            
            # Step 6: Check if it's promo-related
            # Be more lenient for tire promotions - check for discount value or keywords
            is_promo = detect_promo_keywords(ocr_text, PROMO_KEYWORDS)
            extracted_details = extract_promo_details_from_text(ocr_text)
            
            # Also consider it a promo if we found discount value
            if not is_promo and not extracted_details.get("discount_value"):
                # Last check: if alt text contains promo keywords
                alt_lower = alt_text.lower()
                has_keyword = any(kw in alt_lower for kw in ["rebate", "off", "discount", "save", "promo", "tire"])
                if not has_keyword:
                    logger.info(f"Image doesn't contain promo keywords or discount: {image_url}")
                    img_path.unlink()
                    continue
            
            # Step 7: Deduplicate by OCR text similarity
            ocr_hash = calculate_ocr_hash(ocr_text)
            is_duplicate = False
            
            for existing_hash in seen_ocr_hashes:
                # We'd need to store the text, but for now just check hash
                # If exact hash match, it's definitely duplicate
                if ocr_hash == existing_hash:
                    logger.info(f"Skipping duplicate OCR content (exact hash match)")
                    is_duplicate = True
                    break
            
            if is_duplicate:
                img_path.unlink()
                continue
            
            # Store OCR hash for future comparisons
            seen_ocr_hashes.add(ocr_hash)
            
            # Step 8: Extract basic details from text
            extracted_details = extract_promo_details_from_text(ocr_text)
            
            # Step 9: Clean with LLM
            context = f"Trail Tire promotion banner. Alt text: {alt_text}"
            cleaned_data = clean_promo_text_with_llm(ocr_text, context)
            
            # Step 10: Build promotion title
            if cleaned_data and cleaned_data.get("service_name"):
                promotion_title = cleaned_data.get("service_name")
            elif cleaned_data and cleaned_data.get("promo_description"):
                # Use first line of description as title
                first_line = cleaned_data.get("promo_description", "").split("\n")[0].strip()[:100]
                promotion_title = first_line if first_line else "Tire Promotion"
            elif alt_text and len(alt_text.strip()) > 5:
                promotion_title = alt_text[:100]
            else:
                # Extract first meaningful line from OCR
                lines = [l.strip() for l in ocr_text.split("\n") if l.strip() and len(l.strip()) > 5]
                promotion_title = lines[0][:100] if lines else "Tire Promotion"
            
            # Normalize title for deduplication
            normalized_title = normalize_title(promotion_title)
            
            # Skip if we've seen this title before
            if normalized_title in seen_titles:
                logger.info(f"Skipping duplicate title: {promotion_title}")
                img_path.unlink()
                continue
            seen_titles.add(normalized_title)
            
            # Step 11: Build structured promo dict
            if cleaned_data:
                discount_value = cleaned_data.get("discount_value") or extracted_details.get("discount_value")
                coupon_code = cleaned_data.get("coupon_code") or extracted_details.get("coupon_code")
                expiry_date = cleaned_data.get("expiry_date") or extracted_details.get("expiry_date")
                offer_details = cleaned_data.get("promo_description") or ocr_text[:1000]
            else:
                # Fallback: use basic extraction
                discount_value = extracted_details.get("discount_value")
                coupon_code = extracted_details.get("coupon_code")
                expiry_date = extracted_details.get("expiry_date")
                offer_details = ocr_text[:1000]
            
            # Calculate confidence score (based on OCR text length and LLM success)
            confidence_score = 0.7  # Default
            if cleaned_data:
                confidence_score = 0.9
            if len(ocr_text) > 200:
                confidence_score += 0.05
            confidence_score = min(confidence_score, 1.0)
            
            promo = {
                "website": competitor.get("domain", ""),
                "page_url": promo_url,
                "business_name": competitor.get("name", ""),
                "google_reviews": None,
                "service_name": cleaned_data.get("service_name", "tires") if cleaned_data else "tires",
                "promo_description": offer_details,
                "category": "tires",
                "contact": competitor.get("address", ""),
                "location": competitor.get("address", ""),
                "offer_details": offer_details,
                "ad_title": promotion_title,
                "ad_text": alt_text[:200],
                "new_or_updated": "new",
                "date_scraped": datetime.now().isoformat(),
                "image_url": image_url,
                "discount_value": discount_value,
                "coupon_code": coupon_code,
                "expiry_date": expiry_date,
                "image_path": str(img_path),
                "extraction_method": "image_ocr",
                "source": "image_llm",
                "confidence_score": round(confidence_score, 2),
                "promotion_title": promotion_title,
                "ocr_hash": ocr_hash
            }
            
            all_promos.append(promo)
            logger.info(f"✓ Added promo: {promotion_title} - {discount_value or 'N/A'}")
    
    logger.info(f"Total unique promotions found: {len(all_promos)}")
    return all_promos


def scrape_trail(competitor: Dict) -> Dict:
    """Main entry point for Trail Tire scraper."""
    try:
        promos = process_trail_promotions(competitor)
        
        # Save results
        output_file = PROMOTIONS_DIR / f"{competitor.get('name', 'trail').lower().replace(' ', '_')}.json"
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
        logger.error(f"Error scraping Trail Tire: {e}", exc_info=True)
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
    
    # Find Trail Tire
    trail = next((c for c in competitors if "trail" in c.get("name", "").lower()), None)
    
    if not trail:
        logger.error("Trail Tire Auto Centres not found in competitor list")
        sys.exit(1)
    
    result = scrape_trail(trail)
    print(f"\n✅ Scraping complete!")
    print(f"   Found {result.get('count', 0)} promotions")
    print(f"   Saved to: data/promotions/")
    print(f"\n📊 Summary:")
    for promo in result.get("promotions", []):
        print(f"   • {promo.get('promotion_title', 'N/A')}: {promo.get('discount_value', 'N/A')}")

