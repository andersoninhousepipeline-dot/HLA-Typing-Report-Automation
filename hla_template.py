"""
hla_template.py  —  HLA Typing Report PDF Generator
Faithful replica of Anderson Diagnostic Services manual PDFs.
Supports three report types that are auto-selected by hla_data_parser:
    single_hla       → NGS single patient layout (1 page)
    transplant_donor → NGS patient + donor(s) layout (1–2 pages)
    rpl_couple       → RPL couple layout (3 pages, fundamentally different)

Fonts:  GillSansMT-Bold (NGS title), SegoeUI-Bold (labels / RPL title),
        Calibri / Calibri-Bold (body text), all from assets/hla/fonts/.
Colors: Audited via PyMuPDF from all Manual-Report reference PDFs.
"""

import io
import os
import re
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    Image, HRFlowable, PageBreak, KeepTogether
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.pdfmetrics import registerFontFamily

import hla_assets

# ─── Page geometry (A4, matched to reference PDFs) ───────────────────────────
PAGE_W, PAGE_H = A4            # 595.28 × 841.89 pts
MARGIN_L = 15 * mm            # ≈ 42.5 pts  → actual content starts ~43 pts
MARGIN_R = 15 * mm
MARGIN_T = 8  * mm
MARGIN_B = 18 * mm
CONTENT_W = PAGE_W - MARGIN_L - MARGIN_R   # ≈ 510 pts

# ─── Colour palette — extracted precisely via PyMuPDF from all Manual-Report PDFs ─
# NGS
C_NGS_TITLE     = colors.HexColor("#002060")   # NGS title (GillSansMT-Bold 18pt) — exact
C_INFO_BG       = colors.HexColor("#F1F1F7")   # Patient info ALL cells — exact
C_HLA_HDR       = colors.HexColor("#FABF8F")   # HLA table header row — exact
C_HLA_ROW       = colors.HexColor("#F2F2F2")   # HLA data rows — exact
C_APPROVAL      = colors.HexColor("#2C6BAA")   # "Reviewed and approved by" line — exact
# RPL — plain white table, black borders, no fills (confirmed by fitz audit)
C_RPL_TITLE     = colors.HexColor("#001F5F")   # RPL title (SegoeUI-Bold 14pt) — exact
C_RPL_BORDER    = colors.black                 # RPL table borders
# Shared aliases
C_TITLE         = C_NGS_TITLE
C_SECTION_BAR   = C_NGS_TITLE
WHITE = colors.white
BLACK = colors.black
ORANGE = colors.HexColor("#E8772E")

# ─── Static text constants ────────────────────────────────────────────────────
COVERAGE_LINES = [
    ": Class I (HLA-A, -B & -C) - Whole gene",
    ": Class II (HLA-DRB1) - Whole gene except Intron 1",
    ": Class II (HLA-DQB1) - Upto Exon 5",
    ": Class II (HLA-DPB1) - Exon 2 to Exon 4",
]
METHODOLOGY_MINISEQ = "Typing by NGS illumina MiniSeq using MIA FORA NGS Kits from IMMUCOR"
METHODOLOGY_SURFSEQ  = "Typing by NGS Surfseq using GENDx Kit"

RPL_BACKGROUND = (
    "The HLA region maps to chromosome 6p21.31 and spans approximately 7.6 Mb. "
    "The classical HLA genes are divided into Class I (HLA-A, B, C) and Class II "
    "(HLA-DR, DQ, DP) gene families. The gene products (HLA molecules) are expressed "
    "on cell surfaces and play a key role in immune recognition. Sharing of HLA "
    "antigens between couples has been associated with recurrent pregnancy loss (RPL) "
    "and recurrent implantation failure (RIF). Increased HLA compatibility between "
    "partners may impair the development of protective immune responses required for "
    "successful pregnancy. The clinical relevance of individual HLA allele sharing "
    "should be interpreted in the context of the patient's complete clinical history."
)
RPL_DISCLAIMERS = [
    "This test is intended for use in conjunction with clinical evaluation and other "
    "diagnostic procedures. Results should be interpreted by a qualified clinician.",
    "HLA allele frequencies and their association with reproductive outcomes vary "
    "across populations. Reference data used are based on published literature.",
    "The HLA-C supertype (C1/C2) classification is based on published Killer-cell "
    "Immunoglobulin-like Receptor (KIR) ligand groupings.",
    "This report covers Class I (HLA-A, B, C) whole gene and Class II partial gene at "
    "high-resolution allele level.",
    "DPB1 alleles are reported separately; their inclusion in compatibility scoring "
    "is at the discretion of the treating clinician.",
    "Results are reported based on the IMGT/HLA database release version stated above. "
    "Novel alleles may not be detected.",
    "This report is generated for clinical use only and should not be used for "
    "forensic or immigration purposes.",
    "Anderson Diagnostics and Labs shall not be held liable for clinical decisions "
    "made solely on the basis of this report without clinical correlation.",
]


# ─── Font registration ────────────────────────────────────────────────────────
_FONTS_DIR = os.path.join(os.path.dirname(__file__), "assets", "hla", "fonts")
_REGISTERED: set[str] = set()

