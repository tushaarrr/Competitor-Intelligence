"""Valvoline Express Care scraper - Extract promotions from AWeber popup modals."""
import json
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime
import re

from app.extractors.firecrawl.firecrawl_client import fetch_with_firecrawl
from app.extractors.images.image_downloader import download_image, normalize_url
from app.extractors.ocr.ocr_processor import ocr_image
from app.extractors.ocr.llm_cleaner import clean_promo_text_with_llm
from app.config.constants import DATA_DIR, PROMO_KEYWORDS
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__, "valvoline_scraper.log")

DATA_DIR.mkdir(parents=True, exist_ok=True)
PROMOTIONS_DIR = DATA_DIR / "promotions"
PROMOTIONS_DIR.mkdir(parents=True, exist_ok=True)


def detect_aweber_popups(html: str) -> List[Dict]:
    """Detect AWeber popup modals in HTML."""
    from bs4 import BeautifulSoup
    
    soup = BeautifulSoup(html, "html.parser")
    popups = []
    
    # Check for AWeber popup selectors (multiple variations)
    aweber_selectors = [
        "div.af-body",
        "div.af-form",
        "div[class*='af-body']",
        "div[class*='af-form']",
        "div[id*='af-body']",
        "div[id*='af-form']",
        "img[src*='hostedimages']",
        "img[src*='aweber']",
        "img[src*='af-']",
    ]
    
    seen_popups = set()
    
    for selector in aweber_selectors:
        try:
            elements = soup.select(selector)
            for elem in elements:
                # Find the parent popup container
                popup_container = None
                
                # If it's already a form/body div, use it
                if elem.name in ['div']:
                    classes = elem.get('class', [])
                    classes_str = ' '.join(classes) if isinstance(classes, list) else str(classes)
                    if 'af-body' in classes_str or 'af-form' in classes_str:
                        popup_container = elem
                
                if not popup_container:
                    # Find parent div that might be the popup (check multiple levels)
                    for parent_level in range(5):  # Check up to 5 levels up
                        parent = elem.find_parent("div")
                        if not parent:
                            break
                        classes = parent.get('class', [])
                        classes_str = ' '.join(classes) if isinstance(classes, list) else str(classes)
                        parent_id = parent.get('id', '') or ''
                        
                        # Check if parent looks like a popup/modal
                        if any(keyword in (classes_str + parent_id).lower() for keyword in ['af-', 'popup', 'modal', 'overlay', 'lightbox']):
                            popup_container = parent
                            break
                        elem = parent  # Move up for next iteration
                
                # If still no container, use the element itself if it's a div
                if not popup_container and elem.name == 'div':
                    popup_container = elem
                elif not popup_container:
                    # For images, try to find a container
                    popup_container = elem.find_parent("div")
                
                if popup_container:
                    # Create a unique identifier for this popup
                    container_html = str(popup_container)[:200]
                    popup_id = hash(container_html)
                    if popup_id not in seen_popups:
                        seen_popups.add(popup_id)
                        popups.append({
                            "container": popup_container,
                            "selector": selector
                        })
                        logger.info(f"Found AWeber popup using selector: {selector}")
        except Exception as e:
            logger.warning(f"Error searching for selector {selector}: {e}")
            continue
    
    # Also look for any divs containing AWeber-related classes or attributes (more broadly)
    if not popups:
        # Check all divs for AWeber-related content
        all_divs = soup.find_all("div")
        for div in all_divs:
            classes = div.get('class', [])
            classes_str = ' '.join(classes) if isinstance(classes, list) else str(classes)
            div_id = div.get('id', '') or ''
            div_attrs = ' '.join([str(v) for v in div.attrs.values()])
            
            # Check if any AWeber-related keyword exists
            content_str = (classes_str + ' ' + div_id + ' ' + div_attrs).lower()
            if any(keyword in content_str for keyword in ['af-', 'aweber', 'hostedimages']):
                # Also check if it contains images
                images_in_div = div.find_all('img')
                if images_in_div:
                    container_html = str(div)[:200]
                    popup_id = hash(container_html)
                    if popup_id not in seen_popups:
                        seen_popups.add(popup_id)
                        popups.append({
                            "container": div,
                            "selector": "aweber-div-with-images"
                        })
                        logger.info(f"Found AWeber-related div with images: {len(images_in_div)} images")
    
    # Also check for images directly with AWeber URLs (they might not be in a popup container)
    if not popups:
        all_images = soup.find_all("img")
        aweber_images = []
        for img in all_images:
            src = img.get('src') or img.get('data-src') or ''
            if any(keyword in src.lower() for keyword in ['hostedimages', 'aweber', 'af-']):
                # Create a virtual container for this image
                parent = img.find_parent("div") or img.find_parent()
                if parent:
                    container_html = str(parent)[:200]
                    popup_id = hash(container_html)
                    if popup_id not in seen_popups:
                        seen_popups.add(popup_id)
                        popups.append({
                            "container": parent,
                            "selector": "aweber-image-parent"
                        })
                        logger.info(f"Found AWeber image in container")
                        aweber_images.append(img)
    
    logger.info(f"Found {len(popups)} AWeber popup(s)")
    return popups


