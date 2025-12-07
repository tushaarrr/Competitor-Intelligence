"""Midas scraper - Text-based HTML extraction only."""
import json
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime
import re
from fuzzywuzzy import fuzz

from app.extractors.firecrawl.firecrawl_client import fetch_with_firecrawl
from app.extractors.ocr.llm_cleaner import clean_promo_text_with_llm
from app.config.constants import DATA_DIR, PROMO_KEYWORDS
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__, "midas_scraper.log")

DATA_DIR.mkdir(parents=True, exist_ok=True)
PROMOTIONS_DIR = DATA_DIR / "promotions"
PROMOTIONS_DIR.mkdir(parents=True, exist_ok=True)


def fetch_with_fallback(url: str) -> str:
    """Fetch HTML using Firecrawl (Markdown + HTML mode), fallback to ZenRows/ScraperAPI."""
    # Try Firecrawl first - request both HTML and Markdown
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
            zenrows_url = f"https://api.zenrows.com/v1/?apikey={ZENROWS_API_KEY}&url={url}&js_render=true&wait=2000"
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


def extract_promo_blocks(html: str) -> List[Dict]:
    """Extract promotional text blocks from HTML based on keywords."""
    from bs4 import BeautifulSoup
    
    soup = BeautifulSoup(html, "html.parser")
    
    # Remove script and style elements
    for script in soup(["script", "style", "noscript"]):
        script.decompose()
    
    promo_blocks = []
    seen_texts = set()
    
    # Expanded keywords to identify promo blocks
    promo_keywords = [
        "offer", "save", "discount", "special", "deal", "promotion", "% off", "$",
        "rebate", "coupon", "limited", "sale", "financing", "credit",
        "free", "get", "buy", "oil change", "tire", "brake", "rotation",
        "synthetic", "euro", "lifetime", "warranty", "guarantee",
        "monthly", "featured", "includes", "expires", "valid"
    ]
    
    # Expanded selectors for promo content - more comprehensive
    promo_selectors = [
        # Standard promo selectors
        "div[class*='promo']",
        "div[class*='offer']",
        "div[class*='special']",
        "div[class*='deal']",
        "div[class*='rebate']",
        "div[class*='coupon']",
        "div[class*='discount']",
        "section[class*='promo']",
        "section[class*='offer']",
        "article[class*='promo']",
        ".card[class*='promo']",
        ".offer-box",
        ".promotion-box",
        "[class*='rebate']",
        # Archive/featured sections
        "[class*='featured']",
        "[class*='monthly']",
        "[class*='archive']",
        # Price-related sections
        "[class*='price']",
        "[class*='pricing']",
        "[class*='cost']",
        # Service-related sections
        "[id*='oil']",
        "[id*='tire']",
        "[id*='brake']",
        "[class*='oil']",
        "[class*='tire']",
        "[class*='brake']",
    ]
    
    # First, try to find elements with promo-related classes
    for selector in promo_selectors:
        try:
            elements = soup.select(selector)
            for elem in elements:
                text = elem.get_text(separator=" ", strip=True)
                if text and len(text) > 30:
                    # Check if text contains promo keywords
                    text_lower = text.lower()
                    if any(keyword.lower() in text_lower for keyword in promo_keywords):
                        text_hash = hash(text[:200])
                        if text_hash not in seen_texts:
                            seen_texts.add(text_hash)
                            promo_blocks.append({
                                "text": text,
                                "html": str(elem),
                                "selector": selector
                            })
                            logger.info(f"Found promo block with selector {selector}: {len(text)} chars")
        except Exception as e:
            logger.warning(f"Error with selector {selector}: {e}")
            continue
    
    # Also check headings that might contain promo info
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        text = heading.get_text(strip=True)
        if text:
            text_lower = text.lower()
            if any(keyword.lower() in text_lower for keyword in promo_keywords):
                # Get surrounding content (sibling or parent content)
                parent = heading.find_parent()
                if parent:
                    # Get text from parent container
                    parent_text = parent.get_text(separator=" ", strip=True)
                    if len(parent_text) > 50:  # Ensure meaningful content
                        text_hash = hash(parent_text[:200])
                        if text_hash not in seen_texts:
                            seen_texts.add(text_hash)
                            promo_blocks.append({
                                "text": parent_text,
                                "html": str(parent),
                                "selector": f"heading-{heading.name}"
                            })
                            logger.info(f"Found promo block from heading {heading.name}: {len(parent_text)} chars")
    
    # Check paragraphs and divs that contain promo keywords
    for tag in soup.find_all(["p", "div", "section", "article"]):
        text = tag.get_text(separator=" ", strip=True)
        if text and len(text) > 50:  # Minimum length
            text_lower = text.lower()
            
            # Check for promo keywords
            if any(keyword.lower() in text_lower for keyword in promo_keywords):
                # Skip if it's too long (likely not a specific promo)
                if len(text) > 2000:
                    continue
                
                # Check if it's not already captured in a parent element
                is_duplicate = False
                for existing in promo_blocks:
                    if text in existing["text"] or existing["text"] in text:
                        is_duplicate = True
                        break
                
                if not is_duplicate:
                    text_hash = hash(text[:200])
                    if text_hash not in seen_texts:
                        seen_texts.add(text_hash)
                        promo_blocks.append({
                            "text": text,
                            "html": str(tag),
                            "selector": tag.name
                        })
                        logger.info(f"Found promo block in {tag.name}: {len(text)} chars")
    
    # Enhanced: Look for structured promo cards/sections with prices
    # These are often in divs with price indicators
    price_indicators = soup.find_all(string=re.compile(r'\$\d+'))
    seen_price_containers = set()
    for price_text in price_indicators:
        parent = price_text.find_parent()
        if parent:
            # Get surrounding context (up to 4 levels up for better coverage)
            promo_container = None
            current_parent = parent
            for level in range(4):
                if current_parent:
                    classes = ' '.join(current_parent.get('class', [])) if current_parent.get('class') else ''
                    id_attr = current_parent.get('id', '') or ''
                    
                    # Look for promo-related containers or headings nearby
                    has_promo_class = any(promo_word in classes.lower() for promo_word in ['promo', 'offer', 'special', 'featured', 'card', 'box', 'archive', 'monthly'])
                    has_heading_nearby = current_parent.find(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']) is not None
                    
                    # Check if this container has multiple price points (indicates structured promo)
                    prices_in_container = len(current_parent.find_all(string=re.compile(r'\$\d+')))
                    
                    if has_promo_class or (has_heading_nearby and prices_in_container >= 1) or prices_in_container >= 2:
                        promo_container = current_parent
                        break
                    current_parent = current_parent.find_parent()
            
            if promo_container:
                container_id = id(promo_container)
                if container_id not in seen_price_containers:
                    seen_price_containers.add(container_id)
                    text = promo_container.get_text(separator=" ", strip=True)
                    if text and len(text) > 30 and len(text) < 5000:  # Increased max length for complex promos
                        text_hash = hash(text[:300])  # Use longer hash for better uniqueness
                        if text_hash not in seen_texts:
                            seen_texts.add(text_hash)
                            promo_blocks.append({
                                "text": text,
                                "html": str(promo_container),
                                "selector": "price-indicator-container"
                            })
                            logger.info(f"Found promo block from price indicator: {len(text)} chars ({prices_in_container} prices)")
    
    # Enhanced: Look for "Buy X Get Y" patterns and large promo headings
    large_headings = soup.find_all(["h1", "h2", "h3"])
    for heading in large_headings:
        heading_text = heading.get_text(strip=True)
        heading_lower = heading_text.lower()
        
        # Check for promo patterns in large headings
        has_buy_get = bool(re.search(r'buy\s+\d+\s+(?:tire|get)', heading_lower, re.IGNORECASE))
        has_free = "free" in heading_lower
        has_price = bool(re.search(r'\$\d+', heading_text))
        has_lifetime = "lifetime" in heading_lower
        has_promo_words = any(word in heading_lower for word in ['oil', 'tire', 'brake', 'change', 'rotation', 'special', 'offer'])
        
        if has_buy_get or (has_price and has_promo_words) or (has_free and has_promo_words) or has_lifetime:
            # Get the entire section containing this heading
            parent = heading.find_parent(["section", "div", "article"])
            if not parent:
                parent = heading.find_parent()
            
            if parent:
                # Get all siblings after the heading
                section_text = heading_text
                for sibling in heading.find_next_siblings():
                    sibling_text = sibling.get_text(separator=" ", strip=True)
                    if sibling_text and len(sibling_text) < 500:  # Don't include huge blocks
                        section_text += " " + sibling_text
                
                # Also get parent text
                parent_text = parent.get_text(separator=" ", strip=True)
                
                # Use the more complete text (but not too long)
                final_text = parent_text if len(parent_text) < 3000 and len(parent_text) > len(section_text) else section_text
                
                if len(final_text) > 50:
                    text_hash = hash(final_text[:300])
                    if text_hash not in seen_texts:
                        seen_texts.add(text_hash)
                        promo_blocks.append({
                            "text": final_text,
                            "html": str(parent),
                            "selector": "large-heading-promo"
                        })
                        logger.info(f"Found promo block from large heading: {heading_text[:50]}... ({len(final_text)} chars)")
    
    # If still no blocks found, try extracting from main content areas
    if len(promo_blocks) < 3:
        logger.info("Few promo blocks found, trying main content areas...")
        main_content = soup.find("main") or soup.find("article") or soup.find("body")
        if main_content:
            text = main_content.get_text(separator=" ", strip=True)
            # Split by sentences or paragraphs
            sentences = re.split(r'(?<=[.!?])\s+|(?<=\n)\s*', text)
            current_block = []
            for sentence in sentences:
                sentence_lower = sentence.lower().strip()
                if len(sentence_lower) < 10:
                    continue
                
                # Check for promo indicators
                keyword_count = sum(1 for keyword in promo_keywords if keyword.lower() in sentence_lower)
                has_price = bool(re.search(r'\$\d+', sentence))
                has_percent = bool(re.search(r'\d+\s*%', sentence))
                
                if keyword_count >= 1 or has_price or has_percent:
                    current_block.append(sentence.strip())
                elif current_block and len(current_block) >= 1:  # Lower threshold
                    # Save block when we find promo content
                    block_text = " ".join(current_block)
                    if len(block_text) > 30:
                        # Check if similar block already exists
                        is_duplicate = False
                        for existing in promo_blocks:
                            if fuzz.ratio(block_text[:100], existing["text"][:100]) > 85:
                                is_duplicate = True
                                break
                        
                        if not is_duplicate:
                            promo_blocks.append({
                                "text": block_text,
                                "html": "",
                                "selector": "main-content-split"
                            })
                            logger.info(f"Found promo block from main content: {len(block_text)} chars")
                    current_block = []
    
    # Remove very similar duplicates
    unique_blocks = []
    for block in promo_blocks:
        is_duplicate = False
        for existing in unique_blocks:
            similarity = fuzz.ratio(block["text"][:200], existing["text"][:200])
            if similarity > 85:
                is_duplicate = True
                break
        if not is_duplicate:
            unique_blocks.append(block)
    
    logger.info(f"Extracted {len(unique_blocks)} unique promo blocks")
    return unique_blocks


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
        r'code[:\s]*([A-Z0-9]{4,15})',
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
        r'(?:expires?|valid)[:\s]*(\d{1,2}\s+[A-Za-z]+\s+\d{4})',
    ]
    
    for pattern in date_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    
    return None


