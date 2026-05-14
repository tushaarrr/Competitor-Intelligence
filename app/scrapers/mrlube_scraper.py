"""Mr. Lube + Tires v2 scraper.

Site:        https://www.mrlube.com/en/Services/tire-rebates-and-financing
Cities:      Calgary, Edmonton (fan-out, ``national_shared``)
Extraction:  ``image_ocr`` + ``text``
URLs:        1 (single shared rebates/financing page)

The page is image-driven: each rebate or financing offer is a card/banner with
the headline/discount in a graphic, plus nearby text for validity dates and
financing conditions. We:
  1. Pull text-card candidates that hit a strong offer signal (financing,
     rebate, save $X, $X off, no payments, etc.).
  2. OCR promo-looking images (rebate/financing/banner/hero filename or alt),
     keep only those whose OCR text hits the same offer signal.
  3. Fan every valid offer out to Calgary + Edmonton.

No Grande Prairie rows are produced. No cross-URL dedup (we only have one
URL anyway). Duplicate offers across cards get a shared ``duplicate_group_id``.

Public entry point:
    scrape_mrlube_v2(competitor_v2, *, mode="qa_expanded") -> Dict
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from app.config.constants import DATA_DIR, IMAGES_DIR
from app.extractors.firecrawl.firecrawl_client import fetch_with_firecrawl
from app.extractors.images.image_downloader import normalize_url
from app.extractors.ocr.ocr_processor import ocr_image
from app.utils.logging_utils import setup_logger
from app.utils.service_classifier import classify_service
from app.scrapers.jiffy_scraper import (
    _v2_extract_discount,
    _v2_extract_coupon_code,
    _summarize_promo_description,
    _normalize_discount,
    _confidence_from_promo,
    _signature_meaningful_tokens,
)

logger = setup_logger(__name__, "mrlube_scraper.log")

PROMOTIONS_DIR = DATA_DIR / "promotions"
PROMOTIONS_DIR.mkdir(parents=True, exist_ok=True)

_BUSINESS_NAME = "Mr. Lube + Tires"
_WEBSITE = "mrlube.com"
_TARGET_CITIES = ["Calgary", "Edmonton"]

# Mr. Lube's tire-rebates page is single-purpose; default service hint is
# Tire Sales but financing / oil-change references can re-classify.
_DEFAULT_SERVICE_HINT = "Tire Sales"

# Real-offer signal — must hit one of these for the row to count.
# "Financing" and "package price" are first-class here because Mr. Lube's tire
# rebates page leans heavily on financing terms (e.g. "no payments for X months").
_OFFER_SIGNAL = re.compile(
    r"(?:\$\s*\d+(?:\.\d{1,2})?|"
    r"\b\d+\s*%\s*off\b|\bup\s+to\s+\d+\s*%|"
    r"\brebates?\b|\bfinancing\b|\bpackage\s+price\b|"
    r"\bsave\s+(?:up\s+to\s+)?\$?\d|\bget\s+\$\d|\bbonus\b|"
    r"\bfree\s+(?:with|on|when|installation|tire|rotation|oil)|"
    r"\bno\s+payments?\b|\bno\s+interest\b|\b\d+\s*months?\b|"
    r"\blimited[- ]time\b|\bvalid\s+(?:through|until|thru)\b|"
    r"\bexpires?\b|\bmail[- ]in\s+rebate\b|\binstant\s+rebate\b)",
    re.IGNORECASE,
)

# Disclaimer / boilerplate to drop even if the offer regex matches.
_DISCLAIMER_PATTERNS = re.compile(
    r"(?:not\s+valid\s+with|see\s+(?:store|dealer)\s+for|"
    r"terms\s+(?:and|&)\s+conditions|"
    r"o\.?a\.?c\.?\b|on\s+approved\s+credit|"
    r"some\s+restrictions|all\s+rights\s+reserved)",
    re.IGNORECASE,
)

# Newsletter / form CTAs to drop.
_FORM_CTA_PATTERNS = re.compile(
    r"(?:sign\s*up|subscribe|enter\s+your\s+email|email\s+address|"
    r"first\s+name|last\s+name|book\s+(?:now|online|appointment))",
    re.IGNORECASE,
)

# UI / widget noise — when a body is dominated by these phrases there is
# no real offer body, only a store-locator/search widget.
_UI_NOISE_PHRASES = (
    "find stores near me", "find a store", "oh snap", "loading...",
    "select your store", "choose a brand", "shop tires online",
    "order your tires", "buy tires", "check out rebates", "search tires",
)

# Strict service taxonomy — final rows whose service falls outside this set
# are dropped per spec.
_ALLOWED_SERVICES = frozenset({
    "Battery", "Oil Change", "Brake", "Tire Sales", "Tire Rotation",
    "Transmission Fluid", "Radiator Flush", "Fuel System Flush",
})


def _body_is_ui_noise(text: str) -> bool:
    """Return True when most of a body is the store-locator / search widget.

    Used to reject candidates whose only offer-signal hit was in the title
    (e.g. ``Tire Rebates & Financing`` heading) and whose body has no real
    promo content.
    """
    if not text:
        return True
    low = text.lower()
    noise_hits = sum(low.count(p) for p in _UI_NOISE_PHRASES)
    # Strip noise phrases and see how much real content remains.
    stripped = low
    for p in _UI_NOISE_PHRASES:
        stripped = stripped.replace(p, " ")
    stripped = re.sub(r"[\s\.,!?]+", " ", stripped).strip()
    return noise_hits >= 2 and len(stripped) < 60

# Image URL/filename/alt cues that suggest the image is a real offer.
_PROMO_IMAGE_HINTS = re.compile(
    r"(?:coupon|offer|promo|rebate|sale|special|discount|deal|save|saving|"
    r"banner|hero|feature|financing|tire|installment|"
    r"(?:\d+\s*%|\$\s*\d+))",
    re.IGNORECASE,
)

# UI/branding/social — never OCR these.
_UI_IMAGE_SKIP = re.compile(
    r"(?:logo|favicon|icon[-_]?\w*|sprite|placeholder|spacer|loader|"
    r"facebook|twitter|instagram|youtube|linkedin|tiktok|pinterest|"
    r"google-?play|app-?store|badge|trustpilot|star)",
    re.IGNORECASE,
)

_EXPIRY_RE = re.compile(
    r"(?:expires?|valid\s+(?:until|through|thru)|offer\s+ends?)\s*[:\-]?\s*"
    r"((?:[A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})|"
    r"(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}))",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------
def _fetch_page(url: str) -> Tuple[str, List[str]]:
    res = fetch_with_firecrawl(url, timeout=90)
    if res.get("html") and not res.get("error"):
        return res["html"], res.get("images", []) or []
    logger.warning(f"[mrlube-v2] Firecrawl failed for {url}: {res.get('error')}")
    return "", []


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "en-CA,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    # Mr. Lube's Azure-Blob CDN refuses requests without these CORS-style
    # headers; with them it returns the actual image (see scraper docstring).
    "Sec-Fetch-Dest": "image",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "cross-site",
}


def _download_image(url: str, *, referer: str, dest_dir: Path = IMAGES_DIR) -> Optional[Path]:
    """Browser-headers + Referer + Origin download (Mr. Lube's CDN gates
    requests that don't look like a real browser navigating from
    ``www.mrlube.com``)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    headers = dict(_BROWSER_HEADERS)
    headers["Referer"] = referer
    # Use the referer's origin as the Origin header — that's what the live
    # browser sends and what the CDN whitelist looks for.
    parsed_ref = urlparse(referer)
    headers["Origin"] = f"{parsed_ref.scheme}://{parsed_ref.netloc}"
    suffix = Path(urlparse(url).path).suffix or ".jpg"
    fname = f"mrlube_{hashlib.md5(url.encode()).hexdigest()[:10]}{suffix}"
    out = dest_dir / fname
    try:
        r = requests.get(url, headers=headers, timeout=15, allow_redirects=True, stream=True)
        if r.status_code != 200:
            logger.warning(f"[mrlube-v2] image fetch {r.status_code} for {url}")
            return None
        with open(out, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)
        return out
    except Exception as e:
        logger.warning(f"[mrlube-v2] image download error for {url}: {e}")
        return None