def extract_images_from_popup(popup_container, base_url: str) -> List[str]:
    """Extract all image URLs from a popup container."""
    image_urls = []
    seen_urls = set()
    
    # Find all img tags in the popup
    images = popup_container.find_all("img")
    
    for img in images:
        # Check multiple attributes for image source
        for attr in ['src', 'data-src', 'data-lazy-src', 'data-original', 'data-url']:
            src = img.get(attr)
            if src:
                # Skip data URIs and placeholders
                if src.startswith('data:'):
                    continue
                
                img_url = normalize_url(base_url, src)
                
                # Only include PNG, JPG, WebP images
                if any(ext in img_url.lower() for ext in ['.png', '.jpg', '.jpeg', '.webp']) or \
                   'hostedimages' in img_url.lower() or \
                   'aweber' in img_url.lower():
                    if img_url not in seen_urls:
                        seen_urls.add(img_url)
                        image_urls.append(img_url)
                        logger.info(f"Found image in popup: {img_url[:100]}")
    
    # Also check srcset attribute
    for img in images:
        srcset = img.get('srcset')
        if srcset:
            # Parse srcset (format: "url1 size1, url2 size2")
            for src_entry in srcset.split(','):
                src = src_entry.strip().split()[0] if src_entry.strip() else None
                if src and not src.startswith('data:'):
                    img_url = normalize_url(base_url, src)
                    if any(ext in img_url.lower() for ext in ['.png', '.jpg', '.jpeg', '.webp']):
                        if img_url not in seen_urls:
                            seen_urls.add(img_url)
                            image_urls.append(img_url)
    
    logger.info(f"Extracted {len(image_urls)} images from popup")
    return image_urls


def detect_promo_keywords(text: str, keywords: List[str]) -> bool:
    """Check if text contains promo keywords."""
    if not text:
        return False
    
    text_lower = text.lower()
    
    # Check for promo keywords
    for keyword in keywords:
        if keyword.lower() in text_lower:
            return True
    
    # Also check for common promo patterns
    promo_patterns = [
        r'\$(\d+)',  # Dollar amounts
        r'(\d+)\s*%',  # Percentages
        r'\b(off|save|discount|coupon|special|promo|offer|deal)\b',
        r'\b(winter|oil change|service)\b',
    ]
    
    for pattern in promo_patterns:
        if re.search(pattern, text_lower):
            return True
    
    return False


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
    code_patterns = [
        r'(?:code|coupon|promo)[:\s]+([A-Z0-9]{3,20})',
        r'use[:\s]+([A-Z0-9]{3,20})',
    ]
    
    for pattern in code_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    
    return None


def extract_expiry_date(text: str) -> Optional[str]:
    """Extract expiry date from text."""
    date_patterns = [
        r'(?:expires?|valid until|until|ends?)[:\s]+([A-Za-z]+\s+\d{1,2}[,\s]+\d{4})',
        r'(?:expires?|valid until|until|ends?)[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',
    ]
    
    for pattern in date_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    
    return None


def map_service_category(text: str) -> str:
    """Map text to service category."""
    text_lower = text.lower()
    
    if any(word in text_lower for word in ['oil', 'oil change', 'lube']):
        return "oil change"
    
    return "other"


