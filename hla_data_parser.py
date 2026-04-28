"""
hla_data_parser.py
Parses MINISEQ and SURFSEQ Excel files into structured case dictionaries
ready for PDF report generation.
"""

import re
import pandas as pd
from datetime import datetime
from typing import Optional


# ─── HLA-C Supertypes (C1/C2 classification) ────────────────────────────────
# C1 group alleles (incomplete list; covers common alleles in Indian population)
HLA_C_C1 = {
    "C*01", "C*03", "C*07", "C*08", "C*12", "C*13", "C*14", "C*16",
}
# Everything not in C1 is treated as C2 (C*02, C*04, C*05, C*06, C*15, C*17, C*18)


def c_supertype(allele: Optional[str]) -> Optional[str]:
    """Return 'C1' or 'C2' for a given HLA-C allele string."""
    if not allele or allele in ("-", "nan", ""):
        return None
    # Normalise: take first 3 fields e.g. C*07:02:01:01 → C*07
    m = re.match(r"(C\*\d+)", allele)
    if not m:
        return None
    prefix = m.group(1)
    return "C1" if prefix in HLA_C_C1 else "C2"


def _fmt_date(val) -> str:
    """Convert Excel date values to DD-MM-YYYY string.

    Fix 3: Do NOT auto-parse string dates through strptime — that risks swapping
    month and day when the locale interpretation differs (e.g. '11/04/2026' read
    as MM/DD gives November 4 instead of April 11).

    Strategy:
    - datetime / Timestamp objects (from Excel date-serial cells): format directly
      with strftime — the serial → date conversion is unambiguous.
    - String values: split on '/' or '-' and reconstruct as DD-MM-YYYY, trusting
      that the source is already in DD/MM/YYYY (Indian date format convention).
    """
    if pd.isna(val) or str(val).strip() in ("", "nan", "NaT"):
        return ""
    if isinstance(val, datetime):
        return val.strftime("%d-%m-%Y")
    s = str(val).strip()
    parts = re.split(r"[/\-]", s)
    if len(parts) == 3:
        dd, mm, yyyy = parts[0].strip(), parts[1].strip(), parts[2].strip()
        return f"{dd.zfill(2)}-{mm.zfill(2)}-{yyyy}"
    return s


def _clean_str(val) -> str:
    if pd.isna(val) or str(val).strip() in ("nan", "NaT", "None"):
        return ""
    return str(val).strip()


def _norm_col(s) -> str:
    """Normalize a column header: strip whitespace, lowercase, collapse runs of
    whitespace (including newlines) to a single space.  Applied to every column
    in the patient-donor detail sheet so lookups are case- and space-insensitive."""
    return re.sub(r'\s+', ' ', str(s).strip().lower())


# Recognised name prefixes → canonical casing
_PREFIX_MAP = {
    "mr":     "Mr",
    "mrs":    "Mrs",
    "ms":     "Ms",
    "master": "Master",
    "dr":     "Dr",
}


def _sentence_case(val) -> str:
    """
    Convert a text value to sentence case with prefix-aware formatting.

    Rules
    -----
    * Known prefixes (Mr / Mrs / Ms / Master / Dr) are preserved with correct casing.
    * A period immediately after the prefix (e.g. "Mrs.hemalatha") is treated as a
      separator — replaced by a space before processing.
    * The first word of the actual name (after the prefix) is capitalised.
    * All remaining words are lowercased.

    Examples
    --------
    "mr JOHN DOE"     → "Mr John doe"
    "DR jane SMITH"   → "Dr Jane smith"
    "ms aNNa"         → "Ms Anna"
    "Mrs.hemalatha"   → "Mrs Hemalatha"
    "dr.ravi kumar"   → "Dr Ravi kumar"
    "JOHN DOE"        → "John doe"
    """
    s = _clean_str(val)
    if not s:
        return s

    # Normalise "prefix.word" → "prefix word"  (e.g. "Mrs.hemalatha", "dr.ravi")
    s = re.sub(r'^(mr|mrs|ms|master|dr)\.(\S)', r'\1 \2', s, flags=re.IGNORECASE)
    # Also strip a lone trailing period on the prefix word (e.g. "Mr. John" → "Mr John")
    s = re.sub(r'^(mr|mrs|ms|master|dr)\.\s+', r'\1 ', s, flags=re.IGNORECASE)

    words = s.split()
    if not words:
        return s

    # Check whether the first word is a known prefix (ignore trailing punctuation)
    first_key = words[0].rstrip('.').lower()
    if first_key in _PREFIX_MAP:
        prefix    = _PREFIX_MAP[first_key]
        remaining = words[1:]
        if not remaining:
            return prefix
        # First name word: first char upper, rest lower
        fn = remaining[0].lower()
        fn = fn[0].upper() + fn[1:] if fn else fn
        # Subsequent words: all lowercase
        rest = [w.lower() for w in remaining[1:]]
        return " ".join([prefix, fn] + rest)

    # No recognised prefix — standard sentence case
    lowered = [w.lower() for w in words]
    lowered[0] = lowered[0][0].upper() + lowered[0][1:] if lowered[0] else lowered[0]
    result = " ".join(lowered)
    # Capitalise the first letter after any embedded period (e.g. "Baby.sitara" → "Baby.Sitara")
    result = re.sub(r'\.([a-z])', lambda m: '.' + m.group(1).upper(), result)
    return result


