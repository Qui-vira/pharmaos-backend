"""
PharmaOS AI - Vision Service (Snap to Stock)
Uses OpenAI GPT-4 Vision API via httpx (same pattern as ai_provider.py).
"""

import json
import logging
from typing import Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

SHELF_ANALYSIS_PROMPT = """You are a pharmaceutical product recognition system for PharmaOS.
Analyze this image of a pharmacy shelf or product packaging.

Extract ALL visible products with:
- product_name: Full product name as printed on packaging
- strength: Dosage strength if visible (e.g., "500mg", "250mg/5ml")
- manufacturer: Manufacturer name if visible
- batch_number: Batch/lot number if visible
- expiry_date: Expiry date if visible (format: YYYY-MM-DD)

Respond ONLY with JSON:
{
    "products": [
        {
            "product_name": "Paracetamol Tablets",
            "strength": "500mg",
            "manufacturer": "Emzor Pharmaceuticals",
            "batch_number": null,
            "expiry_date": null
        }
    ]
}

Focus on Nigerian pharmaceutical products. If text is partially obscured, make your best guess and note uncertainty."""


async def analyze_shelf_image(image_base64: str, mime_type: str = "image/jpeg") -> list[dict]:
    """
    Send a shelf/product image to OpenAI Vision API and extract product info.
    Returns a list of extracted product dicts.
    """
    api_key = settings.LLM_API_KEY
    if not api_key:
        logger.warning("LLM API key not configured for vision analysis")
        return []

    payload = {
        "model": "gpt-4o",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": SHELF_ANALYSIS_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{image_base64}",
                            "detail": "high",
                        },
                    },
                ],
            }
        ],
        "temperature": 0.1,
        "max_tokens": 2000,
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]

            parsed = json.loads(content)
            return parsed.get("products", [])
    except json.JSONDecodeError:
        logger.error("Failed to parse vision API response as JSON")
        return []
    except Exception as e:
        logger.error(f"Vision API error: {e}")
        return []


async def match_products(extracted_names: list[str], db) -> list[dict]:
    """
    Match extracted product names against the Product table using ILIKE.
    Returns list of {extracted_name, matched_product, confidence} dicts.
    """
    from sqlalchemy import select, or_
    from app.models.models import Product

    results = []
    for name in extracted_names:
        search_term = f"%{name.strip()}%"

        query = select(Product).where(
            or_(
                Product.name.ilike(search_term),
                Product.generic_name.ilike(search_term),
            )
        ).limit(5)

        matched = (await db.execute(query)).scalars().all()

        if matched:
            best = matched[0]
            confidence = "high" if name.lower() in best.name.lower() else "medium"
            results.append({
                "extracted_name": name,
                "matched_product_id": str(best.id),
                "matched_product_name": best.name,
                "generic_name": best.generic_name,
                "strength": best.strength,
                "manufacturer": best.manufacturer,
                "confidence": confidence,
                "alternatives": [
                    {
                        "product_id": str(p.id),
                        "name": p.name,
                        "generic_name": p.generic_name,
                        "strength": p.strength,
                    }
                    for p in matched[1:]
                ],
            })
        else:
            results.append({
                "extracted_name": name,
                "matched_product_id": None,
                "matched_product_name": None,
                "confidence": "none",
                "alternatives": [],
            })

    return results
