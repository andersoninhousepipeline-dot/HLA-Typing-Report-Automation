"""
hla_report_generator.py
PyQt6 GUI — HLA Typing Report Generator
Style and layout adapted directly from PGTA Report Generator.
"""

import os
import sys
import json
import copy
import subprocess
import platform
import datetime
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QPushButton, QLabel, QLineEdit, QFileDialog,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QCheckBox, QComboBox, QMessageBox, QProgressBar,
    QGroupBox, QScrollArea, QSplitter, QFrame,
    QListWidget, QListWidgetItem, QDialog, QDialogButtonBox,
    QTextEdit, QSpinBox, QSizePolicy, QTextBrowser,
    QAbstractItemView, QStyle
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSettings, QTimer, QSize
from PyQt6.QtGui import QFont, QColor, QPalette, QIcon, QPixmap, QImage

try:
    from PyQt6.QtPdf import QPdfDocument
    from PyQt6.QtPdfWidgets import QPdfView
    QTPDF_OK = True
except ImportError:
    QPdfDocument = QPdfView = None
    QTPDF_OK = False

try:
    import fitz as _fitz
    FITZ_OK = True
except ImportError:
    _fitz = None
    FITZ_OK = False

import re as _re

import hla_assets
from hla_data_parser import parse_excel, get_case_summary, c_supertype, compute_rpl_reference
from hla_template import generate_pdf, make_filename, unique_output_path


# ─── Fix 5: Insufficient-Data filter ─────────────────────────────────────────
def _has_insufficient_data(person: dict) -> bool:
    """Return True if any HLA allele for this person was 'Insufficient Data'.

    Fix 3: _build_person now sanitises alleles to None before storing them, so
    the raw string is no longer present in person['hla'].  Instead it sets a
    '_has_insufficient_hla' flag before sanitisation — check that first.
    The string-search fallback handles manually-entered data or older dicts.
    """
    if person.get("_has_insufficient_hla", False):
        return True
    # Fallback: string scan (covers manual entry or dicts without the flag)
    hla = person.get("hla", {})
    for alleles in hla.values():
        for allele in (alleles or []):
            if allele and _re.search(r"insufficient\s*data", str(allele), _re.IGNORECASE):
                return True
    return False


def _filter_valid_cases(cases: list) -> list:
    """Silently drop any case whose patient has an 'Insufficient Data' HLA value."""
    return [c for c in cases if not _has_insufficient_data(c.get("patient", {}))]


# ─── Constants ────────────────────────────────────────────────────────────────
HLA_LOCI = ["A", "B", "C", "DRB1", "DQB1", "DPB1", "DRB3", "DRB4", "DRB5"]
MANUAL_DRAFT_FILE   = os.path.join(os.path.dirname(__file__), "hla_manual_draft.json")
BULK_DRAFT_FILE     = os.path.join(os.path.dirname(__file__), "hla_bulk_draft.json")
TEMP_PREVIEW_PATH   = os.path.join(os.path.dirname(__file__), "temp_hla_preview.pdf")
DRAFTS_DIR          = os.path.join(os.path.dirname(__file__), "drafts")

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "template")

# Report template definitions — each entry maps a friendly name to its report_type
# and the reference PDF template path (used for preview in Settings).
REPORT_TEMPLATES = [
    {
        "name":         "With CL",
        "report_type":  "single_hla",
        "default_path": os.path.join(_TEMPLATE_DIR, "Dummy_NGS High Resolution 28.10.2025.pdf"),
    },
    {
        "name":         "RPL",
        "report_type":  "rpl_couple",
        "default_path": os.path.join(_TEMPLATE_DIR, "HLA fertility _RPL_WITH LOGO.pdf"),
    },
    {
        "name":         "HLA Typing High Resolution (Transplant Donor)",
        "report_type":  "transplant_donor",
        "default_path": os.path.join(_TEMPLATE_DIR, "Dummy_NGS High Resolution 28.10.2025.pdf"),
    },
    {
        "name":         "CDC",
        "report_type":  "cdc_crossmatch",
        "default_path": os.path.join(_TEMPLATE_DIR, ""),
    },
    {
        "name":         "DSA",
        "report_type":  "dsa_crossmatch",
        "default_path": os.path.join(_TEMPLATE_DIR, ""),
    },
    {
        "name":         "SAB Class I",
        "report_type":  "sab_class1",
        "default_path": os.path.join(_TEMPLATE_DIR, ""),
    },
    {
        "name":         "SAB Class II",
        "report_type":  "sab_class2",
        "default_path": os.path.join(_TEMPLATE_DIR, ""),
    },
    {
        "name":         "Flow",
        "report_type":  "flow_crossmatch",
        "default_path": os.path.join(_TEMPLATE_DIR, ""),
    },
    {
        "name":         "HLA Typing (Luminex)",
        "report_type":  "luminex_typing",
        "default_path": os.path.join(_TEMPLATE_DIR, ""),
    },
]
TEMPLATE_NAMES    = [t["name"]        for t in REPORT_TEMPLATES]
TEMPLATE_TO_RTYPE = {t["name"]:        t["report_type"] for t in REPORT_TEMPLATES}
RTYPE_TO_TEMPLATE = {t["report_type"]: t["name"]        for t in REPORT_TEMPLATES}

DEFAULT_SIGNATORIES = [
    {"name": "Ms. S Aruna Devi",      "title": "Team Lead – Transplant Immunogenetics<br/>(Reviewed By)"},
    {"name": "Nikhala Shree S, Ph.D", "title": "Molecular Biologist"},
    {"name": "Dr. B. Rayvathy",        "title": "Consultant Microbiologist"},
]

# Minimal style constants — same as PGTA
GENERATE_BTN_STYLE = (
    "background-color: #1F497D; color: white; font-weight: bold; padding: 8px;"
)
PATH_LABEL_STYLE  = "padding: 5px; border: 1px solid #ccc; background: white;"
STATUS_LABEL_STYLE = "padding: 5px; color: #666; font-style: italic;"


# ─── ClickOnlyComboBox (same as PGTA) ─────────────────────────────────────────
class ClickOnlyComboBox(QComboBox):
    """Ignores mouse wheel events to prevent accidental changes."""
    def wheelEvent(self, event):
        event.ignore()


# ─── Background preview worker (same pattern as PGTA PreviewWorker) ──────────
class PreviewWorker(QThread):
    finished = pyqtSignal(str)
    error    = pyqtSignal(str)

    def __init__(self, case, output_path):
        super().__init__()
        self.case        = case
        self.output_path = output_path

    def run(self):
        try:
            generate_pdf(self.case, self.output_path)
            self.finished.emit(self.output_path)
        except Exception as e:
            self.error.emit(str(e))


# ─── Worker thread ────────────────────────────────────────────────────────────
class GenerateWorker(QThread):
    progress = pyqtSignal(int, str)
    finished = pyqtSignal(list, list)
    error    = pyqtSignal(str)

    def __init__(self, cases, output_dir, with_logo, signatories,
                 sig_count_single, sig_count_donor, signature_stamp):
        super().__init__()
        self.cases            = cases
        self.output_dir       = output_dir
        self.with_logo        = with_logo
        self.signatories      = signatories
        self.sig_count_single = sig_count_single
        self.sig_count_donor  = sig_count_donor
        self.signature_stamp  = signature_stamp

    def run(self):
        os.makedirs(self.output_dir, exist_ok=True)
        success, failed = [], []
        total = len(self.cases)
        for i, case in enumerate(self.cases):
            c = copy.deepcopy(case)
            c["with_logo"]       = self.with_logo
            c["signature_stamp"] = self.signature_stamp
            rtype = c.get("report_type", "single_hla")
            nabl  = c.get("nabl", True)
            n = self.sig_count_single if rtype == "single_hla" else self.sig_count_donor
            c["signatories"] = []
            for sig in self.signatories[:n]:
                sign_info = hla_assets.SIGN_BY_NAME.get(sig["name"])
                if sign_info is None:
                    # fallback: use first available named sign
                    sign_info = next(iter(hla_assets.SIGN_BY_NAME.values()))
                entry = {
                    "name":     sig["name"],
                    "title":    sig["title"],
                    "sign_b64": sign_info["sign_b64"],
                    "is_png":   sign_info["is_png"],
                }
                # Rubber seal: whenever stamp setting is ON, across all templates and both NABL/non-NABL
                if self.signature_stamp and "rayvathy" in sig["name"].lower():
                    entry["seal_b64"] = hla_assets.SEAL_REVATHY_B64
                # Apply custom signature override if provided
                if hasattr(sig, "get") and sig.get("sign_override_b64"):
                    entry["sign_b64"] = sig["sign_override_b64"]
                    entry["is_png"]   = sig.get("sign_override_is_png", True)
                c["signatories"].append(entry)
            # Apply per-case signature name overrides (selected from SIGN_BY_NAME lookup)
            _title_lookup = {s["name"]: s["title"] for s in DEFAULT_SIGNATORIES}
            for slot, sig_name in case.get("sig_name_overrides", {}).items():
                try:
                    slot_i = int(slot)
                    sign_info = hla_assets.SIGN_BY_NAME.get(sig_name)
                    if sign_info and 0 <= slot_i < len(c["signatories"]):
                        c["signatories"][slot_i]["sign_b64"] = sign_info["sign_b64"]
                        c["signatories"][slot_i]["is_png"]   = sign_info["is_png"]
                        c["signatories"][slot_i]["name"]     = sig_name
                        if sig_name in _title_lookup:
                            c["signatories"][slot_i]["title"] = _title_lookup[sig_name]
                except Exception:
                    pass
            fname    = make_filename(c)
            out_path = unique_output_path(self.output_dir, fname)
            fname    = os.path.basename(out_path)   # reflect _(2) suffix if added
            self.progress.emit(int(i / total * 100),
                               f"Generating {fname} ({i+1}/{total})...")
            try:
                generate_pdf(c, out_path)
                success.append(fname)
            except Exception as e:
                failed.append((fname, str(e)))
                self.error.emit(f"Error generating {fname}: {e}")
                import traceback; traceback.print_exc()
        self.progress.emit(100, "Generation complete!")
        self.finished.emit(success, failed)


