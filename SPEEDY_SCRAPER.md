# Speedy Auto Service Scraper

## Overview

This scraper extracts promotion coupons from Speedy Auto Service using a multi-step process:

1. **Firecrawl** - Fetches full HTML and extracts all image URLs
2. **Image Detection** - Finds all promo images in HTML containers
3. **OCR Processing** - Extracts text from coupon images using Tesseract
4. **LLM Cleaning** - Uses Perplexity AI to structure and clean OCR text
5. **Deduplication** - Removes duplicate promos (same image or similar content)

## Features

- ✅ Image-based coupon extraction (no PDF processing)
- ✅ OCR with preprocessing for better accuracy
- ✅ LLM-based text cleaning and structuring
- ✅ Automatic deduplication
- ✅ Service category mapping (oil change, brake, battery, seasonal)

## Setup

### 1. Environment Variables

Add to `.env`:
```bash
FIRECRAWL_API_KEY=your_firecrawl_api_key
PERPLEXITY_API_KEY=your_perplexity_api_key  # For LLM cleaning
```

### 2. Install Tesseract (if not already installed)

```bash
# macOS
brew install tesseract

# Ubuntu/Debian
sudo apt-get install tesseract-ocr
```

## Usage

### Run Speedy Scraper

```bash
python run_speedy.py
```

Or directly:
```bash
python -m app.scrapers.speedy_scraper
```

## Output Format

Results are saved to `data/promotions/speedy_auto_service.json` with this structure:

```json
{
  "competitor": "Speedy Auto Service",
  "scraped_at": "2024-01-01T12:00:00",
  "promotions": [
    {
      "website": "speedy.com",
      "page_url": "https://www.speedy.com/en-ca/promotions/",
      "business_name": "Speedy Auto Service",
      "service_name": "oil change",
      "promo_description": "Clean promo description",
      "category": "oil change",
      "discount_value": "$20 off",
      "coupon_code": "CODE123",
      "expiry_date": "12/31/2024",
      "offer_details": "Full OCR text",
      "image_url": "https://...",
      "date_scraped": "2024-01-01T12:00:00",
      "new_or_updated": "new"
    }
  ],
  "count": 5
}
```

## Process Flow

1. **Fetch with Firecrawl**
   - Gets full HTML content
   - Extracts all image URLs

2. **Find Promo Images**
   - Searches for images in promo containers
   - Filters by class names and attributes

3. **Download & OCR**
   - Downloads each unique image
   - Runs OCR with preprocessing
   - Filters non-promo content

4. **LLM Cleaning**
   - Sends OCR text to Perplexity AI
   - Extracts structured data:
     - Service name
     - Discount value
     - Coupon code
     - Expiry date
     - Category

5. **Deduplication**
   - Compares image hashes
   - Compares promo content hashes
   - Skips duplicates

## Expected Promotions

- Oil change coupons
- Brake service coupons
- Battery coupons
- Seasonal discounts
- Tire service promotions

## Troubleshooting

### No images found
- Check if promo_links are correct in competitor_list.json
- Verify Firecrawl API key is set
- Check HTML structure hasn't changed

### OCR not working
- Verify Tesseract is installed: `tesseract --version`
- Check image quality/download succeeded
- Review logs in `logs/speedy_scraper.log`

### LLM cleaning failing
- Verify PERPLEXITY_API_KEY is set
- Check API quota/limits
- Falls back to raw OCR text if LLM fails