def _register_fonts():
    """Register custom TTF fonts once (idempotent)."""
    global _REGISTERED
    if _REGISTERED:
        return
    font_configs = [
        ("SegoeUI",               "SEGOEUI.TTF"),
        ("SegoeUI-Bold",          "SEGOEUIB.TTF"),
        ("SegoeUI-Italic",        "SEGOEUII.TTF"),
        ("SegoeUI-BoldItalic",    "SEGOEUIZ.TTF"),
        ("GillSansMT",            "GIL_____.TTF"),
        ("GillSansMT-Bold",       "GILB____.TTF"),
        ("Calibri",               "CALIBRI.TTF"),
        ("Calibri-Bold",          "CALIBRIB.TTF"),
        ("Calibri-Italic",        "CALIBRII.TTF"),
        ("Calibri-BoldItalic",    "CALIBRIZ.TTF"),
    ]
    for name, fname in font_configs:
        path = os.path.join(_FONTS_DIR, fname)
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont(name, path))
                _REGISTERED.add(name)
            except Exception:
                pass
    if {"SegoeUI", "SegoeUI-Bold"} <= _REGISTERED:
        try:
            registerFontFamily("SegoeUI", normal="SegoeUI", bold="SegoeUI-Bold",
                               italic="SegoeUI-Italic", boldItalic="SegoeUI-BoldItalic")
        except Exception:
            pass
    if {"GillSansMT", "GillSansMT-Bold"} <= _REGISTERED:
        try:
            registerFontFamily("GillSansMT", normal="GillSansMT", bold="GillSansMT-Bold",
                               italic="GillSansMT-Italic")
        except Exception:
            pass
    if {"Calibri", "Calibri-Bold"} <= _REGISTERED:
        try:
            registerFontFamily("Calibri", normal="Calibri", bold="Calibri-Bold",
                               italic="Calibri-Italic", boldItalic="Calibri-BoldItalic")
        except Exception:
            pass


def _f(preferred: str, fallback: str = "Helvetica") -> str:
    """Return preferred font name if registered, else fallback."""
    try:
        pdfmetrics.getFont(preferred)
        return preferred
    except Exception:
        return fallback


# ─── Style factory ────────────────────────────────────────────────────────────
def _styles() -> dict:
    """Return a dict of ParagraphStyle objects, chosen to match reference PDFs."""
    _register_fonts()

    # Font aliases (resolved at call time so fallbacks work)
    F_GILL_BOLD  = _f("GillSansMT-Bold",  "Helvetica-Bold")
    F_SEGOE      = _f("SegoeUI",           "Helvetica")
    F_SEGOE_BOLD = _f("SegoeUI-Bold",      "Helvetica-Bold")
    F_CALI       = _f("Calibri",           "Helvetica")
    F_CALI_BOLD  = _f("Calibri-Bold",      "Helvetica-Bold")

    return {
        # ── NGS title ────────────────────────────────────────────────────────
        "title_ngs": ParagraphStyle(
            "title_ngs", fontName=F_GILL_BOLD, fontSize=18,
            textColor=C_TITLE, alignment=TA_CENTER,
            spaceBefore=0, spaceAfter=4, leading=22
        ),
        # ── RPL title (SegoeUI-Bold 14pt #001F5F — confirmed by fitz audit) ────
        "title_rpl": ParagraphStyle(
            "title_rpl", fontName=F_SEGOE_BOLD, fontSize=14,
            textColor=C_RPL_TITLE, alignment=TA_CENTER,
            spaceBefore=0, spaceAfter=4, leading=18
        ),
        # ── NGS section bar text (white on blue) ──────────────────────────────
        "section_bar": ParagraphStyle(
            "section_bar", fontName=F_SEGOE_BOLD, fontSize=9.5,
            textColor=WHITE, leading=12
        ),
        # ── Patient info table ────────────────────────────────────────────────
        "lbl": ParagraphStyle(
            "lbl", fontName=F_SEGOE_BOLD, fontSize=10,
            textColor=BLACK, leading=12
        ),
        "val": ParagraphStyle(
            "val", fontName=F_SEGOE_BOLD, fontSize=10,
            textColor=BLACK, leading=12
        ),
        # ── HLA table ────────────────────────────────────────────────────────
        "hla_hdr": ParagraphStyle(
            "hla_hdr", fontName=F_CALI_BOLD, fontSize=12,
            textColor=BLACK, alignment=TA_CENTER, leading=14
        ),
        "hla_val": ParagraphStyle(
            "hla_val", fontName=F_CALI, fontSize=11,
            textColor=BLACK, alignment=TA_CENTER, leading=13
        ),
        "hla_lbl": ParagraphStyle(
            "hla_lbl", fontName=F_CALI_BOLD, fontSize=11,
            textColor=BLACK, alignment=TA_CENTER, leading=13
        ),
        # ── Body text ────────────────────────────────────────────────────────
        "body": ParagraphStyle(
            "body", fontName=F_CALI, fontSize=11,
            textColor=BLACK, leading=13, spaceAfter=2
        ),
        "body_bold": ParagraphStyle(
            "body_bold", fontName=F_CALI_BOLD, fontSize=11,
            textColor=BLACK, leading=13, spaceAfter=2
        ),
        "body_small": ParagraphStyle(
            "body_small", fontName=F_CALI, fontSize=10,
            textColor=BLACK, leading=12
        ),
        "coverage": ParagraphStyle(
            "coverage", fontName=F_CALI, fontSize=11,
            textColor=BLACK, leading=13, leftIndent=10, spaceAfter=1
        ),
        # ── RPL couple table ─────────────────────────────────────────────────
        "rpl_lbl": ParagraphStyle(
            "rpl_lbl", fontName=F_CALI_BOLD, fontSize=11,
            textColor=BLACK, leading=13
        ),
        "rpl_val": ParagraphStyle(
            "rpl_val", fontName=F_CALI, fontSize=11,
            textColor=BLACK, alignment=TA_CENTER, leading=13
        ),
        "rpl_hla_lbl": ParagraphStyle(
            "rpl_hla_lbl", fontName=F_CALI_BOLD, fontSize=11,
            textColor=BLACK, leading=13
        ),
        "rpl_hla_val": ParagraphStyle(
            "rpl_hla_val", fontName=F_CALI_BOLD, fontSize=11,
            textColor=BLACK, alignment=TA_CENTER, leading=13
        ),
        "rpl_hdr_name": ParagraphStyle(
            "rpl_hdr_name", fontName=F_CALI_BOLD, fontSize=11,
            textColor=WHITE, alignment=TA_CENTER, leading=13
        ),
        # ── Match / Comment ────────────────────────────────────────────────────
        "match": ParagraphStyle(
            "match", fontName=F_CALI_BOLD, fontSize=11,
            textColor=BLACK, alignment=TA_LEFT, leading=13, spaceAfter=3
        ),
        "comment": ParagraphStyle(
            "comment", fontName=F_CALI, fontSize=11,
            textColor=BLACK, leading=13, spaceAfter=3
        ),
        # ── Reference heading ─────────────────────────────────────────────────
        "ref_hdr": ParagraphStyle(
            "ref_hdr", fontName=F_CALI_BOLD, fontSize=14,
            textColor=BLACK, leading=18, spaceBefore=4, spaceAfter=2
        ),
        # ── RPL section headings (BACKGROUND, DISCLAIMERS) ────────────────────
        "section_hdr": ParagraphStyle(
            "section_hdr", fontName=F_SEGOE_BOLD, fontSize=12,
            textColor=BLACK, leading=15, spaceBefore=6, spaceAfter=2
        ),
        # ── RPL body/disclaimers ──────────────────────────────────────────────
        "justify": ParagraphStyle(
            "justify", fontName=F_CALI, fontSize=11,
            textColor=BLACK, leading=13, alignment=TA_JUSTIFY, spaceAfter=2
        ),
        "disc_item": ParagraphStyle(
            "disc_item", fontName=F_CALI, fontSize=11,
            textColor=BLACK, leading=13, leftIndent=12, spaceAfter=1
        ),
        # ── Signature block ───────────────────────────────────────────────────
        # Cambria-Bold 12.2pt in reference PDF; SegoeUI-Bold is closest available
        "sign_approval": ParagraphStyle(
            "sign_approval", fontName=F_SEGOE_BOLD, fontSize=12.2,
            textColor=C_APPROVAL, leading=15, spaceBefore=4, spaceAfter=6
        ),
        "sign_name": ParagraphStyle(
            "sign_name", fontName=F_CALI_BOLD, fontSize=10,
            textColor=BLACK, alignment=TA_CENTER, leading=12
        ),
        "sign_role": ParagraphStyle(
            "sign_role", fontName=F_CALI_BOLD, fontSize=10,
            textColor=BLACK, alignment=TA_CENTER, leading=12
        ),
    }


