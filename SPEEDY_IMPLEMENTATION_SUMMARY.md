# Speedy Auto Service Scraper - Implementation Complete ✅

## What Was Built

### 1. **Firecrawl Integration** (`app/extractors/firecrawl/firecrawl_client.py`)
   - Fetches full HTML from promo pages
   - Extracts all image URLs automatically
   - Handles errors gracefully

### 2. **Image Processing Pipeline** (`app/extractors/images/image_downloader.py`)
   - Downloads promo coupon images
   - Normalizes URLs (handles relative/absolute paths)
   - Calculates image hashes for deduplication

### 3. **OCR Processing** (`app/extractors/ocr/ocr_processor.py`)
   - Preprocesses images (grayscale, contrast, denoise, upscale)
   - Runs Tesseract OCR with optimized config
   - Detects promo keywords to filter irrelevant images

### 4. **LLM Text Cleaning** (`app/extractors/ocr/llm_cleaner.py`)
   - Uses Perplexity AI to clean and structure OCR text
   - Extracts: service_name, discount_value, coupon_code, expiry_date, category
   - Falls back to raw OCR if LLM fails

### 5. **HTML Parser** (`app/extractors/html_parser.py`)
   - Finds promo containers using multiple selector strategies
   - Extracts all `<img>` tags within promo sections
   - Provides context (alt text, surrounding HTML)

### 6. **Main Scraper** (`app/scrapers/speedy_scraper.py`)
   - Orchestrates the entire pipeline
   - Handles deduplication (image hash + content hash)
   - Outputs structured JSON matching your schema

## Process Flow

```
Speedy Promo Page
    ↓
Firecrawl → HTML + Image URLs
    ↓
HTML Parser → Find Promo Images
    ↓
For each image:
    Download → OCR → LLM Clean → Deduplicate → Save
    ↓
Structured Promotions JSON
```

## Output Schema

Each promotion includes:
- `website`, `page_url`, `business_name`
- `service_name`, `promo_description`, `category`
- `discount_value`, `coupon_code`, `expiry_date`
- `offer_details` (full OCR text)
- `image_url`, `date_scraped`
- `new_or_updated`

## Key Features

✅ **No PDF Processing** - Uses images only (faster, cleaner)  
✅ **Multi-level Deduplication** - Image hash + content hash  
✅ **LLM Cleaning** - Structured extraction from messy OCR  
✅ **Smart Filtering** - Only processes promo-related images  
✅ **Error Handling** - Continues on failures, logs everything  

## Next Steps

1. Add `FIRECRAWL_API_KEY` to `.env` if not already there
2. Run: `python run_speedy.py`
3. Check results in `data/promotions/speedy_auto_service.json`

## Files Created

- `app/scrapers/speedy_scraper.py` - Main scraper
- `app/extractors/firecrawl/firecrawl_client.py` - Firecrawl integration
- `app/extractors/html_parser.py` - HTML parsing
- `app/extractors/images/image_downloader.py` - Image handling
- `app/extractors/ocr/ocr_processor.py` - OCR processing
- `app/extractors/ocr/llm_cleaner.py` - LLM cleaning
- `run_speedy.py` - Quick runner script