def _ocr_url(url: str, *, referer: str, ocr_cache: Dict[str, str]) -> str:
    if url in ocr_cache:
        return ocr_cache[url]
    img_path = _download_image(url, referer=referer)
    text = ""
    if img_path:
        try:
            text = ocr_image(img_path) or ""
        except Exception as e:
            logger.warning(f"[mrlube-v2] OCR error for {url}: {e}")
        try:
            img_path.unlink()
        except Exception:
            pass
    ocr_cache[url] = text
    return text


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _extract_title(text: str, fallback: str = "") -> str:
    line = re.split(r"[\.\n]", (text or "").strip(), 1)[0].strip()
    line = re.sub(r"\s+", " ", line)[:160]
    return line or fallback


def _extract_expiry(text: str) -> Optional[str]:
    m = _EXPIRY_RE.search(text or "")
    return m.group(1).strip() if m else None


_OFFER_TITLE_RE = re.compile(
    r"(\$\s*\d+(?:\.\d{1,2})?\s*Off\*?\s+[A-Z][A-Za-z0-9 &/\-]{2,50}?)"
    r"(?=\s*(?:Expires?\b|\*|\.|$|\n))",
    re.IGNORECASE,
)


def _better_offer_title(raw_text: str, fallback: str) -> str:
    m = _OFFER_TITLE_RE.search(raw_text or "")
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip(" *.,")[:160]
    return fallback


