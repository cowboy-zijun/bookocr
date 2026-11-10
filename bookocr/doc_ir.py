# -*- coding: utf-8 -*-
"""Page-faithful Document IR and renderers.

Word is one renderer. This module turns the existing PageDoc/TableIR/
layout blocks into a JSON/HTML-friendly IR:

    DocumentIR
      pages[]
        geometry, blocks[], objects[], reading_order[]

It is intentionally lossless enough for JSON/HTML export without rewriting
the Word exporter. Bboxes are derived from the source lines; runs carry the
per-character span styles already used by export.py.
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .table_ir import TableIR


IR_VERSION = 1
_SPACE = re.compile(r'\s+')


def _norm(t: str) -> str:
    return _SPACE.sub('', t or '')


def _kind_type(kind: str, level: int = 0) -> Tuple[str, int]:
    if kind == 'head':
        return ('heading', level or 1)
    if kind in ('body', 'index'):
        return ('paragraph', 0)
    if kind == 'note':
        return ('footnote', 0)
    if kind == 'cite':
        return ('quote', 0)
    if kind == 'cap':
        return ('caption', 0)
    if kind == 'toc':
        return ('toc', 0)
    if kind == 'tablegrid':
        return ('table', 0)
    if kind in ('image', 'tablepage'):
        return ('figure', 0)
    return (kind or 'paragraph', 0)


@dataclass
class RunIR:
    text: str
    size: Optional[float] = None
    font: str = ''
    bold: bool = False
    italic: bool = False
    sup: bool = False
    sub: bool = False
    confidence: float = 1.0

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items() if v not in ('', None)}


@dataclass
class BlockIR:
    id: str
    type: str
    bbox: Optional[List[float]]
    runs: List[RunIR] = field(default_factory=list)
    level: int = 0
    source: str = ''
    confidence: float = 1.0
    table: Optional[dict] = None
    path: str = ''

    def text(self):
        return ''.join(r.text for r in self.runs)

    def to_dict(self):
        d = {'id': self.id, 'type': self.type, 'bbox': self.bbox,
             'runs': [r.to_dict() for r in self.runs],
             'source': self.source,
             'confidence': round(self.confidence, 3)}
        if self.level:
            d['level'] = self.level
        if self.table:
            d['table'] = self.table
        if self.path:
            d['path'] = self.path
        return d


@dataclass
class PageIR:
    no: int
    width: Optional[float] = None
    height: Optional[float] = None
    geometry: Optional[dict] = None
    label: Optional[str] = None
    blocks: List[BlockIR] = field(default_factory=list)
    reading_order: List[str] = field(default_factory=list)

    def to_dict(self):
        return {'no': self.no, 'width': self.width, 'height': self.height,
                'geometry': self.geometry, 'label': self.label,
                'blocks': [b.to_dict() for b in self.blocks],
                'reading_order': self.reading_order}


@dataclass
class DocumentIR:
    ir_version: int = IR_VERSION
    pages: List[PageIR] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return {'ir_version': self.ir_version,
                'metadata': self.metadata,
                'pages': [p.to_dict() for p in self.pages]}

    def to_json(self, indent=2):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


def _page_char_owners(lines):
    chars, owners = [], []
    for l in sorted(lines or [], key=lambda x: (x.get('y0') or 0,
                                                x.get('x0') or 0)):
        for ch in (l.get('text') or ''):
            if ch.isspace():
                continue
            chars.append(ch)
            owners.append(l)
    return ''.join(chars), owners


def _bbox_for_text(block_text, char_str, owners):
    t = _norm(block_text)
    if not t or not char_str:
        return None
    pos = char_str.find(t)
    if pos < 0:
        return None
    seg = owners[pos:pos + len(t)]
    if not seg:
        return None
    return [round(min(l['x0'] for l in seg), 1),
            round(min(l['y0'] for l in seg), 1),
            round(max(l['x1'] for l in seg), 1),
            round(max(l['y1'] for l in seg), 1)]


def _runs_for_block(text, mask, style_map):
    text = text or ''
    mask = mask or [False] * len(text)
    chars, styles = style_map if style_map else ('', [])
    t = _norm(text)
    pos = chars.find(t) if t else -1
    runs: List[RunIR] = []
    si = pos if pos >= 0 else 0
    for i, ch in enumerate(text):
        if ch.isspace():
            sty = (10.5, '', False, False, False, False)
            red = False
        else:
            sty = styles[si] if pos >= 0 and si < len(styles) else \
                (10.5, '', False, False, False, False)
            si += 1
            red = bool(mask[i]) if i < len(mask) else False
        size, font, sup, sub, bold, italic = sty if len(sty) == 6 else \
            (10.5, '', False, False, False, False)
        conf = 0.55 if red else 1.0
        key = (round(float(size or 0), 1), font, bool(sup), bool(sub),
               bool(bold), bool(italic))
        if runs and (runs[-1].size, runs[-1].font, runs[-1].sup,
                     runs[-1].sub, runs[-1].bold, runs[-1].italic) == key:
            runs[-1].text += ch
            runs[-1].confidence = min(runs[-1].confidence, conf)
        else:
            runs.append(RunIR(ch, float(size or 10.5), font, bool(bold),
                              bool(italic), bool(sup), bool(sub), conf))
    return runs


def build_document_ir(blocks, pagebook, pages, style_maps=None,
                      page_geometries=None, page_numbers=None,
                      page_sections=None, source='') -> DocumentIR:
    """Build IR from render blocks + PageBook + source lines."""
    style_maps = style_maps or {}
    page_geometries = page_geometries or {}
    page_numbers = page_numbers or {}
    page_sections = page_sections or {}
    doc = DocumentIR(metadata={'source': os.path.basename(source or ''),
                               'generator': 'bookocr'})
    by_no = {p.page_no: p for p in (pagebook.pages if pagebook else [])}
    for pdoc in (pagebook.pages if pagebook else []):
        if pdoc.page_no <= 0:
            continue
        g = page_geometries.get(pdoc.page_no) or pdoc.geometry or {}
        pir = PageIR(no=pdoc.page_no,
                     width=g.get('page_w'), height=g.get('page_h'),
                     geometry=g or None,
                     label=str(page_numbers.get(pdoc.page_no, '')) or
                     page_sections.get(pdoc.page_no))
        lines = pages.get(pdoc.page_no) or []
        char_str, owners = _page_char_owners(lines)
        sty = style_maps.get(pdoc.page_no)
        for bi, b in enumerate(pdoc.blocks):
            if not b or b[0] == 'page':
                continue
            kind = b[0]
            text = (b[1] or '').replace(u'\ue000', '')
            mask = b[2] or []
            if (b[1] or '').startswith(u'\ue000') and mask:
                mask = mask[1:]
            typ, level = _kind_type(kind, b[4] if len(b) > 4 else 0)
            bid = 'p%db%d' % (pdoc.page_no, bi + 1)
            bbox = _bbox_for_text(text, char_str, owners)
            if kind == 'tablegrid':
                bb = b[7] if len(b) > 7 else None
                if bb:
                    bbox = list(bb)
                try:
                    data = json.loads(text)
                except Exception:
                    data = {'rows': []}
                tir = TableIR.from_data(data, bbox=tuple(bbox) if bbox else None)
                blk = BlockIR(bid, 'table', bbox, [], 0, 'table_ir',
                              tir.quality, table=tir.to_data())
            elif kind in ('image', 'tablepage'):
                bb = b[7] if len(b) > 7 else None
                blk = BlockIR(bid, typ, list(bb) if bb else bbox, [], 0,
                              'image', 1.0, path=text)
            else:
                runs = _runs_for_block(text, mask, sty)
                conf = min([r.confidence for r in runs] or [1.0])
                src = 'text_layer' if sty else 'ocr'
                blk = BlockIR(bid, typ, bbox, runs, level, src, conf)
            pir.blocks.append(blk)
            pir.reading_order.append(bid)
        doc.pages.append(pir)
    return doc


def render_json(doc: DocumentIR, path: str) -> str:
    with open(path, 'w', encoding='utf-8') as f:
        f.write(doc.to_json())
    return path


def _html_runs(runs):
    out = []
    for r in runs:
        txt = html.escape(r.text)
        if r.sup:
            txt = '<sup>%s</sup>' % txt
        if r.sub:
            txt = '<sub>%s</sub>' % txt
        if r.bold:
            txt = '<strong>%s</strong>' % txt
        if r.italic:
            txt = '<em>%s</em>' % txt
        out.append(txt)
    return ''.join(out)


def _html_table(data):
    rows = data.get('rows') or []
    merges = data.get('merges') or []
    if not rows:
        return ''
    covered = set()
    starts = {}
    for m in merges:
        if len(m) < 4:
            continue
        r0, c0, rs, cs = [int(x) for x in m[:4]]
        starts[(r0, c0)] = (max(1, rs), max(1, cs))
        for rr in range(r0, r0 + max(1, rs)):
            for cc in range(c0, c0 + max(1, cs)):
                if (rr, cc) != (r0, c0):
                    covered.add((rr, cc))
    out = ['<table>']
    for ri, row in enumerate(rows):
        out.append('<tr>')
        for ci, cell in enumerate(row):
            if (ri, ci) in covered:
                continue
            rs, cs = starts.get((ri, ci), (1, 1))
            span = ''
            if rs > 1:
                span += ' rowspan="%d"' % rs
            if cs > 1:
                span += ' colspan="%d"' % cs
            out.append('<td%s>%s</td>' % (span, html.escape(str(cell or ''))))
        out.append('</tr>')
    out.append('</table>')
    return ''.join(out)


def _img_data_uri(path):
    try:
        with open(path, 'rb') as f:
            data = f.read()
        ext = os.path.splitext(path)[1].lower().lstrip('.') or 'png'
        if ext == 'jpg':
            ext = 'jpeg'
        return 'data:image/%s;base64,%s' % (
            ext, base64.b64encode(data).decode('ascii'))
    except Exception:
        return ''


def render_html(doc: DocumentIR, title=None, embed_images=True):
    title = html.escape(title or doc.metadata.get('source') or 'BookOCR')
    css = """
