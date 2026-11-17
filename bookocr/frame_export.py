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


_BAD_PARA_START = set(u'。，、；：！？）】》”’…—,.;:!?)]}')


def _is_para_start(text):
    """Conservative: don't call a continuation/marker line a paragraph."""
    t = _norm(text)
    if not t:
        return False
    if t[0] in _BAD_PARA_START:
        return False
    if re.match(u'^[①-⑳⒈-⒚⑴-⒇]', t):
        return False
    return True


def _bbox(lines):
    return [min(l['x0'] for l in lines), min(l['y0'] for l in lines),
            max(l['x1'] for l in lines), max(l['y1'] for l in lines)]


def _indent_cluster(lines, frame_x, scale, size):
    """Find the paragraph-indent cluster from line x0 offsets.

    Returns (indent_pt, units, support).  Only a real cluster (>=2 lines)
    is accepted; a lone ①/marker stays an outlier.
    """
    vals = []
    for l in lines:
        off = l['x0'] * scale - frame_x
        u = off / size if size else 0.0
        if 0.7 <= u <= 4.0:
            vals.append((off, u))
    if len(vals) < 2:
        return 0.0, 0.0, 0
    us = sorted(v[1] for v in vals)
    # median is robust against one marker line far to the right
    med = us[len(us) // 2]
    near = [v for v in vals if abs(v[1] - med) <= 0.55]
    if len(near) < 2:
        return 0.0, 0.0, 0
    return (sum(v[0] for v in near) / len(near),
            sum(v[1] for v in near) / len(near), len(near))


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
        # 双栏正文上沿必须平齐：以两栏更高的上沿为公共 top。
        frs = frames['frames']
        for _n in ('left', 'right'):
            if _n in frs:
                _fr = frs[_n]
                _ip, _iu, _nsup = _indent_cluster(
                    _fr['lines'], _fr['bbox'][0], sx, _fr['size'])
                _fr['indent_pt'] = round(_ip, 2)
                _fr['indent_units'] = round(_iu, 2)
                _fr['indent_support'] = _nsup
        if 'left' in frs and 'right' in frs:
            top = min(frs['left']['bbox'][1], frs['right']['bbox'][1])
            for _n in ('left', 'right'):
                frs[_n]['bbox'][1] = top
            frames['columns_top'] = top
        # 量框间净间距，写进 frames.json，导出/核验都用同一组数。
        def _gap(a, b):
            return round(b['bbox'][1] - a['bbox'][3], 2)
        frames['gaps'] = {}
        if 'front' in frs and ('left' in frs or 'right' in frs):
            bd = min((frs[n]['bbox'][1] for n in ('left', 'right')
                      if n in frs), default=None)
            if bd is not None:
                frames['gaps']['front_body'] = round(bd - frs['front']['bbox'][3], 2)
        if 'notes' in frs and ('left' in frs or 'right' in frs):
            bb = max((frs[n]['bbox'][3] for n in ('left', 'right')
                      if n in frs), default=None)
            if bb is not None:
                frames['gaps']['body_notes'] = round(frs['notes']['bbox'][1] - bb, 2)
        if 'left' in frs and 'right' in frs:
            frames['gaps']['left_right'] = round(
                frs['right']['bbox'][0] - frs['left']['bbox'][2], 2)
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


def _line_runs(line, fs):
    """Preserve span size and mark small/raised citation markers as superscript."""
    spans = line.get('spans') or []
    out = []
    for s in spans:
        t = s.get('text') or ''
        if not t.strip():
            continue
        sz = float(s.get('size') or fs)
        sup = sz < fs * 0.86
        out.append([t, sz, sup])
    if not out:
        t = line.get('text') or ''
        if t.strip():
            out = [[t, fs, False]]
    return out


_MARK_RE = re.compile(u'^[①-⑳]')


def _merge_line_markers(lines_runs):
    """Move a leading ①-⑳ from a continuation line to the previous line.

    PDF line breaks often put a superscript marker at the beginning of the
    next line; it belongs to the end of the previous line, not to a line of
    its own.
    """
    for i in range(1, len(lines_runs)):
        cur = lines_runs[i]
        if not cur:
            continue
        m = _MARK_RE.match(cur[0][0])
        if not m:
            continue
        mark = m.group(0)
        cur[0][0] = cur[0][0][len(mark):]
        if not cur[0][0]:
            cur.pop(0)
        if cur:
            prev = lines_runs[i - 1]
            prev.append([mark, lines_runs[i - 1][0][1] if prev else 9.0,
                         True])
    return lines_runs


def _para_runs(doc, lines, bbox, size, pitch, align):
    p = doc.add_paragraph()
    p.alignment = align
    pf = p.paragraph_format
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)
    pf.line_spacing = Pt(max(6.0, float(pitch or size * 1.15)))
    _set_frame(p, bbox[0], bbox[1],
               max(20.0, bbox[2] - bbox[0]),
               max(10.0, bbox[3] - bbox[1]))
    first = True
    for runs in lines:
        if not runs:
            continue
        if not first:
            p.add_run().add_break()
        first = False
        for text, sz, sup in runs:
            if not text:
                continue
            r = p.add_run(text)
            r.font.size = Pt(sz)
            if sup:
                r.font.superscript = True
            r.font.name = 'Times New Roman'
            r._element.rPr.rFonts.set(qn('w:eastAsia'), '宋体')
            _scale90(r)
    return p


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
            lines_runs = []
            for l in lines:
                runs = _line_runs(l, fs)
                t = (l.get('text') or '').strip()
                off = l['x0'] * m['scale'][0] - fx
                # 只认可信的 x0 段首簇；不用固定阈值。
                _ind = fr.get('indent_pt') or 0.0
                if _ind and abs(off - _ind) <= max(1.5, fs * 0.35) \
                        and _is_para_start(t) and runs:
                    runs[0][0] = u'\u3000\u3000' + runs[0][0]
                lines_runs.append(runs)
            h = fr['bbox'][3] - fr['bbox'][1]
            pitch = min(fr['pitch'] or fr['size'] * 1.15,
                        h / max(1, len(lines_runs)))
            _para_runs(doc, lines_runs, fr['bbox'], fr['size'], pitch,
                       WD_ALIGN_PARAGRAPH.LEFT)
        fr = m['frames'].get('notes')
        if fr:
            runs = [_line_runs(l, fr['size']) for l in fr['lines']]
            h = fr['bbox'][3] - fr['bbox'][1]
            pitch = min(fr['pitch'] or fr['size'] * 1.15,
                        h / max(1, len(runs)))
            _para_runs(doc, runs, fr['bbox'], fr['size'], pitch,
                       WD_ALIGN_PARAGRAPH.LEFT)
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
                           'n': len(v['lines']),
                           'indent_pt': v.get('indent_pt'),
                           'indent_units': v.get('indent_units'),
                           'indent_support': v.get('indent_support')}
                       for k, v in m['frames'].items()},
            'columns_top': m.get('columns_top'),
            'gaps': m.get('gaps') or {},
        }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return path