def _title_from_ocr(ocr_text: str, fallback: str) -> str:
    """Pick a more meaningful title from the OCR text of a banner.

    OCR results often come line-by-line with UI labels first ("Shop",
    "Order Your Tires"). Prefer a line that mentions the actual offer
    (rebate amount, "UP TO", brand names).
    """
    if not ocr_text:
        return fallback
    lines = [ln.strip() for ln in re.split(r"[\n\r]+", ocr_text) if ln.strip()]
    # 1. Try to find a "$X in rebates" / "UP TO $X" / "X% off" line.
    for ln in lines:
        if re.search(
            r"(?:\bup\s+to\b.*\$\s*\d|\$\s*\d.*\b(?:rebate|cash\s+back|off|in\b)|"
            r"\d+\s*%\s*off|\bget\s+\$\d|\bsave\s+\$?\d)",
            ln, re.IGNORECASE,
        ):
            return re.sub(r"\s+", " ", ln).strip(" *.,")[:160]
    # 2. Try lines that contain a tire brand (banner subject).
    for ln in lines:
        if re.search(r"\b(?:bridgestone|michelin|firestone|goodyear|continental|"
                     r"pirelli|hankook|toyo|yokohama|cooper|kumho|nokian|nexen|"
                     r"falken|general|bf\s*goodrich|uniroyal)\b", ln, re.IGNORECASE):
            return re.sub(r"\s+", " ", ln).strip(" *.,")[:160]
    # 3. Try lines that contain "rebate" / "financing".
    for ln in lines:
        if re.search(r"\b(?:rebate|financing)s?\b", ln, re.IGNORECASE):
            return re.sub(r"\s+", " ", ln).strip(" *.,")[:160]
    return fallback


def _refine_mrlube_summary(
    summary: str, *, raw_text: str, discount: Optional[str], service: str
) -> str:
    """Mr. Lube's offers are rebates / financing, not 'off' discounts.

    Rewrite the generic summary when the underlying text clearly indicates a
    rebate or financing offer, and pull tire-brand context when possible.
    """
    text = (raw_text or "").lower()
    is_rebate = bool(re.search(r"\brebate", text))
    is_financing = bool(
        re.search(r"\bfinancing\b|\bno\s+payments?\b|\bno\s+interest\b", text)
    )
    if not (is_rebate or is_financing):
        return summary

    brand_m = re.search(
        r"\b(bridgestone|michelin|firestone|goodyear|continental|pirelli|"
        r"hankook|toyo|yokohama|cooper|kumho|nokian|nexen|falken|general|"
        r"bf\s*goodrich|uniroyal)\b",
        text, re.IGNORECASE,
    )
    brand_phrase = ""
    if brand_m:
        brand_phrase = f" on {brand_m.group(1).title()} tires"

    upto_m = re.search(r"\bup\s+to\s+(\$\s*\d+(?:\.\d{1,2})?)", text, re.IGNORECASE)
    amount = (
        re.sub(r"\s+", "", upto_m.group(1)) if upto_m
        else (discount.strip() if discount else None)
    )

    if is_rebate:
        if amount:
            return f"Up to {amount} mail-in rebate{brand_phrase} at Mr. Lube."
        return f"Mail-in tire rebate{brand_phrase} at Mr. Lube."
    # financing only
    months_m = re.search(r"\b(\d+)\s*months?\b", text, re.IGNORECASE)
    months = months_m.group(1) if months_m else None
    if months:
        return f"Tire financing — no payments for {months} months at Mr. Lube."
    return f"Tire financing available at Mr. Lube."