def _clean_allele(val) -> Optional[str]:
    """
    Normalise a single allele: strip prefix, handle dash/null.
    Truncates to 3-field resolution: A*02:11:01:01 → A*02:11:01
    """
    s = _clean_str(val)
    if s in ("-", "", "nan"):
        return None
    # Root-cause fix: collapse ALL whitespace before testing for "Insufficient Data".
    # Excel may store the value with spaces between every character:
    #   "I n s u f f i c i e n t   d a t a"
    # Standard regex fails on that form; collapsed comparison catches every variant.
    if re.sub(r"\s+", "", s).lower() == "insufficientdata":
        return None
    # Truncate to 3-field: keep gene prefix (before *) + first 3 colon fields
    # e.g. "A*02:11:01:01" → "A*02:11:01"  |  "DRB1*04:01:01:01" → "DRB1*04:01:01"
    if "*" in s:
        prefix, fields_str = s.split("*", 1)
        fields = fields_str.split(":")
        s = prefix + "*" + ":".join(fields[:3])
    return s


def _split_alleles(raw: str):
    """
    Split a typing result string that may contain two alleles.
    Handles:  'A*01:01:01:01, A*11:01:01:01'
              'A*01:01:01:01'   (homozygous / single result)
    Returns (allele1, allele2) — allele2 may be None.
    """
    raw = raw.strip().strip('"').strip("'")
    # Remove trailing/leading pipes and extra commentary after |
    raw = raw.split("|")[0].strip()
    # Split on comma or semicolon
    parts = [p.strip() for p in re.split(r"[,;]", raw) if p.strip()]
    a1 = _clean_allele(parts[0]) if len(parts) > 0 else None
    a2 = _clean_allele(parts[1]) if len(parts) > 1 else None
    # If both alleles are identical → homozygous, keep only one
    if a1 and a2 and a1 == a2:
        a2 = None
    return a1, a2


# ─── Parse MINISEQ result data ───────────────────────────────────────────────

def _parse_miniseq_results(df_result: pd.DataFrame) -> dict:
    """
    Returns dict: PIN → {locus: [a1, a2], ...}
    MINISEQ 'result data' sheet: header at row 2, data from row 3.
    """
    # Find header row
    header_row = None
    for i, row in df_result.iterrows():
        if str(row.iloc[0]).strip() == "SampleName":
            header_row = i
            break
    if header_row is None:
        return {}

    df = df_result.iloc[header_row:].copy()
    df.columns = df.iloc[0]
    df = df.iloc[1:].reset_index(drop=True)

    locus_cols = {
        "A":    ("A/1",    "A/2"),
        "B":    ("B/1",    "B/2"),
        "C":    ("C/1",    "C/2"),
        "DPB1": ("DPB1/1", "DPB1/2"),
        "DQB1": ("DQB1/1", "DQB1/2"),
        "DRB1": ("DRB1/1", "DRB1/2"),
    }

    results = {}
    for _, row in df.iterrows():
        sample = _clean_str(row.get("SampleName", ""))
        if not sample or sample == "nan":
            continue
        hla = {}
        for locus, (c1, c2) in locus_cols.items():
            a1 = _clean_allele(str(row.get(c1, "-")))
            a2 = _clean_allele(str(row.get(c2, "-")))
            # Handle homozygous: if a2 is None/empty but a1 exists, duplicate a1
            if a1 and not a2:
                a2 = a1
            hla[locus] = [a1, a2]
        # Comments/remarks
        remarks = _clean_str(row.get("Comments", ""))
        results[sample] = {"hla": hla, "remarks": remarks}

    return results


