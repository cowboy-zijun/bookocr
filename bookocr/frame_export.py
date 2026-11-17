# -*- coding: utf-8 -*-
"""Absolute frame-mode renderer for text-layer pages (L4).

Measure the page content frame, then place front / left / right / notes as
page-anchored paragraph frames. No Word w:cols, no flow between frames.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

from . import engine, layout


def _norm(s):
    return re.sub(r'\s+', '', s or '')


def _bbox(lines):
    return [min(l['x0'] for l in lines), min(l['y0'] for l in lines),
            max(l['x1'] for l in lines), max(l['y1'] for l in lines)]


def measure_pages(pdf_path, page_numbers=None):
    """Return measured frames for text-layer pages.

    Units: source PDF normalized 3400x5000 + points on the actual page.
    """
    import pymupdf
    doc = pymupdf.open(pdf_path)
    total = doc.page_count
    nums = list(page_numbers or range(1, total + 1))
    pages = engine.extract_text_pages(pdf_path, set(nums))
    out = {}
    for pno in nums:
        pw = doc[pno - 1].rect.width
        ph = doc[pno - 1].rect.height
        sx, sy = pw / 3400.0, ph / 5000.0
        lines = [l for l in pages.get(pno, []) if 350 < l['y0'] < 4650]
        try:
            fn = layout.footnote_start(lines, 9.0, marker_rule=False)
        except Exception:
            fn = None
        notes = [l for l in lines if fn is not None and l['y0'] >= fn]
        bodyall = [l for l in lines if fn is None or l['y0'] < fn]
        try:
            g = layout.gutter_of(bodyall)
        except Exception:
            g = None
        mid = g if g else 1700.0
        # body start: first line that belongs to a real left/right column
        # 只认真正的栏行：左栏行不跨中缝，右栏行起点在中缝右侧。
        # 题名/摘要跨中缝，不能用来推正文起点。
        cand = [l['y0'] for l in bodyall
                if len(_norm(l.get('text') or '')) >= 8 and
                ((l['x0'] < mid - 150 and l['x1'] < mid - 8)
                 or l['x0'] > mid + 8)]
        body_start = min(cand) if cand else (
            min((l['y0'] for l in bodyall), default=0))
        front = [l for l in bodyall if l['y0'] < body_start - 20]
        body = [l for l in bodyall if l['y0'] >= body_start - 20]
        left = [l for l in body if l['x0'] < mid - 20]
        right = [l for l in body if l['x0'] >= mid - 20]
        frames = {
            'page': pno, 'page_w': round(pw, 2), 'page_h': round(ph, 2),
            'scale': [round(sx, 6), round(sy, 6)],
            'frames': {},
        }
        for name, group in (('front', front), ('left', left),
                            ('right', right), ('notes', notes)):
            if not group:
                continue
            b = _bbox(group)
            sizes = [float(s['size']) for l in group
                     for s in (l.get('spans') or []) if s.get('size')]
            ys = sorted({round(l['y0'], 1) for l in group})
            dif = [ys[i + 1] - ys[i] for i in range(len(ys) - 1)
                   if 20 < ys[i + 1] - ys[i] < 300]
            frames['frames'][name] = {
                'bbox': [round(b[0] * sx, 2), round(b[1] * sy, 2),
                         round(b[2] * sx, 2), round(b[3] * sy, 2)],
                'lines': group,
                'size': sorted(sizes)[len(sizes) // 2] if sizes else 9.0,
                'pitch': (sum(dif) / len(dif) * sy) if dif else None,
            }
        out[pno] = frames
    doc.close()
    return out


def _set_frame(p, x, y, w, h):
    pPr = p._p.get_or_add_pPr()
    fr = OxmlElement('w:framePr')
    fr.set(qn('w:w'), str(int(round(w * 20))))
    fr.set(qn('w:h'), str(int(round(h * 20))))
    fr.set(qn('w:hRule'), 'atLeast')
    fr.set(qn('w:wrap'), 'none')
    fr.set(qn('w:vAnchor'), 'page')
    fr.set(qn('w:hAnchor'), 'page')
    fr.set(qn('w:x'), str(int(round(x * 20))))
    fr.set(qn('w:y'), str(int(round(y * 20))))
    pPr.insert(0, fr)


def _scale90(run):
    rPr = run._element.get_or_add_rPr()
    w = OxmlElement('w:w')
    w.set(qn('w:val'), '90')
    rPr.append(w)


def _para(doc, texts, bbox, size, pitch, align, bold=False,
          first_indent=0.0):
    p = doc.add_paragraph()
    p.alignment = align
    pf = p.paragraph_format
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)
    pf.line_spacing = Pt(max(6.0, float(pitch or size * 1.15)))
    if first_indent and first_indent > 0.5:
        pf.first_line_indent = Pt(first_indent)
    _set_frame(p, bbox[0], bbox[1],
               max(20.0, bbox[2] - bbox[0]),
               max(10.0, bbox[3] - bbox[1]))
    first = True
    for t in texts:
        t = (t or '')
        if not t.strip():
            continue
        if not first:
            p.add_run().add_break()
        first = False
        r = p.add_run(t)
        r.bold = bold
        r.font.size = Pt(float(size))
        r.font.name = 'Times New Roman'
        r._element.rPr.rFonts.set(qn('w:eastAsia'), '宋体')
        _scale90(r)
    return p


def export_frame_docx(pdf_path, out_path, pages=None, title=None):
    """Render measured frames into a DOCX (page-anchored frames)."""
    import pymupdf
    doc_in = pymupdf.open(pdf_path)
    first = doc_in[0].rect
    doc = Document()
    sec = doc.sections[0]
    sec.page_width = Pt(first.width)
    sec.page_height = Pt(first.height)
    for s in doc.sections:
        s.left_margin = s.right_margin = s.top_margin = s.bottom_margin = Pt(0)
    measured = measure_pages(pdf_path, pages)
    nums = sorted(measured)
    for idx, pno in enumerate(nums):
        m = measured[pno]
        for name in ('front',):
            fr = m['frames'].get(name)
            if not fr:
                continue
            for l in fr['lines']:
                b = l['bbox'] if 'bbox' in l else None
                # front line frame from measured line coordinates
                x = l['x0'] * m['scale'][0]
                y = l['y0'] * m['scale'][1]
                w = (l['x1'] - l['x0']) * m['scale'][0]
                h = (l['y1'] - l['y0']) * m['scale'][1]
                t = (l.get('text') or '').strip()
                if not t:
                    continue
                tsz = float(l.get('size') or fr['size'])
                if u'试论' in t or u'新谋划' in t:
                    tsz = max(tsz, 18.0)
                    align = WD_ALIGN_PARAGRAPH.CENTER
                    bold = True
                elif t.startswith(u'◎'):
                    align = WD_ALIGN_PARAGRAPH.CENTER
                    bold = False
                else:
                    align = WD_ALIGN_PARAGRAPH.LEFT
                    bold = False
                _para(doc, [t], [x, y, x + max(w, 40), y + max(h, 10)],
                      tsz, max(10.0, h), align, bold)
        for name in ('left', 'right'):
            fr = m['frames'].get(name)
            if not fr:
                continue
            # 首行缩进用两个全角空格写到行首，保持整个框一个段落，
            # 避免多个 framePr 段落叠在同一坐标。
            lines = fr['lines']
            fx = fr['bbox'][0]
            fs = fr['size']
            texts = []
            for l in lines:
                t = (l.get('text') or '').strip()
                off = l['x0'] * m['scale'][0] - fx
                if off >= max(4.0, fs * 0.95):
                    t = u'\u3000\u3000' + t
                texts.append(t)
            h = fr['bbox'][3] - fr['bbox'][1]
            pitch = min(fr['pitch'] or fr['size'] * 1.15,
                        h / max(1, len(texts)))
            _para(doc, texts, fr['bbox'], fr['size'], pitch,
                  WD_ALIGN_PARAGRAPH.LEFT)
        fr = m['frames'].get('notes')
        if fr:
            texts = [l.get('text') for l in fr['lines']]
            h = fr['bbox'][3] - fr['bbox'][1]
            pitch = min(fr['pitch'] or fr['size'] * 1.15,
                        h / max(1, len(texts)))
            _para(doc, texts, fr['bbox'],
                  fr['size'], pitch, WD_ALIGN_PARAGRAPH.LEFT)
        if idx < len(nums) - 1:
            doc.add_page_break()
    doc.save(out_path)
    return out_path, measured


def write_frames_json(measured, path):
    out = {}
    for pno, m in measured.items():
        out[str(pno)] = {
            'page_w': m['page_w'], 'page_h': m['page_h'],
            'frames': {k: {'bbox': v['bbox'], 'size': v['size'],
                           'pitch': round(v['pitch'], 2) if v['pitch'] else None,
                           'n': len(v['lines'])}
                       for k, v in m['frames'].items()},
        }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return path