# ---------------------------------------------------------------------------
# Text-block candidates
# ---------------------------------------------------------------------------
def _extract_text_candidates(html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()

    candidates: List[Dict] = []
    seen_blocks: set = set()

    class_pat = re.compile(
        r"(?:coupon|offer|promo|rebate|special|deal|saving|sale|banner|"
        r"financing|tire-?rebate|hero)",
        re.IGNORECASE,
    )

    def _ok_block(text: str, *, body: str = "") -> bool:
        if not text or len(text) < 25:
            return False
        if _FORM_CTA_PATTERNS.search(text) and len(text) < 100:
            return False
        if _DISCLAIMER_PATTERNS.search(text) and not re.search(
            r"\$\s*\d+\s*off|\d+\s*%\s*off|\bsave\s+\$?\d|\bfinancing\b|\brebate\b",
            text, re.IGNORECASE,
        ):
            return False
        if not _OFFER_SIGNAL.search(text):
            return False
        # Body-level sanity check: when a body is provided, require it to
        # carry the offer signal too. A title-only hit ("Tire Rebates &
        # Financing" + store-locator widget body) is not a real offer.
        if body:
            if _body_is_ui_noise(body):
                return False
            if not _OFFER_SIGNAL.search(body):
                return False
        return True

    for el in soup.find_all(["div", "section", "article", "li"], class_=class_pat):
        if el.find(["form", "input", "textarea"]):
            continue
        text = _clean(el.get_text(" ", strip=True))
        h = el.find(["h1", "h2", "h3", "h4", "strong"])
        title = _clean(h.get_text(" ", strip=True)) if h else ""
        # Body = full text minus the title, so we can require offer signal
        # in the body specifically (the title alone is not enough).
        body_only = text
        if title and text.lower().startswith(title.lower()):
            body_only = text[len(title):].strip()
        if not _ok_block(text, body=body_only):
            continue
        block_id = hash(text[:300])
        if block_id in seen_blocks:
            continue
        seen_blocks.add(block_id)
        if not title:
            title = _extract_title(text)
        candidates.append({
            "title": title,
            "body": text[:1500],
            "raw_text": text[:2500],
            "method": "text_card",
        })

    if not candidates:
        for h in soup.find_all(["h1", "h2", "h3"]):
            title = _clean(h.get_text(" ", strip=True))
            if not title:
                continue
            body_parts: List[str] = []
            for sib in h.find_all_next(limit=12):
                if sib.name in {"h1", "h2", "h3"}:
                    break
                t = _clean(sib.get_text(" ", strip=True))
                if t:
                    body_parts.append(t)
                if sum(len(p) for p in body_parts) > 700:
                    break
            body = " ".join(body_parts)[:1500]
            combined = (title + " " + body).strip()
            if not _ok_block(combined, body=body):
                continue
            block_id = hash(combined[:300])
            if block_id in seen_blocks:
                continue
            seen_blocks.add(block_id)
            candidates.append({
                "title": title[:160],
                "body": body,
                "raw_text": combined[:2500],
                "method": "text_heading",
            })

    return candidates


# ---------------------------------------------------------------------------
# Image candidates
# ---------------------------------------------------------------------------
def _collect_page_images(html: str, page_url: str, extra: List[str]) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["header", "footer", "nav"]):
        tag.decompose()

    found: Dict[str, Dict] = {}
    for img in soup.find_all("img"):
        src = (
            img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            or img.get("data-original") or ""
        )
        if not src or src.startswith("data:"):
            continue
        url = normalize_url(page_url, src)
        if not url or _UI_IMAGE_SKIP.search(url):
            continue
        if not re.search(r"\.(?:jpe?g|png|webp)(?:\?|$)", url, re.IGNORECASE):
            continue
        alt = (img.get("alt") or "").strip()
        cls = " ".join(img.get("class") or [])
        parent_cls = " ".join((img.parent.get("class") or []) if img.parent else [])
        hint_blob = " ".join([url, alt, cls, parent_cls])
        hinted = bool(_PROMO_IMAGE_HINTS.search(hint_blob))
        found.setdefault(url, {"url": url, "alt": alt, "hinted": hinted})

    for url in extra or []:
        if not url or _UI_IMAGE_SKIP.search(url):
            continue
        if not re.search(r"\.(?:jpe?g|png|webp)(?:\?|$)", url, re.IGNORECASE):
            continue
        found.setdefault(url, {"url": url, "alt": "", "hinted": False})

    hinted = [v for v in found.values() if v["hinted"]]
    rest = [v for v in found.values() if not v["hinted"]]
    return hinted + rest[: max(0, 12 - len(hinted))]


