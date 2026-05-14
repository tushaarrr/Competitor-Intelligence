"""OCR processing using Google Cloud Vision API with Tesseract fallback."""
import base64
from pathlib import Path
import logging
from typing import Optional
import time
import os

import requests

# Try Google Cloud Vision first
try:
    from google.cloud import vision
    from google.api_core.exceptions import GoogleAPIError
    VISION_AVAILABLE = True
except ImportError:
    vision = None
    VISION_AVAILABLE = False

GOOGLE_VISION_API_KEY = os.getenv("GOOGLE_CLOUD_VISION_API_KEY")
GOOGLE_VISION_REST_URL = "https://vision.googleapis.com/v1/images:annotate"

# Tesseract as fallback
try:
    import pytesseract
    from PIL import Image
    TESSERACT_AVAILABLE = True
except ImportError:
    pytesseract = None
    TESSERACT_AVAILABLE = False

from app.config.constants import ROOT
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__)

# Retry configuration
MAX_RETRIES = 3
INITIAL_RETRY_DELAY = 1  # seconds
MAX_RETRY_DELAY = 10  # seconds


def get_vision_client():
    """Initialize Google Cloud Vision client."""
    if not VISION_AVAILABLE:
        return None
    
    try:
        # Check for credentials file in project root
        creds_path = ROOT / "service_account.json"
        
        if creds_path.exists():
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(creds_path)
            logger.debug(f"Using credentials from {creds_path}")
        else:
            # Try environment variable
            if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
                logger.warning("No Google Cloud credentials found. Will use Tesseract fallback.")
                return None
        
        client = vision.ImageAnnotatorClient()
        return client
    except Exception as e:
        logger.warning(f"Error initializing Google Cloud Vision client: {e}. Will use Tesseract fallback.")
        return None


def ocr_with_vision(image_path: Path) -> Optional[str]:
    """Extract text using Google Cloud Vision API."""
    client = get_vision_client()
    if not client:
        return None
    
    try:
        # Read image file
        with open(image_path, "rb") as image_file:
            content = image_file.read()
        
        # Create Vision API image object
        image = vision.Image(content=content)
        
        # Perform text detection
        response = client.text_detection(image=image)
        
        # Check for errors
        if response.error.message:
            logger.warning(f"Vision API error: {response.error.message}")
            return None
        
        # Extract text
        texts = response.text_annotations
        if texts:
            full_text = texts[0].description
            logger.debug(f"Google Vision OCR extracted {len(full_text)} characters")
            return full_text.strip()
        
        return None
        
    except Exception as e:
        logger.warning(f"Google Vision OCR error: {e}")
        return None


def ocr_with_tesseract(image_path: Path) -> str:
    """Extract text using Tesseract OCR (fallback)."""
    if not TESSERACT_AVAILABLE:
        logger.error("Tesseract not available")
        return ""
    
    try:
        # Preprocess image
        img = Image.open(image_path)
        if img.mode != 'L':
            img = img.convert("L")
        
        # Run OCR
        text = pytesseract.image_to_string(img, config="--oem 3 --psm 6")
        logger.debug(f"Tesseract OCR extracted {len(text)} characters")
        return text.strip()
    except Exception as e:
        logger.error(f"Tesseract OCR error: {e}")
        return ""


def ocr_with_vision_api_key(image_path: Path) -> Optional[str]:
    """Extract text via Vision REST API using ``GOOGLE_CLOUD_VISION_API_KEY``.

    Used when no service-account JSON is available. Returns text on success,
    None on any failure so the caller can keep falling back to Tesseract.
    """
    # Reload env each call so this works even when .env was edited mid-session.
    api_key = os.getenv("GOOGLE_CLOUD_VISION_API_KEY") or GOOGLE_VISION_API_KEY
    if not api_key:
        return None
    try:
        with open(image_path, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode("ascii")
        body = {
            "requests": [
                {
                    "image": {"content": content_b64},
                    "features": [{"type": "TEXT_DETECTION", "maxResults": 1}],
                }
            ]
        }
        resp = requests.post(
            f"{GOOGLE_VISION_REST_URL}?key={api_key}",
            json=body, timeout=30,
        )
        if not resp.ok:
            logger.warning(
                f"Vision REST API non-200 ({resp.status_code}): {resp.text[:200]}"
            )
            return None
        data = resp.json()
        responses = data.get("responses") or []
        if not responses:
            return None
        first = responses[0] or {}
        if first.get("error"):
            logger.warning(f"Vision REST API error: {first['error']}")
            return None
        # Prefer the consolidated text from full_text_annotation, fall back to
        # the joined description of text_annotations.
        full = (first.get("fullTextAnnotation") or {}).get("text")
        if full:
            logger.debug(f"Vision REST OCR extracted {len(full)} characters")
            return full.strip()
        anns = first.get("textAnnotations") or []
        if anns and anns[0].get("description"):
            text = anns[0]["description"]
            logger.debug(f"Vision REST OCR extracted {len(text)} characters")
            return text.strip()
        return None
    except Exception as e:
        logger.warning(f"Vision REST OCR error: {e}")
        return None


def ocr_image(image_path: Path) -> str:
    """Extract text from image.

    Order of attempts:
      1. Google Vision via service-account client (if credentials available).
      2. Google Vision via REST + ``GOOGLE_CLOUD_VISION_API_KEY``.
      3. Tesseract (if installed locally).
    """
    if not image_path.exists():
        logger.error(f"Image file not found: {image_path}")
        return ""

    text = ocr_with_vision(image_path)
    if text:
        return text

    text = ocr_with_vision_api_key(image_path)
    if text:
        return text

    logger.info(f"Google Vision unavailable, using Tesseract fallback for {image_path.name}")
    text = ocr_with_tesseract(image_path)
    return text or ""


def detect_promo_keywords(text: str, keywords: list) -> bool:
    """Check if text contains promotion-related keywords."""
    if not text:
        return False
    
    text_lower = text.lower()
    return any(keyword.lower() in text_lower for keyword in keywords)
