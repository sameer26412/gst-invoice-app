from __future__ import annotations

import hmac
import html
import io
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import pandas as pd
import qrcode
import requests
import streamlit as st
from dotenv import load_dotenv
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from gst_core import calculate_invoice, indian_number, money

load_dotenv()

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = Path(os.getenv("DATABASE_PATH", str(DATA_DIR / "invoices.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

INDIAN_STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
    "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
    "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya", "Mizoram",
    "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu",
    "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
    "Andaman and Nicobar Islands", "Chandigarh", "Dadra and Nagar Haveli and Daman and Diu",
    "Delhi", "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry",
]


STATE_CODES = {
    "Jammu and Kashmir": "01", "Himachal Pradesh": "02", "Punjab": "03",
    "Chandigarh": "04", "Uttarakhand": "05", "Haryana": "06", "Delhi": "07",
    "Rajasthan": "08", "Uttar Pradesh": "09", "Bihar": "10", "Sikkim": "11",
    "Arunachal Pradesh": "12", "Nagaland": "13", "Manipur": "14", "Mizoram": "15",
    "Tripura": "16", "Meghalaya": "17", "Assam": "18", "West Bengal": "19",
    "Jharkhand": "20", "Odisha": "21", "Chhattisgarh": "22", "Madhya Pradesh": "23",
    "Gujarat": "24", "Dadra and Nagar Haveli and Daman and Diu": "26", "Maharashtra": "27",
    "Karnataka": "29", "Goa": "30", "Lakshadweep": "31", "Kerala": "32",
    "Tamil Nadu": "33", "Puducherry": "34", "Andaman and Nicobar Islands": "35",
    "Telangana": "36", "Andhra Pradesh": "37", "Ladakh": "38",
}

FALLBACK_LANGUAGES = {
    "English": "en", "Hindi": "hi", "Kannada": "kn", "Tamil": "ta",
    "Telugu": "te", "Malayalam": "ml", "Bengali": "bn", "Marathi": "mr",
    "Gujarati": "gu", "Punjabi": "pa", "German": "de", "French": "fr",
    "Spanish": "es", "Italian": "it", "Portuguese": "pt", "Arabic": "ar",
    "Japanese": "ja", "Chinese (Simplified)": "zh-CN", "Dutch": "nl",
    "Russian": "ru", "Turkish": "tr",
}

# ------------------------------ Database ----------------------------------

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_no TEXT NOT NULL UNIQUE,
                issue_date TEXT NOT NULL,
                due_date TEXT,
                customer_name TEXT NOT NULL,
                customer_phone TEXT NOT NULL,
                customer_email TEXT,
                customer_gstin TEXT,
                customer_address TEXT NOT NULL,
                customer_state TEXT NOT NULL,
                seller_name TEXT NOT NULL,
                seller_gstin TEXT,
                seller_address TEXT NOT NULL,
                seller_state TEXT NOT NULL,
                seller_phone TEXT,
                seller_email TEXT,
                taxable_total TEXT NOT NULL,
                cgst_total TEXT NOT NULL,
                sgst_total TEXT NOT NULL,
                utgst_total TEXT NOT NULL DEFAULT '0.00',
                igst_total TEXT NOT NULL,
                gst_total TEXT NOT NULL,
                grand_total TEXT NOT NULL,
                payment_status TEXT NOT NULL DEFAULT 'UNPAID',
                payment_preference TEXT,
                preference_updated_at TEXT,
                payment_method TEXT,
                paid_at TEXT,
                reverse_charge TEXT NOT NULL DEFAULT 'No',
                notes TEXT
            );
            CREATE TABLE IF NOT EXISTS invoice_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
                description TEXT NOT NULL,
                hsn TEXT,
                uqc TEXT NOT NULL DEFAULT 'NOS',
                quantity TEXT NOT NULL,
                unit_price TEXT NOT NULL,
                gst_rate TEXT NOT NULL,
                taxable TEXT NOT NULL,
                cgst TEXT NOT NULL,
                sgst TEXT NOT NULL,
                utgst TEXT NOT NULL DEFAULT '0.00',
                igst TEXT NOT NULL,
                line_total TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS preference_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
                preference TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        invoice_columns = {row[1] for row in conn.execute("PRAGMA table_info(invoices)").fetchall()}
        if "reverse_charge" not in invoice_columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN reverse_charge TEXT NOT NULL DEFAULT 'No'")
        if "utgst_total" not in invoice_columns:
            conn.execute("ALTER TABLE invoices ADD COLUMN utgst_total TEXT NOT NULL DEFAULT '0.00'")
        item_columns = {row[1] for row in conn.execute("PRAGMA table_info(invoice_items)").fetchall()}
        if "uqc" not in item_columns:
            conn.execute("ALTER TABLE invoice_items ADD COLUMN uqc TEXT NOT NULL DEFAULT 'NOS'")
        if "utgst" not in item_columns:
            conn.execute("ALTER TABLE invoice_items ADD COLUMN utgst TEXT NOT NULL DEFAULT '0.00'")


def get_settings() -> dict[str, str]:
    with db_connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def save_settings(values: dict[str, str]) -> None:
    with db_connect() as conn:
        conn.executemany(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [(key, str(value or "").strip()) for key, value in values.items()],
        )


def make_invoice_number(for_date: date | None = None) -> str:
    today = for_date or date.today()
    fy_start = today.year if today.month >= 4 else today.year - 1
    fy_label = f"{fy_start}-{str(fy_start + 1)[-2:]}"
    prefix = f"INV/{fy_label}/"
    with db_connect() as conn:
        row = conn.execute(
            "SELECT invoice_no FROM invoices WHERE invoice_no LIKE ? ORDER BY id DESC LIMIT 1",
            (prefix + "%",),
        ).fetchone()
    sequence = 1
    if row:
        match = re.search(r"/(\d+)$", row["invoice_no"])
        if match:
            sequence = int(match.group(1)) + 1
    # Rule 46 serial-number ceiling: 16 characters.
    invoice_no = f"{prefix}{sequence:04d}"
    if len(invoice_no) > 16:
        raise RuntimeError("Invoice sequence exceeded the 16-character serial-number limit.")
    return invoice_no


def insert_invoice(payload: dict[str, Any], totals: dict[str, Any]) -> int:
    with db_connect() as conn:
        cursor = conn.execute(
            """INSERT INTO invoices (
                invoice_no, issue_date, due_date, customer_name, customer_phone,
                customer_email, customer_gstin, customer_address, customer_state,
                seller_name, seller_gstin, seller_address, seller_state, seller_phone,
                seller_email, taxable_total, cgst_total, sgst_total, utgst_total, igst_total,
                gst_total, grand_total, reverse_charge, notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                payload["invoice_no"], payload["issue_date"], payload["due_date"],
                payload["customer_name"], payload["customer_phone"], payload["customer_email"],
                payload["customer_gstin"], payload["customer_address"], payload["customer_state"],
                payload["seller_name"], payload["seller_gstin"], payload["seller_address"],
                payload["seller_state"], payload["seller_phone"], payload["seller_email"],
                str(totals["taxable_total"]), str(totals["cgst_total"]),
                str(totals["sgst_total"]), str(totals["utgst_total"]), str(totals["igst_total"]),
                str(totals["gst_total"]), str(totals["grand_total"]), payload["reverse_charge"], payload["notes"],
            ),
        )
        invoice_id = int(cursor.lastrowid)
        conn.executemany(
            """INSERT INTO invoice_items (
                invoice_id, description, hsn, uqc, quantity, unit_price, gst_rate,
                taxable, cgst, sgst, utgst, igst, line_total
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    invoice_id, item["description"], item["hsn"], item["uqc"], str(item["quantity"]),
                    str(item["unit_price"]), str(item["gst_rate"]), str(item["taxable"]),
                    str(item["cgst"]), str(item["sgst"]), str(item["utgst"]), str(item["igst"]), str(item["total"]),
                )
                for item in totals["items"]
            ],
        )
    return invoice_id


def get_invoice(invoice_no: str) -> dict[str, Any] | None:
    with db_connect() as conn:
        inv = conn.execute("SELECT * FROM invoices WHERE invoice_no=?", (invoice_no.strip(),)).fetchone()
        if not inv:
            return None
        items = conn.execute("SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY id", (inv["id"],)).fetchall()
    result = dict(inv)
    result["items"] = [dict(row) for row in items]
    return result


