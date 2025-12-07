"""Image download and normalization utilities."""
import requests
from urllib.parse import urljoin, urlparse
from pathlib import Path
from typing import Optional
import hashlib
from app.config.constants import IMAGES_DIR
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__)

TIMEOUT = 10
IMAGES_DIR.mkdir(parents=True, exist_ok=True)


def normalize_url(base: str, src: str) -> str:
    """Normalize image URL relative to base URL."""
    # Skip data URIs (base64 encoded images - usually placeholders)
    if src.startswith("data:"):
        return None
    
    if src.startswith("//"):
        src = "http:" + src
    elif src.startswith("/"):
        parsed_base = urlparse(base)
        src = f"{parsed_base.scheme}://{parsed_base.netloc}{src}"
    return urljoin(base, src)


def get_image_hash(image_path: Path) -> str:
    """Calculate SHA256 hash of image file for deduplication."""
    try:
        with open(image_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return ""


def download_image(url: str, dest_dir: Path = None, filename: Optional[str] = None) -> Optional[Path]:
    """Download an image from URL to destination directory."""
    dest_dir = dest_dir or IMAGES_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        r = requests.get(url, stream=True, timeout=TIMEOUT, headers=headers, allow_redirects=True)
        r.raise_for_status()
        
        # Verify content type
        content_type = r.headers.get('content-type', '').lower()
        if 'image' not in content_type:
            logger.warning(f"URL {url} doesn't appear to be an image (content-type: {content_type})")
            # Still try to download if it's a common image extension
            if not any(ext in url.lower() for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):
                return None
        
        if not filename:
            parsed = urlparse(url)
            filename = Path(parsed.path).name
            if not filename or '.' not in filename:
                filename = f"image_{hashlib.md5(url.encode()).hexdigest()[:8]}.jpg"
        
        out = dest_dir / filename
        
        with open(out, "wb") as f:
            for chunk in r.iter_content(1024):
                f.write(chunk)
        
        logger.debug(f"Downloaded image: {out}")
        return out
        
    except Exception as e:
        logger.error(f"Error downloading image {url}: {e}")
        return None

