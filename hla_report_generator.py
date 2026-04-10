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

import hla_assets
from hla_data_parser import parse_excel, get_case_summary, c_supertype, compute_rpl_reference
from hla_template import generate_pdf, make_filename

# ─── Constants ────────────────────────────────────────────────────────────────
HLA_LOCI = ["A", "B", "C", "DRB1", "DQB1", "DPB1", "DRB3", "DRB4", "DRB5"]
MANUAL_DRAFT_FILE   = os.path.join(os.path.dirname(__file__), "hla_manual_draft.json")
BULK_DRAFT_FILE     = os.path.join(os.path.dirname(__file__), "hla_bulk_draft.json")
TEMP_PREVIEW_PATH   = os.path.join(os.path.dirname(__file__), "temp_hla_preview.pdf")

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
]
TEMPLATE_NAMES    = [t["name"]        for t in REPORT_TEMPLATES]
TEMPLATE_TO_RTYPE = {t["name"]:        t["report_type"] for t in REPORT_TEMPLATES}
RTYPE_TO_TEMPLATE = {t["report_type"]: t["name"]        for t in REPORT_TEMPLATES}

DEFAULT_SIGNATORIES = [
    {"name": "Ms. S Aruna Devi",      "title": "Team Lead, Reviewed By"},
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
                # Rubber seal: only when stamp setting is ON and NABL is enabled
                if self.signature_stamp and nabl and rtype != "rpl_couple" and "rayvathy" in sig["name"].lower():
                    entry["seal_b64"] = hla_assets.SEAL_REVATHY_B64
                # Apply custom signature override if provided
                if hasattr(sig, "get") and sig.get("sign_override_b64"):
                    entry["sign_b64"] = sig["sign_override_b64"]
                    entry["is_png"]   = sig.get("sign_override_is_png", True)
                c["signatories"].append(entry)
            # Apply per-case signature name overrides (selected from SIGN_BY_NAME lookup)
            for slot, sig_name in case.get("sig_name_overrides", {}).items():
                try:
                    slot_i = int(slot)
                    sign_info = hla_assets.SIGN_BY_NAME.get(sig_name)
                    if sign_info and 0 <= slot_i < len(c["signatories"]):
                        c["signatories"][slot_i]["sign_b64"] = sign_info["sign_b64"]
                        c["signatories"][slot_i]["is_png"]   = sign_info["is_png"]
                except Exception:
                    pass
            fname    = make_filename(c)
            out_path = os.path.join(self.output_dir, fname)
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
        
        # Debounce timer for real-time updates
        self._edit_timer = QTimer()
        self._edit_timer.setSingleShot(True)
        self._edit_timer.timeout.connect(self._refresh_bulk_preview)

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
        main_layout.setContentsMargins(10, 2, 10, 5)
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
        tab.setLayout(main_layout)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter)

        # ── Left: form ─────────────────────────────────────────────────────
        left_widget = QWidget()
        left_widget.setMaximumWidth(540)
        left_layout = QVBoxLayout()
        left_widget.setLayout(left_layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout()
        scroll_widget.setLayout(scroll_layout)
        scroll.setWidget(scroll_widget)
        left_layout.addWidget(scroll)

        # Patient Information
        pat_group = QGroupBox("Patient Information")
        pat_form  = QFormLayout(); pat_group.setLayout(pat_form)
        pat_form.setSpacing(1); pat_form.setContentsMargins(4, 1, 4, 1)
        scroll_layout.addWidget(pat_group)

        self.f = {}
        PAT_FIELDS = [
            ("patient_name",    "Patient Name *",       ""),
            ("gender_age",      "Gender / Age",         ""),
            ("hospital_mr_no",  "Hospital MR No.",      ""),
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

        # Report Options moved to global header

        # Patient HLA Results (FormLayout, one locus per row, two alleles side-by-side)
        hla_group = QGroupBox("HLA Results — Patient")
        hla_form  = QFormLayout(); hla_group.setLayout(hla_form)
        hla_form.setSpacing(1); hla_form.setContentsMargins(4, 1, 4, 1)
        scroll_layout.addWidget(hla_group)
        self.hla_pat = {}
        for locus in HLA_LOCI:
            row_w, a1, a2 = _make_allele_row()
            hla_form.addRow(f"{locus}:", row_w)
            self.hla_pat[locus] = [a1, a2]

        # ── Donors section — supports multiple donors ──────────────────────
        self._manual_donors = []   # list of {container, fields, hla}
        donors_outer = QGroupBox("Donors (Optional)")
        donors_outer_layout = QVBoxLayout()
        donors_outer_layout.setSpacing(2)
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

        scroll_layout.addWidget(donors_outer)

        # ── Signature Override — select from configured signatories ───────────
        self._manual_sig_name_overrides = {}   # {slot_idx: sig_name_string}
        self._manual_sig_combos         = {}   # {slot_idx: QComboBox}

        sig_group = QGroupBox("Signature Override (select from Settings signatories)")
        sig_form  = QFormLayout()
        sig_form.setSpacing(2)
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
        gen_layout = QVBoxLayout(); gen_group.setLayout(gen_layout)

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

        # ── Right: PDF preview (exact PGTA layout) ───────────────────────────
        right_widget = QGroupBox("Report Preview")
        right_widget.setMinimumWidth(600)
        right_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        right_layout = QVBoxLayout(); right_widget.setLayout(right_layout)

        preview_label = QLabel("Report Preview (PDF)")
        preview_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        right_layout.addWidget(preview_label)

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

        # Manual Entry layout: Left (form) + Right (Fixed 620px Preview)
        right_widget.setFixedWidth(620)

        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        return tab

    def browse_manual_output(self):
        start = self.qsettings.value("last_output_dir", str(Path.home()))
        path  = QFileDialog.getExistingDirectory(self, "Select Output Directory", start)
        if path:
            self.manual_output_label.setText(path)
            self.bulk_output_label.setText(path)   # sync with bulk tab
            self.qsettings.setValue("last_output_dir", path)

    def generate_manual(self):
        name = self.f["patient_name"].text().strip()
        pin  = self.f["pin"].text().strip()
        if not name or not pin:
            QMessageBox.warning(self, "Missing Fields", "Patient Name and PIN are required.")
            return
        out_dir = self.manual_output_label.text()
        if out_dir == "No directory selected":
            QMessageBox.warning(self, "No Output", "Please select an output folder.")
            return

        with_logo = self.logo_combo.currentText() == "With Logo"
        rtype     = TEMPLATE_TO_RTYPE.get(self.template_combo.currentText(), "single_hla")
        nabl      = self.qsettings.value("nabl_stamp", True, type=bool)
        sig_stamp = self.qsettings.value("signature_stamp", False, type=bool)

        patient = {k: w.text().strip() for k, w in self.f.items()}
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
                "name":           d.get("donor_name", ""),
                "relationship":   d.get("relationship", ""),
                "gender_age":     d.get("donor_gender_age", ""),
                "pin":            d.get("donor_pin", ""),
                "sample_number":  d.get("donor_sample_no", ""),
                "collection_date":d.get("donor_collect", ""),
                "receipt_date":   d.get("donor_receipt", ""),
                "report_date":    patient.get("report_date", ""),
                "match":          d.get("match", ""),
                "hla": donor_hla, "hla_c_type": "", "remarks": "",
                "hospital_clinic": patient.get("hospital_clinic", ""),
                "diagnosis":       patient.get("diagnosis", ""),
                "specimen":        patient.get("specimen", "Blood - EDTA"),
            })

        case = self._build_case(rtype, nabl, with_logo, sig_stamp, patient, donors)
        # Apply any custom signature image overrides from the Manual tab
        self._apply_sig_name_overrides(case, self._manual_sig_name_overrides)
        fname    = make_filename(case)
        out_path = os.path.join(out_dir, fname)
        os.makedirs(out_dir, exist_ok=True)
        try:
            generate_pdf(case, out_path)
            self.manual_status_label.setText(f"✓ Saved: {fname}")
            self.statusBar().showMessage(f"Generated: {fname}")
            # Launch preview in background (PGTA pattern)
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
        """Load generated preview — PGTA on_preview_generated pattern."""
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
        if os.path.exists(TEMP_PREVIEW_PATH):
            self._on_manual_preview_generated(TEMP_PREVIEW_PATH)

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
        idx  = self.template_combo.findText(name)
        if idx >= 0:
            self.template_combo.blockSignals(True)
            self.template_combo.setCurrentIndex(idx)
            self.template_combo.blockSignals(False)

    def _clear_manual_form(self):
        for w in self.f.values(): w.clear()
        for locus in HLA_LOCI:
            self.hla_pat[locus][0].clear(); self.hla_pat[locus][1].clear()
        # Remove all donor panels
        for entry in list(self._manual_donors):
            entry["container"].deleteLater()
        self._manual_donors.clear()
        self.manual_status_label.setText("Form cleared.")

    # ── Multi-donor helpers ────────────────────────────────────────────────────
    def _add_manual_donor(self, donor_data=None):
        """Create and insert a new donor panel into the manual tab."""
        di = len(self._manual_donors)
        fields = donor_data.get("fields", {}) if isinstance(donor_data, dict) and "fields" in donor_data else (donor_data or {})
        hla_data = donor_data.get("hla", {}) if isinstance(donor_data, dict) and "hla" in donor_data else {}

        # Container widget (group + remove button)
        container = QWidget()
        c_lay = QVBoxLayout(container)
        c_lay.setContentsMargins(0, 0, 0, 2)
        c_lay.setSpacing(2)

        group = QGroupBox(f"Donor {di + 1}")
        form  = QFormLayout()
        form.setSpacing(1)
        form.setContentsMargins(4, 1, 4, 1)
        group.setLayout(form)

        DONOR_FIELDS = [
            ("donor_name",       "Donor Name",      ""),
            ("relationship",     "Relationship",    ""),
            ("donor_gender_age", "Gender / Age",    ""),
            ("donor_pin",        "Donor PIN",       ""),
            ("donor_sample_no",  "Sample Number",   ""),
            ("donor_collect",    "Collection Date", ""),
            ("donor_receipt",    "Receipt Date",    ""),
            ("match",            "Match Score",     ""),
        ]
        d_fields = {}
        for key, lbl, default in DONOR_FIELDS:
            val = fields.get(key, default)
            w = QLineEdit(val)
            w.setMaximumHeight(24)
            d_fields[key] = w
            form.addRow(lbl + ":", w)
            if key == "relationship":
                w.textChanged.connect(self._auto_detect_manual_template)

        form.addRow(QLabel("<b>Donor HLA Results</b>"), QLabel(""))
        d_hla = {}
        for locus in HLA_LOCI:
            alleles = hla_data.get(locus, ["", ""])
            a1_val  = alleles[0] if len(alleles) > 0 else ""
            a2_val  = alleles[1] if len(alleles) > 1 else ""
            row_w, a1, a2 = _make_allele_row(a1_val, a2_val)
            form.addRow(f"{locus}:", row_w)
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

    def _remove_manual_donor(self, entry):
        """Remove a donor panel from the manual tab."""
        if entry in self._manual_donors:
            self._manual_donors.remove(entry)
            entry["container"].deleteLater()
            # Re-number remaining donor groups
            for i, e in enumerate(self._manual_donors):
                e["group"].setTitle(f"Donor {i + 1}")
            self._auto_detect_manual_template()

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
        Replace signatory sign_b64 / is_png using name-keyed lookup from SIGN_BY_NAME.
        name_overrides: {slot_int_or_str: sig_name_string}
        Slots with "(Use Default)" or missing entries are left unchanged.
        """
        for i, sig in enumerate(case.get("signatories", [])):
            name = name_overrides.get(i) or name_overrides.get(str(i))
            if not name or name == "(Use Default)":
                continue
            sign_info = hla_assets.SIGN_BY_NAME.get(name)
            if sign_info:
                sig["sign_b64"] = sign_info["sign_b64"]
                sig["is_png"]   = sign_info["is_png"]

    def save_manual_draft(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Manual Draft", MANUAL_DRAFT_FILE,
            "JSON Files (*.json);;All Files (*)"
        )
        if not path: return
        saved_donors = []
        for entry in self._manual_donors:
            saved_donors.append({
                "fields": {k: w.text().strip() for k, w in entry["fields"].items()},
                "hla":    {locus: [a[0].text().strip(), a[1].text().strip()]
                           for locus, a in entry["hla"].items()},
            })
        data = {
            "patient_fields": {k: w.text().strip() for k, w in self.f.items()},
            "report_type": TEMPLATE_TO_RTYPE.get(self.template_combo.currentText(), "single_hla"),
            "with_logo":   self.logo_combo.currentText(),
            "donors":      saved_donors,
            "patient_hla": {locus: [a[0].text().strip(), a[1].text().strip()]
                            for locus, a in self.hla_pat.items()},
            "sig_name_overrides": {str(k): v for k, v in self._manual_sig_name_overrides.items()},
        }
        try:
            with open(path, "w") as fh: json.dump(data, fh, indent=2)
            self.manual_status_label.setText(f"Draft saved: {os.path.basename(path)}")
        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e))

    def load_manual_draft(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Manual Draft", os.path.dirname(MANUAL_DRAFT_FILE),
            "JSON Files (*.json);;All Files (*)"
        )
        if not path: return
        try:
            with open(path) as fh: data = json.load(fh)
            for k, v in data.get("patient_fields", {}).items():
                if k in self.f: self.f[k].setText(v)
            _tmpl_name = RTYPE_TO_TEMPLATE.get(data.get("report_type", "single_hla"), TEMPLATE_NAMES[0])
            idx = self.template_combo.findText(_tmpl_name)
            if idx >= 0: self.template_combo.setCurrentIndex(idx)
            idx = self.logo_combo.findText(data.get("with_logo", "With Logo"))
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
                    self.hla_pat[locus][0].setText(vals[0] if len(vals) > 0 else "")
                    self.hla_pat[locus][1].setText(vals[1] if len(vals) > 1 else "")
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
            self.manual_status_label.setText(f"Draft loaded: {os.path.basename(path)}")
        except Exception as e:
            QMessageBox.critical(self, "Load Error", str(e))

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
        left_layout.addWidget(QLabel("Cases:"))
        self.bulk_search = QLineEdit()
        self.bulk_search.setPlaceholderText("Search by patient name…")
        self.bulk_search.textChanged.connect(self._filter_bulk_list)
        left_layout.addWidget(self.bulk_search)
        self.bulk_list = QListWidget()
        self.bulk_list.setAlternatingRowColors(True)
        self.bulk_list.currentItemChanged.connect(self._on_bulk_item_changed)
        left_layout.addWidget(self.bulk_list)

        sel_row = QHBoxLayout()
        sel_all  = QPushButton("Select All");  sel_all.clicked.connect(self._select_all)
        sel_none = QPushButton("Select None"); sel_none.clicked.connect(self._select_none)
        sel_row.addWidget(sel_all); sel_row.addWidget(sel_none)
        left_layout.addLayout(sel_row)

        # Draft buttons under list (exact PGTA position)
        draft_row = QHBoxLayout()
        save_draft_btn = QPushButton("Save All Draft")
        save_draft_btn.clicked.connect(self.save_bulk_draft)
        load_draft_btn = QPushButton("Load Draft")
        load_draft_btn.clicked.connect(self.load_bulk_draft)
        draft_row.addWidget(save_draft_btn)
        draft_row.addWidget(load_draft_btn)
        left_layout.addLayout(draft_row)

        gen_btn = QPushButton("Generate All Reports")
        gen_btn.setStyleSheet(GENERATE_BTN_STYLE)
        gen_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        gen_btn.clicked.connect(self.generate_bulk)
        left_layout.addWidget(gen_btn)

        # RIGHT: Patient Editor
        editor_widget = QWidget()
        editor_layout = QVBoxLayout(); editor_widget.setLayout(editor_layout)
        editor_layout.addWidget(QLabel("Patient Editor (select a case from the list):"))

        self._bulk_scroll = QScrollArea()
        self._bulk_scroll.setWidgetResizable(True)
        self._bulk_editor_container = QWidget()
        self._bulk_editor_layout    = QVBoxLayout()
        self._bulk_editor_container.setLayout(self._bulk_editor_layout)
        self._bulk_scroll.setWidget(self._bulk_editor_container)
        editor_layout.addWidget(self._bulk_scroll, 1)
        
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

    def load_excel(self):
        path = self.bulk_file_label.text()
        if path == "No file selected" or not os.path.exists(path):
            QMessageBox.warning(self, "No File", "Please select a valid Excel file.")
            return
        try:
            self.cases = parse_excel(path, nabl=self.chk_nabl.isChecked())
            self._populate_bulk_list()
            self._bulk_log(f"Loaded {len(self.cases)} case(s) from {os.path.basename(path)}")
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

    def _filter_bulk_list(self, text):
        for i in range(self.bulk_list.count()):
            item = self.bulk_list.item(i)
            item.setHidden(text.lower() not in item.text().lower())

    # ── Bulk editor — PGTA-style dynamic form rebuild ─────────────────────────
    def _on_bulk_item_changed(self, current, previous):
        """Flush edits from previous then rebuild the editor for the new selection."""
        # Flush previous case edits first
        if previous is not None:
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
            w = QLineEdit(str(p.get(key, "")))
            w.setFixedHeight(24)
            if "date" in key.lower(): w.setPlaceholderText("DD-MM-YYYY")
            w.textChanged.connect(self._on_bulk_field_debounced)
            self._bulk_fields[key] = w
            pat_form.addRow(lbl + ":", w)

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
        rtype_combo.currentIndexChanged.connect(self._on_bulk_field_debounced)
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
            a1_val  = alleles[0] if len(alleles) > 0 else ""
            a2_val  = alleles[1] if len(alleles) > 1 else ""
            row_w, a1, a2 = _make_allele_row(a1_val, a2_val)
            a1.textChanged.connect(self._on_bulk_field_debounced)
            a2.textChanged.connect(self._on_bulk_field_debounced)
            hla_pat_form.addRow(f"{locus}:", row_w)
            self._bulk_hla_pat[locus] = [a1, a2]

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
            self._bulk_editor_layout.addWidget(rpl_group)

        self._bulk_editor_layout.addWidget(hla_pat_group)

        # ── Donors ──────────────────────────────────────────────────────────
        self._bulk_donor_fields = []
        self._bulk_hla_don      = []

        DONOR_FIELDS_DEF = [
            ("name",            "Name"),
            ("relationship",    "Relationship"),
            ("gender_age",      "Gender / Age"),
            ("pin",             "PIN"),
            ("sample_number",   "Sample Number"),
            ("match",           "Match Score"),
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
                w = QLineEdit(str(d.get(key, ""))); w.setFixedHeight(24)
                w.textChanged.connect(self._on_bulk_field_debounced)
                d_fields[key] = w; d_form.addRow(lbl + ":", w)

            # Donor HLA
            d_form.addRow(QLabel(f"<b>Donor {di+1} HLA</b>"), QLabel(""))
            d_hla  = {}
            d_hla_data = d.get("hla", {})
            for locus in HLA_LOCI:
                alleles = d_hla_data.get(locus, ["", ""])
                a1_val  = alleles[0] if len(alleles) > 0 else ""
                a2_val  = alleles[1] if len(alleles) > 1 else ""
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

        # Save button for this case
        save_btn = QPushButton(f"Apply Edits to Case {idx+1}")
        save_btn.setStyleSheet(GENERATE_BTN_STYLE)
        save_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        save_btn.clicked.connect(lambda: self._flush_bulk_edits(self._bulk_current_row))
        self._bulk_editor_layout.addWidget(save_btn)
        self._bulk_editor_layout.addStretch()

        # Auto-generate preview for this case
        QTimer.singleShot(200, self._refresh_bulk_preview)

    def _flush_bulk_edits(self, idx):
        """Read current form field values and write back to cases[idx]."""
        if idx < 0 or idx >= len(self.cases): return
        if not self._bulk_fields: return
        case = self.cases[idx]
        p    = case["patient"]

        # Patient fields
        for key, w in self._bulk_fields.items():
            p[key] = w.text().strip()

        # Patient HLA
        if not isinstance(p.get("hla"), dict): p["hla"] = {}
        for locus, (a1, a2) in self._bulk_hla_pat.items():
            v1 = a1.text().strip(); v2 = a2.text().strip()
            if v1 or v2: p["hla"][locus] = [v1, v2]
            else:        p["hla"].pop(locus, None)

        # Report meta — use GLOBAL header bar combos as source
        case["report_type"] = TEMPLATE_TO_RTYPE.get(self.template_combo.currentText(), "single_hla")
        case["with_logo"]   = self.logo_combo.currentText() == "With Logo"
        
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
                          json.dumps([d.get("hla", {}) for d in case.get("donors", [])])
        
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
            "pin": "", "sample_number": "", "match": "",
            "collection_date": "", "receipt_date": "",
            "report_date": patient.get("report_date", ""),
            "hla": {}, "hla_c_type": "", "remarks": "",
            "hospital_clinic": patient.get("hospital_clinic", ""),
            "diagnosis":       patient.get("diagnosis", ""),
            "specimen":        patient.get("specimen", "Blood - EDTA"),
        })
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
            self._refresh_manual_preview()
        elif idx == 1: # Bulk Tab
            b_idx = self._bulk_current_row
            if 0 <= b_idx < len(self.cases):
                case = self.cases[b_idx]
                case["report_type"] = TEMPLATE_TO_RTYPE.get(self.template_combo.currentText(), "single_hla")
                case["with_logo"]   = self.logo_combo.currentText() == "With Logo"
                # Rebuild editor because layout might change based on template (RPL extra fields)
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
        # Copy case-level imgt/methodology/typing_status overrides
        for key in ("imgt_release", "methodology", "typing_status", "coverage", "rpl_reference"):
            if case.get(key):
                c[key] = case[key]
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
        QMessageBox.information(self, "Done",
            f"Generated {len(success)} report(s).\nOutput: {self.bulk_output_label.text()}")
        # Status bar message removed as requested


    def _bulk_log(self, msg):
        self.bulk_log.append(msg)

    def save_bulk_draft(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Bulk Draft", BULK_DRAFT_FILE,
            "JSON Files (*.json);;All Files (*)"
        )
        if not path or not self.cases: return
        self._flush_bulk_edits(self._bulk_current_row)
        draft = [{k: v for k, v in c.items() if k != "signatories"}
                 for c in self.cases]
        try:
            with open(path, "w") as fh: json.dump(draft, fh, indent=2, default=str)
            self._bulk_log(f"Draft saved: {os.path.basename(path)} ({len(draft)} cases)")
        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e))

    def load_bulk_draft(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Bulk Draft", os.path.dirname(BULK_DRAFT_FILE),
            "JSON Files (*.json);;All Files (*)"
        )
        if not path: return
        try:
            with open(path) as fh: draft = json.load(fh)
            self.cases = draft
            self._populate_bulk_list()
            self._bulk_log(f"Draft loaded: {os.path.basename(path)} ({len(draft)} cases)")
        except Exception as e:
            QMessageBox.critical(self, "Load Error", str(e))

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
        raw = self.qsettings.value("signatories", "")
        if raw:
            try: return json.loads(raw)
            except Exception: pass
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
            # Rubber seal: only when BOTH the stamp setting is ON and NABL is enabled
            # (non-RPL reports only; sig_stamp=False → seal is never attached)
            if sig_stamp and nabl and rtype != "rpl_couple" and "rayvathy" in sig["name"].lower():
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
