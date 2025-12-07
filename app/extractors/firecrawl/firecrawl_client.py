"""Firecrawl client for fetching HTML and extracting images."""
import os
from typing import Dict, List, Optional
from firecrawl import FirecrawlApp
from app.config.constants import FIRECRAWL_API_KEY
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__)


def get_firecrawl_client() -> Optional[FirecrawlApp]:
    """Initialize Firecrawl client."""
    # Reload from env directly to ensure latest value
    from dotenv import load_dotenv
    from pathlib import Path
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=True)
    
    api_key = os.getenv("FIRECRAWL_API_KEY") or FIRECRAWL_API_KEY
    if not api_key or api_key == "your_firecrawl_api_key_here":
        logger.warning("FIRECRAWL_API_KEY not set or still has placeholder value")
        return None
    
    try:
        return FirecrawlApp(api_key=api_key)
    except Exception as e:
        logger.error(f"Error initializing Firecrawl client: {e}")
        return None


def fetch_with_firecrawl(url: str, timeout: int = 60) -> Dict:
    """Fetch HTML and extract data using Firecrawl."""
    import requests
    from dotenv import load_dotenv
    from pathlib import Path
    
    # Reload from env directly to ensure latest value
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=True)
    
    api_key = os.getenv("FIRECRAWL_API_KEY") or FIRECRAWL_API_KEY
    if not api_key or api_key == "your_firecrawl_api_key_here":
        logger.warning("FIRECRAWL_API_KEY not set or still has placeholder value")
        return {"html": "", "images": [], "error": "Firecrawl API key not set"}
    
    try:
        logger.info(f"Fetching {url} with Firecrawl (direct API)")
        
        # Use direct API call (v2 scrape endpoint)
        api_url = "https://api.firecrawl.dev/v2/scrape"
        payload = {
            "url": url,
            "onlyMainContent": False,
            "maxAge": 172800000,  # 2 days
            "formats": ["html", "markdown"]  # Get both for better content extraction
        }
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        response = requests.post(api_url, json=payload, headers=headers, timeout=timeout)
        response.raise_for_status()
        
        result = response.json()
        
        # Extract HTML from result
        html = ""
        if isinstance(result, dict):
            # Check for data field (common in API responses)
            data = result.get("data", result)
            html = data.get("html", "") or data.get("markdown", "") or data.get("content", "")
            
            # If still no HTML, try direct fields
            if not html:
                html = result.get("html", "") or result.get("markdown", "") or result.get("content", "")
        
        # Extract images from HTML
        images = []
        if html:
            from bs4 import BeautifulSoup
            from urllib.parse import urljoin
            soup = BeautifulSoup(html, "html.parser")
            seen_urls = set()
            for img in soup.find_all("img"):
                for attr in ["src", "data-src", "data-lazy-src", "data-original", "data-url"]:
                    src = img.get(attr)
                    if src and not src.startswith("data:") and src not in seen_urls:
                        img_url = urljoin(url, src)
                        images.append(img_url)
                        seen_urls.add(src)
        
        # Also check for images in metadata if available
        if isinstance(result, dict):
            data = result.get("data", result)
            if "images" in data:
                images.extend([urljoin(url, img) for img in data["images"] if img])
            elif "metadata" in data and "images" in data["metadata"]:
                images.extend([urljoin(url, img) for img in data["metadata"]["images"] if img])
        
        # Deduplicate images
        images = list(set(images))
        
        logger.info(f"Firecrawl successful: {len(html)} chars, {len(images)} images")
        
        return {
            "html": html,
            "images": images,
            "error": None
        }
        
    except requests.exceptions.Timeout:
        error_msg = f"Firecrawl timeout after {timeout}s"
        logger.warning(error_msg)
        return {"html": "", "images": [], "error": error_msg}
    except requests.exceptions.RequestException as e:
        error_msg = f"Firecrawl API error: {str(e)}"
        logger.warning(error_msg)
        
        # Try SDK as fallback
        logger.info("Trying Firecrawl SDK as fallback...")
        try:
            return _fetch_with_firecrawl_sdk(url, timeout)
        except Exception as sdk_error:
            logger.warning(f"SDK fallback also failed: {sdk_error}")
            error_msg = str(e)
    
    # Final fallback to httpx
    logger.warning(f"Firecrawl failed ({error_msg[:100]}), trying httpx fallback...")
    try:
        import httpx
        response = httpx.get(url, timeout=30, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        response.raise_for_status()
        html = response.text
        logger.info(f"Fallback httpx fetch successful: {len(html)} chars")
        
        # Extract images from HTML
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin
        soup = BeautifulSoup(html, "html.parser")
        images = []
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            if src:
                images.append(urljoin(url, src))
        
        return {
            "html": html,
            "images": list(set(images)),
            "error": None
        }
    except Exception as fallback_error:
        logger.warning(f"httpx fallback failed, trying ZenRows...")
        
        # Try ZenRows as second fallback
        try:
            from app.config.constants import ZENROWS_API_KEY
            import requests
            
            if ZENROWS_API_KEY:
                zenrows_url = "https://api.zenrows.com/v1/"
                params = {
                    "url": url,
                    "apikey": ZENROWS_API_KEY,
                    "js_render": "true",
                    "wait": "2000"
                }
                response = requests.get(zenrows_url, params=params, timeout=30)
                response.raise_for_status()
                html = response.text
                logger.info(f"ZenRows fallback successful: {len(html)} chars")
                
                # Extract images from HTML
                from bs4 import BeautifulSoup
                from urllib.parse import urljoin
                soup = BeautifulSoup(html, "html.parser")
                images = []
                for img in soup.find_all("img"):
                    src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
                    if src:
                        images.append(urljoin(url, src))
                
                return {
                    "html": html,
                    "images": list(set(images)),
                    "error": None
                }
        except Exception as zenrows_error:
            logger.error(f"All fallbacks failed. Firecrawl: {error_msg}; httpx: {fallback_error}; ZenRows: {zenrows_error}")
            return {"html": "", "images": [], "error": f"All methods failed: Firecrawl error, httpx error, ZenRows error"}


def _fetch_with_firecrawl_sdk(url: str, timeout: int = 60) -> Dict:
    """Fallback: Fetch using Firecrawl Python SDK."""
    client = get_firecrawl_client()
    if not client:
        raise Exception("Firecrawl client not initialized")
    
    logger.info(f"Fetching {url} with Firecrawl SDK")
    
    # Use Firecrawl to scrape the URL
    try:
        result = client.scrape(
            url=url,
            formats=["html"],
            only_main_content=False,
            fast_mode=True
        )
    except Exception as e:
        logger.warning(f"Fast mode failed, trying normal mode: {e}")
        try:
            result = client.scrape(
                url=url,
                formats=["html"],
                only_main_content=False
            )
        except Exception as e2:
            raise Exception(f"SDK scrape failed: {e2}")
    
    # Extract HTML from result
    html = ""
    if hasattr(result, 'html'):
        html = result.html or ""
    elif hasattr(result, 'markdown'):
        html = result.markdown or ""
    elif hasattr(result, 'content'):
        html = result.content or ""
    elif isinstance(result, dict):
        html = result.get("html", "") or result.get("markdown", "") or result.get("content", "")
    
    # Extract images from HTML
    images = []
    if html:
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin
        soup = BeautifulSoup(html, "html.parser")
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            if src:
                images.append(urljoin(url, src))
    
    return {
        "html": html,
        "images": list(set(images)),
        "error": None
    }


def extract_image_urls_from_firecrawl(result: Dict, base_url: str) -> List[str]:
    """Extract image URLs from Firecrawl result."""
    from urllib.parse import urljoin
    
    images = []
    
    # Try to extract from metadata or links
    if isinstance(result, dict):
        # Check for images in metadata
        metadata = result.get("metadata", {})
        if "images" in metadata:
            images.extend(metadata["images"])
        
        # Check for image links in the content
        links = result.get("links", [])
        for link in links:
            if isinstance(link, dict):
                url = link.get("url", "")
                if url and any(ext in url.lower() for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
                    images.append(urljoin(base_url, url))
        
        # Extract from HTML if present
        html = result.get("html", "")
        if html:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for img in soup.find_all("img"):
                src = img.get("src") or img.get("data-src")
                if src:
                    images.append(urljoin(base_url, src))
    
    # Deduplicate
    return list(set(images))