# ─── Image helpers ────────────────────────────────────────────────────────────
def _img_b64(b64: str, width: float, height: Optional[float] = None) -> Image:
    data = hla_assets.get_image_bytes(b64)
    return Image(io.BytesIO(data), width=width, height=height)


def _strip_prefix(allele: str) -> str:
    """'A*02:11:01' → '02:11:01'; returns '—' if falsy."""
    if not allele:
        return "—"
    m = re.match(r"[A-Za-z0-9]+\*(.+)", allele)
    return m.group(1) if m else allele


# ─── Canvas: header + footer on every page ────────────────────────────────────
class _HFCanvas:
    """Draws header image (or text) and footer image on every page."""

    def __init__(self, case: dict, title: str, banner_h: float, footer_h: float, total_pages: int = 1):
        self.case        = case
        self.title       = title
        self.banner_h    = banner_h
        self.footer_h    = footer_h
        self.total_pages = total_pages

    def __call__(self, canvas, doc):
        canvas.saveState()
        nabl      = self.case.get("nabl", True)
        with_logo = self.case.get("with_logo", True)

        # ── Header ──────────────────────────────────────────────────────────
        # Only draw header if with_logo is True
        if with_logo:
            b64 = hla_assets.HEADER_NABL_B64 if nabl else hla_assets.HEADER_NONNABL_B64
            raw = hla_assets.get_image_bytes(b64)
            canvas.drawImage(
                ImageReader(io.BytesIO(raw)),
                MARGIN_L, PAGE_H - MARGIN_T - self.banner_h,
                width=CONTENT_W, height=self.banner_h,
                preserveAspectRatio=True, mask="auto"
            )

        # ── Footer ──────────────────────────────────────────────────────────
        # Only draw footer if with_logo is True
        if with_logo:
            raw_f = hla_assets.get_image_bytes(hla_assets.FOOTER_BAR_B64)
            fy = MARGIN_B - self.footer_h - 2 * mm
            canvas.drawImage(
                ImageReader(io.BytesIO(raw_f)),
                MARGIN_L, fy,
                width=CONTENT_W, height=self.footer_h,
                preserveAspectRatio=True, mask="auto"
            )
            # Page number "Page X of N" (white, right-aligned, centred on footer bar)
            canvas.setFont(_f("Calibri", "Helvetica"), 11)
            canvas.setFillColor(WHITE)
            canvas.drawRightString(
                PAGE_W - MARGIN_R,
                fy + self.footer_h / 2 - 5,
                f"Page {doc.page} of {self.total_pages}"
            )
        # No logo mode: leave header/footer space completely empty (no page numbers, no content)

        canvas.restoreState()


# ─── NGS: section header bar ─────────────────────────────────────────────────
def _ngs_section_bar(text: str, S: dict) -> Table:
    """Dark-blue full-width bar with white text — 'Patient: Name'."""
    p = Paragraph(text, S["section_bar"])
    t = Table([[p]], colWidths=[CONTENT_W])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), C_SECTION_BAR),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
    ]))
    return t


