"""
PharmaOS AI - Utility Functions
Order number generation, product name normalization, pagination helpers.
"""

import re
import random
import string
from typing import TypeVar, Generic, List, Optional
from math import ceil

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession


def sanitize_like(value: str) -> str:
    """Escape special LIKE/ILIKE characters (%, _) in user input."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def generate_order_number() -> str:
    """Generate a human-readable order number: ORD-XXXXXX."""
    chars = string.ascii_uppercase + string.digits
    suffix = "".join(random.choices(chars, k=6))
    return f"ORD-{suffix}"


def normalize_product_name(name: str) -> str:
    """
    Normalize a drug product name for cross-supplier matching.

    Examples:
        "Paracetamol 500mg" → "paracetamol_500mg"
        "PCM 500 Tablets" → "pcm_500_tablets"
        "Emzor Paracetamol 500mg Tab" → "emzor_paracetamol_500mg_tab"
    """
    if not name:
        return ""

    # Lowercase
    normalized = name.lower().strip()

    # Common abbreviation expansion
    abbreviations = {
        "pcm": "paracetamol",
        "amox": "amoxicillin",
        "tab": "tablet",
        "tabs": "tablets",
        "cap": "capsule",
        "caps": "capsules",
        "syr": "syrup",
        "inj": "injection",
        "susp": "suspension",
    }

    words = normalized.split()
    expanded = []
    for word in words:
        clean_word = re.sub(r"[^a-z0-9]", "", word)
        expanded.append(abbreviations.get(clean_word, clean_word))

    # Join with underscore, remove special chars
    result = "_".join(expanded)
    result = re.sub(r"[^a-z0-9_]", "", result)
    result = re.sub(r"_+", "_", result)
    return result.strip("_")


async def paginate(
    db: AsyncSession,
    query,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """
    Generic pagination helper for SQLAlchemy queries.
    Returns dict with items, total, page, page_size, pages.
    """
    page = max(1, page)
    page_size = min(max(1, page_size), 100)

    # Count total
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Fetch page
    offset = (page - 1) * page_size
    paginated_query = query.offset(offset).limit(page_size)
    result = await db.execute(paginated_query)
    items = result.scalars().all()

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": ceil(total / page_size) if total > 0 else 0,
    }
