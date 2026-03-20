# HLA Typing Report Automation

Automated HLA NGS typing report generation system for Anderson Diagnostic Services.

## Features

- **Multiple Report Types**:
  - Single HLA Typing (High Resolution)
  - HLA Typing for Transplant Donors
  - RPL (Recurrent Pregnancy Loss) Couple Reports

- **Manual Entry**: Create individual reports with a GUI form
- **Bulk Upload**: Parse Excel files and generate multiple reports in batch
- **Live Preview**: View PDF previews before final generation
- **Customizable Templates**: Configure signatories, logos, and NABL stamps
- **Natural Page Breaks**: Intelligent pagination that flows content naturally across pages

## Installation

### Windows
```bash
setup.bat
```

### Linux/Mac
```bash
chmod +x setup.sh
./setup.sh
```

## Usage

### Windows
```bash
launch.bat
```

### Linux/Mac
```bash
chmod +x launch.sh
./launch.sh
```

## Report Types

### 1. Single HLA Report
- Patient information + HLA alleles
- Automatically selected when no donor is present

### 2. Transplant Donor Report
- Patient + one or more donors
- Match scoring between patient and donors
- Automatically selected for non-RPL donor cases

### 3. RPL Couple Report
- Specialized 3-page format for recurrent pregnancy loss cases
- Maternal and paternal HLA compatibility analysis
- HLA-C supertype classification
- Background and disclaimer sections
- Automatically selected when diagnosis contains "RPL" or relationship is "wife/husband"

## Excel Format

For bulk upload, provide an Excel file with two sheets:
1. **patient-donor detail**: Patient and donor metadata
2. **result data**: HLA allele results for all loci

## Recent Updates

- **Fixed RPL pagination**: Removed forced page breaks, now uses natural flow with KeepTogether
- Page 1 content (couple table + reference) flows naturally to Page 2 when needed
- Methodology, background, disclaimers, and signatures kept together as one cohesive unit
- No more blank spaces or forced breaks

## Directory Structure

```
HLA-Typing-Report/
├── hla_report_generator.py  # Main GUI application
├── hla_template.py           # PDF generation logic
├── hla_data_parser.py        # Excel parsing
├── hla_assets.py             # Asset management (fonts, images, signatures)
├── assets/
│   └── hla/
│       ├── fonts/            # Custom fonts (SegoeUI, Calibri, GillSans)
│       └── raw/              # Headers, footers, signatures, seals
├── template/                 # Reference PDF templates
└── requirements.txt          # Python dependencies
```

## Dependencies

- PyQt6 - GUI framework
- reportlab - PDF generation
- openpyxl - Excel file parsing
- PyMuPDF (fitz) - PDF preview rendering
- Pillow - Image processing

## License

Proprietary - Anderson Diagnostic Services Private Limited