# ─── NGS: 6-column patient info table (PGTA-style colon alignment) ───────────
# Layout: [label] [":"] [value] | [label] [":"] [value]
# Colon in its own narrow column ensures all colons are vertically aligned.
# Widths proportioned from PGTA reference: [108, 12, 161, 108, 12, 89] on 490pt
# Scaled to CONTENT_W ≈ 510pt: [112, 13, 168, 112, 13, 92]

def _ngs_info_table(person: dict, S: dict, is_donor: bool = False) -> Table:
    pf = "Donor" if is_donor else "Patient"
    cw = CONTENT_W

    # 6-col widths matching PGTA proportions
    col_w = [cw * 0.220, cw * 0.025, cw * 0.329, cw * 0.220, cw * 0.025, cw * 0.181]

    def L(text): return Paragraph(f"<b>{text}</b>", S["lbl"])
    def C():     return Paragraph("<b>:</b>", S["lbl"])
    def V(text): return Paragraph(str(text) if text else "\u2014", S["val"])
    def E():     return Paragraph("", S["lbl"])

    left_rows = [
        [L(f"{pf} Name"), C(), V(person.get("name", ""))],
        [L("Gender / Age"), C(), V(person.get("gender_age", ""))],
        [L("Hospital MR No"), C(), V(person.get("hospital_mr_no", ""))],
    ]

    # Add diagnosis only for patients, not donors
    if not is_donor:
        left_rows.append([L("Diagnosis"), C(), V(person.get("diagnosis") or "NA")])

    left_rows.extend([
        [L("Referred By"), C(), V(person.get("referred_by", ""))],
        [L("Hospital / Clinic"), C(), V(person.get("hospital_clinic", ""))],
    ])

    if is_donor:
        left_rows.insert(2, [L("Relationship"), C(), V(person.get("relationship", ""))])

    right_rows = [
        [L("PIN"), C(), V(person.get("pin", ""))],
        [L("Sample Number"), C(), V(person.get("sample_number", ""))],
        [L("Specimen"), C(), V(person.get("specimen") or "Blood - EDTA")],
        [L("Collection Date"), C(), V(person.get("collection_date", ""))],
        [L("Sample Receipt Date"), C(), V(person.get("receipt_date", ""))],
        [L("Report Date"), C(), V(person.get("report_date", ""))],
    ]

    max_r = max(len(left_rows), len(right_rows))
    while len(left_rows)  < max_r: left_rows.append([E(), E(), E()])
    while len(right_rows) < max_r: right_rows.append([E(), E(), E()])

    rows = [lr + rr for lr, rr in zip(left_rows, right_rows)]
    t = Table(rows, colWidths=col_w)
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), C_INFO_BG),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        # Colon columns: no left padding so colon sits flush next to label
        ("LEFTPADDING",   (1, 0), (1, -1), 0),
        ("LEFTPADDING",   (4, 0), (4, -1), 0),
        ("RIGHTPADDING",  (1, 0), (1, -1), 2),
        ("RIGHTPADDING",  (4, 0), (4, -1), 2),
    ]))
    return t


# ─── NGS: HLA allele results table ────────────────────────────────────────────
# Header row → C_HLA_HDR (#F9BE8F).  Data rows → C_HLA_ROW (#F1F2F1).
# Row labels: "1" and "2" (Calibri-Bold 11).

def _hla_table(person: dict, S: dict) -> Table:
    LOCI       = ["A", "B", "C", "DRB1", "DQB1", "DPB1"]
    EXTRA_LOCI = ["DRB3", "DRB4", "DRB5"]
    hla = person.get("hla", {})

    loci = [l for l in LOCI if any(hla.get(l, [None, None]))]
    loci += [l for l in EXTRA_LOCI if any(hla.get(l, [None, None]))]
    if not loci:
        loci = LOCI

    def HH(t): return Paragraph(t, S["hla_hdr"])
    def HV(t): return Paragraph(t or "\u2014", S["hla_val"])
    def HL(t): return Paragraph(t, S["hla_lbl"])

    header = [HH("LOCUS")] + [HH(f"HLA-{l}*") for l in loci]
    r1     = [HV("1")]
    r2     = [HV("2")]
    for l in loci:
        al = hla.get(l, [None, None])
        r1.append(HV(_strip_prefix(al[0]) if al and al[0] else "\u2014"))
        r2.append(HV(_strip_prefix(al[1]) if al and len(al) > 1 and al[1] else "\u2014"))

    n = len(loci)
    col_w = [CONTENT_W * 0.10] + [CONTENT_W * 0.90 / n] * n
    t = Table([header, r1, r2], colWidths=col_w)
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), C_HLA_HDR),
        ("TEXTCOLOR",     (0, 0), (-1, 0), BLACK),
        ("BACKGROUND",    (0, 1), (-1, 1), C_HLA_ROW),
        ("BACKGROUND",    (0, 2), (-1, 2), C_HLA_ROW),
        ("GRID",          (0, 0), (-1, -1), 0.5, BLACK),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


# ─── NGS: one person block (info + HLA + optional match + remarks) ────────────
def _ngs_person_block(person: dict, is_donor: bool, match_str: str, S: dict) -> list:
    elems = []
    elems.append(_ngs_info_table(person, S, is_donor=is_donor))
    elems.append(Spacer(1, 2 * mm))
    elems.append(_hla_table(person, S))

    if person.get("remarks"):
        remarks = person["remarks"]
        if len(remarks) > 600:
            remarks = remarks[:580] + "..."
        elems.append(Spacer(1, 1 * mm))
        elems.append(Paragraph(f"<b>Remarks:</b> {remarks}", S["body_small"]))

    if match_str:
        elems.append(Spacer(1, 1 * mm))
        elems.append(Paragraph(
            f"<b>Match: {match_str}</b>",
            ParagraphStyle("ms", fontName=_f("Calibri-Bold","Helvetica-Bold"),
                           fontSize=11, textColor=BLACK, alignment=TA_LEFT,
                           leading=13, spaceBefore=2, spaceAfter=2)
        ))

    elems.append(Spacer(1, 3 * mm))
    return elems


