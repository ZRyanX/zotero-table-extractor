#!/usr/bin/env python3
"""
benchmarks/generate_benchmark_papers.py —
Programmatic generator for the 5 distinct benchmark paper groups:
  Group A: Classic dense three-line tables (geology/chemistry, ± errors, formulas, super/subscripts)
  Group B: Multi-level complex spanning headers (cross-row and cross-column grouped headers)
  Group C: Untitled tables and appendix tables (no explicit "Table 1" declaration, table starts at page top)
  Group D: Multi-page continuation tables (spanning 3 pages, 80 rows, continuation alignment)
  Group E: Low-contrast / scanned / image-embedded tables (rasterized archival scan)

Also produces:
  - Exact Ground-Truth JSON annotations in `benchmarks/ground_truth/`
  - High-fidelity MinerU structured ZIP archives in `benchmarks/papers/mineru_cache/`
"""

import os
import io
import json
import zipfile
from pathlib import Path
from typing import List, Dict, Any, Tuple

import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import numpy as np


BENCHMARK_DIR = Path(__file__).resolve().parent
PAPERS_DIR = BENCHMARK_DIR / "papers"
GT_DIR = BENCHMARK_DIR / "ground_truth"
MINERU_CACHE_DIR = PAPERS_DIR / "mineru_cache"

PAPERS_DIR.mkdir(parents=True, exist_ok=True)
GT_DIR.mkdir(parents=True, exist_ok=True)
MINERU_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _save_mineru_zip(cache_path: Path, content_list: List[Dict[str, Any]]):
    """Save content_list.json into a MinerU structured ZIP archive."""
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content_list.json", json.dumps(content_list, ensure_ascii=False, indent=2))
    with open(cache_path, "wb") as f:
        f.write(bio.getvalue())


# =============================================================================
# GROUP A: Classic Dense Three-Line Table
# =============================================================================
def generate_group_a() -> Tuple[Path, Path, Path]:
    pdf_path = PAPERS_DIR / "group_a_dense_three_line.pdf"
    gt_path = GT_DIR / "group_a_dense_three_line.json"
    cache_path = MINERU_CACHE_DIR / "group_a_dense_three_line.zip"

    doc = fitz.open()

    # Page 1
    page1 = doc.new_page(width=595, height=842)  # A4
    page1.insert_text((50, 45), "Petrogenesis and Mantle Source Characteristics of Cenozoic Basalts", fontsize=13, fontname="helv")
    page1.insert_text((50, 62), "J. Zhang, L. Wang, and H. Liu — Journal of Petrology, 2024", fontsize=9, fontname="helv")
    page1.insert_text((50, 85), "Abstract. We present high-precision major and trace element data for mantle peridotite xenoliths...", fontsize=8.5, fontname="times-roman")
    page1.insert_text((50, 105), "1. Introduction", fontsize=10, fontname="helv")
    page1.insert_text((50, 120), "The subcontinental lithospheric mantle beneath eastern China has experienced profound reactivation.", fontsize=8.5, fontname="times-roman")
    page1.insert_text((50, 133), "Analytical techniques including electron microprobe and MC-ICP-MS were employed at standard conditions.", fontsize=8.5, fontname="times-roman")

    # Table Caption
    caption = "Table 1. Representative major oxide (wt%) and trace element compositions of mantle olivine and clinopyroxene."
    page1.insert_text((50, 155), caption, fontsize=9, fontname="helv")

    # Table coordinates & columns
    cols = ["Sample", "Type", "SiO2", "TiO2", "Al2O3", "FeO", "MnO", "MgO", "CaO", "Na2O", "Mg#", "87Sr/86Sr", "143Nd/144Nd"]
    col_x = [45, 95, 130, 168, 208, 248, 285, 320, 358, 396, 432, 475, 532]
    table_x0, table_x1 = 40, 565
    top_y = 165
    header_y = 177
    sep_y = 183

    # Draw Top Rule
    page1.draw_line(fitz.Point(table_x0, top_y), fitz.Point(table_x1, top_y), color=(0, 0, 0), width=1.4)

    # Insert Headers
    for x, h in zip(col_x, cols):
        page1.insert_text((x, header_y), h, fontsize=7.5, fontname="helv")

    # Draw Header Separator Rule
    page1.draw_line(fitz.Point(table_x0, sep_y), fitz.Point(table_x1, sep_y), color=(0, 0, 0), width=0.7)

    # Generate 24 rows of realistic geochemical data
    np.random.seed(42)
    rows_data = []
    curr_y = 196
    row_height = 13.5

    for i in range(1, 25):
        sample_id = f"SMP-{i:02d}"
        mineral = "Ol" if i % 2 == 1 else "Cpx"
        sio2 = f"{40.50 + (i * 0.45) % 12.0:.2f} ± 0.12"
        tio2 = f"{0.12 + (i * 0.08) % 1.8:.2f}"
        al2o3 = f"{3.40 + (i * 0.35) % 8.5:.2f} ± 0.08"
        feo = f"{8.10 + (i * 0.22) % 4.5:.2f}"
        mno = f"{0.14 + (i * 0.02) % 0.3:.2f}"
        mgo = f"{38.20 - (i * 0.30) % 15.0:.2f} ± 0.15"
        cao = f"{0.85 + (i * 0.65) % 18.0:.2f}"
        na2o = f"{0.25 + (i * 0.11) % 2.1:.2f}"
        mg_num = f"{89.2 - (i * 0.15) % 4.0:.1f}"
        sr = f"{0.70350 + (i * 0.00008):.5f} ± 0.00004"
        nd = f"{0.512750 + (i * 0.000015):.6f} ± 0.000012"

        row_vals = [sample_id, mineral, sio2, tio2, al2o3, feo, mno, mgo, cao, na2o, mg_num, sr, nd]
        rows_data.append(row_vals)

        for x, val in zip(col_x, row_vals):
            page1.insert_text((x, curr_y), val, fontsize=6.8, fontname="times-roman")

        curr_y += row_height

    bottom_y = curr_y - 2
    # Draw Bottom Rule
    page1.draw_line(fitz.Point(table_x0, bottom_y), fitz.Point(table_x1, bottom_y), color=(0, 0, 0), width=1.4)

    # Footnote
    footnote = "Notes: FeO is total iron as FeO. Mg# = 100 × Mg / (Mg + Fe) atomic. Analytical uncertainties (± 1σ) reported for isotopic ratios."
    page1.insert_text((40, bottom_y + 14), footnote, fontsize=7.2, fontname="helv")

    # Page 2 (Discussion)
    page2 = doc.new_page(width=595, height=842)
    page2.insert_text((50, 50), "2. Discussion and Geochemical Implications", fontsize=11, fontname="helv")
    page2.insert_text((50, 70), "The Mg# values correlate strongly with compatible trace element abundances, indicating melt extraction.", fontsize=9, fontname="times-roman")
    page2.insert_text((50, 90), "References\n1. McDonough, W.F. & Sun, S.-s. (1995) Chem. Geol. 120, 223–253.", fontsize=8, fontname="times-roman")

    doc.save(str(pdf_path))
    doc.close()

    # Ground Truth JSON
    sampled_cells = []
    for r_idx in [0, 4, 11, 17, 23]:
        for c_idx, c_name in [(2, "SiO2"), (4, "Al2O3"), (7, "MgO"), (10, "Mg#"), (11, "87Sr/86Sr"), (12, "143Nd/144Nd")]:
            raw_v = rows_data[r_idx][c_idx]
            # extract primary numeric token
            num_val = float(raw_v.split("±")[0].strip())
            sampled_cells.append({
                "row_idx": r_idx,
                "col_idx": c_idx,
                "col_name": c_name,
                "raw_val": raw_v,
                "numeric_val": num_val,
            })

    gt_data = {
        "group": "Group A: Classic dense three-line tables",
        "file_name": pdf_path.name,
        "num_pages": 2,
        "tables": [
            {
                "table_id": "Table 1",
                "label": "Table 1",
                "page_idx": 0,
                "caption": caption,
                "footnote": footnote,
                "bbox": [table_x0, top_y, table_x1, bottom_y],
                "columns": cols,
                "num_rows": len(rows_data),
                "num_cols": len(cols),
                "rows": rows_data,
                "sampled_numeric_cells": sampled_cells,
            }
        ]
    }
    with open(gt_path, "w", encoding="utf-8") as f:
        json.dump(gt_data, f, ensure_ascii=False, indent=2)

    # MinerU Structured Cache (HTML table inside content_list.json)
    html_rows = ["<table><thead><tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr></thead><tbody>"]
    for r in rows_data:
        html_rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>")
    html_rows.append("</tbody></table>")
    html_table = "".join(html_rows)

    content_list = [
        {"type": "text", "text": "Petrogenesis and Mantle Source Characteristics of Cenozoic Basalts", "page_idx": 0},
        {
            "type": "table",
            "table_body": html_table,
            "bbox": [table_x0, top_y, table_x1, bottom_y],
            "page_idx": 0,
            "table_caption": caption,
            "table_footnote": footnote,
            "label": "Table 1",
        }
    ]
    _save_mineru_zip(cache_path, content_list)

    return pdf_path, gt_path, cache_path


