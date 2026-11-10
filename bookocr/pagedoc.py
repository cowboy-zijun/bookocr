# -*- coding: utf-8 -*-
"""PageDoc：把全书 block 列表收拢成“页对象”。

旧代码里 blocks 是一条平铺列表，页码靠全局下标/偏移维护，任何一次
增删块都可能把页标记切错。PageDoc 只负责一件事：块属于哪一页。

它不改变块的排版语义，也不替代 layout/table；只是给后续“按页几何、
对象账本、页码验收、回归检查”一个稳定入口。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


PAGE = 'page'
OBJECT_KINDS = {
    'image', 'tablepage', 'tablegrid', 'linepage', 'frametree',
    'coltable', 'table', 'cols2', 'cols2page',
}


def _page_marker(no: int) -> List[Any]:
    # 和 export_docx 的解包形状一致；后面的字段允许为空。
    return [PAGE, str(no), [], True, 0, None, 0, None]


@dataclass
class PageDoc:
    page_no: int
    blocks: List[Any] = field(default_factory=list)
    geometry: Optional[dict] = None
    label: Optional[str] = None
    source: str = ''

    def add(self, block: Any) -> None:
        self.blocks.append(block)

    def by_kind(self, *kinds: str) -> List[Any]:
        return [b for b in self.blocks if b and b[0] in kinds]

    @property
    def objects(self) -> List[Any]:
        return [b for b in self.blocks if b and b[0] in OBJECT_KINDS]

    @property
    def text(self) -> str:
        return ''.join((b[1] or '') for b in self.blocks
                       if b and b[0] in ('body', 'head', 'index', 'note'))

    def to_blocks(self) -> List[Any]:
        return [_page_marker(self.page_no)] + list(self.blocks)


class PageBook:
    """按页归属的块账本。

    from_blocks 只做分组，不丢块；to_blocks 再按页拼回。页标记里的
    page_no 必须递增且不重复，否则 validate() 会报错。
    """

    def __init__(self, pages: Optional[Sequence[PageDoc]] = None):
        self.pages: List[PageDoc] = list(pages or [])

    @classmethod
    def from_blocks(cls, blocks: Iterable[Any], geometry=None,
                    labels: Optional[Dict[int, str]] = None,
                    geometry_by_page: Optional[
                        Dict[int, dict]] = None,
                    source: str = '') -> 'PageBook':
        pb = cls()
        cur: Optional[PageDoc] = None
        for b in blocks:
            if not b:
                continue
            if b[0] == PAGE:
                try:
                    no = int(b[1])
                except Exception:
                    no = len(pb.pages) + 1
                g = None
                if geometry_by_page:
                    g = geometry_by_page.get(no)
                if g is None and isinstance(geometry, dict):
                    g = geometry
                lab = None
                if labels:
                    lab = labels.get(no)
                cur = PageDoc(no, [], g, lab, source)
                pb.pages.append(cur)
                continue
            if cur is None:
                cur = PageDoc(0, [], None, None, source)
                pb.pages.append(cur)
            cur.add(b)
        return pb

    def page(self, no: int) -> Optional[PageDoc]:
        for p in self.pages:
            if p.page_no == no:
                return p
        return None

    def to_blocks(self) -> List[Any]:
        out: List[Any] = []
        for p in self.pages:
            out.extend(p.to_blocks())
        return out

    def geometry_map(self) -> Dict[int, dict]:
        """每页几何入口；目前多为全局 geom，后续可逐页覆盖。"""
        return {p.page_no: p.geometry for p in self.pages
                if p.page_no > 0 and p.geometry}

    def page_numbers(self) -> List[int]:
        return [p.page_no for p in self.pages if p.page_no > 0]

    def count(self) -> int:
        return len([p for p in self.pages if p.page_no > 0])

    def validate(self) -> List[str]:
        errs: List[str] = []
        nos = self.page_numbers()
        if len(nos) != len(set(nos)):
            errs.append('页号重复')
        if any(b[0] == PAGE for p in self.pages for b in p.blocks):
            errs.append('页块里混入了 page 标记')
        for p in self.pages:
            for b in p.blocks:
                if not isinstance(b, (list, tuple)) or len(b) < 2:
                    errs.append('第 %s 页有坏块' % p.page_no)
                    break
        return errs

    def object_count(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for p in self.pages:
            for b in p.objects:
                out[b[0]] = out.get(b[0], 0) + 1
        return out