# ─── RPL: unified 5-column couple + HLA table ─────────────────────────────────
# Demographic rows: SPAN cols 1-2 for patient, SPAN cols 3-4 for donor.
# HLA rows: separate allele per column (p_a1 | p_a2 | d_a1 | d_a2).
# All cells WHITE with black 0.5pt grid.  Header row: black bg, white text.
# Column widths derived from fitz measurements of Mrs.Hemalatha RPL PDF.

def _rpl_couple_table(patient: dict, donor: dict, S: dict) -> Table:
    p_name = patient.get("name", "\u2014")
    d_name = donor.get("name",   "\u2014")
    cw = CONTENT_W

    # Col widths: [label 24.6%] [p_a1 18.3%] [p_a2 18.4%] [d_a1 19.4%] [d_a2 19.3%]
    col_w = [cw * 0.246, cw * 0.183, cw * 0.184, cw * 0.194, cw * 0.193]

    def RL(t): return Paragraph(f"<b>{t}</b>", S["rpl_lbl"])
    def RV(t): return Paragraph(str(t) if t else "\u2014", S["rpl_val"])
    def HL(t): return Paragraph(f"<b>{t}</b>", S["rpl_hla_lbl"])
    def HV(t): return Paragraph(str(t) if t else "\u2014", S["rpl_hla_val"])
    def HDR(t): return Paragraph(f"<b>{t}</b>", S["rpl_hdr_name"])

    data = []
    spans = []

    # No header row — reference PDF starts directly with demographic rows (confirmed by fitz audit)

    # Demographic rows - patient has diagnosis, donor does not
    # Patient labels (12 rows with diagnosis)
    p_labels = [
        "Name", "Relationship stated/\nClaimed", "Age/Gender",
        "Diagnosis", "Referred By", "Hospital/Clinic",
        "PIN", "Sample Number", "Specimen",
        "Collection Date", "Sample receipt date", "Report date",
    ]
    p_vals = [
        patient.get("name", ""), patient.get("relationship", "") or "NA",
        patient.get("gender_age", ""), patient.get("diagnosis") or "NA",
        patient.get("referred_by", ""), patient.get("hospital_clinic", ""),
        patient.get("pin", ""), patient.get("sample_number", ""),
        patient.get("specimen") or "Blood - EDTA",
        patient.get("collection_date", ""), patient.get("receipt_date", ""),
        patient.get("report_date", ""),
    ]

    # Donor labels (11 rows without diagnosis)
    d_labels = [
        "Name", "Relationship stated/\nClaimed", "Age/Gender",
        "Referred By", "Hospital/Clinic",
        "PIN", "Sample Number", "Specimen",
        "Collection Date", "Sample receipt date", "Report date",
    ]
    d_vals = [
        donor.get("name", ""), donor.get("relationship", "") or "NA",
        donor.get("gender_age", ""),
        donor.get("referred_by", ""), donor.get("hospital_clinic", ""),
        donor.get("pin", ""), donor.get("sample_number", ""),
        donor.get("specimen") or "Blood - EDTA",
        donor.get("collection_date", ""), donor.get("receipt_date", ""),
        donor.get("report_date", ""),
    ]
    demo_start = 0
    # Build rows - patient has 12 rows, donor has 11 (no diagnosis)
    for i in range(max(len(p_labels), len(d_labels))):
        r = demo_start + i
        # Patient side
        p_lbl = p_labels[i] if i < len(p_labels) else ""
        p_val = p_vals[i] if i < len(p_vals) else ""
        # Donor side - for diagnosis row (index 3), show empty
        if i < len(d_labels):
            if p_lbl == "Diagnosis":
                # Diagnosis row - patient has it, donor doesn't
                d_val = ""
            else:
                # Find matching donor value (offset by 1 after diagnosis)
                d_idx = i if i < 3 else i - 1  # Before diagnosis row: same index, after: offset by -1
                d_val = d_vals[d_idx] if d_idx < len(d_vals) else ""
        else:
            d_val = ""

        data.append([RL(p_lbl), RV(p_val), Paragraph("", S["rpl_lbl"]),
                     RV(d_val), Paragraph("", S["rpl_lbl"])])
        spans += [("SPAN", (1, r), (2, r)), ("SPAN", (3, r), (4, r))]

    hla_start = len(data)

    # HLA rows — 5-column, no SPAN
    LOCI = ["A", "B", "C", "DRB1", "DQB1", "DPB1"]
    p_hla = patient.get("hla", {})
    d_hla = donor.get("hla",   {})
    for i, locus in enumerate(LOCI):
        pa = p_hla.get(locus, [None, None])
        da = d_hla.get(locus, [None, None])
        pa1 = _strip_prefix(pa[0]) if pa and pa[0] else "\u2014"
        pa2 = _strip_prefix(pa[1]) if pa and len(pa) > 1 and pa[1] else "\u2014"
        da1 = _strip_prefix(da[0]) if da and da[0] else "\u2014"
        da2 = _strip_prefix(da[1]) if da and len(da) > 1 and da[1] else "\u2014"
        data.append([HL(f"HLA-{locus}*"), HV(pa1), HV(pa2), HV(da1), HV(da2)])

    n_rows = len(data)
    style_cmds = [
        # All cells: plain white, black 0.5pt grid (confirmed by fitz audit — no fills)
        ("BACKGROUND",    (0, 0), (-1, -1),             WHITE),
        ("TEXTCOLOR",     (0, 0), (-1, 0),              BLACK),
        ("ALIGN",         (0, 0), (-1, 0),              "CENTER"),
        ("GRID",          (0, 0), (-1, -1),             0.5, C_RPL_BORDER),
        ("VALIGN",        (0, 0), (-1, -1),             "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1),             4),
        ("BOTTOMPADDING", (0, 0), (-1, -1),             4),
        ("LEFTPADDING",   (0, 0), (-1, -1),             4),
        ("RIGHTPADDING",  (0, 0), (-1, -1),             4),
        # HLA rows: allele cols centered, label col left
        ("ALIGN",         (1, hla_start), (-1, n_rows - 1), "CENTER"),
        # Demographic value cols centered
        ("ALIGN",         (1, 1), (-1, hla_start - 1), "CENTER"),
    ] + spans

    t = Table(data, colWidths=col_w)
    t.setStyle(TableStyle(style_cmds))
    return t


# ─── RPL: reference table + HLA-C supertype table ─────────────────────────────
def _rpl_reference_section(rpl_ref: dict, patient: dict, donor: dict, S: dict) -> list:
    elems = []
    p_name    = patient.get("name", "")
    d_name    = donor.get("name",   "")
    match_str = rpl_ref.get("match_str", "")
    match_pct = rpl_ref.get("match_pct", "")

    # Comment block
    if match_str or match_pct:
        bold_match = f"<b>{match_str} ({match_pct})</b>" if match_str else f"<b>{match_pct}</b>"
        comment = (
            f"Comment: HLA-A, B, C, DRB1, DQB1 &amp; DPB1 locus typing patterns of the "
            f"above individuals indicate {bold_match} matches at High resolution."
        )
        elems.append(Paragraph(comment, S["comment"]))
        elems.append(Spacer(1, 2 * mm))

    # Reference: heading (Calibri-Bold 14, black)
    elems.append(Paragraph("<b>Reference:</b>", S["ref_hdr"]))

    # 3-column reference table — headers SegoeUI-Bold 10pt (confirmed by fitz audit)
    # Build the HLA matching cell content: vertical stacking "Overall – X%" and "Class-II – Y%"
    class2_pct = rpl_ref.get("class2_pct", "")
    if match_pct and class2_pct:
        hla_matching_text = f"Overall – {match_pct}<br/>Class-II – {class2_pct}"
    elif match_pct:
        hla_matching_text = f"Overall – {match_pct}"
    else:
        hla_matching_text = "\u2014"

    ref_data = [
        [
            Paragraph("<b>Names/Code</b>",                                    S["lbl"]),
            Paragraph("<b>HLA matching between couples</b>",                  S["lbl"]),
            Paragraph("<b>HLA sharing for Recurrent miscarriage/RIF</b>",     S["lbl"]),
        ],
        [
            Paragraph(f"{p_name} / {d_name}", S["rpl_val"]),
            Paragraph(hla_matching_text,      S["rpl_val"]),
            Paragraph(rpl_ref.get("hla_sharing_rif", ">50%"), S["rpl_val"]),
        ],
    ]
    # Reference table header uses SegoeUI-Bold 10pt black on white (no colour fill — confirmed)
    ref_t = Table(ref_data,
                  colWidths=[CONTENT_W * 0.34, CONTENT_W * 0.30, CONTENT_W * 0.36])
    ref_t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), WHITE),
        ("TEXTCOLOR",     (0, 0), (-1, -1), BLACK),
        ("GRID",          (0, 0), (-1, -1), 0.5, C_RPL_BORDER),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    elems.append(ref_t)

    # HLA-C supertype table — also plain white, no fills (confirmed)
    hla_c_p = rpl_ref.get("hla_c_patient", "")
    hla_c_d = rpl_ref.get("hla_c_donor",   "")
    if hla_c_p or hla_c_d:
        elems.append(Spacer(1, 2 * mm))
        c_data = [
            [Paragraph("<b>Maternal HLA-C Type</b>", S["rpl_lbl"]),
             Paragraph("<b>Paternal HLA-C Type</b>",  S["rpl_lbl"])],
            [Paragraph(hla_c_p or "\u2014", S["rpl_val"]),
             Paragraph(hla_c_d or "\u2014", S["rpl_val"])],
        ]
        c_t = Table(c_data, colWidths=[CONTENT_W * 0.50, CONTENT_W * 0.50])
        c_t.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), WHITE),
            ("TEXTCOLOR",     (0, 0), (-1, -1), BLACK),
            ("GRID",          (0, 0), (-1, -1), 0.5, C_RPL_BORDER),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        elems.append(c_t)

    return elems


