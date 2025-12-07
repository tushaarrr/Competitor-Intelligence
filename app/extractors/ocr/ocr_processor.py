"""OCR processing using Google Cloud Vision API with Tesseract fallback."""
from pathlib import Path
import logging
from typing import Optional
import time
import os

# Try Google Cloud Vision first
try:
    from google.cloud import vision
    from google.api_core.exceptions import GoogleAPIError
    VISION_AVAILABLE = True
except ImportError:
    vision = None
    VISION_AVAILABLE = False

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


def ocr_image(image_path: Path) -> str:
    """
    Extract text from image using Google Vision API (primary) or Tesseract (fallback).
    """
    if not image_path.exists():
        logger.error(f"Image file not found: {image_path}")
        return ""
    
    # Try Google Vision first
    text = ocr_with_vision(image_path)
    
    # Fallback to Tesseract if Vision fails
    if not text:
        logger.info(f"Google Vision failed, using Tesseract fallback for {image_path.name}")
        text = ocr_with_tesseract(image_path)
    
    return text or ""


def detect_promo_keywords(text: str, keywords: list) -> bool:
    """Check if text contains promotion-related keywords."""
    if not text:
        return False
    
    text_lower = text.lower()
    return any(keyword.lower() in text_lower for keyword in keywords)