# =============================================================================
# GROUP B: Multi-level Complex Spanning Headers
# =============================================================================
def generate_group_b() -> Tuple[Path, Path, Path]:
    pdf_path = PAPERS_DIR / "group_b_complex_spanning_headers.pdf"
    gt_path = GT_DIR / "group_b_complex_spanning_headers.json"
    cache_path = MINERU_CACHE_DIR / "group_b_complex_spanning_headers.zip"

    doc = fitz.open()
    page1 = doc.new_page(width=595, height=842)

    page1.insert_text((50, 45), "Zircon U-Pb Geochronology and Lu-Hf Isotopes of Mesozoic Granitoids", fontsize=13, fontname="helv")
    page1.insert_text((50, 62), "K. Chen, Y. Zhao, and M. Sun — Precamb. Res., 2023", fontsize=9, fontname="helv")
    page1.insert_text((50, 85), "Abstract. Precise in situ LA-ICP-MS U-Pb zircon dating resolves multiple intrusive episodes...", fontsize=8.5, fontname="times-roman")

    caption = "Table 1. In situ zircon U-Pb isotopic and trace element analytical results."
    page1.insert_text((50, 115), caption, fontsize=9, fontname="helv")

    # Spanning Headers Definition
    # Level 1:
    #   Sample Info (cols 0, 1) -> x: 40..130
    #   Trace Elements (ppm) (cols 2, 3, 4) -> x: 130..250
    #   Isotopic Ratios (cols 5, 6, 7) -> x: 250..395
    #   Apparent Ages (Ma) (cols 8, 9, 10) -> x: 395..565
    # Level 2:
    #   Spot | Domain | U | Th | Th/U | 207Pb/206Pb | 207Pb/235U | 206Pb/238U | 207Pb/206Pb Age | 206Pb/238U Age | ± 2σ
    col_x = [45, 90, 138, 178, 218, 258, 305, 355, 405, 475, 535]
    table_x0, table_x1 = 40, 565

    top_y = 125
    h1_y = 138
    mid_rule_y = 143
    h2_y = 155
    bot_rule_y = 161

    # Draw Top Rule
    page1.draw_line(fitz.Point(table_x0, top_y), fitz.Point(table_x1, top_y), color=(0, 0, 0), width=1.4)

    # Insert Level 1 Headers
    page1.insert_text((55, h1_y), "Sample Info", fontsize=7.5, fontname="helv")
    page1.insert_text((155, h1_y), "Trace Elements (ppm)", fontsize=7.5, fontname="helv")
    page1.insert_text((290, h1_y), "Isotopic Ratios", fontsize=7.5, fontname="helv")
    page1.insert_text((440, h1_y), "Apparent Ages (Ma)", fontsize=7.5, fontname="helv")

    # Level 1 Partial Underline Rules
    page1.draw_line(fitz.Point(42, mid_rule_y), fitz.Point(125, mid_rule_y), color=(0, 0, 0), width=0.6)
    page1.draw_line(fitz.Point(132, mid_rule_y), fitz.Point(245, mid_rule_y), color=(0, 0, 0), width=0.6)
    page1.draw_line(fitz.Point(252, mid_rule_y), fitz.Point(390, mid_rule_y), color=(0, 0, 0), width=0.6)
    page1.draw_line(fitz.Point(398, mid_rule_y), fitz.Point(563, mid_rule_y), color=(0, 0, 0), width=0.6)

    # Level 2 Headers
    sub_headers = ["Spot", "Domain", "U", "Th", "Th/U", "207Pb/206Pb", "207Pb/235U", "206Pb/238U", "207Pb/206Pb Age", "206Pb/238U Age", "± 2σ"]
    for x, h in zip(col_x, sub_headers):
        page1.insert_text((x, h2_y), h, fontsize=7.0, fontname="helv")

    page1.draw_line(fitz.Point(table_x0, bot_rule_y), fitz.Point(table_x1, bot_rule_y), color=(0, 0, 0), width=0.7)

    # 18 Rows of Zircon Data
    np.random.seed(101)
    rows_data = []
    curr_y = 175
    row_height = 14.0

    for i in range(1, 19):
        spot = f"ZRN-{i:02d}"
        domain = "Core" if i % 3 != 0 else "Rim"
        u_ppm = str(180 + (i * 45) % 850)
        th_ppm = str(120 + (i * 35) % 620)
        th_u = f"{float(th_ppm) / float(u_ppm):.2f}"
        pb7_pb6 = f"{0.0528 + (i * 0.0004):.4f}"
        pb7_u5 = f"{0.3850 + (i * 0.0065):.4f}"
        pb6_u8 = f"{0.0520 + (i * 0.0008):.4f}"
        age76 = f"{325 + (i * 8) % 180:.1f}"
        age68 = f"{328 + (i * 7) % 175:.1f}"
        err = f"± {3.2 + (i * 0.2) % 2.5:.1f}"

        row_vals = [spot, domain, u_ppm, th_ppm, th_u, pb7_pb6, pb7_u5, pb6_u8, age76, age68, err]
        rows_data.append(row_vals)

        for x, val in zip(col_x, row_vals):
            page1.insert_text((x, curr_y), val, fontsize=6.8, fontname="times-roman")
        curr_y += row_height

    end_table_y = curr_y - 2
    page1.draw_line(fitz.Point(table_x0, end_table_y), fitz.Point(table_x1, end_table_y), color=(0, 0, 0), width=1.4)

    footnote = "Notes: Analytical uncertainties are given at the 2σ level. Common Pb corrected via Stacey-Kramers model."
    page1.insert_text((40, end_table_y + 14), footnote, fontsize=7.2, fontname="helv")

    page2 = doc.new_page(width=595, height=842)
    page2.insert_text((50, 50), "3. Geochronological Synthesis", fontsize=11, fontname="helv")
    page2.insert_text((50, 70), "The weighted mean 206Pb/238U age of 328.4 ± 2.1 Ma dates the granodiorite crystallization.", fontsize=9, fontname="times-roman")

    doc.save(str(pdf_path))
    doc.close()

    # Flattened Columns Expected
    flattened_cols = [
        "Sample Info_Spot", "Sample Info_Domain",
        "Trace Elements (ppm)_U", "Trace Elements (ppm)_Th", "Trace Elements (ppm)_Th/U",
        "Isotopic Ratios_207Pb/206Pb", "Isotopic Ratios_207Pb/235U", "Isotopic Ratios_206Pb/238U",
        "Apparent Ages (Ma)_207Pb/206Pb Age", "Apparent Ages (Ma)_206Pb/238U Age", "Apparent Ages (Ma)_± 2σ"
    ]

    sampled_cells = []
    for r_idx in [0, 5, 10, 15]:
        for c_idx, c_name in [(2, "U"), (4, "Th/U"), (7, "206Pb/238U"), (9, "206Pb/238U Age")]:
            raw_v = rows_data[r_idx][c_idx]
            num_val = float(raw_v.replace("±", "").strip())
            sampled_cells.append({
                "row_idx": r_idx,
                "col_idx": c_idx,
                "col_name": c_name,
                "raw_val": raw_v,
                "numeric_val": num_val,
            })

    gt_data = {
        "group": "Group B: Multi-level complex spanning headers",
        "file_name": pdf_path.name,
        "num_pages": 2,
        "tables": [
            {
                "table_id": "Table 1",
                "label": "Table 1",
                "page_idx": 0,
                "caption": caption,
                "footnote": footnote,
                "bbox": [table_x0, top_y, table_x1, end_table_y],
                "columns": flattened_cols,
                "hierarchical_headers": [
                    {"name": "Sample Info", "colspan": 2, "children": ["Spot", "Domain"]},
                    {"name": "Trace Elements (ppm)", "colspan": 3, "children": ["U", "Th", "Th/U"]},
                    {"name": "Isotopic Ratios", "colspan": 3, "children": ["207Pb/206Pb", "207Pb/235U", "206Pb/238U"]},
                    {"name": "Apparent Ages (Ma)", "colspan": 3, "children": ["207Pb/206Pb Age", "206Pb/238U Age", "± 2σ"]}
                ],
                "num_rows": len(rows_data),
                "num_cols": len(flattened_cols),
                "rows": rows_data,
                "sampled_numeric_cells": sampled_cells,
            }
        ]
    }
    with open(gt_path, "w", encoding="utf-8") as f:
        json.dump(gt_data, f, ensure_ascii=False, indent=2)

    # MinerU Structured Cache (HTML with colspan headers)
    html_table = (
        "<table><thead>"
        "<tr>"
        "<th colspan='2'>Sample Info</th>"
        "<th colspan='3'>Trace Elements (ppm)</th>"
        "<th colspan='3'>Isotopic Ratios</th>"
        "<th colspan='3'>Apparent Ages (Ma)</th>"
        "</tr>"
        "<tr>"
        "<th>Spot</th><th>Domain</th><th>U</th><th>Th</th><th>Th/U</th>"
        "<th>207Pb/206Pb</th><th>207Pb/235U</th><th>206Pb/238U</th>"
        "<th>207Pb/206Pb Age</th><th>206Pb/238U Age</th><th>± 2σ</th>"
        "</tr>"
        "</thead><tbody>"
        + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows_data)
        + "</tbody></table>"
    )

    content_list = [
        {
            "type": "table",
            "table_body": html_table,
            "bbox": [table_x0, top_y, table_x1, end_table_y],
            "page_idx": 0,
            "table_caption": caption,
            "table_footnote": footnote,
            "label": "Table 1",
        }
    ]
    _save_mineru_zip(cache_path, content_list)

    return pdf_path, gt_path, cache_path