# ─── Signatory dialog ─────────────────────────────────────────────────────────
class SignatoryDialog(QDialog):
    def __init__(self, signatories, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Signatories")
        self.setMinimumWidth(520)
        self._sigs = copy.deepcopy(signatories)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Configure signatories (top = first in report):"))
        self.tbl = QTableWidget(0, 2)
        self.tbl.setHorizontalHeaderLabels(["Name", "Title / Role"])
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setAlternatingRowColors(True)
        lay.addWidget(self.tbl)
        btn_row = QHBoxLayout()
        for lbl, fn in [("+ Add", self._add), ("✕ Remove", self._rm),
                         ("↑ Up", self._up), ("↓ Down", self._dn)]:
            b = QPushButton(lbl); b.clicked.connect(fn); btn_row.addWidget(b)
        btn_row.addStretch()
        lay.addLayout(btn_row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._load()

    def _load(self):
        self.tbl.setRowCount(0)
        for s in self._sigs:
            r = self.tbl.rowCount(); self.tbl.insertRow(r)
            self.tbl.setItem(r, 0, QTableWidgetItem(s["name"]))
            self.tbl.setItem(r, 1, QTableWidgetItem(s["title"]))

    def _add(self):
        r = self.tbl.rowCount(); self.tbl.insertRow(r)
        self.tbl.setItem(r, 0, QTableWidgetItem("Name"))
        self.tbl.setItem(r, 1, QTableWidgetItem("Title"))

    def _rm(self):
        r = self.tbl.currentRow()
        if r >= 0: self.tbl.removeRow(r)

    def _up(self):
        r = self.tbl.currentRow()
        if r > 0: self._swap(r, r - 1); self.tbl.selectRow(r - 1)

    def _dn(self):
        r = self.tbl.currentRow()
        if r < self.tbl.rowCount() - 1: self._swap(r, r + 1); self.tbl.selectRow(r + 1)

    def _swap(self, a, b):
        for c in range(2):
            ai = self.tbl.item(a, c); bi = self.tbl.item(b, c)
            at = ai.text() if ai else ""; bt = bi.text() if bi else ""
            self.tbl.setItem(a, c, QTableWidgetItem(bt))
            self.tbl.setItem(b, c, QTableWidgetItem(at))

    def get_signatories(self):
        out = []
        for r in range(self.tbl.rowCount()):
            n = (self.tbl.item(r, 0) or QTableWidgetItem()).text().strip()
            t = (self.tbl.item(r, 1) or QTableWidgetItem()).text().strip()
            if n: out.append({"name": n, "title": t})
        return out


# ─── HLA allele row builder (shared by both tabs) ─────────────────────────────
def _make_allele_row(allele1="", allele2=""):
    """Return (widget, a1_lineedit, a2_lineedit) for one locus row."""
    row_w = QWidget()
    rh    = QHBoxLayout(row_w)
    rh.setContentsMargins(0, 0, 0, 0)
    a1 = QLineEdit(allele1)
    a2 = QLineEdit(allele2)
    a1.setMaximumHeight(24); a2.setMaximumHeight(24)
    a1.setPlaceholderText("Allele 1 (e.g. A*02:01:01:01)")
    a2.setPlaceholderText("Allele 2")
    rh.addWidget(a1, 1)
    rh.addWidget(a2, 1)
    return row_w, a1, a2


def _allele_str(val) -> str:
    """Convert a stored allele value to a display string. None → empty string."""
    return str(val) if val is not None else ""


def _render_pdf_pages(pdf_path: str, width_px: int = 600) -> list:
    """Render every page of a PDF to QPixmap using fitz. Returns list of QPixmap."""
    if not FITZ_OK or not os.path.exists(pdf_path):
        return []
    pixmaps = []
    try:
        doc = _fitz.open(pdf_path)
        for page in doc:
            zoom   = width_px / page.rect.width
            mat    = _fitz.Matrix(zoom, zoom)
            pix    = page.get_pixmap(matrix=mat, alpha=False)
            img    = QImage(pix.samples, pix.width, pix.height,
                            pix.stride, QImage.Format.Format_RGB888)
            pixmaps.append(QPixmap.fromImage(img))
        doc.close()
    except Exception as e:
        print(f"[preview] fitz error: {e}")
    return pixmaps


# ─── Main Application (single class — same pattern as PGTA) ───────────────────
class HLAReportGeneratorApp(QMainWindow):

    def __init__(self):
        super().__init__()
        self.qsettings         = QSettings("AndersonDiagnostics", "HLAReportGenerator")
        self.cases             = []
        self._bulk_current_row = -1
        self.worker            = None
        # Bulk editor field refs (rebuilt each selection)
        self._bulk_fields      = {}   # patient info QLineEdits
        self._bulk_hla_pat     = {}   # {locus: [a1, a2]}
        self._bulk_donor_fields  = []   # list of dicts per donor
        self._bulk_hla_don     = []   # list of {locus: [a1, a2]} per donor
        # Bulk specialised-editor photo byte caches — initialised here so that
        # _flush_bulk_edits never raises AttributeError on the very first call.
        self._bulk_cdc_photo_bytes = {}
        self._bulk_dsa_photo_bytes = {}
        self._bulk_flow_photo_bytes = {}
        # Manual tab state
        self._loading_draft           = False  # guard: skip preview during draft load
        self._manual_sig_name_overrides = {}   # {slot: sig_name}
        self._manual_donors           = []     # list of donor panel dicts
        
        # Debounce timers for real-time preview
        self._edit_timer = QTimer()
        self._edit_timer.setSingleShot(True)
        self._edit_timer.timeout.connect(self._refresh_bulk_preview)

        self._manual_edit_timer = QTimer()
        self._manual_edit_timer.setSingleShot(True)
        self._manual_edit_timer.timeout.connect(self._refresh_manual_preview)

        self.init_ui()
        self._load_persistent()

    # ══════════════════════════════════════════════════════════════════════════
    # UI BOOTSTRAP
    # ══════════════════════════════════════════════════════════════════════════
    def init_ui(self):
        self.setWindowTitle("HLA Typing Report Generator")
        self.setGeometry(100, 100, 1300, 850)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(10, 0, 10, 5)
        main_layout.setSpacing(2)
        central.setLayout(main_layout)

        # Header — matches PGTA exactly
        top_row = QHBoxLayout()
        title_label = QLabel("HLA Typing Report Generator")
        title_label.setStyleSheet("font-size: 18px; font-weight: bold; padding: 0px;")
        title_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        top_row.addWidget(title_label)
        top_row.addStretch()
        
        # Right side selectors (two rows if needed, but we'll try compact first)
        right_panel = QWidget()
        right_lay   = QFormLayout(right_panel)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(5)

        self.template_combo = ClickOnlyComboBox()
        self.template_combo.addItems(TEMPLATE_NAMES)
        self.template_combo.setMinimumWidth(280)
        self.template_combo.setFixedHeight(24)
        right_lay.addRow("<b>Select Template:</b>", self.template_combo)

        self.logo_combo = ClickOnlyComboBox()
        self.logo_combo.addItems(["With Logo", "Without Logo"])
        self.logo_combo.setFixedWidth(140)
        self.logo_combo.setFixedHeight(24)
        right_lay.addRow("<b>Select Logo:</b>", self.logo_combo)
        
        top_row.addWidget(right_panel)
        main_layout.addLayout(top_row)

        # Tabs
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        self.manual_tab   = self._create_manual_tab()
        self.bulk_tab     = self._create_bulk_tab()
        self.settings_tab = self._create_settings_tab()
        self.guide_tab    = self._create_guide_tab()

        self.tabs.addTab(self.manual_tab,   "Manual Entry")
        self.tabs.setTabIcon(0, self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
        self.tabs.addTab(self.bulk_tab,     "Bulk Upload")
        self.tabs.setTabIcon(1, self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogListView))
        self.tabs.addTab(self.settings_tab, "Settings")
        self.tabs.setTabIcon(2, self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        self.tabs.addTab(self.guide_tab,    "User Guide")
        self.tabs.setTabIcon(3, self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation))

        
        # Connect global selectors to refresh functions
        self.template_combo.currentIndexChanged.connect(self._on_global_pref_changed)
        self.logo_combo.currentIndexChanged.connect(self._on_global_pref_changed)

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 1 — MANUAL ENTRY
    # ══════════════════════════════════════════════════════════════════════════
    def _create_manual_tab(self):
        tab = QWidget()
        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(2, 2, 2, 2)
        main_layout.setSpacing(2)
        tab.setLayout(main_layout)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter)

        # ── Left: form ─────────────────────────────────────────────────────
        left_widget = QWidget()
        left_layout = QVBoxLayout()
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(2)
        left_widget.setLayout(left_layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(0)   # allow shrinking so draft/gen buttons always show
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout()
        scroll_layout.setSpacing(2)
        scroll_layout.setContentsMargins(2, 2, 2, 2)
        scroll_widget.setLayout(scroll_layout)
        scroll.setWidget(scroll_widget)
        left_layout.addWidget(scroll, 1)

        # Patient Information
        pat_group = QGroupBox("Patient Information")
        self._std_pat_group = pat_group   # ref for show/hide when switching templates
        pat_form  = QFormLayout(); pat_group.setLayout(pat_form)
        pat_form.setSpacing(1); pat_form.setContentsMargins(4, 2, 4, 2)
        scroll_layout.addWidget(pat_group)

        self.f = {}
        PAT_FIELDS = [
            ("patient_name",    "Patient Name *",       ""),
            ("gender_age",      "Gender / Age",         ""),
            ("hospital_mr_no",  "Hospital MR No.",      "NA"),
            ("diagnosis",       "Diagnosis",            ""),
            ("referred_by",     "Referred By",          ""),
            ("hospital_clinic", "Hospital / Clinic",    ""),
            ("pin",             "PIN *",                ""),
            ("sample_number",   "Sample Number",        ""),
            ("specimen",        "Specimen",             "Blood - EDTA"),
            ("collection_date", "Collection Date",      ""),
            ("receipt_date",    "Sample Receipt Date",  ""),
            ("report_date",     "Report Date",          ""),
            ("remarks",         "Remarks",              ""),
        ]
        for key, lbl, default in PAT_FIELDS:
            w = QLineEdit(default)
            if "date" in key.lower(): w.setPlaceholderText("DD-MM-YYYY")
            w.setMaximumHeight(24)
            self.f[key] = w
            pat_form.addRow(lbl + ":", w)
            if key == "diagnosis":
                w.textChanged.connect(self._auto_detect_manual_template)
            w.textChanged.connect(self._on_manual_field_debounced)

        self._manual_nabl_chk = QCheckBox("NABL Accreditation")
        self._manual_nabl_chk.setChecked(self.qsettings.value("nabl_stamp", True, type=bool))
        self._manual_nabl_chk.stateChanged.connect(self._on_manual_field_debounced)
        pat_form.addRow(self._manual_nabl_chk)

        self._manual_seal_chk = QCheckBox("Signature Seal")
        self._manual_seal_chk.setChecked(self.qsettings.value("signature_stamp", False, type=bool))
        self._manual_seal_chk.stateChanged.connect(self._refresh_manual_preview)
        pat_form.addRow(self._manual_seal_chk)

        # ── Report Settings ────────────────────────────────────────────────────
        rs_group = QGroupBox("Report Settings")
        rs_form  = QFormLayout(); rs_group.setLayout(rs_form)
        rs_form.setSpacing(1); rs_form.setContentsMargins(4, 2, 4, 2)

        self._manual_rtype_combo = ClickOnlyComboBox()
        self._manual_rtype_combo.addItems(TEMPLATE_NAMES)
        self._manual_rtype_combo.setFixedHeight(24)
        self._manual_rtype_combo.setCurrentIndex(self.template_combo.currentIndex())
        self._manual_rtype_combo.currentIndexChanged.connect(self._on_manual_rtype_changed)
        rs_form.addRow("Report Type:", self._manual_rtype_combo)

        self._manual_report_settings = {}
        RS_FIELDS = [
            ("typing_status", "Typing Status",     "Complete"),
            ("imgt_release",  "IMGT Release",      ""),
            ("methodology",   "Methodology",       ""),
            ("coverage",      "Coverage Override", ""),
        ]
        for key, lbl, default in RS_FIELDS:
            w = QLineEdit(default)
            w.setFixedHeight(24)
            self._manual_report_settings[key] = w
            rs_form.addRow(lbl + ":", w)
            w.textChanged.connect(self._on_manual_field_debounced)
        scroll_layout.addWidget(rs_group)

        # ── RPL Reference (shown only when RPL template selected) ──────────────
        self._manual_rpl_group = QGroupBox("RPL / Fertility Reference (calculated, editable)")
        rpl_form = QFormLayout(); self._manual_rpl_group.setLayout(rpl_form)
        rpl_form.setSpacing(1); rpl_form.setContentsMargins(4, 2, 4, 2)
        self._manual_rpl_fields = {}
        RPL_FIELDS_MANUAL = [
            ("match_str",       "Match (Overall)"),
            ("match_pct",       "Overall %"),
            ("class2_pct",      "Class-II %"),
            ("hla_sharing_rif", "HLA Sharing (RIF)"),
            ("hla_c_patient",   "Maternal HLA-C Type"),
            ("hla_c_donor",     "Paternal HLA-C Type"),
        ]
        for key, lbl in RPL_FIELDS_MANUAL:
            w = QLineEdit()
            w.setMaximumHeight(24)
            self._manual_rpl_fields[key] = w
            rpl_form.addRow(lbl + ":", w)
            w.textChanged.connect(self._on_manual_field_debounced)

        def _on_manual_match_str_changed(text, _fields=self._manual_rpl_fields):
            m = _re.search(r'(\d+)\s+of\s+(\d+)', text, _re.I)
            pct_w = _fields.get("match_pct")
            if m and pct_w:
                n, total = int(m.group(1)), int(m.group(2))
                pct = round(n / total * 100) if total else 0
                pct_w.blockSignals(True)
                pct_w.setText(f"{pct}%")
                pct_w.blockSignals(False)
        self._manual_rpl_fields["match_str"].textChanged.connect(_on_manual_match_str_changed)

        scroll_layout.addWidget(self._manual_rpl_group)
        self._manual_rpl_group.setVisible(False)

        # ── CDC Patient Information (shown only when CDC template selected) ──────
        self._manual_photo_bytes = {}   # {"patient": bytes, "donor": bytes}
        self._cdc_pat_f  = {}           # QLineEdit widgets for CDC patient fields
        self._cdc_don_f  = {}           # QLineEdit widgets for CDC donor fields
        self._manual_cdc_fields = {}    # QComboBox widgets for CDC result dropdowns

        _cdc_pat_group = QGroupBox("Patient Information")
        self._cdc_pat_group = _cdc_pat_group
        _cpf = QFormLayout(); _cdc_pat_group.setLayout(_cpf)
        _cpf.setSpacing(1); _cpf.setContentsMargins(4, 2, 4, 2)

        _CDC_PAT_FIELDS = [
            ("patient_name",    "Patient Name *",       ""),
            ("gender_age",      "Gender / Age",         ""),
            ("pin",             "PIN *",                ""),
            ("sample_number",   "Sample Number",        ""),
            ("diagnosis",       "Diagnosis",            "NA"),
            ("hospital_clinic", "Hospital / Clinic",    ""),
            ("sample_type",     "Sample Type",          "Serum"),
            ("collection_date", "Collection Date",      ""),
            ("receipt_date",    "Sample Receipt Date",  ""),
            ("report_date",     "Report Date",          ""),
            ("remarks",         "Remarks",              ""),
            ("comments",        "Additional Comments",  ""),
        ]
        for _k, _l, _d in _CDC_PAT_FIELDS:
            _w = QLineEdit(_d); _w.setMaximumHeight(24)
            if "date" in _k: _w.setPlaceholderText("DD-MM-YYYY")
            self._cdc_pat_f[_k] = _w
            _cpf.addRow(_l + ":", _w)
            _w.textChanged.connect(self._on_manual_field_debounced)

        # Patient photo
        _cpp_row = QHBoxLayout()
        self._manual_cdc_patient_photo_lbl = QLabel("No photo selected")
        self._manual_cdc_patient_photo_lbl.setStyleSheet("color:gray;font-style:italic;")
        _cpp_btn = QPushButton("Upload Patient Photo"); _cpp_btn.setMaximumHeight(26)
        _cpp_btn.clicked.connect(lambda: self._upload_cdc_photo("patient"))
        _cpp_row.addWidget(self._manual_cdc_patient_photo_lbl, 1); _cpp_row.addWidget(_cpp_btn)
        _cpf.addRow("Patient Photo:", _cpp_row)

        # NABL / Seal — separate checkboxes for CDC (can't share Qt widget with std form)
        self._cdc_nabl_chk = QCheckBox("NABL Accreditation")
        self._cdc_nabl_chk.setChecked(self.qsettings.value("nabl_stamp", True, type=bool))
        self._cdc_nabl_chk.stateChanged.connect(self._on_manual_field_debounced)
        self._cdc_seal_chk = QCheckBox("Signature Seal")
        self._cdc_seal_chk.setChecked(self.qsettings.value("signature_stamp", False, type=bool))
        self._cdc_seal_chk.stateChanged.connect(self._refresh_manual_preview)
        _cpf.addRow(self._cdc_nabl_chk)
        _cpf.addRow(self._cdc_seal_chk)

        scroll_layout.addWidget(_cdc_pat_group)
        _cdc_pat_group.setVisible(False)

        # ── CDC Donor Information ─────────────────────────────────────────────
        _cdc_don_group = QGroupBox("Donor Information")
        self._cdc_don_group = _cdc_don_group
        _cdf = QFormLayout(); _cdc_don_group.setLayout(_cdf)
        _cdf.setSpacing(1); _cdf.setContentsMargins(4, 2, 4, 2)

        _CDC_DON_FIELDS = [
            ("name",             "Donor Name",                  ""),
            ("gender_age",       "Gender / Age",                ""),
            ("pin",              "Donor PIN",                   "NA"),
            ("sample_number",    "Sample Number",               "NA"),
            ("relationship",     "Relationship to Recipient",   ""),
            ("sample_type",      "Sample Type",                 "Sodium Heparin Whole Blood"),
            ("collection_date",  "Collection Date",             ""),
            ("receipt_date",     "Sample Receipt Date",         ""),
            ("report_date",      "Report Date",                 ""),
        ]
        for _k, _l, _d in _CDC_DON_FIELDS:
            _w = QLineEdit(_d); _w.setMaximumHeight(24)
            if "date" in _k: _w.setPlaceholderText("DD-MM-YYYY")
            self._cdc_don_f[_k] = _w
            _cdf.addRow(_l + ":", _w)
            _w.textChanged.connect(self._on_manual_field_debounced)

        # Donor photo
        _cdp_row = QHBoxLayout()
        self._manual_cdc_donor_photo_lbl = QLabel("No photo selected")
        self._manual_cdc_donor_photo_lbl.setStyleSheet("color:gray;font-style:italic;")
        _cdp_btn = QPushButton("Upload Donor Photo"); _cdp_btn.setMaximumHeight(26)
        _cdp_btn.clicked.connect(lambda: self._upload_cdc_photo("donor"))
        _cdp_row.addWidget(self._manual_cdc_donor_photo_lbl, 1); _cdp_row.addWidget(_cdp_btn)
        _cdf.addRow("Donor Photo:", _cdp_row)

        scroll_layout.addWidget(_cdc_don_group)
        _cdc_don_group.setVisible(False)

        # ── CDC Results ───────────────────────────────────────────────────────
        _cdc_res_group = QGroupBox("CDC Results")
        self._cdc_res_group = _cdc_res_group
        _crf = QFormLayout(); _cdc_res_group.setLayout(_crf)
        _crf.setSpacing(1); _crf.setContentsMargins(4, 2, 4, 2)

        _CDC_RESULT_OPTIONS = ["Negative", "Doubtful", "Weak Positive", "Positive", "Strong Positive"]
        _CDC_DTT_OPTIONS    = ["<10% Dead cells", "10-20% Dead cells", "20-50% Dead cells",
                               "50-80% Dead cells", ">80% Dead cells"]
        for _k, _l, _opts in [
            ("t_cell",       "T Cell Crossmatch",   _CDC_RESULT_OPTIONS),
            ("b_cell",       "B Cell Crossmatch",   _CDC_RESULT_OPTIONS),
            ("t_with_dtt",   "T Cells (With DTT)",  _CDC_DTT_OPTIONS),
            ("t_without_dtt","T Cells (Without DTT)",_CDC_DTT_OPTIONS),
            ("b_with_dtt",   "B Cells (With DTT)",  _CDC_DTT_OPTIONS),
            ("b_without_dtt","B Cells (Without DTT)",_CDC_DTT_OPTIONS),
        ]:
            _cmb = ClickOnlyComboBox(); _cmb.addItems(_opts); _cmb.setFixedHeight(24)
            self._manual_cdc_fields[_k] = _cmb
            _crf.addRow(_l + ":", _cmb)
            _cmb.currentIndexChanged.connect(self._on_manual_field_debounced)

        _CDC_RES_TO_DTT = {
            "Negative":        "<10% Dead cells",
            "Doubtful":        "10-20% Dead cells",
            "Weak Positive":   "20-50% Dead cells",
            "Positive":        "50-80% Dead cells",
            "Strong Positive": ">80% Dead cells",
        }
        _DTT_TO_CDC_RES = {v: k for k, v in _CDC_RES_TO_DTT.items()}
        def _sync_manual_t_dtt():
            v = _CDC_RES_TO_DTT.get(self._manual_cdc_fields["t_cell"].currentText())
            if v:
                self._manual_cdc_fields["t_with_dtt"].setCurrentText(v)
                self._manual_cdc_fields["t_without_dtt"].setCurrentText(v)
        def _sync_manual_b_dtt():
            v = _CDC_RES_TO_DTT.get(self._manual_cdc_fields["b_cell"].currentText())
            if v:
                self._manual_cdc_fields["b_with_dtt"].setCurrentText(v)
                self._manual_cdc_fields["b_without_dtt"].setCurrentText(v)
        def _sync_manual_t_from_dtt():
            r = _DTT_TO_CDC_RES.get(self._manual_cdc_fields["t_with_dtt"].currentText())
            if r: self._manual_cdc_fields["t_cell"].setCurrentText(r)
        def _sync_manual_b_from_dtt():
            r = _DTT_TO_CDC_RES.get(self._manual_cdc_fields["b_with_dtt"].currentText())
            if r: self._manual_cdc_fields["b_cell"].setCurrentText(r)
        self._manual_cdc_fields["t_cell"].currentIndexChanged.connect(lambda _: _sync_manual_t_dtt())
        self._manual_cdc_fields["b_cell"].currentIndexChanged.connect(lambda _: _sync_manual_b_dtt())
        self._manual_cdc_fields["t_with_dtt"].currentIndexChanged.connect(lambda _: _sync_manual_t_from_dtt())
        self._manual_cdc_fields["t_without_dtt"].currentIndexChanged.connect(lambda _: _sync_manual_t_from_dtt())
        self._manual_cdc_fields["b_with_dtt"].currentIndexChanged.connect(lambda _: _sync_manual_b_from_dtt())
        self._manual_cdc_fields["b_without_dtt"].currentIndexChanged.connect(lambda _: _sync_manual_b_from_dtt())

        scroll_layout.addWidget(_cdc_res_group)
        _cdc_res_group.setVisible(False)

        # ── DSA Patient Information (shown only when DSA template selected) ──────
        self._dsa_photo_bytes = {}   # {"patient": bytes, "donor": bytes}
        self._dsa_pat_f  = {}        # QLineEdit widgets for DSA patient fields
        self._dsa_don_f  = {}        # QLineEdit widgets for DSA donor fields
        self._dsa_result_f = {}      # widgets for DSA result fields

        _dsa_pat_group = QGroupBox("Patient Information")
        self._dsa_pat_group = _dsa_pat_group
        _dpf = QFormLayout(); _dsa_pat_group.setLayout(_dpf)
        _dpf.setSpacing(1); _dpf.setContentsMargins(4, 2, 4, 2)

        _DSA_PAT_FIELDS = [
            ("patient_name",    "Patient Name *",       ""),
            ("gender_age",      "Gender / Age",         ""),
            ("pin",             "PIN *",                ""),
            ("sample_number",   "Sample Number",        ""),
            ("diagnosis",       "Diagnosis",            "NA"),
            ("hospital_clinic", "Hospital / Clinic",    ""),
            ("sample_type",     "Sample Type",          "Serum"),
            ("collection_date", "Collection Date",      ""),
            ("receipt_date",    "Sample Receipt Date",  ""),
            ("report_date",     "Report Date",          ""),
            ("remarks",         "Remarks",              ""),
            ("comments",        "Additional Comments",  ""),
        ]
        for _k, _l, _d in _DSA_PAT_FIELDS:
            _w = QLineEdit(_d); _w.setMaximumHeight(24)
            if "date" in _k: _w.setPlaceholderText("DD-MM-YYYY")
            self._dsa_pat_f[_k] = _w
            _dpf.addRow(_l + ":", _w)
            _w.textChanged.connect(self._on_manual_field_debounced)

        # Patient photo
        _dpp_row = QHBoxLayout()
        self._manual_dsa_patient_photo_lbl = QLabel("No photo selected")
        self._manual_dsa_patient_photo_lbl.setStyleSheet("color:gray;font-style:italic;")
        _dpp_btn = QPushButton("Upload Patient Photo"); _dpp_btn.setMaximumHeight(26)
        _dpp_btn.clicked.connect(lambda: self._upload_dsa_photo("patient", self._manual_dsa_patient_photo_lbl))
        _dpp_row.addWidget(self._manual_dsa_patient_photo_lbl, 1); _dpp_row.addWidget(_dpp_btn)
        _dpf.addRow("Patient Photo:", _dpp_row)

        # NABL / Seal — separate checkboxes for DSA
        self._dsa_nabl_chk = QCheckBox("NABL Accreditation")
        self._dsa_nabl_chk.setChecked(self.qsettings.value("nabl_stamp", True, type=bool))
        self._dsa_nabl_chk.stateChanged.connect(self._on_manual_field_debounced)
        self._dsa_seal_chk = QCheckBox("Signature Seal")
        self._dsa_seal_chk.setChecked(self.qsettings.value("signature_stamp", False, type=bool))
        self._dsa_seal_chk.stateChanged.connect(self._refresh_manual_preview)
        _dpf.addRow(self._dsa_nabl_chk)
        _dpf.addRow(self._dsa_seal_chk)

        scroll_layout.addWidget(_dsa_pat_group)
        _dsa_pat_group.setVisible(False)

        # ── DSA Donor Information ─────────────────────────────────────────────
        _dsa_don_group = QGroupBox("Donor Information")
        self._dsa_don_group = _dsa_don_group
        _ddf = QFormLayout(); _dsa_don_group.setLayout(_ddf)
        _ddf.setSpacing(1); _ddf.setContentsMargins(4, 2, 4, 2)

        _DSA_DON_FIELDS = [
            ("name",             "Donor Name",                  ""),
            ("gender_age",       "Gender / Age",                ""),
            ("pin",              "Donor PIN",                   "NA"),
            ("sample_number",    "Sample Number",               "NA"),
            ("relationship",     "Relationship to Recipient",   ""),
            ("sample_type",      "Sample Type",                 "ACD Tube"),
            ("collection_date",  "Collection Date",             ""),
            ("receipt_date",     "Sample Receipt Date",         ""),
            ("report_date",      "Report Date",                 ""),
        ]
        for _k, _l, _d in _DSA_DON_FIELDS:
            _w = QLineEdit(_d); _w.setMaximumHeight(24)
            if "date" in _k: _w.setPlaceholderText("DD-MM-YYYY")
            self._dsa_don_f[_k] = _w
            _ddf.addRow(_l + ":", _w)
            _w.textChanged.connect(self._on_manual_field_debounced)

        # Donor photo
        _ddp_row = QHBoxLayout()
        self._manual_dsa_donor_photo_lbl = QLabel("No photo selected")
        self._manual_dsa_donor_photo_lbl.setStyleSheet("color:gray;font-style:italic;")
        _ddp_btn = QPushButton("Upload Donor Photo"); _ddp_btn.setMaximumHeight(26)
        _ddp_btn.clicked.connect(lambda: self._upload_dsa_photo("donor", self._manual_dsa_donor_photo_lbl))
        _ddp_row.addWidget(self._manual_dsa_donor_photo_lbl, 1); _ddp_row.addWidget(_ddp_btn)
        _ddf.addRow("Donor Photo:", _ddp_row)

        scroll_layout.addWidget(_dsa_don_group)
        _dsa_don_group.setVisible(False)

        # ── DSA Results ───────────────────────────────────────────────────────
        _dsa_res_group = QGroupBox("DSA Results")
        self._dsa_res_group = _dsa_res_group
        _drf = QFormLayout(); _dsa_res_group.setLayout(_drf)
        _drf.setSpacing(1); _drf.setContentsMargins(4, 2, 4, 2)

        _DSA_RESULT_OPTIONS = ["Negative", "Positive", "Weakly Positive", "Borderline"]
        for _k, _l in [("class1_result", "Class I Result"), ("class2_result", "Class II Result")]:
            _cmb = ClickOnlyComboBox(); _cmb.addItems(_DSA_RESULT_OPTIONS); _cmb.setFixedHeight(24)
            self._dsa_result_f[_k] = _cmb
            _drf.addRow(_l + ":", _cmb)
            _cmb.currentIndexChanged.connect(self._on_manual_field_debounced)

        for _k, _l, _ph in [
            ("class1_mfi",    "Class I MFI",         "e.g. 405"),
            ("class1_cutoff", "Class I Cutoff",       ">1000"),
            ("class2_mfi",    "Class II MFI",         "e.g. 372"),
            ("class2_cutoff", "Class II Cutoff",      ">1000"),
        ]:
            _w = QLineEdit(); _w.setMaximumHeight(24)
            if _l.endswith("Cutoff"): _w.setText(_ph)
            else: _w.setPlaceholderText(_ph)
            self._dsa_result_f[_k] = _w
            _drf.addRow(_l + ":", _w)
            _w.textChanged.connect(self._on_manual_field_debounced)

        scroll_layout.addWidget(_dsa_res_group)
        _dsa_res_group.setVisible(False)

        # ── SAB Patient Information (shown only when SAB template selected) ──────
        self._sab_pat_f = {}
        _sab_pat_group = QGroupBox("Patient Information")
        self._sab_pat_group = _sab_pat_group
        _spf = QFormLayout(); _sab_pat_group.setLayout(_spf)
        _spf.setSpacing(1); _spf.setContentsMargins(4, 2, 4, 2)
        _SAB_PAT_FIELDS = [
            ("patient_name",    "Patient Name *",         ""),
            ("gender_age",      "Gender / Age",           ""),
            ("hospital_mr_no",  "Hospital MR No",         "NA"),
            ("specimen",        "Specimen",               "Serum"),
            ("hospital_clinic", "Hospital / Clinic",      ""),
            ("pin",             "PIN",                    ""),
            ("sample_number",   "Sample Number",          ""),
            ("collection_date", "Sample Collection Date", ""),
            ("receipt_date",    "Sample Receipt Date",    ""),
            ("report_date",     "Report Date",            ""),
            ("remarks",         "Remarks",                ""),
        ]
        for _k, _l, _dflt in _SAB_PAT_FIELDS:
            _w = QLineEdit(_dflt); _w.setFixedHeight(24)
            if "date" in _k: _w.setPlaceholderText("DD-MM-YYYY")
            _w.textChanged.connect(self._on_manual_field_debounced)
            self._sab_pat_f[_k] = _w
            _spf.addRow(_l + ":", _w)

        _sab_class_row = QHBoxLayout()
        _sab_class_lbl = QLabel("SAB Class:")
        self._sab_class_combo = ClickOnlyComboBox()
        self._sab_class_combo.addItems(["I", "II"])
        self._sab_class_combo.setFixedHeight(24)
        self._sab_class_combo.currentIndexChanged.connect(self._on_manual_field_debounced)
        _sab_class_row.addWidget(_sab_class_lbl); _sab_class_row.addWidget(self._sab_class_combo, 1)
        _spf.addRow(_sab_class_row)

        self._sab_nabl_chk = QCheckBox("NABL Accreditation")
        self._sab_nabl_chk.setChecked(self.qsettings.value("nabl_stamp", True, type=bool))
        self._sab_nabl_chk.stateChanged.connect(self._on_manual_field_debounced)
        _spf.addRow(self._sab_nabl_chk)
        self._sab_seal_chk = QCheckBox("Signature Seal")
        self._sab_seal_chk.setChecked(self.qsettings.value("sig_stamp", True, type=bool))
        self._sab_seal_chk.stateChanged.connect(self._on_manual_field_debounced)
        _spf.addRow(self._sab_seal_chk)
        scroll_layout.addWidget(_sab_pat_group)
        _sab_pat_group.setVisible(False)

        # ── SAB Allele Data ───────────────────────────────────────────────────────
        _sab_allele_group = QGroupBox("Allele Data  (one per line:  Allele,MFI)")
        self._sab_allele_group = _sab_allele_group
        _saf = QVBoxLayout(); _sab_allele_group.setLayout(_saf)
        _saf.setContentsMargins(4, 2, 4, 2)
        self._sab_allele_edit = QTextEdit()
        self._sab_allele_edit.setPlaceholderText("A*01:01,2126\nA*36:01,992\n...")
        self._sab_allele_edit.setFixedHeight(120)
        self._sab_allele_edit.textChanged.connect(self._on_manual_field_debounced)
        _saf.addWidget(self._sab_allele_edit)

        _sab_chart_row = QHBoxLayout()
        self._sab_chart_lbl = QLabel("No chart selected"); self._sab_chart_lbl.setStyleSheet("color:gray;font-style:italic;")
        _sab_chart_btn = QPushButton("Upload Chart Image"); _sab_chart_btn.setMaximumHeight(26)
        self._sab_chart_bytes = None
        def _upload_sab_chart():
            path, _ = QFileDialog.getOpenFileName(self, "Select Chart Image", str(Path.home()), "Images (*.png *.jpg *.jpeg *.bmp *.tiff)")
            if path:
                with open(path, "rb") as fh: self._sab_chart_bytes = fh.read()
                self._sab_chart_lbl.setText(os.path.basename(path))
                self._on_manual_field_debounced()
        _sab_chart_btn.clicked.connect(_upload_sab_chart)
        _sab_chart_row.addWidget(self._sab_chart_lbl, 1); _sab_chart_row.addWidget(_sab_chart_btn)
        _saf.addLayout(_sab_chart_row)
        scroll_layout.addWidget(_sab_allele_group)
        _sab_allele_group.setVisible(False)

        # ---- Flow Cytometry Patient Information ------------------------------
        self._flow_pat_f = {}
        _flow_pat_group = QGroupBox("Patient Information")
        self._flow_pat_group = _flow_pat_group
        _fpf = QFormLayout(); _flow_pat_group.setLayout(_fpf)
        _fpf.setSpacing(1); _fpf.setContentsMargins(4, 2, 4, 2)
        _FLOW_PAT_FIELDS = [
            ("patient_name",    "Patient Name *",      ""),
            ("gender_age",      "Gender / Age",        ""),
            ("pin",             "PIN",                 ""),
            ("sample_number",   "Sample Number",       ""),
            ("diagnosis",       "Diagnosis",           "NA"),
            ("hospital_clinic", "Hospital / Clinic",   ""),
            ("sample_type",     "Sample Type",         "Serum"),
            ("collection_date", "Collection Date",     ""),
            ("receipt_date",    "Sample Receipt Date", ""),
            ("report_date",     "Report Date",         ""),
            ("remarks",         "Remarks",             ""),
            ("comments",        "Additional Comments", ""),
        ]
        for _k, _l, _dflt in _FLOW_PAT_FIELDS:
            _w = QLineEdit(_dflt); _w.setFixedHeight(24)
            if "date" in _k: _w.setPlaceholderText("DD-MM-YYYY")
            _w.textChanged.connect(self._on_manual_field_debounced)
            self._flow_pat_f[_k] = _w
            _fpf.addRow(_l + ":", _w)
        self._flow_nabl_chk = QCheckBox("NABL Accreditation")
        self._flow_nabl_chk.setChecked(self.qsettings.value("nabl_stamp", True, type=bool))
        self._flow_nabl_chk.stateChanged.connect(self._on_manual_field_debounced)
        _fpf.addRow(self._flow_nabl_chk)
        self._flow_seal_chk = QCheckBox("Signature Seal")
        self._flow_seal_chk.setChecked(self.qsettings.value("sig_stamp", True, type=bool))
        self._flow_seal_chk.stateChanged.connect(self._on_manual_field_debounced)
        _fpf.addRow(self._flow_seal_chk)
        self._flow_photo_bytes = {}
        _fp_row = QHBoxLayout()
        self._flow_pat_photo_lbl = QLabel("No photo selected"); self._flow_pat_photo_lbl.setStyleSheet("color:gray;font-style:italic;")
        _fp_btn = QPushButton("Upload Patient Photo"); _fp_btn.setMaximumHeight(26)
        _fp_btn.clicked.connect(lambda: self._upload_flow_photo("patient"))
        _fp_row.addWidget(self._flow_pat_photo_lbl, 1); _fp_row.addWidget(_fp_btn)
        _fpf.addRow("Patient Photo:", _fp_row)
        scroll_layout.addWidget(_flow_pat_group)
        _flow_pat_group.setVisible(False)

        self._flow_don_f = {}
        _flow_don_group = QGroupBox("Donor Information")
        self._flow_don_group = _flow_don_group
        _fdf = QFormLayout(); _flow_don_group.setLayout(_fdf)
        _fdf.setSpacing(1); _fdf.setContentsMargins(4, 2, 4, 2)
        _FLOW_DON_FIELDS = [
            ("name",            "Donor Name",                ""),
            ("gender_age",      "Gender / Age",              ""),
            ("pin",             "Donor PIN",                 "NA"),
            ("sample_number",   "Sample Number",             "NA"),
            ("relationship",    "Relationship to Recipient", ""),
            ("sample_type",     "Sample Type",               "Sodium Heparin Whole Blood"),
            ("collection_date", "Collection Date",           ""),
            ("receipt_date",    "Sample Receipt Date",       ""),
            ("report_date",     "Report Date",               ""),
        ]
        for _k, _l, _dflt in _FLOW_DON_FIELDS:
            _w = QLineEdit(_dflt); _w.setFixedHeight(24)
            if "date" in _k: _w.setPlaceholderText("DD-MM-YYYY")
            _w.textChanged.connect(self._on_manual_field_debounced)
            self._flow_don_f[_k] = _w
            _fdf.addRow(_l + ":", _w)
        _fd_row = QHBoxLayout()
        self._flow_don_photo_lbl = QLabel("No photo selected"); self._flow_don_photo_lbl.setStyleSheet("color:gray;font-style:italic;")
        _fd_btn = QPushButton("Upload Donor Photo"); _fd_btn.setMaximumHeight(26)
        _fd_btn.clicked.connect(lambda: self._upload_flow_photo("donor"))
        _fd_row.addWidget(self._flow_don_photo_lbl, 1); _fd_row.addWidget(_fd_btn)
        _fdf.addRow("Donor Photo:", _fd_row)
        scroll_layout.addWidget(_flow_don_group)
        _flow_don_group.setVisible(False)

        self._flow_result_f = {}
        _flow_res_group = QGroupBox("Flow Results")
        self._flow_res_group = _flow_res_group
        _frf = QFormLayout(); _flow_res_group.setLayout(_frf)
        _frf.setSpacing(1); _frf.setContentsMargins(4, 2, 4, 2)
        _FLOW_INTERP_OPTS = ["Negative", "Borderline", "Positive"]
        for _k, _l in [("t_interpretation", "T Cell Crossmatch"), ("b_interpretation", "B Cell Crossmatch")]:
            _cmb = ClickOnlyComboBox(); _cmb.addItems(_FLOW_INTERP_OPTS); _cmb.setFixedHeight(24)
            self._flow_result_f[_k] = _cmb
            _frf.addRow(_l + ":", _cmb)
            _cmb.currentIndexChanged.connect(self._on_manual_field_debounced)
        for _k, _l, _ph in [("t_mcs", "T Cell MCS Value", "<45"), ("b_mcs", "B Cell MCS Value", "<86")]:
            _w = QLineEdit(); _w.setFixedHeight(24); _w.setPlaceholderText(_ph)
            self._flow_result_f[_k] = _w
            _frf.addRow(_l + ":", _w)
            _w.textChanged.connect(self._on_manual_field_debounced)
        _interp_w = QLineEdit()
        _interp_w.setFixedHeight(24)
        _interp_w.setPlaceholderText("Auto-generated if left blank")
        self._flow_result_f["interpretation"] = _interp_w
        _frf.addRow("Interpretation Override:", _interp_w)
        _interp_w.textChanged.connect(self._on_manual_field_debounced)
        scroll_layout.addWidget(_flow_res_group)
        _flow_res_group.setVisible(False)

        # ── Luminex Patient Information ───────────────────────────────────────
        self._lx_pat_f = {}
        self._lx_pat_photo_bytes = None
        _lx_pat_group = QGroupBox("Patient Information")
        self._lx_pat_group = _lx_pat_group
        _lpf = QFormLayout(); _lx_pat_group.setLayout(_lpf)
        _lpf.setSpacing(1); _lpf.setContentsMargins(4, 2, 4, 2)
        _LX_PAT_FIELDS = [
            ("patient_name",    "Patient Name *",         ""),
            ("gender_age",      "Gender / Age",           ""),
            ("pin",             "PIN *",                  ""),
            ("sample_number",   "Sample Number",          ""),
            ("diagnosis",       "Diagnosis",              "NA"),
            ("hospital_clinic", "Hospital / Clinic",      ""),
            ("receipt_date",    "Sample Receipt Date",    ""),
            ("report_date",     "Report Date",            ""),
            ("relation",        "Relation",               "Patient"),
            ("sample_type",     "Sample Type",            "EDTA Blood"),
            ("collection_date", "Date of Collection",     ""),
        ]
        for _k, _l, _d in _LX_PAT_FIELDS:
            _w = QLineEdit(_d); _w.setMaximumHeight(24)
            if "date" in _k: _w.setPlaceholderText("DD-MM-YYYY")
            self._lx_pat_f[_k] = _w
            _lpf.addRow(_l + ":", _w)
            _w.textChanged.connect(self._on_manual_field_debounced)
        _lx_pp_row = QHBoxLayout()
        self._lx_pat_photo_lbl = QLabel("No photo selected")
        self._lx_pat_photo_lbl.setStyleSheet("color:gray;font-style:italic;")
        _lx_pp_btn = QPushButton("Upload Patient Photo"); _lx_pp_btn.setMaximumHeight(26)
        def _upload_lx_pat_photo():
            path, _ = QFileDialog.getOpenFileName(self, "Select Patient Photo",
                str(Path.home()), "Images (*.png *.jpg *.jpeg *.bmp *.tiff)")
            if path:
                with open(path, "rb") as fh: self._lx_pat_photo_bytes = fh.read()
                self._lx_pat_photo_lbl.setText(os.path.basename(path))
                self._on_manual_field_debounced()
        _lx_pp_btn.clicked.connect(_upload_lx_pat_photo)
        _lx_pp_row.addWidget(self._lx_pat_photo_lbl, 1); _lx_pp_row.addWidget(_lx_pp_btn)
        _lpf.addRow("Patient Photo:", _lx_pp_row)
        self._lx_nabl_chk = QCheckBox("NABL Accreditation")
        self._lx_nabl_chk.setChecked(self.qsettings.value("nabl_stamp", True, type=bool))
        self._lx_nabl_chk.stateChanged.connect(self._on_manual_field_debounced)
        self._lx_seal_chk = QCheckBox("Signature Seal")
        self._lx_seal_chk.setChecked(self.qsettings.value("sig_stamp", True, type=bool))
        self._lx_seal_chk.stateChanged.connect(self._on_manual_field_debounced)
        _lpf.addRow(self._lx_nabl_chk)
        _lpf.addRow(self._lx_seal_chk)
        scroll_layout.addWidget(_lx_pat_group)
        _lx_pat_group.setVisible(False)

        # ── Luminex Donor Information ─────────────────────────────────────────
        self._lx_don_f = {}
        self._lx_don_photo_bytes = None
        _lx_don_group = QGroupBox("Donor Information")
        self._lx_don_group = _lx_don_group
        _ldf = QFormLayout(); _lx_don_group.setLayout(_ldf)
        _ldf.setSpacing(1); _ldf.setContentsMargins(4, 2, 4, 2)
        _LX_DON_FIELDS = [
            ("name",            "Donor Name *",           ""),
            ("gender_age",      "Gender / Age",           ""),
            ("pin",             "PIN",                    ""),
            ("sample_number",   "Sample Number",          ""),
            ("relation",        "Relation",               ""),
            ("sample_type",     "Sample Type",            "EDTA Blood"),
            ("collection_date", "Date of Collection",     ""),
        ]
        for _k, _l, _d in _LX_DON_FIELDS:
            _w = QLineEdit(_d); _w.setMaximumHeight(24)
            if "date" in _k: _w.setPlaceholderText("DD-MM-YYYY")
            self._lx_don_f[_k] = _w
            _ldf.addRow(_l + ":", _w)
            _w.textChanged.connect(self._on_manual_field_debounced)
        _lx_dp_row = QHBoxLayout()
        self._lx_don_photo_lbl = QLabel("No photo selected")
        self._lx_don_photo_lbl.setStyleSheet("color:gray;font-style:italic;")
        _lx_dp_btn = QPushButton("Upload Donor Photo"); _lx_dp_btn.setMaximumHeight(26)
        def _upload_lx_don_photo():
            path, _ = QFileDialog.getOpenFileName(self, "Select Donor Photo",
                str(Path.home()), "Images (*.png *.jpg *.jpeg *.bmp *.tiff)")
            if path:
                with open(path, "rb") as fh: self._lx_don_photo_bytes = fh.read()
                self._lx_don_photo_lbl.setText(os.path.basename(path))
                self._on_manual_field_debounced()
        _lx_dp_btn.clicked.connect(_upload_lx_don_photo)
        _lx_dp_row.addWidget(self._lx_don_photo_lbl, 1); _lx_dp_row.addWidget(_lx_dp_btn)
        _ldf.addRow("Donor Photo:", _lx_dp_row)
        scroll_layout.addWidget(_lx_don_group)
        _lx_don_group.setVisible(False)

        # ── Luminex HLA Alleles ───────────────────────────────────────────────
        _lx_hla_group = QGroupBox("HLA Alleles  (Patient left · Donor right per locus)")
        self._lx_hla_group = _lx_hla_group
        _lhf = QFormLayout(); _lx_hla_group.setLayout(_lhf)
        _lhf.setSpacing(1); _lhf.setContentsMargins(4, 2, 4, 2)
        self._lx_pat_hla = {}  # {locus: [w1, w2]}
        self._lx_don_hla = {}
        for _locus in HLA_LOCI:
            _row_w = QWidget(); _row_l = QHBoxLayout(_row_w)
            _row_l.setContentsMargins(0,0,0,0); _row_l.setSpacing(4)
            _pa1 = QLineEdit(); _pa1.setFixedWidth(72); _pa1.setFixedHeight(22)
            _pa2 = QLineEdit(); _pa2.setFixedWidth(72); _pa2.setFixedHeight(22)
            _sep = QLabel("|"); _sep.setStyleSheet("color:gray;")
            _da1 = QLineEdit(); _da1.setFixedWidth(72); _da1.setFixedHeight(22)
            _da2 = QLineEdit(); _da2.setFixedWidth(72); _da2.setFixedHeight(22)
            _row_l.addWidget(_pa1); _row_l.addWidget(_pa2)
            _row_l.addWidget(_sep)
            _row_l.addWidget(_da1); _row_l.addWidget(_da2); _row_l.addStretch()
            for _ew in (_pa1, _pa2, _da1, _da2):
                _ew.textChanged.connect(self._on_manual_field_debounced)
            self._lx_pat_hla[_locus] = [_pa1, _pa2]
            self._lx_don_hla[_locus] = [_da1, _da2]
            _lhf.addRow(f"{_locus}:", _row_w)
        scroll_layout.addWidget(_lx_hla_group)
        _lx_hla_group.setVisible(False)

        # ── Luminex Interpretation ────────────────────────────────────────────
        _lx_interp_group = QGroupBox("Interpretation")
        self._lx_interp_group = _lx_interp_group
        _lif = QVBoxLayout(); _lx_interp_group.setLayout(_lif)
        _lif.setContentsMargins(4, 2, 4, 2)
        self._lx_interp_edit = QTextEdit()
        self._lx_interp_edit.setPlaceholderText(
            "e.g. The HLA typing shows 5/10 match with the donor.")
        self._lx_interp_edit.setFixedHeight(72)
        self._lx_interp_edit.textChanged.connect(self._on_manual_field_debounced)
        _lif.addWidget(self._lx_interp_edit)
        scroll_layout.addWidget(_lx_interp_group)
        _lx_interp_group.setVisible(False)

        # ── Patient HLA Results ────────────────────────────────────────────────
        hla_group = QGroupBox("HLA Results — Patient")
        self._std_hla_group = hla_group   # ref for show/hide
        hla_form  = QFormLayout(); hla_group.setLayout(hla_form)
        hla_form.setSpacing(1); hla_form.setContentsMargins(4, 2, 4, 2)
        scroll_layout.addWidget(hla_group)
        self.hla_pat = {}
        for locus in HLA_LOCI:
            row_w, a1, a2 = _make_allele_row()
            hla_form.addRow(f"{locus}:", row_w)
            self.hla_pat[locus] = [a1, a2]
            a1.textChanged.connect(self._on_manual_field_debounced)
            a2.textChanged.connect(self._on_manual_field_debounced)

        # ── Donors section — supports multiple donors ──────────────────────
        self._manual_donors  = []   # list of {container, fields, hla}
        self._loading_draft  = False
        donors_outer = QGroupBox("Donors (Optional)")
        donors_outer_layout = QVBoxLayout()
        donors_outer_layout.setSpacing(1)
        donors_outer_layout.setContentsMargins(4, 2, 4, 2)
        donors_outer.setLayout(donors_outer_layout)

        add_donor_btn = QPushButton("+ Add Donor")
        add_donor_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder))
        add_donor_btn.setMaximumHeight(26)
        add_donor_btn.clicked.connect(self._add_manual_donor)
        donors_outer_layout.addWidget(add_donor_btn)

        self._donors_list_layout = QVBoxLayout()
        self._donors_list_layout.setSpacing(2)
        donors_outer_layout.addLayout(self._donors_list_layout)

        self._std_donors_outer = donors_outer   # ref for show/hide
        scroll_layout.addWidget(donors_outer)

        # ── Signature Override — select from configured signatories ───────────
        self._manual_sig_name_overrides = {}   # {slot_idx: sig_name_string}
        self._manual_sig_combos         = {}   # {slot_idx: QComboBox}

        sig_group = QGroupBox("Signature Override")
        sig_form  = QFormLayout()
        sig_form.setSpacing(1)
        sig_form.setContentsMargins(4, 2, 4, 2)
        sig_group.setLayout(sig_form)

        _sig_options = ["(Use Default)"] + list(hla_assets.SIGN_BY_NAME.keys())
        for i in range(3):
            cmb = ClickOnlyComboBox()
            cmb.addItems(_sig_options)
            cmb.setFixedHeight(24)
            cmb.currentTextChanged.connect(
                lambda text, slot=i: self._on_manual_sig_changed(slot, text))
            self._manual_sig_combos[i] = cmb
            sig_form.addRow(f"Signatory {i+1}:", cmb)

        scroll_layout.addWidget(sig_group)

        # Draft buttons — under form (same position as PGTA)
        draft_layout = QHBoxLayout()
        btn_save_draft = QPushButton("Save Draft")
        btn_save_draft.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
        btn_save_draft.clicked.connect(self.save_manual_draft)
        btn_load_draft = QPushButton("Load Draft")
        btn_load_draft.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton))
        btn_load_draft.clicked.connect(self.load_manual_draft)
        btn_clear = QPushButton("Clear Form")
        btn_clear.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
        btn_clear.clicked.connect(self._clear_manual_form)
        draft_layout.addWidget(btn_save_draft)
        draft_layout.addWidget(btn_load_draft)
        draft_layout.addWidget(btn_clear)
        draft_layout.addStretch()
        left_layout.addLayout(draft_layout)

        # Fast Report Generation group — mirrors PGTA's gen_group
        gen_group  = QGroupBox("Fast Report Generation")
        gen_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        gen_layout = QVBoxLayout()
        gen_layout.setContentsMargins(4, 4, 4, 4)
        gen_layout.setSpacing(2)
        gen_group.setLayout(gen_layout)

        out_row = QHBoxLayout()
        self.manual_output_label = QLabel("No directory selected")
        self.manual_output_label.setStyleSheet(PATH_LABEL_STYLE)
        browse_out_btn = QPushButton("Select Output Folder")
        browse_out_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton))
        browse_out_btn.clicked.connect(self.browse_manual_output)
        out_row.addWidget(self.manual_output_label, 1)
        out_row.addWidget(browse_out_btn)
        gen_layout.addLayout(out_row)

        action_row = QHBoxLayout()
        self.manual_generate_btn = QPushButton("Generate Report")
        self.manual_generate_btn.setStyleSheet(GENERATE_BTN_STYLE)
        self.manual_generate_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.manual_generate_btn.clicked.connect(self.generate_manual)
        action_row.addStretch()
        action_row.addWidget(self.manual_generate_btn)
        gen_layout.addLayout(action_row)

        self.manual_status_label = QLabel("")
        self.manual_status_label.setStyleSheet(STATUS_LABEL_STYLE)
        gen_layout.addWidget(self.manual_status_label)
        left_layout.addWidget(gen_group)

        # ── Right: PDF preview ───────────────────────────────────────────────
        right_widget = QGroupBox("Report Preview")
        right_widget.setMinimumWidth(600)
        right_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        right_layout = QVBoxLayout()
        right_layout.setContentsMargins(2, 4, 2, 2)
        right_layout.setSpacing(2)
        right_widget.setLayout(right_layout)

        prev_top = QHBoxLayout()
        self._manual_preview_status = QLabel("Fill in the form to preview")
        self._manual_preview_status.setStyleSheet("color:gray; font-style:italic;")
        self._manual_preview_status.setWordWrap(True)
        prev_top.addWidget(self._manual_preview_status, 1)
        right_layout.addLayout(prev_top)

        if QTPDF_OK:
            self._manual_pdf_doc  = QPdfDocument(self)
            self._manual_pdf_view = QPdfView(self)
            self._manual_pdf_view.setDocument(self._manual_pdf_doc)
            self._manual_pdf_view.setPageMode(QPdfView.PageMode.MultiPage)
            right_layout.addWidget(self._manual_pdf_view, 1)
        else:
            self._manual_pdf_doc  = None
            self._manual_pdf_view = None
            prev_scroll = QScrollArea()
            prev_scroll.setWidgetResizable(True)
            self._manual_preview_inner = QWidget()
            self._manual_preview_vbox  = QVBoxLayout(self._manual_preview_inner)
            self._manual_preview_vbox.setAlignment(
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
            prev_scroll.setWidget(self._manual_preview_inner)
            right_layout.addWidget(prev_scroll, 1)

        refresh_btn = QPushButton("Refresh Preview")
        refresh_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        refresh_btn.clicked.connect(self._refresh_manual_preview)
        right_layout.addWidget(refresh_btn)

        self._manual_preview_worker = None

        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        self._update_manual_rpl_visibility()
        return tab

    def browse_manual_output(self):
        start = self.qsettings.value("last_output_dir", str(Path.home()))
        path  = QFileDialog.getExistingDirectory(self, "Select Output Directory", start)
        if path:
            self.manual_output_label.setText(path)
            self.bulk_output_label.setText(path)   # sync with bulk tab
            self.qsettings.setValue("last_output_dir", path)

    @staticmethod
    def _parse_sab_allele_text_static(text: str) -> list:
        """Parse allele text (allele,mfi per line) into [(allele, mfi_int), ...] desc."""
        import re as _re2
        result = []
        for line in text.strip().splitlines():
            line = line.strip()
            if not line: continue
            parts = _re2.split(r"[,\t]", line)
            if len(parts) >= 2:
                allele = parts[0].strip()
                try:
                    mfi = int(float(parts[1].strip()))
                    result.append((allele, mfi))
                except ValueError:
                    continue
        return sorted(result, key=lambda x: -x[1])

    def _collect_manual_case(self) -> dict:
        """Build a case dict from the current Manual tab form state + current settings."""
        with_logo = self.logo_combo.currentText() == "With Logo"
        rtype     = TEMPLATE_TO_RTYPE.get(
            self._manual_rtype_combo.currentText()
            if hasattr(self, "_manual_rtype_combo") else self.template_combo.currentText(),
            "single_hla")
        nabl      = self._manual_nabl_chk.isChecked()
        sig_stamp = self._manual_seal_chk.isChecked()

        patient = {k: w.text().strip() for k, w in self.f.items()}
        patient["name"] = patient.get("patient_name", "")
        patient["hla"] = {
            locus: [a[0].text().strip(), a[1].text().strip()]
            for locus, a in self.hla_pat.items()
            if a[0].text().strip() or a[1].text().strip()
        }

        donors = []
        for entry in self._manual_donors:
            d = {k: w.text().strip() for k, w in entry["fields"].items()}
            donor_hla = {
                locus: [a[0].text().strip(), a[1].text().strip()]
                for locus, a in entry["hla"].items()
                if a[0].text().strip() or a[1].text().strip()
            }
            donors.append({
                "name":            d.get("name", ""),
                "relationship":    d.get("relationship", ""),
                "gender_age":      d.get("gender_age", ""),
                "hospital_mr_no":  d.get("hospital_mr_no", "NA") or "NA",
                "diagnosis":       d.get("diagnosis", "") or patient.get("diagnosis", ""),
                "referred_by":     d.get("referred_by", "") or patient.get("referred_by", ""),
                "pin":             d.get("pin", ""),
                "sample_number":   d.get("sample_number", ""),
                "collection_date": d.get("collection_date", ""),
                "receipt_date":    d.get("receipt_date", ""),
                "report_date":     d.get("report_date", "") or patient.get("report_date", ""),
                "match":           d.get("match", ""),
                "hla": donor_hla, "hla_c_type": "", "remarks": d.get("remarks", ""),
                "hospital_clinic": d.get("hospital_clinic", "") or patient.get("hospital_clinic", ""),
                "specimen":        patient.get("specimen", "Blood - EDTA"),
            })

        case = self._build_case(rtype, nabl, with_logo, sig_stamp, patient, donors)

        for key, w in self._manual_report_settings.items():
            case[key] = w.text().strip()

        # RPL reference: use form values, then auto-recalculate from HLA if donor present
        if rtype == "rpl_couple":
            ref = {k: w.text().strip() for k, w in self._manual_rpl_fields.items()}
            if donors:
                pc = patient["hla"].get("C", [None, None])
                ct1 = c_supertype(pc[0]) if pc[0] else None
                ct2 = c_supertype(pc[1]) if pc[1] else None
                new_pc = ",".join(filter(None, [ct1, ct2]))
                dc = donors[0]["hla"].get("C", [None, None])
                dt1 = c_supertype(dc[0]) if dc[0] else None
                dt2 = c_supertype(dc[1]) if dc[1] else None
                new_dc = ",".join(filter(None, [dt1, dt2]))
                calc = compute_rpl_reference(patient, donors[0])
                calc["hla_c_patient"] = new_pc
                calc["hla_c_donor"]   = new_dc
                for k, v in calc.items():
                    if k in ("match_str", "match_pct") and donors[0].get("match", "").strip():
                        # Always sync match from donor's Match Score field; update widget too
                        ref[k] = v
                        w = self._manual_rpl_fields.get(k)
                        if w and w.text().strip() != v:
                            w.blockSignals(True); w.setText(v); w.blockSignals(False)
                    elif k in ref and not ref[k]:
                        ref[k] = v
                # Always push HLA-C supertypes back to UI
                for ui_key, val in (("hla_c_patient", new_pc), ("hla_c_donor", new_dc)):
                    w = self._manual_rpl_fields.get(ui_key)
                    if w and w.text().strip() != val:
                        w.blockSignals(True); w.setText(val); w.blockSignals(False)
                        ref[ui_key] = val
                patient["hla_c_type"]    = new_pc
                donors[0]["hla_c_type"]  = new_dc
            case["rpl_reference"] = ref

        # Attach CDC-specific fields when applicable
        if rtype == "cdc_crossmatch":
            pf = getattr(self, "_cdc_pat_f",  {})
            df = getattr(self, "_cdc_don_f",  {})
            cf = getattr(self, "_manual_cdc_fields", {})
            photos = getattr(self, "_manual_photo_bytes", {})

            def _tv(d, k, default=""): return d[k].text().strip() if k in d else default

            patient = {
                "name":            _tv(pf, "patient_name"),
                "gender_age":      _tv(pf, "gender_age"),
                "pin":             _tv(pf, "pin"),
                "sample_number":   _tv(pf, "sample_number"),
                "diagnosis":       _tv(pf, "diagnosis") or "NA",
                "hospital_clinic": _tv(pf, "hospital_clinic"),
                "sample_type":     _tv(pf, "sample_type") or "Serum",
                "collection_date": _tv(pf, "collection_date"),
                "receipt_date":    _tv(pf, "receipt_date"),
                "report_date":     _tv(pf, "report_date"),
                "remarks":         _tv(pf, "remarks"),
                "comments":        _tv(pf, "comments"),
                "photo_bytes":     photos.get("patient"),
                "hla": {}, "hla_c_type": "",
                "_join_key": _tv(pf, "pin"),
                "_has_insufficient_hla": False,
            }
            cdc_donor = {
                "name":            _tv(df, "name"),
                "gender_age":      _tv(df, "gender_age"),
                "pin":             _tv(df, "pin") or "NA",
                "sample_number":   _tv(df, "sample_number") or "NA",
                "relationship":    _tv(df, "relationship"),
                "sample_type":     _tv(df, "sample_type") or "Sodium Heparin Whole Blood",
                "collection_date": _tv(df, "collection_date"),
                "receipt_date":    _tv(df, "receipt_date"),
                "report_date":     _tv(df, "report_date"),
                "photo_bytes":     photos.get("donor"),
                "hla": {}, "hla_c_type": "",
                "_join_key": "", "_has_insufficient_hla": False,
            }
            nabl      = self._cdc_nabl_chk.isChecked() if hasattr(self, "_cdc_nabl_chk") else nabl
            sig_stamp = self._cdc_seal_chk.isChecked() if hasattr(self, "_cdc_seal_chk") else sig_stamp
            case = self._build_case(rtype, nabl, with_logo, sig_stamp, patient, [cdc_donor])
            case["cdc_results"] = {k: w.currentText() for k, w in cf.items()}

        # Attach DSA-specific fields when applicable
        if rtype == "dsa_crossmatch":
            pf     = getattr(self, "_dsa_pat_f",    {})
            df     = getattr(self, "_dsa_don_f",    {})
            rf     = getattr(self, "_dsa_result_f", {})
            photos = getattr(self, "_dsa_photo_bytes", {})

            def _tv(d, k, default=""): return d[k].text().strip() if k in d else default

            patient = {
                "name":            _tv(pf, "patient_name"),
                "gender_age":      _tv(pf, "gender_age"),
                "pin":             _tv(pf, "pin"),
                "sample_number":   _tv(pf, "sample_number"),
                "diagnosis":       _tv(pf, "diagnosis") or "NA",
                "hospital_clinic": _tv(pf, "hospital_clinic"),
                "sample_type":     _tv(pf, "sample_type") or "Serum",
                "collection_date": _tv(pf, "collection_date"),
                "receipt_date":    _tv(pf, "receipt_date"),
                "report_date":     _tv(pf, "report_date"),
                "remarks":         _tv(pf, "remarks"),
                "comments":        _tv(pf, "comments"),
                "photo_bytes":     photos.get("patient"),
                "hla": {}, "hla_c_type": "",
                "_join_key": _tv(pf, "pin"),
                "_has_insufficient_hla": False,
            }
            dsa_donor = {
                "name":            _tv(df, "name"),
                "gender_age":      _tv(df, "gender_age"),
                "pin":             _tv(df, "pin") or "NA",
                "sample_number":   _tv(df, "sample_number") or "NA",
                "relationship":    _tv(df, "relationship"),
                "sample_type":     _tv(df, "sample_type") or "ACD Tube",
                "collection_date": _tv(df, "collection_date"),
                "receipt_date":    _tv(df, "receipt_date"),
                "report_date":     _tv(df, "report_date"),
                "photo_bytes":     photos.get("donor"),
                "hla": {}, "hla_c_type": "",
                "_join_key": "", "_has_insufficient_hla": False,
            }
            nabl      = self._dsa_nabl_chk.isChecked() if hasattr(self, "_dsa_nabl_chk") else nabl
            sig_stamp = self._dsa_seal_chk.isChecked() if hasattr(self, "_dsa_seal_chk") else sig_stamp
            case = self._build_case(rtype, nabl, with_logo, sig_stamp, patient, [dsa_donor])
            case["dsa_results"] = {
                "class1_result": rf["class1_result"].currentText() if "class1_result" in rf else "Negative",
                "class1_mfi":    rf["class1_mfi"].text().strip()    if "class1_mfi"    in rf else "",
                "class1_cutoff": rf["class1_cutoff"].text().strip() if "class1_cutoff" in rf else ">1000",
                "class2_result": rf["class2_result"].currentText() if "class2_result" in rf else "Negative",
                "class2_mfi":    rf["class2_mfi"].text().strip()    if "class2_mfi"    in rf else "",
                "class2_cutoff": rf["class2_cutoff"].text().strip() if "class2_cutoff" in rf else ">1000",
            }

        # Attach SAB-specific fields
        if rtype in ("sab_class1", "sab_class2"):
            pf = getattr(self, "_sab_pat_f", {})
            def _tv(d, k, default=""): return d[k].text().strip() if k in d else default
            patient = {
                "name":            _tv(pf, "patient_name"),
                "gender_age":      _tv(pf, "gender_age"),
                "hospital_mr_no":  _tv(pf, "hospital_mr_no") or "NA",
                "specimen":        _tv(pf, "specimen") or "Serum",
                "hospital_clinic": _tv(pf, "hospital_clinic"),
                "pin":             _tv(pf, "pin"),
                "sample_number":   _tv(pf, "sample_number"),
                "collection_date": _tv(pf, "collection_date"),
                "receipt_date":    _tv(pf, "receipt_date"),
                "report_date":     _tv(pf, "report_date"),
                "remarks":         _tv(pf, "remarks"),
                "hla": {}, "hla_c_type": "", "_join_key": _tv(pf, "pin"),
                "_has_insufficient_hla": False,
            }
            nabl      = self._sab_nabl_chk.isChecked() if hasattr(self, "_sab_nabl_chk") else nabl
            sig_stamp = self._sab_seal_chk.isChecked() if hasattr(self, "_sab_seal_chk") else sig_stamp
            _raw_alleles = getattr(self, "_sab_allele_edit", None)
            _allele_text = _raw_alleles.toPlainText() if _raw_alleles else ""
            _alleles = self._parse_sab_allele_text_static(_allele_text)
            _sab_class = getattr(self, "_sab_class_combo", None)
            _class = _sab_class.currentText() if _sab_class else "I"
            case = self._build_case(rtype, nabl, with_logo, sig_stamp, patient, [])
            case["sab_alleles"]    = _alleles
            case["sab_chart_bytes"] = getattr(self, "_sab_chart_bytes", None)
            case["sab_class"]      = _class

        # Attach Flow-specific fields
        if rtype == "flow_crossmatch":
            pf = getattr(self, "_flow_pat_f", {})
            df = getattr(self, "_flow_don_f", {})
            rf = getattr(self, "_flow_result_f", {})
            def _tv(d, k, default=""): return d[k].text().strip() if k in d and hasattr(d[k], "text") else (d[k].currentText() if k in d else default)
            patient = {
                "name":            _tv(pf, "patient_name"),
                "gender_age":      _tv(pf, "gender_age"),
                "pin":             _tv(pf, "pin"),
                "sample_number":   _tv(pf, "sample_number"),
                "diagnosis":       _tv(pf, "diagnosis") or "NA",
                "hospital_clinic": _tv(pf, "hospital_clinic"),
                "sample_type":     _tv(pf, "sample_type") or "Serum",
                "collection_date": _tv(pf, "collection_date"),
                "receipt_date":    _tv(pf, "receipt_date"),
                "report_date":     _tv(pf, "report_date"),
                "remarks":         _tv(pf, "remarks"),
                "comments":        _tv(pf, "comments"),
                "photo_bytes":     getattr(self, "_flow_photo_bytes", {}).get("patient"),
                "hla": {}, "hla_c_type": "", "_join_key": _tv(pf, "pin"),
                "_has_insufficient_hla": False,
            }
            flow_donor = {
                "name":            _tv(df, "name"),
                "gender_age":      _tv(df, "gender_age"),
                "pin":             _tv(df, "pin") or "NA",
                "sample_number":   _tv(df, "sample_number") or "NA",
                "relationship":    _tv(df, "relationship"),
                "sample_type":     _tv(df, "sample_type") or "Sodium Heparin Whole Blood",
                "collection_date": _tv(df, "collection_date"),
                "receipt_date":    _tv(df, "receipt_date"),
                "report_date":     _tv(df, "report_date"),
                "photo_bytes": getattr(self, "_flow_photo_bytes", {}).get("donor"), "hla": {}, "hla_c_type": "",
                "_join_key": "", "_has_insufficient_hla": False,
            }
            nabl      = self._flow_nabl_chk.isChecked() if hasattr(self, "_flow_nabl_chk") else nabl
            sig_stamp = self._flow_seal_chk.isChecked() if hasattr(self, "_flow_seal_chk") else sig_stamp
            case = self._build_case(rtype, nabl, with_logo, sig_stamp, patient, [flow_donor])
            case["flow_results"] = {
                "t_interpretation": rf["t_interpretation"].currentText() if "t_interpretation" in rf else "Negative",
                "t_mcs":            rf["t_mcs"].text().strip()            if "t_mcs"            in rf else "<45",
                "b_interpretation": rf["b_interpretation"].currentText() if "b_interpretation" in rf else "Negative",
                "b_mcs":            rf["b_mcs"].text().strip()            if "b_mcs"            in rf else "<86",
                "interpretation":   rf["interpretation"].text().strip()   if "interpretation"   in rf else "",
            }

        # Attach Luminex-specific fields
        if rtype == "luminex_typing":
            pf = getattr(self, "_lx_pat_f", {})
            df = getattr(self, "_lx_don_f", {})
            def _tv(d, k, default=""): return d[k].text().strip() if k in d else default
            patient = {
                "name":            _tv(pf, "patient_name"),
                "gender_age":      _tv(pf, "gender_age"),
                "pin":             _tv(pf, "pin"),
                "sample_number":   _tv(pf, "sample_number"),
                "diagnosis":       _tv(pf, "diagnosis") or "NA",
                "hospital_clinic": _tv(pf, "hospital_clinic"),
                "receipt_date":    _tv(pf, "receipt_date"),
                "report_date":     _tv(pf, "report_date"),
                "relation":        _tv(pf, "relation") or "Patient",
                "sample_type":     _tv(pf, "sample_type") or "EDTA Blood",
                "collection_date": _tv(pf, "collection_date"),
                "hla": {locus: [w.text().strip() for w in ws]
                        for locus, ws in getattr(self, "_lx_pat_hla", {}).items()},
                "hla_c_type": "", "_join_key": _tv(pf, "pin"),
                "_has_insufficient_hla": False,
            }
            lx_donor = {
                "name":            _tv(df, "name"),
                "gender_age":      _tv(df, "gender_age"),
                "pin":             _tv(df, "pin"),
                "sample_number":   _tv(df, "sample_number"),
                "relation":        _tv(df, "relation"),
                "sample_type":     _tv(df, "sample_type") or "EDTA Blood",
                "collection_date": _tv(df, "collection_date"),
                "hla": {locus: [w.text().strip() for w in ws]
                        for locus, ws in getattr(self, "_lx_don_hla", {}).items()},
                "hla_c_type": "", "_join_key": "", "_has_insufficient_hla": False,
            }
            nabl      = self._lx_nabl_chk.isChecked() if hasattr(self, "_lx_nabl_chk") else nabl
            sig_stamp = self._lx_seal_chk.isChecked() if hasattr(self, "_lx_seal_chk") else sig_stamp
            case = self._build_case(rtype, nabl, with_logo, sig_stamp, patient, [lx_donor])
            _lx_ie = getattr(self, "_lx_interp_edit", None)
            case["luminex_interpretation"] = _lx_ie.toPlainText().strip() if _lx_ie else ""
            case["luminex_pat_photo"] = getattr(self, "_lx_pat_photo_bytes", None)
            case["luminex_don_photo"] = getattr(self, "_lx_don_photo_bytes", None)

        self._apply_sig_name_overrides(case, self._manual_sig_name_overrides)
        return case

    def generate_manual(self):
        rtype = TEMPLATE_TO_RTYPE.get(
            self._manual_rtype_combo.currentText() if hasattr(self, "_manual_rtype_combo") else "",
            "single_hla")
        if rtype == "cdc_crossmatch":
            name = self._cdc_pat_f.get("patient_name", QLineEdit()).text().strip() if hasattr(self, "_cdc_pat_f") else ""
            pin  = self._cdc_pat_f.get("pin",           QLineEdit()).text().strip() if hasattr(self, "_cdc_pat_f") else ""
        elif rtype == "dsa_crossmatch":
            name = self._dsa_pat_f.get("patient_name", QLineEdit()).text().strip() if hasattr(self, "_dsa_pat_f") else ""
            pin  = self._dsa_pat_f.get("pin",           QLineEdit()).text().strip() if hasattr(self, "_dsa_pat_f") else ""
        elif rtype in ("sab_class1", "sab_class2"):
            name = self._sab_pat_f.get("patient_name", QLineEdit()).text().strip() if hasattr(self, "_sab_pat_f") else ""
            pin  = self._sab_pat_f.get("pin",           QLineEdit()).text().strip() if hasattr(self, "_sab_pat_f") else ""
        elif rtype == "flow_crossmatch":
            name = self._flow_pat_f.get("patient_name", QLineEdit()).text().strip() if hasattr(self, "_flow_pat_f") else ""
            pin  = self._flow_pat_f.get("pin",           QLineEdit()).text().strip() if hasattr(self, "_flow_pat_f") else ""
        elif rtype == "luminex_typing":
            name = self._lx_pat_f.get("patient_name", QLineEdit()).text().strip() if hasattr(self, "_lx_pat_f") else ""
            pin  = self._lx_pat_f.get("pin",           QLineEdit()).text().strip() if hasattr(self, "_lx_pat_f") else ""
        else:
            name = self.f["patient_name"].text().strip()
            pin  = self.f["pin"].text().strip()
        if not name:
            QMessageBox.warning(self, "Missing Fields", "Patient Name is required.")
            return
        if not pin and rtype not in ("cdc_crossmatch", "dsa_crossmatch", "sab_class1", "sab_class2", "flow_crossmatch", "luminex_typing"):
            QMessageBox.warning(self, "Missing Fields", "PIN is required.")
            return
        out_dir = self.manual_output_label.text()
        if out_dir == "No directory selected":
            QMessageBox.warning(self, "No Output", "Please select an output folder.")
            return

        case = self._collect_manual_case()
        # Fix 5: silently skip cases with Insufficient Data in any HLA value
        # CDC reports do not have HLA data — skip this check for them
        if rtype == "cdc_crossmatch":
            pass  # skip HLA validation for CDC
        elif _has_insufficient_data(case.get("patient", {})):
            return
        fname    = make_filename(case)
        os.makedirs(out_dir, exist_ok=True)
        out_path = unique_output_path(out_dir, fname)
        fname    = os.path.basename(out_path)   # reflect _(2) suffix if added
        try:
            generate_pdf(case, out_path)
            self.manual_status_label.setText(f"✓ Saved: {fname}")
            self.statusBar().showMessage(f"Generated: {fname}")
            self._start_manual_preview(case)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def _start_manual_preview(self, case):
        """Generate preview PDF in background thread — PGTA PreviewWorker pattern."""
        if hasattr(self, '_manual_preview_worker') and self._manual_preview_worker \
                and self._manual_preview_worker.isRunning():
            self._manual_preview_worker.finished.disconnect()
            self._manual_preview_worker.terminate()
        tmp = copy.deepcopy(case)
        self._manual_preview_worker = PreviewWorker(tmp, TEMP_PREVIEW_PATH)
        self._manual_preview_worker.finished.connect(self._on_manual_preview_generated)
        self._manual_preview_worker.error.connect(
            lambda e: print(f"[preview] error: {e}"))
        self._manual_preview_worker.start()

    def _on_manual_preview_generated(self, pdf_path: str):
        if hasattr(self, "_manual_preview_status"):
            self._manual_preview_status.setText("")
        if QTPDF_OK and self._manual_pdf_doc is not None and os.path.exists(pdf_path):
            try:
                self._manual_pdf_doc.close()
                self._manual_pdf_doc.load(pdf_path)
                self._manual_pdf_view.setZoomMode(QPdfView.ZoomMode.FitInView)
            except Exception as e:
                print(f"[preview] load error: {e}")
        elif FITZ_OK and os.path.exists(pdf_path):
            while self._manual_preview_vbox.count():
                child = self._manual_preview_vbox.takeAt(0)
                if child.widget(): child.widget().deleteLater()
            for pm in _render_pdf_pages(pdf_path, width_px=600):
                lbl = QLabel(); lbl.setPixmap(pm)
                lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
                lbl.setStyleSheet("margin: 8px; border: 1px solid #ccc;")
                self._manual_preview_vbox.addWidget(lbl)

    def _refresh_manual_preview(self):
        """Regenerate preview from current form state (picks up latest stamp/logo/template settings)."""
        if self._loading_draft:
            return
        if hasattr(self, "_manual_preview_status"):
            self._manual_preview_status.setText("Generating preview…")
        try:
            case = self._collect_manual_case()
            _rtype_preview = case.get("report_type", "single_hla")
            _NO_HLA_TYPES = ("cdc_crossmatch", "dsa_crossmatch", "flow_crossmatch", "sab_class1", "sab_class2", "luminex_typing")
            if _rtype_preview not in _NO_HLA_TYPES and _has_insufficient_data(case.get("patient", {})):
                return
            self._start_manual_preview(case)
        except Exception:
            if os.path.exists(TEMP_PREVIEW_PATH):
                self._on_manual_preview_generated(TEMP_PREVIEW_PATH)
            if hasattr(self, "_manual_preview_status"):
                self._manual_preview_status.setText("Preview unavailable")

    def _on_manual_field_debounced(self):
        self._manual_edit_timer.start(400)

    def _on_manual_rtype_changed(self):
        """Sync global template_combo when the per-case Report Type combo changes."""
        if hasattr(self, "_manual_rtype_combo"):
            name = self._manual_rtype_combo.currentText()
            idx  = self.template_combo.findText(name)
            if idx >= 0:
                self.template_combo.blockSignals(True)
                self.template_combo.setCurrentIndex(idx)
                self.template_combo.blockSignals(False)
        self._update_manual_rpl_visibility()
        self._on_manual_field_debounced()

    def _update_manual_rpl_visibility(self):
        if not (hasattr(self, "_manual_rpl_group") and hasattr(self, "_manual_rtype_combo")):
            return
        rtype = TEMPLATE_TO_RTYPE.get(self._manual_rtype_combo.currentText(), "single_hla")
        is_cdc = rtype == "cdc_crossmatch"
        is_dsa = rtype == "dsa_crossmatch"
        # Standard form groups — hidden for CDC, DSA, SAB, and Luminex
        is_sab_check  = rtype in ("sab_class1", "sab_class2")
        is_flow_check = rtype == "flow_crossmatch"
        is_lx_check   = rtype == "luminex_typing"
        for _grp in ("_std_pat_group", "_std_hla_group", "_std_donors_outer"):
            if hasattr(self, _grp):
                getattr(self, _grp).setVisible(
                    not is_cdc and not is_dsa and not is_sab_check
                    and not is_flow_check and not is_lx_check)
        # RPL reference — only for rpl_couple, only within standard form
        self._manual_rpl_group.setVisible(rtype == "rpl_couple")
        # CDC form groups — only for CDC
        for _grp in ("_cdc_pat_group", "_cdc_don_group", "_cdc_res_group"):
            if hasattr(self, _grp):
                getattr(self, _grp).setVisible(is_cdc)
        # DSA form groups — only for DSA
        for _grp in ("_dsa_pat_group", "_dsa_don_group", "_dsa_res_group"):
            if hasattr(self, _grp):
                getattr(self, _grp).setVisible(is_dsa)
        # SAB form groups — only for SAB
        is_sab = rtype in ("sab_class1", "sab_class2")
        for _grp in ("_sab_pat_group", "_sab_allele_group"):
            if hasattr(self, _grp):
                getattr(self, _grp).setVisible(is_sab)
        if is_sab and hasattr(self, "_sab_class_combo"):
            _auto_cls = "II" if rtype == "sab_class2" else "I"
            self._sab_class_combo.blockSignals(True)
            self._sab_class_combo.setCurrentText(_auto_cls)
            self._sab_class_combo.blockSignals(False)
        # Flow form groups — only for Flow
        is_flow = rtype == "flow_crossmatch"
        for _grp in ("_flow_pat_group", "_flow_don_group", "_flow_res_group"):
            if hasattr(self, _grp):
                getattr(self, _grp).setVisible(is_flow)
        # Luminex form groups — only for Luminex
        is_lx = rtype == "luminex_typing"
        for _grp in ("_lx_pat_group", "_lx_don_group", "_lx_hla_group", "_lx_interp_group"):
            if hasattr(self, _grp):
                getattr(self, _grp).setVisible(is_lx)

    def _upload_cdc_photo(self, who: str):
        """Open file dialog for CDC patient/donor photo upload."""
        path, _ = QFileDialog.getOpenFileName(
            self, f"Select {who.title()} Photo",
            str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.bmp *.tiff)"
        )
        if not path:
            return
        with open(path, "rb") as fh:
            self._manual_photo_bytes[who] = fh.read()
        fname = os.path.basename(path)
        if who == "patient":
            self._manual_cdc_patient_photo_lbl.setText(fname)
        else:
            self._manual_cdc_donor_photo_lbl.setText(fname)
        self._on_manual_field_debounced()

    def _upload_dsa_photo(self, who: str, label):
        """Open file dialog for DSA patient/donor photo upload."""
        path, _ = QFileDialog.getOpenFileName(
            self, f"Select {who.title()} Photo",
            str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.bmp *.tiff)"
        )
        if not path:
            return
        with open(path, "rb") as fh:
            self._dsa_photo_bytes[who] = fh.read()
        label.setText(os.path.basename(path))
        self._on_manual_field_debounced()

    def _auto_detect_manual_template(self):
        """Intelligently update the Template combo based on donor+relationship+diagnosis."""
        has_donors = bool(self._manual_donors)
        diagnosis  = self.f.get("diagnosis", QLineEdit()).text().strip().upper()

        # Collect all donor relationships
        relationships = [
            entry["fields"].get("relationship", QLineEdit()).text().strip().lower()
            for entry in self._manual_donors
        ]

        if not has_donors:
            rtype = "single_hla"
        elif any(k in diagnosis for k in ("RPL", "RECURRENT", "MISCARRIAGE", "RIF")):
            rtype = "rpl_couple"
        elif any(r in ("wife", "husband", "spouse", "partner") for r in relationships):
            rtype = "rpl_couple"
        else:
            rtype = "transplant_donor"

        name = RTYPE_TO_TEMPLATE.get(rtype, TEMPLATE_NAMES[0])
        for combo in (self.template_combo,
                      getattr(self, "_manual_rtype_combo", None)):
            if combo is None: continue
            idx = combo.findText(name)
            if idx >= 0:
                combo.blockSignals(True)
                combo.setCurrentIndex(idx)
                combo.blockSignals(False)
        self._update_manual_rpl_visibility()

    def _clear_manual_form(self):
        # Standard form fields
        for w in self.f.values(): w.clear()
        for locus in HLA_LOCI:
            self.hla_pat[locus][0].clear(); self.hla_pat[locus][1].clear()
        for w in self._manual_report_settings.values(): w.clear()
        self._manual_report_settings.get("typing_status", QLineEdit()).setText("Complete")
        for w in self._manual_rpl_fields.values(): w.clear()
        for entry in list(self._manual_donors):
            entry["container"].deleteLater()
        self._manual_donors.clear()
        # CDC form fields
        for d in (getattr(self, "_cdc_pat_f", {}), getattr(self, "_cdc_don_f", {})):
            for w in d.values():
                if isinstance(w, QLineEdit): w.clear()
        for w in getattr(self, "_manual_cdc_fields", {}).values():
            if isinstance(w, QComboBox): w.setCurrentIndex(0)
        getattr(self, "_manual_photo_bytes", {}).clear()
        if hasattr(self, "_manual_cdc_patient_photo_lbl"):
            self._manual_cdc_patient_photo_lbl.setText("No photo selected")
        if hasattr(self, "_manual_cdc_donor_photo_lbl"):
            self._manual_cdc_donor_photo_lbl.setText("No photo selected")
        # DSA form fields
        for d in (getattr(self, "_dsa_pat_f", {}), getattr(self, "_dsa_don_f", {})):
            for w in d.values():
                if isinstance(w, QLineEdit): w.clear()
        for w in getattr(self, "_dsa_result_f", {}).values():
            if isinstance(w, QComboBox): w.setCurrentIndex(0)
            elif isinstance(w, QLineEdit): w.clear()
        getattr(self, "_dsa_photo_bytes", {}).clear()
        if hasattr(self, "_manual_dsa_patient_photo_lbl"):
            self._manual_dsa_patient_photo_lbl.setText("No photo selected")
        if hasattr(self, "_manual_dsa_donor_photo_lbl"):
            self._manual_dsa_donor_photo_lbl.setText("No photo selected")
        self.manual_status_label.setText("Form cleared.")

    # ── Multi-donor helpers ────────────────────────────────────────────────────
    def _add_manual_donor(self, donor_data=None):
        """Create and insert a new donor panel into the manual tab."""
        di = len(self._manual_donors)
        raw_fields = donor_data.get("fields", {}) if isinstance(donor_data, dict) and "fields" in donor_data else (donor_data or {})
        hla_data   = donor_data.get("hla", {})    if isinstance(donor_data, dict) and "hla"    in donor_data else {}

        # Backward-compat: normalise old donor_* key names to bulk-style keys
        _key_map = {
            "donor_name": "name", "donor_gender_age": "gender_age",
            "donor_diagnosis": "diagnosis", "donor_referred_by": "referred_by",
            "donor_pin": "pin", "donor_sample_no": "sample_number",
            "donor_collect": "collection_date", "donor_receipt": "receipt_date",
        }
        fields = {}
        for k, v in raw_fields.items():
            fields[_key_map.get(k, k)] = v

        # Auto-switch: adding first donor to a single-HLA case → transplant_donor
        if di == 0 and hasattr(self, "_manual_rtype_combo"):
            cur_rtype = TEMPLATE_TO_RTYPE.get(self._manual_rtype_combo.currentText(), "single_hla")
            if cur_rtype == "single_hla":
                td_name = RTYPE_TO_TEMPLATE.get("transplant_donor", TEMPLATE_NAMES[0])
                td_idx  = self._manual_rtype_combo.findText(td_name)
                if td_idx >= 0:
                    self._manual_rtype_combo.blockSignals(True)
                    self._manual_rtype_combo.setCurrentIndex(td_idx)
                    self._manual_rtype_combo.blockSignals(False)
                self.template_combo.blockSignals(True)
                gt_idx = self.template_combo.findText(td_name)
                if gt_idx >= 0: self.template_combo.setCurrentIndex(gt_idx)
                self.template_combo.blockSignals(False)

        container = QWidget()
        c_lay = QVBoxLayout(container)
        c_lay.setContentsMargins(0, 0, 0, 2)
        c_lay.setSpacing(2)

        group = QGroupBox(f"Donor {di + 1}")
        form  = QFormLayout()
        form.setSpacing(1)
        form.setContentsMargins(4, 2, 4, 2)
        group.setLayout(form)

        DONOR_FIELDS = [
            ("name",            "Donor Name",      ""),
            ("relationship",    "Relationship",    ""),
            ("gender_age",      "Gender / Age",    ""),
            ("hospital_mr_no",  "Hospital MR No.", "NA"),
            ("diagnosis",       "Diagnosis",       ""),
            ("referred_by",     "Referred By",     ""),
            ("hospital_clinic", "Hospital / Clinic", ""),
            ("pin",             "Donor PIN",       ""),
            ("sample_number",   "Sample Number",   ""),
            ("collection_date", "Collection Date", ""),
            ("receipt_date",    "Receipt Date",    ""),
            ("report_date",     "Report Date",     ""),
            ("match",           "Match Score",     ""),
            ("remarks",         "Remarks",         ""),
        ]
        d_fields = {}
        for key, lbl, default in DONOR_FIELDS:
            val = fields.get(key, default)
            w = QLineEdit(val)
            w.setFixedHeight(24)
            if "date" in key.lower(): w.setPlaceholderText("DD-MM-YYYY")
            d_fields[key] = w
            form.addRow(lbl + ":", w)
            w.textChanged.connect(self._on_manual_field_debounced)
            if key == "relationship":
                w.textChanged.connect(self._auto_detect_manual_template)

        form.addRow(QLabel("<b>Donor HLA Results</b>"), QLabel(""))
        d_hla = {}
        for locus in HLA_LOCI:
            alleles = hla_data.get(locus, ["", ""])
            a1_val  = _allele_str(alleles[0] if len(alleles) > 0 else None)
            a2_val  = _allele_str(alleles[1] if len(alleles) > 1 else None)
            row_w, a1, a2 = _make_allele_row(a1_val, a2_val)
            a1.textChanged.connect(self._on_manual_field_debounced)
            a2.textChanged.connect(self._on_manual_field_debounced)
            form.addRow(f"  {locus}:", row_w)
            d_hla[locus] = [a1, a2]

        remove_btn = QPushButton(f"Remove Donor {di + 1}")
        remove_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
        remove_btn.setMaximumHeight(22)

        c_lay.addWidget(group)
        c_lay.addWidget(remove_btn)

        entry = {"container": container, "fields": d_fields, "hla": d_hla, "group": group}
        self._manual_donors.append(entry)
        remove_btn.clicked.connect(lambda checked, e=entry: self._remove_manual_donor(e))

        self._donors_list_layout.addWidget(container)
        self._auto_detect_manual_template()
        self._refresh_manual_preview()

    def _remove_manual_donor(self, entry):
        """Remove a donor panel from the manual tab."""
        if entry in self._manual_donors:
            self._manual_donors.remove(entry)
            entry["container"].deleteLater()
            # Re-number remaining donor groups
            for i, e in enumerate(self._manual_donors):
                e["group"].setTitle(f"Donor {i + 1}")
            self._auto_detect_manual_template()
            # Fix 6: propagate donor removal immediately to preview
            self._refresh_manual_preview()

    # ── Signature image overrides (manual tab) ─────────────────────────────────
    # ── Signature name-override helpers ───────────────────────────────────────
    def _on_manual_sig_changed(self, slot: int, text: str):
        """Called when the user changes a signature-override dropdown in the Manual tab."""
        if text == "(Use Default)":
            self._manual_sig_name_overrides.pop(slot, None)
        else:
            self._manual_sig_name_overrides[slot] = text

    def _apply_sig_name_overrides(self, case: dict, name_overrides: dict):
        """
        Replace signatory sign_b64 / is_png / name / title using name-keyed lookup from SIGN_BY_NAME.
        name_overrides: {slot_int_or_str: sig_name_string}
        Slots with "(Use Default)" or missing entries are left unchanged.
        """
        title_lookup = {s["name"]: s["title"] for s in DEFAULT_SIGNATORIES}
        for i, sig in enumerate(case.get("signatories", [])):
            name = name_overrides.get(i) or name_overrides.get(str(i))
            if not name or name == "(Use Default)":
                continue
            sign_info = hla_assets.SIGN_BY_NAME.get(name)
            if sign_info:
                sig["sign_b64"] = sign_info["sign_b64"]
                sig["is_png"]   = sign_info["is_png"]
                sig["name"]     = name
                if name in title_lookup:
                    sig["title"] = title_lookup[name]

    def save_manual_draft(self):
        os.makedirs(DRAFTS_DIR, exist_ok=True)
        rtype = TEMPLATE_TO_RTYPE.get(
            self._manual_rtype_combo.currentText()
            if hasattr(self, "_manual_rtype_combo") else self.template_combo.currentText(),
            "single_hla")
        # Read patient name from the active form for the current report type
        if rtype == "cdc_crossmatch":
            name_val = getattr(self, "_cdc_pat_f", {}).get("patient_name", QLineEdit()).text().strip()
        elif rtype == "dsa_crossmatch":
            name_val = getattr(self, "_dsa_pat_f", {}).get("patient_name", QLineEdit()).text().strip()
        elif rtype == "flow_crossmatch":
            name_val = getattr(self, "_flow_pat_f", {}).get("patient_name", QLineEdit()).text().strip()
        else:
            name_val = self.f.get("patient_name", QLineEdit()).text().strip()
        safe_name      = _re.sub(r"[^\w\-]", "_", name_val) if name_val else "Unknown"
        template_label = _re.sub(r"[^\w\-]", "_", RTYPE_TO_TEMPLATE.get(rtype, rtype))
        filename       = f"{safe_name}_{template_label}_draft.json"
        path           = os.path.join(DRAFTS_DIR, filename)

        saved_donors = []
        for entry in self._manual_donors:
            saved_donors.append({
                "fields": {k: w.text().strip() for k, w in entry["fields"].items()},
                "hla":    {locus: [a[0].text().strip(), a[1].text().strip()]
                           for locus, a in entry["hla"].items()},
            })
        data = {
            "patient_fields": {k: w.text().strip() for k, w in self.f.items()},
            "report_type": rtype,
            "with_logo":   self.logo_combo.currentText(),
            "donors":      saved_donors,
            "patient_hla": {locus: [a[0].text().strip(), a[1].text().strip()]
                            for locus, a in self.hla_pat.items()},
            "sig_name_overrides": {str(k): v for k, v in self._manual_sig_name_overrides.items()},
            "nabl":            self._manual_nabl_chk.isChecked(),
            "report_settings": {k: w.text().strip() for k, w in self._manual_report_settings.items()},
            "rpl_reference":   {k: w.text().strip() for k, w in self._manual_rpl_fields.items()},
        }
        # Save CDC/DSA/Flow-specific form data so it can be fully restored on load
        if rtype == "cdc_crossmatch" and hasattr(self, "_cdc_pat_f"):
            data["cdc_patient_fields"] = {k: w.text().strip() for k, w in self._cdc_pat_f.items()}
            data["cdc_donor_fields"]   = {k: w.text().strip() for k, w in self._cdc_don_f.items()}
            data["cdc_results"]        = {k: w.currentText() for k, w in self._manual_cdc_fields.items()}
        elif rtype == "dsa_crossmatch" and hasattr(self, "_dsa_pat_f"):
            data["dsa_patient_fields"] = {k: w.text().strip() for k, w in self._dsa_pat_f.items()}
            data["dsa_donor_fields"]   = {k: w.text().strip() for k, w in self._dsa_don_f.items()}
            data["dsa_results"]        = {
                k: (w.currentText() if isinstance(w, QComboBox) else w.text().strip())
                for k, w in self._dsa_result_f.items()
            }
        elif rtype == "flow_crossmatch" and hasattr(self, "_flow_pat_f"):
            data["flow_patient_fields"] = {k: w.text().strip() for k, w in self._flow_pat_f.items()}
            data["flow_donor_fields"]   = {k: w.text().strip() for k, w in self._flow_don_f.items()}
            data["flow_results"]        = {
                k: (w.currentText() if isinstance(w, QComboBox) else w.text().strip())
                for k, w in self._flow_result_f.items()
            }
        try:
            with open(path, "w") as fh: json.dump(data, fh, indent=2)
            self.manual_status_label.setText(
                f"Draft saved for {name_val or 'Patient'}: {filename}")
        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e))

    def load_manual_draft(self):
        start_dir = DRAFTS_DIR if os.path.isdir(DRAFTS_DIR) else os.path.dirname(MANUAL_DRAFT_FILE)
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Manual Draft", start_dir,
            "JSON Files (*.json);;All Files (*)"
        )
        if not path: return
        self._loading_draft = True
        try:
            with open(path) as fh: data = json.load(fh)
            # Normalize bulk-format drafts (have "patient" key instead of "patient_fields")
            if "patient" in data and "patient_fields" not in data:
                p = data["patient"]
                p_hla = p.get("hla", {})
                p_fields = {k: v for k, v in p.items() if k != "hla"}
                # Excel bulk uses "name"; manual form field key is "patient_name"
                if "name" in p_fields and "patient_name" not in p_fields:
                    p_fields["patient_name"] = p_fields.pop("name")
                manual_donors = []
                for d in data.get("donors", []):
                    manual_donors.append({
                        "fields": {
                            "name":            d.get("name", ""),
                            "relationship":    d.get("relationship", ""),
                            "gender_age":      d.get("gender_age", ""),
                            "hospital_mr_no":  d.get("hospital_mr_no", "NA") or "NA",
                            "diagnosis":       d.get("diagnosis", ""),
                            "referred_by":     d.get("referred_by", ""),
                            "pin":             d.get("pin", ""),
                            "sample_number":   d.get("sample_number", ""),
                            "collection_date": d.get("collection_date", ""),
                            "receipt_date":    d.get("receipt_date", ""),
                            "report_date":     d.get("report_date", ""),
                            "match":           d.get("match", ""),
                            "remarks":         d.get("remarks", ""),
                        },
                        "hla": d.get("hla", {}),
                    })
                data = {
                    "patient_fields":    p_fields,
                    "patient_hla":       p_hla,
                    "donors":            manual_donors,
                    "report_type":       data.get("report_type", "single_hla"),
                    "with_logo":         data.get("with_logo", True),
                    "nabl":              data.get("nabl", True),
                    "sig_name_overrides": data.get("sig_name_overrides", {}),
                    "report_settings": {
                        "typing_status": data.get("typing_status", "Complete"),
                        "imgt_release":  data.get("imgt_release", ""),
                        "methodology":   data.get("methodology", ""),
                        "coverage":      data.get("coverage", ""),
                    },
                    "rpl_reference": data.get("rpl_reference", {}),
                }
            for k, v in data.get("patient_fields", {}).items():
                if k in self.f: self.f[k].setText(v)
            _tmpl_name = RTYPE_TO_TEMPLATE.get(data.get("report_type", "single_hla"), TEMPLATE_NAMES[0])
            for combo in (self.template_combo,
                          getattr(self, "_manual_rtype_combo", None)):
                if combo is None: continue
                idx = combo.findText(_tmpl_name)
                if idx >= 0:
                    combo.blockSignals(True)
                    combo.setCurrentIndex(idx)
                    combo.blockSignals(False)
            _wl = data.get("with_logo", "With Logo")
            if isinstance(_wl, bool):
                _wl = "With Logo" if _wl else "Without Logo"
            idx = self.logo_combo.findText(_wl)
            if idx >= 0: self.logo_combo.setCurrentIndex(idx)
            # Clear existing donor panels
            for entry in list(self._manual_donors):
                entry["container"].deleteLater()
            self._manual_donors.clear()
            # Restore donor panels
            for donor_data in data.get("donors", []):
                self._add_manual_donor(donor_data)
            # Backward compat: old single-donor format
            if not data.get("donors") and data.get("donor_enabled"):
                old_fields = data.get("donor_fields", {})
                old_hla    = data.get("donor_hla", {})
                self._add_manual_donor({"fields": old_fields, "hla": old_hla})
            for locus, vals in data.get("patient_hla", {}).items():
                if locus in self.hla_pat:
                    self.hla_pat[locus][0].setText(_allele_str(vals[0] if len(vals) > 0 else None))
                    self.hla_pat[locus][1].setText(_allele_str(vals[1] if len(vals) > 1 else None))
            # Restore signature name overrides
            self._manual_sig_name_overrides.clear()
            for slot_s, sig_name in data.get("sig_name_overrides", {}).items():
                slot_i = int(slot_s)
                if sig_name and sig_name != "(Use Default)":
                    self._manual_sig_name_overrides[slot_i] = sig_name
                cmb = self._manual_sig_combos.get(slot_i)
                if cmb:
                    idx = cmb.findText(sig_name)
                    if idx >= 0: cmb.setCurrentIndex(idx)
            self._manual_nabl_chk.setChecked(data.get("nabl", True))
            for k, v in data.get("report_settings", {}).items():
                if k in self._manual_report_settings:
                    self._manual_report_settings[k].setText(str(v))
            for k, v in data.get("rpl_reference", {}).items():
                if k in self._manual_rpl_fields:
                    self._manual_rpl_fields[k].setText(str(v))
            # Restore CDC/DSA/Flow-specific form fields
            _rtype = data.get("report_type", "single_hla")
            if _rtype == "cdc_crossmatch":
                for k, v in data.get("cdc_patient_fields", {}).items():
                    if hasattr(self, "_cdc_pat_f") and k in self._cdc_pat_f:
                        self._cdc_pat_f[k].setText(str(v))
                for k, v in data.get("cdc_donor_fields", {}).items():
                    if hasattr(self, "_cdc_don_f") and k in self._cdc_don_f:
                        self._cdc_don_f[k].setText(str(v))
                for k, v in data.get("cdc_results", {}).items():
                    if hasattr(self, "_manual_cdc_fields") and k in self._manual_cdc_fields:
                        self._manual_cdc_fields[k].setCurrentText(str(v))
            elif _rtype == "dsa_crossmatch":
                for k, v in data.get("dsa_patient_fields", {}).items():
                    if hasattr(self, "_dsa_pat_f") and k in self._dsa_pat_f:
                        self._dsa_pat_f[k].setText(str(v))
                for k, v in data.get("dsa_donor_fields", {}).items():
                    if hasattr(self, "_dsa_don_f") and k in self._dsa_don_f:
                        self._dsa_don_f[k].setText(str(v))
                for k, v in data.get("dsa_results", {}).items():
                    if hasattr(self, "_dsa_result_f") and k in self._dsa_result_f:
                        w = self._dsa_result_f[k]
                        if isinstance(w, QComboBox): w.setCurrentText(str(v))
                        else: w.setText(str(v))
            elif _rtype == "flow_crossmatch":
                for k, v in data.get("flow_patient_fields", {}).items():
                    if hasattr(self, "_flow_pat_f") and k in self._flow_pat_f:
                        self._flow_pat_f[k].setText(str(v))
                for k, v in data.get("flow_donor_fields", {}).items():
                    if hasattr(self, "_flow_don_f") and k in self._flow_don_f:
                        self._flow_don_f[k].setText(str(v))
                for k, v in data.get("flow_results", {}).items():
                    if hasattr(self, "_flow_result_f") and k in self._flow_result_f:
                        w = self._flow_result_f[k]
                        if isinstance(w, QComboBox): w.setCurrentText(str(v))
                        else: w.setText(str(v))
            self._update_manual_rpl_visibility()
            self.manual_status_label.setText(f"Draft loaded: {os.path.basename(path)}")
        except Exception as e:
            QMessageBox.critical(self, "Load Error", str(e))
        finally:
            self._loading_draft = False
            self._refresh_manual_preview()  # single preview refresh after everything is loaded

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 2 — BULK UPLOAD
    # ══════════════════════════════════════════════════════════════════════════
    def _create_bulk_tab(self):
        tab = QWidget()
        main_layout = QVBoxLayout()
        main_layout.setSpacing(0)
        main_layout.setContentsMargins(5, 5, 5, 5)
        tab.setLayout(main_layout)

        # 1 & 2. File and Output Selection (Combined for space)
        config_group  = QGroupBox("1 & 2. File and Output Selection")
        config_layout = QVBoxLayout(); config_group.setLayout(config_layout)
        
        # File row
        file_row = QHBoxLayout()
        self.bulk_file_label = QLabel("No file selected")
        self.bulk_file_label.setStyleSheet(PATH_LABEL_STYLE)
        file_row.addWidget(QLabel("<b>Excel File:</b>"))
        file_row.addWidget(self.bulk_file_label, 1)
        browse_btn = QPushButton("Browse")
        browse_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton))
        browse_btn.clicked.connect(self.browse_bulk_file)
        file_row.addWidget(browse_btn)
        config_layout.addLayout(file_row)
        config_layout.setSpacing(1)
        config_layout.setContentsMargins(5, 2, 5, 2)

        # Output row
        out_row = QHBoxLayout()
        self.bulk_output_label = QLabel("No folder selected")
        self.bulk_output_label.setStyleSheet(PATH_LABEL_STYLE)
        out_row.addWidget(QLabel("<b>Output Folder:</b>"))
        out_row.addWidget(self.bulk_output_label, 1)
        out_browse_btn = QPushButton("Browse")
        out_browse_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton))
        out_browse_btn.clicked.connect(self.browse_bulk_output)
        out_row.addWidget(out_browse_btn)

        config_layout.addLayout(out_row)
        
        # Options row (NABL + Load)
        opts_row = QHBoxLayout()
        self.chk_nabl = QCheckBox("NABL-Accredited (MINISEQ)")
        self.chk_nabl.setChecked(True)
        opts_row.addWidget(self.chk_nabl)
        opts_row.addStretch()
        load_btn = QPushButton("Load & Parse Excel")
        load_btn.setStyleSheet(GENERATE_BTN_STYLE)
        load_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowRight))
        load_btn.clicked.connect(self.load_excel)
        opts_row.addWidget(load_btn)
        config_layout.addLayout(opts_row)
        
        main_layout.addWidget(config_group)

        # 3. Review and Edit Cases (PGTA layout)
        review_group  = QGroupBox("3. Review and Edit Cases")
        review_layout = QHBoxLayout(); review_group.setLayout(review_layout)

        # LEFT: patient list + controls
        left_panel  = QWidget()
        left_panel.setMinimumWidth(200)
        left_panel.setMaximumWidth(350)
        left_layout = QVBoxLayout(); left_panel.setLayout(left_layout)
        left_layout.setSpacing(3)

        # Search row
        self.bulk_search = QLineEdit()
        self.bulk_search.setPlaceholderText("Search by patient name…")
        self.bulk_search.setFixedHeight(24)
        self.bulk_search.textChanged.connect(self._filter_bulk_list)
        left_layout.addWidget(self.bulk_search)

        # Cases list — takes all available vertical space
        self.bulk_list = QListWidget()
        self.bulk_list.setAlternatingRowColors(True)
        self.bulk_list.currentItemChanged.connect(self._on_bulk_item_changed)
        self.bulk_list.itemChanged.connect(self._on_bulk_check_changed)
        left_layout.addWidget(self.bulk_list, 1)

        # ── Compact controls row 1: Select/Deselect All + Save Draft (Selected) ──
        ctrl_row1 = QHBoxLayout()
        ctrl_row1.setSpacing(4)

        self._sel_all_btn = QPushButton("Select All")
        self._sel_all_btn.setFixedHeight(24)
        self._sel_all_btn.setToolTip("Select or deselect all cases")
        self._sel_all_btn.clicked.connect(self._on_sel_all_toggled)
        ctrl_row1.addWidget(self._sel_all_btn)

        save_sel_btn = QPushButton("Save Draft (Selected)")
        save_sel_btn.setFixedHeight(24)
        save_sel_btn.setToolTip("Save drafts for all checked patients to /drafts")
        save_sel_btn.clicked.connect(self.save_bulk_selected_draft)
        ctrl_row1.addWidget(save_sel_btn)
        left_layout.addLayout(ctrl_row1)

        # ── Compact controls row 2: Save All Draft + Load Draft ──────────────────
        ctrl_row2 = QHBoxLayout()
        ctrl_row2.setSpacing(4)

        save_all_btn = QPushButton("Save All Draft")
        save_all_btn.setFixedHeight(24)
        save_all_btn.clicked.connect(self.save_bulk_draft)
        ctrl_row2.addWidget(save_all_btn)

        load_draft_btn = QPushButton("Load Draft")
        load_draft_btn.setFixedHeight(24)
        load_draft_btn.clicked.connect(self.load_bulk_draft)
        ctrl_row2.addWidget(load_draft_btn)
        left_layout.addLayout(ctrl_row2)

        # ── Report Generation group ───────────────────────────────────────────────
        gen_group_box = QGroupBox("Report Generation")
        gen_group_lay = QVBoxLayout(); gen_group_box.setLayout(gen_group_lay)
        gen_group_lay.setSpacing(3)
        gen_group_lay.setContentsMargins(4, 4, 4, 4)

        gen_current_btn = QPushButton("Generate Report")
        gen_current_btn.setFixedHeight(28)
        gen_current_btn.setStyleSheet(GENERATE_BTN_STYLE)
        gen_current_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        gen_current_btn.setToolTip("Generate report for the currently selected patient")
        gen_current_btn.clicked.connect(self.generate_bulk_current)
        gen_group_lay.addWidget(gen_current_btn)

        gen_btn = QPushButton("Generate All Reports")
        gen_btn.setFixedHeight(28)
        gen_btn.setStyleSheet(GENERATE_BTN_STYLE)
        gen_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        gen_btn.setToolTip("Generate reports for all patients regardless of selection")
        gen_btn.clicked.connect(self.generate_bulk)
        gen_group_lay.addWidget(gen_btn)

        left_layout.addWidget(gen_group_box)

        # RIGHT: Patient Editor
        editor_widget = QWidget()
        editor_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        editor_layout = QVBoxLayout(); editor_widget.setLayout(editor_layout)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.addWidget(QLabel("Patient Editor (select a case from the list):"))

        self._bulk_scroll = QScrollArea()
        self._bulk_scroll.setWidgetResizable(True)
        self._bulk_scroll.setMinimumHeight(60)   # allow shrinking so action buttons always show
        self._bulk_editor_container = QWidget()
        self._bulk_editor_layout    = QVBoxLayout()
        self._bulk_editor_container.setLayout(self._bulk_editor_layout)
        self._bulk_scroll.setWidget(self._bulk_editor_container)
        editor_layout.addWidget(self._bulk_scroll, 1)

        # Persistent action buttons — live OUTSIDE the scroll area so they are
        # always visible and never overlap scrollable content.
        _bulk_action_row = QHBoxLayout()
        _bulk_action_row.setContentsMargins(0, 4, 0, 0)
        self._bulk_apply_btn = QPushButton("Apply Edits")
        self._bulk_apply_btn.setStyleSheet(GENERATE_BTN_STYLE)
        self._bulk_apply_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self._bulk_apply_btn.setEnabled(False)
        self._bulk_apply_btn.clicked.connect(lambda: self._flush_bulk_edits(self._bulk_current_row))
        self._bulk_save_draft_btn = QPushButton("Save Draft")
        self._bulk_save_draft_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
        self._bulk_save_draft_btn.setToolTip("Save this patient's data as a draft to the /drafts folder")
        self._bulk_save_draft_btn.setEnabled(False)
        self._bulk_save_draft_btn.clicked.connect(self.save_bulk_current_draft)
        _bulk_action_row.addWidget(self._bulk_apply_btn)
        _bulk_action_row.addWidget(self._bulk_save_draft_btn)
        _bulk_action_row.addStretch()
        editor_layout.addLayout(_bulk_action_row)

        placeholder = QLabel("Select a case from the list to edit")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setStyleSheet(STATUS_LABEL_STYLE)
        self._bulk_editor_layout.addWidget(placeholder)

        # Batch Report Preview
        preview_group  = QGroupBox("Batch Report Preview")
        preview_group.setFixedWidth(620) # Fixed size as requested
        preview_layout = QVBoxLayout(); preview_group.setLayout(preview_layout)
        
        prev_top = QHBoxLayout()
        self.bulk_preview_status = QLabel("Select a patient to preview")
        self.bulk_preview_status.setStyleSheet("color:gray; font-style:italic;")
        self.bulk_preview_status.setWordWrap(True)
        prev_top.addWidget(self.bulk_preview_status, 1)
        
        refresh_preview_btn = QPushButton("Refresh Preview")
        refresh_preview_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        refresh_preview_btn.clicked.connect(self._refresh_bulk_preview)
        prev_top.addWidget(refresh_preview_btn)
        preview_layout.addLayout(prev_top)
        
        prev_scroll = QScrollArea()
        prev_scroll.setWidgetResizable(True)
        self._bulk_preview_inner = QWidget()
        self._bulk_preview_vbox  = QVBoxLayout(self._bulk_preview_inner)
        self._bulk_preview_vbox.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        prev_scroll.setWidget(self._bulk_preview_inner)
        preview_layout.addWidget(prev_scroll, 1)

        # Assemble: Splitter for [Cases | Editor] + Fixed [Preview]
        edit_splitter = QSplitter(Qt.Orientation.Horizontal)
        edit_splitter.addWidget(left_panel)
        edit_splitter.addWidget(editor_widget)
        edit_splitter.setSizes([280, 500])
        edit_splitter.setStretchFactor(0, 0)
        edit_splitter.setStretchFactor(1, 1)

        review_layout.addWidget(edit_splitter, 1)
        review_layout.addWidget(preview_group, 0)
        review_layout.setContentsMargins(5, 5, 5, 5)
        review_layout.setSpacing(5)
        review_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        main_layout.addWidget(review_group, 1)


        # Note: bulk_status_label and bulk_log are no longer added to layout
        # they are kept as attributes so background logging still works.
        self.bulk_status_label = QLabel("")
        self.bulk_log = QTextEdit() # hidden
        return tab

    def browse_bulk_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Excel File",
            self.qsettings.value("last_excel_dir", ""),
            "Excel Files (*.xlsx *.xls);;All Files (*)"
        )
        if path:
            self.bulk_file_label.setText(path)
            self.qsettings.setValue("last_excel_dir", os.path.dirname(path))
            if "MINISEQ" in path.upper():  self.chk_nabl.setChecked(True)
            elif "SURFSEQ" in path.upper(): self.chk_nabl.setChecked(False)

    def browse_bulk_output(self):
        start = self.qsettings.value("last_output_dir", str(Path.home()))
        path  = QFileDialog.getExistingDirectory(self, "Select Output Folder", start)
        if path:
            self.bulk_output_label.setText(path)
            self.manual_output_label.setText(path)   # sync
            self.qsettings.setValue("last_output_dir", path)

    def _reset_bulk_session(self):
        """Fix 1: Fully reset all session state before loading a new file/draft.
        No previously edited alleles, donors, or manual changes must persist."""
        self.cases             = []
        self._bulk_current_row = -1
        self._bulk_fields      = {}
        self._bulk_hla_pat     = {}
        self._bulk_donor_fields = []
        self._bulk_hla_don     = []
        self._bulk_nabl_chk    = None
        # Clear the editor UI so no stale widgets remain
        while self._bulk_editor_layout.count():
            child = self._bulk_editor_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

    def load_excel(self):
        path = self.bulk_file_label.text()
        if path == "No file selected" or not os.path.exists(path):
            QMessageBox.warning(self, "No File", "Please select a valid Excel file.")
            return
        # Fix 1: reset all state before parsing so no prior session data leaks in
        self._reset_bulk_session()
        try:
            raw_cases  = parse_excel(path, nabl=self.chk_nabl.isChecked())
            self.cases = _filter_valid_cases(raw_cases)
            skipped    = len(raw_cases) - len(self.cases)
            self._populate_bulk_list()
            msg = f"Loaded {len(self.cases)} case(s) from {os.path.basename(path)}"
            if skipped:
                msg += f" ({skipped} suppressed — Insufficient Data)"
            self._bulk_log(msg)
        except Exception as e:
            QMessageBox.critical(self, "Parse Error", str(e))
            import traceback; traceback.print_exc()


    def _populate_bulk_list(self):
        self.bulk_list.clear()
        colors = {"single_hla": "#E8F5E9", "rpl_couple": "#FFF3E0",
                  "transplant_donor": "#E3F2FD"}
        for i, case in enumerate(self.cases):
            p      = case["patient"]
            donors = case.get("donors", [])
            label  = p.get("name", "(no name)")
            if donors:
                label += " + " + " + ".join(d.get("name","") for d in donors)
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, i)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setBackground(QColor(colors.get(case.get("report_type",""), "#FFFFFF")))
            self.bulk_list.addItem(item)
        # Sync button + checkbox state after population
        self._on_bulk_check_changed(None)

    def _filter_bulk_list(self, text):
        for i in range(self.bulk_list.count()):
            item = self.bulk_list.item(i)
            item.setHidden(text.lower() not in item.text().lower())

    # ── Bulk editor — PGTA-style dynamic form rebuild ─────────────────────────
    def _on_bulk_item_changed(self, current, previous):
        """Flush edits from previous then rebuild the editor for the new selection."""
        # Only flush if we have a tracked row — guards against stale flushes that
        # fire when bulk_list.clear() is called during a new Excel/draft load.
        # At that point _bulk_current_row == -1 (reset by _reset_bulk_session) so
        # a stale _bulk_rtype_combo from a prior session cannot corrupt the new cases.
        if previous is not None and self._bulk_current_row >= 0:
            prev_idx = previous.data(Qt.ItemDataRole.UserRole)
            if prev_idx is not None:
                self._flush_bulk_edits(prev_idx)

        if current is None: return
        idx = current.data(Qt.ItemDataRole.UserRole)
        if idx is None or idx >= len(self.cases): return
        self._bulk_current_row = idx
        self._rebuild_bulk_editor(idx)


    def _on_bulk_field_debounced(self):
        """Triggered by any field change; restarts debounce timer."""
        self._edit_timer.start(400) # 400ms debounce

    def _on_bulk_rtype_combo_changed(self):
        """Per-case Report Type combo changed — flush edits then rebuild form immediately."""
        idx = self._bulk_current_row
        if idx < 0 or idx >= len(self.cases):
            return
        # Flush saves the new report_type from the combo into the case dict.
        self._flush_bulk_edits(idx)
        # Rebuild so the form layout switches to match the new type right away.
        self._rebuild_bulk_editor(idx)

    def _rebuild_bulk_editor(self, idx):
        """Clear and rebuild form fields for cases[idx]. Same pattern as PGTA."""
        # Sync global header combos to this case (block signals to avoid re-triggering)
        case = self.cases[idx]
        _tmpl = RTYPE_TO_TEMPLATE.get(case.get("report_type", "single_hla"), TEMPLATE_NAMES[0])
        self.template_combo.blockSignals(True)
        ti = self.template_combo.findText(_tmpl)
        if ti >= 0: self.template_combo.setCurrentIndex(ti)
        self.template_combo.blockSignals(False)

        # Logo: derive from case's with_logo flag
        with_logo = case.get("with_logo", True)
        logo_txt  = "With Logo" if with_logo else "Without Logo"
        self.logo_combo.blockSignals(True)
        li = self.logo_combo.findText(logo_txt)
        if li >= 0: self.logo_combo.setCurrentIndex(li)
        self.logo_combo.blockSignals(False)

        # Clear existing widgets (PGTA pattern)
        while self._bulk_editor_layout.count():
            child = self._bulk_editor_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        case = self.cases[idx]
        p    = case["patient"]

        # ── Patient Info ────────────────────────────────────────────────────
        pat_group = QGroupBox("Patient Information")
        pat_form  = QFormLayout(); pat_group.setLayout(pat_form)
        pat_form.setSpacing(1); pat_form.setContentsMargins(4, 1, 4, 1)

        self._bulk_fields = {}
        PAT_FIELDS = [
            ("name",             "Name *"),
            ("gender_age",       "Gender / Age"),
            ("pin",              "PIN"),
            ("sample_number",    "Sample Number"),
            ("diagnosis",        "Diagnosis"),
            ("referred_by",      "Referred By"),
            ("hospital_clinic",  "Hospital / Clinic"),
            ("hospital_mr_no",   "Hospital MR No."),
            ("collection_date",  "Collection Date"),
            ("receipt_date",     "Sample Receipt Date"),
            ("report_date",      "Report Date"),
            ("specimen",         "Specimen"),
            ("remarks",          "Remarks"),
        ]
        for key, lbl in PAT_FIELDS:
            _def = "NA" if key == "hospital_mr_no" else ""
            w = QLineEdit(str(p.get(key, _def) or _def))
            w.setFixedHeight(24)
            if "date" in key.lower(): w.setPlaceholderText("DD-MM-YYYY")
            w.textChanged.connect(self._on_bulk_field_debounced)
            self._bulk_fields[key] = w
            pat_form.addRow(lbl + ":", w)

        _nabl_default = case.get("nabl", self.qsettings.value("nabl_stamp", True, type=bool))
        self._bulk_nabl_chk = QCheckBox("NABL Accreditation")
        self._bulk_nabl_chk.setChecked(_nabl_default)
        self._bulk_nabl_chk.stateChanged.connect(self._on_bulk_field_debounced)
        pat_form.addRow(self._bulk_nabl_chk)

        # Report-level fields
        meta_group = QGroupBox("Report Settings")
        meta_form  = QFormLayout(); meta_group.setLayout(meta_form)
        meta_form.setSpacing(1)
        meta_form.setContentsMargins(4, 1, 4, 1)

        rtype_combo = ClickOnlyComboBox()
        rtype_combo.addItems(TEMPLATE_NAMES)
        _cur_tmpl = RTYPE_TO_TEMPLATE.get(case.get("report_type", "single_hla"), TEMPLATE_NAMES[0])
        ri = rtype_combo.findText(_cur_tmpl)
        if ri >= 0: rtype_combo.setCurrentIndex(ri)
        rtype_combo.currentIndexChanged.connect(self._on_bulk_rtype_combo_changed)
        self._bulk_rtype_combo = rtype_combo
        meta_form.addRow("Report Type:", rtype_combo)

        ts_edit = QLineEdit(case.get("typing_status", "Complete"))
        ts_edit.setFixedHeight(24)
        ts_edit.textChanged.connect(self._on_bulk_field_debounced)
        self._bulk_ts_edit = ts_edit
        meta_form.addRow("Typing Status:", ts_edit)

        imgt_edit = QLineEdit(case.get("imgt_release", ""))
        imgt_edit.setFixedHeight(24)
        imgt_edit.textChanged.connect(self._on_bulk_field_debounced)
        self._bulk_imgt_edit = imgt_edit
        meta_form.addRow("IMGT Release:", imgt_edit)

        meth_edit = QLineEdit(case.get("methodology", ""))
        meth_edit.setFixedHeight(24)
        meth_edit.textChanged.connect(self._on_bulk_field_debounced)
        self._bulk_meth_edit = meth_edit
        meta_form.addRow("Methodology:", meth_edit)

        cov_edit = QLineEdit(case.get("coverage", ""))
        cov_edit.setFixedHeight(24)
        cov_edit.textChanged.connect(self._on_bulk_field_debounced)
        self._bulk_cov_edit = cov_edit
        meta_form.addRow("Coverage Override:", cov_edit)

        # ── Patient HLA ─────────────────────────────────────────────────────
        hla_pat_group = QGroupBox("HLA Results — Patient")
        hla_pat_form  = QFormLayout(); hla_pat_group.setLayout(hla_pat_form)
        hla_pat_form.setSpacing(2)
        hla_pat_form.setContentsMargins(4, 4, 4, 4)

        self._bulk_hla_pat = {}
        hla_data = p.get("hla", {})
        for locus in HLA_LOCI:
            alleles = hla_data.get(locus, ["", ""])
            a1_val  = _allele_str(alleles[0] if len(alleles) > 0 else None)
            a2_val  = _allele_str(alleles[1] if len(alleles) > 1 else None)
            row_w, a1, a2 = _make_allele_row(a1_val, a2_val)
            a1.textChanged.connect(self._on_bulk_field_debounced)
            a2.textChanged.connect(self._on_bulk_field_debounced)
            hla_pat_form.addRow(f"{locus}:", row_w)
            self._bulk_hla_pat[locus] = [a1, a2]

        # ── SAB branch ──────────────────────────────────────────────────────────
        if case.get("report_type") in ("sab_class1", "sab_class2"):
            self._rebuild_bulk_sab_editor(idx, case, pat_group, meta_group)
            return

        # ── Flow Cytometry branch ────────────────────────────────────────────
        if case.get("report_type") == "flow_crossmatch":
            self._rebuild_bulk_flow_editor(idx, case, pat_group, meta_group)
            return

        # ── Luminex branch ───────────────────────────────────────────────────
        if case.get("report_type") == "luminex_typing":
            self._rebuild_bulk_luminex_editor(idx, case, pat_group, meta_group)
            return

        # ── CDC Cross match branch — separate form for CDC reports ───────────
        if case.get("report_type") == "cdc_crossmatch":
            self._rebuild_bulk_cdc_editor(idx, case, pat_group, meta_group)
            return

        # ── DSA Cross match branch — separate form for DSA reports ───────────
        if case.get("report_type") == "dsa_crossmatch":
            self._rebuild_bulk_dsa_editor(idx, case, pat_group, meta_group)
            return

        self._bulk_editor_layout.addWidget(pat_group)
        self._bulk_editor_layout.addWidget(meta_group)

        # ── RPL Reference Section (Conditional) ─────────────────────────────
        self._bulk_rpl_fields = {}
        if case.get("report_type") == "rpl_couple":
            rpl_group = QGroupBox("RPL / Fertility Reference (calculated, editable)")
            rpl_form  = QFormLayout(); rpl_group.setLayout(rpl_form)
            rpl_form.setSpacing(1)
            rpl_form.setContentsMargins(4, 4, 4, 2)
            ref = case.get("rpl_reference", {})
            RPL_FIELDS = [
                ("match_str",        "Match (Overall)"),
                ("match_pct",        "Overall %"),
                ("class2_pct",       "Class-II %"),
                ("hla_sharing_rif",  "HLA Sharing (RIF)"),
                ("hla_c_patient",    "Maternal HLA-C Type"),
                ("hla_c_donor",      "Paternal HLA-C Type"),
            ]
            for key, lbl in RPL_FIELDS:
                w = QLineEdit(str(ref.get(key, "")))
                w.setFixedHeight(24)
                w.textChanged.connect(self._on_bulk_field_debounced)
                self._bulk_rpl_fields[key] = w
                rpl_form.addRow(lbl + ":", w)

            # Auto-calculate Overall % whenever Match (Overall) changes
            def _on_match_str_changed(text, _fields=self._bulk_rpl_fields):
                import re as _re
                m = _re.search(r'(\d+)\s+of\s+(\d+)', text, _re.I)
                pct_w = _fields.get("match_pct")
                if m and pct_w:
                    n, total = int(m.group(1)), int(m.group(2))
                    pct = round(n / total * 100) if total else 0
                    pct_w.blockSignals(True)
                    pct_w.setText(f"{pct}%")
                    pct_w.blockSignals(False)
            self._bulk_rpl_fields["match_str"].textChanged.connect(_on_match_str_changed)

            self._bulk_editor_layout.addWidget(rpl_group)

        self._bulk_editor_layout.addWidget(hla_pat_group)

        # ── Donors ──────────────────────────────────────────────────────────
        self._bulk_donor_fields = []
        self._bulk_hla_don      = []

        _is_rpl = case.get("report_type") == "rpl_couple"
        DONOR_FIELDS_DEF = [
            ("name",            "Name"),
            ("relationship",    "Relationship"),
            ("gender_age",      "Gender / Age"),
            ("hospital_mr_no",  "Hospital MR No."),
            ("diagnosis",       "Diagnosis"),
            ("referred_by",     "Referred By"),
            ("hospital_clinic", "Hospital / Clinic"),
            ("pin",             "PIN"),
            ("sample_number",   "Sample Number"),
            ("match",           "Match Score"),
            ("remarks",         "Remarks"),
            ("collection_date", "Collection Date"),
            ("receipt_date",    "Receipt Date"),
            ("report_date",     "Report Date"),
        ]

        for di, d in enumerate(case.get("donors", [])):
            d_container = QWidget()
            d_container_lay = QVBoxLayout(d_container)
            d_container_lay.setContentsMargins(0, 0, 0, 2)
            d_container_lay.setSpacing(2)

            d_group = QGroupBox(f"Donor {di+1} — {d.get('name','')}")
            d_form  = QFormLayout(); d_group.setLayout(d_form)
            d_form.setSpacing(1)
            d_form.setContentsMargins(4, 4, 4, 2)
            d_fields = {}
            for key, lbl in DONOR_FIELDS_DEF:
                _default = "NA" if key == "hospital_mr_no" else ""
                _val = str(d.get(key, _default) or _default)
                w = QLineEdit(_val); w.setFixedHeight(24)
                if "date" in key.lower(): w.setPlaceholderText("DD-MM-YYYY")
                w.textChanged.connect(self._on_bulk_field_debounced)
                d_fields[key] = w; d_form.addRow(lbl + ":", w)

            # Donor HLA
            d_form.addRow(QLabel(f"<b>Donor {di+1} HLA</b>"), QLabel(""))
            d_hla  = {}
            d_hla_data = d.get("hla", {})
            for locus in HLA_LOCI:
                alleles = d_hla_data.get(locus, ["", ""])
                a1_raw  = alleles[0] if len(alleles) > 0 else None
                a2_raw  = alleles[1] if len(alleles) > 1 else None
                a1_val  = str(a1_raw) if a1_raw is not None else ""
                a2_val  = str(a2_raw) if a2_raw is not None else ""
                row_w, a1, a2 = _make_allele_row(a1_val, a2_val)
                a1.textChanged.connect(self._on_bulk_field_debounced)
                a2.textChanged.connect(self._on_bulk_field_debounced)
                d_form.addRow(f"  {locus}:", row_w)
                d_hla[locus] = [a1, a2]

            # Remove donor button
            rm_btn = QPushButton(f"Remove Donor {di+1}")
            rm_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
            rm_btn.setMaximumHeight(22)
            rm_btn.clicked.connect(
                lambda checked, ci=idx, di_=di: self._remove_bulk_donor(ci, di_))

            d_container_lay.addWidget(d_group)
            d_container_lay.addWidget(rm_btn)

            self._bulk_donor_fields.append(d_fields)
            self._bulk_hla_don.append(d_hla)
            self._bulk_editor_layout.addWidget(d_container)

        # Add donor button
        add_d_btn = QPushButton("+ Add Donor")
        add_d_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder))
        add_d_btn.setMaximumHeight(26)
        add_d_btn.clicked.connect(lambda: self._add_bulk_donor(self._bulk_current_row))
        self._bulk_editor_layout.addWidget(add_d_btn)

        # ── Signature Override (select from configured signatories) ──────────
        self._bulk_sig_combos = {}
        name_overrides = case.get("sig_name_overrides", {})
        sig_group = QGroupBox("Signature Override (select from Settings signatories)")
        sig_form  = QFormLayout()
        sig_form.setSpacing(2)
        sig_form.setContentsMargins(4, 2, 4, 2)
        sig_group.setLayout(sig_form)
        _sig_opts = ["(Use Default)"] + list(hla_assets.SIGN_BY_NAME.keys())
        for i in range(3):
            cmb = ClickOnlyComboBox()
            cmb.addItems(_sig_opts)
            cmb.setFixedHeight(24)
            # Restore saved choice for this slot
            saved_name = name_overrides.get(i, name_overrides.get(str(i), ""))
            if saved_name:
                pos = cmb.findText(saved_name)
                if pos >= 0: cmb.setCurrentIndex(pos)
            cmb.currentTextChanged.connect(
                lambda text, ci=idx, slot=i: self._on_bulk_sig_changed(ci, slot, text))
            self._bulk_sig_combos[i] = cmb
            sig_form.addRow(f"Signatory {i+1}:", cmb)
        self._bulk_editor_layout.addWidget(sig_group)
        self._bulk_editor_layout.addStretch()

        # Update persistent action buttons (outside the scroll) for this case.
        self._bulk_apply_btn.setText(f"Apply Edits to Case {idx+1}")
        self._bulk_apply_btn.setEnabled(True)
        self._bulk_save_draft_btn.setEnabled(True)

        # Auto-generate preview for this case
        QTimer.singleShot(200, self._refresh_bulk_preview)

    # ── Bulk CDC editor builder ────────────────────────────────────────────────
    def _rebuild_bulk_cdc_editor(self, idx, case, _old_pat_group, meta_group):
        """Build CDC-specific editor form inside the bulk editor scroll area."""
        p = case["patient"]
        d = case["donors"][0] if case.get("donors") else {}
        cdc = case.get("cdc_results", {})

        self._bulk_cdc_pat_f    = {}
        self._bulk_cdc_don_f    = {}
        self._bulk_cdc_result_f = {}
        self._bulk_cdc_photo_bytes = dict(case.get("_photo_bytes_tmp", {}))

        # ── Patient info ────────────────────────────────────────────────────
        cdc_pat_grp = QGroupBox("Patient Information")
        cpf = QFormLayout(); cdc_pat_grp.setLayout(cpf)
        cpf.setSpacing(1); cpf.setContentsMargins(4, 2, 4, 2)
        CDC_PAT = [
            ("name",             "Patient Name *",      ""),
            ("gender_age",       "Gender / Age",        ""),
            ("pin",              "PIN",                  ""),
            ("sample_number",    "Sample Number",        ""),
            ("diagnosis",        "Diagnosis",            "NA"),
            ("hospital_clinic",  "Hospital / Clinic",   ""),
            ("sample_type",      "Sample Type",         "Serum"),
            ("collection_date",  "Collection Date",     ""),
            ("receipt_date",     "Sample Receipt Date", ""),
            ("report_date",      "Report Date",         ""),
            ("remarks",          "Remarks",             ""),
            ("comments",         "Additional Comments", ""),
        ]
        for key, lbl, dflt in CDC_PAT:
            w = QLineEdit(str(p.get(key, dflt) or dflt))
            w.setFixedHeight(24)
            if "date" in key: w.setPlaceholderText("DD-MM-YYYY")
            w.textChanged.connect(self._on_bulk_field_debounced)
            self._bulk_cdc_pat_f[key] = w
            cpf.addRow(lbl + ":", w)

        _nabl_default = case.get("nabl", self.qsettings.value("nabl_stamp", True, type=bool))
        self._bulk_nabl_chk = QCheckBox("NABL Accreditation")
        self._bulk_nabl_chk.setChecked(_nabl_default)
        self._bulk_nabl_chk.stateChanged.connect(self._on_bulk_field_debounced)
        cpf.addRow(self._bulk_nabl_chk)

        # Patient photo
        _pprow = QHBoxLayout()
        _pplbl = QLabel(os.path.basename(p.get("_photo_path", "")) or "No photo selected")
        _pplbl.setStyleSheet("color:gray;font-style:italic;")
        _ppbtn = QPushButton("Upload Patient Photo"); _ppbtn.setMaximumHeight(26)
        _ppbtn.clicked.connect(lambda: self._bulk_upload_cdc_photo("patient", _pplbl))
        _pprow.addWidget(_pplbl, 1); _pprow.addWidget(_ppbtn)
        cpf.addRow("Patient Photo:", _pprow)

        # ── Donor info ──────────────────────────────────────────────────────
        cdc_don_grp = QGroupBox("Donor Information")
        cdf = QFormLayout(); cdc_don_grp.setLayout(cdf)
        cdf.setSpacing(1); cdf.setContentsMargins(4, 2, 4, 2)
        CDC_DON = [
            ("name",             "Donor Name",                  ""),
            ("gender_age",       "Gender / Age",                ""),
            ("pin",              "Donor PIN",                   "NA"),
            ("sample_number",    "Sample Number",               "NA"),
            ("relationship",     "Relationship to Recipient",   ""),
            ("sample_type",      "Sample Type",                 "Sodium Heparin Whole Blood"),
            ("collection_date",  "Collection Date",             ""),
            ("receipt_date",     "Sample Receipt Date",         ""),
            ("report_date",      "Report Date",                 ""),
        ]
        for key, lbl, dflt in CDC_DON:
            w = QLineEdit(str(d.get(key, dflt) or dflt))
            w.setFixedHeight(24)
            if "date" in key: w.setPlaceholderText("DD-MM-YYYY")
            w.textChanged.connect(self._on_bulk_field_debounced)
            self._bulk_cdc_don_f[key] = w
            cdf.addRow(lbl + ":", w)

        # Donor photo
        _dprow = QHBoxLayout()
        _dplbl = QLabel(os.path.basename(d.get("_photo_path", "")) or "No photo selected")
        _dplbl.setStyleSheet("color:gray;font-style:italic;")
        _dpbtn = QPushButton("Upload Donor Photo"); _dpbtn.setMaximumHeight(26)
        _dpbtn.clicked.connect(lambda: self._bulk_upload_cdc_photo("donor", _dplbl))
        _dprow.addWidget(_dplbl, 1); _dprow.addWidget(_dpbtn)
        cdf.addRow("Donor Photo:", _dprow)

        # ── CDC Results ─────────────────────────────────────────────────────
        cdc_res_grp = QGroupBox("CDC Results")
        crf = QFormLayout(); cdc_res_grp.setLayout(crf)
        crf.setSpacing(1); crf.setContentsMargins(4, 2, 4, 2)
        _RES_OPTS = ["Negative", "Doubtful", "Weak Positive", "Positive", "Strong Positive"]
        _DTT_OPTS = ["<10% Dead cells", "10-20% Dead cells", "20-50% Dead cells",
                     "50-80% Dead cells", ">80% Dead cells"]
        for key, lbl, opts in [
            ("t_cell",       "T Cell Crossmatch",    _RES_OPTS),
            ("b_cell",       "B Cell Crossmatch",    _RES_OPTS),
            ("t_with_dtt",   "T Cells (With DTT)",   _DTT_OPTS),
            ("t_without_dtt","T Cells (Without DTT)",_DTT_OPTS),
            ("b_with_dtt",   "B Cells (With DTT)",   _DTT_OPTS),
            ("b_without_dtt","B Cells (Without DTT)",_DTT_OPTS),
        ]:
            cmb = ClickOnlyComboBox(); cmb.addItems(opts); cmb.setFixedHeight(24)
            saved = cdc.get(key, opts[0])
            pos = cmb.findText(saved)
            if pos >= 0: cmb.setCurrentIndex(pos)
            cmb.currentIndexChanged.connect(self._on_bulk_field_debounced)
            self._bulk_cdc_result_f[key] = cmb
            crf.addRow(lbl + ":", cmb)

        _CDC_RES_TO_DTT = {
            "Negative":        "<10% Dead cells",
            "Doubtful":        "10-20% Dead cells",
            "Weak Positive":   "20-50% Dead cells",
            "Positive":        "50-80% Dead cells",
            "Strong Positive": ">80% Dead cells",
        }
        _DTT_TO_CDC_RES = {v: k for k, v in _CDC_RES_TO_DTT.items()}
        def _sync_bulk_t_dtt():
            v = _CDC_RES_TO_DTT.get(self._bulk_cdc_result_f["t_cell"].currentText())
            if v:
                self._bulk_cdc_result_f["t_with_dtt"].setCurrentText(v)
                self._bulk_cdc_result_f["t_without_dtt"].setCurrentText(v)
        def _sync_bulk_b_dtt():
            v = _CDC_RES_TO_DTT.get(self._bulk_cdc_result_f["b_cell"].currentText())
            if v:
                self._bulk_cdc_result_f["b_with_dtt"].setCurrentText(v)
                self._bulk_cdc_result_f["b_without_dtt"].setCurrentText(v)
        def _sync_bulk_t_from_dtt():
            r = _DTT_TO_CDC_RES.get(self._bulk_cdc_result_f["t_with_dtt"].currentText())
            if r: self._bulk_cdc_result_f["t_cell"].setCurrentText(r)
        def _sync_bulk_b_from_dtt():
            r = _DTT_TO_CDC_RES.get(self._bulk_cdc_result_f["b_with_dtt"].currentText())
            if r: self._bulk_cdc_result_f["b_cell"].setCurrentText(r)
        self._bulk_cdc_result_f["t_cell"].currentIndexChanged.connect(lambda _: _sync_bulk_t_dtt())
        self._bulk_cdc_result_f["b_cell"].currentIndexChanged.connect(lambda _: _sync_bulk_b_dtt())
        self._bulk_cdc_result_f["t_with_dtt"].currentIndexChanged.connect(lambda _: _sync_bulk_t_from_dtt())
        self._bulk_cdc_result_f["t_without_dtt"].currentIndexChanged.connect(lambda _: _sync_bulk_t_from_dtt())
        self._bulk_cdc_result_f["b_with_dtt"].currentIndexChanged.connect(lambda _: _sync_bulk_b_from_dtt())
        self._bulk_cdc_result_f["b_without_dtt"].currentIndexChanged.connect(lambda _: _sync_bulk_b_from_dtt())

        # Assemble + signature override
        for grp in (cdc_pat_grp, meta_group, cdc_don_grp, cdc_res_grp):
            self._bulk_editor_layout.addWidget(grp)

        self._bulk_sig_combos = {}
        name_overrides = case.get("sig_name_overrides", {})
        sig_group = QGroupBox("Signature Override")
        sig_form  = QFormLayout(); sig_group.setLayout(sig_form)
        sig_form.setSpacing(2); sig_form.setContentsMargins(4, 2, 4, 2)
        _sig_opts = ["(Use Default)"] + list(hla_assets.SIGN_BY_NAME.keys())
        for i in range(3):
            cmb = ClickOnlyComboBox(); cmb.addItems(_sig_opts); cmb.setFixedHeight(24)
            saved_name = name_overrides.get(i, name_overrides.get(str(i), ""))
            if saved_name:
                pos = cmb.findText(saved_name)
                if pos >= 0: cmb.setCurrentIndex(pos)
            cmb.currentTextChanged.connect(
                lambda text, ci=idx, slot=i: self._on_bulk_sig_changed(ci, slot, text))
            self._bulk_sig_combos[i] = cmb
            sig_form.addRow(f"Signatory {i+1}:", cmb)
        self._bulk_editor_layout.addWidget(sig_group)
        self._bulk_editor_layout.addStretch()

        self._bulk_apply_btn.setText(f"Apply Edits to Case {idx+1}")
        self._bulk_apply_btn.setEnabled(True)
        self._bulk_save_draft_btn.setEnabled(True)
        QTimer.singleShot(200, self._refresh_bulk_preview)

    def _bulk_upload_cdc_photo(self, who: str, label: QLabel):
        """File picker for CDC photo in the bulk editor."""
        path, _ = QFileDialog.getOpenFileName(
            self, f"Select {who.title()} Photo", str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.bmp *.tiff)")
        if not path: return
        with open(path, "rb") as fh:
            self._bulk_cdc_photo_bytes[who] = fh.read()
        label.setText(os.path.basename(path))
        self._on_bulk_field_debounced()

    # ── Bulk DSA editor builder ────────────────────────────────────────────────
    def _rebuild_bulk_dsa_editor(self, idx, case, _old_pat_group, meta_group):
        """Build DSA-specific editor form inside the bulk editor scroll area."""
        p   = case["patient"]
        d   = case["donors"][0] if case.get("donors") else {}
        dsa = case.get("dsa_results", {})

        self._bulk_dsa_pat_f    = {}
        self._bulk_dsa_don_f    = {}
        self._bulk_dsa_result_f = {}
        self._bulk_dsa_photo_bytes = dict(case.get("_photo_bytes_tmp", {}))

        # ── Patient info ────────────────────────────────────────────────────
        dsa_pat_grp = QGroupBox("Patient Information")
        dpf = QFormLayout(); dsa_pat_grp.setLayout(dpf)
        dpf.setSpacing(1); dpf.setContentsMargins(4, 2, 4, 2)
        DSA_PAT = [
            ("name",             "Patient Name *",      ""),
            ("gender_age",       "Gender / Age",        ""),
            ("pin",              "PIN",                  ""),
            ("sample_number",    "Sample Number",        ""),
            ("diagnosis",        "Diagnosis",            "NA"),
            ("hospital_clinic",  "Hospital / Clinic",   ""),
            ("sample_type",      "Sample Type",         "Serum"),
            ("collection_date",  "Collection Date",     ""),
            ("receipt_date",     "Sample Receipt Date", ""),
            ("report_date",      "Report Date",         ""),
            ("remarks",          "Remarks",             ""),
            ("comments",         "Additional Comments", ""),
        ]
        for key, lbl, dflt in DSA_PAT:
            w = QLineEdit(str(p.get(key, dflt) or dflt))
            w.setFixedHeight(24)
            if "date" in key: w.setPlaceholderText("DD-MM-YYYY")
            w.textChanged.connect(self._on_bulk_field_debounced)
            self._bulk_dsa_pat_f[key] = w
            dpf.addRow(lbl + ":", w)

        _nabl_default = case.get("nabl", self.qsettings.value("nabl_stamp", True, type=bool))
        self._bulk_nabl_chk = QCheckBox("NABL Accreditation")
        self._bulk_nabl_chk.setChecked(_nabl_default)
        self._bulk_nabl_chk.stateChanged.connect(self._on_bulk_field_debounced)
        dpf.addRow(self._bulk_nabl_chk)

        # Patient photo
        _pprow = QHBoxLayout()
        _pplbl = QLabel(os.path.basename(p.get("_photo_path", "")) or "No photo selected")
        _pplbl.setStyleSheet("color:gray;font-style:italic;")
        _ppbtn = QPushButton("Upload Patient Photo"); _ppbtn.setMaximumHeight(26)
        _ppbtn.clicked.connect(lambda: self._bulk_upload_dsa_photo("patient", _pplbl))
        _pprow.addWidget(_pplbl, 1); _pprow.addWidget(_ppbtn)
        dpf.addRow("Patient Photo:", _pprow)

        # ── Donor info ──────────────────────────────────────────────────────
        dsa_don_grp = QGroupBox("Donor Information")
        ddf = QFormLayout(); dsa_don_grp.setLayout(ddf)
        ddf.setSpacing(1); ddf.setContentsMargins(4, 2, 4, 2)
        DSA_DON = [
            ("name",             "Donor Name",                  ""),
            ("gender_age",       "Gender / Age",                ""),
            ("pin",              "Donor PIN",                   "NA"),
            ("sample_number",    "Sample Number",               "NA"),
            ("relationship",     "Relationship to Recipient",   ""),
            ("sample_type",      "Sample Type",                 "ACD Tube"),
            ("collection_date",  "Collection Date",             ""),
            ("receipt_date",     "Sample Receipt Date",         ""),
            ("report_date",      "Report Date",                 ""),
        ]
        for key, lbl, dflt in DSA_DON:
            w = QLineEdit(str(d.get(key, dflt) or dflt))
            w.setFixedHeight(24)
            if "date" in key: w.setPlaceholderText("DD-MM-YYYY")
            w.textChanged.connect(self._on_bulk_field_debounced)
            self._bulk_dsa_don_f[key] = w
            ddf.addRow(lbl + ":", w)

        # Donor photo
        _dprow = QHBoxLayout()
        _dplbl = QLabel(os.path.basename(d.get("_photo_path", "")) or "No photo selected")
        _dplbl.setStyleSheet("color:gray;font-style:italic;")
        _dpbtn = QPushButton("Upload Donor Photo"); _dpbtn.setMaximumHeight(26)
        _dpbtn.clicked.connect(lambda: self._bulk_upload_dsa_photo("donor", _dplbl))
        _dprow.addWidget(_dplbl, 1); _dprow.addWidget(_dpbtn)
        ddf.addRow("Donor Photo:", _dprow)

        # ── DSA Results ─────────────────────────────────────────────────────
        dsa_res_grp = QGroupBox("DSA Results")
        drf = QFormLayout(); dsa_res_grp.setLayout(drf)
        drf.setSpacing(1); drf.setContentsMargins(4, 2, 4, 2)
        _DSA_RES_OPTS = ["Negative", "Positive", "Weakly Positive", "Borderline"]
        for key, lbl in [("class1_result", "Class I Result"), ("class2_result", "Class II Result")]:
            cmb = ClickOnlyComboBox(); cmb.addItems(_DSA_RES_OPTS); cmb.setFixedHeight(24)
            saved = dsa.get(key, "Negative")
            pos = cmb.findText(saved)
            if pos >= 0: cmb.setCurrentIndex(pos)
            cmb.currentIndexChanged.connect(self._on_bulk_field_debounced)
            self._bulk_dsa_result_f[key] = cmb
            drf.addRow(lbl + ":", cmb)
        for key, lbl, dflt in [
            ("class1_mfi",    "Class I MFI",    ""),
            ("class1_cutoff", "Class I Cutoff", ">1000"),
            ("class2_mfi",    "Class II MFI",   ""),
            ("class2_cutoff", "Class II Cutoff",">1000"),
        ]:
            w = QLineEdit(str(dsa.get(key, dflt) or dflt))
            w.setFixedHeight(24)
            w.textChanged.connect(self._on_bulk_field_debounced)
            self._bulk_dsa_result_f[key] = w
            drf.addRow(lbl + ":", w)

        # Assemble + signature override
        for grp in (dsa_pat_grp, meta_group, dsa_don_grp, dsa_res_grp):
            self._bulk_editor_layout.addWidget(grp)

        self._bulk_sig_combos = {}
        name_overrides = case.get("sig_name_overrides", {})
        sig_group = QGroupBox("Signature Override")
        sig_form  = QFormLayout(); sig_group.setLayout(sig_form)
        sig_form.setSpacing(2); sig_form.setContentsMargins(4, 2, 4, 2)
        _sig_opts = ["(Use Default)"] + list(hla_assets.SIGN_BY_NAME.keys())
        for i in range(3):
            cmb = ClickOnlyComboBox(); cmb.addItems(_sig_opts); cmb.setFixedHeight(24)
            saved_name = name_overrides.get(i, name_overrides.get(str(i), ""))
            if saved_name:
                pos = cmb.findText(saved_name)
                if pos >= 0: cmb.setCurrentIndex(pos)
            cmb.currentTextChanged.connect(
                lambda text, ci=idx, slot=i: self._on_bulk_sig_changed(ci, slot, text))
            self._bulk_sig_combos[i] = cmb
            sig_form.addRow(f"Signatory {i+1}:", cmb)
        self._bulk_editor_layout.addWidget(sig_group)
        self._bulk_editor_layout.addStretch()

        self._bulk_apply_btn.setText(f"Apply Edits to Case {idx+1}")
        self._bulk_apply_btn.setEnabled(True)
        self._bulk_save_draft_btn.setEnabled(True)
        QTimer.singleShot(200, self._refresh_bulk_preview)

    def _bulk_upload_dsa_photo(self, who: str, label: QLabel):
        """File picker for DSA photo in the bulk editor."""
        path, _ = QFileDialog.getOpenFileName(
            self, f"Select {who.title()} Photo", str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.bmp *.tiff)")
        if not path: return
        with open(path, "rb") as fh:
            self._bulk_dsa_photo_bytes[who] = fh.read()
        label.setText(os.path.basename(path))
        self._on_bulk_field_debounced()

    # ── Bulk Flow editor builder ───────────────────────────────────────────────
    def _upload_flow_photo(self, who: str):
        path, _ = QFileDialog.getOpenFileName(
            self, f"Select {who.title()} Photo", str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.bmp *.tiff)")
        if not path: return
        if not hasattr(self, "_flow_photo_bytes"):
            self._flow_photo_bytes = {}
        with open(path, "rb") as fh:
            self._flow_photo_bytes[who] = fh.read()
        lbl = self._flow_pat_photo_lbl if who == "patient" else self._flow_don_photo_lbl
        lbl.setText(os.path.basename(path))
        self._on_manual_field_debounced()

    def _rebuild_bulk_flow_editor(self, idx, case, _old_pat_group, meta_group):
        """Build Flow-specific editor form inside the bulk editor scroll area."""
        p   = case["patient"]
        d   = case["donors"][0] if case.get("donors") else {}
        fr  = case.get("flow_results", {})
        self._bulk_flow_pat_f    = {}
        self._bulk_flow_don_f    = {}
        self._bulk_flow_result_f = {}

        flow_pat_grp = QGroupBox("Patient Information")
        fpf = QFormLayout(); flow_pat_grp.setLayout(fpf)
        fpf.setSpacing(1); fpf.setContentsMargins(4, 2, 4, 2)
        for key, lbl, dflt in [
            ("name","Patient Name *",""), ("gender_age","Gender / Age",""),
            ("pin","PIN",""), ("sample_number","Sample Number",""),
            ("diagnosis","Diagnosis","NA"), ("hospital_clinic","Hospital / Clinic",""),
            ("sample_type","Sample Type","Serum"), ("collection_date","Collection Date",""),
            ("receipt_date","Sample Receipt Date",""), ("report_date","Report Date",""),
            ("remarks","Remarks",""), ("comments","Additional Comments",""),
        ]:
            w = QLineEdit(str(p.get(key, dflt) or dflt)); w.setFixedHeight(24)
            if "date" in key: w.setPlaceholderText("DD-MM-YYYY")
            w.textChanged.connect(self._on_bulk_field_debounced)
            self._bulk_flow_pat_f[key] = w; fpf.addRow(lbl + ":", w)

        _nabl_default = case.get("nabl", self.qsettings.value("nabl_stamp", True, type=bool))
        self._bulk_nabl_chk = QCheckBox("NABL Accreditation")
        self._bulk_nabl_chk.setChecked(_nabl_default)
        self._bulk_nabl_chk.stateChanged.connect(self._on_bulk_field_debounced)
        fpf.addRow(self._bulk_nabl_chk)
        self._bulk_flow_photo_bytes = dict(case.get("_photo_bytes_tmp", {}))
        _bfpp_row = QHBoxLayout()
        _bfpp_lbl = QLabel("No photo selected"); _bfpp_lbl.setStyleSheet("color:gray;font-style:italic;")
        _bfpp_btn = QPushButton("Upload Patient Photo"); _bfpp_btn.setMaximumHeight(26)
        def _upload_flow_pat():
            path, _ = QFileDialog.getOpenFileName(self, "Select Patient Photo", str(Path.home()), "Images (*.png *.jpg *.jpeg *.bmp *.tiff)")
            if path:
                with open(path, "rb") as fh: self._bulk_flow_photo_bytes["patient"] = fh.read()
                _bfpp_lbl.setText(os.path.basename(path)); self._on_bulk_field_debounced()
        _bfpp_btn.clicked.connect(_upload_flow_pat)
        _bfpp_row.addWidget(_bfpp_lbl, 1); _bfpp_row.addWidget(_bfpp_btn)
        fpf.addRow("Patient Photo:", _bfpp_row)

        flow_don_grp = QGroupBox("Donor Information")
        fdf = QFormLayout(); flow_don_grp.setLayout(fdf)
        fdf.setSpacing(1); fdf.setContentsMargins(4, 2, 4, 2)
        for key, lbl, dflt in [
            ("name","Donor Name",""), ("gender_age","Gender / Age",""),
            ("pin","Donor PIN","NA"), ("sample_number","Sample Number","NA"),
            ("relationship","Relationship to Recipient",""),
            ("sample_type","Sample Type","Sodium Heparin Whole Blood"),
            ("collection_date","Collection Date",""), ("receipt_date","Sample Receipt Date",""),
            ("report_date","Report Date",""),
        ]:
            w = QLineEdit(str(d.get(key, dflt) or dflt)); w.setFixedHeight(24)
            if "date" in key: w.setPlaceholderText("DD-MM-YYYY")
            w.textChanged.connect(self._on_bulk_field_debounced)
            self._bulk_flow_don_f[key] = w; fdf.addRow(lbl + ":", w)
        _bfdp_row = QHBoxLayout()
        _bfdp_lbl = QLabel("No photo selected"); _bfdp_lbl.setStyleSheet("color:gray;font-style:italic;")
        _bfdp_btn = QPushButton("Upload Donor Photo"); _bfdp_btn.setMaximumHeight(26)
        def _upload_flow_don():
            path, _ = QFileDialog.getOpenFileName(self, "Select Donor Photo", str(Path.home()), "Images (*.png *.jpg *.jpeg *.bmp *.tiff)")
            if path:
                with open(path, "rb") as fh: self._bulk_flow_photo_bytes["donor"] = fh.read()
                _bfdp_lbl.setText(os.path.basename(path)); self._on_bulk_field_debounced()
        _bfdp_btn.clicked.connect(_upload_flow_don)
        _bfdp_row.addWidget(_bfdp_lbl, 1); _bfdp_row.addWidget(_bfdp_btn)
        fdf.addRow("Donor Photo:", _bfdp_row)

        flow_res_grp = QGroupBox("Flow Results")
        frf = QFormLayout(); flow_res_grp.setLayout(frf)
        frf.setSpacing(1); frf.setContentsMargins(4, 2, 4, 2)
        _FLOW_OPTS = ["Negative", "Borderline", "Positive"]
        for key, lbl in [("t_interpretation","T Cell Crossmatch"), ("b_interpretation","B Cell Crossmatch")]:
            cmb = ClickOnlyComboBox(); cmb.addItems(_FLOW_OPTS); cmb.setFixedHeight(24)
            pos = cmb.findText(fr.get(key, "Negative"))
            if pos >= 0: cmb.setCurrentIndex(pos)
            cmb.currentIndexChanged.connect(self._on_bulk_field_debounced)
            self._bulk_flow_result_f[key] = cmb; frf.addRow(lbl + ":", cmb)
        for key, lbl, ph in [("t_mcs","T Cell MCS Value","<45"), ("b_mcs","B Cell MCS Value","<86")]:
            w = QLineEdit(fr.get(key, "")); w.setFixedHeight(24); w.setPlaceholderText(ph)
            w.textChanged.connect(self._on_bulk_field_debounced)
            self._bulk_flow_result_f[key] = w; frf.addRow(lbl + ":", w)
        _bfi = QLineEdit(fr.get("interpretation", ""))
        _bfi.setFixedHeight(24)
        _bfi.setPlaceholderText("Auto-generated if left blank")
        _bfi.textChanged.connect(self._on_bulk_field_debounced)
        self._bulk_flow_result_f["interpretation"] = _bfi
        frf.addRow("Interpretation Override:", _bfi)

        for grp in (flow_pat_grp, meta_group, flow_don_grp, flow_res_grp):
            self._bulk_editor_layout.addWidget(grp)

        self._bulk_sig_combos = {}
        name_overrides = case.get("sig_name_overrides", {})
        sig_group = QGroupBox("Signature Override")
        sig_form  = QFormLayout(); sig_group.setLayout(sig_form)
        sig_form.setSpacing(2); sig_form.setContentsMargins(4, 2, 4, 2)
        _sig_opts = ["(Use Default)"] + list(hla_assets.SIGN_BY_NAME.keys())
        for i in range(3):
            cmb = ClickOnlyComboBox(); cmb.addItems(_sig_opts); cmb.setFixedHeight(24)
            saved_name = name_overrides.get(i, name_overrides.get(str(i), ""))
            if saved_name:
                pos = cmb.findText(saved_name)
                if pos >= 0: cmb.setCurrentIndex(pos)
            cmb.currentIndexChanged.connect(self._on_bulk_field_debounced)
            self._bulk_sig_combos[i] = cmb; sig_form.addRow(f"Signatory {i+1}:", cmb)
        self._bulk_editor_layout.addWidget(sig_group)
        QTimer.singleShot(200, self._refresh_bulk_preview)

    # ── Bulk SAB editor builder ────────────────────────────────────────────────
    def _rebuild_bulk_sab_editor(self, idx, case, _old_pat_group, meta_group):
        """Build SAB-specific editor form inside the bulk editor scroll area."""
        p = case["patient"]
        self._bulk_sab_pat_f       = {}
        self._bulk_sab_chart_bytes = case.get("sab_chart_bytes")

        sab_pat_grp = QGroupBox("Patient Information")
        spf = QFormLayout(); sab_pat_grp.setLayout(spf)
        spf.setSpacing(1); spf.setContentsMargins(4, 2, 4, 2)
        SAB_PAT = [
            ("name",            "Patient Name *",         ""),
            ("gender_age",      "Gender / Age",           ""),
            ("hospital_mr_no",  "Hospital MR No",         "NA"),
            ("specimen",        "Specimen",               "Serum"),
            ("hospital_clinic", "Hospital / Clinic",      ""),
            ("pin",             "PIN",                    ""),
            ("sample_number",   "Sample Number",          ""),
            ("collection_date", "Sample Collection Date", ""),
            ("receipt_date",    "Sample Receipt Date",    ""),
            ("report_date",     "Report Date",            ""),
            ("remarks",         "Remarks",                ""),
        ]
        for key, lbl, dflt in SAB_PAT:
            w = QLineEdit(str(p.get(key, dflt) or dflt))
            w.setFixedHeight(24)
            if "date" in key: w.setPlaceholderText("DD-MM-YYYY")
            w.textChanged.connect(self._on_bulk_field_debounced)
            self._bulk_sab_pat_f[key] = w
            spf.addRow(lbl + ":", w)

        _nabl_default = case.get("nabl", self.qsettings.value("nabl_stamp", True, type=bool))
        self._bulk_nabl_chk = QCheckBox("NABL Accreditation")
        self._bulk_nabl_chk.setChecked(_nabl_default)
        self._bulk_nabl_chk.stateChanged.connect(self._on_bulk_field_debounced)
        spf.addRow(self._bulk_nabl_chk)

        sab_allele_grp = QGroupBox("Allele Data  (one per line:  Allele,MFI)")
        saf = QVBoxLayout(); sab_allele_grp.setLayout(saf)
        saf.setContentsMargins(4, 2, 4, 2)
        self._bulk_sab_allele_edit = QTextEdit()
        self._bulk_sab_allele_edit.setPlaceholderText("A*01:01,2126\nA*36:01,992\n...")
        self._bulk_sab_allele_edit.setFixedHeight(140)
        existing_alleles = case.get("sab_alleles", [])
        if existing_alleles:
            self._bulk_sab_allele_edit.setPlainText(
                "\n".join(f"{a},{m}" for a, m in existing_alleles))
        self._bulk_sab_allele_edit.textChanged.connect(self._on_bulk_field_debounced)
        saf.addWidget(self._bulk_sab_allele_edit)

        _chart_row = QHBoxLayout()
        _chart_lbl = QLabel("No chart selected"); _chart_lbl.setStyleSheet("color:gray;font-style:italic;")
        _chart_btn = QPushButton("Upload Chart Image"); _chart_btn.setMaximumHeight(26)
        def _upload():
            path, _ = QFileDialog.getOpenFileName(self, "Select Chart Image", str(Path.home()),
                                                  "Images (*.png *.jpg *.jpeg *.bmp *.tiff)")
            if path:
                with open(path, "rb") as fh: self._bulk_sab_chart_bytes = fh.read()
                _chart_lbl.setText(os.path.basename(path))
                self._on_bulk_field_debounced()
        _chart_btn.clicked.connect(_upload)
        _chart_row.addWidget(_chart_lbl, 1); _chart_row.addWidget(_chart_btn)
        saf.addLayout(_chart_row)

        _class_row = QHBoxLayout()
        _class_lbl = QLabel("SAB Class:")
        self._bulk_sab_class_combo = ClickOnlyComboBox()
        self._bulk_sab_class_combo.addItems(["I", "II"])
        _cur_class = "II" if case.get("sab_class") == "II" else "I"
        self._bulk_sab_class_combo.setCurrentText(_cur_class)
        self._bulk_sab_class_combo.setFixedHeight(24)
        self._bulk_sab_class_combo.currentIndexChanged.connect(self._on_bulk_field_debounced)
        _class_row.addWidget(_class_lbl); _class_row.addWidget(self._bulk_sab_class_combo, 1)
        saf.addLayout(_class_row)

        for grp in (sab_pat_grp, meta_group, sab_allele_grp):
            self._bulk_editor_layout.addWidget(grp)

        self._bulk_sig_combos = {}
        name_overrides = case.get("sig_name_overrides", {})
        sig_group = QGroupBox("Signature Override")
        sig_form  = QFormLayout(); sig_group.setLayout(sig_form)
        sig_form.setSpacing(2); sig_form.setContentsMargins(4, 2, 4, 2)
        _sig_opts = ["(Use Default)"] + list(hla_assets.SIGN_BY_NAME.keys())
        for i in range(3):
            cmb = ClickOnlyComboBox(); cmb.addItems(_sig_opts); cmb.setFixedHeight(24)
            saved_name = name_overrides.get(i, name_overrides.get(str(i), ""))
            if saved_name:
                pos = cmb.findText(saved_name)
                if pos >= 0: cmb.setCurrentIndex(pos)
            cmb.currentIndexChanged.connect(self._on_bulk_field_debounced)
            self._bulk_sig_combos[i] = cmb
            sig_form.addRow(f"Signatory {i+1}:", cmb)
        self._bulk_editor_layout.addWidget(sig_group)

        QTimer.singleShot(200, self._refresh_bulk_preview)

    # ── Bulk Luminex editor builder ───────────────────────────────────────────
    def _rebuild_bulk_luminex_editor(self, idx, case, _old_pat_group, meta_group):
        """Build Luminex-specific editor form inside the bulk editor scroll area."""
        p   = case["patient"]
        don = case["donors"][0] if case.get("donors") else {}
        self._bulk_lx_pat_f       = {}
        self._bulk_lx_don_f       = {}
        self._bulk_lx_pat_hla     = {}
        self._bulk_lx_don_hla     = {}
        self._bulk_lx_pat_photo   = case.get("luminex_pat_photo")
        self._bulk_lx_don_photo   = case.get("luminex_don_photo")

        lx_pat_grp = QGroupBox("Patient Information")
        lpf = QFormLayout(); lx_pat_grp.setLayout(lpf)
        lpf.setSpacing(1); lpf.setContentsMargins(4, 2, 4, 2)
        for key, lbl, dflt in [
            ("patient_name",    "Patient Name *",      ""),
            ("gender_age",      "Gender / Age",        ""),
            ("pin",             "PIN",                 ""),
            ("sample_number",   "Sample Number",       ""),
            ("diagnosis",       "Diagnosis",           "NA"),
            ("hospital_clinic", "Hospital / Clinic",   ""),
            ("receipt_date",    "Sample Receipt Date", ""),
            ("report_date",     "Report Date",         ""),
            ("relation",        "Relation",            "Patient"),
            ("sample_type",     "Sample Type",         "EDTA Blood"),
            ("collection_date", "Date of Collection",  ""),
        ]:
            src_key = "name" if key == "patient_name" else key
            w = QLineEdit(str(p.get(src_key, dflt) or dflt))
            w.setFixedHeight(24)
            if "date" in key: w.setPlaceholderText("DD-MM-YYYY")
            w.textChanged.connect(self._on_bulk_field_debounced)
            self._bulk_lx_pat_f[key] = w
            lpf.addRow(lbl + ":", w)
        _nabl_default = case.get("nabl", self.qsettings.value("nabl_stamp", True, type=bool))
        self._bulk_lx_nabl_chk = QCheckBox("NABL Accreditation")
        self._bulk_lx_nabl_chk.setChecked(_nabl_default)
        self._bulk_lx_nabl_chk.stateChanged.connect(self._on_bulk_field_debounced)
        lpf.addRow(self._bulk_lx_nabl_chk)
        _pp_row = QHBoxLayout()
        _pp_lbl = QLabel("No photo" if not self._bulk_lx_pat_photo else "Photo loaded")
        _pp_lbl.setStyleSheet("color:gray;font-style:italic;")
        _pp_btn = QPushButton("Patient Photo"); _pp_btn.setMaximumHeight(24)
        def _load_pp():
            path, _ = QFileDialog.getOpenFileName(self, "Patient Photo", str(Path.home()),
                                                  "Images (*.png *.jpg *.jpeg *.bmp *.tiff)")
            if path:
                with open(path, "rb") as fh: self._bulk_lx_pat_photo = fh.read()
                _pp_lbl.setText(os.path.basename(path)); self._on_bulk_field_debounced()
        _pp_btn.clicked.connect(_load_pp)
        _pp_row.addWidget(_pp_lbl, 1); _pp_row.addWidget(_pp_btn)
        lpf.addRow("Patient Photo:", _pp_row)

        lx_don_grp = QGroupBox("Donor Information")
        ldf = QFormLayout(); lx_don_grp.setLayout(ldf)
        ldf.setSpacing(1); ldf.setContentsMargins(4, 2, 4, 2)
        for key, lbl, dflt in [
            ("name",            "Donor Name *",        ""),
            ("gender_age",      "Gender / Age",        ""),
            ("pin",             "PIN",                 ""),
            ("sample_number",   "Sample Number",       ""),
            ("relation",        "Relation",            ""),
            ("sample_type",     "Sample Type",         "EDTA Blood"),
            ("collection_date", "Date of Collection",  ""),
        ]:
            w = QLineEdit(str(don.get(key, dflt) or dflt))
            w.setFixedHeight(24)
            if "date" in key: w.setPlaceholderText("DD-MM-YYYY")
            w.textChanged.connect(self._on_bulk_field_debounced)
            self._bulk_lx_don_f[key] = w
            ldf.addRow(lbl + ":", w)
        _dp_row = QHBoxLayout()
        _dp_lbl = QLabel("No photo" if not self._bulk_lx_don_photo else "Photo loaded")
        _dp_lbl.setStyleSheet("color:gray;font-style:italic;")
        _dp_btn = QPushButton("Donor Photo"); _dp_btn.setMaximumHeight(24)
        def _load_dp():
            path, _ = QFileDialog.getOpenFileName(self, "Donor Photo", str(Path.home()),
                                                  "Images (*.png *.jpg *.jpeg *.bmp *.tiff)")
            if path:
                with open(path, "rb") as fh: self._bulk_lx_don_photo = fh.read()
                _dp_lbl.setText(os.path.basename(path)); self._on_bulk_field_debounced()
        _dp_btn.clicked.connect(_load_dp)
        _dp_row.addWidget(_dp_lbl, 1); _dp_row.addWidget(_dp_btn)
        ldf.addRow("Donor Photo:", _dp_row)

        pat_hla = p.get("hla", {})
        don_hla = don.get("hla", {})
        lx_hla_grp = QGroupBox("HLA Alleles  (Patient | Donor per locus)")
        lhf = QFormLayout(); lx_hla_grp.setLayout(lhf)
        lhf.setSpacing(1); lhf.setContentsMargins(4, 2, 4, 2)
        for locus in HLA_LOCI:
            row_w = QWidget(); row_l = QHBoxLayout(row_w)
            row_l.setContentsMargins(0,0,0,0); row_l.setSpacing(4)
            pa = pat_hla.get(locus, ["",""])
            da = don_hla.get(locus, ["",""])
            pa1 = QLineEdit(pa[0] if pa else ""); pa1.setFixedWidth(72); pa1.setFixedHeight(22)
            pa2 = QLineEdit(pa[1] if len(pa)>1 else ""); pa2.setFixedWidth(72); pa2.setFixedHeight(22)
            sep = QLabel("|"); sep.setStyleSheet("color:gray;")
            da1 = QLineEdit(da[0] if da else ""); da1.setFixedWidth(72); da1.setFixedHeight(22)
            da2 = QLineEdit(da[1] if len(da)>1 else ""); da2.setFixedWidth(72); da2.setFixedHeight(22)
            row_l.addWidget(pa1); row_l.addWidget(pa2); row_l.addWidget(sep)
            row_l.addWidget(da1); row_l.addWidget(da2); row_l.addStretch()
            for ew in (pa1, pa2, da1, da2): ew.textChanged.connect(self._on_bulk_field_debounced)
            self._bulk_lx_pat_hla[locus] = [pa1, pa2]
            self._bulk_lx_don_hla[locus] = [da1, da2]
            lhf.addRow(f"{locus}:", row_w)

        lx_interp_grp = QGroupBox("Interpretation")
        lif = QVBoxLayout(); lx_interp_grp.setLayout(lif)
        lif.setContentsMargins(4, 2, 4, 2)
        self._bulk_lx_interp_edit = QTextEdit()
        self._bulk_lx_interp_edit.setPlaceholderText("Interpretation text…")
        self._bulk_lx_interp_edit.setFixedHeight(60)
        self._bulk_lx_interp_edit.setPlainText(case.get("luminex_interpretation", ""))
        self._bulk_lx_interp_edit.textChanged.connect(self._on_bulk_field_debounced)
        lif.addWidget(self._bulk_lx_interp_edit)

        for grp in (lx_pat_grp, meta_group, lx_don_grp, lx_hla_grp, lx_interp_grp):
            self._bulk_editor_layout.addWidget(grp)

        self._bulk_sig_combos = {}
        name_overrides = case.get("sig_name_overrides", {})
        sig_group = QGroupBox("Signature Override")
        sig_form  = QFormLayout(); sig_group.setLayout(sig_form)
        sig_form.setSpacing(2); sig_form.setContentsMargins(4, 2, 4, 2)
        _sig_opts = ["(Use Default)"] + list(hla_assets.SIGN_BY_NAME.keys())
        for i in range(3):
            cmb = ClickOnlyComboBox(); cmb.addItems(_sig_opts); cmb.setFixedHeight(24)
            saved = name_overrides.get(i, name_overrides.get(str(i), ""))
            if saved:
                pos = cmb.findText(saved)
                if pos >= 0: cmb.setCurrentIndex(pos)
            cmb.currentIndexChanged.connect(self._on_bulk_field_debounced)
            self._bulk_sig_combos[i] = cmb
            sig_form.addRow(f"Signatory {i+1}:", cmb)
        self._bulk_editor_layout.addWidget(sig_group)
        QTimer.singleShot(200, self._refresh_bulk_preview)

    def _flush_bulk_edits(self, idx):
        """Read current form field values and write back to cases[idx]."""
        if idx < 0 or idx >= len(self.cases): return
        case = self.cases[idx]
        p    = case["patient"]

        # ── CDC path ────────────────────────────────────────────────────────
        if case.get("report_type") == "cdc_crossmatch":
            # Guard: _bulk_cdc_photo_bytes may not be initialised if the editor
            # was never rebuilt for this case (e.g. first flush before first render).
            _cdc_photos = getattr(self, "_bulk_cdc_photo_bytes", {})
            if hasattr(self, "_bulk_cdc_pat_f"):
                for key, w in self._bulk_cdc_pat_f.items():
                    p[key] = w.text().strip()
                p["photo_bytes"] = _cdc_photos.get("patient")
            if case.get("donors") and hasattr(self, "_bulk_cdc_don_f"):
                d = case["donors"][0]
                for key, w in self._bulk_cdc_don_f.items():
                    d[key] = w.text().strip()
                d["photo_bytes"] = _cdc_photos.get("donor")
            if hasattr(self, "_bulk_cdc_result_f"):
                case["cdc_results"] = {k: w.currentText()
                                       for k, w in self._bulk_cdc_result_f.items()}
            # For specialised types, keep report_type locked to cdc_crossmatch
            # unless the per-case rtype_combo was explicitly changed.
            if hasattr(self, "_bulk_rtype_combo") and self._bulk_rtype_combo is not None:
                new_rtype = TEMPLATE_TO_RTYPE.get(
                    self._bulk_rtype_combo.currentText(), "cdc_crossmatch")
                # Only allow change within crossmatch/compatible types, not to
                # HLA typing types, to prevent accidental type corruption.
                case["report_type"] = new_rtype
            case["with_logo"] = self.logo_combo.currentText() == "With Logo"
            if hasattr(self, "_bulk_nabl_chk") and self._bulk_nabl_chk is not None:
                case["nabl"] = self._bulk_nabl_chk.isChecked()
            return

        # ── DSA path ────────────────────────────────────────────────────────
        if case.get("report_type") == "dsa_crossmatch":
            # Guard: _bulk_dsa_photo_bytes may not be initialised if the editor
            # was never rebuilt for this case (e.g. first flush before first render).
            _dsa_photos = getattr(self, "_bulk_dsa_photo_bytes", {})
            if hasattr(self, "_bulk_dsa_pat_f"):
                for key, w in self._bulk_dsa_pat_f.items():
                    p[key] = w.text().strip()
                p["photo_bytes"] = _dsa_photos.get("patient")
            if case.get("donors") and hasattr(self, "_bulk_dsa_don_f"):
                d = case["donors"][0]
                for key, w in self._bulk_dsa_don_f.items():
                    d[key] = w.text().strip()
                d["photo_bytes"] = _dsa_photos.get("donor")
            if hasattr(self, "_bulk_dsa_result_f"):
                dsa_res = {}
                for k, w in self._bulk_dsa_result_f.items():
                    if isinstance(w, QComboBox):
                        dsa_res[k] = w.currentText()
                    else:
                        dsa_res[k] = w.text().strip()
                case["dsa_results"] = dsa_res
            # Keep report_type locked to dsa_crossmatch unless per-case combo changed.
            if hasattr(self, "_bulk_rtype_combo") and self._bulk_rtype_combo is not None:
                case["report_type"] = TEMPLATE_TO_RTYPE.get(
                    self._bulk_rtype_combo.currentText(), "dsa_crossmatch")
            case["with_logo"] = self.logo_combo.currentText() == "With Logo"
            if hasattr(self, "_bulk_nabl_chk") and self._bulk_nabl_chk is not None:
                case["nabl"] = self._bulk_nabl_chk.isChecked()
            return

        # ── SAB path ────────────────────────────────────────────────────────
        if case.get("report_type") in ("sab_class1", "sab_class2"):
            if hasattr(self, "_bulk_sab_pat_f"):
                for key, w in self._bulk_sab_pat_f.items():
                    p[key] = w.text().strip()
            if hasattr(self, "_bulk_sab_allele_edit"):
                case["sab_alleles"] = self._parse_sab_allele_text_static(
                    self._bulk_sab_allele_edit.toPlainText())
            if hasattr(self, "_bulk_sab_chart_bytes"):
                case["sab_chart_bytes"] = self._bulk_sab_chart_bytes
            if hasattr(self, "_bulk_sab_class_combo"):
                case["sab_class"] = self._bulk_sab_class_combo.currentText()
            if hasattr(self, "_bulk_rtype_combo") and self._bulk_rtype_combo is not None:
                case["report_type"] = TEMPLATE_TO_RTYPE.get(
                    self._bulk_rtype_combo.currentText(), "sab_class1")
            case["with_logo"] = self.logo_combo.currentText() == "With Logo"
            if hasattr(self, "_bulk_nabl_chk") and self._bulk_nabl_chk is not None:
                case["nabl"] = self._bulk_nabl_chk.isChecked()
            return

        # ---- Flow path ----------------------------------------------------------
        if case.get("report_type") == "flow_crossmatch":
            if hasattr(self, "_bulk_flow_pat_f"):
                for key, w in self._bulk_flow_pat_f.items():
                    p[key] = w.text().strip()
                p["photo_bytes"] = self._bulk_flow_photo_bytes.get("patient") if hasattr(self, "_bulk_flow_photo_bytes") else None
            if hasattr(self, "_bulk_flow_don_f") and case.get("donors"):
                d = case["donors"][0]
                for key, w in self._bulk_flow_don_f.items():
                    d[key] = w.text().strip()
                d["photo_bytes"] = self._bulk_flow_photo_bytes.get("donor") if hasattr(self, "_bulk_flow_photo_bytes") else None
            if hasattr(self, "_bulk_flow_result_f"):
                fr = {}
                for k, w in self._bulk_flow_result_f.items():
                    fr[k] = w.currentText() if isinstance(w, QComboBox) else w.text().strip()
                case["flow_results"] = fr
            if hasattr(self, "_bulk_rtype_combo") and self._bulk_rtype_combo is not None:
                case["report_type"] = TEMPLATE_TO_RTYPE.get(self._bulk_rtype_combo.currentText(), "flow_crossmatch")
            case["with_logo"] = self.logo_combo.currentText() == "With Logo"
            if hasattr(self, "_bulk_nabl_chk") and self._bulk_nabl_chk is not None:
                case["nabl"] = self._bulk_nabl_chk.isChecked()
            return

        # ── Luminex path ─────────────────────────────────────────────────────
        if case.get("report_type") == "luminex_typing":
            if hasattr(self, "_bulk_lx_pat_f"):
                for key, w in self._bulk_lx_pat_f.items():
                    dest = "name" if key == "patient_name" else key
                    p[dest] = w.text().strip()
                p["hla"] = {locus: [w.text().strip() for w in ws]
                            for locus, ws in self._bulk_lx_pat_hla.items()
                            if hasattr(self, "_bulk_lx_pat_hla")}
                case["luminex_pat_photo"] = getattr(self, "_bulk_lx_pat_photo", None)
            if hasattr(self, "_bulk_lx_don_f") and case.get("donors"):
                d = case["donors"][0]
                for key, w in self._bulk_lx_don_f.items():
                    d[key] = w.text().strip()
                d["hla"] = {locus: [w.text().strip() for w in ws]
                            for locus, ws in self._bulk_lx_don_hla.items()
                            if hasattr(self, "_bulk_lx_don_hla")}
                case["luminex_don_photo"] = getattr(self, "_bulk_lx_don_photo", None)
            if hasattr(self, "_bulk_lx_interp_edit"):
                case["luminex_interpretation"] = self._bulk_lx_interp_edit.toPlainText().strip()
            if hasattr(self, "_bulk_rtype_combo") and self._bulk_rtype_combo is not None:
                case["report_type"] = TEMPLATE_TO_RTYPE.get(
                    self._bulk_rtype_combo.currentText(), "luminex_typing")
            case["with_logo"] = self.logo_combo.currentText() == "With Logo"
            if hasattr(self, "_bulk_lx_nabl_chk") and self._bulk_lx_nabl_chk is not None:
                case["nabl"] = self._bulk_lx_nabl_chk.isChecked()
            return

        if not self._bulk_fields: return

        # Patient fields — write every text widget back to case["patient"]
        for key, w in self._bulk_fields.items():
            p[key] = w.text().strip()

        # Patient HLA
        if not isinstance(p.get("hla"), dict): p["hla"] = {}
        for locus, (a1, a2) in self._bulk_hla_pat.items():
            v1 = a1.text().strip(); v2 = a2.text().strip()
            if v1 or v2: p["hla"][locus] = [v1, v2]
            else:        p["hla"].pop(locus, None)

        # Report meta — per-case combo is authoritative for report_type; global logo combo for logo
        if hasattr(self, "_bulk_rtype_combo") and self._bulk_rtype_combo is not None:
            case["report_type"] = TEMPLATE_TO_RTYPE.get(self._bulk_rtype_combo.currentText(), "single_hla")
        else:
            case["report_type"] = TEMPLATE_TO_RTYPE.get(self.template_combo.currentText(), "single_hla")
        case["with_logo"]   = self.logo_combo.currentText() == "With Logo"
        if hasattr(self, "_bulk_nabl_chk") and self._bulk_nabl_chk is not None:
            case["nabl"] = self._bulk_nabl_chk.isChecked()

        if hasattr(self, "_bulk_ts_edit"):
            case["typing_status"] = self._bulk_ts_edit.text().strip()
        if hasattr(self, "_bulk_imgt_edit"):
            case["imgt_release"] = self._bulk_imgt_edit.text().strip()
        if hasattr(self, "_bulk_meth_edit"):
            case["methodology"] = self._bulk_meth_edit.text().strip()
        if hasattr(self, "_bulk_cov_edit"):
            case["coverage"] = self._bulk_cov_edit.text().strip()

        # Donors
        for di, (d_fields, d_hla) in enumerate(
                zip(self._bulk_donor_fields, self._bulk_hla_don)):
            if di >= len(case.get("donors", [])): break
            d = case["donors"][di]
            for key, w in d_fields.items():
                d[key] = w.text().strip()
            if not isinstance(d.get("hla"), dict): d["hla"] = {}
            for locus, (a1, a2) in d_hla.items():
                v1 = a1.text().strip(); v2 = a2.text().strip()
                if v1 or v2: d["hla"][locus] = [v1, v2]
                else:        d["hla"].pop(locus, None)

        # RPL / Fertility manual overrides
        if case["report_type"] == "rpl_couple" and hasattr(self, "_bulk_rpl_fields"):
            if "rpl_reference" not in case: case["rpl_reference"] = {}
            ref = case["rpl_reference"]
            for key, w in self._bulk_rpl_fields.items():
                ref[key] = w.text().strip()
            # Also sync patient-level hla_c_type used by template
            case["patient"]["hla_c_type"] = ref.get("hla_c_patient", "")
            if case.get("donors"):
                case["donors"][0]["hla_c_type"] = ref.get("hla_c_donor", "")

        # ── Real-time Recalculation Logic ───────────────────────────────────
        # If alleles changed, update the CALCULATED fields in the UI.
        # We detect change by comparing current HLA to a cached version.
        current_hla_str = json.dumps(case["patient"].get("hla", {})) + \
                          json.dumps([d.get("hla", {}) for d in case.get("donors", [])]) + \
                          json.dumps([d.get("match", "") for d in case.get("donors", [])])
        
        last_hla = case.get("_last_hla_sync", "")
        if current_hla_str != last_hla:
            case["_last_hla_sync"] = current_hla_str
            if case["report_type"] == "rpl_couple" and case.get("donors"):
                # Recalculate patient C-type
                pc = case["patient"]["hla"].get("C", [None, None])
                ct1 = c_supertype(pc[0]) if pc[0] else None
                ct2 = c_supertype(pc[1]) if pc[1] else None
                new_pc_type = ",".join(filter(None, [ct1, ct2]))
                
                # Recalculate donor C-type
                dc = case["donors"][0]["hla"].get("C", [None, None])
                dt1 = c_supertype(dc[0]) if dc[0] else None
                dt2 = c_supertype(dc[1]) if dc[1] else None
                new_dc_type = ",".join(filter(None, [dt1, dt2]))
                
                # Recalculate Match Stats
                new_ref = compute_rpl_reference(case["patient"], case["donors"][0])
                new_ref["hla_c_patient"] = new_pc_type
                new_ref["hla_c_donor"]   = new_dc_type
                
                # Update UI fields and data (triggers debounce again, which is fine)
                if hasattr(self, "_bulk_rpl_fields"):
                    for key, val in new_ref.items():
                        if key in self._bulk_rpl_fields:
                            w = self._bulk_rpl_fields[key]
                            if w.text().strip() != val: # only if changed to allow manual edits to stick if they match
                                w.blockSignals(True)
                                w.setText(val)
                                w.blockSignals(False)
                                case["rpl_reference"][key] = val
                case["patient"]["hla_c_type"] = new_pc_type
                case["donors"][0]["hla_c_type"] = new_dc_type

        # self._bulk_log(f"Edits applied to case {idx+1}: {case['patient'].get('name','')}")
        # Message removed as per user request (Image 2)

    # ── Bulk donor add/remove ──────────────────────────────────────────────────
    def _add_bulk_donor(self, case_idx):
        """Add an empty donor to case and rebuild the editor."""
        if case_idx < 0 or case_idx >= len(self.cases): return
        self._flush_bulk_edits(case_idx)
        case = self.cases[case_idx]
        if "donors" not in case: case["donors"] = []
        patient = case.get("patient", {})
        case["donors"].append({
            "name": "", "relationship": "", "gender_age": "",
            "hospital_mr_no": "NA",
            "pin": "", "sample_number": "", "match": "",
            "remarks": "",
            "collection_date": "", "receipt_date": "",
            "report_date": patient.get("report_date", ""),
            "hla": {}, "hla_c_type": "",
            "hospital_clinic": patient.get("hospital_clinic", ""),
            "diagnosis":       patient.get("diagnosis", ""),
            "specimen":        patient.get("specimen", "Blood - EDTA"),
        })
        # Auto-switch: a case with donors cannot stay as single_hla
        if case.get("report_type", "single_hla") == "single_hla":
            case["report_type"] = "transplant_donor"
        self._rebuild_bulk_editor(case_idx)

    def _remove_bulk_donor(self, case_idx, donor_idx):
        """Remove a donor from a case and rebuild the editor."""
        if case_idx < 0 or case_idx >= len(self.cases): return
        self._flush_bulk_edits(case_idx)
        case = self.cases[case_idx]
        donors = case.get("donors", [])
        if 0 <= donor_idx < len(donors):
            donors.pop(donor_idx)
        self._rebuild_bulk_editor(case_idx)

    # ── Bulk signature name-override helpers ──────────────────────────────────
    def _on_bulk_sig_changed(self, case_idx: int, slot: int, text: str):
        """Called when the user changes a signature-override dropdown in the Bulk editor."""
        if case_idx < 0 or case_idx >= len(self.cases):
            return
        if "sig_name_overrides" not in self.cases[case_idx]:
            self.cases[case_idx]["sig_name_overrides"] = {}
        if text == "(Use Default)":
            self.cases[case_idx]["sig_name_overrides"].pop(slot, None)
            self.cases[case_idx]["sig_name_overrides"].pop(str(slot), None)
        else:
            self.cases[case_idx]["sig_name_overrides"][slot] = text

    def _on_global_pref_changed(self):
        """Called when Template or Logo selection in the global header changes."""
        idx = self.tabs.currentIndex()
        if idx == 0: # Manual Tab
            # Sync per-case combo with global selection
            if hasattr(self, "_manual_rtype_combo"):
                ti = self.template_combo.currentIndex()
                self._manual_rtype_combo.blockSignals(True)
                self._manual_rtype_combo.setCurrentIndex(ti)
                self._manual_rtype_combo.blockSignals(False)
            self._update_manual_rpl_visibility()
            self._refresh_manual_preview()
        elif idx == 1: # Bulk Tab
            b_idx = self._bulk_current_row
            if 0 <= b_idx < len(self.cases):
                case = self.cases[b_idx]
                # Allow full manual override — auto-detection sets the initial type
                # but the user can switch to any template at any time.
                new_rtype = TEMPLATE_TO_RTYPE.get(
                    self.template_combo.currentText(), "single_hla")
                case["report_type"] = new_rtype
                case["with_logo"] = self.logo_combo.currentText() == "With Logo"
                # Rebuild editor so form layout matches the newly selected type.
                self._rebuild_bulk_editor(b_idx)
            self._refresh_bulk_preview()

    def _load_bulk_preview(self, pdf_path: str):
        """Render a PDF into the bulk preview panel using fitz (PGTA TERA-style)."""
        while self._bulk_preview_vbox.count():
            child = self._bulk_preview_vbox.takeAt(0)
            if child.widget(): child.widget().deleteLater()
        if not FITZ_OK or not os.path.exists(pdf_path):
            lbl = QLabel("Preview not available")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._bulk_preview_vbox.addWidget(lbl)
            self.bulk_preview_status.setText("Preview not available")
            return
        pixmaps = _render_pdf_pages(pdf_path, width_px=600)
        if pixmaps:
            for pm in pixmaps:
                lbl = QLabel(); lbl.setPixmap(pm)
                lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
                lbl.setStyleSheet("margin: 6px; border: 1px solid #ccc;")
                self._bulk_preview_vbox.addWidget(lbl)
            self.bulk_preview_status.setText(f"Preview ready — {len(pixmaps)} page(s)")
        else:
            self.bulk_preview_status.setText("Preview unavailable")

    def _refresh_bulk_preview(self):
        """Generate a temp preview for the currently selected bulk case in background."""
        idx = self._bulk_current_row
        if idx < 0 or idx >= len(self.cases):
            self.bulk_preview_status.setText("No case selected")
            return
        self._flush_bulk_edits(idx)
        case = self.cases[idx]
        with_logo = self.logo_combo.currentText() == "With Logo"
        nabl      = self.qsettings.value("nabl_stamp", True, type=bool)
        sig_stamp = self.qsettings.value("signature_stamp", False, type=bool)
        c = self._build_case(
            case.get("report_type", "single_hla"),
            case.get("nabl", nabl), with_logo, sig_stamp,
            case["patient"], case.get("donors", [])
        )
        # Apply any per-case signature overrides
        self._apply_sig_name_overrides(c, case.get("sig_name_overrides", {}))
        # Copy case-level overrides including all result types
        for key in ("imgt_release", "methodology", "typing_status", "coverage",
                    "rpl_reference", "cdc_results", "dsa_results",
                    "flow_results", "sab_alleles", "sab_chart_bytes", "sab_class"):
            if case.get(key):
                c[key] = case[key]
        # Carry photo bytes into the preview case patient/donor dicts
        if case.get("report_type") in ("cdc_crossmatch", "dsa_crossmatch", "flow_crossmatch"):
            c["patient"]["photo_bytes"] = case["patient"].get("photo_bytes")
            if c.get("donors") and case.get("donors"):
                c["donors"][0]["photo_bytes"] = case["donors"][0].get("photo_bytes")
        tmp = TEMP_PREVIEW_PATH.replace(".pdf", "_bulk.pdf")
        self.bulk_preview_status.setText("Generating preview…")
        if hasattr(self, "_bulk_preview_worker") and self._bulk_preview_worker \
                and self._bulk_preview_worker.isRunning():
            self._bulk_preview_worker.finished.disconnect()
            self._bulk_preview_worker.terminate()
        self._bulk_preview_worker = PreviewWorker(c, tmp)
        self._bulk_preview_worker.finished.connect(self._load_bulk_preview)
        self._bulk_preview_worker.error.connect(
            lambda e: self.bulk_preview_status.setText(f"Preview error: {e}"))
        self._bulk_preview_worker.start()

    def _select_all(self):
        for i in range(self.bulk_list.count()):
            self.bulk_list.item(i).setCheckState(Qt.CheckState.Checked)

    def _select_none(self):
        for i in range(self.bulk_list.count()):
            self.bulk_list.item(i).setCheckState(Qt.CheckState.Unchecked)

    def _on_sel_all_toggled(self):
        """Toggle between Select All and Deselect All on button click."""
        total = self.bulk_list.count()
        checked = sum(
            1 for i in range(total)
            if self.bulk_list.item(i).checkState() == Qt.CheckState.Checked
        )
        # If all are already checked, deselect all; otherwise select all
        if checked == total:
            self._select_none()
        else:
            self._select_all()

    def _on_bulk_check_changed(self, _item):
        """Sync the Select All button label whenever a list item checkbox changes."""
        total = self.bulk_list.count()
        checked_count = sum(
            1 for i in range(total)
            if self.bulk_list.item(i).checkState() == Qt.CheckState.Checked
        )
        if checked_count == total:
            self._sel_all_btn.setText("Deselect All")
        else:
            self._sel_all_btn.setText("Select All")

    def _save_cases_as_drafts(self, cases: list, stacked_prefix: str) -> tuple:
        """Save each case as an individual file AND as one stacked file.
        Returns (saved_individual, stacked_filename, failed_list).
        """
        os.makedirs(DRAFTS_DIR, exist_ok=True)
        today = datetime.date.today().strftime("%Y%m%d")
        saved, failed = [], []

        # ── Individual files ──────────────────────────────────────────────────
        for case in cases:
            name_val       = case.get("patient", {}).get("name", "Unknown")
            safe_name      = _re.sub(r"[^\w\-]", "_", name_val)
            rtype          = case.get("report_type", "single_hla")
            template_label = _re.sub(r"[^\w\-]", "_", RTYPE_TO_TEMPLATE.get(rtype, rtype))
            filename       = f"{safe_name}_{template_label}_draft.json"
            path      = os.path.join(DRAFTS_DIR, filename)
            draft     = {k: v for k, v in case.items() if k != "signatories"}
            try:
                with open(path, "w") as fh: json.dump(draft, fh, indent=2, default=str)
                saved.append(filename)
            except Exception as e:
                failed.append(f"{name_val}: {e}")

        # ── Stacked file (all cases in one list) ──────────────────────────────
        stacked_filename = f"{stacked_prefix}_stacked_{today}.json"
        stacked_path     = os.path.join(DRAFTS_DIR, stacked_filename)
        all_drafts       = [{k: v for k, v in c.items() if k != "signatories"} for c in cases]
        try:
            with open(stacked_path, "w") as fh: json.dump(all_drafts, fh, indent=2, default=str)
        except Exception as e:
            stacked_filename = None
            failed.append(f"stacked file: {e}")

        return saved, stacked_filename, failed

    def save_bulk_selected_draft(self):
        """Save drafts only for the currently checked/selected patients."""
        self._flush_bulk_edits(self._bulk_current_row)
        checked = [
            self.cases[self.bulk_list.item(i).data(Qt.ItemDataRole.UserRole)]
            for i in range(self.bulk_list.count())
            if self.bulk_list.item(i).checkState() == Qt.CheckState.Checked
        ]
        if not checked:
            QMessageBox.warning(self, "No Selection", "No patients are checked.")
            return
        saved, stacked, failed = self._save_cases_as_drafts(checked, "selected")
        msg = f"Saved {len(saved)} individual draft(s) to /drafts."
        if stacked:
            msg += f"\nStacked file: {stacked}"
        if failed:
            msg += f"\n{len(failed)} failed: " + "; ".join(failed)
        self._bulk_log(msg)
        QMessageBox.information(self, "Drafts Saved", msg)

    def _get_checked_cases(self):
        return [self.cases[self.bulk_list.item(i).data(Qt.ItemDataRole.UserRole)]
                for i in range(self.bulk_list.count())
                if self.bulk_list.item(i).checkState() == Qt.CheckState.Checked]

    def generate_bulk(self):
        # Flush any unsaved edits for the currently displayed case
        self._flush_bulk_edits(self._bulk_current_row)

        cases = self._get_checked_cases()
        if not cases:
            QMessageBox.warning(self, "No Selection", "Please select at least one case.")
            return
        out = self.bulk_output_label.text()
        if out == "No folder selected":
            QMessageBox.warning(self, "No Output", "Please select an output folder.")
            return

        with_logo  = self.logo_combo.currentText() == "With Logo"
        sigs       = self._get_signatories()
        sig_single = self.qsettings.value("sig_count_single", 3, type=int)
        sig_donor  = self.qsettings.value("sig_count_donor",  2, type=int)
        sig_stamp  = self.qsettings.value("signature_stamp", False, type=bool)

        self.bulk_status_label.setText("Generating…")
        self.bulk_log.clear()

        self.worker = GenerateWorker(
            cases, out, with_logo, sigs, sig_single, sig_donor, sig_stamp
        )
        self.worker.progress.connect(self._on_bulk_progress)
        self.worker.finished.connect(self._on_bulk_done)
        self.worker.error.connect(lambda m: self._bulk_log(f"ERROR: {m}"))
        self.worker.start()

    def _on_bulk_progress(self, pct, msg):
        self.bulk_status_label.setText(msg)
        self._bulk_log(f"  {msg}")

    def _on_bulk_done(self, success, failed):
        self.bulk_status_label.setText(
            f"✓ Done — {len(success)} generated, {len(failed)} failed.")
        self._bulk_log(f"\n✓ Complete. {len(success)} reports saved.")
        msg = f"Generated {len(success)} report(s).\nOutput: {self.bulk_output_label.text()}"
        if failed:
            errors = "\n".join(f"• {f[0]}: {f[1]}" for f in failed)
            msg += f"\n\nFailed ({len(failed)}):\n{errors}"
        QMessageBox.information(self, "Done", msg)
        # Status bar message removed as requested


    def _bulk_log(self, msg):
        self.bulk_log.append(msg)

    def save_bulk_draft(self):
        if not self.cases:
            QMessageBox.warning(self, "No Cases", "No cases loaded to save.")
            return
        self._flush_bulk_edits(self._bulk_current_row)
        saved, stacked, failed = self._save_cases_as_drafts(self.cases, "all")
        msg = f"Saved {len(saved)} individual draft(s) to /drafts."
        if stacked:
            msg += f"\nStacked file: {stacked}"
        if failed:
            msg += f"\n{len(failed)} failed: " + "; ".join(failed)
        self._bulk_log(msg)
        QMessageBox.information(self, "Drafts Saved", msg)

    @staticmethod
    def _normalize_manual_draft_to_bulk(data: dict) -> dict:
        """Convert a manual-tab draft (patient_fields key) to the bulk case dict format."""
        rtype = data.get("report_type", "single_hla")
        pf    = data.get("patient_fields", {})

        # For CDC/DSA/Flow: prefer the type-specific fields saved by the fixed save_manual_draft
        if rtype == "cdc_crossmatch" and "cdc_patient_fields" in data:
            cpf = data["cdc_patient_fields"]
            patient = dict(cpf)
            patient["name"] = patient.pop("patient_name", cpf.get("patient_name", ""))
        elif rtype == "dsa_crossmatch" and "dsa_patient_fields" in data:
            dpf = data["dsa_patient_fields"]
            patient = dict(dpf)
            patient["name"] = patient.pop("patient_name", dpf.get("patient_name", ""))
        elif rtype == "flow_crossmatch" and "flow_patient_fields" in data:
            fpf = data["flow_patient_fields"]
            patient = dict(fpf)
            patient["name"] = patient.pop("patient_name", fpf.get("patient_name", ""))
        else:
            patient = {k: v for k, v in pf.items() if k != "patient_name"}
            patient["name"] = pf.get("patient_name", "")

        patient.setdefault("hla", {})
        patient.setdefault("hla_c_type", "")
        patient.setdefault("_join_key", patient.get("pin", ""))
        patient.setdefault("_has_insufficient_hla", False)

        # Build donors list
        if rtype == "cdc_crossmatch" and "cdc_donor_fields" in data:
            raw_don = data["cdc_donor_fields"]
            raw_don.setdefault("hla", {}); raw_don.setdefault("hla_c_type", "")
            donors = [raw_don]
        elif rtype == "dsa_crossmatch" and "dsa_donor_fields" in data:
            raw_don = data["dsa_donor_fields"]
            raw_don.setdefault("hla", {}); raw_don.setdefault("hla_c_type", "")
            donors = [raw_don]
        elif rtype == "flow_crossmatch" and "flow_donor_fields" in data:
            raw_don = data["flow_donor_fields"]
            raw_don.setdefault("hla", {}); raw_don.setdefault("hla_c_type", "")
            donors = [raw_don]
        else:
            donors = []
            for d in data.get("donors", []):
                df = d.get("fields", d)
                entry = dict(df)
                entry.setdefault("name", "")
                entry.setdefault("hla", d.get("hla", {}))
                entry.setdefault("hla_c_type", "")
                donors.append(entry)

        case = {
            "report_type": rtype,
            "patient":     patient,
            "donors":      donors,
            "nabl":        data.get("nabl", True),
            "with_logo":   data.get("with_logo", True) if isinstance(data.get("with_logo"), bool)
                           else data.get("with_logo", "With Logo") == "With Logo",
            "sig_name_overrides": data.get("sig_name_overrides", {}),
            "rpl_reference": data.get("rpl_reference", {}),
        }
        for key in ("cdc_results", "dsa_results", "flow_results"):
            if key in data:
                case[key] = data[key]
        return case

    def load_bulk_draft(self):
        start_dir = DRAFTS_DIR if os.path.isdir(DRAFTS_DIR) else os.path.dirname(BULK_DRAFT_FILE)
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Draft", start_dir,
            "JSON Files (*.json);;All Files (*)"
        )
        if not path: return
        # Fix 1: reset all state before loading draft so no prior session data leaks in
        self._reset_bulk_session()
        try:
            with open(path) as fh: draft = json.load(fh)
            # Support both list-of-cases (Save All Draft) and single-case dict (Save Draft)
            if isinstance(draft, dict):
                draft = [draft]
            # Normalize manual-tab drafts (have "patient_fields" instead of "patient")
            normalized = []
            for item in draft:
                if "patient_fields" in item and "patient" not in item:
                    item = self._normalize_manual_draft_to_bulk(item)
                normalized.append(item)
            draft = normalized
            self.cases = _filter_valid_cases(draft)
            skipped    = len(draft) - len(self.cases)
            self._populate_bulk_list()
            msg = f"Draft loaded: {os.path.basename(path)} ({len(self.cases)} cases)"
            if skipped:
                msg += f" ({skipped} suppressed — Insufficient Data)"
            self._bulk_log(msg)
        except Exception as e:
            QMessageBox.critical(self, "Load Error", str(e))

    def save_bulk_current_draft(self):
        """Save only the currently selected patient's data as a draft to the /drafts folder."""
        idx = self._bulk_current_row
        if idx < 0 or idx >= len(self.cases):
            QMessageBox.warning(self, "No Case Selected", "Please select a patient first.")
            return
        self._flush_bulk_edits(idx)
        case = self.cases[idx]

        os.makedirs(DRAFTS_DIR, exist_ok=True)
        p              = case.get("patient", {})
        name_val       = p.get("name", "Unknown")
        rtype          = case.get("report_type", "single_hla")
        safe_name      = _re.sub(r"[^\w\-]", "_", name_val)
        template_label = _re.sub(r"[^\w\-]", "_", RTYPE_TO_TEMPLATE.get(rtype, rtype))
        filename       = f"{safe_name}_{template_label}_draft.json"
        path      = os.path.join(DRAFTS_DIR, filename)

        draft = {k: v for k, v in case.items() if k != "signatories"}
        try:
            with open(path, "w") as fh: json.dump(draft, fh, indent=2, default=str)
            QMessageBox.information(self, "Draft Saved",
                f"Draft saved for {name_val}:\n{filename}")
        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e))

    def generate_bulk_current(self):
        """Generate a report for the currently selected case only."""
        idx = self._bulk_current_row
        if idx < 0 or idx >= len(self.cases):
            QMessageBox.warning(self, "No Case Selected", "Please select a patient first.")
            return
        self._flush_bulk_edits(idx)

        out = self.bulk_output_label.text()
        if out == "No folder selected":
            QMessageBox.warning(self, "No Output", "Please select an output folder.")
            return

        with_logo  = self.logo_combo.currentText() == "With Logo"
        sigs       = self._get_signatories()
        sig_single = self.qsettings.value("sig_count_single", 3, type=int)
        sig_donor  = self.qsettings.value("sig_count_donor",  2, type=int)
        sig_stamp  = self.qsettings.value("signature_stamp", False, type=bool)

        self.bulk_status_label.setText("Generating…")
        self.worker = GenerateWorker(
            [self.cases[idx]], out, with_logo, sigs, sig_single, sig_donor, sig_stamp
        )
        self.worker.progress.connect(self._on_bulk_progress)
        self.worker.finished.connect(self._on_bulk_done)
        self.worker.error.connect(lambda m: self._bulk_log(f"ERROR: {m}"))
        self.worker.start()

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 3 — SETTINGS
    # ══════════════════════════════════════════════════════════════════════════
    def _create_settings_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(); tab.setLayout(lay)

        grp1 = QGroupBox("Report Options")
        g1   = QVBoxLayout(); grp1.setLayout(g1)
        self.chk_logo       = QCheckBox("Include hospital logo (WITH LOGO variant)")
        self.chk_nabl_stamp = QCheckBox("NABL accreditation stamp")
        self.chk_stamp      = QCheckBox("Signature rubber stamp")
        self.chk_logo.setChecked(      self.qsettings.value("with_logo",        True,  type=bool))
        self.chk_nabl_stamp.setChecked(self.qsettings.value("nabl_stamp",       True,  type=bool))
        self.chk_stamp.setChecked(     self.qsettings.value("signature_stamp",  False, type=bool))
        for chk in [self.chk_logo, self.chk_nabl_stamp, self.chk_stamp]:
            g1.addWidget(chk)
        # Auto-persist checkbox state immediately on change so that report generation
        # always reads the current UI value (prevents stale-cache stamp/logo bugs).
        self.chk_logo.stateChanged.connect(
            lambda: self.qsettings.setValue("with_logo",       self.chk_logo.isChecked()))
        self.chk_nabl_stamp.stateChanged.connect(
            lambda: self.qsettings.setValue("nabl_stamp",      self.chk_nabl_stamp.isChecked()))
        self.chk_stamp.stateChanged.connect(
            lambda: self.qsettings.setValue("signature_stamp", self.chk_stamp.isChecked()))
        lay.addWidget(grp1)

        grp2 = QGroupBox("Signatory Count Defaults")
        g2   = QFormLayout(); grp2.setLayout(g2)
        self.spin_single = QSpinBox(); self.spin_single.setRange(1, 5)
        self.spin_donor  = QSpinBox(); self.spin_donor.setRange(1, 5)
        self.spin_single.setValue(self.qsettings.value("sig_count_single", 3, type=int))
        self.spin_donor.setValue( self.qsettings.value("sig_count_donor",  2, type=int))
        g2.addRow("Single HLA report — signatures:", self.spin_single)
        g2.addRow("Donor / RPL report — signatures:", self.spin_donor)
        lay.addWidget(grp2)

        grp3 = QGroupBox("Signatories (order = report display order)")
        g3   = QVBoxLayout(); grp3.setLayout(g3)
        self.tbl_sigs = QTableWidget(0, 2)
        self.tbl_sigs.setHorizontalHeaderLabels(["Name", "Title / Role"])
        self.tbl_sigs.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tbl_sigs.setAlternatingRowColors(True)
        self.tbl_sigs.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_sigs.setFixedHeight(110)  # Compact fixed height
        g3.addWidget(self.tbl_sigs)
        
        g3.addSpacing(5) # Explicit gap
        sig_row = QHBoxLayout()
        edit_sigs_btn  = QPushButton("Edit Signatories…")
        reset_sigs_btn = QPushButton("Reset Defaults")
        edit_sigs_btn.clicked.connect(self._edit_signatories)
        reset_sigs_btn.clicked.connect(self._reset_signatories)
        sig_row.addWidget(edit_sigs_btn); sig_row.addWidget(reset_sigs_btn)
        sig_row.addStretch()
        g3.addLayout(sig_row)
        lay.addWidget(grp3)

        # Report Templates — paths to reference template PDFs
        grp_tmpl = QGroupBox("Report Templates")
        g_tmpl   = QFormLayout(); grp_tmpl.setLayout(g_tmpl)
        self._tmpl_path_labels = {}
        for tpl in REPORT_TEMPLATES:
            key   = f"template_path_{tpl['report_type']}"
            saved = self.qsettings.value(key, tpl["default_path"])
            row_w = QWidget(); row_h = QHBoxLayout(row_w); row_h.setContentsMargins(0, 0, 0, 0)
            lbl   = QLabel(saved if os.path.exists(saved) else tpl["default_path"])
            lbl.setStyleSheet(PATH_LABEL_STYLE)
            lbl.setWordWrap(True)
            self._tmpl_path_labels[tpl["report_type"]] = lbl
            browse_btn  = QPushButton("Browse…")
            preview_btn = QPushButton("Preview")
            browse_btn.setFixedWidth(72)
            preview_btn.setFixedWidth(64)
            _rtype = tpl["report_type"]
            browse_btn.clicked.connect(lambda checked, rt=_rtype: self._browse_template(rt))
            preview_btn.clicked.connect(lambda checked, rt=_rtype: self._preview_template(rt))
            row_h.addWidget(lbl, 1)
            row_h.addWidget(browse_btn)
            row_h.addWidget(preview_btn)
            g_tmpl.addRow(f"{tpl['name']}:", row_w)
        lay.addWidget(grp_tmpl)

        save_btn = QPushButton("Save Settings")
        save_btn.setStyleSheet(GENERATE_BTN_STYLE)
        save_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
        save_btn.clicked.connect(self._save_settings)
        lay.addWidget(save_btn)
        lay.addStretch()
        self._refresh_sig_table()
        return tab

    def _browse_template(self, report_type):
        key  = f"template_path_{report_type}"
        cur  = self.qsettings.value(key, _TEMPLATE_DIR)
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Template PDF",
            os.path.dirname(cur) if os.path.exists(cur) else _TEMPLATE_DIR,
            "PDF Files (*.pdf);;All Files (*)"
        )
        if path:
            self.qsettings.setValue(key, path)
            self._tmpl_path_labels[report_type].setText(path)

    def _preview_template(self, report_type):
        key  = f"template_path_{report_type}"
        tpl  = next((t for t in REPORT_TEMPLATES if t["report_type"] == report_type), None)
        path = self.qsettings.value(key, tpl["default_path"] if tpl else "")
        if not os.path.exists(path):
            QMessageBox.warning(self, "File Not Found", f"Template file not found:\n{path}")
            return
        if platform.system() == "Windows":
            os.startfile(path)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def _refresh_sig_table(self):
        self.tbl_sigs.setRowCount(0)
        for sig in self._get_signatories():
            r = self.tbl_sigs.rowCount(); self.tbl_sigs.insertRow(r)
            self.tbl_sigs.setItem(r, 0, QTableWidgetItem(sig["name"]))
            self.tbl_sigs.setItem(r, 1, QTableWidgetItem(sig["title"]))

    def _edit_signatories(self):
        dlg = SignatoryDialog(self._get_signatories(), self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.qsettings.setValue("signatories", json.dumps(dlg.get_signatories()))
            self._refresh_sig_table()

    def _reset_signatories(self):
        self.qsettings.setValue("signatories", json.dumps(DEFAULT_SIGNATORIES))
        self._refresh_sig_table()

    def _save_settings(self):
        self.qsettings.setValue("with_logo",        self.chk_logo.isChecked())
        self.qsettings.setValue("nabl_stamp",        self.chk_nabl_stamp.isChecked())
        self.qsettings.setValue("signature_stamp",   self.chk_stamp.isChecked())
        self.qsettings.setValue("sig_count_single",  self.spin_single.value())
        self.qsettings.setValue("sig_count_donor",   self.spin_donor.value())
        QMessageBox.information(self, "Saved", "Settings saved successfully.")

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 4 — USER GUIDE
    # ══════════════════════════════════════════════════════════════════════════
    def _create_guide_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(); tab.setLayout(lay)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setHtml("""
<html><head><style>
  body { font-family:'Segoe UI',sans-serif; line-height:1.6; color:#333; padding:20px; }
  .hdr { background:#1F497D; color:white; padding:24px; border-radius:8px; margin-bottom:18px; }
  .hdr h1 { margin:0; font-size:22px; }
  .hdr p  { margin:4px 0 0; opacity:.85; }
  h3 { color:#1F497D; border-bottom:1px solid #eee; padding-bottom:4px; }
  .tip { background:#e7f3ff; border:1px solid #b8daff; border-radius:4px;
         padding:10px; margin:8px 0; font-style:italic; }
  table { border-collapse:collapse; width:100%; }
  th { background:#1F497D; color:white; padding:6px 10px; }
  td { padding:5px 10px; border:1px solid #ddd; }
  tr:nth-child(even) { background:#f9f9f9; }
  code { background:#f1f5f9; padding:2px 6px; border-radius:3px; font-family:Courier; }
</style></head><body>
<div class="hdr">
  <h1>HLA Typing Report Generator</h1>
  <p>Automated HLA NGS typing report generation — Anderson Diagnostic Services</p>
</div>

<h3>Quick Start — Bulk Upload</h3>
<ol>
  <li>Go to <b>Bulk Upload</b> tab</li>
  <li>Browse and select your Excel file (MINISEQ or SURFSEQ)</li>
  <li>Tick <b>NABL-Accredited</b> for MINISEQ files; untick for SURFSEQ</li>
  <li>Click <b>Load &amp; Parse Excel</b> — cases are auto-detected</li>
  <li>Select a case from the list to review and edit it in the editor on the right</li>
  <li>Click <b>Apply Edits</b> after making changes to a case</li>
  <li>Select output folder and click <b>Generate All Reports</b></li>
</ol>

<h3>Excel File Sheets</h3>
<table>
  <tr><th>Sheet</th><th>Purpose</th></tr>
  <tr><td><code>patient-donor detail</code></td><td>Patient &amp; donor metadata</td></tr>
  <tr><td><code>result data</code></td><td>HLA allele results</td></tr>
</table>
<div class="tip">MINISEQ → enable NABL stamp. SURFSEQ → disable NABL stamp.</div>

<h3>Report Types</h3>
<table>
  <tr><th>Type</th><th>Detection</th><th>Pages</th></tr>
  <tr><td>Single HLA</td><td>No donor rows</td><td>1–2</td></tr>
  <tr><td>RPL Couple</td><td>Diagnosis=RPL or wife/husband relationship</td><td>3</td></tr>
  <tr><td>Transplant Donor</td><td>Donor present, non-RPL</td><td>2+</td></tr>
</table>

<h3>Manual Entry</h3>
<p>Fill in the Patient Information form. Enter HLA alleles in the <b>HLA Results</b> section
(one row per locus, Allele&nbsp;1 and Allele&nbsp;2 side by side). Enable the
<b>Donor Information</b> checkbox for transplant or RPL cases.</p>

<h3>Editing in Bulk Upload</h3>
<p>Select a case from the list. The right panel shows a full editable form for patient info,
HLA alleles, and each donor. Edit any field, then click <b>Apply Edits</b> (or select another
case — edits are flushed automatically).</p>

<h3>Draft JSON</h3>
<p>Use <b>Save Draft</b> / <b>Load Draft</b> in either tab to resume work across sessions.</p>

<h3>Signatures</h3>
<ul>
  <li>Single HLA — default 3 signatures; Donor/RPL — default 2 signatures</li>
  <li>Configure counts and names in the <b>Settings</b> tab</li>
</ul>
</body></html>""")
        lay.addWidget(browser)
        return tab

    # ══════════════════════════════════════════════════════════════════════════
    # SHARED HELPERS
    # ══════════════════════════════════════════════════════════════════════════
    def _get_signatories(self):
        # Build a lookup of canonical titles from DEFAULT_SIGNATORIES
        _canonical = {s["name"]: s["title"] for s in DEFAULT_SIGNATORIES}
        raw = self.qsettings.value("signatories", "")
        if raw:
            try:
                loaded = json.loads(raw)
                # Migrate any stale titles to the current canonical value
                changed = False
                for sig in loaded:
                    canonical = _canonical.get(sig.get("name", ""))
                    if canonical and sig.get("title") != canonical:
                        sig["title"] = canonical
                        changed = True
                if changed:
                    self.qsettings.setValue("signatories", json.dumps(loaded))
                return loaded
            except Exception:
                pass
        return copy.deepcopy(DEFAULT_SIGNATORIES)

    def _build_case(self, rtype, nabl, with_logo, sig_stamp, patient, donors):
        sigs_raw   = self._get_signatories()
        n          = (self.qsettings.value("sig_count_single", 3, type=int)
                      if rtype == "single_hla"
                      else self.qsettings.value("sig_count_donor", 2, type=int))
        sigs = []
        for sig in sigs_raw[:n]:
            sign_info = hla_assets.SIGN_BY_NAME.get(sig["name"],
                            next(iter(hla_assets.SIGN_BY_NAME.values())))
            entry = {**sig, **sign_info}
            # Rubber seal: whenever stamp setting is ON, across all templates and both NABL/non-NABL
            if sig_stamp and "rayvathy" in sig["name"].lower():
                entry["seal_b64"] = hla_assets.SEAL_REVATHY_B64
            sigs.append(entry)
        return {
            "report_type": rtype, "nabl": nabl,
            "with_logo": with_logo, "signature_stamp": sig_stamp,
            "methodology": "", "imgt_release": "", "coverage": "",
            "typing_status": "Complete", "reviewer": "",
            "patient": patient, "donors": donors,
            "rpl_reference": {}, "signatories": sigs,
        }

    def _load_persistent(self):
        last_out = self.qsettings.value("last_output_dir", "")
        if last_out:
            self.manual_output_label.setText(last_out)
            self.bulk_output_label.setText(last_out)


# ─── Entry point ──────────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("HLA Typing Report Generator")
    app.setOrganizationName("AndersonDiagnostics")
    app.setStyle("Fusion")

    # Palette — matches PGTA's look with clean backgrounds and correct text contrast
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window,          QColor(248, 249, 250))
    pal.setColor(QPalette.ColorRole.WindowText,      QColor(30,  30,  30))
    pal.setColor(QPalette.ColorRole.Base,            QColor(255, 255, 255))
    pal.setColor(QPalette.ColorRole.AlternateBase,   QColor(240, 240, 247))
    pal.setColor(QPalette.ColorRole.ToolTipBase,     QColor(255, 255, 220))
    pal.setColor(QPalette.ColorRole.ToolTipText,     QColor(0,   0,   0))
    pal.setColor(QPalette.ColorRole.Text,            QColor(30,  30,  30))
    pal.setColor(QPalette.ColorRole.Button,          QColor(240, 240, 240))
    pal.setColor(QPalette.ColorRole.ButtonText,      QColor(30,  30,  30))
    pal.setColor(QPalette.ColorRole.BrightText,      QColor(255, 0,   0))
    pal.setColor(QPalette.ColorRole.Highlight,       QColor(31,  73,  125))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    pal.setColor(QPalette.ColorRole.Link,            QColor(31,  73,  125))
    app.setPalette(pal)

    win = HLAReportGeneratorApp()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
