# -*- coding: utf-8 -*-
"""Table IR：所有表格先变成同一种中间表示，再做质量判断/合并/导出。

旧代码 table.py 直接返回 dict/list，job/export 各自猜字段；复杂表只要
少一个 merges 或 rows 被压成 2 行，就会变成“2 行表”或整页图。Table IR
只统一形状，不负责识别；识别失败仍由 job 回退截图。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _cell(v: Any) -> str:
    if v is None:
        return ''
    return str(v)


@dataclass
class CellRun:
    text: str
    sup: bool = False
    sub: bool = False
    bold: bool = False
    italic: bool = False
    confidence: float = 1.0


@dataclass
class CellIR:
    runs: List[CellRun] = field(default_factory=list)
    objects: List[Any] = field(default_factory=list)

    @classmethod
    def from_text(cls, text):
        """Split a cell string into runs, keeping common symbols/styles.

        This is deliberately conservative: only patterns we can justify
        (R2 -> R + superscript 2, trailing *** -> superscript stars) are
        split.  Everything else stays one run, so unicode alpha/beta/units
        are never dropped.
        """
        s = '' if text is None else str(text)
        if not s:
            return cls([])
        runs = []
        # trailing significance stars are superscript in journal tables
        import re as _re
        m = _re.search(r'(\*{1,3})$', s)
        tail = ''
        if m:
            tail = m.group(1)
            s = s[:m.start()]
        # R2 / R^2 / adj. R2 at the end
        m2 = _re.search(r'(R)\^?([2-9])$', s)
        if m2:
            prefix = s[:m2.start()]
            if prefix:
                runs.append(CellRun(prefix))
            runs.append(CellRun(m2.group(1)))
            runs.append(CellRun(m2.group(2), sup=True))
        else:
            runs.append(CellRun(s))
        if tail:
            runs.append(CellRun(tail, sup=True))
        return cls([r for r in runs if r.text != ''])

    def text(self):
        return ''.join(r.text for r in self.runs)


@dataclass
class TableIR:
    rows: List[List[str]] = field(default_factory=list)
    merges: List[List[int]] = field(default_factory=list)
    widths: List[float] = field(default_factory=list)
    bbox: Optional[Tuple[float, float, float, float]] = None
    source: str = ''
    quality: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_data(cls, data: Any, bbox=None, source='') -> 'TableIR':
        extra: Dict[str, Any] = {}
        if isinstance(data, dict):
            rows = data.get('rows') or []
            merges = data.get('merges') or []
            widths = data.get('widths') or []
            for k, v in data.items():
                if k not in ('rows', 'merges', 'widths'):
                    extra[k] = v
            if bbox is None:
                bb = data.get('bbox')
                if bb:
                    try:
                        bbox = tuple(float(x) for x in bb[:4])
                    except Exception:
                        bbox = None
        else:
            rows, merges, widths = data or [], [], []
        ir = cls(rows=[[_cell(c) for c in r] for r in rows],
                 merges=[[int(x) for x in m[:4]] for m in merges
                         if isinstance(m, (list, tuple)) and len(m) >= 4],
                 widths=[float(x) for x in widths],
                 bbox=bbox, source=source, extra=extra)
        ir.normalize()
        ir.quality = ir.score()
        return ir

    def normalize(self) -> None:
        if not self.rows:
            self.rows = []
            self.merges = []
            return
        ncol = max(len(r) for r in self.rows)
        ncol = max(1, ncol)
        self.rows = [list(r) + [''] * (ncol - len(r)) for r in self.rows]
        nrow = len(self.rows)
        fixed = []
        seen = set()
        for m in self.merges:
            r0, c0, rs, cs = m
            rs = max(1, rs)
            cs = max(1, cs)
            if r0 < 0 or c0 < 0 or r0 >= nrow or c0 >= ncol:
                continue
            rs = min(rs, nrow - r0)
            cs = min(cs, ncol - c0)
            # 去掉被同一左上角覆盖的重复 merge
            key = (r0, c0)
            if key in seen:
                continue
            seen.add(key)
            fixed.append([r0, c0, rs, cs])
        self.merges = fixed
        if self.widths and len(self.widths) != ncol:
            self.widths = []

    def score(self) -> float:
        if not self.rows:
            return 0.0
        nrow, ncol = len(self.rows), len(self.rows[0])
        cells = [c for r in self.rows for c in r]
        nonempty = sum(1 for c in cells if c.strip())
        if nrow < 2 or ncol < 2:
            return 0.15
        q = 0.55
        q += 0.30 * (nonempty / max(1.0, len(cells)))
        if nrow >= 3:
            q += 0.10
        if self.merges:
            q += 0.05
        if nrow <= 2 and nonempty > 12:
            q -= 0.45          # 合并表/双栏表被压成 2 行
        if ncol >= 8 and nrow <= 3:
            q -= 0.15
        if ncol == 1:
            q -= 0.25
        return max(0.0, min(1.0, q))

    def cell_ir(self, ri, ci):
        try:
            return CellIR.from_text(self.rows[ri][ci])
        except Exception:
            return CellIR([])

    def to_data(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            'rows': [list(r) for r in self.rows],
            'merges': [list(m) for m in self.merges],
            'widths': list(self.widths),
            '_quality': round(self.quality, 3),
            '_table_ir': 1,
        }
        if self.bbox:
            d['bbox'] = list(self.bbox)
        for k, v in self.extra.items():
            if k not in d:
                d[k] = v
        return d

    def is_usable(self, min_quality: float = 0.45) -> bool:
        return self.quality >= min_quality

    def shape(self) -> Tuple[int, int]:
        return (len(self.rows), len(self.rows[0]) if self.rows else 0)