# ─── Methodology block (shared) ───────────────────────────────────────────────
def _methodology_block(case: dict, S: dict) -> list:
    """
    IMGT → Remarks: → Coverage (: prefix lines) → Methodology → Typing Status
    Matches the exact format seen in all manual report PDFs.
    NO horizontal rules between sections - only one line before signatures.
    """
    nabl   = case.get("nabl", True)
    imgt   = case.get("imgt_release", "") or "3.56.0"  # Default IMGT version if not specified
    method = case.get("methodology", "") or (METHODOLOGY_MINISEQ if nabl else METHODOLOGY_SURFSEQ)
    status = case.get("typing_status", "Complete")

    elems = [Spacer(1, 2 * mm)]

    # IMGT/HLA Release - ALWAYS display (as per reference templates)
    elems.append(Paragraph(f"<b>IMGT/HLA Release</b> {imgt}", S["body"]))

    elems.append(Paragraph("<b>Remarks:</b>", S["body"]))
    # Coverage: first line inline "Coverage : Class I..." then remaining lines indented
    elems.append(Paragraph(f"<b>Coverage</b>{COVERAGE_LINES[0]}", S["body"]))
    for line in COVERAGE_LINES[1:]:
        elems.append(Paragraph(line, S["coverage"]))

    elems.append(Spacer(1, 1 * mm))
    elems.append(Paragraph(f"<b>Methodology:</b>  {method}", S["body"]))
    elems.append(Spacer(1, 1 * mm))
    # Horizontal line after Methodology (as per reference templates)
    elems.append(HRFlowable(width="100%", thickness=0.5, color=BLACK))
    elems.append(Spacer(1, 1 * mm))
    elems.append(Paragraph(f"<b>Typing Status:</b>  {status}", S["body"]))
    # Line after methodology matches reference templates
    return elems