# ─── Parse SURFSEQ result data ───────────────────────────────────────────────

def _parse_surfseq_results(df_csv: pd.DataFrame) -> dict:
    """
    Returns dict: sample_number (str) → {locus: [a1, a2], ...}

    Excel splits the semicolon-delimited CSV row across two columns:
      Col A: barcode;"LOCUS";    (truncated at cell boundary)
      Col B: ALLELE_VALUE"       (overflow)
    Each row = one locus + one allele for a sample.
    For heterozygous loci, the same locus may appear twice (allele1 then allele2).
    """
    LOCUS_MAP = {
        "HLA_A": "A", "HLA_B": "B", "HLA_C": "C",
        "DRB1": "DRB1", "DRB3": "DRB3", "DRB4": "DRB4", "DRB5": "DRB5",
        "DQB1": "DQB1", "DPB1": "DPB1",
    }

    # raw_results: sample_number → {locus: [allele, ...]}
    raw_results = {}

    for _, row in df_csv.iterrows():
        col_a = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
        col_b = str(row.iloc[1]).strip() if pd.notna(row.iloc[1]) else ""

        if not col_a or col_a == "nan" or ";" not in col_a:
            continue

        # Strip quotes from all tokens and filter out run-prefix tokens
        # (tokens containing _R1/_R2 or matching a run-ID pattern like 8+ leading digits)
        raw_tokens = [t.strip().strip('"').strip("'") for t in col_a.split(";")]
        if col_b and col_b not in ("nan", ""):
            raw_tokens.append(col_b.strip().strip('"').strip("'"))

        barcode = raw_tokens[0] if raw_tokens else ""

        # Filter: remove run-prefix tokens, keep locus headers (HLA_X) and allele tokens (*digits)
        clean_tokens = []
        for t in raw_tokens[1:]:   # skip barcode at index 0
            if not t:
                continue
            if re.search(r"_R[12]\b", t, re.I):   # run suffix _R1 / _R2
                continue
            if re.match(r"\d{6,}", t):             # timestamp / run-ID (6+ leading digits)
                continue
            clean_tokens.append(t)

        # First clean token = locus header; subsequent tokens = alleles
        locus_raw = clean_tokens[0] if clean_tokens else ""
        locus = LOCUS_MAP.get(locus_raw)
        if not locus:
            continue

        allele_tokens = [t for t in clean_tokens[1:] if t and t not in ("-", "nan")]
        if not allele_tokens:
            continue

        # Extract sample number: try HLA-{digits} first, then any 4-6 digit run in barcode
        m = re.search(r"HLA-(\d+)(?:[_\-])", barcode)
        if not m:
            m = re.search(r"[_\-](\d{4,6})[_\-]", barcode)
        if not m:
            continue
        sample_num = m.group(1)

        if sample_num not in raw_results:
            raw_results[sample_num] = {}
        if locus not in raw_results[sample_num]:
            raw_results[sample_num][locus] = []
        for allele_str in allele_tokens:
            raw_results[sample_num][locus].append(allele_str)

    # Convert to [a1, a2] pairs
    results = {}
    for sample_num, loci in raw_results.items():
        hla = {}
        for locus, allele_list in loci.items():
            if len(allele_list) == 1:
                # Single allele — homozygous, duplicate to both positions
                a1 = _clean_allele(allele_list[0])
                a2 = a1  # Duplicate for homozygous
            else:
                # Multiple alleles per locus (hetero) — pick first two
                a1 = _clean_allele(allele_list[0])
                a2 = _clean_allele(allele_list[1]) if len(allele_list) > 1 else None
                # If both alleles are identical, it's homozygous
                if not a2 or a1 == a2:
                    a2 = a1
            hla[locus] = [a1, a2]
        results[sample_num] = {"hla": hla, "remarks": ""}

    return results


# ─── Detect report type ──────────────────────────────────────────────────────