# ---------------------------------------------------------------------------
# Service hint refinement
# ---------------------------------------------------------------------------
_LIKELY_CATEGORIES = {"Tire Sales", "Tire Rotation", "Oil Change", "Other"}


def _refine_service(service_hint: str, text: str) -> str:
    if not text:
        return service_hint or _DEFAULT_SERVICE_HINT
    text_lc = text.lower()

    if re.search(r"\btire\s+rotation\b|\brotate\s+tires?\b", text_lc):
        return "Tire Rotation"
    if re.search(r"\boil\s+change\b", text_lc):
        return "Oil Change"
    if re.search(r"\btires?\b|\brebate\b|\bfinancing\b", text_lc):
        return "Tire Sales"

    classified = classify_service(text)
    if classified in _LIKELY_CATEGORIES:
        return classified
    return service_hint or _DEFAULT_SERVICE_HINT


# ---------------------------------------------------------------------------
# Row + signature
# ---------------------------------------------------------------------------
def _signature_local(*, title: str, discount: Optional[str], expiry: Optional[str],
                     service: str, page_url: str) -> str:
    d = _normalize_discount(discount) or "none"
    e = (expiry or "").strip()
    t = _signature_meaningful_tokens((title or "").lower())
    return f"u={page_url}|s={service}|d={d}|e={e}|t={t}"


def _signature_cross_url(*, title: str, discount: Optional[str], expiry: Optional[str],
                         service: str) -> str:
    d = _normalize_discount(discount) or "none"
    e = (expiry or "").strip()
    t = _signature_meaningful_tokens((title or "").lower())
    return f"s={service}|d={d}|e={e}|t={t}"


def _build_row(
    *,
    page_url: str,
    city: str,
    service: str,
    title: str,
    offer_details: str,
    raw_text: str,
    discount: Optional[str],
    code: Optional[str],
    expiry: Optional[str],
    extraction_method: str,
    source_image: Optional[str],
    promo_description: str,
    needs_review_reason: Optional[str],
) -> Dict:
    row: Dict = {
        # Sheet-compatible columns first
        "website": _WEBSITE,
        "page_url": page_url,
        "business_name": _BUSINESS_NAME,
        "google_reviews": "",
        "service_name": service,
        "promo_description": promo_description,
        "category": service,
        "contact": "National",
        "location": "National",
        "offer_details": offer_details[:1000],
        "ad_title": title,
        "ad_text": (raw_text or "")[:500],
        "new_or_updated": "new",
        "date_scraped": datetime.now().isoformat(),
        # QA / meta columns
        "city": city,
        "store_name": "National",
        "source_scope": "national_shared",
        "extraction_method": extraction_method,
        "confidence": None,
        "needs_review": bool(needs_review_reason),
        "needs_review_reason": needs_review_reason or "",
        "discount_value": discount,
        "coupon_code": code,
        "expiry_date": expiry,
        "promotion_title": title,
        "normalized_title": re.sub(r"\s+", " ", (title or "").lower().strip()),
        "applicable_cities": list(_TARGET_CITIES),
        "duplicate_group_id": None,
        "duplicate_group_total": 0,
        "source_image": source_image or "",
    }
    row["confidence"] = _confidence_from_promo(row)
    return row


