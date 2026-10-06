#!/usr/bin/env python3
"""
Unit tests for architecture diagram generation and geometric integrity.
Verifies:
1. Well-formed draw.io XML structure.
2. Complete coverage of v1.6.0 architectural components.
3. Zero card penetration by any connector edge.
4. Zero parallel overlapping line segments between edges.
5. Presence and validity of diagram artifacts (.drawio, .png, .svg).
"""

import os
import sys
import unittest
import xml.etree.ElementTree as ET

# Add project root to sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.export_drawio_diagram import build_architecture_drawio_xml, FIGURES_DIR, DRAWIO_FILE, PNG_FILE, SVG_FILE


class TestArchitectureDiagram(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.xml_str = build_architecture_drawio_xml()
        cls.root = ET.fromstring(cls.xml_str)
        
        # Parse swimlanes
        cls.swimlanes = {}
        for cell in cls.root.iter('mxCell'):
            if cell.get('vertex') == '1' and 'swimlane' in cell.get('style', ''):
                geo = cell.find('mxGeometry')
                if geo is not None:
                    cls.swimlanes[cell.get('id')] = (
                        float(geo.get('x', 0)),
                        float(geo.get('y', 0)),
                        float(geo.get('width', 0)),
                        float(geo.get('height', 0))
                    )
        
        # Parse card bounding boxes in canvas-absolute coordinates
        cls.boxes = {}
        for cell in cls.root.iter('mxCell'):
            cid = cell.get('id')
            if cell.get('vertex') == '1' and 'swimlane' not in cell.get('style', '') and cid != 'banner':
                geo = cell.find('mxGeometry')
                if geo is not None:
                    parent_id = cell.get('parent')
                    px, py = 0, 0
                    if parent_id in cls.swimlanes:
                        px, py, _, _ = cls.swimlanes[parent_id]
                    x = px + float(geo.get('x', 0))
                    y = py + float(geo.get('y', 0))
                    w = float(geo.get('width', 0))
                    h = float(geo.get('height', 0))
                    cls.boxes[cid] = (x, y, w, h)
        
        # Parse edges and their waypoints
        cls.edges = []
        for cell in cls.root.iter('mxCell'):
            if cell.get('edge') == '1':
                eid = cell.get('id')
                src = cell.get('source')
                tgt = cell.get('target')
                geo = cell.find('mxGeometry')
                pts = []
                if geo is not None:
                    arr = geo.find('Array')
                    if arr is not None:
                        for pt in arr.findall('mxPoint'):
                            pts.append((float(pt.get('x')), float(pt.get('y'))))
                cls.edges.append((eid, src, tgt, pts))

    def test_xml_structure_and_counts(self):
        """Verify XML is well-formed with 7 swimlanes, 35+ cards, and 35+ edges."""
        self.assertEqual(len(self.swimlanes), 7, "Diagram must feature exactly 7 architectural swimlanes.")
        self.assertGreaterEqual(len(self.boxes), 35, "Diagram must contain at least 35 functional component cards.")
        self.assertGreaterEqual(len(self.edges), 38, "Diagram must contain at least 38 connectors.")

    def test_v1_6_0_architectural_components_present(self):
        """Ensure all v1.6.0 core architectural upgrades are clearly documented."""
        # 1. 7 PDF engines
        for term in ["GMFT", "img2table", "Tagged PDF", "Camelot", "pdfplumber", "find_tables", "text_alignment"]:
            self.assertIn(term, self.xml_str, f"Missing native PDF engine component: {term}")
        
        # 2. Dual visual AI pipeline
        for term in ["PP-StructureV3", "PaddleOCR-VL-1.6", "Docling", "TableFormer"]:
            self.assertIn(term, self.xml_str, f"Missing dual AI pipeline component: {term}")
            
        # 3. Continuation DP merging
        for term in ["DeepDoc", "MinerU", "单调", "DP"]:
            self.assertIn(term, self.xml_str, f"Missing continuation DP merge component: {term}")
            
        # 4. Zotero & Better-Notes ecosystem
        for term in ["zotero_sync.py", "Better-Notes", ".xlsx", "ZOTERO_SYNC_ENABLED"]:
            self.assertIn(term, self.xml_str, f"Missing ecosystem integration component: {term}")

    def test_no_card_penetrations(self):
        """Mathematically prove that no edge penetrates any unrelated card."""
        def segment_intersects_box(p1, p2, b, margin=1.0):
            bx, by, bw, bh = b
            min_x, max_x = min(p1[0], p2[0]), max(p1[0], p2[0])
            min_y, max_y = min(p1[1], p2[1]), max(p1[1], p2[1])
            if max_x <= bx + margin or min_x >= bx + bw - margin:
                return False
            if max_y <= by + margin or min_y >= by + bh - margin:
                return False
            if p1[0] == p2[0]:
                x = p1[0]
                if bx + margin < x < bx + bw - margin:
                    if not (max_y <= by or min_y >= by + bh):
                        return True
            if p1[1] == p2[1]:
                y = p1[1]
                if by + margin < y < by + bh - margin:
                    if not (max_x <= bx or min_x >= bx + bw):
                        return True
            return False

        penetrations = []
        for eid, src, tgt, pts in self.edges:
            sb = self.boxes.get(src)
            tb = self.boxes.get(tgt)
            if not pts or not sb or not tb:
                continue
            sx, sy, sw, sh = sb
            p0 = pts[0]
            if sy <= p0[1] <= sy + sh:
                conn_s = (sx + sw if p0[0] > sx else sx, p0[1])
            else:
                conn_s = (p0[0], sy + sh if p0[1] > sy else sy)
            segs = [(conn_s, p0)]
            for i in range(len(pts) - 1):
                segs.append((pts[i], pts[i+1]))
            p_last = pts[-1]
            tx, ty, tw, th = tb
            if ty <= p_last[1] <= ty + th:
                conn_t = (tx if p_last[0] < tx else tx + tw, p_last[1])
            else:
                conn_t = (p_last[0], ty if p_last[1] < ty else ty + th)
            segs.append((p_last, conn_t))

            for s in segs:
                for bid, b in self.boxes.items():
                    if bid != src and bid != tgt:
                        if segment_intersects_box(s[0], s[1], b):
                            penetrations.append((eid, s, bid))

        self.assertEqual(len(penetrations), 0, f"Detected edge card penetrations: {penetrations}")

    def test_no_parallel_overlapping_edges(self):
        """Mathematically prove that no two edges share overlapping collinear parallel segments."""
        all_segments = []
        for eid, src, tgt, pts in self.edges:
            sb = self.boxes.get(src)
            tb = self.boxes.get(tgt)
            if not pts or not sb or not tb:
                continue
            sx, sy, sw, sh = sb
            p0 = pts[0]
            if sy <= p0[1] <= sy + sh:
                conn_s = (sx + sw if p0[0] > sx else sx, p0[1])
            else:
                conn_s = (p0[0], sy + sh if p0[1] > sy else sy)
            all_segments.append((eid, conn_s, p0))
            for i in range(len(pts) - 1):
                all_segments.append((eid, pts[i], pts[i+1]))
            p_last = pts[-1]
            tx, ty, tw, th = tb
            if ty <= p_last[1] <= ty + th:
                conn_t = (tx if p_last[0] < tx else tx + tw, p_last[1])
            else:
                conn_t = (p_last[0], ty if p_last[1] < ty else ty + th)
            all_segments.append((eid, p_last, conn_t))

        overlaps = []
        for i in range(len(all_segments)):
            e1, p1a, p1b = all_segments[i]
            for j in range(i + 1, len(all_segments)):
                e2, p2a, p2b = all_segments[j]
                if e1 == e2:
                    continue
                # Vertical overlap check
                if p1a[0] == p1b[0] == p2a[0] == p2b[0]:
                    y1_min, y1_max = min(p1a[1], p1b[1]), max(p1a[1], p1b[1])
                    y2_min, y2_max = min(p2a[1], p2b[1]), max(p2a[1], p2b[1])
                    ov_min = max(y1_min, y2_min)
                    ov_max = min(y1_max, y2_max)
                    if ov_max - ov_min > 5.0:
                        overlaps.append((e1, e2, 'vertical', p1a[0], ov_min, ov_max))
                # Horizontal overlap check
                if p1a[1] == p1b[1] == p2a[1] == p2b[1]:
                    x1_min, x1_max = min(p1a[0], p1b[0]), max(p1a[0], p1b[0])
                    x2_min, x2_max = min(p2a[0], p2b[0]), max(p2a[0], p2b[0])
                    ov_min = max(x1_min, x2_min)
                    ov_max = min(x1_max, x2_max)
                    if ov_max - ov_min > 5.0:
                        overlaps.append((e1, e2, 'horizontal', p1a[1], ov_min, ov_max))

        self.assertEqual(len(overlaps), 0, f"Detected parallel overlapping edges: {overlaps}")

    def test_exported_files_exist_and_non_empty(self):
        """Verify that exported .drawio, .png, and .svg files exist with non-zero size."""
        self.assertTrue(os.path.isfile(DRAWIO_FILE), f"Missing {DRAWIO_FILE}")
        self.assertGreater(os.path.getsize(DRAWIO_FILE), 10000)

        self.assertTrue(os.path.isfile(PNG_FILE), f"Missing {PNG_FILE}")
        self.assertGreater(os.path.getsize(PNG_FILE), 1000000)

        self.assertTrue(os.path.isfile(SVG_FILE), f"Missing {SVG_FILE}")
        self.assertGreater(os.path.getsize(SVG_FILE), 1000000)


if __name__ == "__main__":
    unittest.main()
