r"""Extract hotel invoice fields from text-based PDFs and store them in PostgreSQL.

Usage:
    python invoice_extractor.py "C:\Users\Khushi\Downloads\MMT\MMT\Khushi-Hotel\Test"

Set PGHOST, PGPORT, PGDATABASE, PGUSER, and PGPASSWORD in the environment.
The database must already exist. The script creates invoice_details if needed.
"""

from __future__ import annotations

import argparse
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

import pdfplumber
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

load_dotenv()


FIELDS = [
    "booking_id", "invoice_no", "invoice_date", "hsn_sac", "mmt_gstin",
    "mmt_pan", "company_address", "company_gstin",
    "customer_or_passanger_name", "basic_amount", "grand_total",
    "hotel_name", "hotel_city", "check_in_date", "check_out_date",
]

# Date formats commonly printed on invoices. PostgreSQL receives ISO dates.
DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d",
                "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y")


def _positioned_value(pdf_path: str | Path, heading: str, multiline: bool = False) -> Optional[str]:
    """Read a value below a column heading using PDF word coordinates."""
    heading_re = re.compile(heading, re.I)
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            words = page.extract_words() or []
            # Group words into visual rows, preserving their x positions.
            rows = []
            for word in sorted(words, key=lambda item: (item["top"], item["x0"])):
                row = next((r for r in rows if abs(r[0]["top"] - word["top"]) <= 2.5), None)
                if row is None:
                    rows.append([word])
                else:
                    row.append(word)
            rows.sort(key=lambda row: min(w["top"] for w in row))
            for row_index, row in enumerate(rows[:-1]):
                row.sort(key=lambda item: item["x0"])
                row_text = " ".join(word["text"] for word in row)
                match = heading_re.search(row_text)
                if not match:
                    continue
                # Map the matched heading words to their horizontal position.
                words_in_match = []
                for start in range(len(row)):
                    for end in range(start + 1, len(row) + 1):
                        phrase = " ".join(w["text"] for w in row[start:end])
                        if heading_re.fullmatch(phrase):
                            words_in_match = row[start:end]
                            break
                    if words_in_match:
                        break
                x_start = min(w["x0"] for w in words_in_match) if words_in_match else row[0]["x0"]
                heading_end = max(w["x1"] for w in words_in_match) if words_in_match else x_start
                later_headers = [w["x0"] for w in row if w["x0"] > heading_end + 2 and
                                 re.match(r"^(?:PAN|HSN/SAC|GSTIN|CIN|Hotel|City|Check-in|Check-Out)$", w["text"], re.I)]
                x_end = min(later_headers) if later_headers else float("inf")
                header_top = min(w["top"] for w in row)
                data_rows = []
                for candidate_row in rows[row_index + 1:]:
                    row_top = min(w["top"] for w in candidate_row)
                    if row_top <= header_top or row_top - header_top > (32 if multiline else 20):
                        if data_rows:
                            break
                        continue
                    column_words = [w for w in candidate_row
                                    if w["x0"] >= x_start - 2 and w["x0"] < x_end - 2]
                    if column_words:
                        data_rows.append(" ".join(w["text"] for w in sorted(column_words, key=lambda item: item["x0"])))
                    elif data_rows:
                        break
                    if not multiline and data_rows:
                        break
                if data_rows:
                    return _clean(" ".join(data_rows))
    return None


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = re.sub(r"\s+", " ", value).strip(" \t:|,-")
    return value or None


def _label_value(text: str, labels: str) -> Optional[str]:
    """Find a value following one of the supplied label alternatives."""
    match = re.search(
        rf"(?im)(?:^|\n)\s*(?:{labels})\s*(?:[:#\-]|\s)\s*([^\n]+)", text
    )
    return _clean(match.group(1)) if match else None


def _line_value(text: str, label: str) -> Optional[str]:
    """Return text after a label even when the label is not at line start."""
    match = re.search(rf"(?im)^.*?{label}\s*[:#-]?\s*([^\n]+)$", text)
    return _clean(match.group(1)) if match else None