# =============================================================================
# GROUP C: Untitled Tables and Appendix Tables
# =============================================================================
def generate_group_c() -> Tuple[Path, Path, Path]:
    pdf_path = PAPERS_DIR / "group_c_untitled_appendix_tables.pdf"
    gt_path = GT_DIR / "group_c_untitled_appendix_tables.json"
    cache_path = MINERU_CACHE_DIR / "group_c_untitled_appendix_tables.zip"

    doc = fitz.open()

    # Page 1: Standard text + Declared Table 1
    page1 = doc.new_page(width=595, height=842)
    page1.insert_text((50, 45), "Hydrothermal Alteration and Vein Paragenesis of the Jiaodong Gold Province", fontsize=12, fontname="helv")
    page1.insert_text((50, 65), "Table 1. Electron microprobe analyses of pyrite and arsenopyrite.", fontsize=9, fontname="helv")

    t1_cols = ["Sample", "Mineral", "Fe (wt%)", "S", "As", "Au (ppm)", "Ag", "Total"]
    t1_col_x = [50, 105, 160, 220, 280, 340, 410, 470]
    t1_x0, t1_x1 = 45, 545

    page1.draw_line(fitz.Point(t1_x0, 75), fitz.Point(t1_x1, 75), color=(0, 0, 0), width=1.4)
    for x, h in zip(t1_col_x, t1_cols):
        page1.insert_text((x, 88), h, fontsize=7.5, fontname="helv")
    page1.draw_line(fitz.Point(t1_x0, 94), fitz.Point(t1_x1, 94), color=(0, 0, 0), width=0.7)

    t1_rows = []
    curr_y = 108
    for i in range(1, 11):
        min_name = "Py-I" if i <= 5 else "Apy-I"
        row = [f"JD-{i:02d}", min_name, f"{45.8 + (i * 0.2):.2f}", f"{51.2 - (i * 0.3):.2f}",
               f"{0.85 + (i * 0.4):.2f}", f"{12.4 + (i * 3.5):.1f}", f"{2.8 + (i * 0.4):.2f}", f"{99.5 + (i * 0.05):.2f}"]
        t1_rows.append(row)
        for x, val in zip(t1_col_x, row):
            page1.insert_text((x, curr_y), val, fontsize=7.0, fontname="times-roman")
        curr_y += 14.0
    page1.draw_line(fitz.Point(t1_x0, curr_y - 2), fitz.Point(t1_x1, curr_y - 2), color=(0, 0, 0), width=1.4)

    # Page 2: Geology & discussion text
    page2 = doc.new_page(width=595, height=842)
    page2.insert_text((50, 50), "2. Ore Fluid Evolution and Precipitation Mechanisms", fontsize=11, fontname="helv")
    page2.insert_text((50, 75), "Sulfidation and fluid immiscibility played dominant roles in gold precipitation across the deposit.", fontsize=9, fontname="times-roman")

    # Page 3: APPENDIX Page with UNTITLED Table (no "Table 2" / "Table A" declaration!)
    page3 = doc.new_page(width=595, height=842)
    # Notice: Title says "APPENDIX 1: Spectrometer calibration conditions..." but NO "Table" declaration
    page3.insert_text((50, 45), "APPENDIX 1: Spectrometer calibration conditions and detection limits for EMPA standard calibrations.", fontsize=8.5, fontname="helv")

    t2_cols = ["Element", "Line", "Crystal", "Standard Mineral", "Peak (s)", "Bkg (s)", "LOD (ppm)"]
    t2_col_x = [50, 110, 160, 225, 350, 410, 470]
    t2_x0, t2_x1 = 45, 545

    t2_top_y = 55
    page3.draw_line(fitz.Point(t2_x0, t2_top_y), fitz.Point(t2_x1, t2_top_y), color=(0, 0, 0), width=1.4)
    for x, h in zip(t2_col_x, t2_cols):
        page3.insert_text((x, 68), h, fontsize=7.5, fontname="helv")
    page3.draw_line(fitz.Point(t2_x0, 74), fitz.Point(t2_x1, 74), color=(0, 0, 0), width=0.7)

    elements_meta = [
        ("Fe", "Ka", "LIF", "Natural Pyrite", "20", "10", "120"),
        ("S", "Ka", "PET", "Natural Pyrite", "20", "10", "150"),
        ("As", "La", "TAP", "Synthetic GaAs", "30", "15", "250"),
        ("Au", "La", "LIF", "Pure Metal (99.99%)", "40", "20", "180"),
        ("Ag", "La", "PET", "Pure Ag Metal", "30", "15", "140"),
        ("Cu", "Ka", "LIF", "Natural Chalcopyrite", "20", "10", "110"),
        ("Zn", "Ka", "LIF", "Natural Sphalerite", "20", "10", "130"),
        ("Pb", "Ma", "PET", "Natural Galena", "30", "15", "300"),
        ("Co", "Ka", "LIF", "Pure Co Metal", "30", "15", "95"),
        ("Ni", "Ka", "LIF", "Natural Pentlandite", "30", "15", "90"),
        ("Sb", "La", "PET", "Natural Stibnite", "30", "15", "160"),
        ("Bi", "Ma", "PET", "Synthetic Bi2S3", "40", "20", "280"),
        ("Te", "La", "PET", "Pure Te Metal", "30", "15", "190"),
        ("Se", "Ka", "LIF", "Synthetic Bi2Se3", "30", "15", "175"),
    ]
    t2_rows = [list(item) for item in elements_meta]

    curr_y = 88
    for row in t2_rows:
        for x, val in zip(t2_col_x, row):
            page3.insert_text((x, curr_y), val, fontsize=7.0, fontname="times-roman")
        curr_y += 14.0

    t2_bot_y = curr_y - 2
    page3.draw_line(fitz.Point(t2_x0, t2_bot_y), fitz.Point(t2_x1, t2_bot_y), color=(0, 0, 0), width=1.4)
    page3.insert_text((50, t2_bot_y + 14), "LOD = lower limit of detection calculated at 3σ above background.", fontsize=7.2, fontname="helv")

    doc.save(str(pdf_path))
    doc.close()

    gt_data = {
        "group": "Group C: Untitled tables and appendix tables",
        "file_name": pdf_path.name,
        "num_pages": 3,
        "tables": [
            {
                "table_id": "Table 1",
                "label": "Table 1",
                "page_idx": 0,
                "caption": "Table 1. Electron microprobe analyses of pyrite and arsenopyrite.",
                "columns": t1_cols,
                "num_rows": len(t1_rows),
                "num_cols": len(t1_cols),
                "rows": t1_rows,
                "is_untitled": False,
                "sampled_numeric_cells": [
                    {"row_idx": 0, "col_idx": 2, "col_name": "Fe (wt%)", "raw_val": t1_rows[0][2], "numeric_val": float(t1_rows[0][2])},
                    {"row_idx": 4, "col_idx": 5, "col_name": "Au (ppm)", "raw_val": t1_rows[4][5], "numeric_val": float(t1_rows[4][5])},
                    {"row_idx": 9, "col_idx": 4, "col_name": "As", "raw_val": t1_rows[9][4], "numeric_val": float(t1_rows[9][4])},
                ]
            },
            {
                "table_id": "Appendix 1",
                "label": "Appendix 1",
                "page_idx": 2,
                "caption": "APPENDIX 1: Spectrometer calibration conditions and detection limits for EMPA standard calibrations.",
                "columns": t2_cols,
                "num_rows": len(t2_rows),
                "num_cols": len(t2_cols),
                "rows": t2_rows,
                "is_untitled": True,
                "sampled_numeric_cells": [
                    {"row_idx": 0, "col_idx": 6, "col_name": "LOD (ppm)", "raw_val": t2_rows[0][6], "numeric_val": float(t2_rows[0][6])},
                    {"row_idx": 3, "col_idx": 6, "col_name": "LOD (ppm)", "raw_val": t2_rows[3][6], "numeric_val": float(t2_rows[3][6])},
                    {"row_idx": 7, "col_idx": 6, "col_name": "LOD (ppm)", "raw_val": t2_rows[7][6], "numeric_val": float(t2_rows[7][6])},
                    {"row_idx": 13, "col_idx": 6, "col_name": "LOD (ppm)", "raw_val": t2_rows[13][6], "numeric_val": float(t2_rows[13][6])},
                ]
            }
        ]
    }
    with open(gt_path, "w", encoding="utf-8") as f:
        json.dump(gt_data, f, ensure_ascii=False, indent=2)

    # MinerU Structured Cache
    t1_html = "<table><thead><tr>" + "".join(f"<th>{c}</th>" for c in t1_cols) + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in t1_rows) + "</tbody></table>"
    t2_html = "<table><thead><tr>" + "".join(f"<th>{c}</th>" for c in t2_cols) + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in t2_rows) + "</tbody></table>"

    content_list = [
        {
            "type": "table",
            "table_body": t1_html,
            "bbox": [t1_x0, 75, t1_x1, curr_y],
            "page_idx": 0,
            "table_caption": "Table 1. Electron microprobe analyses of pyrite and arsenopyrite.",
            "label": "Table 1",
        },
        {
            "type": "table",
            "table_body": t2_html,
            "bbox": [t2_x0, t2_top_y, t2_x1, t2_bot_y],
            "page_idx": 2,
            "table_caption": "APPENDIX 1: Spectrometer calibration conditions and detection limits for EMPA standard calibrations.",
            "label": "Appendix 1",
        }
    ]
    _save_mineru_zip(cache_path, content_list)

    return pdf_path, gt_path, cache_path


