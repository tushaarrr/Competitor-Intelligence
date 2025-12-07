"""Good News Auto scraper - Extract from "What's happening?" section."""
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

logger = setup_logger(__name__, "goodnews_scraper.log")

DATA_DIR.mkdir(parents=True, exist_ok=True)
PROMOTIONS_DIR = DATA_DIR / "promotions"
PROMOTIONS_DIR.mkdir(parents=True, exist_ok=True)


def fetch_with_fallback(url: str) -> str:
    """Fetch HTML using Firecrawl, fallback to ZenRows/ScraperAPI/BeautifulSoup."""
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
    
    # Final fallback: Direct HTTP request with BeautifulSoup
    try:
        import requests
        response = requests.get(url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        response.raise_for_status()
        logger.info("Successfully fetched with direct HTTP")
        return response.text
    except Exception as e:
        logger.error(f"All fetch methods failed: {e}")
    
    return ""


def find_whats_happening_section(html: str) -> Optional[str]:
    """Find the 'What's happening?' section and extract all text."""
    from bs4 import BeautifulSoup
    
    soup = BeautifulSoup(html, "html.parser")
    
    # Find heading with case-insensitive match - prioritize headings first
    heading = None
    
    # Multiple variations to search for (more robust)
    heading_patterns = [
        r"what'?s\s+happening",
        r"whats\s+happening",
        r"what\s+is\s+happening",
        r"what's\s+new",
        r"whats\s+new",
    ]
    
    # First try to find in actual heading tags
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        text = tag.get_text(strip=True)
        if text:
            text_lower = text.lower()
            # Check against all patterns
            for pattern in heading_patterns:
                if re.search(pattern, text_lower):
                    heading = tag
                    logger.info(f"Found 'What's happening?' heading: {text[:50]}")
                    break
            if heading:
                break
    
    # If not found in headings, try other tags
    if not heading:
        for tag in soup.find_all(["p", "div", "span", "strong", "b", "h3", "h4"]):
            text = tag.get_text(strip=True)
            if text:
                text_lower = text.lower()
                # Check against all patterns
                for pattern in heading_patterns:
                    if re.search(pattern, text_lower):
                        # Check if it's likely a heading (short text, possibly bold)
                        if len(text.split()) < 15:  # More lenient
                            heading = tag
                            logger.info(f"Found 'What's happening?' in {tag.name}: {text[:50]}")
                            break
                if heading:
                    break
    
    if not heading:
        logger.warning("Could not find 'What's happening?' heading")
        return None
    
    # Method 1: Find next elements after heading
    promo_texts = []
    
    # Method 1a: Get next sibling elements (more comprehensive search)
    count = 0
    for sibling in heading.find_next_siblings():
        from bs4 import Tag
        if isinstance(sibling, Tag):
            # Skip script, style, and navigation elements
            if sibling.name in ['script', 'style', 'nav', 'footer', 'header']:
                continue
            text = sibling.get_text(separator=" ", strip=True)
            if text and len(text) > 30:
                # Skip if it's clearly navigation/footer content
                text_lower = text.lower()
                nav_keywords = ['copyright', 'web design', 'terms of service', 'privacy policy']
                if any(keyword in text_lower for keyword in nav_keywords) and len(text) < 100:
                    continue
                promo_texts.append(text)
                count += 1
                if count >= 20:  # Increased limit to capture more content
                    break
    
    if promo_texts:
        logger.info(f"Found {len(promo_texts)} text chunks from next siblings: {sum(len(t) for t in promo_texts)} total chars")
    
    # Method 2: Find parent's children
    parent = heading.find_parent()
    if parent:
        # Get all children after the heading
        found_heading = False
        for child in parent.children:
            from bs4 import NavigableString, Tag
            if isinstance(child, Tag):
                # Check if this is the heading element
                if child == heading:
                    found_heading = True
                    continue
                
                if found_heading:
                    text = child.get_text(separator=" ", strip=True)
                    if text and len(text) > 30:
                        promo_texts.append(text)
    
    # Method 3: Find parent section and extract all text (more comprehensive)
    # Try multiple parent levels
    for parent_tag in ["section", "div", "article", "main"]:
        section = heading.find_parent(parent_tag)
        if section:
            # Get all text from section, but exclude the heading itself
            section_text = section.get_text(separator=" ", strip=True)
            # Remove the heading text from the start
            heading_text = heading.get_text(strip=True)
            # Try multiple ways to remove heading text
            if section_text.lower().startswith(heading_text.lower()):
                section_text = section_text[len(heading_text):].strip()
            elif heading_text in section_text:
                # Remove first occurrence
                section_text = section_text.replace(heading_text, "", 1).strip()
            
            if section_text and len(section_text) > 50:  # Increased minimum
                promo_texts.append(section_text)
                logger.info(f"Found text from parent {parent_tag}: {len(section_text)} chars")
                break  # Use first good match
    
    # Method 4: Get all text after the heading in the document order
    all_text = []
    found_heading_in_tree = False
    from bs4 import NavigableString
    
    for elem in soup.find_all(string=True):
        if not isinstance(elem, NavigableString):
            continue
            
        parent_elem = elem.find_parent()
        if not parent_elem:
            continue
        
        # Check if we've passed the heading
        if parent_elem == heading or heading in parent_elem.parents if hasattr(parent_elem, 'parents') else False:
            found_heading_in_tree = True
            continue
        
        if found_heading_in_tree and parent_elem.name not in ['script', 'style']:
            text = elem.strip()
            if text:
                all_text.append(text)
    
    if all_text:
        combined_text = " ".join(all_text)
        if len(combined_text) > 30:
            promo_texts.append(combined_text)
    
    # Combine all collected texts
    if promo_texts:
        # Deduplicate similar chunks (more lenient deduplication to avoid losing content)
        unique_chunks = []
        seen_chunks = set()
        for chunk in promo_texts:
            # Use first 30 words for comparison (was 20) to be more lenient
            chunk_normalized = " ".join(chunk.lower().split()[:30])
            # Also check if chunk is substantially different (not just a subset)
            is_duplicate = False
            for seen in seen_chunks:
                # Check if one is a subset of the other
                if chunk_normalized in seen or seen in chunk_normalized:
                    if abs(len(chunk_normalized) - len(seen)) < 50:  # Similar length = likely duplicate
                        is_duplicate = True
                        break
            
            if not is_duplicate:
                seen_chunks.add(chunk_normalized)
                unique_chunks.append(chunk)
        
        if unique_chunks:
            combined = "\n\n".join(unique_chunks)
            logger.info(f"Extracted {len(unique_chunks)} unique text chunks from 'What's happening?' section ({len(combined)} total chars)")
            return combined
        else:
            # All chunks were duplicates - use first one
            logger.warning("All chunks were duplicates, using first chunk")
            return promo_texts[0] if promo_texts else None
    
    logger.warning("No promo texts extracted from 'What's happening?' section")
    return None


def chunk_text_into_promos(text: str, min_chars: int = 30) -> List[str]:
    """Chunk text into promo units (minimum characters per chunk)."""
    if not text:
        return []
    
    # Split by patterns that indicate new promotions
    # Look for specific promo patterns first
    promo_keywords = [
        r'(fall/winter|winter|fall)\s+[A-Z][a-z]+\s+(?:Sale|Special|Promo|Offer|Deal|Repair|Inspection)',
        r'(?:Sale|Special|Promo|Offer|Deal)\s+\$\d+',
        r'^\$?\d+\+?\s+',  # Starts with price
        r'[A-Z][a-z]+\s+[A-Z][a-z]+\s+(?:Sale|Special|Promo)',
    ]
    
    # Try to find natural breaks in the text
    # Look for patterns like "fall/winter X" or "$X" at sentence starts
    split_points = []
    
    # Find all potential split points
    for i, char in enumerate(text):
        # Look for patterns like "fall/winter X" or price patterns
        if i > 0:
            # Check for "fall/winter" or "winter" followed by capital letter
            if text[max(0, i-10):i].lower().endswith(('fall/winter ', 'winter ', 'fall ')):
                if i < len(text) and text[i].isupper():
                    split_points.append(i)
            # Check for price patterns at start of potential sentences
            if text[i] == '$' and (i == 0 or text[i-1] in ' .\n'):
                split_points.append(i)
    
    # Split text at identified points
    chunks = []
    if split_points:
        split_points = sorted(set(split_points))
        start = 0
        for point in split_points:
            if point > start:
                chunk = text[start:point].strip()
                if chunk and len(chunk) >= min_chars:
                    chunks.append(chunk)
                start = point
        # Add remaining text
        if start < len(text):
            chunk = text[start:].strip()
            if chunk and len(chunk) >= min_chars:
                chunks.append(chunk)
    
    # If no split points found or too few chunks, try regex splitting
    if len(chunks) < 7:
        # Split by common promo delimiters
        promo_patterns = [
            r'(?=\bfall/winter\s+[A-Z])',  # "fall/winter" followed by capital
            r'(?=\bwinter\s+[A-Z][a-z]+\s+(?:Sale|Tire))',  # "winter" followed by promo type
            r'(?=\$?\d+\+?\s+[A-Z])',  # Price followed by capital (new promo start)
            r'(?=\b[A-Z][a-z]+\s+[A-Z][a-z]+\s+(?:Sale|Special))',  # "Word Word Sale/Special"
        ]
        
        temp_chunks = [text]
        for pattern in promo_patterns:
            new_chunks = []
            for chunk in temp_chunks:
                split_chunks = re.split(pattern, chunk)
                new_chunks.extend([c.strip() for c in split_chunks if c.strip()])
            temp_chunks = new_chunks
            if len(temp_chunks) >= 7:
                break
        chunks = temp_chunks
    
    # Filter and clean chunks
    valid_chunks = []
    for chunk in chunks:
        chunk = chunk.strip()
        if chunk and len(chunk) >= min_chars:
            # Remove extra whitespace
            chunk = " ".join(chunk.split())
            valid_chunks.append(chunk)
    
    # If chunks are too small, try splitting by sentences or paragraphs
    if len(valid_chunks) < 7:
        # First try splitting by paragraphs (double newlines)
        paragraphs = [p.strip() for p in text.split('\n\n') if p.strip() and len(p.strip()) >= min_chars]
        if len(paragraphs) >= 7:
            valid_chunks = paragraphs[:10]  # Limit to 10
        else:
            # Split by sentences
            sentences = re.split(r'[.!?]\s+', text)
            valid_chunks = []
            current_chunk = ""
            for sentence in sentences:
                sentence = sentence.strip()
                if sentence and len(sentence) > 10:  # Skip very short sentences
                    if len(current_chunk) + len(sentence) < 200:  # Build chunks up to 200 chars
                        current_chunk += " " + sentence if current_chunk else sentence
                    else:
                        if len(current_chunk) >= min_chars:
                            valid_chunks.append(current_chunk)
                        current_chunk = sentence
            
            if current_chunk and len(current_chunk) >= min_chars:
                valid_chunks.append(current_chunk)
    
    # If still fewer than 7 chunks, try more aggressive splitting
    if len(valid_chunks) < 7 and len(text) > 300:
        # Try splitting by sentences and grouping intelligently
        sentences = re.split(r'[.!?]\s+', text)
        valid_chunks = []
        current_chunk = ""
        min_chunk_size = 50  # Minimum chunk size
        
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence or len(sentence) < 10:
                continue
            
            # Check if sentence starts a new promo (has price, "Sale", "Special", etc.)
            is_new_promo = bool(re.search(r'(?:^|\s)(\$?\d+|Sale|Special|Promo|Offer|free)', sentence, re.IGNORECASE))
            
            if is_new_promo and current_chunk and len(current_chunk) >= min_chunk_size:
                valid_chunks.append(current_chunk)
                current_chunk = sentence
            else:
                if current_chunk:
                    current_chunk += ". " + sentence
                else:
                    current_chunk = sentence
        
        if current_chunk and len(current_chunk) >= min_chars:
            valid_chunks.append(current_chunk)
    
    # Final fallback: split into equal parts if still too few
    if len(valid_chunks) < 7 and len(text) > 400:
        target_chunks = 7
        chunk_size = len(text) // target_chunks
        valid_chunks = []
        for i in range(target_chunks):
            start = i * chunk_size
            end = start + chunk_size if i < target_chunks - 1 else len(text)
            chunk = text[start:end].strip()
            if chunk and len(chunk) >= min_chars:
                valid_chunks.append(chunk)
    
    logger.info(f"Created {len(valid_chunks)} text chunks")
    return valid_chunks


def calculate_title_word_overlap(title1: str, title2: str) -> float:
    """Calculate word overlap percentage between two titles."""
    if not title1 or not title2:
        return 0.0
    
    words1 = set(title1.lower().split())
    words2 = set(title2.lower().split())
    
    if not words1 or not words2:
        return 0.0
    
    # Remove common words
    common_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by'}
    words1 = {w for w in words1 if w not in common_words and len(w) > 2}
    words2 = {w for w in words2 if w not in common_words and len(w) > 2}
    
    if not words1 or not words2:
        return 0.0
    
    intersection = words1.intersection(words2)
    union = words1.union(words2)
    
    if not union:
        return 0.0
    
    return (len(intersection) / len(union)) * 100


def are_promos_duplicate(promo1: Dict, promo2: Dict) -> bool:
    """Check if two promotions are duplicates based on 70%+ title word overlap."""
    title1 = promo1.get("promotion_title", "")
    title2 = promo2.get("promotion_title", "")
    
    if not title1 or not title2:
        return False
    
    overlap = calculate_title_word_overlap(title1, title2)
    
    if overlap >= 70:
        logger.info(f"Found duplicate: {title1[:50]} and {title2[:50]} ({overlap:.1f}% overlap)")
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


def process_goodnews_promotions(competitor: Dict) -> List[Dict]:
    """Process Good News Auto promotions from 'What's happening?' section."""
    logger.info(f"Processing promotions for {competitor.get('name')}")
    
    promo_links = competitor.get("promo_links", [])
    if not promo_links:
        logger.warning(f"No promo_links found for {competitor.get('name')}")
        return []
    
    all_promos = []
    processed_chunks = set()  # Track processed chunks to prevent duplicates
    
    for promo_url in promo_links:
        logger.info(f"Fetching {promo_url}")
        
        # Fetch HTML with fallback
        html = fetch_with_fallback(promo_url)
        
        if not html:
            logger.error(f"Failed to fetch HTML from {promo_url}")
            continue
        
        # Find "What's happening?" section
        whats_happening_text = find_whats_happening_section(html)
        
        if not whats_happening_text:
            logger.warning("Could not extract 'What's happening?' section text")
            continue
        
        logger.info(f"Extracted {len(whats_happening_text)} characters from 'What's happening?' section")
        
        # Chunk text into promo units
        text_chunks = chunk_text_into_promos(whats_happening_text, min_chars=30)
        
        # Merge incomplete chunks (very short chunks that likely continue in next chunk)
        merged_chunks = []
        i = 0
        while i < len(text_chunks):
            chunk = text_chunks[i]
            # If chunk is very short (< 50 chars) and next chunk exists, try to merge
            if len(chunk) < 50 and i + 1 < len(text_chunks):
                next_chunk = text_chunks[i + 1]
                chunk_lower = chunk.lower()
                next_lower = next_chunk.lower()
                
                # Check if chunk ends with promo keywords (Special, Sale, Inspections, etc.) and next starts with price
                ends_with_promo = bool(re.search(r'(special|sale|promo|offer|deal|repair|inspection|inspections)\s*$', chunk_lower))
                starts_with_price = bool(re.match(r'^\s*\$?\d+', next_chunk))
                
                # Also merge if chunk is very short (< 40 chars) and ends with promo-related words
                is_short_promo_header = len(chunk) < 40 and bool(re.search(r'(inspection|repair|sale|special)$', chunk_lower))
                
                # Also check if chunk + next would form a complete promo
                # (e.g., "Brake Repair Special" + "$79 PREMIUM BRAKE PAD" = complete promo)
                # Also merge short promo headers with price-continued chunks
                if (ends_with_promo and starts_with_price) or (is_short_promo_header and starts_with_price):
                    merged = chunk + " " + next_chunk
                    merged_chunks.append(merged)
                    i += 2  # Skip next chunk since we merged it
                    logger.info(f"Merged incomplete promo with price: {chunk[:30]}... + {next_chunk[:30]}...")
                    continue
                
                # Check if next chunk doesn't start with new promo keyword
                next_starts_promo = bool(re.match(r'^\s*(fall/winter|winter|fall)', next_lower))
                if not next_starts_promo and not starts_with_price:
                    merged = chunk + " " + next_chunk
                    merged_chunks.append(merged)
                    i += 2
                    logger.info(f"Merged incomplete chunk with next: {chunk[:30]}... + {next_chunk[:30]}...")
                    continue
            
            merged_chunks.append(chunk)
            i += 1
        
        text_chunks = merged_chunks if merged_chunks else text_chunks
        
        # Split very long chunks that might contain multiple promotions
        final_chunks = []
        for chunk in text_chunks:
            if len(chunk) > 200:
                # Try to split long chunks by sentence or promo patterns
                # Look for patterns like "Ready to Experience" or other promo starts
                splits = re.split(r'(?=\bReady to)|(?=^\$?\d+\s+[A-Z])|(?=\b[A-Z][a-z]+\s+[A-Z][a-z]+\s+(?:Top|Experience|Ready))', chunk)
                if len(splits) > 1:
                    final_chunks.extend([s.strip() for s in splits if s.strip() and len(s.strip()) >= 30])
                    logger.info(f"Split long chunk ({len(chunk)} chars) into {len(splits)} parts")
                else:
                    final_chunks.append(chunk)
            else:
                final_chunks.append(chunk)
        
        text_chunks = final_chunks
        
        # Always use the chunks we have - don't force 7 promotions
        # The page might have more or fewer promotions, and that's okay
        logger.info(f"Final chunk count: {len(text_chunks)} chunks to process")
        
        # Only fall back to full text if we have very few chunks (< 3) and substantial text exists
        if len(text_chunks) < 3 and len(whats_happening_text) > 200:
            logger.warning(f"Very few chunks found ({len(text_chunks)}), but substantial text exists. Using chunks as-is.")
            # Don't replace - use what we have
        
        # Log the expected vs actual count for monitoring
        if len(text_chunks) != 7:
            logger.info(f"Note: Found {len(text_chunks)} chunks (previously expected 7). Page content may have changed.")
        
        # Validate we have content to process
        if not text_chunks:
            logger.error("No text chunks extracted - this indicates the page structure may have changed significantly")
            # Fallback: try to process full section text
            if whats_happening_text and len(whats_happening_text) > 50:
                logger.warning("Falling back to processing full section text as single chunk")
                text_chunks = [whats_happening_text]
        
        # Process each chunk
        for i, chunk in enumerate(text_chunks):
            # Skip chunks that look like navigation/footer content (but allow if they're substantial and contain promo keywords)
            chunk_lower = chunk.lower()
            
            # More specific skip patterns - only skip if clearly navigation/footer
            skip_patterns = [
                r'follow us|our company|about us|contact us|book appointment|testimonials|donate',
                r'copyright|web design|good news auto ®',
                r'^standard maintenance|^engine services|^suspension services|^brake services|^exhaust services|^fleet repair|^air conditioner repair$',
            ]
            
            is_nav_content = any(re.search(pattern, chunk_lower) for pattern in skip_patterns)
            has_promo_keywords = bool(re.search(r'(sale|special|promo|offer|deal|\$\d+|free|rebate|starting at|inspection)', chunk_lower))
            
            # Skip only if it's clearly navigation content AND doesn't have promo keywords or prices
            # But don't skip if it has fall/winter, sale, special, or prices
            if is_nav_content and not has_promo_keywords and not re.search(r'(fall/winter|winter|fall)', chunk_lower):
                logger.info(f"Skipping navigation/footer chunk: {chunk[:50]}")
                continue
            
            # Also check if chunk is too short (but allow short chunks if they have prices or promo keywords)
            if len(chunk) < 30 and not has_promo_keywords:
                logger.info(f"Skipping very short chunk without promo keywords: {chunk[:50]}")
                continue
            
            # Skip if already processed (normalize chunk to detect duplicates)
            chunk_normalized = " ".join(chunk.lower().split()[:30])
            if chunk_normalized in processed_chunks:
                logger.info(f"Skipping duplicate chunk {i+1}")
                continue
            
            processed_chunks.add(chunk_normalized)
            
            logger.info(f"Processing chunk {i+1}/{len(text_chunks)} ({len(chunk)} chars)")
            
            # Extract basic details
            discount_value = extract_discount_value(chunk)
            coupon_code = extract_coupon_code(chunk)
            expiry_date = extract_expiry_date(chunk)
            
            # Clean with LLM
            context = f"Good News Auto promotion from 'What's happening?' section. Text: {chunk[:500]}"
            cleaned_data = clean_promo_text_with_llm(chunk, context)
            
            # Build promotion title
            if cleaned_data and cleaned_data.get("service_name"):
                promotion_title = cleaned_data.get("service_name")
            elif cleaned_data and cleaned_data.get("promo_description"):
                first_line = cleaned_data.get("promo_description", "").split("\n")[0].strip()[:100]
                promotion_title = first_line if first_line else chunk.split("\n")[0][:100]
            else:
                # Extract first meaningful line
                lines = [l.strip() for l in chunk.split("\n") if l.strip() and len(l.strip()) > 10]
                promotion_title = lines[0][:100] if lines else "Good News Auto Promotion"
            
            # Use LLM cleaned data if available
            if cleaned_data:
                offer_details = cleaned_data.get("promo_description") or chunk[:1000]
                discount_value = cleaned_data.get("discount_value") or discount_value
                coupon_code = cleaned_data.get("coupon_code") or coupon_code
                expiry_date = cleaned_data.get("expiry_date") or expiry_date
            else:
                offer_details = chunk[:1000]
            
            promo = {
                "website": competitor.get("domain", ""),
                "page_url": promo_url,
                "business_name": competitor.get("name", ""),
                "google_reviews": None,
                "service_name": cleaned_data.get("service_name", "auto service") if cleaned_data else "auto service",
                "promo_description": offer_details,
                "category": "auto service",
                "contact": competitor.get("address", ""),
                "location": competitor.get("address", ""),
                "offer_details": offer_details,
                "ad_title": promotion_title,
                "ad_text": chunk[:500],
                "new_or_updated": "new",
                "date_scraped": datetime.now().isoformat(),
                "discount_value": discount_value,
                "coupon_code": coupon_code,
                "expiry_date": expiry_date,
                "promotion_title": promotion_title
            }
            
            all_promos.append(promo)
            logger.info(f"✓ Added promo: {promotion_title[:50]} - {discount_value or 'N/A'}")
    
    # Deduplicate by 70%+ title word overlap
    logger.info(f"Found {len(all_promos)} promotions before deduplication")
    
    deduplicated = []
    seen = []
    
    for promo in all_promos:
        is_duplicate = False
        for seen_promo in seen:
            if are_promos_duplicate(promo, seen_promo):
                is_duplicate = True
                break
        
        if not is_duplicate:
            deduplicated.append(promo)
            seen.append(promo)
    
    logger.info(f"Total unique promotions found: {len(deduplicated)}")
    return deduplicated


def scrape_goodnews(competitor: Dict) -> Dict:
    """Main entry point for Good News Auto scraper."""
    try:
        promos = process_goodnews_promotions(competitor)
        
        # Save results
        output_file = PROMOTIONS_DIR / f"{competitor.get('name', 'goodnews').lower().replace(' ', '_')}.json"
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
        logger.error(f"Error scraping Good News Auto: {e}", exc_info=True)
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
    
    # Find Good News Auto
    goodnews = next((c for c in competitors if "good news" in c.get("name", "").lower()), None)
    
    if not goodnews:
        logger.error("Good News Auto not found in competitor list")
        sys.exit(1)
    
    result = scrape_goodnews(goodnews)
    print(f"\n✅ Scraping complete!")
    print(f"   Found {result.get('count', 0)} promotions")
    print(f"   Saved to: data/promotions/")
    print(f"\n📊 Summary:")
    for promo in result.get("promotions", []):
        print(f"   • {promo.get('promotion_title', 'N/A')}: {promo.get('discount_value', 'N/A')}")

