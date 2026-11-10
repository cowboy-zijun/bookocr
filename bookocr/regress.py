# -*- coding: utf-8 -*-
"""对象级回归检查：只看页标、表格形状、插图数，不看全文像素。

用法：
    python -m bookocr.regress 输出.docx --pages 3 --tables 6x9 --images 4
"""
from __future__ import annotations

import argparse
import re
import sys
from typing import Any, Dict, List, Optional


PAGE_RE = re.compile(u'— 原书第\\s*([^\\s]+?)\\s*页')


def inspect_docx(path: str) -> Dict[str, Any]:
    from docx import Document
    doc = Document(path)
    labels = []
    for p in doc.paragraphs:
        m = PAGE_RE.search(p.text or '')
        if m:
            labels.append(m.group(1))
    tables = []
    for t in doc.tables:
        tables.append((len(t.rows), len(t.columns)))
    return {
        'path': path,
        'page_labels': labels,
        'page_count': len(labels),
        'tables': tables,
        'table_count': len(tables),
        'images': len(doc.inline_shapes),
        'sections': len(doc.sections),
    }


def _parse_tables(spec: Optional[str]) -> Optional[List[tuple]]:
    if not spec:
        return None
    out = []
    for part in spec.split(','):
        part = part.strip().lower()
        if not part:
            continue
        if 'x' in part:
            a, b = part.split('x', 1)
            out.append((int(a), int(b)))
        else:
            out.append((int(part),))
    return out


def check(path: str, pages: Optional[int] = None,
          tables: Optional[str] = None,
          images: Optional[int] = None) -> bool:
    info = inspect_docx(path)
    ok = True
    if pages is not None and info['page_count'] != pages:
        print('FAIL page_count: %d != %d' % (info['page_count'], pages))
        ok = False
    exp_t = _parse_tables(tables)
    if exp_t is not None:
        got = info['tables']
        if len(got) != len(exp_t):
            print('FAIL tables count: %s != %s' % (got, exp_t))
            ok = False
        else:
            for g, e in zip(got, exp_t):
                if len(e) == 2 and g != e:
                    print('FAIL table shape: %s != %s' % (g, e))
                    ok = False
                elif len(e) == 1 and g[0] != e[0]:
                    print('FAIL table rows: %s != %s' % (g, e))
                    ok = False
    if images is not None and info['images'] != images:
        print('FAIL images: %d != %d' % (info['images'], images))
        ok = False
    if ok:
        print('OK %s pages=%d tables=%s images=%d'
              % (path, info['page_count'], info['tables'], info['images']))
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='对象级回归检查')
    ap.add_argument('docx')
    ap.add_argument('--pages', type=int)
    ap.add_argument('--tables', help='如 6x9,8x4,20x10')
    ap.add_argument('--images', type=int)
    a = ap.parse_args(argv)
    return 0 if check(a.docx, a.pages, a.tables, a.images) else 1


if __name__ == '__main__':
    sys.exit(main())