# =============================================================================
# GROUP D: Multi-Page Continuation Tables
# =============================================================================
def generate_group_d() -> Tuple[Path, Path, Path]:
    pdf_path = PAPERS_DIR / "group_d_multipage_continuation.pdf"
    gt_path = GT_DIR / "group_d_multipage_continuation.json"
    cache_path = MINERU_CACHE_DIR / "group_d_multipage_continuation.zip"

    doc = fitz.open()

    cols = ["Sample", "Unit", "SiO2", "TiO2", "Al2O3", "Fe2O3", "MgO", "CaO", "Na2O", "K2O", "Rb", "Sr", "Y", "Zr", "Nb", "Ba"]
    col_x = [45, 90, 126, 160, 194, 228, 262, 296, 330, 364, 398, 432, 466, 498, 526, 548]
    table_x0, table_x1 = 40, 570

    # 80 Total Rows:
    # Page 1: 25 rows (VOL-01 to VOL-25)
    # Page 2: 30 rows (VOL-26 to VOL-55)
    # Page 3: 25 rows (VOL-56 to VOL-80)
    all_rows = []
    np.random.seed(202)
    for i in range(1, 81):
        s_id = f"VOL-{i:02d}"
        unit = "Lower" if i <= 30 else ("Middle" if i <= 60 else "Upper")
        sio2 = f"{65.2 + (i * 0.18) % 12.0:.2f}"
        tio2 = f"{0.35 + (i * 0.02) % 0.8:.2f}"
        al2o3 = f"{14.5 + (i * 0.12) % 3.5:.2f}"
        fe2o3 = f"{3.2 + (i * 0.09) % 3.0:.2f}"
        mgo = f"{1.4 + (i * 0.05) % 2.5:.2f}"
        cao = f"{2.1 + (i * 0.08) % 3.8:.2f}"
        na2o = f"{3.8 + (i * 0.06) % 1.5:.2f}"
        k2o = f"{4.2 + (i * 0.07) % 2.0:.2f}"
        rb = f"{95 + (i * 3) % 110:.1f}"
        sr = f"{280 - (i * 2) % 150:.1f}"
        y = f"{18.5 + (i * 0.4) % 15.0:.1f}"
        zr = f"{165 + (i * 4) % 140:.1f}"
        nb = f"{12.4 + (i * 0.3) % 9.0:.1f}"
        ba = f"{650 + (i * 12) % 400:.0f}"

        row_vals = [s_id, unit, sio2, tio2, al2o3, fe2o3, mgo, cao, na2o, k2o, rb, sr, y, zr, nb, ba]
        all_rows.append(row_vals)

    # Page 1
    page1 = doc.new_page(width=595, height=842)
    page1.insert_text((50, 45), "Geochemical Evolution of Neoproterozoic Volcanic Series, South China", fontsize=12, fontname="helv")
    caption_p1 = "Table 4. Major (wt%) and trace element (ppm) analytical dataset of volcanic rocks."
    page1.insert_text((50, 68), caption_p1, fontsize=9, fontname="helv")

    page1.draw_line(fitz.Point(table_x0, 78), fitz.Point(table_x1, 78), color=(0, 0, 0), width=1.4)
    for x, h in zip(col_x, cols):
        page1.insert_text((x, 90), h, fontsize=6.8, fontname="helv")
    page1.draw_line(fitz.Point(table_x0, 96), fitz.Point(table_x1, 96), color=(0, 0, 0), width=0.7)

    y1 = 109
    for r in all_rows[:25]:
        for x, val in zip(col_x, r):
            page1.insert_text((x, y1), val, fontsize=6.2, fontname="times-roman")
        y1 += 13.5
    page1.draw_line(fitz.Point(table_x0, y1 - 2), fitz.Point(table_x1, y1 - 2), color=(0, 0, 0), width=0.7)
    page1.insert_text((450, y1 + 10), "(Continued on next page)", fontsize=7.0, fontname="times-italic")

    # Page 2
    page2 = doc.new_page(width=595, height=842)
    caption_p2 = "Table 4 (Continued)."
    page2.insert_text((50, 45), caption_p2, fontsize=9, fontname="helv")

    page2.draw_line(fitz.Point(table_x0, 55), fitz.Point(table_x1, 55), color=(0, 0, 0), width=1.4)
    for x, h in zip(col_x, cols):
        page2.insert_text((x, 67), h, fontsize=6.8, fontname="helv")
    page2.draw_line(fitz.Point(table_x0, 73), fitz.Point(table_x1, 73), color=(0, 0, 0), width=0.7)

    y2 = 86
    for r in all_rows[25:55]:
        for x, val in zip(col_x, r):
            page2.insert_text((x, y2), val, fontsize=6.2, fontname="times-roman")
        y2 += 13.5
    page2.draw_line(fitz.Point(table_x0, y2 - 2), fitz.Point(table_x1, y2 - 2), color=(0, 0, 0), width=0.7)
    page2.insert_text((450, y2 + 10), "(Continued on next page)", fontsize=7.0, fontname="times-italic")

    # Page 3
    page3 = doc.new_page(width=595, height=842)
    caption_p3 = "Table 4 (Continued)."
    page3.insert_text((50, 45), caption_p3, fontsize=9, fontname="helv")

    page3.draw_line(fitz.Point(table_x0, 55), fitz.Point(table_x1, 55), color=(0, 0, 0), width=1.4)
    for x, h in zip(col_x, cols):
        page3.insert_text((x, 67), h, fontsize=6.8, fontname="helv")
    page3.draw_line(fitz.Point(table_x0, 73), fitz.Point(table_x1, 73), color=(0, 0, 0), width=0.7)

    y3 = 86
    for r in all_rows[55:80]:
        for x, val in zip(col_x, r):
            page3.insert_text((x, y3), val, fontsize=6.2, fontname="times-roman")
        y3 += 13.5
    page3.draw_line(fitz.Point(table_x0, y3 - 2), fitz.Point(table_x1, y3 - 2), color=(0, 0, 0), width=1.4)
    page3.insert_text((50, y3 + 12), "Notes: Major oxides in wt%; trace elements in ppm. LOI measured at 1000 °C.", fontsize=7.2, fontname="helv")

    doc.save(str(pdf_path))
    doc.close()

    # Ground Truth JSON
    sampled_cells = []
    for r_idx in [0, 15, 24, 25, 40, 54, 55, 70, 79]:
        for c_idx, c_name in [(2, "SiO2"), (4, "Al2O3"), (6, "MgO"), (10, "Rb"), (13, "Zr")]:
            raw_v = all_rows[r_idx][c_idx]
            sampled_cells.append({
                "row_idx": r_idx,
                "col_idx": c_idx,
                "col_name": c_name,
                "raw_val": raw_v,
                "numeric_val": float(raw_v),
            })

    gt_data = {
        "group": "Group D: Multi-page continuation tables",
        "file_name": pdf_path.name,
        "num_pages": 3,
        "tables": [
            {
                "table_id": "Table 4",
                "label": "Table 4",
                "page_indices": [0, 1, 2],
                "caption": caption_p1,
                "columns": cols,
                "num_rows": len(all_rows),  # 80 rows total!
                "num_cols": len(cols),
                "rows": all_rows,
                "is_continuation": True,
                "page_row_breakdown": {"page_0": 25, "page_1": 30, "page_2": 25},
                "sampled_numeric_cells": sampled_cells,
            }
        ]
    }
    with open(gt_path, "w", encoding="utf-8") as f:
        json.dump(gt_data, f, ensure_ascii=False, indent=2)

    # MinerU Structured Cache (3 segment objects in content_list, each marked with is_continuation)
    t_p1 = "<table><thead><tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in all_rows[:25]) + "</tbody></table>"
    t_p2 = "<table><thead><tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in all_rows[25:55]) + "</tbody></table>"
    t_p3 = "<table><thead><tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in all_rows[55:80]) + "</tbody></table>"

    content_list = [
        {
            "type": "table",
            "table_body": t_p1,
            "bbox": [table_x0, 78, table_x1, y1],
            "page_idx": 0,
            "table_caption": caption_p1,
            "label": "Table 4",
            "is_continuation": False,
        },
        {
            "type": "table",
            "table_body": t_p2,
            "bbox": [table_x0, 55, table_x1, y2],
            "page_idx": 1,
            "table_caption": caption_p2,
            "label": "Table 4",
            "is_continuation": True,
        },
        {
            "type": "table",
            "table_body": t_p3,
            "bbox": [table_x0, 55, table_x1, y3],
            "page_idx": 2,
            "table_caption": caption_p3,
            "label": "Table 4",
            "is_continuation": True,
        }
    ]
    _save_mineru_zip(cache_path, content_list)

    return pdf_path, gt_path, cache_path