def process_valvoline_promotions(competitor: Dict) -> List[Dict]:
    """Process Valvoline Express Care promotions from AWeber popups."""
    logger.info(f"Processing promotions for {competitor.get('name')}")
    
    promo_links = competitor.get("promo_links", [])
    if not promo_links:
        logger.warning(f"No promo_links found for {competitor.get('name')}")
        return []
    
    all_promos = []
    seen_image_urls = set()
    
    # Use Playwright to handle JavaScript-loaded popups
    from playwright.sync_api import sync_playwright
    
    for promo_url in promo_links:
        logger.info(f"Fetching {promo_url} with Playwright to detect dynamic popups")
        
        # Use Firecrawl as primary method (works better for this site)
        logger.info(f"Fetching {promo_url} with Firecrawl")
        firecrawl_result = fetch_with_firecrawl(promo_url, timeout=90)
        
        if firecrawl_result.get("error"):
            logger.warning(f"Firecrawl returned error: {firecrawl_result.get('error')}, trying Playwright...")
            # Fallback to Playwright for dynamic content
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    context = browser.new_context(
                        viewport={"width": 1920, "height": 1080},
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        extra_http_headers={
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                            "Accept-Language": "en-US,en;q=0.5",
                            "Accept-Encoding": "gzip, deflate",
                            "Connection": "keep-alive",
                            "Upgrade-Insecure-Requests": "1"
                        }
                    )
                    page = context.new_page()
                    
                    logger.info(f"Loading page with Playwright: {promo_url}")
                    response = page.goto(promo_url, wait_until="networkidle", timeout=60000)
                    
                    # Wait for page to fully load and potential popups to appear
                    page.wait_for_timeout(5000)  # Wait 5 seconds for popups to load
                    
                    # Try to wait for popup elements to appear (if they exist)
                    try:
                        page.wait_for_selector("div.af-body, div.af-form, img[src*='hostedimages'], img[src*='aweber']", timeout=5000)
                    except:
                        pass  # Popup might not appear - continue anyway
                    
                    # Scroll to trigger lazy-loaded content
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(2000)
                    page.evaluate("window.scrollTo(0, 0)")
                    page.wait_for_timeout(1000)
                    
                    # Get HTML after JavaScript execution
                    html = page.content()
                    browser.close()
            except Exception as e:
                logger.error(f"Both Firecrawl and Playwright failed: {e}")
                continue
        else:
            html = firecrawl_result.get("html", "")
            if not html:
                logger.error(f"No HTML content from Firecrawl for {promo_url}")
                continue
        
        # Detect AWeber popups
        popups = detect_aweber_popups(html)
        
        if not popups:
            logger.warning(f"No AWeber popups found on {promo_url}")
            continue
        
        # Process each popup
        for popup in popups:
            popup_container = popup["container"]
            
            # Extract images from popup
            image_urls = extract_images_from_popup(popup_container, promo_url)
            
            if not image_urls:
                logger.info("No images found in popup")
                continue
            
            # Process each image
            for img_url in image_urls:
                # Skip if already processed
                if img_url in seen_image_urls:
                    logger.info(f"Skipping duplicate image: {img_url[:100]}")
                    continue
                
                seen_image_urls.add(img_url)
                
                logger.info(f"Processing image: {img_url[:100]}")
                
                try:
                    # Download image
                    img_path = download_image(img_url)
                    if not img_path:
                        logger.warning(f"Failed to download image: {img_url}")
                        continue
                    
                    # Run OCR
                    ocr_text = ocr_image(img_path)
                    
                    # Clean up downloaded image
                    try:
                        img_path.unlink()
                    except:
                        pass
                    
                    if not ocr_text or len(ocr_text.strip()) < 10:
                        logger.warning(f"OCR returned empty or too short text for {img_url}")
                        continue
                    
                    # Check if OCR text contains promo keywords
                    if not detect_promo_keywords(ocr_text, PROMO_KEYWORDS):
                        logger.info(f"OCR text doesn't contain promo keywords, skipping: {ocr_text[:100]}")
                        continue
                    
                    logger.info(f"OCR text (first 200 chars): {ocr_text[:200]}")
                    
                    # Clean with LLM
                    context = f"Valvoline Express Care promotion image OCR text. Image URL: {img_url}"
                    cleaned_data = clean_promo_text_with_llm(ocr_text, context)
                    
                    # Extract basic details from OCR
                    discount_value = extract_discount_value(ocr_text)
                    coupon_code = extract_coupon_code(ocr_text)
                    expiry_date = extract_expiry_date(ocr_text)
                    service_category = map_service_category(ocr_text)
                    
                    # Build promotion using LLM cleaned data if available
                    if cleaned_data and cleaned_data.get("service_name"):
                        promotion_title = cleaned_data.get("service_name")
                    elif cleaned_data and cleaned_data.get("promo_description"):
                        first_line = cleaned_data.get("promo_description", "").split("\n")[0].strip()[:100]
                        promotion_title = first_line if first_line else ocr_text.split("\n")[0][:100]
                    else:
                        # Fallback: Extract first meaningful line from OCR
                        lines = [l.strip() for l in ocr_text.split("\n") if l.strip() and len(l.strip()) > 5]
                        promotion_title = lines[0][:100] if lines else "Valvoline Express Care Promotion"
                    
                    # Use LLM cleaned data if available, otherwise use OCR text
                    if cleaned_data:
                        promo_description = cleaned_data.get("promo_description") or ocr_text[:500]
                        offer_details = cleaned_data.get("promo_description") or ocr_text[:1000]
                        discount_value = cleaned_data.get("discount_value") or discount_value
                        coupon_code = cleaned_data.get("coupon_code") or coupon_code
                        expiry_date = cleaned_data.get("expiry_date") or expiry_date
                        
                        # Override service_category if LLM provides it
                        if cleaned_data.get("service_name"):
                            service_category = map_service_category(cleaned_data.get("service_name"))
                    else:
                        # Fallback to OCR text
                        promo_description = ocr_text[:500]
                        offer_details = ocr_text[:1000]
                    
                    # Calculate confidence score
                    confidence = 0.7  # Base confidence
                    if cleaned_data:
                        confidence = 0.9  # Higher if LLM cleaned
                    if discount_value:
                        confidence += 0.05
                    if coupon_code:
                        confidence += 0.05
                    if expiry_date:
                        confidence += 0.05
                    confidence = min(confidence, 1.0)
                    
                    promo = {
                        "website": competitor.get("domain", ""),
                        "page_url": promo_url,
                        "business_name": competitor.get("name", ""),
                        "google_reviews": None,
                        "service_name": cleaned_data.get("service_name", service_category) if cleaned_data else service_category,
                        "promo_description": promo_description,
                        "category": service_category,
                        "contact": competitor.get("address", ""),
                        "location": competitor.get("address", ""),
                        "offer_details": offer_details,
                        "ad_title": promotion_title,
                        "ad_text": ocr_text[:500],
                        "new_or_updated": "new",
                        "date_scraped": datetime.now().isoformat(),
                        "discount_value": discount_value,
                        "coupon_code": coupon_code,
                        "expiry_date": expiry_date,
                        "promotion_title": promotion_title,
                        "image_url": img_url,
                        "service_category": service_category,
                        "source": "image_llm",
                        "confidence": {
                            "overall": confidence,
                            "fields": {
                                "promotion_title": 0.8 if cleaned_data else 0.6,
                                "discount_value": 0.9 if discount_value else 0.0,
                                "coupon_code": 0.9 if coupon_code else 0.0,
                                "expiry_date": 0.8 if expiry_date else 0.0,
                                "service_category": 0.8
                            }
                        },
                        "ocr_text": ocr_text
                    }
                    
                    all_promos.append(promo)
                    logger.info(f"✓ Added promo: {promotion_title[:50]} - {discount_value or 'N/A'}")
                    
                except Exception as e:
                    logger.error(f"Error processing image {img_url}: {e}", exc_info=True)
                    continue
    
    logger.info(f"Total promotions found: {len(all_promos)}")
    return all_promos


def scrape_valvoline(competitor: Dict) -> Dict:
    """Main entry point for Valvoline Express Care scraper."""
    try:
        promos = process_valvoline_promotions(competitor)
        
        # Save results
        output_file = PROMOTIONS_DIR / f"{competitor.get('name', 'valvoline').lower().replace(' ', '_')}.json"
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
        logger.error(f"Error scraping Valvoline Express Care: {e}", exc_info=True)
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
    
    # Find Valvoline Express Care
    valvoline = next((c for c in competitors if "valvoline" in c.get("name", "").lower()), None)
    
    if not valvoline:
        logger.error("Valvoline Express Care not found in competitor list")
        sys.exit(1)
    
    result = scrape_valvoline(valvoline)
    print(f"\n✅ Scraping complete!")
    print(f"   Found {result.get('count', 0)} promotions")
    print(f"   Saved to: data/promotions/")
    print(f"\n📊 Summary:")
    for promo in result.get("promotions", []):
        print(f"   • {promo.get('promotion_title', 'N/A')}: {promo.get('discount_value', 'N/A')}")