# ─── Signature block ──────────────────────────────────────────────────────────
def _signature_block(signatories: list, S: dict) -> list:
    """
    HR line → 'This report has been reviewed and approved by:'  (SegoeUI-Bold 11.8pt #2C6BAA)
    → signature images + name + role side-by-side directly below (matching reference PDF layout).
    If signatory has seal_b64, the rubber stamp appears below the signature image.
    """
    if not signatories:
        return []

    n        = len(signatories)
    col_each = CONTENT_W / n

    cols = []
    for sig in signatories:
        sign_data = hla_assets.get_image_bytes(sig["sign_b64"])
        sign_img  = Image(io.BytesIO(sign_data), width=30 * mm, height=14 * mm)
        cell_rows = [
            [sign_img],
            [Paragraph(sig.get("name",  ""), S["sign_name"])],
            [Paragraph(sig.get("title", ""), S["sign_role"])],
        ]
        seal_b64 = sig.get("seal_b64")
        if seal_b64:
            seal_data = hla_assets.get_image_bytes(seal_b64)
            seal_img  = Image(io.BytesIO(seal_data), width=22 * mm, height=22 * mm)
            cell_rows.append([seal_img])

        inner = Table(cell_rows, colWidths=[col_each])
        inner.setStyle(TableStyle([
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING",    (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("BACKGROUND",    (0, 0), (-1, -1), WHITE),
        ]))
        cols.append(inner)

    outer = Table([cols], colWidths=[col_each] * n)
    outer.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))

    return [
        Spacer(1, 2 * mm),  # Reduced spacing before signature section
        HRFlowable(width="100%", thickness=0.5, color=BLACK, spaceAfter=2),  # Thinner line, less space
        Paragraph("<b>This report has been reviewed and approved by:</b>",
                  S["sign_approval"]),
        outer,
    ]


# ─── Report builders ──────────────────────────────────────────────────────────

def _build_ngs_single(case: dict, S: dict) -> list:
    """
    single_hla — title + patient block + methodology + signatures.

    Strategy:
    - Patient block: wrapped in KeepTogether to prevent splitting
    - Methodology + Signatures: allowed to flow naturally across pages if needed
    """
    patient     = case["patient"]
    signatories = case.get("signatories") or hla_assets.get_default_signatories(
        "single_hla", case.get("nabl", True))

    elems = []

    # Patient block - keep together
    patient_block = _ngs_person_block(patient, is_donor=False, match_str="", S=S)
    elems.append(KeepTogether(patient_block))

    # Methodology and signatures - keep together to prevent page break
    methodology_and_sigs = _methodology_block(case, S) + _signature_block(signatories, S)
    elems.append(KeepTogether(methodology_and_sigs))

    return elems


def _build_ngs_transplant(case: dict, S: dict) -> list:
    """
    transplant_donor — title + patient + each donor + methodology + signatures.

    Strategy:
    - Patient block: wrapped in KeepTogether to prevent splitting
    - Each donor block: wrapped in KeepTogether to prevent splitting
    - Methodology + Signatures: kept together on final section

    This ensures clean page breaks only between major sections, never within a person's data.
    """
    patient     = case["patient"]
    donors      = case.get("donors", [])
    signatories = case.get("signatories") or hla_assets.get_default_signatories(
        "transplant_donor", case.get("nabl", True))

    elems = []

    # Patient block - keep together
    patient_block = _ngs_person_block(patient, is_donor=False, match_str="", S=S)
    elems.append(KeepTogether(patient_block))

    # Each donor block - keep together individually
    for d in donors:
        donor_block = _ngs_person_block(d, is_donor=True, match_str=d.get("match", ""), S=S)
        elems.append(KeepTogether(donor_block))

    # Methodology and signatures - keep together to prevent page break
    methodology_and_sigs = _methodology_block(case, S) + _signature_block(signatories, S)
    elems.append(KeepTogether(methodology_and_sigs))

    return elems


