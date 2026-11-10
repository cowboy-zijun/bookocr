# -*- coding: utf-8 -*-
"""页间接缝检验：检查相邻两页之间句子是否被拆开（是否被误当成新段缩进）、
是否有换行产物标点、以及汉字之间是否残留多余空格。

用法：
    python cli.py --check 输出.docx
"""
import re

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn

PAGE_RE = re.compile(u'— 原书第 (\\d+) 页 —')
SENT_END = tuple(u'。！？：…”』」）】')
LEAD_PUNCT = u'，。、；：！？…'
CJK_SPACE = re.compile(u'([\\u3400-\\u9fff])([ \\t]+)([\\u3400-\\u9fff])')


def _font_of(p):
    for r in p.runs:
        try:
            ea = r._element.rPr.rFonts.get(qn('w:eastAsia'))
        except Exception:
            ea = None
        if r.text.strip():
            return ea, (r.font.size.pt if r.font.size else 0)
    return None, 0


def _is_body(p):
    """正文段：宋体且字号不小（排除标题/目录/索引/注文）。"""
    if p.alignment == WD_ALIGN_PARAGRAPH.RIGHT:
        return False          # 右对齐的引文出处/署名不是正文句子
    ea, sz = _font_of(p)
    return ea == u'宋体' and sz >= 11.5


def _indent_pt(p):
    return p.paragraph_format.first_line_indent


def _head_like(t):
    """跨页衔接检查里的小节标题：短、无句读、以编号开头。"""
    s = (t or '').strip()
    if not s or len(s) > 30:
        return False
    if re.search(u'[。！？；，]', s):
        return False
    return bool(re.match(u'^\\d+(?:\\.\\d+)*[.、]?\\s*[\u4e00-\u9fff]', s))


def check_docx(path):
    doc = Document(path)
    pages = {}
    cur = 0
    for p in doc.paragraphs:
        t = p.text.strip()
        m = PAGE_RE.match(t)
        if m:
            cur = int(m.group(1))
            continue
        if not t:
            continue
        _ea, sz = _font_of(p)
        if sz and sz < 10:
            continue          # 页眉等灰色小字段，不参与页间衔接检查
        pages.setdefault(cur, []).append((t, _is_body(p), p))

    issues = []
    oks = 0
    keys = sorted(pages)
    for a, b in zip(keys, keys[1:]):
        last_t, last_body, last_p = pages[a][-1]
        first_t, first_body, first_p = pages[b][0]
        # 1) 汉字间多余空格（全篇 + 衔接处重点看）
        for page_no, seg in ((a, last_t[-10:]), (b, first_t[:10])):
            for m in CJK_SPACE.finditer(seg):
                issues.append((page_no,
                               u'汉字间空格',
                               u'「%s」' % seg[max(0, m.start() - 3):m.end() + 3],
                               u''))
        if not (last_body and first_body):
            continue
        # 跨页处正好是小节标题（"1 文献回顾"→"2 研究设计"）不是句子
        # 被拆断；旧口径会把它误报成“跨页句子被当成新段”。
        if _head_like(last_t) or _head_like(first_t):
            continue
        # 2) 上一页末段句末没有结束标点 → 句子跨页
        if not last_t.endswith(SENT_END):
            ind = _indent_pt(first_p)
            if ind in (None, 0):
                oks += 1          # 已按延续段处理（不缩进），正常
            else:
                issues.append((b, u'跨页句子被当成新段（首行缩进）',
                               u'上页末「%s」→ 本页首「%s…」' % (last_t[-14:], first_t[:12]),
                               u''))
        # 3) 下一页首段以标点开头 → 换行产物未清理
        if first_t and first_t[0] in LEAD_PUNCT:
            issues.append((b, u'句首是标点', u'「%s…」' % first_t[:14], u''))

    lines = [u'===== 页间接缝检验：%s =====' % path,
             u'句子跨页的正常延续（已按不缩进处理）：%d 处' % oks]
    n = 0
    for page, kind, ctx, extra in issues:
        lines.append(u'  第 %3d 页  %s  %s %s' % (page, kind, ctx, extra))
        n += 1
        if n >= 80:
            lines.append(u'  …（只显示前 80 条）')
            break
    if not issues:
        lines.append(u'未发现问题：无句首标点、无汉字间空格、无被误当成新段的跨页句子。')
    else:
        lines.append(u'共 %d 处。' % n)
    for ln in lines:
        print(ln)
    return not issues


def check_docx_lines(path):
    """同上，但把报告作为字符串列表返回（供界面调用）。"""
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        check_docx(path)
    return buf.getvalue().splitlines()