def map_service_category(text: str) -> str:
    """Map text to service category."""
    text_lower = text.lower()
    
    service_keywords = {
        "oil change": ["oil", "lube", "oil change"],
        "brakes": ["brake", "brakes", "brake pad", "brake service"],
        "tires": ["tire", "tires", "wheel", "wheels", "alignment"],
        "battery": ["battery", "batteries"],
        "exhaust": ["exhaust", "muffler"],
        "transmission": ["transmission", "trans"],
        "cooling": ["coolant", "radiator", "cooling system"],
        "filters": ["filter", "filters", "air filter"],
    }
    
    for category, keywords in service_keywords.items():
        if any(keyword in text_lower for keyword in keywords):
            return category
    
    return "other"


def process_midas_promotions(competitor: Dict) -> List[Dict]:
    """Process Midas promotions using text-based HTML extraction."""
    logger.info(f"Processing promotions for {competitor.get('name')}")
    
    promo_links = competitor.get("promo_links", [])
    if not promo_links:
        logger.warning(f"No promo_links found for {competitor.get('name')}")
        return []
    
    all_promos = []
    seen_promos = set()
    
    for promo_url in promo_links:
        logger.info(f"Fetching {promo_url}")
        
        # Step 1: Fetch HTML with fallback
        html = fetch_with_fallback(promo_url)
        
        if not html:
            logger.error(f"Failed to fetch HTML from {promo_url}")
            continue
        
        # Step 2: Extract promo blocks
        promo_blocks = extract_promo_blocks(html)
        
        if not promo_blocks:
            logger.warning(f"No promo blocks found on {promo_url}")
            continue
        
        # Step 3: Process each promo block with LLM
        for block in promo_blocks:
            text = block["text"]
            
            # Skip if too short or likely not a promo
            if len(text) < 30:
                continue
            
            logger.info(f"Processing promo block: {len(text)} chars")
            
            try:
                # Send to LLM for cleaning and structuring
                context = f"Midas promotion from {promo_url}. Block selector: {block.get('selector', 'unknown')}"
                cleaned_data = clean_promo_text_with_llm(text, context)
                
                # Extract basic details from text
                discount_value = extract_discount_value(text)
                coupon_code = extract_coupon_code(text)
                expiry_date = extract_expiry_date(text)
                service_category = map_service_category(text)
                
                # Build promotion using LLM cleaned data if available
                if cleaned_data:
                    promotion_title = cleaned_data.get("service_name") or cleaned_data.get("promo_description", "").split("\n")[0].strip()[:100]
                    if not promotion_title:
                        # Fallback: Extract first meaningful line
                        lines = [l.strip() for l in text.split("\n") if l.strip() and len(l.strip()) > 5]
                        promotion_title = lines[0][:100] if lines else "Midas Promotion"
                    
                    promo_description = cleaned_data.get("promo_description") or text[:500]
                    offer_details = cleaned_data.get("promo_description") or text[:1000]
                    discount_value = cleaned_data.get("discount_value") or discount_value
                    coupon_code = cleaned_data.get("coupon_code") or coupon_code
                    expiry_date = cleaned_data.get("expiry_date") or expiry_date
                    
                    # Override service_category if LLM provides it
                    if cleaned_data.get("service_name"):
                        service_category = map_service_category(cleaned_data.get("service_name"))
                else:
                    # Fallback to direct text extraction
                    lines = [l.strip() for l in text.split("\n") if l.strip() and len(l.strip()) > 5]
                    promotion_title = lines[0][:100] if lines else "Midas Promotion"
                    promo_description = text[:500]
                    offer_details = text[:1000]
                
                # Create promo hash for deduplication
                promo_hash = hash(
                    (promotion_title[:100] + str(discount_value) + str(coupon_code)).lower()
                )
                if promo_hash in seen_promos:
                    logger.info(f"Skipping duplicate promo: {promotion_title[:50]}")
                    continue
                seen_promos.add(promo_hash)
                
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
                    "ad_text": text[:500],
                    "new_or_updated": "new",
                    "date_scraped": datetime.now().isoformat(),
                    "discount_value": discount_value,
                    "coupon_code": coupon_code,
                    "expiry_date": expiry_date,
                    "promotion_title": promotion_title,
                    "image_url": None,  # No images for Midas
                    "service_category": service_category,
                    "source": "midas_html",
                    "confidence": {
                        "overall": confidence,
                        "fields": {
                            "promotion_title": 0.8 if cleaned_data else 0.6,
                            "discount_value": 0.9 if discount_value else 0.0,
                            "coupon_code": 0.9 if coupon_code else 0.0,
                            "expiry_date": 0.8 if expiry_date else 0.0,
                            "service_category": 0.8
                        }
                    }
                }
                
                all_promos.append(promo)
                logger.info(f"✓ Added promo: {promotion_title[:50]} - {discount_value or 'N/A'}")
                
            except Exception as e:
                logger.error(f"Error processing promo block: {e}", exc_info=True)
                continue
    
    logger.info(f"Total promotions found: {len(all_promos)}")
    return all_promos


def scrape_midas(competitor: Dict) -> Dict:
    """Main entry point for Midas scraper."""
    try:
        promos = process_midas_promotions(competitor)
        
        # Save results
        output_file = PROMOTIONS_DIR / f"{competitor.get('name', 'midas').lower().replace(' ', '_')}.json"
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
        logger.error(f"Error scraping Midas: {e}", exc_info=True)
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
    
    # Find Midas
    midas = next((c for c in competitors if "midas" in c.get("name", "").lower()), None)
    
    if not midas:
        logger.error("Midas not found in competitor list")
        sys.exit(1)
    
    result = scrape_midas(midas)
    print(f"\n✅ Scraping complete!")
    print(f"   Found {result.get('count', 0)} promotions")
    print(f"   Saved to: data/promotions/")
    print(f"\n📊 Summary:")
    for promo in result.get("promotions", []):
        print(f"   • {promo.get('promotion_title', 'N/A')}: {promo.get('discount_value', 'N/A')}")