def list_invoices(limit: int = 500) -> list[dict[str, Any]]:
    with db_connect() as conn:
        rows = conn.execute("SELECT * FROM invoices ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def save_customer_preference(invoice_id: int, preference: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with db_connect() as conn:
        cursor = conn.execute(
            "UPDATE invoices SET payment_preference=?, preference_updated_at=? WHERE id=? AND payment_status='UNPAID'",
            (preference, now, invoice_id),
        )
        if cursor.rowcount:
            conn.execute(
                "INSERT INTO preference_events(invoice_id,preference,created_at) VALUES(?,?,?)",
                (invoice_id, preference, now),
            )


def mark_invoice_paid(invoice_id: int, method: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with db_connect() as conn:
        conn.execute(
            "UPDATE invoices SET payment_status='PAID', payment_method=?, paid_at=? WHERE id=? AND payment_status='UNPAID'",
            (method, now, invoice_id),
        )


def get_dashboard_stats(invoices: list[dict[str, Any]]) -> dict[str, Decimal | int]:
    total = sum((money(i["grand_total"]) for i in invoices), Decimal("0.00"))
    paid = [i for i in invoices if i["payment_status"] == "PAID"]
    paid_total = sum((money(i["grand_total"]) for i in paid), Decimal("0.00"))
    online_received = sum((money(i["grand_total"]) for i in paid if i["payment_method"] in {"UPI (verified manually)", "Bank transfer", "Online (verified manually)"}), Decimal("0.00"))
    cash_received = sum((money(i["grand_total"]) for i in paid if i["payment_method"] == "Cash"), Decimal("0.00"))
    outstanding = sum((money(i["grand_total"]) for i in invoices if i["payment_status"] != "PAID"), Decimal("0.00"))
    online_requests = sum(1 for i in invoices if i["payment_status"] != "PAID" and i["payment_preference"] == "ONLINE")
    cash_choices = sum(1 for i in invoices if i["payment_status"] != "PAID" and i["payment_preference"] == "CASH")
    return {
        "invoice_count": len(invoices), "total": total, "paid_total": paid_total,
        "online_received": online_received, "cash_received": cash_received,
        "outstanding": outstanding, "online_requests": online_requests,
        "cash_choices": cash_choices,
    }

# ----------------------------- QR + Translation ---------------------------

def customer_portal_url() -> str:
    base = os.getenv("PUBLIC_APP_URL", "http://localhost:8501").strip()
    parsed = urlsplit(base)
    params = parse_qs(parsed.query)
    params["view"] = ["customer"]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", urlencode(params, doseq=True), ""))


def make_qr_image(url: str, box_size: int = 8) -> io.BytesIO:
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=box_size, border=3)
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#14213d", back_color="white").convert("RGB")
    out = io.BytesIO()
    image.save(out, format="PNG")
    out.seek(0)
    return out


def get_languages() -> dict[str, str]:
    key = os.getenv("GOOGLE_TRANSLATE_API_KEY", "").strip()
    if not key:
        return FALLBACK_LANGUAGES
    try:
        response = requests.get(
            "https://translation.googleapis.com/language/translate/v2/languages",
            params={"key": key, "target": "en"}, timeout=12,
        )
        response.raise_for_status()
        languages = response.json().get("data", {}).get("languages", [])
        return {item.get("name", item["language"]): item["language"] for item in languages}
    except Exception:
        return FALLBACK_LANGUAGES


def translate_texts(texts: list[str], target_code: str) -> list[str]:
    if target_code == "en":
        return texts
    key = os.getenv("GOOGLE_TRANSLATE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Add GOOGLE_TRANSLATE_API_KEY to your .env file to enable translation.")
    response = requests.post(
        "https://translation.googleapis.com/language/translate/v2",
        params={"key": key},
        json={"q": texts, "target": target_code, "source": "en", "format": "text"},
        timeout=25,
    )
    if not response.ok:
        detail = response.json().get("error", {}).get("message", response.text[:300])
        raise RuntimeError(f"Google Translation API error: {detail}")
    translated = response.json().get("data", {}).get("translations", [])
    if len(translated) != len(texts):
        raise RuntimeError("Translation service returned an unexpected response. Please retry.")
    return [item.get("translatedText", original) for item, original in zip(translated, texts)]

# -------------------------------- PDF --------------------------------------

def register_pdf_font(language_code: str = "en") -> tuple[str, str]:
    """Use an installed Unicode font when available; never bundle font files.

    For Indic scripts, prefer Noto Sans for that script or Windows Nirmala UI.
    If no suitable font is found, PDF generation is stopped rather than silently
    producing missing glyphs.
    """
    script_fonts = {
        "hi": "Devanagari", "mr": "Devanagari", "ne": "Devanagari",
        "kn": "Kannada", "ta": "Tamil", "te": "Telugu", "ml": "Malayalam",
        "bn": "Bengali", "gu": "Gujarati", "pa": "Gurmukhi", "or": "Oriya",
        "ar": "Arabic",
    }
    script = script_fonts.get(language_code)
    candidates: list[tuple[str, str]] = []
    if script:
        candidates.append((
            f"/usr/share/fonts/truetype/noto/NotoSans{script}-Regular.ttf",
            f"/usr/share/fonts/truetype/noto/NotoSans{script}-Bold.ttf",
        ))
        candidates.append((r"C:\Windows\Fonts\Nirmala.ttf", r"C:\Windows\Fonts\NirmalaB.ttf"))
        if language_code == "ar":
            candidates.append((r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"))
    elif language_code in {"ja", "zh-CN", "zh-TW", "ko"}:
        # CJK font collections are platform-specific; ask for a suitable TTF if none is available.
        candidates.extend([
            ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", ""),
            (r"C:\Windows\Fonts\YuGothR.ttc", ""),
        ])
    else:
        candidates.extend([
            (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
            ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
            ("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
        ])

    for regular, bold in candidates:
        if regular and Path(regular).exists():
            try:
                pdfmetrics.registerFont(TTFont("InvoiceUnicode", regular))
                bold_name = "InvoiceUnicode"
                if bold and Path(bold).exists():
                    pdfmetrics.registerFont(TTFont("InvoiceUnicode-Bold", bold))
                    bold_name = "InvoiceUnicode-Bold"
                return "InvoiceUnicode", bold_name
            except Exception:
                continue
    if script or language_code in {"ja", "zh-CN", "zh-TW", "ko"}:
        return "Helvetica", "Helvetica-Bold"
    return "Helvetica", "Helvetica-Bold"


def build_invoice_pdf(invoice: dict[str, Any], language_code: str = "en") -> bytes:
    labels = [
        "TAX INVOICE", "Invoice number", "Invoice date", "Due date", "Bill to",
        "Description", "HSN/SAC", "Qty", "Unit", "Unit price", "GST rate", "Taxable value",
        "CGST", "SGST", "UTGST", "IGST", "Total", "Subtotal", "Total GST", "Amount due",
        "Seller details", "Customer details", "Place of supply", "GST amount", "Reverse charge", "Yes", "No", "Authorized signatory", "ORIGINAL FOR RECIPIENT", "Scan this QR to choose how you would like to pay",
        "This QR records your payment preference only. It does not collect money.",
        "Online payment requested", "Pay in cash at the counter", "Thank you for your business!",
        "Phone", "Email", "GSTIN", "State", "Notes", "Prices are exclusive of GST.",
    ]
    item_descriptions = [str(item["description"]) for item in invoice["items"]]
    translated = translate_texts(labels + item_descriptions, language_code)
    L = dict(zip(labels, translated[:len(labels)]))
    desc_translations = translated[len(labels):]
    body_font, bold_font = register_pdf_font(language_code)
    needs_script_font = language_code in {"hi", "kn", "ta", "te", "ml", "bn", "mr", "gu", "pa", "or", "ar", "ja", "zh-CN", "zh-TW", "ko"}
    if needs_script_font and body_font == "Helvetica":
        raise RuntimeError("This computer does not have a compatible font installed for the selected script. Install the matching Noto Sans font (or Nirmala UI on Windows) and retry the PDF.")

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=15 * mm, leftMargin=15 * mm,
        topMargin=14 * mm, bottomMargin=14 * mm,
        title=f"Invoice {invoice['invoice_no']}", author=invoice["seller_name"],
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="InvTitle", parent=styles["Title"], fontName=bold_font, fontSize=19, leading=23, textColor=colors.HexColor("#14213d"), alignment=TA_LEFT, spaceAfter=3))
    styles.add(ParagraphStyle(name="InvBody", parent=styles["BodyText"], fontName=body_font, fontSize=8.5, leading=12, textColor=colors.HexColor("#263238")))
    styles.add(ParagraphStyle(name="InvSmall", parent=styles["BodyText"], fontName=body_font, fontSize=7.5, leading=10, textColor=colors.HexColor("#52606d")))
    styles.add(ParagraphStyle(name="InvHead", parent=styles["BodyText"], fontName=bold_font, fontSize=9, leading=12, textColor=colors.HexColor("#14213d")))
    styles.add(ParagraphStyle(name="InvTableHead", parent=styles["BodyText"], fontName=bold_font, fontSize=7.2, leading=8.5, textColor=colors.white))
    styles.add(ParagraphStyle(name="InvNum", parent=styles["BodyText"], fontName=body_font, fontSize=7.3, leading=8.5, alignment=TA_RIGHT))
    styles.add(ParagraphStyle(name="InvRight", parent=styles["BodyText"], fontName=body_font, fontSize=8.5, leading=12, alignment=TA_RIGHT))

    def P(value: Any, style: str = "InvBody") -> Paragraph:
        safe = str(value if value not in (None, "") else "—").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")
        return Paragraph(safe, styles[style])

    story: list[Any] = []
    title_row = [P(invoice["seller_name"], "InvTitle"), P(L["TAX INVOICE"], "InvHead")]
    head = Table([title_row], colWidths=[112 * mm, 65 * mm])
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8), ("LINEBELOW", (0, 0), (-1, -1), 1.2, colors.HexColor("#2f6690")),
    ]))
    story.extend([head, Spacer(1, 7)])

    meta = [
        [P(f"{L['Invoice number']}: {invoice['invoice_no']}"), P(f"{L['Invoice date']}: {invoice['issue_date']}", "InvRight")],
        [P(f"{L['Due date']}: {invoice.get('due_date') or '—'}"), P(f"{L['Place of supply']}: {invoice['customer_state']} ({STATE_CODES.get(invoice['customer_state'], '—')})", "InvRight")],
        [P(f"{L['Reverse charge']}: {L['Yes'] if invoice.get('reverse_charge') == 'Yes' else L['No']}"), P(L["ORIGINAL FOR RECIPIENT"], "InvRight")],
    ]
    mt = Table(meta, colWidths=[88 * mm, 89 * mm])
    mt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story.extend([mt, Spacer(1, 8)])

    seller_text = [P(L["Seller details"], "InvHead"), P(invoice["seller_name"]), P(invoice["seller_address"], "InvSmall")]
    if invoice.get("seller_gstin"):
        seller_text.append(P(f"{L['GSTIN']}: {invoice['seller_gstin']}", "InvSmall"))
    if invoice.get("seller_phone"):
        seller_text.append(P(f"{L['Phone']}: {invoice['seller_phone']}", "InvSmall"))
    if invoice.get("seller_email"):
        seller_text.append(P(f"{L['Email']}: {invoice['seller_email']}", "InvSmall"))

    customer_text = [P(L["Customer details"], "InvHead"), P(invoice["customer_name"]), P(invoice["customer_address"], "InvSmall")]
    if invoice.get("customer_gstin"):
        customer_text.append(P(f"{L['GSTIN']}: {invoice['customer_gstin']}", "InvSmall"))
    customer_text.append(P(f"{L['Phone']}: {invoice['customer_phone']}", "InvSmall"))
    if invoice.get("customer_email"):
        customer_text.append(P(f"{L['Email']}: {invoice['customer_email']}", "InvSmall"))
    parties = Table([[seller_text, customer_text]], colWidths=[88 * mm, 89 * mm])
    parties.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f3f6fa")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#d8e0e8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([parties, Spacer(1, 11)])

    table_data = [[P(L["Description"], "InvTableHead"), P(L["HSN/SAC"], "InvTableHead"), P(L["Qty"], "InvTableHead"), P(L["Unit"], "InvTableHead"), P(L["Unit price"], "InvTableHead"), P(L["GST rate"], "InvTableHead"), P(L["Taxable value"], "InvTableHead"), P(L["GST amount"], "InvTableHead"), P(L["Total"], "InvTableHead")]]
    for item, translated_desc in zip(invoice["items"], desc_translations):
        line_tax = money(item["line_total"]) - money(item["taxable"])
        table_data.append([
            P(translated_desc), P(item.get("hsn"), "InvNum"), P(item["quantity"], "InvNum"), P(item.get("uqc", "NOS"), "InvNum"),
            P("₹" + indian_number(item["unit_price"]), "InvNum"), P(f"{money(item['gst_rate']):g}%", "InvNum"),
            P("₹" + indian_number(item["taxable"]), "InvNum"), P("₹" + indian_number(line_tax), "InvNum"),
            P("₹" + indian_number(item["line_total"]), "InvNum"),
        ])
    col_widths = [38 * mm, 18 * mm, 9 * mm, 11 * mm, 20 * mm, 17 * mm, 23 * mm, 18 * mm, 24 * mm]
    items_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#14213d")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#d8e0e8")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
    ]))
    story.extend([P(L["Prices are exclusive of GST."], "InvSmall"), Spacer(1, 4), items_table, Spacer(1, 9)])

    totals_rows = [
        [P(L["Subtotal"]), P("₹" + indian_number(invoice["taxable_total"]), "InvRight")],
    ]
    if money(invoice["cgst_total"]) > 0:
        totals_rows.append([P(L["CGST"]), P("₹" + indian_number(invoice["cgst_total"]), "InvRight")])
    if money(invoice["sgst_total"]) > 0:
        totals_rows.append([P(L["SGST"]), P("₹" + indian_number(invoice["sgst_total"]), "InvRight")])
    if money(invoice.get("utgst_total", "0.00")) > 0:
        totals_rows.append([P(L["UTGST"]), P("₹" + indian_number(invoice["utgst_total"]), "InvRight")])
    if money(invoice["igst_total"]) > 0:
        totals_rows.append([P(L["IGST"]), P("₹" + indian_number(invoice["igst_total"]), "InvRight")])
    totals_rows.extend([
        [P(L["Total GST"]), P("₹" + indian_number(invoice["gst_total"]), "InvRight")],
        [P(L["Amount due"], "InvHead"), P("₹" + indian_number(invoice["grand_total"]), "InvHead")],
    ])
    totals_table = Table(totals_rows, colWidths=[42 * mm, 38 * mm], hAlign="RIGHT")
    totals_table.setStyle(TableStyle([
        ("LINEABOVE", (0, -1), (-1, -1), 1, colors.HexColor("#2f6690")),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#edf3f8")),
        ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.extend([totals_table, Spacer(1, 12)])

    qr_buffer = make_qr_image(customer_portal_url())
    qr_img = Image(qr_buffer, width=31 * mm, height=31 * mm)
    qr_caption = [P(L["Scan this QR to choose how you would like to pay"], "InvHead"), P(L["Online payment requested"] + " / " + L["Pay in cash at the counter"], "InvSmall"), P(L["This QR records your payment preference only. It does not collect money."], "InvSmall")]
    qr_block = Table([[qr_img, qr_caption]], colWidths=[37 * mm, 140 * mm])
    qr_block.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#d8e0e8")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fbfcfe")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.append(qr_block)
    if invoice.get("notes"):
        story.extend([Spacer(1, 8), P(f"{L['Notes']}: {invoice['notes']}", "InvSmall")])
    story.extend([Spacer(1, 15), P("______________________________________________", "InvSmall"), P(L["Authorized signatory"], "InvSmall"), Spacer(1, 8), P(L["Thank you for your business!"], "InvHead")])

    doc.build(story)
    return buffer.getvalue()

# ------------------------------- UI ---------------------------------------

st.set_page_config(page_title="Ledgerly • GST Invoices", page_icon="🧾", layout="wide", initial_sidebar_state="expanded")

# ---- Theme: premium navy + champagne-gold palette, light & dark ----------------
# Every colour in the UI comes from the CSS variables below, so light and dark
# mode stay consistent. Text/background pairs are chosen for WCAG AA contrast.
THEME_PALETTES: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#F5F3EE",             # warm ivory page background
        "surface": "#FFFFFF",        # cards, forms, metrics
        "surface-2": "#EFEBE2",      # code blocks, disabled fields, hover
        "input-bg": "#FFFFFF",
        "border": "#E3DED2",
        "border-strong": "#CBC4B2",
        "text": "#1B2433",
        "heading": "#0F1E38",
        "muted": "#566176",
        "primary": "#1F3A5F",        # navy buttons
        "primary-hover": "#162C4A",
        "on-primary": "#FFFFFF",
        "control": "#1F3A5F",        # selected radio / checkbox fill
        "accent": "#C9A24D",         # gold accents (borders, lines)
        "accent-text": "#7A5614",    # gold that is safe to use as text
        "ring": "rgba(201,162,77,.40)",
        "shadow": "rgba(15,30,56,.07)",
        "hero-a": "#0F1E38",
        "hero-b": "#25507F",
        "hero-border": "rgba(15,30,56,.10)",
        "sb-top": "#0F1E38",
        "sb-bottom": "#16294A",
        "glow": "rgba(201,162,77,.16)",
        "accent-soft": "rgba(201,162,77,.18)",
        "success": "#176B3A", "success-soft": "rgba(23,107,58,.12)",
        "warning": "#8A5300", "warning-soft": "rgba(176,106,0,.14)",
        "info": "#1D5AA8", "info-soft": "rgba(29,90,168,.12)",
    },
    "dark": {
        "bg": "#0B1220",
        "surface": "#131D31",
        "surface-2": "#1B2842",
        "input-bg": "#0F1829",
        "border": "#263650",
        "border-strong": "#364B70",
        "text": "#E6EBF4",
        "heading": "#F5F7FB",
        "muted": "#9DABC2",
        "primary": "#D9B45F",        # gold buttons on dark
        "primary-hover": "#E7C777",
        "on-primary": "#1A1405",
        "control": "#4C86D6",
        "accent": "#D9B45F",
        "accent-text": "#E3C27A",
        "ring": "rgba(217,180,95,.40)",
        "shadow": "rgba(0,0,0,.45)",
        "hero-a": "#14264A",
        "hero-b": "#1F4474",
        "hero-border": "#2F4A75",
        "sb-top": "#080E1A",
        "sb-bottom": "#0E1830",
        "glow": "rgba(76,134,214,.18)",
        "accent-soft": "rgba(217,180,95,.18)",
        "success": "#5FD38D", "success-soft": "rgba(95,211,141,.15)",
        "warning": "#F0B35A", "warning-soft": "rgba(240,179,90,.15)",
        "info": "#7FB2F0", "info-soft": "rgba(127,178,240,.15)",
    },
}

THEME_FONT_IMPORT = (
    "@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700"
    "&family=Manrope:wght@500;600;700;800&display=swap');"
)

THEME_BASE_CSS = """
html, body, [class*="css"], .stApp { font-family: 'DM Sans', sans-serif; }
.stApp { background: radial-gradient(900px 420px at 88% -8%, var(--glow), transparent 62%), radial-gradient(700px 380px at -6% 0%, var(--accent-soft), transparent 60%), var(--bg); background-attachment: fixed; color: var(--text); }
[data-testid="stAppViewContainer"], [data-testid="stMain"] { background: transparent; color: var(--text); }
footer, [data-testid="stAppDeployButton"] { display: none !important; }
[data-testid="stHeader"] { background: transparent; }
.block-container { padding-top: 2.2rem; padding-bottom: 3rem; }

/* Typography */
.stApp h1, .stApp h2, .stApp h3, .stApp h4 { font-family: 'Manrope', sans-serif; letter-spacing: -0.03em; color: var(--heading); }
.stApp h4 { font-size: 1.05rem; padding-top: .6rem; }
.stApp p, .stApp li, .stApp label, .stApp [data-testid="stMarkdownContainer"] { color: var(--text); }
.stApp [data-testid="stWidgetLabel"] p { font-weight: 600; font-size: .88rem; color: var(--text); }
.stApp [data-testid="stCaptionContainer"], .stApp [data-testid="stCaptionContainer"] * { color: var(--muted); }
.stApp a { color: var(--accent-text); }
.small-muted { color: var(--muted); font-size: .88rem; }

/* Separation: dividers, cards, forms */
.stApp hr { border: none !important; border-top: 1px solid var(--border-strong) !important; background: transparent !important; opacity: .8; margin: 1.6rem 0 !important; }
.soft-card { padding: 16px 18px; background: var(--surface); border: 1px solid var(--border); border-radius: 14px; box-shadow: 0 1px 2px var(--shadow); }
div[data-testid="stForm"] { background: var(--surface); border: 1px solid var(--border); border-radius: 16px; padding: 20px; box-shadow: 0 1px 2px var(--shadow), 0 8px 24px var(--shadow); }
div[data-testid="stMetric"] { background: var(--surface); border: 1px solid var(--border); border-left: 4px solid var(--accent); padding: 15px 17px; border-radius: 14px; box-shadow: 0 1px 2px var(--shadow), 0 6px 18px var(--shadow); }
div[data-testid="stMetric"] [data-testid="stMetricLabel"], div[data-testid="stMetric"] [data-testid="stMetricLabel"] * { color: var(--muted) !important; font-weight: 500; }
div[data-testid="stMetric"] [data-testid="stMetricValue"], div[data-testid="stMetric"] [data-testid="stMetricValue"] * { color: var(--heading) !important; font-family: 'Manrope', sans-serif; font-weight: 700; }
div[data-testid="stDataFrame"] { border: 1px solid var(--border); border-radius: 12px; overflow: hidden; box-shadow: 0 1px 2px var(--shadow); }
div[data-testid="stAlert"] { border: 1px solid var(--border); border-radius: 12px; }
div[data-testid="stAlert"] p { color: var(--text); }
div[data-testid="stImage"] img { background: #FFFFFF; padding: 6px; border-radius: 12px; border: 1px solid var(--border); }
.stApp pre, .stApp [data-testid="stCode"] pre, .stApp code { background: var(--surface-2) !important; color: var(--text) !important; border-radius: 10px; }
.stApp pre code { background: transparent !important; }

/* Hero banner (same navy gradient in both modes, light text always on dark) */
.stApp .hero { position: relative; overflow: hidden; display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 14px; padding: 26px 30px 28px; border-radius: 20px; margin-bottom: 22px; background: linear-gradient(120deg, var(--hero-a), var(--hero-b)); border: 1px solid var(--hero-border); box-shadow: 0 14px 38px var(--shadow); }
.stApp .hero::before { content: ""; position: absolute; right: -70px; top: -90px; width: 280px; height: 280px; border-radius: 50%; background: radial-gradient(circle, rgba(217,180,95,.30), transparent 66%); }
.stApp .hero::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 3px; background: linear-gradient(90deg, #D9B45F, rgba(217,180,95,0) 70%); }
.stApp .hero > * { position: relative; z-index: 1; }
.stApp .hero-eyebrow { color: #E8C877; font-weight: 700; font-size: .74rem; letter-spacing: .16em; text-transform: uppercase; margin-bottom: 8px; }
.stApp .hero h1 { color: #FFFFFF !important; margin: 0; padding: 0; font-size: 30px; line-height: 1.15; }
.stApp .hero p { color: #D6E2F5 !important; margin: 8px 0 0 0; max-width: 640px; }
.stApp .hero-chip { background: rgba(255,255,255,.10); border: 1px solid rgba(255,255,255,.24); color: #EEF3FB; padding: 6px 13px; border-radius: 999px; font-size: .8rem; font-weight: 600; white-space: nowrap; }

/* Inputs */
.stApp div[data-baseweb="input"], .stApp div[data-baseweb="base-input"], .stApp div[data-baseweb="textarea"], .stApp div[data-baseweb="select"] > div { background: var(--input-bg) !important; border-color: var(--border-strong) !important; border-radius: 10px; }
.stApp input, .stApp textarea { color: var(--text) !important; -webkit-text-fill-color: var(--text); caret-color: var(--accent-text); }
.stApp input::placeholder, .stApp textarea::placeholder { color: var(--muted) !important; -webkit-text-fill-color: var(--muted); opacity: .85; }
.stApp input:disabled, .stApp textarea:disabled { -webkit-text-fill-color: var(--muted) !important; color: var(--muted) !important; opacity: 1; }
.stApp div[data-baseweb="input"]:has(input:disabled) { background: var(--surface-2) !important; }
.stApp div[data-baseweb="input"]:focus-within, .stApp div[data-baseweb="textarea"]:focus-within, .stApp div[data-baseweb="select"] > div:focus-within { border-color: var(--accent) !important; box-shadow: 0 0 0 3px var(--ring) !important; }
.stApp div[data-baseweb="select"] *, .stApp div[data-baseweb="select"] input { color: var(--text) !important; }
.stApp div[data-baseweb="select"] svg { fill: var(--muted) !important; }
.stApp [data-testid="stNumberInputStepDown"], .stApp [data-testid="stNumberInputStepUp"] { background: var(--surface-2) !important; color: var(--text) !important; }
div[data-baseweb="popover"] ul, div[data-baseweb="popover"] [role="listbox"], div[data-baseweb="menu"] { background: var(--surface) !important; color: var(--text) !important; }
div[data-baseweb="popover"] li, div[data-baseweb="popover"] li * { color: var(--text) !important; }
div[data-baseweb="popover"] li:hover, div[data-baseweb="popover"] li[aria-selected="true"] { background: var(--surface-2) !important; }
div[data-baseweb="calendar"] { background: var(--surface) !important; color: var(--text) !important; }

/* Radio / checkbox accents (replaces Streamlit's default red) */
[data-testid="stMain"] label[data-baseweb="radio"]:has(input:checked) > div:first-child { background-color: var(--control) !important; border-color: var(--control) !important; }
[data-testid="stMain"] label[data-baseweb="checkbox"]:has(input:checked) > span:first-child { background-color: var(--control) !important; border-color: var(--control) !important; }

/* Buttons */
.stApp button[kind="secondary"], .stApp button[kind="secondaryFormSubmit"], .stApp [data-testid="stBaseButton-secondary"], .stApp [data-testid="stBaseButton-secondaryFormSubmit"] { background: var(--surface); color: var(--text); border: 1px solid var(--border-strong); border-radius: 10px; font-weight: 600; }
.stApp button[kind="secondary"]:hover, .stApp button[kind="secondaryFormSubmit"]:hover, .stApp [data-testid="stBaseButton-secondary"]:hover { border-color: var(--accent); color: var(--accent-text); background: var(--surface-2); }
.stApp button[kind="primary"], .stApp button[kind="primaryFormSubmit"], .stApp [data-testid="stBaseButton-primary"], .stApp [data-testid="stBaseButton-primaryFormSubmit"] { background: var(--primary) !important; color: var(--on-primary) !important; border: 1px solid var(--primary) !important; border-radius: 10px; font-weight: 700; box-shadow: 0 2px 8px var(--shadow); }
.stApp button[kind="primary"]:hover, .stApp button[kind="primaryFormSubmit"]:hover, .stApp [data-testid="stBaseButton-primary"]:hover, .stApp [data-testid="stBaseButton-primaryFormSubmit"]:hover { background: var(--primary-hover) !important; border-color: var(--primary-hover) !important; }
.stApp button p, .stApp button span { color: inherit !important; }
.stApp button:disabled { opacity: .55; }
.stApp button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

/* Sidebar: always a deep-navy panel with light text, in both modes */
.stApp section[data-testid="stSidebar"] { background: linear-gradient(180deg, var(--sb-top), var(--sb-bottom)); border-right: 1px solid rgba(255,255,255,.08); }
.stApp section[data-testid="stSidebar"] * { color: #EDF2FA !important; }
.stApp section[data-testid="stSidebar"] [data-testid="stCaptionContainer"], .stApp section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] * { color: #AEBBD1 !important; }
.stApp section[data-testid="stSidebar"] hr { border-top-color: rgba(255,255,255,.16) !important; }
.stApp section[data-testid="stSidebar"] label[data-baseweb="radio"] { padding: 9px 12px; border-radius: 10px; margin-bottom: 2px; width: 100%; }
.stApp section[data-testid="stSidebar"] label[data-baseweb="radio"]:hover { background: rgba(255,255,255,.07); }
.stApp section[data-testid="stSidebar"] label[data-baseweb="radio"]:has(input:checked) { background: rgba(217,180,95,.16); box-shadow: inset 3px 0 0 #D9B45F; }
.stApp section[data-testid="stSidebar"] label[data-baseweb="radio"]:has(input:checked) > div:first-child { background-color: #D9B45F !important; border-color: #D9B45F !important; }
.stApp section[data-testid="stSidebar"] button { background: rgba(255,255,255,.07) !important; border: 1px solid rgba(255,255,255,.28) !important; box-shadow: none; }
.stApp section[data-testid="stSidebar"] button:hover { background: rgba(255,255,255,.14) !important; border-color: #D9B45F !important; }

/* Brand */
.brand { display: flex; align-items: center; gap: 12px; padding: 6px 4px 2px; }
.brand-mark { width: 38px; height: 38px; flex: none; border-radius: 11px; display: grid; place-items: center; font-family: 'Manrope', sans-serif; font-weight: 800; font-size: 1.2rem; color: #1A1405; background: linear-gradient(135deg, #F2D790, #C9A24D); box-shadow: 0 4px 12px rgba(0,0,0,.25); }
.brand-name { font-family: 'Manrope', sans-serif; font-weight: 800; font-size: 1.28rem; letter-spacing: -.02em; line-height: 1.1; }
.brand-sub { font-size: .76rem; }
.stApp section[data-testid="stSidebar"] .brand-mark { color: #1A1405 !important; }
.stApp section[data-testid="stSidebar"] .brand-sub { color: #AEBBD1 !important; }
.sb-pill { display: inline-flex; align-items: center; gap: 8px; padding: 5px 12px; border-radius: 999px; background: rgba(95,211,141,.15); font-size: .78rem; font-weight: 600; margin-bottom: 8px; }
.sb-pill::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: #5FD38D; }
.portal-brand { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; font-family: 'Manrope', sans-serif; font-weight: 800; font-size: 1.15rem; color: var(--heading); }
.portal-brand .brand-mark { width: 32px; height: 32px; font-size: 1rem; }

/* KPI cards */
.kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 14px; margin: 4px 0 16px; }
.kpi { position: relative; overflow: hidden; background: var(--surface); border: 1px solid var(--border); border-radius: 16px; padding: 16px 18px 16px 22px; box-shadow: 0 1px 2px var(--shadow), 0 8px 22px var(--shadow); }
.kpi::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 5px; background: var(--kpi-color); }
.kpi-top { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.kpi-label { color: var(--muted); font-size: .76rem; font-weight: 700; text-transform: uppercase; letter-spacing: .07em; }
.kpi-icon { width: 34px; height: 34px; flex: none; border-radius: 10px; display: grid; place-items: center; background: var(--kpi-soft); font-size: 1rem; }
.kpi-value { font-family: 'Manrope', sans-serif; font-weight: 800; font-size: 1.7rem; letter-spacing: -.02em; color: var(--heading); margin-top: 10px; font-variant-numeric: tabular-nums; word-break: break-word; }
.kpi-note { color: var(--muted); font-size: .82rem; margin-top: 2px; }
.kpi-sm .kpi-value { font-size: 1.3rem; margin-top: 6px; }
.kpi-neutral { --kpi-color: var(--accent); --kpi-soft: var(--accent-soft); }
.kpi-success { --kpi-color: var(--success); --kpi-soft: var(--success-soft); }
.kpi-warning { --kpi-color: var(--warning); --kpi-soft: var(--warning-soft); }
.kpi-info { --kpi-color: var(--info); --kpi-soft: var(--info-soft); }

/* Section titles, form steps */
.sec { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 12px; margin: 28px 0 12px; padding-left: 12px; border-left: 4px solid var(--accent); }
.sec-title { font-family: 'Manrope', sans-serif; font-weight: 800; font-size: 1.2rem; letter-spacing: -.02em; color: var(--heading); }
.sec-hint { color: var(--muted); font-size: .86rem; }
.step { display: flex; align-items: center; gap: 12px; margin: 6px 0 10px; }
.step-n { width: 30px; height: 30px; flex: none; border-radius: 9px; display: grid; place-items: center; font-weight: 800; font-size: .9rem; color: var(--on-primary); background: var(--primary); }
.step-t { font-family: 'Manrope', sans-serif; font-weight: 800; font-size: 1.05rem; color: var(--heading); line-height: 1.2; }
.step-s { color: var(--muted); font-size: .82rem; }

/* Status pills */
.pill { display: inline-flex; align-items: center; gap: 6px; padding: 3px 11px; border-radius: 999px; font-size: .76rem; font-weight: 700; line-height: 1.5; white-space: nowrap; }
.pill::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.pill-success { color: var(--success); background: var(--success-soft); }
.pill-warning { color: var(--warning); background: var(--warning-soft); }
.pill-info { color: var(--info); background: var(--info-soft); }
.pill-neutral { color: var(--accent-text); background: var(--accent-soft); }
.pill-muted { color: var(--muted); background: var(--surface-2); }

/* HTML data tables (follow the theme exactly, unlike canvas grids) */
.tbl-wrap { background: var(--surface); border: 1px solid var(--border); border-radius: 16px; box-shadow: 0 1px 2px var(--shadow), 0 8px 22px var(--shadow); overflow: auto; max-height: 440px; margin-bottom: 8px; }
.stApp .tbl { width: 100%; border-collapse: separate; border-spacing: 0; font-size: .9rem; margin: 0; }
.stApp .tbl th, .stApp .tbl td { border: 0; border-bottom: 1px solid var(--border); padding: 13px 16px; text-align: left; white-space: nowrap; background: transparent; color: var(--text); }
.stApp .tbl th { position: sticky; top: 0; z-index: 1; background: var(--surface-2); color: var(--muted); text-transform: uppercase; letter-spacing: .07em; font-size: .72rem; font-weight: 700; }
.stApp .tbl tbody tr:last-child td { border-bottom: 0; }
.stApp .tbl tbody tr:hover td { background: var(--surface-2); }
.stApp .tbl .r { text-align: right; font-variant-numeric: tabular-nums; }
.stApp .tbl .strong { font-weight: 700; color: var(--heading); }
.tbl-empty { padding: 26px; text-align: center; color: var(--muted); background: var(--surface); border: 1px dashed var(--border-strong); border-radius: 16px; }

/* Summary strip (customer portal, library) */
.summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 18px; background: var(--surface); border: 1px solid var(--border); border-radius: 16px; padding: 18px 22px; box-shadow: 0 1px 2px var(--shadow), 0 8px 22px var(--shadow); margin: 6px 0 14px; }
.s-label { color: var(--muted); font-size: .72rem; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; }
.s-val { font-family: 'Manrope', sans-serif; font-weight: 700; font-size: 1.05rem; color: var(--heading); margin-top: 5px; word-break: break-word; }
.s-big { font-size: 1.45rem; font-weight: 800; }

/* Keyed card containers: st.container(key="card_...") */
div[class*="st-key-card_"] { background: var(--surface); border: 1px solid var(--border); border-radius: 16px; padding: 20px 22px; box-shadow: 0 1px 2px var(--shadow), 0 8px 22px var(--shadow); margin-bottom: 8px; }

/* Customer payment choice: selectable cards */
[data-testid="stMain"] div[role="radiogroup"] label[data-baseweb="radio"] { background: var(--surface); border: 1px solid var(--border-strong); border-radius: 12px; padding: 12px 14px; width: 100%; }
[data-testid="stMain"] div[role="radiogroup"] label[data-baseweb="radio"]:has(input:checked) { border-color: var(--accent); box-shadow: 0 0 0 3px var(--ring); }
"""


def active_theme() -> str | None:
    """Return 'light' or 'dark' when Streamlit can report it, else None."""
    try:
        theme_type = st.context.theme.type  # Streamlit >= 1.46
    except Exception:
        return None
    return theme_type if theme_type in ("light", "dark") else None


def _css_vars(palette: dict[str, str]) -> str:
    return " ".join(f"--{name}: {value};" for name, value in palette.items())


def build_theme_css() -> str:
    light, dark = THEME_PALETTES["light"], THEME_PALETTES["dark"]
    mode = active_theme()
    if mode == "dark":
        root = f":root {{ {_css_vars(dark)} color-scheme: dark; }}"
    elif mode == "light":
        root = f":root {{ {_css_vars(light)} color-scheme: light; }}"
    else:  # older Streamlit: follow the operating-system preference
        root = (
            f":root {{ {_css_vars(light)} color-scheme: light; }} "
            f"@media (prefers-color-scheme: dark) {{ :root {{ {_css_vars(dark)} color-scheme: dark; }} }}"
        )
    return f"<style>{THEME_FONT_IMPORT}{root}{THEME_BASE_CSS}</style>"


st.markdown(build_theme_css(), unsafe_allow_html=True)

init_db()


def money_inr(value: Any) -> str:
    return f"₹{indian_number(value)}"


def esc(value: Any) -> str:
    """Escape user-supplied text before it goes into an HTML snippet."""
    return html.escape("" if value is None else str(value), quote=True).replace("$", "&#36;")


def show_hero(title: str, subtitle: str, eyebrow: str = "Ledgerly · GST invoicing") -> None:
    today = date.today().strftime("%d %b %Y")
    st.markdown(
        f'<div class="hero"><div class="hero-main"><div class="hero-eyebrow">{eyebrow}</div>'
        f'<h1>{title}</h1><p>{subtitle}</p></div><div class="hero-chip">{today}</div></div>',
        unsafe_allow_html=True,
    )


def pill(text: str, tone: str = "muted") -> str:
    return f'<span class="pill pill-{tone}">{esc(text)}</span>'


def kpi_card(label: str, value: str, note: str = "", tone: str = "neutral", icon: str = "", small: bool = False) -> str:
    size = " kpi-sm" if small else ""
    note_html = f'<div class="kpi-note">{esc(note)}</div>' if note else ""
    return (
        f'<div class="kpi kpi-{tone}{size}"><div class="kpi-top"><span class="kpi-label">{esc(label)}</span>'
        f'<span class="kpi-icon">{icon}</span></div><div class="kpi-value">{esc(value)}</div>{note_html}</div>'
    )


def render_kpis(cards: list[str]) -> None:
    st.markdown('<div class="kpi-grid">' + "".join(cards) + "</div>", unsafe_allow_html=True)


def section_title(title: str, hint: str = "") -> None:
    hint_html = f'<span class="sec-hint">{esc(hint)}</span>' if hint else ""
    st.markdown(f'<div class="sec"><div class="sec-title">{esc(title)}</div>{hint_html}</div>', unsafe_allow_html=True)


def form_step(number: Any, title: str, hint: str = "") -> None:
    hint_html = f'<div class="step-s">{esc(hint)}</div>' if hint else ""
    st.markdown(
        f'<div class="step"><span class="step-n">{number}</span><div><div class="step-t">{esc(title)}</div>{hint_html}</div></div>',
        unsafe_allow_html=True,
    )


def data_table(headers: list[str], rows: list[list[str]], right: tuple[int, ...] = ()) -> None:
    """Render a themed table. Cells must already be HTML-safe (use esc() / pill())."""
    head = "".join(f'<th class="{"r" if i in right else ""}">{esc(h)}</th>' for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(f'<td class="{"r" if i in right else ""}">{cell}</td>' for i, cell in enumerate(row)) + "</tr>"
        for row in rows
    )
    st.markdown(
        f'<div class="tbl-wrap"><table class="tbl"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>',
        unsafe_allow_html=True,
    )


PAYMENT_PILL = {"PAID": ("Paid", "success"), "UNPAID": ("Unpaid", "warning")}
PREFERENCE_PILL = {"ONLINE": ("Online requested", "info"), "CASH": ("Cash at counter", "neutral")}


def payment_pill(status: str) -> str:
    text, tone = PAYMENT_PILL.get(status, (status.title(), "muted"))
    return pill(text, tone)


def preference_pill(preference: str | None) -> str:
    text, tone = PREFERENCE_PILL.get(preference or "", ("No response", "muted"))
    return pill(text, tone)

def admin_login() -> bool:
    if st.session_state.get("admin_authenticated"):
        return True
    show_hero("Business dashboard", "Sign in to manage invoices and payment records.", "Secure access")
    _, centre, _ = st.columns([1, 1.6, 1])
    with centre:
        with st.form("admin_login_form"):
            form_step("🔒", "Admin sign in", "Use the password from your .env file")
            password = st.text_input("Admin password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)
        if submitted:
            expected = os.getenv("ADMIN_PASSWORD", "change-me-now")
            if hmac.compare_digest(password, expected):
                st.session_state["admin_authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")
        st.caption("Set a strong ADMIN_PASSWORD in your .env file before exposing this app to a network.")
    return False

def render_customer_portal() -> None:
    st.markdown("<style>.block-container{max-width:720px !important;}</style>", unsafe_allow_html=True)
    st.markdown('<div class="portal-brand"><div class="brand-mark">L</div><span>Ledgerly</span></div>', unsafe_allow_html=True)
    show_hero("Choose your payment preference", "Enter the details printed on your invoice to continue.", "Customer portal")
    with st.form("customer_lookup_form"):
        inv_no = st.text_input("Invoice number", placeholder="e.g. INV/2026-27/0001").strip()
        registered_mobile = st.text_input("Registered 10-digit mobile number", max_chars=10, placeholder="10-digit number").strip()
        lookup = st.form_submit_button("Find my invoice", type="primary", use_container_width=True)
    if lookup:
        phone_digits = re.sub(r"\D", "", registered_mobile)
        if not re.fullmatch(r"[6-9]\d{9}", phone_digits):
            st.error("Enter the registered 10-digit Indian mobile number.")
        else:
            invoice = get_invoice(inv_no)
            if not invoice or not hmac.compare_digest(invoice["customer_phone"], phone_digits):
                st.error("We couldn't verify those details. Check the invoice number and registered mobile number.")
            else:
                st.session_state["customer_verified_invoice"] = invoice["invoice_no"]
                st.session_state.pop("customer_choice_confirmation", None)
                st.rerun()

    verified_no = st.session_state.get("customer_verified_invoice")
    if verified_no:
        invoice = get_invoice(verified_no)
        if not invoice:
            st.session_state.pop("customer_verified_invoice", None)
            st.warning("This invoice is no longer available.")
            return
        section_title("Invoice summary")
        status_html = pill("Paid", "success") if invoice["payment_status"] == "PAID" else pill("Payment pending", "warning")
        st.markdown(
            '<div class="summary">'
            f'<div><div class="s-label">Invoice</div><div class="s-val">{esc(invoice["invoice_no"])}</div></div>'
            f'<div><div class="s-label">Amount</div><div class="s-val s-big">{esc(money_inr(invoice["grand_total"]))}</div></div>'
            f'<div><div class="s-label">Customer</div><div class="s-val">{esc(invoice["customer_name"])}</div></div>'
            f'<div><div class="s-label">Status</div><div class="s-val">{status_html}</div></div>'
            "</div>",
            unsafe_allow_html=True,
        )
        if invoice["payment_status"] == "PAID":
            if invoice.get("payment_method") in {"UPI (verified manually)", "Bank transfer", "Online (verified manually)"}:
                st.success("Thanks for your contactless payment. Visit again 😊")
            elif invoice.get("payment_method") == "Cash":
                st.success("Thanks for your payment. Visit again 😊")
            else:
                st.success("Payment received. Thank you for your business. Visit again 😊")
            st.caption(f"Payment method recorded: {invoice.get('payment_method') or 'Not specified'}")
        else:
            section_title("How would you like to pay?")
            choice_default = 0 if invoice.get("payment_preference") == "ONLINE" else 1 if invoice.get("payment_preference") == "CASH" else 0
            with st.form("customer_preference_form"):
                choice = st.radio(
                    "Select one option",
                    ["I want to pay online", "I'll pay by cash at the counter"],
                    index=choice_default,
                    help="This only notifies the counter. No payment is collected on this page.",
                )
                confirm = st.form_submit_button("Confirm my choice", type="primary", use_container_width=True)
            if confirm:
                preference = "ONLINE" if choice.startswith("I want") else "CASH"
                save_customer_preference(invoice["id"], preference)
                st.session_state["customer_choice_confirmation"] = preference
                st.rerun()
            confirmation = st.session_state.get("customer_choice_confirmation")
            if confirmation == "ONLINE":
                st.info("Your request for online payment has been sent to the counter. Please wait for the staff to share the available payment option. This is not a payment confirmation.")
            elif confirmation == "CASH":
                st.success("Your choice has been sent. You can pay by cash at the counter.")
    st.caption("For help, please contact the business using the details on your invoice.")

def render_dashboard() -> None:
    if not admin_login():
        return
    settings = get_settings()
    invoices = list_invoices()
    stats = get_dashboard_stats(invoices)
    show_hero("Invoice overview", "A clear view of billing, customer choices and confirmed collections.", "Dashboard")

    if os.getenv("ADMIN_PASSWORD", "change-me-now") == "change-me-now":
        st.warning("Security setup incomplete: change ADMIN_PASSWORD in your .env file before sharing this app on a network.")

    render_kpis([
        kpi_card("Invoice value", money_inr(stats["total"]), f"{stats['invoice_count']} invoices", "neutral", "🧾"),
        kpi_card("Received", money_inr(stats["paid_total"]), "Manually confirmed", "success", "✅"),
        kpi_card("Outstanding", money_inr(stats["outstanding"]), "Not marked paid", "warning", "⏳"),
        kpi_card("Online requests", str(stats["online_requests"]), "Preference only", "info", "📲"),
    ])
    render_kpis([
        kpi_card("Online collected", money_inr(stats["online_received"]), "UPI / bank (verified)", "info", "🏦", small=True),
        kpi_card("Cash collected", money_inr(stats["cash_received"]), "Cash (confirmed)", "success", "💵", small=True),
        kpi_card("Cash preferences", str(stats["cash_choices"]), "Cash at counter", "neutral", "🪙", small=True),
    ])

    left, right = st.columns([1.5, 1], gap="large")
    with left:
        section_title("Recent invoices", "Newest first")
        if invoices:
            data_table(
                ["Invoice", "Customer", "Amount", "Payment", "Preference"],
                [
                    [
                        f'<span class="strong">{esc(inv["invoice_no"])}</span>', esc(inv["customer_name"]),
                        f'<span class="strong">{esc(money_inr(inv["grand_total"]))}</span>',
                        payment_pill(inv["payment_status"]), preference_pill(inv["payment_preference"]),
                    ]
                    for inv in invoices
                ],
                right=(2,),
            )
        else:
            st.markdown('<div class="tbl-empty">No invoices yet. Create your first invoice from the sidebar.</div>', unsafe_allow_html=True)
    with right:
        section_title("Fixed customer QR", "Same code on every invoice")
        with st.container(key="card_qr"):
            url = customer_portal_url()
            qr_data = make_qr_image(url)
            st.image(qr_data, width=190)
            st.caption("Use this same QR on every invoice. Customers enter invoice number + their registered 10-digit mobile number.")
            st.download_button("Download fixed QR (PNG)", data=qr_data.getvalue(), file_name="customer-payment-preference-qr.png", mime="image/png", use_container_width=True)
            st.code(url, language=None)
            if "localhost" in url or "127.0.0.1" in url:
                st.warning("This QR uses localhost and will only open on the same computer. Set PUBLIC_APP_URL in .env to your computer's LAN address or deployed HTTPS URL for phone scanning.")

    responses = [i for i in invoices if i.get("payment_preference")]
    section_title("Customer payment requests", "Latest 20")
    if responses:
        data_table(
            ["Invoice", "Customer", "Amount", "Request", "Received", "Updated"],
            [
                [
                    f'<span class="strong">{esc(inv["invoice_no"])}</span>', esc(inv["customer_name"]),
                    f'<span class="strong">{esc(money_inr(inv["grand_total"]))}</span>',
                    preference_pill(inv["payment_preference"]),
                    pill("Paid", "success") if inv["payment_status"] == "PAID" else pill("Awaiting payment", "warning"),
                    esc(inv.get("preference_updated_at") or "—"),
                ]
                for inv in responses[:20]
            ],
            right=(2,),
        )
    else:
        st.markdown('<div class="tbl-empty">No customer payment preferences have been submitted yet.</div>', unsafe_allow_html=True)

    section_title("Update payment record", "Confirm only after you have verified the money")
    unpaid = [i for i in invoices if i["payment_status"] != "PAID"]
    with st.container(key="card_update"):
        if unpaid:
            options = {f"{i['invoice_no']} · {i['customer_name']} · {money_inr(i['grand_total'])}": i for i in unpaid}
            selected_label = st.selectbox("Choose an unpaid invoice", list(options.keys()))
            chosen = options[selected_label]
            method = st.selectbox("Payment received via", ["Cash", "UPI (verified manually)", "Bank transfer", "Online (verified manually)", "Other"])
            st.caption("Only mark an invoice paid after you have independently confirmed that the money was received in your account or at the counter.")
            if st.button("Confirm payment received", type="primary"):
                mark_invoice_paid(chosen["id"], method)
                st.success(f"{chosen['invoice_no']} marked as paid.")
                st.rerun()
        else:
            st.success("All invoices are currently marked as paid.")

def render_create_invoice() -> None:
    settings = get_settings()
    show_hero("Create a GST invoice", "Add customer and item details. GST will be split automatically based on the states.")
    if not settings.get("business_name"):
        st.warning("Complete your business profile in Settings before creating your first invoice.")

    with st.form("create_invoice_form", clear_on_submit=False):
        form_step(1, "Customer details", "Who is this invoice for?")
        c1, c2 = st.columns(2)
        customer_name = c1.text_input("Customer / business name *")
        customer_phone = c2.text_input("Mobile number *", placeholder="10-digit Indian mobile number")
        c3, c4 = st.columns(2)
        customer_email = c3.text_input("Email (optional)")
        customer_gstin = c4.text_input("Customer GSTIN (optional)")
        customer_address = st.text_area("Billing address *", height=75)
        customer_state = st.selectbox("Place of supply / customer state *", INDIAN_STATES, index=INDIAN_STATES.index("Karnataka"))

        form_step(2, "Invoice details", "Dates and the automatic invoice number")
        d1, d2, d3 = st.columns(3)
        issue_date = d1.date_input("Invoice date", value=date.today())
        due_date = d2.date_input("Due date", value=date.today() + timedelta(days=7))
        invoice_no_preview = make_invoice_number(issue_date)
        d3.text_input("Invoice number (automatic)", value=invoice_no_preview, disabled=True)

        form_step(3, "Items and GST", "Prices before tax, one row per item")
        st.caption("Enter prices before GST. Choose the rate that applies to each HSN/SAC classification. Common post-22 September 2025 slabs include 5%, 18% and 40%; special, exempt and notified categories can differ.")
        default_df = pd.DataFrame([{"Description": "", "HSN/SAC": "", "Quantity": 1.0, "Unit (UQC)": "NOS", "Unit price (₹)": 0.0, "GST rate (%)": 18.0}])
        items_df = st.data_editor(
            default_df, num_rows="dynamic", use_container_width=True, hide_index=True,
            column_config={
                "Description": st.column_config.TextColumn("Description", required=True, width="large"),
                "HSN/SAC": st.column_config.TextColumn("HSN/SAC", required=True),
                "Quantity": st.column_config.NumberColumn("Quantity", min_value=0.01, step=1.0, format="%.2f", required=True),
                "Unit (UQC)": st.column_config.TextColumn("Unit (UQC)", help="e.g. NOS, KGS, HRS, OTH", required=True),
                "Unit price (₹)": st.column_config.NumberColumn("Unit price (₹)", min_value=0.0, step=10.0, format="%.2f", required=True),
                "GST rate (%)": st.column_config.NumberColumn("GST rate (%)", min_value=0.0, max_value=100.0, step=0.25, format="%.2f", required=True),
            },
        )
        reverse_charge = st.checkbox("Tax payable on reverse charge basis?", value=False)
        notes = st.text_area("Notes / terms (optional)", height=60)
        submitted = st.form_submit_button("Create invoice", type="primary", use_container_width=True)

    if submitted:
        errors = []
        required_seller = ["business_name", "business_gstin", "business_address", "business_state"]
        if any(not settings.get(k) for k in required_seller):
            errors.append("Complete business name, address and state in Settings first.")
        if not customer_name.strip():
            errors.append("Customer name is required.")
        phone_digits = re.sub(r"\D", "", customer_phone)
        if not re.fullmatch(r"[6-9]\d{9}", phone_digits):
            errors.append("Enter a valid 10-digit Indian mobile number. The customer QR portal verifies this registered mobile number.")
        if not customer_address.strip():
            errors.append("Customer billing address is required.")
        valid_items = []
        for row_number, row in items_df.iterrows():
            def cell_value(column: str, default: Any = "") -> Any:
                value = row.get(column, default)
                try:
                    if pd.isna(value):
                        return default
                except (TypeError, ValueError):
                    pass
                return value

            desc = str(cell_value("Description", "")).strip()
            if not desc:
                continue
            valid_items.append({
                "description": desc, "hsn": str(cell_value("HSN/SAC", "")).strip(),
                "uqc": str(cell_value("Unit (UQC)", "NOS")).strip() or "NOS",
                "quantity": cell_value("Quantity", 0), "unit_price": cell_value("Unit price (₹)", 0),
                "gst_rate": cell_value("GST rate (%)", 18),
            })
        try:
            totals = calculate_invoice(valid_items, settings.get("business_state", ""), customer_state)
            seller_gstin = settings.get("business_gstin", "").strip().upper()
            customer_gstin_clean = customer_gstin.strip().upper()
            gstin_pattern = r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]"
            if not re.fullmatch(gstin_pattern, seller_gstin):
                errors.append("A valid 15-character supplier GSTIN is required for this TAX INVOICE.")
            elif seller_gstin[:2] != STATE_CODES.get(settings.get("business_state", ""), ""):
                errors.append("The first two digits of the supplier GSTIN do not match the business state selected in Settings.")
            if any(not item["hsn"] for item in totals["items"]):
                errors.append("Enter the applicable HSN code or SAC for every invoice item.")
            if customer_gstin_clean and not re.fullmatch(gstin_pattern, customer_gstin_clean):
                errors.append("Customer GSTIN format is invalid. Check the 15-character GSTIN.")
            if due_date < issue_date:
                errors.append("Due date cannot be earlier than invoice date.")
        except Exception as exc:
            errors.append(str(exc))
            totals = None
        if errors:
            for error in errors:
                st.error(error)
        elif totals:
            invoice_no = make_invoice_number(issue_date)
            payload = {
                "invoice_no": invoice_no, "issue_date": issue_date.isoformat(),
                "due_date": due_date.isoformat(), "customer_name": customer_name.strip(),
                "customer_phone": phone_digits, "customer_email": customer_email.strip(),
                "customer_gstin": customer_gstin.strip().upper(),
                "customer_address": customer_address.strip(), "customer_state": customer_state,
                "seller_name": settings.get("business_name", ""),
                "seller_gstin": settings.get("business_gstin", ""),
                "seller_address": settings.get("business_address", ""),
                "seller_state": settings.get("business_state", ""),
                "seller_phone": settings.get("business_phone", ""),
                "seller_email": settings.get("business_email", ""),
                "reverse_charge": "Yes" if reverse_charge else "No", "notes": notes.strip(),
            }
            try:
                new_id = insert_invoice(payload, totals)
                st.session_state["new_invoice_no"] = invoice_no
                st.success(f"Invoice {invoice_no} created successfully.")
                st.rerun()
            except sqlite3.IntegrityError:
                st.error("Invoice number collision. Please submit again.")

    latest_no = st.session_state.get("new_invoice_no")
    if latest_no:
        invoice = get_invoice(latest_no)
        if invoice:
            st.divider()
            st.success(f"Ready to download: {latest_no}")
            p1, p2 = st.columns([1, 1])
            with p1:
                render_kpis([kpi_card(
                    "Invoice total", money_inr(invoice["grand_total"]),
                    "GST type: " + ("CGST + SGST" if invoice["seller_state"] == invoice["customer_state"] else "IGST"),
                    "success", "🧾",
                )])
            with p2:
                st.image(make_qr_image(customer_portal_url()), width=130)
                st.caption("Fixed QR included in the invoice PDF")
            render_invoice_download(invoice)


def render_invoice_download(invoice: dict[str, Any]) -> None:
    languages = get_languages()
    language_names = list(languages.keys())
    default_name = "English" if "English" in languages else language_names[0]
    lang_name = st.selectbox("Invoice language", language_names, index=language_names.index(default_name), key=f"lang_{invoice['id']}")
    lang_code = languages[lang_name]
    if lang_code != "en" and not os.getenv("GOOGLE_TRANSLATE_API_KEY", "").strip():
        st.info("Translation requires a Google Cloud Translation API key. English PDF downloads work without it.")
    if st.button("Prepare PDF", key=f"prepare_{invoice['id']}", type="primary"):
        try:
            pdf_bytes = build_invoice_pdf(invoice, lang_code)
            st.session_state[f"pdf_{invoice['id']}_{lang_code}"] = pdf_bytes
            st.success(f"{lang_name} invoice is ready.")
        except Exception as exc:
            st.error(str(exc))
    prepared = st.session_state.get(f"pdf_{invoice['id']}_{lang_code}")
    if prepared:
        st.download_button(
            "Download invoice PDF", data=prepared, file_name=f"{invoice['invoice_no']}-{lang_code}.pdf",
            mime="application/pdf", key=f"download_{invoice['id']}_{lang_code}", use_container_width=True,
        )


def render_invoice_library() -> None:
    show_hero("Invoice library", "Find, download and manage invoices already created.", "Library")
    invoices = list_invoices()
    if not invoices:
        st.markdown('<div class="tbl-empty">Your invoice library is empty.</div>', unsafe_allow_html=True)
        return
    options = {f"{i['invoice_no']} · {i['customer_name']} · {money_inr(i['grand_total'])}": i for i in invoices}
    selected = st.selectbox("Select invoice", list(options.keys()))
    invoice = get_invoice(options[selected]["invoice_no"])
    if not invoice:
        st.error("Invoice not found.")
        return
    is_paid = invoice["payment_status"] == "PAID"
    choice_label = {"ONLINE": "Online requested", "CASH": "Cash at counter", None: "No response"}.get(invoice["payment_preference"], "No response")
    render_kpis([
        kpi_card("Invoice total", money_inr(invoice["grand_total"]), "", "neutral", "🧾"),
        kpi_card("Payment status", invoice["payment_status"], "", "success" if is_paid else "warning", "✅" if is_paid else "⏳"),
        kpi_card("Customer choice", choice_label, "", "info", "📲"),
    ])
    st.markdown(
        '<div class="summary">'
        f'<div><div class="s-label">Customer</div><div class="s-val">{esc(invoice["customer_name"])}</div></div>'
        f'<div><div class="s-label">Issued</div><div class="s-val">{esc(invoice["issue_date"])}</div></div>'
        f'<div><div class="s-label">Due</div><div class="s-val">{esc(invoice.get("due_date") or "—")}</div></div>'
        f'<div><div class="s-label">Place of supply</div><div class="s-val">{esc(invoice["customer_state"])}</div></div>'
        "</div>",
        unsafe_allow_html=True,
    )
    section_title("Download / translate")
    with st.container(key="card_download"):
        render_invoice_download(invoice)

def render_settings() -> None:
    show_hero("Business settings", "Set the supplier details printed on every invoice and configure the customer QR.", "Settings")
    current = get_settings()
    with st.form("business_settings_form"):
        form_step(1, "Business profile", "Printed on every invoice")
        business_name = st.text_input("Business / legal name *", value=current.get("business_name", ""))
        business_gstin = st.text_input("Business GSTIN *", value=current.get("business_gstin", ""))
        business_address = st.text_area("Registered business address *", value=current.get("business_address", ""), height=85)
        state_index = INDIAN_STATES.index(current.get("business_state")) if current.get("business_state") in INDIAN_STATES else INDIAN_STATES.index("Karnataka")
        business_state = st.selectbox("State *", INDIAN_STATES, index=state_index)
        c1, c2 = st.columns(2)
        business_phone = c1.text_input("Business phone", value=current.get("business_phone", ""))
        business_email = c2.text_input("Business email", value=current.get("business_email", ""))
        saved = st.form_submit_button("Save business profile", type="primary", use_container_width=True)
    if saved:
        if not business_name.strip() or not business_address.strip():
            st.error("Business name and address are required.")
        else:
            save_settings({
                "business_name": business_name, "business_gstin": business_gstin.upper(),
                "business_address": business_address, "business_state": business_state,
                "business_phone": business_phone, "business_email": business_email,
            })
            st.success("Business profile saved.")
            st.rerun()

    section_title("Customer QR configuration", "One fixed code for every bill")
    with st.container(key="card_qr_config"):
        url = customer_portal_url()
        st.write("The same fixed QR is placed on every bill. It links to the customer preference page; it does not initiate or collect a payment.")
        st.code(url, language=None)
        st.image(make_qr_image(url), width=210)
        st.download_button("Download fixed QR", data=make_qr_image(url).getvalue(), file_name="customer-payment-preference-qr.png", mime="image/png")
        st.info("To make the QR work on a customer's phone, set PUBLIC_APP_URL in .env to a reachable LAN URL or HTTPS deployment URL. Do not use localhost for phone scanning.")

    section_title("Translation setup")
    with st.container(key="card_translation"):
        if os.getenv("GOOGLE_TRANSLATE_API_KEY", "").strip():
            st.success("Google Cloud Translation API key detected.")
        else:
            st.warning("No Google Cloud Translation API key found. English invoices work; one-click translation is disabled until you add the key to .env.")
        st.caption("Create a Google Cloud Translation API key, enable the Cloud Translation API, and store the key in GOOGLE_TRANSLATE_API_KEY. Never paste the key into this chat or commit it to Git.")

    section_title("Admin password")
    with st.container(key="card_admin"):
        st.caption("Set ADMIN_PASSWORD in your .env file, then restart the app. The dashboard is password-protected; the customer QR page remains public.")

def main() -> None:
    view = st.query_params.get("view", "")
    if view == "customer":
        render_customer_portal()
        return

    with st.sidebar:
        st.markdown(
            '<div class="brand"><div class="brand-mark">L</div><div><div class="brand-name">Ledgerly</div>'
            '<div class="brand-sub">GST invoice workspace</div></div></div>',
            unsafe_allow_html=True,
        )
        st.divider()
        nav_icons = {"Dashboard": "📊", "Create invoice": "🧾", "Invoice library": "🗂️", "Settings": "⚙️"}
        page = st.radio(
            "Workspace", ["Dashboard", "Create invoice", "Invoice library", "Settings"],
            label_visibility="collapsed", format_func=lambda name: f"{nav_icons[name]}  {name}",
        )
        st.divider()
        if st.session_state.get("admin_authenticated"):
            st.markdown('<span class="sb-pill">Admin session active</span>', unsafe_allow_html=True)
            if st.button("Sign out", use_container_width=True):
                st.session_state["admin_authenticated"] = False
                st.rerun()
        st.caption("Local-first • SQLite")

    if page == "Dashboard":
        render_dashboard()
    elif page == "Create invoice":
        if admin_login():
            render_create_invoice()
    elif page == "Invoice library":
        if admin_login():
            render_invoice_library()
    elif page == "Settings":
        if admin_login():
            render_settings()


if __name__ == "__main__":
    main()