def _company_letterhead_address(text: str) -> Optional[str]:
    """Extract the MakeMyTrip company address printed below its letterhead."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    company_index = next((i for i, line in enumerate(lines)
                          if re.search(r"MAKEMYTRIP\s*\(INDIA\)|MAKEMYTRIP\s+INDIA|MAKEMYTRIP", line, re.I)), None)
    if company_index is None:
        return None
    address_parts = []
    for line in lines[company_index + 1:]:
        if re.search(r"Booking\s+ID", line, re.I):
            break
        if re.search(r"TAX\s+INVOICE|REFUND\s+VOUCHER", line, re.I):
            continue
        if re.search(r"\b(?:Floor|Epitome|Building|Cybercity|Cyber\s*City|Gurgaon|Gurugram|Haryana|India|No\.)\b", line, re.I):
            address_parts.append(line)
        if len(address_parts) >= 4:
            break
    joined = ", ".join(address_parts)
    joined = re.sub(r"\s*,\s*,+", ",", joined)
    return _clean(joined)


def _date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    candidate = value.strip()
    candidate = re.sub(r"^[A-Za-z]{3,9},?\s+", "", candidate)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).date().isoformat()
        except ValueError:
            pass
    # Find a date inside a longer value (for example a date with a weekday).
    for fmt in DATE_FORMATS:
        pattern = {
            "%d-%m-%Y": r"\b\d{1,2}-\d{1,2}-\d{4}\b",
            "%d/%m/%Y": r"\b\d{1,2}/\d{1,2}/\d{4}\b",
            "%d.%m.%Y": r"\b\d{1,2}\.\d{1,2}\.\d{4}\b",
            "%Y-%m-%d": r"\b\d{4}-\d{1,2}-\d{1,2}\b",
            "%d %b %Y": r"\b\d{1,2} [A-Za-z]{3} \d{4}\b",
            "%d %B %Y": r"\b\d{1,2} [A-Za-z]+ \d{4}\b",
            "%b %d, %Y": r"\b[A-Za-z]{3} \d{1,2}, \d{4}\b",
            "%B %d, %Y": r"\b[A-Za-z]+ \d{1,2}, \d{4}\b",
        }[fmt]
        m = re.search(pattern, candidate)
        if m:
            try:
                return datetime.strptime(m.group(), fmt).date().isoformat()
            except ValueError:
                continue
    return None


def _amount(value: Optional[str]) -> Optional[Decimal]:
    if not value:
        return None
    # Remove currency symbols, grouping commas and surrounding text; preserve sign/decimal.
    m = re.search(r"-?\s*(?:\d[\d,]*)(?:\.\d{1,2})?", value)
    if not m:
        return None
    try:
        return Decimal(m.group().replace(",", "").replace(" ", "")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _labeled_amount(text: str, labels: str) -> Optional[Decimal]:
    """Find a currency amount after a matching label, including wrapped labels."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    label_re = re.compile(labels, re.I)
    for index, line in enumerate(lines):
        match = label_re.search(line)
        if not match:
            continue
        # Prefer the amount on the same line as the label.
        value = _amount(line[match.end():])
        if value is not None:
            return value
        # Some PDF text layers put a wrapped amount on the next line.
        if index + 1 < len(lines) and re.match(r"^[₹$€£?]?\s*-?\s*\d", lines[index + 1]):
            value = _amount(lines[index + 1])
            if value is not None:
                return value
    return None


def _extract_text(pdf_path: str | Path) -> str:
    pages = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text(layout=False) or "")
    return "\n".join(pages)


