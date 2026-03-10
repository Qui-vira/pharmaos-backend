"""
PharmaOS AI - Product Import Service (v3)
Handles CSV and Excel bulk imports for:
- Global product catalog seeding (NAFDAC Greenbook, EMDEX)
- Distributor catalog uploads
- Pharmacy inventory migration

Expected CSV/Excel columns:
  product_name (required)
  generic_name (required)
  brand_name
  dosage_form
  strength
  manufacturer
  nafdac_number
  category
  barcode (EAN-13, 13 digits)
  unit_price (for supplier uploads)
  quantity (for supplier/inventory uploads)
"""

import csv
import io
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Optional

from app.utils.helpers import normalize_product_name

logger = logging.getLogger(__name__)


def validate_ean13(barcode: str) -> bool:
    """Validate EAN-13 barcode: must be exactly 13 digits."""
    if not barcode:
        return True  # Optional field
    return bool(re.match(r"^\d{13}$", barcode.strip()))


def parse_csv_content(content: str) -> list[dict]:
    """Parse CSV/TSV string into list of row dicts. Auto-detects delimiter."""
    # Auto-detect delimiter (tab vs comma)
    first_line = content.split("\n")[0] if content else ""
    delimiter = "\t" if "\t" in first_line else ","

    reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
    rows = []
    for i, row in enumerate(reader):
        # Normalize keys (strip whitespace, lowercase) — keep empty values as empty string
        clean = {}
        for k, v in row.items():
            if k:
                key = k.strip().lower().replace(" ", "_")
                clean[key] = v.strip() if v else ""
        clean["_row_number"] = i + 2
        if any(v for k, v in clean.items() if k != "_row_number"):  # Skip completely empty rows
            rows.append(clean)
    return rows


def parse_excel_content(file_bytes: bytes) -> list[dict]:
    """Parse Excel bytes into list of row dicts."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True)
        ws = wb.active

        headers = []
        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:
                headers = [str(h).strip().lower().replace(" ", "_") if h else f"col_{j}" for j, h in enumerate(row)]
                continue
            clean = {}
            for j, val in enumerate(row):
                if j < len(headers) and val is not None:
                    clean[headers[j]] = str(val).strip()
            if clean:
                clean["_row_number"] = i + 1
                rows.append(clean)

        wb.close()
        return rows
    except ImportError:
        logger.error("openpyxl not installed — cannot parse Excel files")
        return []
    except Exception as e:
        logger.error(f"Excel parse error: {e}")
        return []


def validate_product_row(row: dict) -> tuple[bool, Optional[str]]:
    """Validate a single product row. Returns (is_valid, error_message)."""
    row_num = row.get("_row_number", "?")

    # Required fields
    name = row.get("product_name") or row.get("display_name") or row.get("name")
    generic = row.get("generic_name") or row.get("generic")
    if not name:
        return False, f"Row {row_num}: Missing product_name or display_name"
    if not generic:
        return False, f"Row {row_num}: Missing generic_name"

    # Barcode validation
    barcode = row.get("barcode", "")
    if barcode and not validate_ean13(barcode):
        return False, f"Row {row_num}: Invalid barcode '{barcode}' — must be 13 digits (EAN-13)"

    # Price validation
    price = row.get("unit_price") or row.get("price")
    if price:
        try:
            Decimal(price)
        except (InvalidOperation, ValueError):
            return False, f"Row {row_num}: Invalid price '{price}'"

    # Quantity validation
    qty = row.get("quantity") or row.get("qty") or row.get("stock")
    if qty:
        try:
            int(qty)
        except ValueError:
            return False, f"Row {row_num}: Invalid quantity '{qty}'"

    return True, None


def _trunc(value: str, max_len: int) -> str:
    """Truncate string to max length for database varchar fields."""
    if not value:
        return value
    return value[:max_len] if len(value) > max_len else value


def extract_product_data(row: dict) -> dict:
    """Extract and normalize product data from a row. Handles NAFDAC Greenbook format."""
    name = row.get("product_name") or row.get("display_name") or row.get("name", "")
    generic = row.get("generic_name") or row.get("generic", name)

    # Use normalized_name from file if provided, otherwise generate
    norm_name = row.get("normalized_name") or normalize_product_name(name)

    # Handle barcode — only keep if valid EAN-13
    barcode = row.get("barcode", "").strip()
    if barcode and not validate_ean13(barcode):
        barcode = None

    # Handle requires_prescription (could be true/false/yes/no/1/0)
    rx_val = row.get("requires_prescription", "").lower().strip()
    requires_rx = rx_val in ("true", "yes", "1", "t")

    return {
        "name": _trunc(name, 255),
        "generic_name": _trunc(generic, 255),
        "brand_name": _trunc(row.get("brand_name") or row.get("brand") or "", 255) or None,
        "dosage_form": _trunc(row.get("dosage_form") or row.get("form") or "", 100) or None,
        "strength": _trunc(row.get("strength") or row.get("dose") or "", 100) or None,
        "manufacturer": _trunc(row.get("manufacturer") or row.get("mfg") or "", 255) or None,
        "nafdac_number": _trunc(row.get("nafdac_number") or row.get("nafdac") or "", 100) or None,
        "category": _trunc(row.get("category") or "", 100) or None,
        "barcode": barcode or None,
        "normalized_name": _trunc(norm_name, 255) or None,
        "requires_prescription": requires_rx,
    }


def extract_supplier_data(row: dict) -> dict:
    """Extract supplier-specific data (price, quantity)."""
    price_str = row.get("unit_price") or row.get("price") or "0"
    qty_str = row.get("quantity") or row.get("qty") or row.get("stock") or "0"

    try:
        price = Decimal(price_str)
    except (InvalidOperation, ValueError):
        price = Decimal("0")

    try:
        quantity = int(qty_str)
    except ValueError:
        quantity = 0

    return {
        "unit_price": price,
        "quantity": quantity,
    }


def process_import(
    rows: list[dict],
    import_type: str = "catalog",
) -> dict:
    """
    Process imported rows and return results summary.
    import_type: "catalog" (global products), "supplier" (supplier catalog), "inventory" (pharmacy stock)
    
    Returns: {
        "valid_rows": [...],
        "errors": [...],
        "total": N,
        "valid_count": N,
        "error_count": N,
    }
    """
    valid_rows = []
    errors = []

    for row in rows:
        is_valid, error = validate_product_row(row)
        if not is_valid:
            errors.append(error)
            continue

        product_data = extract_product_data(row)

        if import_type in ("supplier", "inventory"):
            product_data.update(extract_supplier_data(row))

        valid_rows.append(product_data)

    return {
        "valid_rows": valid_rows,
        "errors": errors,
        "total": len(rows),
        "valid_count": len(valid_rows),
        "error_count": len(errors),
    }
