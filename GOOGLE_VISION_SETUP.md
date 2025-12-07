# Google Cloud Vision API Setup

## Overview

The OCR system now uses **Google Cloud Vision API** instead of Tesseract, with automatic retry logic for reliability.

## Features

✅ **Google Cloud Vision API** - Industry-leading OCR accuracy  
✅ **Retry Logic** - Automatic retries with exponential backoff  
✅ **Error Handling** - Graceful handling of API errors  
✅ **Credential Management** - Supports service account JSON or environment variable  

## Setup

### 1. Install Dependencies

Already added to `requirements.txt`:
```bash
google-cloud-vision==3.5.0
```

Install with:
```bash
pip install -r requirements.txt
```

### 2. Google Cloud Credentials

The system looks for credentials in this order:

1. **Service Account JSON file** (recommended)
   - Place `service_account.json` in project root
   - Same file can be used for Google Sheets and Vision API

2. **Environment Variable**
   ```bash
   export GOOGLE_APPLICATION_CREDENTIALS=/path/to/credentials.json
   ```

### 3. Enable Google Cloud Vision API

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Enable "Cloud Vision API" for your project
3. Ensure your service account has "Cloud Vision API User" role

## Retry Logic

The OCR processor includes robust retry logic:

- **Max Retries**: 3 attempts
- **Initial Delay**: 1 second
- **Exponential Backoff**: Doubles delay each retry (max 10s)
- **Error Handling**: 
  - Retries on transient errors
  - Skips retry on invalid image format errors
  - Logs all errors for debugging

## Usage

```python
from app.extractors.ocr.ocr_processor import ocr_image

text = ocr_image(image_path)
```

## Benefits over Tesseract

1. **Higher Accuracy** - Better at reading coupon images
2. **No Local Dependencies** - No need to install Tesseract binary
3. **Handles Complex Images** - Better with logos, fonts, layouts
4. **Cloud-Scale** - Can process many images concurrently
5. **Automatic Retry** - Handles API rate limits automatically

## Troubleshooting

### "No credentials found"
- Ensure `service_account.json` exists in project root
- Or set `GOOGLE_APPLICATION_CREDENTIALS` environment variable

### "API not enabled"
- Enable Cloud Vision API in Google Cloud Console
- Check service account has correct permissions

### Rate limiting
- Retry logic handles this automatically
- Consider adding delays between batch operations

