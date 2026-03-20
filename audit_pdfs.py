#!/usr/bin/env python3
"""
Complete PDF audit script for HLA Typing Report templates.
Extracts all visual elements: text, colors, rectangles, images, fonts.
"""

import sys
import json
import os

# Add venv site-packages to path
sys.path.insert(0, '/data/Sethu/HLA-Typing-Report/venv/lib/python3.12/site-packages')

import fitz  # PyMuPDF

def rgb_to_hex(r, g, b):
    """Convert 0-1 float RGB to hex string."""
    return "#{:02X}{:02X}{:02X}".format(int(r*255), int(g*255), int(b*255))

def color_to_hex(color):
    """Convert fitz color (int or tuple) to hex."""
    if color is None:
        return None
    if isinstance(color, (int, float)):
        # Grayscale
        v = int(color * 255)
        return "#{:02X}{:02X}{:02X}".format(v, v, v)
    if isinstance(color, (list, tuple)):
        if len(color) == 3:
            return rgb_to_hex(color[0], color[1], color[2])
        elif len(color) == 4:
            # CMYK
            c, m, y, k = color
            r = 1 - min(1, c + k)
            g = 1 - min(1, m + k)
            b = 1 - min(1, y + k)
            return rgb_to_hex(r, g, b)
    return str(color)

def audit_page(page, page_num):
    """Extract all visual elements from a page."""
    result = {
        "page_num": page_num,
        "width": page.rect.width,
        "height": page.rect.height,
        "text_blocks": [],
        "drawings": [],
        "images": [],
        "fill_colors": {},
        "stroke_colors": {},
    }

    # --- TEXT EXTRACTION ---
    blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
    for block in blocks:
        if block["type"] == 0:  # text block
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    color_int = span.get("color", 0)
                    r = (color_int >> 16) & 0xFF
                    g = (color_int >> 8) & 0xFF
                    b = color_int & 0xFF
                    text_color = "#{:02X}{:02X}{:02X}".format(r, g, b)
                    flags = span.get("flags", 0)
                    is_bold = bool(flags & 2**4)
                    is_italic = bool(flags & 2**1)

                    result["text_blocks"].append({
                        "text": span["text"],
                        "font": span["font"],
                        "size": round(span["size"], 2),
                        "color": text_color,
                        "bold": is_bold,
                        "italic": is_italic,
                        "x0": round(span["origin"][0], 2),
                        "y0": round(span["origin"][1], 2),
                        "bbox": [round(v, 2) for v in span["bbox"]],
                    })

    # --- DRAWINGS (rectangles, lines, paths) ---
    drawings = page.get_drawings()
    for d in drawings:
        fill_hex = color_to_hex(d.get("fill"))
        stroke_hex = color_to_hex(d.get("color"))

        drawing_info = {
            "type": d.get("type"),
            "rect": [round(v, 2) for v in d.get("rect", [0,0,0,0])],
            "fill": fill_hex,
            "stroke": stroke_hex,
            "width": d.get("width"),
            "opacity": d.get("fill_opacity"),
        }
        result["drawings"].append(drawing_info)

        # Collect fill colors with counts
        if fill_hex:
            result["fill_colors"][fill_hex] = result["fill_colors"].get(fill_hex, 0) + 1
        if stroke_hex:
            result["stroke_colors"][stroke_hex] = result["stroke_colors"].get(stroke_hex, 0) + 1

    # --- IMAGES ---
    image_list = page.get_images(full=True)
    for img_idx, img in enumerate(image_list):
        xref = img[0]
        # Get image placement
        for item in page.get_image_rects(xref):
            result["images"].append({
                "xref": xref,
                "index": img_idx,
                "rect": [round(v, 2) for v in item],
                "width_pts": round(item.width, 2),
                "height_pts": round(item.height, 2),
                "x0": round(item.x0, 2),
                "y0": round(item.y0, 2),
            })

    return result

def audit_pdf(pdf_path):
    """Audit an entire PDF file."""
    doc = fitz.open(pdf_path)
    result = {
        "path": pdf_path,
        "filename": os.path.basename(pdf_path),
        "num_pages": len(doc),
        "pages": []
    }

    for page_num, page in enumerate(doc):
        page_data = audit_page(page, page_num + 1)
        result["pages"].append(page_data)

    doc.close()
    return result