def extract_invoice_data(pdf_path: str | Path) -> dict:
    """Return one dictionary containing all requested keys (unfound values are None).

    This is regex-based and intended for text PDFs. Scanned/image-only PDFs need
    OCR, which this implementation deliberately does not use.
    """
    text = _extract_text(pdf_path)
    invoice_date_position = _positioned_value(pdf_path, r"Date")
    hsn_position = _positioned_value(pdf_path, r"HSN\s*/?\s*SAC")
    guest_position = _positioned_value(pdf_path, r"Customer\s+Name")
    hotel_position = _positioned_value(pdf_path, r"Hotel\s+Name", multiline=True)
    city_position = _positioned_value(pdf_path, r"Hotel\s+City")
    checkin_position = _positioned_value(pdf_path, r"Check[- ]?in")
    checkout_position = _positioned_value(pdf_path, r"Check[- ]?out")
    # Prefix-anchored IDs are found anywhere in the document because templates vary.
    booking = re.search(r"\bNH[A-Z0-9/-]*\b", text, re.I)
    invoice = re.search(r"\bM0[A-Z0-9/-]*\b", text, re.I)
    gstins = re.findall(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b", text, re.I)
    pan = re.search(r"\b[A-Z]{5}\d{4}[A-Z]\b", text, re.I)

    # GSTIN for MakeMyTrip may be identified by a nearby label; fallback to first GSTIN.
    mmt_gstin = _label_value(text, r"(?:MMT|MakeMyTrip)(?:\s+India)?\s+GSTIN")
    company_gstin = _label_value(text, r"(?:Company\s+)?GSTIN|GST\s*(?:No\.?|Number)")
    if not mmt_gstin and gstins:
        mmt_gstin = gstins[0]
    if not company_gstin and len(gstins) > 1:
        company_gstin = gstins[1]

    data = {key: None for key in FIELDS}
    data.update({
        "booking_id": booking.group(0).upper() if booking else _label_value(text, r"Booking\s*(?:ID|Id|No\.?)"),
        "invoice_no": invoice.group(0).upper() if invoice else _label_value(text, r"Invoice\s*(?:No\.?|Number)"),
        "invoice_date": _date(invoice_date_position or _label_value(text, r"Invoice\s*Date|Date\s+of\s+Invoice")),
        "hsn_sac": hsn_position or _label_value(text, r"HSN\s*/?\s*SAC|HSN\s+Code|SAC\s+Code"),
        "mmt_gstin": mmt_gstin,
        "mmt_pan": _label_value(text, r"(?:MMT|MakeMyTrip)(?:\s+India)?\s+PAN") or (pan.group(0) if pan else None),
        "company_address": _company_letterhead_address(text) or _label_value(text, r"(?:Registered\s+)?Company\s+Address|Corporate\s+Address"),
        "company_gstin": company_gstin,
        "customer_or_passanger_name": guest_position or _label_value(text, r"Customer\s+Name|Passenger\s+Name|Passanger\s+Name|Guest\s+Name|Name\s+of\s+Guest"),
        # Use numeric zero when a value is absent/unreadable so PostgreSQL stores 0.00, not NULL.
        "basic_amount": _labeled_amount(text, r"Basic\s+Amount|Taxable\s+Value|Base\s+Fare|Accommodation\s+Charges") or Decimal("0.00"),
        "grand_total": _labeled_amount(text, r"Grand\s+Total|Total\s+Amount|Invoice\s+Total|Amount\s+Payable") or Decimal("0.00"),
        "hotel_name": hotel_position or _label_value(text, r"Hotel\s+Name|Property\s+Name|Accommodation"),
        "hotel_city": city_position or _label_value(text, r"Hotel\s+City|City"),
        "check_in_date": _date(checkin_position or _label_value(text, r"Check[- ]?in(?:\s+Date)?|Arrival\s+Date")),
        "check_out_date": _date(checkout_position or _label_value(text, r"Check[- ]?out(?:\s+Date)?|Departure\s+Date")),
    })
    # Keep only the identifier starting prefixes required by the table constraints.
    for key, prefix in (("booking_id", "NH"), ("invoice_no", "M0")):
        value = data[key]
        if value:
            # Restrict label-derived values to the same identifier shape as text matches.
            match = re.search(rf"\b{prefix}[A-Z0-9/-]*\b", value.upper())
            data[key] = match.group(0) if match else None
    return data


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS invoice_details (

    id SERIAL PRIMARY KEY,

    booking_id VARCHAR(50)
        CHECK (booking_id IS NULL OR booking_id ~ '^NH'),

    invoice_no VARCHAR(50) NOT NULL UNIQUE
        CHECK (invoice_no ~ '^M0'),

    invoice_date DATE,

    hsn_sac TEXT,

    mmt_gstin VARCHAR(20),

    mmt_pan VARCHAR(15),

    company_address TEXT,

    company_gstin VARCHAR(20),

    customer_or_passanger_name VARCHAR(255),

    basic_amount NUMERIC(12,2),

    grand_total NUMERIC(12,2),

    hotel_name VARCHAR(255),

    hotel_city VARCHAR(100),

    check_in_date DATE,

    check_out_date DATE,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

);
"""

INSERT_SQL = """
INSERT INTO invoice_details (
 booking_id, invoice_no, invoice_date, hsn_sac, mmt_gstin, mmt_pan,
 company_address, company_gstin, customer_or_passanger_name, basic_amount,
 grand_total, hotel_name, hotel_city, check_in_date, check_out_date
) VALUES %s
ON CONFLICT (invoice_no) DO UPDATE SET
 booking_id = COALESCE(EXCLUDED.booking_id, invoice_details.booking_id),
 invoice_date = COALESCE(EXCLUDED.invoice_date, invoice_details.invoice_date),
 hsn_sac = COALESCE(EXCLUDED.hsn_sac, invoice_details.hsn_sac),
 mmt_gstin = COALESCE(EXCLUDED.mmt_gstin, invoice_details.mmt_gstin),
 mmt_pan = COALESCE(EXCLUDED.mmt_pan, invoice_details.mmt_pan),
 company_address = COALESCE(EXCLUDED.company_address, invoice_details.company_address),
 company_gstin = COALESCE(EXCLUDED.company_gstin, invoice_details.company_gstin),
 customer_or_passanger_name = COALESCE(EXCLUDED.customer_or_passanger_name, invoice_details.customer_or_passanger_name),
 basic_amount = COALESCE(EXCLUDED.basic_amount, invoice_details.basic_amount),
 grand_total = COALESCE(EXCLUDED.grand_total, invoice_details.grand_total),
 hotel_name = COALESCE(EXCLUDED.hotel_name, invoice_details.hotel_name),
 hotel_city = COALESCE(EXCLUDED.hotel_city, invoice_details.hotel_city),
 check_in_date = COALESCE(EXCLUDED.check_in_date, invoice_details.check_in_date),
 check_out_date = COALESCE(EXCLUDED.check_out_date, invoice_details.check_out_date)
"""


def save_to_postgres(records: list[dict]) -> int:
    """Create the table if missing and insert uniquely identified records.

    Rows without an invoice number are skipped because they cannot be safely
    deduplicated. PostgreSQL's unique constraint also protects concurrent runs.
    """
    if not records:
        return 0
    identified = [record for record in records if record.get("invoice_no") and
                  re.fullmatch(r"M0[A-Z0-9/-]*", str(record["invoice_no"]).upper())]
    for record in identified:
        record["invoice_no"] = str(record["invoice_no"]).upper()
        booking_id = record.get("booking_id")
        if booking_id:
            match = re.fullmatch(r"NH[A-Z0-9/-]*", str(booking_id).upper())
            record["booking_id"] = match.group(0) if match else None
    skipped = len(records) - len(identified)
    if skipped:
        print(f"Skipped {skipped} record(s) without a valid M0 invoice number; cannot safely deduplicate them.")
    if not identified:
        return 0
    config = {
        "host": os.getenv("PGHOST", "localhost"),
        "port": os.getenv("PGPORT", "5432"),
        "dbname": os.getenv("PGDATABASE", "invoice_extraction_db"),
        "user": os.getenv("PGUSER", "postgres"),
        "password": os.getenv("PGPASSWORD", ""),
    }
    # PostgreSQL rejects a single INSERT ... ON CONFLICT statement if the same
    # invoice_no appears more than once in its VALUES list. Keep the last PDF
    # encountered for each invoice number and report every duplicate source.
    unique_by_invoice = {}
    duplicate_sources = {}
    for record in identified:
        invoice_no = record["invoice_no"]
        previous = unique_by_invoice.get(invoice_no)
        if previous is not None:
            duplicate_sources.setdefault(invoice_no, [previous.get("_source_file", "(unknown file)")])
            duplicate_sources[invoice_no].append(record.get("_source_file", "(unknown file)"))
        unique_by_invoice[invoice_no] = record
    for invoice_no, sources in duplicate_sources.items():
        print(f"Duplicate invoice number {invoice_no} in: {', '.join(sources)}; using the last file.")
    identified = list(unique_by_invoice.values())
    rows = [tuple(record.get(field) for field in FIELDS) for record in identified]
    with psycopg2.connect(**config) as connection:
        with connection.cursor() as cursor:
            cursor.execute(CREATE_TABLE_SQL)
            # Upgrade databases created with the older, length-limited column.
            cursor.execute("ALTER TABLE invoice_details ALTER COLUMN hsn_sac TYPE TEXT")
            execute_values(cursor, INSERT_SQL, rows)
            return cursor.rowcount


def process_folder(folder_path: str | Path) -> list[dict]:
    """Extract every .pdf in a folder and insert successful reads into PostgreSQL."""
    folder = Path(folder_path)
    if not folder.is_dir():
        raise NotADirectoryError(f"PDF folder does not exist: {folder}")
    results = []
    for pdf_path in sorted(folder.glob("*.pdf")):
        try:
            record = extract_invoice_data(pdf_path)
            record["_source_file"] = pdf_path.name
            results.append(record)
            print(f"Extracted: {pdf_path.name}")
        except Exception as exc:
            print(f"Failed: {pdf_path.name}: {exc}")
    # Strip the display-only filename from the database payload.
    inserted = save_to_postgres(results)
    print(f"Inserted or updated {inserted} invoice row(s); invalid or missing invoice numbers were skipped.")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract hotel invoice data from PDFs into PostgreSQL")
    parser.add_argument("folder", help="Folder containing text-based PDF invoices")
    args = parser.parse_args()
    process_folder(args.folder)