def _detect_report_type(patient_row: pd.Series, donor_rows: list) -> str:
    diag = _clean_str(patient_row.get("diagnosis", "")).upper()
    patient_rel = _clean_str(patient_row.get("relationship", "")).lower()

    # Check diagnosis first
    if "RPL" in diag or "RECURRENT" in diag or "MISCARRIAGE" in diag or "RIF" in diag:
        return "rpl_couple"

    # Check if patient+donor are a couple (wife/husband relationship)
    if donor_rows:
        donor_rels = [_clean_str(d.get("relationship", "")).lower() for d in donor_rows]
        is_couple = (
            patient_rel in ("wife", "husband")
            or any(r in ("wife", "husband") for r in donor_rels)
        )
        if is_couple:
            return "rpl_couple"
        return "transplant_donor"

    return "single_hla"


# ─── Parse match column ──────────────────────────────────────────────────────

def _parse_match(val) -> str:
    """Extract 'N of M at High Resolution (X%)' from any match format.
    Percentage is always auto-calculated from N and M."""
    s = _clean_str(val)
    if not s or s.lower() in ("nan", ""):
        return ""
    m = re.search(r"(\d+)\s+of\s+(\d+)", s, re.I)
    if m:
        matched, total = int(m.group(1)), int(m.group(2))
        pct = round(matched / total * 100) if total else 0
        qualifier = "at High Resolution" if "high resolution" in s.lower() else ""
        base = f"{matched} of {total} {qualifier}".strip()
        return f"{base} ({pct}%)"
    return s.strip()


# ─── Gender/Age combiner ─────────────────────────────────────────────────────

def _build_gender_age(row) -> str:
    """Return a combined 'Gender / Age' string.
    Tries the combined 'Gender / Age' column first; falls back to separate
    'Gender' and 'Age' columns (new Excel format).
    Age normalization (years-only, drop months/days) is applied by
    _normalize_age in the template at render time.
    Handles numeric ages from pandas (e.g. 21.0 → '21') and text ages
    like '21 y 2 months 30 days' which _normalize_age will reduce to '21 Years'."""
    combined = _clean_str(row.get("gender / age", ""))
    if combined:
        return _sentence_case(combined)
    gender  = _sentence_case(row.get("gender", ""))
    raw_age = row.get("age", "")
    # pandas returns numeric Excel cells as float (e.g. 21.0) — convert to bare int string
    if isinstance(raw_age, (int, float)) and not pd.isna(raw_age):
        age = str(int(raw_age))
    else:
        age = _clean_str(raw_age)
    parts = [p for p in (gender, age) if p]
    return " / ".join(parts)


# ─── Build person dict ───────────────────────────────────────────────────────

def _build_person(row: pd.Series, hla_lookup: dict, join_by: str) -> dict:
    """Build a patient or donor dict from a patient-donor detail row."""
    # Determine join key
    if join_by == "pin":
        key = _clean_str(row.get("pin", ""))
    else:  # sample_number
        key = str(row.get("sample number", "")).strip().split(".")[0]

    hla_data = hla_lookup.get(key, {})
    hla = hla_data.get("hla", {locus: [None, None] for locus in ["A", "B", "C", "DRB1", "DQB1", "DPB1"]})
    remarks = hla_data.get("remarks", "")

    # Fix 3 Stage 1: check for "Insufficient Data" BEFORE sanitizing so the
    # filter in hla_report_generator can still detect it via the flag.
    _insuff_re = re.compile(r"insufficient\s*data", re.IGNORECASE)
    _has_insufficient_hla = any(
        a and _insuff_re.search(str(a))
        for alleles in hla.values() for a in (alleles or [])
    )
    # Now sanitize: replace every "Insufficient Data" allele with None so it
    # renders as "—" via the normal fallback and never reaches the PDF as raw text.
    hla = {
        locus: [
            None if (a and _insuff_re.search(str(a))) else a
            for a in (alleles or [])
        ]
        for locus, alleles in hla.items()
    }

    # HLA-C supertype (for RPL)
    c_alleles = hla.get("C", [None, None])
    ct1 = c_supertype(c_alleles[0]) if c_alleles[0] else None
    ct2 = c_supertype(c_alleles[1]) if c_alleles[1] else None
    hla_c_type = ",".join(filter(None, [ct1, ct2])) if (ct1 or ct2) else ""

    # Only use the Excel Remarks/comments column; skip raw instrument Comments
    # (instrument Comments are very long DPB1 allele lists — not suitable for reports)
    excel_remarks = _clean_str(row.get("remarks/comments", ""))
    combined_remarks = excel_remarks  # Instrument remarks excluded intentionally

    return {
        # Text fields → sentence case (first char upper, rest lower)
        "name":           _sentence_case(row.get("name", "")),
        "gender_age":     _build_gender_age(row),
        "diagnosis":      _sentence_case(row.get("diagnosis", "")),
        "referred_by":    _sentence_case(row.get("referred by", "")),
        "hospital_clinic":_sentence_case(row.get("hospital/clinic", "")),
        "specimen":       _sentence_case(row.get("specimen", "")),
        "relationship":   _sentence_case(row.get("relationship", "")),
        "remarks":        _sentence_case(combined_remarks),
        # ID / code fields → left as-is (no case conversion)
        "hospital_mr_no": _clean_str(row.get("hospital mr no", "")),
        "pin":            _clean_str(row.get("pin", "")),
        "sample_number":  str(row.get("sample number", "")).strip().split(".")[0],
        # Date fields (already formatted DD-MM-YYYY)
        "collection_date":_fmt_date(row.get("collection date")),
        "receipt_date":   _fmt_date(row.get("sample receipt date")),
        "report_date":    _fmt_date(row.get("report date")),
        # Computed / structured fields
        "match":          _parse_match(row.get("match", "")),
        "hla":                   hla,
        "hla_c_type":            hla_c_type,
        "_join_key":             key,
        "_has_insufficient_hla": _has_insufficient_hla,  # Fix 3: pre-sanitisation flag
    }