def print_page_report(pdf_data, page_data):
    """Print a formatted report for one page."""
    print(f"\n{'='*80}")
    print(f"  PAGE {page_data['page_num']} — {page_data['width']:.1f} x {page_data['height']:.1f} pts")
    print(f"{'='*80}")

    # Text blocks
    print(f"\n--- TEXT BLOCKS ({len(page_data['text_blocks'])}) ---")
    for tb in page_data['text_blocks']:
        bold_str = " [BOLD]" if tb['bold'] else ""
        italic_str = " [ITALIC]" if tb['italic'] else ""
        print(f"  [{tb['x0']:6.1f}, {tb['y0']:6.1f}]  size={tb['size']:5.1f}  color={tb['color']}  "
              f"font={tb['font']}{bold_str}{italic_str}  |  \"{tb['text']}\"")

    # Fill colors summary
    if page_data['fill_colors']:
        print(f"\n--- FILL COLORS (unique: {len(page_data['fill_colors'])}) ---")
        for hex_col, count in sorted(page_data['fill_colors'].items()):
            print(f"  {hex_col}  count={count}")

    # Stroke colors summary
    if page_data['stroke_colors']:
        print(f"\n--- STROKE COLORS (unique: {len(page_data['stroke_colors'])}) ---")
        for hex_col, count in sorted(page_data['stroke_colors'].items()):
            print(f"  {hex_col}  count={count}")

    # Drawings (rects)
    print(f"\n--- DRAWINGS/RECTANGLES ({len(page_data['drawings'])}) ---")
    for d in page_data['drawings']:
        r = d['rect']
        w = round(r[2]-r[0], 2)
        h = round(r[3]-r[1], 2)
        print(f"  [{r[0]:6.1f},{r[1]:6.1f},{r[2]:6.1f},{r[3]:6.1f}]  w={w:6.1f} h={h:5.1f}  "
              f"fill={str(d['fill']):9s}  stroke={str(d['stroke']):9s}  lw={d['width']}")

    # Images
    if page_data['images']:
        print(f"\n--- IMAGES ({len(page_data['images'])}) ---")
        for img in page_data['images']:
            print(f"  xref={img['xref']}  pos=[{img['x0']:.1f},{img['y0']:.1f}]  "
                  f"size={img['width_pts']:.1f}x{img['height_pts']:.1f} pts")

def print_pdf_report(pdf_data):
    """Print full report for a PDF."""
    print(f"\n{'#'*80}")
    print(f"  PDF: {pdf_data['filename']}")
    print(f"  Path: {pdf_data['path']}")
    print(f"  Pages: {pdf_data['num_pages']}")
    print(f"{'#'*80}")

    for page_data in pdf_data['pages']:
        print_page_report(pdf_data, page_data)


# ============================================================
# MAIN: Audit all PDFs
# ============================================================

pdfs_to_audit = [
    # Template A - NGS
    "/data/Sethu/HLA-Typing-Report/template/Dummy_NGS High Resolution 28.10.2025.pdf",
    "/data/Sethu/HLA-Typing-Report/template/Single Dummy_NGS High Resolution.pdf",
    # Template A - Populated
    "/data/Sethu/HLA-Typing-Report/Manual-Report/NABL/Baby.Sitrarasan_HLA_NGS_WITH_LOGO .pdf",
    "/data/Sethu/HLA-Typing-Report/Manual-Report/NABL/Baby_Shaik_Neha_Sri_Baby_Shaik_Prashid_Baby_Shaik_Rithik_HLA_NGS.pdf",
    "/data/Sethu/HLA-Typing-Report/Manual-Report/Non-NABL/Mr.Punniyamoorthy_ Mr.Hariharan_HLA_NGS_WITH LOGO.pdf",
    # Template B - RPL
    "/data/Sethu/HLA-Typing-Report/template/HLA fertility _RPL_WITH LOGO.pdf",
    # Template B - Populated
    "/data/Sethu/HLA-Typing-Report/Manual-Report/NABL/Mrs.Hemalatha_ Mr.Vinoth_RPL_WITH_LOGO.pdf",
    "/data/Sethu/HLA-Typing-Report/Manual-Report/Non-NABL/Mrs.Nayana Golhar_RPL_WITH LOGO.pdf",
]

all_data = {}
for pdf_path in pdfs_to_audit:
    if not os.path.exists(pdf_path):
        print(f"WARNING: File not found: {pdf_path}")
        continue
    print(f"Auditing: {pdf_path}")
    data = audit_pdf(pdf_path)
    all_data[pdf_path] = data
    print_pdf_report(data)

# Save JSON for further analysis
with open("/data/Sethu/HLA-Typing-Report/audit_data.json", "w") as f:
    json.dump(all_data, f, indent=2, default=str)

print("\n\nAudit complete. JSON saved to audit_data.json")
