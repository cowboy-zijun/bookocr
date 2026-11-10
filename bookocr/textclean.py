# -*- coding: utf-8 -*-
"""统一文字清洗层：字体私用区、旋转水印、独立上下标行。"""
from __future__ import annotations

from typing import Iterable, List

WATERMARK_DIR = 0.9

PUA_MAP = {
    0xF077: u'ω', 0xF078: u'ξ', 0xF07A: u'ξ', 0xF06E: u'η',
    0xF061: u'α', 0xF062: u'β', 0xF067: u'γ', 0xF064: u'δ',
    0xF065: u'ε', 0xF06C: u'λ', 0xF06D: u'μ', 0xF073: u'σ',
    0xF074: u'τ', 0xF066: u'φ',
}


def fix_symbol_text(t):
    """Symbol 字体的私用区码位（\\uf077 之类）转回 Unicode 希腊字母。"""
    if not t:
        return t
    return u''.join(PUA_MAP.get(ord(c), c) for c in t)


def is_watermark_line(line):
    """斜排/竖排的水印文字行（dir 不是水平）。"""
    d = line.get('dir') or (1.0, 0.0)
    return abs(d[1]) > 0.08 or d[0] < WATERMARK_DIR


def merge_sub_lines(lines):
    """把独立的上下标小行（ω 的 n、引用号）并回相邻视觉行。

    只处理 len<=3 且字号小于正文 80% 的独立行；页眉/页码/分隔线不动。
    """
    if not lines:
        return lines
    sizes = sorted(l.get('size') or 0 for l in lines if l.get('size'))
    body = sizes[len(sizes) // 2] if sizes else 0
    if not body:
        return lines
    out = []
    for l in lines:
        t = (l.get('text') or '').strip()
        if l.get('sep') or len(t) > 3 or not l.get('size') \
                or l['size'] >= body * 0.8:
            out.append(l)
            continue
        cand = None
        for o in lines:
            if o is l or o.get('sep'):
                continue
            oy0, oy1 = o.get('y0', 0), o.get('y1', 0)
            if l['y1'] < oy0 - 2 or l['y0'] > oy1 + 2:
                continue
            if not (o.get('x0', 0) - 6 <= l.get('x0', 0)
                    <= o.get('x1', 0) + 6):
                continue
            if cand is None or len(o.get('text') or '') > len(
                    cand.get('text') or ''):
                cand = o
        if cand is None:
            out.append(l)
            continue
        pos = len(cand.get('text') or '')
        acc = 0
        for s in cand.get('spans') or []:
            if s.get('x0', 0) <= l.get('x0', 0) + 2:
                pos = acc + len(s.get('text') or '')
            acc += len(s.get('text') or '')
        cand['text'] = (cand.get('text') or '')[:pos] + t + \
            (cand.get('text') or '')[pos:]
        cand['spans'] = list(cand.get('spans') or []) + \
            list(l.get('spans') or [])
        cand['x0'] = min(cand.get('x0', 0), l.get('x0', 0))
        cand['x1'] = max(cand.get('x1', 0), l.get('x1', 0))
        cand['y0'] = min(cand.get('y0', 0), l.get('y0', 0))
        cand['y1'] = max(cand.get('y1', 0), l.get('y1', 0))
    return out