# ─── Compute RPL reference table ─────────────────────────────────────────────

def compute_rpl_reference(patient: dict, donor: dict) -> dict:
    """Compute HLA match counts for RPL reference table."""
    loci = ["A", "B", "C", "DRB1", "DQB1", "DPB1"]
    p_alleles = set()
    d_alleles = set()
    for locus in loci:
        for a in (patient["hla"].get(locus) or []):
            if a:
                p_alleles.add((locus, a))
        for a in (donor["hla"].get(locus) or []):
            if a:
                d_alleles.add((locus, a))

    # Use the pre-computed Match column if available
    match_str = donor.get("match", "")
    m = re.search(r"(\d+)\s+of\s+(\d+)", match_str or "", re.I)
    if m:
        matched = int(m.group(1))
        total = int(m.group(2))
    else:
        matched, total = 0, 12

    pct = round(matched / total * 100) if total else 0

    # Class II loci (DRB1 + DQB1) — compute separately
    class2_p = set()
    class2_d = set()
    for locus in ["DRB1", "DQB1"]:
        for a in (patient["hla"].get(locus) or []):
            if a:
                class2_p.add((locus, a))
        for a in (donor["hla"].get(locus) or []):
            if a:
                class2_d.add((locus, a))
    class2_shared = len(class2_p & class2_d)
    class2_total = max(len(class2_p | class2_d), 1)
    class2_pct = round(class2_shared / 4 * 100)  # out of 4 alleles

    return {
        "match_str":    f"{matched} of {total}",
        "match_pct":    f"{pct}%",
        "class2_pct":   f"{class2_pct}%",
        "hla_sharing_rif": ">50%",
        "hla_c_patient": patient.get("hla_c_type", ""),
        "hla_c_donor":   donor.get("hla_c_type", ""),
    }


# ─── Main parser ─────────────────────────────────────────────────────────────