# ---------------------------------------------------------------------------
# Per-page scraper
# ---------------------------------------------------------------------------
def _scrape_one_page(
    *,
    url: str,
    service_hint: str,
    excluded_log: List[Dict],
    ocr_cache: Dict[str, str],
    enable_ocr: bool = True,
) -> Dict:
    logger.info(f"[mrlube-v2] Fetching {service_hint} | {url}")
    html, fc_images = _fetch_page(url)
    if not html:
        return {
            "url": url, "status": "fetch_failed", "rows": [],
            "excluded": 0, "cards_on_page": 0,
            "text_extracted_count": 0, "image_ocr_extracted_count": 0,
            "image_ocr_failed_needs_review_count": 0,
            "ocr_attempted": 0, "ocr_success": 0, "ocr_failed": 0,
            "service_hint": service_hint,
        }

    rows: List[Dict] = []
    excluded_here = 0
    seen_local: set = set()
    text_count = 0
    image_count = 0
    image_failed_count = 0
    ocr_attempted = 0
    ocr_success = 0
    ocr_failed = 0

    # ---- Text candidates --------------------------------------------------
    for cand in _extract_text_candidates(html):
        raw_text = cand["raw_text"]
        generic_title = cand["title"] or _extract_title(raw_text)
        title = _better_offer_title(raw_text, generic_title)
        body = cand["body"]

        if not _OFFER_SIGNAL.search(raw_text):
            excluded_here += 1
            excluded_log.append({
                "url": url, "scope": "national_shared",
                "extraction_method": "text",
                "reason": "no_offer_signal", "raw_text": raw_text[:240],
            })
            continue

        service = _refine_service(service_hint, raw_text)
        discount = _v2_extract_discount(raw_text)
        code = _v2_extract_coupon_code(raw_text)
        expiry = _extract_expiry(raw_text)

        sig = _signature_local(
            title=title, discount=discount, expiry=expiry,
            service=service, page_url=url,
        )
        if sig in seen_local:
            continue
        seen_local.add(sig)

        summary = _summarize_promo_description(
            promotion_title=title,
            offer_details=body,
            discount=discount,
            code=code,
            std_service=service,
            ad_text=raw_text,
            brand="Mr. Lube",
        )
        summary = _refine_mrlube_summary(
            summary, raw_text=raw_text, discount=discount, service=service,
        )

        cross_sig = _signature_cross_url(
            title=title, discount=discount, expiry=expiry, service=service,
        )
        for city in _TARGET_CITIES:
            row = _build_row(
                page_url=url, city=city, service=service,
                title=title, offer_details=body, raw_text=raw_text,
                discount=discount, code=code, expiry=expiry,
                extraction_method="text", source_image=None,
                promo_description=summary, needs_review_reason=None,
            )
            row["_signature_base"] = cross_sig
            rows.append(row)
        text_count += 1

    # ---- Image OCR candidates ---------------------------------------------
    if enable_ocr:
        for img in _collect_page_images(html, url, fc_images):
            img_url = img["url"]
            ocr_attempted += 1
            ocr_text = _ocr_url(img_url, referer=url, ocr_cache=ocr_cache)
            if not ocr_text or len(ocr_text.strip()) < 8:
                ocr_failed += 1
                if img["hinted"]:
                    image_failed_count += 1
                    cross_sig = _signature_cross_url(
                        title=img_url, discount=None, expiry=None,
                        service=service_hint,
                    )
                    for city in _TARGET_CITIES:
                        row = _build_row(
                            page_url=url, city=city, service=service_hint,
                            title="(image-only offer, OCR failed)",
                            offer_details="", raw_text="",
                            discount=None, code=None, expiry=None,
                            extraction_method="image_ocr_failed",
                            source_image=img_url,
                            promo_description="",
                            needs_review_reason="image_ocr_failed",
                        )
                        row["_signature_base"] = cross_sig
                        rows.append(row)
                else:
                    excluded_here += 1
                    excluded_log.append({
                        "url": url, "scope": "national_shared",
                        "extraction_method": "image_ocr",
                        "reason": "no_ocr_text",
                        "source_image": img_url, "raw_text": "",
                    })
                continue

            ocr_success += 1
            if not _OFFER_SIGNAL.search(ocr_text):
                excluded_here += 1
                excluded_log.append({
                    "url": url, "scope": "national_shared",
                    "extraction_method": "image_ocr",
                    "reason": "no_offer_signal_in_ocr",
                    "source_image": img_url,
                    "raw_text": ocr_text[:300],
                })
                continue

            title = _title_from_ocr(ocr_text, _extract_title(ocr_text))
            service = _refine_service(service_hint, ocr_text)
            discount = _v2_extract_discount(ocr_text)
            code = _v2_extract_coupon_code(ocr_text)
            expiry = _extract_expiry(ocr_text)

            sig = _signature_local(
                title=title, discount=discount, expiry=expiry,
                service=service, page_url=url,
            )
            if sig in seen_local:
                continue
            seen_local.add(sig)

            summary = _summarize_promo_description(
                promotion_title=title,
                offer_details=ocr_text[:1000],
                discount=discount,
                code=code,
                std_service=service,
                ad_text=ocr_text,
                brand="Mr. Lube",
            )
            summary = _refine_mrlube_summary(
                summary, raw_text=ocr_text, discount=discount, service=service,
            )

            cross_sig = _signature_cross_url(
                title=title, discount=discount, expiry=expiry, service=service,
            )
            for city in _TARGET_CITIES:
                row = _build_row(
                    page_url=url, city=city, service=service,
                    title=title, offer_details=ocr_text[:1000],
                    raw_text=ocr_text,
                    discount=discount, code=code, expiry=expiry,
                    extraction_method="image_ocr", source_image=img_url,
                    promo_description=summary, needs_review_reason=None,
                )
                row["_signature_base"] = cross_sig
                rows.append(row)
            image_count += 1

    cards = text_count + image_count
    logger.info(
        f"[mrlube-v2] {url}: text={text_count} images={image_count} "
        f"excluded={excluded_here} ocr_attempted={ocr_attempted} "
        f"ocr_success={ocr_success} ocr_failed={ocr_failed}"
    )

    return {
        "url": url, "status": "ok", "rows": rows,
        "excluded": excluded_here, "cards_on_page": cards,
        "text_extracted_count": text_count,
        "image_ocr_extracted_count": image_count,
        "image_ocr_failed_needs_review_count": image_failed_count,
        "ocr_attempted": ocr_attempted,
        "ocr_success": ocr_success,
        "ocr_failed": ocr_failed,
        "service_hint": service_hint,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def scrape_mrlube_v2(
    competitor_v2: Dict,
    *,
    mode: str = "qa_expanded",
    enable_ocr: bool = True,
) -> Dict:
    """Scrape the Mr. Lube + Tires shared rebates/financing page and fan
    valid offers out to Calgary and Edmonton.

    Args:
        competitor_v2: Entry from ``app/config/competitors.v2.json``.
        mode: ``"qa_expanded"`` (default) or ``"final_deduped"``.
        enable_ocr: When False, skip image OCR (text-only smoke test).
    """
    if mode not in ("qa_expanded", "final_deduped"):
        raise ValueError(f"mode must be qa_expanded or final_deduped, got {mode!r}")

    competitor_name = competitor_v2.get("competitor", _BUSINESS_NAME)
    all_rows: List[Dict] = []
    url_log: List[Dict] = []
    excluded_log: List[Dict] = []
    expected_urls: List[str] = []
    ocr_cache: Dict[str, str] = {}

    for link in competitor_v2.get("promo_links", []):
        if isinstance(link, dict):
            url = link["url"]
            hint = link.get("service_hint") or _DEFAULT_SERVICE_HINT
        else:
            url = link
            hint = _DEFAULT_SERVICE_HINT
        expected_urls.append(url)

        res = _scrape_one_page(
            url=url, service_hint=hint,
            excluded_log=excluded_log, ocr_cache=ocr_cache,
            enable_ocr=enable_ocr,
        )
        all_rows.extend(res["rows"])
        url_log.append({
            "url": url, "scope": "national_shared",
            "service_hint": hint,
            "status": res["status"],
            "cards_on_page": res.get("cards_on_page", 0),
            "added_rows": len(res["rows"]),
            "excluded_count": res.get("excluded", 0),
            "text_extracted_count": res.get("text_extracted_count", 0),
            "image_ocr_extracted_count": res.get("image_ocr_extracted_count", 0),
            "image_ocr_failed_needs_review_count":
                res.get("image_ocr_failed_needs_review_count", 0),
            "ocr_attempted": res.get("ocr_attempted", 0),
            "ocr_success": res.get("ocr_success", 0),
            "ocr_failed": res.get("ocr_failed", 0),
        })

    # Strict service taxonomy: drop anything that resolved to "Other" (or any
    # other label outside the allowed 8). Log the drops as exclusions so QA
    # can see what was thrown away.
    kept_rows: List[Dict] = []
    for r in all_rows:
        if r.get("service_name") in _ALLOWED_SERVICES:
            kept_rows.append(r)
        else:
            excluded_log.append({
                "url": r.get("page_url", ""),
                "scope": "national_shared",
                "extraction_method": r.get("extraction_method", ""),
                "reason": "service_outside_taxonomy",
                "source_image": r.get("source_image", ""),
                "raw_text": (r.get("ad_text") or "")[:300],
            })
    all_rows = kept_rows

    # duplicate_group_id / total via cross-URL signature.
    sig_to_group: Dict[str, str] = {}
    sig_counts: Dict[str, int] = {}
    for r in all_rows:
        sig = r["_signature_base"]
        sig_counts[sig] = sig_counts.get(sig, 0) + 1
        if sig not in sig_to_group:
            sig_to_group[sig] = f"mrlube-{len(sig_to_group)+1:03d}"
    for r in all_rows:
        sig = r.pop("_signature_base")
        r["duplicate_group_id"] = sig_to_group[sig]
        r["duplicate_group_total"] = sig_counts[sig]

    if mode == "final_deduped":
        kept: List[Dict] = []
        seen: set = set()
        for r in all_rows:
            key = (r["duplicate_group_id"], r["city"])
            if key in seen:
                continue
            seen.add(key)
            kept.append(r)
        all_rows = kept

    # ---- Validation -------------------------------------------------------
    processed = {e["url"] for e in url_log if e["status"] == "ok"}
    failed = [e["url"] for e in url_log if e["status"] == "fetch_failed"]
    missing = sorted(set(expected_urls) - {e["url"] for e in url_log})

    row_count_by_url: Dict[str, int] = {}
    row_count_by_city: Dict[str, int] = {}
    svc_counts: Dict[str, int] = {}
    method_counts: Dict[str, int] = {}
    for r in all_rows:
        row_count_by_url[r["page_url"]] = row_count_by_url.get(r["page_url"], 0) + 1
        c = r.get("city") or ""
        row_count_by_city[c] = row_count_by_city.get(c, 0) + 1
        s = r.get("service_name") or ""
        svc_counts[s] = svc_counts.get(s, 0) + 1
        m = r.get("extraction_method") or ""
        method_counts[m] = method_counts.get(m, 0) + 1

    excl_reason_counts: Dict[str, int] = {}
    for x in excluded_log:
        excl_reason_counts[x["reason"]] = excl_reason_counts.get(x["reason"], 0) + 1

    unique_descs = sorted({
        (r.get("promo_description") or "").strip()
        for r in all_rows
        if r.get("promo_description")
    })

    ocr_attempted = sum(e.get("ocr_attempted", 0) for e in url_log)
    ocr_success = sum(e.get("ocr_success", 0) for e in url_log)
    ocr_failed = sum(e.get("ocr_failed", 0) for e in url_log)

    result = {
        "competitor": competitor_name,
        "scraped_at": datetime.now().isoformat(),
        "config_version": "v2",
        "mode": mode,
        "promotions": all_rows,
        "count": len(all_rows),
        "needs_review_count": sum(1 for r in all_rows if r.get("needs_review")),
        "by_city": row_count_by_city,
        "validation": {
            "expected_url_count": len(expected_urls),
            "processed_url_count": len(processed),
            "failed_url_count": len(failed),
            "failed_urls": failed,
            "missing_urls": missing,
            "row_count_by_url": row_count_by_url,
            "row_count_by_city": row_count_by_city,
            "needs_review_count": sum(1 for r in all_rows if r.get("needs_review")),
            "excluded_row_count": len(excluded_log),
            "excluded_reason_counts": excl_reason_counts,
            "extraction_method_counts": method_counts,
            "service_count_by_category": svc_counts,
            "unique_promo_descriptions": unique_descs,
            "duplicate_group_total": len(sig_to_group),
            "ocr_attempted": ocr_attempted,
            "ocr_success": ocr_success,
            "ocr_failed": ocr_failed,
            "url_log": url_log,
            "excluded_rows": excluded_log,
        },
    }

    output_file = PROMOTIONS_DIR / "mrlube_v2.json"
    output_file.write_text(json.dumps(result, indent=2, default=str))
    logger.info(f"[mrlube-v2|{mode}] Saved {len(all_rows)} rows to {output_file}")
    return result
