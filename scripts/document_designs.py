"""Clearly synthetic, type-specific layouts for the case-study documents."""
import hashlib
import re
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth

PALETTES = [
    ("#17365D", "#EAF0F7"),
    ("#1F5B54", "#E8F3EF"),
    ("#553E69", "#F1EBF5"),
]
WIDTH, HEIGHT = A4


def _text(pdf, value: str, x: float, y: float, width: float, *, size: int = 12,
          bold: bool = False, leading: int = 17) -> float:
    font = "Helvetica-Bold" if bold else "Helvetica"
    pdf.setFont(font, size)
    for line in simpleSplit(str(value), font, size, width):
        if stringWidth(line, font, size) > width + 1:
            raise ValueError("Document field contains a word too wide for this layout")
        pdf.drawString(x, y, line)
        y -= leading
    return y


def _label(pdf, label: str, value: str, x: float, y: float, width: float, accent) -> float:
    pdf.setFillColor(accent)
    y = _text(pdf, label.upper(), x, y, width, size=9, bold=True, leading=13)
    pdf.setFillColor(colors.HexColor("#1B2738"))
    lines = simpleSplit(str(value or "N/A"), "Helvetica", 12, width)
    if len(lines) > 3:
        raise ValueError(f"{label} is too long for the synthetic document layout")
    return _text(pdf, value or "N/A", x, y - 3, width, size=12, leading=17) - 20


def _page_frame(pdf, case_id: str, kind: str, variant: int, title: str, subtitle: str):
    dark, pale = (colors.HexColor(code) for code in PALETTES[variant % len(PALETTES)])
    pdf.setFillColor(colors.white)
    pdf.rect(0, 0, WIDTH, HEIGHT, fill=1, stroke=0)
    if variant % 2:
        pdf.setFillColor(pale)
        pdf.rect(0, HEIGHT - 130, WIDTH, 130, fill=1, stroke=0)
        pdf.setFillColor(dark)
        pdf.rect(38, HEIGHT - 135, 6, 101, fill=1, stroke=0)
    else:
        pdf.setFillColor(dark)
        pdf.rect(0, HEIGHT - 92, WIDTH, 92, fill=1, stroke=0)
    pdf.setFillColor(dark if variant % 2 else colors.white)
    _text(pdf, title, 54, HEIGHT - 51, WIDTH - 108, size=18, bold=True, leading=22)
    _text(pdf, subtitle, 54, HEIGHT - 75, WIDTH - 108, size=9, leading=12)
    # Keep the document body clear. The footer identifies demo evidence without
    # overlapping field values or interfering with PDF text extraction.
    pdf.setStrokeColor(colors.HexColor("#D4DAE2"))
    pdf.line(44, 74, WIDTH - 44, 74)
    pdf.setFillColor(colors.HexColor("#657184"))
    _text(pdf, f"Case {case_id}  |  {kind.replace('_', ' ').title()}  |  Demo evidence only", 45, 58,
          WIDTH - 90, size=9, leading=12)
    _text(pdf, "Not issued by a bank, tax authority, or company registry.", 45, 43,
          WIDTH - 90, size=8, leading=10)
    return dark, pale


