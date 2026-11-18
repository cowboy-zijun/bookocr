# -*- coding: utf-8 -*-
"""Generic page/region measurement (no journal-specific templates).

This module is the measurement layer, not a renderer:
  page -> outer frame -> front / columns / notes -> gaps
It measures every document with its own body font size, line pitch, gutter,
and page margins; no "journal A / journal B" hard-coded layouts.
"""
from __future__ import annotations

import json
import os
import re
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pymupdf

from . import engine, layout


@dataclass
class Region:
    role: str
    bbox: List[float]          # points
    lines: List[dict] = field(default_factory=list)
    size: float = 0.0
    pitch: float = 0.0
    char_gap: float = 0.0
    top_slack: float = 0.0
    bottom_slack: float = 0.0
    fill_ratio: float = 0.0

    def to_dict(self):
        return {'role': self.role, 'bbox': [round(x, 2) for x in self.bbox],
                'size': round(self.size, 2),
                'pitch': round(self.pitch, 2),
                'char_gap': round(self.char_gap, 2),
                'top_slack': round(self.top_slack, 2),
                'bottom_slack': round(self.bottom_slack, 2),
                'fill_ratio': round(self.fill_ratio, 3),
                'n': len(self.lines)}


@dataclass
class PageProfile:
    page: int
    width: float
    height: float
    outer: List[float]
    regions: Dict[str, Region] = field(default_factory=dict)
    gaps: Dict[str, float] = field(default_factory=dict)
    columns_top: Optional[float] = None
    confidence: float = 1.0
    notes: List[str] = field(default_factory=list)

    def to_dict(self):
        return {'page': self.page, 'width': round(self.width, 2),
                'height': round(self.height, 2),
                'outer': [round(x, 2) for x in self.outer],
                'regions': {k: v.to_dict() for k, v in self.regions.items()},
                'gaps': {k: round(v, 2) for k, v in self.gaps.items()},
                'columns_top': (round(self.columns_top, 2)
                                if self.columns_top is not None else None),
                'confidence': round(self.confidence, 2),
                'notes': self.notes}


def _bbox(lines):
    return [min(l['x0'] for l in lines), min(l['y0'] for l in lines),
            max(l['x1'] for l in lines), max(l['y1'] for l in lines)]


def _median_size(lines, default=9.0):
    vals = [float(s.get('size') or 0) for l in lines
            for s in (l.get('spans') or []) if s.get('size')]
    return statistics.median(vals) if vals else default


def _char_gap(lines):
    """Median horizontal gap between adjacent spans in a frame."""
    gaps = []
    for l in lines:
        spans = [s for s in (l.get('spans') or [])
                 if (s.get('text') or '').strip()]
        for a, b in zip(spans, spans[1:]):
            try:
                gaps.append(float(b['x0']) - float(a['x1']))
            except Exception:
                pass
    if not gaps:
        return 0.0
    return statistics.median(gaps)


def _median_pitch(lines, default=None):
    ys = sorted({round(l['y0'], 1) for l in lines})
    dif = [ys[i + 1] - ys[i] for i in range(len(ys) - 1)
           if 20 < ys[i + 1] - ys[i] < 300]
    return statistics.median(dif) if dif else default


def _region(name, group, b, sx, sy):
    size = _median_size(group)
    pitch = (_median_pitch(group, 0) or 0) * sy
    bb = [b[0] * sx, b[1] * sy, b[2] * sx, b[3] * sy]
    top = min(l['y0'] for l in group)
    bottom = max(l['y1'] for l in group)
    h = max(1.0, bb[3] - bb[1])
    return Region(name, bb, group, size, pitch,
                  char_gap=_char_gap(group) * sx,
                  top_slack=max(0.0, (top - b[1]) * sy),
                  bottom_slack=max(0.0, (b[3] - bottom) * sy),
                  fill_ratio=min(1.5, ((bottom - top) * sy) / h))