def _build_rpl_couple(case: dict, S: dict) -> list:
    """
    rpl_couple — multi-page layout with natural page breaks:
      Page 1: title + unified couple+HLA table + comment + reference tables
              (flows to Page 2 if needed)
      Page 2+: methodology + BACKGROUND + DISCLAIMERS + signatures
              (all kept together, flows naturally across pages)

    Single-person RPL (no donor): falls back to NGS-single layout + RPL background.

    Strategy:
    - Page 1 content (couple table + reference) wrapped in KeepTogether where possible
    - Methodology block + Background + Disclaimers + Signatures kept together
    - Natural page breaks between major sections instead of forced PageBreaks
    """
    patient     = case["patient"]
    donors      = case.get("donors", [])
    donor       = donors[0] if donors else None
    rpl_ref     = case.get("rpl_reference", {})
    signatories = case.get("signatories") or hla_assets.get_default_signatories(
        "rpl_couple", case.get("nabl", True))

    elems = []

    # ── Page 1 content ────────────────────────────────────────────────────────
    if donor:
        # Unified couple+HLA table - keep together to prevent splitting
        page1_content = []
        page1_content.append(_rpl_couple_table(patient, donor, S))
        page1_content.append(Spacer(1, 3 * mm))
        # Comment + Reference + HLA-C tables
        page1_content += _rpl_reference_section(rpl_ref, patient, donor, S)

        # Wrap page 1 content in KeepTogether to prevent table splitting
        elems.append(KeepTogether(page1_content))
    else:
        # Single-person RPL: NGS-style patient block
        patient_block = _ngs_person_block(patient, is_donor=False, match_str="", S=S)
        elems.append(KeepTogether(patient_block))

    # Add spacer to help with page flow
    elems.append(Spacer(1, 5 * mm))

    # ── Methodology + Background + Disclaimers + Signatures ───────────────────
    # Keep all of these together as one cohesive section that flows naturally
    page2_content = []
    page2_content += _methodology_block(case, S)
    page2_content.append(Spacer(1, 3 * mm))
    page2_content.append(Paragraph("<b>BACKGROUND</b>",   S["section_hdr"]))
    page2_content.append(Paragraph(RPL_BACKGROUND,        S["justify"]))
    page2_content.append(Spacer(1, 2 * mm))
    page2_content.append(Paragraph("<b>DISCLAIMERS</b>",  S["section_hdr"]))
    for i, disc in enumerate(RPL_DISCLAIMERS, 1):
        page2_content.append(Paragraph(f"{i}.  {disc}", S["disc_item"]))
    page2_content.append(Spacer(1, 4 * mm))
    page2_content += _signature_block(signatories, S)

    # Keep methodology+background+disclaimers+signatures together as one unit
    # This will naturally flow to page 2 if page 1 content fills the page
    elems.append(KeepTogether(page2_content))

    return elems


# ─── Top-level entry point ────────────────────────────────────────────────────

def generate_pdf(case: dict, output_path: str) -> str:
    """
    Generate a PDF for the given case dict and save to output_path.
    The report type (single_hla / transplant_donor / rpl_couple) is read directly
    from case["report_type"] — set automatically by hla_data_parser.py.
    Returns output_path.
    """
    _register_fonts()
    S = _styles()

    report_type = case.get("report_type", "single_hla")
    nabl        = case.get("nabl", True)
    with_logo   = case.get("with_logo", True)

    # Title strings per report type
    TITLES = {
        "single_hla":       "HLA Typing High Resolution",
        "transplant_donor": "HLA Typing High Resolution",
        "rpl_couple":       "HLA Typing \u2013 NGS High Resolution Typing",
    }
    title = TITLES.get(report_type, "HLA Typing Report")

    # Title paragraph (font differs between NGS and RPL)
    title_style = S["title_rpl"] if report_type == "rpl_couple" else S["title_ngs"]
    title_para  = Paragraph(title, title_style)

    # Compute header/footer heights and adjust margins accordingly
    from PIL import Image as PILImage

    # Reserve space for header and footer banners (always, for consistent layout)
    b64  = hla_assets.HEADER_NABL_B64 if nabl else hla_assets.HEADER_NONNABL_B64
    raw  = hla_assets.get_image_bytes(b64)
    pil  = PILImage.open(io.BytesIO(raw))
    ow, oh = pil.size
    banner_h = (oh / ow) * CONTENT_W

    raw_f  = hla_assets.get_image_bytes(hla_assets.FOOTER_BAR_B64)
    pil_f  = PILImage.open(io.BytesIO(raw_f))
    fw, fh = pil_f.size
    footer_h = (fh / fw) * CONTENT_W

    top_margin    = MARGIN_T + banner_h + 4 * mm
    bottom_margin = MARGIN_B + footer_h + 4 * mm

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=MARGIN_L, rightMargin=MARGIN_R,
        topMargin=top_margin,
        bottomMargin=bottom_margin,
    )

    # Build story
    if report_type == "single_hla":
        body = _build_ngs_single(case, S)
    elif report_type == "transplant_donor":
        body = _build_ngs_transplant(case, S)
    elif report_type == "rpl_couple":
        body = _build_rpl_couple(case, S)
    else:
        body = _build_ngs_single(case, S)

    story = [title_para, Spacer(1, 3 * mm)] + body

    # Total pages: estimate based on report type
    # For RPL couple: typically 2-3 pages (patient/donor table + methodology/background + signatures)
    # For NGS: 1 page per person (patient + donors) + 1 for signatures
    if report_type == "rpl_couple":
        # RPL reports with donor typically span 2-3 pages, estimate 3 for page numbering
        total_pages = 3
    else:
        # NGS reports: estimate 1 page per patient/donor block
        total_pages = 1 + len(case.get("donors", []))

    cb = _HFCanvas(case, title, banner_h, footer_h, total_pages=total_pages)
    doc.build(story, onFirstPage=cb, onLaterPages=cb)
    return output_path


# ─── Filename helper ──────────────────────────────────────────────────────────

def make_filename(case: dict) -> str:
    def safe(s):
        return re.sub(r"[^\w.\-]", "_", str(s).strip()).strip("_") or "Unknown"
    p = safe(case["patient"].get("name", ""))
    donors = "_".join(safe(d.get("name", "")) for d in case.get("donors", []))
    rtype = {"single_hla": "HLA_NGS", "transplant_donor": "HLA_NGS",
             "rpl_couple": "RPL"}.get(case.get("report_type", ""), "HLA")
    logo  = "WITH_LOGO" if case.get("with_logo", True) else "WITHOUT_LOGO"
    parts = [p] + ([donors] if donors else []) + [rtype, logo]
    return "_".join(parts) + ".pdf"