def draw_document_page(pdf, kind: str, values: dict, case_id: str, variant: int,
                       unreadable: bool = False) -> None:
    """Draw one A4 page and advance the canvas."""
    if unreadable and kind == "bank_proof":
        values = {**values, "bank_account": "[OBSCURED - request a clearer copy]"}
    if kind == "pan":
        titles = ["PAN Card - Synthetic Sample", "Permanent Account Number - Demo", "Tax Identifier Summary - PAN"]
        dark, pale = _page_frame(pdf, case_id, kind, variant, titles[variant % 3],
                                 "Tax identity evidence - sample layout")
        pdf.setFillColor(pale)
        pdf.roundRect(55, 360, WIDTH - 110, 310, 16, fill=1, stroke=0)
        pdf.setStrokeColor(dark)
        pdf.roundRect(55, 360, WIDTH - 110, 310, 16, fill=0, stroke=1)
        pdf.setFillColor(dark)
        _text(pdf, "PERMANENT ACCOUNT NUMBER", 82, 625, WIDTH - 165, size=12, bold=True)
        y = _label(pdf, "Name on PAN", values["legal_name"], 82, 565, WIDTH - 165, dark)
        _label(pdf, "PAN", values["pan"], 82, y - 4, WIDTH - 165, dark)
        pdf.setFillColor(colors.HexColor("#475569"))
        _text(pdf, "Check the visible name and identifier against the submitted form.",
              82, 410, WIDTH - 165, size=9)
    elif kind == "gst":
        titles = ["GST Registration Summary - Synthetic", "Goods and Services Tax - Demo Record",
                  "GSTIN Certificate Extract - Demo"]
        dark, pale = _page_frame(pdf, case_id, kind, variant, titles[variant % 3],
                                 "Tax registration details supplied by the vendor")
        pdf.setFillColor(pale)
        pdf.rect(55, 582, WIDTH - 110, 53, fill=1, stroke=0)
        pdf.setFillColor(dark)
        _text(pdf, "REGISTRATION DETAILS", 72, 605, WIDTH - 145, size=12, bold=True)
        y = 550
        y = _label(pdf, "GSTIN", values["gstin"], 70, y, WIDTH - 140, dark)
        y = _label(pdf, "Legal Name", values["legal_name"], 70, y, WIDTH - 140, dark)
        _label(pdf, "Principal Place of Business", values["address"], 70, y, WIDTH - 140, dark)
        pdf.setFillColor(colors.HexColor("#475569"))
        _text(pdf, "A format check does not confirm active registration.", 70, 240, WIDTH - 140, size=10)
    elif kind == "bank_proof":
        titles = ["Account Confirmation - Demo", "Cancelled Cheque Copy - Demo",
                  "Bank Statement Extract - Demo"]
        dark, pale = _page_frame(pdf, case_id, kind, variant, titles[variant % 3],
                                 "Bank details submitted for supplier onboarding")
        pdf.setStrokeColor(dark)
        pdf.setLineWidth(1.3)
        pdf.roundRect(55, 309, WIDTH - 110, 330, 11, fill=0, stroke=1)
        pdf.setFillColor(pale)
        pdf.roundRect(56, 583, WIDTH - 112, 55, 10, fill=1, stroke=0)
        pdf.setFillColor(dark)
        _text(pdf, "ACCOUNT DETAILS", 73, 606, WIDTH - 145, size=12, bold=True)
        y = 548
        y = _label(pdf, "Account Holder", values["account_holder"], 75, y, WIDTH - 150, dark)
        y = _label(pdf, "Account Number", values["bank_account"], 75, y, WIDTH - 150, dark)
        _label(pdf, "IFSC", values["ifsc"], 75, y, WIDTH - 150, dark)
        pdf.setFillColor(colors.HexColor("#475569"))
        _text(pdf, "Confirm ownership and account details independently before payment.",
              65, 270, WIDTH - 130, size=10)
    elif kind == "business_registration":
        titles = ["Certificate of Incorporation - Demo", "Business Registration Extract - Demo",
                  "Entity Registration Summary - Demo"]
        dark, pale = _page_frame(pdf, case_id, kind, variant, titles[variant % 3],
                                 "Registered business details - synthetic sample")
        pdf.setStrokeColor(dark)
        pdf.setLineWidth(2)
        pdf.rect(54, 221, WIDTH - 108, 448, fill=0, stroke=1)
        pdf.setFillColor(pale)
        pdf.rect(63, 606, WIDTH - 126, 54, fill=1, stroke=0)
        pdf.setFillColor(dark)
        _text(pdf, "ENTITY DETAILS", 78, 629, WIDTH - 155, size=13, bold=True)
        y = 565
        y = _label(pdf, "Registered Legal Name", values["legal_name"], 79, y, WIDTH - 158, dark)
        y = _label(pdf, "Registration Number", values["registration_number"], 79, y, WIDTH - 158, dark)
        _label(pdf, "Registered Address", values["address"], 79, y, WIDTH - 158, dark)
        pdf.setFillColor(colors.HexColor("#475569"))
        _text(pdf, "This document is a synthetic reference, not a registry extract.",
              79, 272, WIDTH - 158, size=10)
    elif kind == "other":
        dark, pale = _page_frame(pdf, case_id, kind, variant, "Purchase Invoice - Wrong Attachment",
                                 "Unrelated purchase invoice supplied in the onboarding batch")
        y = _label(pdf, "Invoice Number", values["invoice_number"], 70, 575, WIDTH - 140, dark)
        y = _label(pdf, "Amount", "INR 12,500", 70, y, WIDTH - 140, dark)
        _label(pdf, "Description", "Goods and services billed", 70, y, WIDTH - 140, dark)
    else:
        raise ValueError(f"Unknown document type: {kind}")
    pdf.showPage()


def document_values(kind: str, form: dict, case_id: str, overrides: dict | None = None) -> dict:
    values = dict(form)
    values.update(overrides or {})
    match = re.fullmatch(r"[A-Za-z]([0-9]+)", case_id)
    number = int(match[1]) if match else int.from_bytes(hashlib.sha256(case_id.encode()).digest()[:4], "big") % 1000000
    values.setdefault("registration_number", f"U74999MH2024PTC{number:06d}")
    values.setdefault("invoice_number", f"INV-{case_id}")
    return values
