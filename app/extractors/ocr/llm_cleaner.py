"""LLM-based text cleaning for OCR output."""
import os
import requests
import json
from typing import Optional
from app.config.constants import PERPLEXITY_API_KEY
from app.utils.logging_utils import setup_logger

logger = setup_logger(__name__)

PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"


def clean_promo_text_with_llm(ocr_text: str, context: str = "") -> Optional[str]:
    """Clean and extract structured promo information using Perplexity LLM."""
    if not PERPLEXITY_API_KEY:
        logger.warning("PERPLEXITY_API_KEY not set, skipping LLM cleaning")
        return None
    
    if not ocr_text or len(ocr_text.strip()) < 10:
        return None
    
    try:
        prompt = f"""Extract promotion details from this OCR text from an automotive service coupon image. 
Return ONLY a clean JSON object with these fields:
{{
    "service_name": "oil change/brake/battery/etc",
    "promo_description": "clean description",
    "discount_value": "$X or X% or free",
    "coupon_code": "code if present",
    "expiry_date": "date if present",
    "category": "oil change/brakes/battery/seasonal/etc"
}}

OCR Text:
{ocr_text}

Context: {context}

Return only the JSON, no other text."""

        headers = {
            "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
            "Content-Type": "application/json"
        }
        
        # Try multiple model name variations if one fails
        model_options = [
            "llama-3.1-sonar-small-128k-online",
            "llama-3.1-sonar-small",
            "sonar-small",
            "llama-3-sonar-small",
            "sonar"
        ]
        
        last_error = None
        for model_name in model_options:
            try:
                data = {
                    "model": model_name,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a helpful assistant that extracts structured promotion data from OCR text. Return only valid JSON."
                        },
                        {
                            "role": "user",
                            "content": prompt
                        }
                    ],
                    "temperature": 0.1,
                    "max_tokens": 500
                }
                
                response = requests.post(PERPLEXITY_API_URL, headers=headers, json=data, timeout=30)
                
                if response.ok:
                    # Success, break and process response
                    break
                else:
                    error_data = response.json()
                    if "invalid_model" in str(error_data):
                        last_error = error_data
                        continue  # Try next model
                    else:
                        # Other error, break and raise
                        last_error = error_data
                        break
            except Exception as e:
                last_error = str(e)
                continue
        
        # If we got here without a successful response, raise error
        if not response.ok:
            error_detail = response.text if hasattr(response, 'text') else str(last_error)
            logger.error(f"Perplexity API error {response.status_code}: {error_detail[:200]}")
            response.raise_for_status()
        
        result = response.json()
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        
        # Extract JSON from response
        content = content.strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
        
        # Try to parse JSON
        try:
            parsed = json.loads(content)
            logger.debug(f"LLM cleaned promo text: {parsed}")
            return parsed
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse LLM response as JSON: {content[:100]}")
            return None
            
    except Exception as e:
        logger.error(f"Error cleaning text with LLM: {e}")
        return None

