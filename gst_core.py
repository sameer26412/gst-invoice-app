"""Core GST calculations and invoice helpers for the local invoice app."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

UTS_WITHOUT_LEGISLATURE = {
    "Andaman and Nicobar Islands", "Chandigarh",
    "Dadra and Nagar Haveli and Daman and Diu", "Ladakh", "Lakshadweep",
}


def money(value: Any) -> Decimal:
    """Convert a value to a 2-decimal Decimal using commercial rounding."""
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def indian_number(value: Any) -> str:
    """Format a number with Indian digit grouping, e.g. 12,34,567.89."""
    amount = money(value)
    sign = "-" if amount < 0 else ""
    raw = f"{abs(amount):.2f}"
    whole, fraction = raw.split(".")
    if len(whole) <= 3:
        grouped = whole
    else:
        last_three = whole[-3:]
        rest = whole[:-3]
        groups: list[str] = []
        while rest:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        grouped = ",".join(groups + [last_three])
    return f"{sign}{grouped}.{fraction}"


def calculate_invoice(items: list[dict[str, Any]], seller_state: str, buyer_state: str) -> dict[str, Any]:
    """Calculate line-level GST and invoice totals.

    Rates are applied to the entered unit price (prices are exclusive of GST).
    Same-state supply splits GST equally into CGST and SGST; inter-state supply
    applies the full rate as IGST.
    """
    intra_state = (seller_state or "").strip().casefold() == (buyer_state or "").strip().casefold()
    calculated: list[dict[str, Any]] = []
    taxable_total = Decimal("0.00")
    cgst_total = Decimal("0.00")
    sgst_total = Decimal("0.00")
    utgst_total = Decimal("0.00")
    igst_total = Decimal("0.00")
    grand_total = Decimal("0.00")

    for raw in items:
        description = str(raw.get("description", "")).strip()
        if not description:
            continue
        qty = Decimal(str(raw.get("quantity", 0) or 0))
        raw_unit_price = Decimal(str(raw.get("unit_price", 0) or 0))
        rate = Decimal(str(raw.get("gst_rate", 0) or 0))
        if not qty.is_finite() or qty <= 0:
            raise ValueError(f"Quantity must be greater than zero for '{description}'.")
        if not raw_unit_price.is_finite() or raw_unit_price < 0:
            raise ValueError(f"Unit price cannot be negative for '{description}'.")
        if not rate.is_finite() or rate < 0 or rate > 100:
            raise ValueError(f"Enter a valid GST rate between 0 and 100 for '{description}'.")
        unit_price = money(raw_unit_price)

        taxable = money(qty * unit_price)
        tax = money(taxable * rate / Decimal("100"))
        cgst = money(tax / 2) if intra_state else Decimal("0.00")
        sgst = money(tax - cgst) if intra_state and buyer_state not in UTS_WITHOUT_LEGISLATURE else Decimal("0.00")
        utgst = money(tax - cgst) if intra_state and buyer_state in UTS_WITHOUT_LEGISLATURE else Decimal("0.00")
        igst = tax if not intra_state else Decimal("0.00")
        line_total = money(taxable + cgst + sgst + utgst + igst)

        calculated.append({
            "description": description,
            "hsn": str(raw.get("hsn", "")).strip(),
            "uqc": str(raw.get("uqc", "NOS") or "NOS").strip(),
            "quantity": qty,
            "unit_price": unit_price,
            "gst_rate": rate,
            "taxable": taxable,
            "cgst": cgst,
            "sgst": sgst,
            "utgst": utgst,
            "igst": igst,
            "total": line_total,
        })
        taxable_total += taxable
        cgst_total += cgst
        sgst_total += sgst
        utgst_total += utgst
        igst_total += igst
        grand_total += line_total

    if not calculated:
        raise ValueError("Add at least one invoice item with a description.")

    return {
        "items": calculated,
        "intra_state": intra_state,
        "taxable_total": money(taxable_total),
        "cgst_total": money(cgst_total),
        "sgst_total": money(sgst_total),
        "utgst_total": money(utgst_total),
        "igst_total": money(igst_total),
        "gst_total": money(cgst_total + sgst_total + utgst_total + igst_total),
        "grand_total": money(grand_total),
    }
