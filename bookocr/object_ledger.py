# -*- coding: utf-8 -*-
"""统一对象账本：页内表/图只从这里查，不再各路径自行猜。

一个 ObjectFrame 只描述“页面上的一个对象”：
  page, kind, bbox, path/table_data, quality
对象身份由 (page, kind, bbox) 去重，防止 tablepage + image 同时插同一张表。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _bbox(b) -> Optional[Tuple[float, float, float, float]]:
    if not b:
        return None
    try:
        vals = [float(x) for x in b[:4]]
    except Exception:
        return None
    if len(vals) != 4:
        return None
    return tuple(vals)


@dataclass
class ObjectFrame:
    page: int
    kind: str                       # table | figure | formula
    bbox: Tuple[float, float, float, float]
    path: str = ''
    table_data: Dict[str, Any] = field(default_factory=dict)
    quality: float = 1.0
    source: str = ''
    id: str = ''

    def __post_init__(self):
        if not self.id:
            self.id = '%d:%s:%s' % (
                self.page, self.kind,
                ','.join('%.1f' % x for x in self.bbox))

    def key(self):
        return (self.page, self.kind,
                tuple(round(x, 1) for x in self.bbox))

    def to_dict(self):
        return {'id': self.id, 'page': self.page, 'kind': self.kind,
                'bbox': [round(x, 2) for x in self.bbox],
                'path': self.path, 'quality': round(self.quality, 3),
                'has_table': bool(self.table_data.get('rows')),
                'source': self.source}


def from_block(page: int, block: Any) -> Optional[ObjectFrame]:
    if not block or not isinstance(block, (list, tuple)):
        return None
    kind = block[0]
    if kind not in ('image', 'tablepage', 'tablegrid', 'table'):
        return None
    data = {}
    path = ''
    if kind == 'tablegrid':
        try:
            data = json.loads(block[1]) if isinstance(block[1], str) else (block[1] or {})
        except Exception:
            data = {}
        path = data.get('_path') or ''
    elif kind == 'table':
        try:
            data = json.loads(block[1]) if isinstance(block[1], str) else (block[1] or {})
        except Exception:
            data = {}
        path = block[1] if isinstance(block[1], str) and block[1].endswith(('.png', '.jpg')) else ''
    elif kind == 'tablepage':
        path = block[1] if isinstance(block[1], str) else ''
    else:
        path = block[1] if isinstance(block[1], str) else ''
    bb = None
    # tablegrid/tablepage 的 bbox 在 b[7]；image 在 b[5]
    for idx in (7, 5, 3):
        if len(block) > idx:
            bb = _bbox(block[idx])
            if bb:
                break
    if not bb:
        return None
    role = 'table' if kind in ('tablegrid', 'table', 'tablepage') else 'figure'
    return ObjectFrame(page=page, kind=role, bbox=bb, path=path or '',
                       table_data=data, source=kind)


def from_pagebook(pb) -> List[ObjectFrame]:
    out: List[ObjectFrame] = []
    seen = set()
    for p in getattr(pb, 'pages', []) or []:
        for b in p.blocks:
            obj = from_block(p.page_no, b)
            if obj is None:
                continue
            if obj.key() in seen:
                continue
            seen.add(obj.key())
            out.append(obj)
    return out


def summary(frames: Iterable[ObjectFrame]) -> Dict[str, Any]:
    frames = list(frames)
    by_kind: Dict[str, int] = {}
    pages = set()
    for f in frames:
        by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
        pages.add(f.page)
    return {'total': len(frames), 'by_kind': by_kind,
            'pages': len(pages),
            'page_screenshots': 0,
            'frames': [f.to_dict() for f in frames]}


def to_json(frames: Iterable[ObjectFrame], path: str) -> str:
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(summary(frames), f, ensure_ascii=False, indent=2)
    return path