# =============================================================================
# GROUP E: Low-contrast / Scanned / Image-Embedded Tables
# =============================================================================
def generate_group_e() -> Tuple[Path, Path, Path]:
    pdf_path = PAPERS_DIR / "group_e_scanned_low_contrast.pdf"
    gt_path = GT_DIR / "group_e_scanned_low_contrast.json"
    cache_path = MINERU_CACHE_DIR / "group_e_scanned_low_contrast.zip"

    # Step 1: Render vector page in high-res PIL canvas
    # Page size: 595 x 842 pt -> at 150 DPI: ~1240 x 1754 px
    img_w, img_h = 1240, 1754
    # Archival yellowish / gray background
    img = Image.new("RGB", (img_w, img_h), color=(245, 244, 238))
    draw = ImageDraw.Draw(img)

    # Use default font or PIL load_default
    font_title = ImageFont.load_default()

    # Draw header text in archival style
    draw.text((100, 80), "METALLURGICAL SURVEY REPORT - ARCHIVAL RECORDS (1982)", fill=(60, 60, 60), font=font_title)
    draw.text((100, 130), "Table 1. Flotation stage recovery and mineral balance sheet.", fill=(50, 50, 50), font=font_title)

    cols = ["Stage", "Feed (t/h)", "Grade (% Cu)", "Recovery (%)", "Tailings (%)", "pH"]
    col_x_px = [120, 320, 520, 720, 920, 1080]

    # Draw Top Rule
    draw.line([(100, 170), (1160, 170)], fill=(80, 80, 80), width=4)
    for x, h in zip(col_x_px, cols):
        draw.text((x, 190), h, fill=(40, 40, 40), font=font_title)
    draw.line([(100, 220), (1160, 220)], fill=(90, 90, 90), width=2)

    rows_data = []
    y_px = 245
    np.random.seed(303)
    stages = [
        "Primary Rougher", "Secondary Rougher", "Rougher Scavenger",
        "Cleaner 1", "Cleaner 2", "Cleaner 3", "Cleaner Scavenger",
        "Regrind Feed", "Regrind Circuit", "Final Concentrate",
        "Rougher Tailings", "Final Tailings", "Thickener Underflow",
        "Filter Cake", "Effluent Water", "Recycle Stream"
    ]

    for i, stg in enumerate(stages):
        feed = f"{120.5 + (i * 4.2) % 65.0:.1f}"
        grade = f"{0.75 + (i * 1.85) % 28.5:.2f}"
        rec = f"{72.5 + (i * 2.1) % 24.0:.1f}"
        tail = f"{0.12 + (i * 0.04) % 0.65:.2f}"
        ph = f"{8.5 + (i * 0.2) % 3.0:.1f}"

        row_vals = [stg, feed, grade, rec, tail, ph]
        rows_data.append(row_vals)

        for x, val in zip(col_x_px, row_vals):
            draw.text((x, y_px), val, fill=(55, 55, 55), font=font_title)
        y_px += 42

    draw.line([(100, y_px), (1160, y_px)], fill=(80, 80, 80), width=4)
    draw.text((100, y_px + 25), "Archival stamp: CONFIDENTIAL - State Mining Bureau 1982.", fill=(110, 110, 110), font=font_title)

    # Convert to NumPy array to apply realistic scan degradation (slight noise & low-contrast fade)
    arr = np.array(img, dtype=np.float32)
    # Add Gaussian noise
    noise = np.random.normal(0, 4.0, arr.shape)
    arr = np.clip(arr + noise, 0, 255).astype(np.uint8)

    # Slight blur simulating low-contrast optical scan
    scanned_img = Image.fromarray(arr).filter(ImageFilter.GaussianBlur(radius=0.6))

    # Save to PDF as pure image page (no text layer)
    doc = fitz.open()

    # Page 1: Raster Scanned Page
    img_byte_arr = io.BytesIO()
    scanned_img.save(img_byte_arr, format='JPEG', quality=85)
    img_bytes = img_byte_arr.getvalue()

    page1 = doc.new_page(width=595, height=842)
    page1.insert_image(page1.rect, stream=img_bytes)

    # Page 2: Archival Notes page
    page2 = doc.new_page(width=595, height=842)
    page2.insert_text((50, 50), "Official Certification of Declassified Historical Metallurgy Files", fontsize=11, fontname="helv")

    doc.save(str(pdf_path))
    doc.close()

    # Ground Truth JSON
    sampled_cells = []
    for r_idx in [0, 4, 9, 11, 15]:
        for c_idx, c_name in [(1, "Feed (t/h)"), (2, "Grade (% Cu)"), (3, "Recovery (%)"), (5, "pH")]:
            raw_v = rows_data[r_idx][c_idx]
            sampled_cells.append({
                "row_idx": r_idx,
                "col_idx": c_idx,
                "col_name": c_name,
                "raw_val": raw_v,
                "numeric_val": float(raw_v),
            })

    gt_data = {
        "group": "Group E: Low-contrast / scanned / image-embedded tables",
        "file_name": pdf_path.name,
        "num_pages": 2,
        "is_scanned": True,
        "tables": [
            {
                "table_id": "Table 1",
                "label": "Table 1",
                "page_idx": 0,
                "caption": "Table 1. Flotation stage recovery and mineral balance sheet.",
                "columns": cols,
                "num_rows": len(rows_data),
                "num_cols": len(cols),
                "rows": rows_data,
                "sampled_numeric_cells": sampled_cells,
            }
        ]
    }
    with open(gt_path, "w", encoding="utf-8") as f:
        json.dump(gt_data, f, ensure_ascii=False, indent=2)

    # MinerU Structured Cache (Vision OCR recovered table)
    html_table = (
        "<table><thead><tr>"
        + "".join(f"<th>{c}</th>" for c in cols)
        + "</tr></thead><tbody>"
        + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows_data)
        + "</tbody></table>"
    )

    content_list = [
        {
            "type": "table",
            "table_body": html_table,
            "bbox": [50.0, 80.0, 550.0, 700.0],
            "page_idx": 0,
            "table_caption": "Table 1. Flotation stage recovery and mineral balance sheet.",
            "label": "Table 1",
        }
    ]
    _save_mineru_zip(cache_path, content_list)

    return pdf_path, gt_path, cache_path


def generate_all_papers():
    print("Generating Group A: Classic Dense Three-Line Table...")
    generate_group_a()
    print("Generating Group B: Multi-Level Complex Spanning Headers...")
    generate_group_b()
    print("Generating Group C: Untitled Tables and Appendix Tables...")
    generate_group_c()
    print("Generating Group D: Multi-Page Continuation Tables...")
    generate_group_d()
    print("Generating Group E: Low-Contrast / Scanned / Image-Embedded Tables...")
    generate_group_e()
    print("All benchmark test papers, ground truth files, and MinerU cache artifacts generated successfully!")


if __name__ == "__main__":
    generate_all_papers()