body{font-family:"Noto Serif SC","Songti SC",serif;max-width:920px;margin:2rem auto;padding:0 1rem;line-height:1.75;color:#222}
.page{border-top:1px solid #ddd;margin-top:2.2rem;padding-top:1.2rem}
.page::before{content:"第 " attr(data-page) " 页";display:block;color:#999;font-size:.8rem}
h1,h2,h3,h4{font-family:"Noto Sans SC","Microsoft YaHei",sans-serif}
p{margin:.55rem 0;text-align:justify}
table{border-collapse:collapse;margin:1rem 0;font-size:.86rem}
td,th{border:1px solid #777;padding:3px 5px;vertical-align:top}
figure{margin:1rem 0;text-align:center}
img{max-width:100%}
.meta{color:#888;font-size:.85rem}
"""
    out = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           '<title>%s</title><style>%s</style></head><body>' % (title, css),
           '<h1>%s</h1>' % title,
           '<p class="meta">Page-faithful IR renderer · %d 页</p>'
           % len(doc.pages)]
    for p in doc.pages:
        out.append('<section class="page" data-page="%d">' % p.no)
        for b in p.blocks:
            if b.type == 'heading':
                lvl = min(4, max(1, b.level or 1))
                out.append('<h%d>%s</h%d>' % (lvl, _html_runs(b.runs), lvl))
            elif b.type == 'paragraph':
                out.append('<p>%s</p>' % _html_runs(b.runs))
            elif b.type == 'footnote':
                out.append('<p class="note"><small>%s</small></p>'
                           % _html_runs(b.runs))
            elif b.type == 'quote':
                out.append('<blockquote>%s</blockquote>' % _html_runs(b.runs))
            elif b.type == 'caption':
                out.append('<p class="caption"><small>%s</small></p>'
                           % _html_runs(b.runs))
            elif b.type == 'table' and b.table:
                out.append(_html_table(b.table))
            elif b.type == 'figure':
                src = _img_data_uri(b.path) if (embed_images and b.path) else ''
                if src:
                    out.append('<figure><img src="%s" alt="figure"></figure>'
                               % src)
                else:
                    out.append('<p class="meta">[图]</p>')
        out.append('</section>')
    out.append('</body></html>')
    return ''.join(out)