def parse_excel(filepath: str, nabl: bool = True) -> list:
    """
    Parse a MINISEQ or SURFSEQ Excel file into a list of case dicts.

    Parameters
    ----------
    filepath : str   Path to Excel file.
    nabl     : bool  True = NABL-accredited lab (MINISEQ), False = non-NABL (SURFSEQ).

    Returns
    -------
    List of case dicts, each containing patient, donors[], report_type, etc.
    """
    # ── Determine lab type from filename ──────────────────────────────────────
    fname_upper = filepath.upper()
    is_miniseq = "MINISEQ" in fname_upper
    join_by = "pin" if is_miniseq else "sample_number"

    # ── Read patient-donor detail ─────────────────────────────────────────────
    df_pd = pd.read_excel(filepath, sheet_name="patient-donor detail", header=0)
    # Normalise all column headers: strip, lowercase, collapse whitespace so lookups
    # work regardless of whether the Excel uses "Gender / Age", "GENDER / AGE", etc.
    df_pd.columns = [_norm_col(c) for c in df_pd.columns]

    # ── Read and parse HLA results ────────────────────────────────────────────
    if is_miniseq:
        df_res = pd.read_excel(filepath, sheet_name="result data", header=None)
        hla_lookup = _parse_miniseq_results(df_res)
    else:
        df_csv = pd.read_excel(filepath, sheet_name="complete csv data", header=None)
        hla_lookup = _parse_surfseq_results(df_csv)

    # ── Group rows into cases ─────────────────────────────────────────────────
    cases = []
    current_patient = None
    current_donors = []

    def _flush():
        nonlocal current_patient, current_donors
        if current_patient is None:
            return
        report_type = _detect_report_type(current_patient["_row"], current_donors)

        patient_dict = _build_person(current_patient["_row"], hla_lookup, join_by)

        donors = []
        for d_row in current_donors:
            donors.append(_build_person(d_row, hla_lookup, join_by))

        # RPL reference (use first donor for couple stats)
        rpl_ref = {}
        if report_type == "rpl_couple" and donors:
            rpl_ref = compute_rpl_reference(patient_dict, donors[0])

        # Methodology + IMGT from first row of case
        row0 = current_patient["_row"]
        methodology   = _clean_str(row0.get("methodology", ""))
        imgt_release  = _clean_str(row0.get("imgt/hla release", ""))
        coverage      = _clean_str(row0.get("coverage", ""))
        typing_status = _clean_str(row0.get("typing status complete/incomplete", ""))
        reviewer      = _clean_str(row0.get("this report has been reviewed and approved by", ""))

        cases.append({
            "report_type":    report_type,
            "nabl":           nabl,
            "with_logo":      True,    # default; user can toggle in GUI
            "signature_stamp": False,  # default
            "methodology":    methodology,
            "imgt_release":   imgt_release,
            "coverage":       coverage,
            "typing_status":  typing_status if typing_status else "Complete",
            "reviewer":       reviewer,
            "patient":        patient_dict,
            "donors":         donors,
            "rpl_reference":  rpl_ref,
        })

        current_patient = None
        current_donors = []

    for _, row in df_pd.iterrows():
        role = _clean_str(row.get("patient/donor", "")).lower()
        name = _clean_str(row.get("name", ""))
        if not name:
            continue

        if role == "patient":
            _flush()
            current_patient = {"_row": row}
        elif role == "donor" and current_patient is not None:
            current_donors.append(row)

    _flush()  # last case

    return cases


def get_case_summary(cases: list) -> list:
    """Return a lightweight summary list for the GUI table."""
    summary = []
    for i, case in enumerate(cases):
        p = case["patient"]
        donor_names = [d["name"] for d in case["donors"]]
        summary.append({
            "index":       i,
            "patient":     p["name"],
            "donors":      ", ".join(donor_names) if donor_names else "—",
            "report_type": case["report_type"],
            "diagnosis":   p["diagnosis"],
            "report_date": p["report_date"],
            "nabl":        case["nabl"],
            "status":      case.get("typing_status", "Complete"),
        })
    return summary


# ─── Quick test ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json, sys

    files = [
        ("/data/Sethu/HLA-Typing-Report/TRANSPLANT MINISEQ SAMPLES DATA - SOFTWARE REPORT PREPARE.xlsx", True),
        ("/data/Sethu/HLA-Typing-Report/TRANSPLANT SURFSEQ SAMPLES DATA - SOFTWARE REPORT PREPARE.xlsx", False),
    ]

    for fpath, nabl in files:
        print(f"\n{'='*60}")
        print(f"Parsing: {fpath.split('/')[-1]}")
        cases = parse_excel(fpath, nabl=nabl)
        print(f"Found {len(cases)} cases:")
        for c in cases:
            p = c["patient"]
            donors = c["donors"]
            print(f"\n  Case: {p['name']} | type={c['report_type']} | nabl={c['nabl']}")
            print(f"    PIN={p['pin']}  SampleNo={p['sample_number']}")
            print(f"    Diagnosis: {p['diagnosis']}")
            print(f"    Report date: {p['report_date']}")
            for locus, alleles in p["hla"].items():
                a1, a2 = alleles
                print(f"    {locus:6s}: {a1 or '-':30s}  {a2 or '-'}")
            for d in donors:
                print(f"    DONOR: {d['name']} | rel={d['relationship']} | match={d['match']}")
                for locus, alleles in d["hla"].items():
                    a1, a2 = alleles
                    print(f"      {locus:6s}: {a1 or '-':30s}  {a2 or '-'}")
            if c["rpl_reference"]:
                print(f"    RPL ref: {c['rpl_reference']}")