def measure_page(pdf_path, pno, all_pages=None):
    doc = pymupdf.open(pdf_path)
    try:
        pg = doc[pno - 1]
        pw, ph = pg.rect.width, pg.rect.height
        sx, sy = pw / 3400.0, ph / 5000.0
        pages = all_pages or engine.extract_text_pages(pdf_path, {pno})
        lines = [l for l in pages.get(pno, []) if 350 < l['y0'] < 4650]
        if not lines:
            return PageProfile(pno, pw, ph, [0, 0, pw, ph], confidence=0)
        _ob = _bbox(lines)
        outer = [_ob[0] * sx, _ob[1] * sy, _ob[2] * sx, _ob[3] * sy]
        # content-point outer frame, removing obvious page number/deco lines
        try:
            fn = layout.footnote_start(lines, _median_size(lines),
                                       marker_rule=False)
        except Exception:
            fn = None
        notes = [l for l in lines if fn is not None and l['y0'] >= fn]
        body_all = [l for l in lines if fn is None or l['y0'] < fn]
        try:
            g = layout.gutter_of(body_all)
        except Exception:
            g = None
        mid = g if g else 1700.0
        # column lines: not crossing the gutter
        col_cand = [l for l in body_all
                    if (l['x0'] < mid - 100 and l['x1'] < mid + 50)
                    or l['x0'] > mid - 50]
        body_start = min((l['y0'] for l in col_cand),
                         default=min((l['y0'] for l in body_all), default=0))
        front = [l for l in body_all if l['y0'] < body_start - 20]
        body = [l for l in body_all if l['y0'] >= body_start - 20]
        left = [l for l in body if l['x0'] < mid - 20]
        right = [l for l in body if l['x0'] >= mid - 20]
        prof = PageProfile(pno, pw, ph, outer)
        # 只有左右框真正分开才叫双栏；否则是单栏/横排表页，
        # 不能硬拆成 left/right（会得到负中缝）。
        two = False
        if left and right:
            _bb_l = _bbox(left); _bb_r = _bbox(right)
            _gap = (_bb_r[0] - _bb_l[2]) * sx
            if _gap > 2.0:
                two = True
        if two:
            for name, group in (('left', left), ('right', right)):
                if not group:
                    continue
                b = _bbox(group)
                prof.regions[name] = _region(name, group, b, sx, sy)
            top = min(prof.regions['left'].bbox[1],
                      prof.regions['right'].bbox[1])
            prof.regions['left'].bbox[1] = top
            prof.regions['right'].bbox[1] = top
            prof.columns_top = top
            prof.gaps['left_right'] = (
                prof.regions['right'].bbox[0] - prof.regions['left'].bbox[2])
        else:
            _body = body or body_all
            b = _bbox(_body)
            prof.regions['body'] = _region('body', _body, b, sx, sy)
        if front:
            b = _bbox(front)
            prof.regions['front'] = _region('front', front, b, sx, sy)
        if notes:
            b = _bbox(notes)
            prof.regions['notes'] = _region('notes', notes, b, sx, sy)
        _body_names = [n for n in ('left', 'right', 'body') if n in prof.regions]
        if 'front' in prof.regions and _body_names:
            bt = min(prof.regions[n].bbox[1] for n in _body_names)
            prof.gaps['front_body'] = bt - prof.regions['front'].bbox[3]
        if 'notes' in prof.regions and _body_names:
            bb = max(prof.regions[n].bbox[3] for n in _body_names)
            prof.gaps['body_notes'] = prof.regions['notes'].bbox[1] - bb
        # confidence: a page with no body columns is suspicious
        if not any(n in prof.regions for n in ('left', 'right', 'body')):
            prof.confidence = min(prof.confidence, 0.4)
        if prof.gaps.get('body_notes', 0) < 2 and 'notes' in prof.regions:
            prof.confidence = min(prof.confidence, 0.5)
        if front:
            prof.notes.append('front')
        return prof
    finally:
        doc.close()


def measure_book(pdf_path):
    doc = pymupdf.open(pdf_path)
    n = doc.page_count
    doc.close()
    pages = engine.extract_text_pages(pdf_path, set(range(1, n + 1)))
    return {p: measure_page(pdf_path, p, pages) for p in range(1, n + 1)}


def write_report(profiles, path):
    data = {str(p): v.to_dict() for p, v in profiles.items()}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path
