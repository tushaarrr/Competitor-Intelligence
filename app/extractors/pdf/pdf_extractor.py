"""PDF extraction utilities for promo extraction."""
from pathlib import Path
from typing import Optional
import requests
from urllib.parse import urlparse
import hashlib
from PIL import Image
import io

from app.config.constants import IMAGES_DIR
from app.utils.logging_utils import setup_logger
from app.extractors.ocr.ocr_processor import ocr_image

logger = setup_logger(__name__)

TIMEOUT = 30


def download_pdf(url: str, dest_dir: Path = None, filename: Optional[str] = None) -> Optional[Path]:
    """Download a PDF from URL to destination directory."""
    dest_dir = dest_dir or IMAGES_DIR  # Store PDFs in images directory for now
    dest_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        r = requests.get(url, stream=True, timeout=TIMEOUT, headers=headers, allow_redirects=True)
        r.raise_for_status()
        
        # Verify content type
        content_type = r.headers.get('content-type', '').lower()
        if 'pdf' not in content_type and not url.lower().endswith('.pdf'):
            logger.warning(f"URL {url} doesn't appear to be a PDF (content-type: {content_type})")
            # Still try to download if URL ends in .pdf
            if not url.lower().endswith('.pdf'):
                return None
        
        if not filename:
            parsed = urlparse(url)
            filename = Path(parsed.path).name
            if not filename or not filename.endswith('.pdf'):
                filename = f"pdf_{hashlib.md5(url.encode()).hexdigest()[:8]}.pdf"
        
        out = dest_dir / filename
        
        with open(out, "wb") as f:
            for chunk in r.iter_content(1024):
                f.write(chunk)
        
        logger.debug(f"Downloaded PDF: {out}")
        return out
        
    except Exception as e:
        logger.error(f"Error downloading PDF {url}: {e}")
        return None


def extract_text_from_pdf(pdf_path: Path) -> str:
    """Extract text from PDF file using pdfplumber (primary) or PyMuPDF (fallback).
    
    If no text is found (image-based PDF), extract images and run OCR.
    """
    if not pdf_path.exists():
        logger.error(f"PDF file not found: {pdf_path}")
        return ""
    
    text = ""
    
    # Try pdfplumber first (better for structured text)
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            pages_text = []
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    pages_text.append(page_text)
            text = "\n".join(pages_text)
            
        if text.strip():
            logger.info(f"Extracted {len(text)} characters from PDF using pdfplumber")
            return text.strip()
            
    except ImportError:
        logger.warning("pdfplumber not installed, trying PyMuPDF")
    except Exception as e:
        logger.warning(f"pdfplumber extraction failed: {e}, trying PyMuPDF")
    
    # Fallback to PyMuPDF for text extraction
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(pdf_path)
        pages_text = []
        for page in doc:
            page_text = page.get_text()
            if page_text:
                pages_text.append(page_text)
        text = "\n".join(pages_text)
        doc.close()
        
        if text.strip():
            logger.info(f"Extracted {len(text)} characters from PDF using PyMuPDF")
            return text.strip()
            
    except ImportError:
        logger.error("Neither pdfplumber nor PyMuPDF installed. Install with: pip install pdfplumber PyMuPDF")
        return ""
    except Exception as e:
        logger.warning(f"PyMuPDF text extraction failed: {e}, trying OCR on PDF pages")
    
    # If no text found, PDF is likely image-based → extract images and run OCR
    if not text.strip():
        logger.info("No text found in PDF, attempting OCR on PDF pages as images...")
        text = extract_text_from_pdf_via_ocr(pdf_path)
    
    return text.strip() if text else ""


def extract_text_from_pdf_via_ocr(pdf_path: Path) -> str:
    """Extract text from image-based PDF by converting pages to images and running OCR."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(pdf_path)
        all_text = []
        num_pages = len(doc)
        
        try:
            for page_num in range(num_pages):
                page = doc[page_num]
                
                # Render page as image (300 DPI for good quality)
                mat = fitz.Matrix(300/72, 300/72)  # 300 DPI
                pix = page.get_pixmap(matrix=mat)
                
                # Convert to PIL Image
                img_data = pix.tobytes("png")
                img = Image.open(io.BytesIO(img_data))
                
                # Save temporary image for OCR
                temp_img_path = pdf_path.parent / f"{pdf_path.stem}_page_{page_num + 1}.png"
                img.save(temp_img_path)
                
                # Clean up pixmap immediately
                pix = None
                
                # Run OCR on the image
                page_text = ocr_image(temp_img_path)
                if page_text:
                    all_text.append(page_text)
                
                # Clean up temp image
                if temp_img_path.exists():
                    temp_img_path.unlink()
        finally:
            # Always close the document
            doc.close()
        
        combined_text = "\n".join(all_text)
        if combined_text.strip():
            logger.info(f"Extracted {len(combined_text)} characters from PDF using OCR on {num_pages} page(s)")
            return combined_text
        
    except ImportError:
        logger.error("PyMuPDF not installed, cannot extract images from PDF")
    except Exception as e:
        logger.error(f"Error extracting text from PDF via OCR: {e}", exc_info=True)
    
    return ""

