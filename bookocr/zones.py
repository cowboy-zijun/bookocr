# -*- coding: utf-8 -*-
"""页面分区：先按几何把一页切成"带 → 栏 → 行"，再交给整合层贴标签。

思路是递归 XY 切割：
  1. 先剥掉页眉/页脚（跨页重复的短行、页码）；
  2. 在当前区域里找横向大空白/横线，切成若干"带"；
  3. 每条带里找贯穿的竖向空白通道，切成若干"栏"；
  4. 对每个子区域重复，直到区域里栏宽、字号基本一致。

这一层只看几何（行框、空白、字号），不看"摘要""参考文献"这些词；
语义标签留给整合层。这样论文、图书、期刊可以用同一套切割器。
"""
import collections
import json
import re


def _norm(t):
    return re.sub(u'[^0-9A-Za-z\\u3400-\\u9fff]', '', t or '')


def _median(vals, default=0.0):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return default
    return vals[len(vals) // 2]


def _text_preview(lines, n=2):
    out = []
    for l in sorted(lines, key=lambda x: (x['y0'], x['x0'])):
        t = (l.get('text') or '').strip()
        if t:
            out.append(t)
        if len(out) >= n:
            break
    return u' / '.join(out)


def _rows(lines):
    """按 y 中心把行归并成视觉行；返回 [(y0,y1,lines), ...], line_h。"""
    ls = sorted(lines, key=lambda l: ((l['y0'] + l['y1']) / 2.0, l['x0']))
    hs = [l['y1'] - l['y0'] for l in ls if l['y1'] > l['y0']]
    line_h = _median(hs, 60.0)
    rows = []
    for l in ls:
        cy = (l['y0'] + l['y1']) / 2.0
        if rows and abs(cy - rows[-1][3]) <= max(8.0, line_h * 0.45):
            y0, y1, arr, _cy = rows[-1]
            arr.append(l)
            rows[-1] = (min(y0, l['y0']), max(y1, l['y1']), arr, cy)
        else:
            rows.append((l['y0'], l['y1'], [l], cy))
    return [(a, b, c) for a, b, c, _ in rows], line_h


def _is_furniture_text(t):
    t = (t or '').strip()
    if not t:
        return False
    if re.match(u'^\\d{1,4}$', t):
        return True
    return not re.sub(u'[\\d\\s—\\-_.·]', '', t)


def _furniture_size_limit(pages):
    """页眉/页脚通常是小字；首页大标题即使出现在页顶也不能当家具删掉。"""
    szs = []
    for ls in pages.values():
        for l in ls:
            if (l['y0'] < 900 or l['y0'] > 4400) and l.get('size'):
                szs.append(l['size'])
    med = _median(szs, 0.0)
    return max(12.0, med * 1.5) if med else 0.0


def _furniture_keys(pages):
    """跨页重复的页眉/页脚短行（去数字后比较）。"""
    cnt = collections.Counter()
    lim = _furniture_size_limit(pages)
    for ls in pages.values():
        for l in ls:
            y = l['y0']
            if y < 900 or y > 4400:
                if lim and (l.get('size') or 0) >= lim:
                    continue
                k = _norm(l.get('text') or '')
                if 2 <= len(k) <= 30:
                    cnt[k] += 1
    return {k for k, c in cnt.items() if c >= 2}


def _collect_furniture(pages):
    keys = _furniture_keys(pages)
    lim = _furniture_size_limit(pages)
    out = {}
    for p, ls in pages.items():
        fur, body = [], []
        for l in ls:
            y = l['y0']
            t = (l.get('text') or '').strip()
            if y < 900 or y > 4400:
                if lim and (l.get('size') or 0) >= lim:
                    body.append(l)
                    continue
                if _is_furniture_text(t) or _norm(t) in keys:
                    fur.append(l)
                    continue
            body.append(l)
        out[p] = (fur, body)
    return out


def _split_h(lines, line_h, page_h, rules=None, bbox=None):
    """在横向大空白/横线处切成带；返回 [lines...]（>1 表示切开了）。"""
    # 硬边界：矢量横线/扫描图里量到的横线。期刊第 3 页本文与
    # "（上接第X页）"他文之间的分隔线就是这一类。
    if rules and bbox:
        cuts = []
        for y, x0, x1 in rules:
            if bbox[1] + 10 < y < bbox[3] - 10 \
                    and (x1 - x0) >= (bbox[2] - bbox[0]) * 0.55:
                cuts.append(y)
        if cuts:
            cuts = sorted(set(round(y, 1) for y in cuts))
            groups, cur, ci = [], [], 0
            for l in sorted(lines, key=lambda z: (z['y0'], z['x0'])):
                while ci < len(cuts) and (l['y0'] + l['y1']) / 2.0 >= cuts[ci]:
                    if cur:
                        groups.append(cur)
                    cur = []
                    ci += 1
                cur.append(l)
            if cur:
                groups.append(cur)
            if len(groups) > 1:
                return groups
    rows, _lh = _rows(lines)
    if len(rows) < 2:
        return [lines]
    # 字号突变：正文→参考文献/脚注、正文→标题。只在突变后至少
    # 连续两行保持新字号时切，避免把上标/角标当成新区域。
    sizes = []
    for _y0, _y1, arr in rows:
        ss = [l.get('size') or 0 for l in arr if l.get('size')]
        sizes.append(_median(ss, 0.0))
    size_cuts = []
    for i in range(1, len(rows) - 2):
        a, b = sizes[i - 1], sizes[i]
        if not a or not b:
            continue
        if abs(b - a) / float(max(a, b)) < 0.12:
            continue
        if abs(sizes[i + 1] - b) / float(max(sizes[i + 1], b) or 1) > 0.08:
            continue
        if abs(sizes[i + 2] - b) / float(max(sizes[i + 2], b) or 1) > 0.08:
            continue
        size_cuts.append(i)
    if size_cuts:
        groups, start = [], 0
        for i in size_cuts:
            g = [l for _y0, _y1, arr in rows[start:i] for l in arr]
            if g:
                groups.append(g)
            start = i
        g = [l for _y0, _y1, arr in rows[start:] for l in arr]
        if g:
            groups.append(g)
        if len(groups) > 1:
            return groups
    # 行间空白：下一行顶 - 上一行底。段间距通常 <1 行，这里只切大空白
    gap_min = max(1.6 * line_h, page_h * 0.030)
    groups, cur = [], [rows[0]]
    for prev, nxt in zip(rows, rows[1:]):
        gap = nxt[0] - prev[1]
        if gap >= gap_min:
            groups.append(cur)
            cur = [nxt]
        else:
            cur.append(nxt)
    groups.append(cur)
    if len(groups) <= 1:
        return [lines]
    out = []
    for g in groups:
        ls = [l for _y0, _y1, arr in g for l in arr]
        if ls:
            out.append(ls)
    return out


def _char_w(lines):
    vals = []
    for l in lines:
        n = len(_norm(l.get('text') or ''))
        if n >= 2 and l['x1'] > l['x0']:
            vals.append((l['x1'] - l['x0']) / float(n))
    return _median(vals, 30.0)


def _row_central_gap(row_lines, cw, page_w):
    """这一视觉行中间有没有一个栏间空白（用于识别"正文从此处起分栏"）。"""
    if len(row_lines) < 2:
        return False
    ls = sorted(row_lines, key=lambda l: l['x0'])
    for a, b in zip(ls, ls[1:]):
        gap = b['x0'] - a['x1']
        if gap < max(35.0, cw * 1.20):
            continue
        mid = (a['x1'] + b['x0']) / 2.0
        if page_w * 0.32 <= mid <= page_w * 0.68:
            return True
    return False


def _column_start(lines, page_w):
    """白纸黑字的正文从哪里开始分栏：前几行通栏、后几行出现中缝。

    返回切点行号（从 0 起）或 None。期刊第 1 页的"上半通栏、下半
    双栏"就是靠这个切出来的，而不是靠"文献标识码"这类文字。
    """
    rows, _lh = _rows(lines)
    if len(rows) < 4:
        return None
    cw = _char_w(lines)
    flags = []
    for _y0, _y1, arr in rows:
        txt = ''.join(l.get('text') or '' for l in arr)
        # 中图分类号/文献标识码/文章编号这一行本身也有几个"格"，
        # 不能把它当成正文开始分栏的那一行
        if re.search(u'(中图分类号|文献标识码|文章编号|作者简介|基金项目|'
                     u'关键词|摘要)', txt):
            flags.append(False)
        else:
            flags.append(_row_central_gap(arr, cw, page_w))
    if flags[0]:
        return None
    for i in range(1, len(flags) - 2):
        if not flags[i] and not flags[i - 1]:
            continue
        if flags[i] and flags[i + 1] and flags[i + 2]:
            return i
    return None


def _hanging_refs(lls, rls):
    """参考文献的悬挂编号：[11] 在左、正文在右，不是两栏。

    这种版式会在编号和正文之间留下一条贯穿空白，_find_gutter 会把它
    当成中缝，结果整页参考文献被拆成"编号栏 + 正文栏"。
    """
    if len(lls) < 4 or len(rls) < 4:
        return False
    nums = sum(1 for l in lls
               if re.match(u'^\\[\\d{1,3}\\]$', (l.get('text') or '').strip()))
    if nums < len(lls) * 0.7:
        return False
    ln = _median([len((l.get('text') or '').strip()) for l in lls], 0)
    rn = _median([len((l.get('text') or '').strip()) for l in rls], 0)
    return rn >= max(12, ln * 3)


def _find_gutter(lines, page_w):
    """找一条贯穿的竖向空白通道；返回 (lo, hi) 或 None。"""
    if len(lines) < 6:
        return None
    cw = _char_w(lines)
    W = float(page_w)
    bins = 400
    size = W / bins
    cov = [0] * bins
    for l in lines:
        a = max(0, int(l['x0'] / size))
        b = min(bins - 1, int(l['x1'] / size))
        for i in range(a, b + 1):
            cov[i] += 1
    empty = [i for i, c in enumerate(cov) if c == 0]
    if not empty:
        return None
    # 把连续的空 bin 合成通道
    runs = []
    start = prev = empty[0]
    for i in empty[1:]:
        if i == prev + 1:
            prev = i
        else:
            runs.append((start, prev))
            start = prev = i
    runs.append((start, prev))
    best = None
    for a, b in runs:
        lo, hi = a * size, (b + 1) * size
        w = hi - lo
        # 中缝可能只有半个字宽：中文行末的标点会探进中缝
        if w < max(30.0, cw * 0.60):
            continue
        # 允许 1—2 个标点缩进 / 折行稍微探进中缝，别因此否决整条通道
        left = sum(1 for l in lines if l['x1'] <= lo + 10)
        right = sum(1 for l in lines if l['x0'] >= hi - 10)
        cross = len(lines) - left - right
        if left < 3 or right < 3:
            continue
        if cross > max(3, int(len(lines) * 0.25)):
            continue
        score = w * min(left, right)
        if best is None or score > best[0]:
            best = (score, lo, hi, left, right, cross, w)
    if best is None:
        return None
    return best[1], best[2], best[3], best[4], best[5], best[6]


def _bbox(lines):
    return (min(l['x0'] for l in lines), min(l['y0'] for l in lines),
            max(l['x1'] for l in lines), max(l['y1'] for l in lines))


def _bbox_leaves(ls):
    bs = [x.get('bbox') or (0, 0, 0, 0) for x in ls]
    if not bs:
        return (0, 0, 0, 0)
    return (min(b[0] for b in bs), min(b[1] for b in bs),
            max(b[2] for b in bs), max(b[3] for b in bs))


def _zone(lines, level, evidence=None):
    z = {
        'level': level,
        'bbox': _bbox(lines) if lines else (0, 0, 0, 0),
        'n': len(lines),
        'size': round(_median([l.get('size') or 0 for l in lines], 0.0), 1),
        'preview': _text_preview(lines),
    }
    if evidence:
        z['evidence'] = evidence
    return z


def segment_lines(lines, page_w=3400.0, page_h=5000.0, depth=0,
                  rules=None):
    """递归切一页/一个区域；返回节点（叶子带 lines）。"""
    if len(lines) < 2 or depth > 6:
        return {'level': 'leaf', 'lines': lines,
                'bbox': _bbox(lines) if lines else (0, 0, 0, 0)}
    rows, line_h = _rows(lines)
    bands = _split_h(lines, line_h, page_h, rules=rules, bbox=_bbox(lines))
    if len(bands) > 1:
        children = [segment_lines(b, page_w, page_h, depth + 1, rules=rules)
                    for b in bands]
        return {'level': 'band', 'lines': lines, 'children': children,
                'bbox': _bbox(lines),
                'evidence': {'cut': 'h', 'bands': len(bands)}}
    # 上半通栏、下半双栏：在"开始出现中缝"的那一行切
    ci = _column_start(lines, page_w)
    if ci is not None:
        rows, _lh = _rows(lines)
        a = [l for _y0, _y1, arr in rows[:ci] for l in arr]
        b = [l for _y0, _y1, arr in rows[ci:] for l in arr]
        if a and b:
            return {'level': 'band', 'lines': lines,
                    'children': [segment_lines(a, page_w, page_h, depth + 1,
                                               rules=rules),
                                 segment_lines(b, page_w, page_h, depth + 1,
                                               rules=rules)],
                    'bbox': _bbox(lines),
                    'evidence': {'cut': 'h-cols', 'row': ci}}
    g = _find_gutter(lines, page_w)
    if g is not None:
        lo, hi, left, right, cross, w = g
        lls = [l for l in lines if l['x1'] <= lo]
        rls = [l for l in lines if l['x0'] >= hi]
        if lls and rls and not _hanging_refs(lls, rls):
            children = [segment_lines(lls, page_w, page_h, depth + 1,
                                      rules=rules),
                        segment_lines(rls, page_w, page_h, depth + 1,
                                      rules=rules)]
            return {'level': 'columns', 'lines': lines,
                    'children': children, 'bbox': _bbox(lines),
                    'evidence': {'cut': 'v', 'gutter': (round(lo, 1),
                                                       round(hi, 1)),
                                 'width': round(w, 1),
                                 'left': left, 'right': right,
                                 'cross': cross}}
    return {'level': 'leaf', 'lines': lines, 'bbox': _bbox(lines)}


def page_tree(lines, page_w=3400.0, page_h=5000.0, rules=None):
    """单页（已去掉页眉页脚）的分区树。"""
    return segment_lines(lines, page_w, page_h, rules=rules)


def build_trees(pages, page_w=3400.0, page_h=5000.0, rules=None):
    """多页分区：先统一识别页眉页脚，再逐页切。返回 {页码: 树}。

    rules: {页码: [(y, x0, x1), ...]} 横线（矢量线/图像线）。
    """
    fur = _collect_furniture(pages)
    trees = {}
    for p, ls in pages.items():
        furniture, body = fur.get(p, ([], ls))
        trees[p] = {
            'bbox': (0, 0, page_w, page_h),
            'n': len(ls),
            'level': 'page',
            'furniture': _zone(furniture, 'furniture') if furniture else None,
            'content': page_tree(body, page_w, page_h,
                                 rules=(rules or {}).get(p)),
        }
    return trees


def leaves(node):
    """展开树的叶子。"""
    if 'children' not in node:
        return [node]
    out = []
    for c in node['children']:
        out.extend(leaves(c))
    return out


REF_HEAD = re.compile(u'^(参考文献|注释|附注)[：:]?\\s*')
FOREIGN_ANCHOR = re.compile(u'上接第\\s*\\d+\\s*页')
META_WORDS = (u'中图分类号', u'文献标识码', u'文章编号',
              u'作者简介', u'基金项目', u'关键词', u'摘要')


def _node_from_lines(lines, level='leaf', evidence=None):
    z = {'level': level, 'lines': list(lines), 'bbox': _bbox(lines),
         'n': len(lines)}
    if evidence:
        z['evidence'] = evidence
    return z


def _split_ref_leaf(node):
    """叶子中途出现"参考文献/注释"标题时，切成正文 + 参考两块。"""
    if 'children' in node:
        node['children'] = [_split_ref_leaf(c) for c in node['children']]
        return node
    ls = list(node.get('lines') or [])
    for i, l in enumerate(ls):
        if i > 0 and REF_HEAD.match((l.get('text') or '').strip()):
            a = _node_from_lines(ls[:i])
            b = _node_from_lines(ls[i:])
            return {'level': 'band', 'n': len(ls),
                    'bbox': _bbox(ls), 'lines': ls,
                    'children': [a, b], 'evidence': {'cut': 'refs'}}
    return node


def _text_of(node):
    return u'\n'.join((l.get('text') or '') for l in (node.get('lines') or []))


def _label_node(node, page, page_h, body_size, foreign=False):
    txt = _text_of(node)
    if 'children' in node:
        b = node.get('bbox') or (0, 0, 0, 0)
        # 只在"确实位于页面下半截、且包含上接锚点"的那一层整块标他文，
        # 不能因为根节点包含这句就把上半页本文也标成他文。
        if FOREIGN_ANCHOR.search(txt) and b[1] > page_h * 0.30:
            node['foreign'] = True
            for c in node['children']:
                _label_node(c, page, page_h, body_size, True)
            return
        node['foreign'] = foreign
        for c in node['children']:
            _label_node(c, page, page_h, body_size, foreign)
        return
    b = node.get('bbox') or (0, 0, 0, 0)
    szs = [l.get('size') or 0 for l in (node.get('lines') or [])
           if l.get('size')]
    sz = _median(szs, 0.0)
    if foreign:
        node['role'] = u'他文续文'
        return
    head = (txt.strip().split(u'\n')[0] if txt.strip() else u'')
    if REF_HEAD.match(head):
        node['role'] = u'参考文献'
        return
    if u'参考文献' in txt and not REF_HEAD.match(head):
        node['role'] = u'正文/参考'
        return
    if page == 1 and b[1] < page_h * 0.30 and body_size \
            and max(szs or [0]) >= body_size * 1.25:
        node['role'] = u'题名'
        return
    if any(w in txt for w in (u'摘要', u'关键词')):
        node['role'] = u'摘要/关键词'
        return
    if any(w in txt for w in META_WORDS):
        node['role'] = u'元数据'
        return
    if b[1] < page_h * 0.075:
        node['role'] = u'页眉'
        return
    if b[3] > page_h * 0.87 and body_size and sz < body_size * 0.98:
        node['role'] = u'注文'
        return
    node['role'] = u'正文'


def label_trees(trees, pages, page_h=5000.0):
    """给分区树贴语义标签：题名/摘要/元数据/正文/参考/他文续文/注文。"""
    allsz = []
    for _p, ls in pages.items():
        allsz.extend(l.get('size') or 0 for l in ls if l.get('size'))
    body_size = _median(allsz, 0.0)
    for p, tr in trees.items():
        # 先把"参考资料"从正文叶子中切出来
        tr['content'] = _split_ref_leaf(tr['content'])
        _label_node(tr['content'], p, page_h, body_size)
        # 根节点没有 role，子节点有
    return body_size


def _shift_lines(lines, dy):
    out = []
    for l in lines:
        l2 = dict(l)
        l2['y0'] = l['y0'] + dy
        l2['y1'] = l['y1'] + dy
        spans = []
        for s in (l.get('spans') or []):
            s2 = dict(s)
            s2['y0'] = s.get('y0', 0) + dy
            s2['y1'] = s.get('y1', 0) + dy
            spans.append(s2)
        if spans:
            l2['spans'] = spans
        out.append(l2)
    return out


def _linearize(leaves_):
    """把同一页的多个栏叶子按阅读顺序接起来：右栏顺延到左栏之后。"""
    out = []
    cursor = None
    for leaf in leaves_:
        ls = sorted(leaf.get('lines') or [], key=lambda l: (l['y0'], l['x0']))
        if not ls:
            continue
        hs = [l['y1'] - l['y0'] for l in ls if l['y1'] > l['y0']]
        lh = _median(hs, 60.0)
        y0 = min(l['y0'] for l in ls)
        dy = 0.0 if cursor is None else cursor + lh * 0.5 - y0
        if dy:
            ls = _shift_lines(ls, dy)
        out.extend(ls)
        cursor = max(l['y1'] for l in ls)
    return out


def _blocks_from_lines(lines, page, kind='body'):
    from . import layout
    if not lines:
        return []
    bs = layout.build_blocks({page: lines})
    out = []
    for b in bs:
        if b[0] == 'page':
            continue
        if kind == 'note':
            b = ('note',) + b[1:]
        out.append(b)
    return out


def _line_font(line):
    """取一行里占字数最多的字体名（文字层才有，扫描 OCR 为空）。"""
    cnt = {}
    for s in (line.get('spans') or []):
        f = (s.get('font') or '').strip()
        if f:
            cnt[f] = cnt.get(f, 0) + len(s.get('text') or '')
    return max(cnt, key=cnt.get) if cnt else ''


def _split_heading_chunks(lines):
    """把"一、…"这类一级标题的物理行单独切出来。

    标题和后面的正文常被 PDF 文字层并成一段（字号相同），直接线性化
    会出现"……推广体 / 会思政课……"这种把标题末字甩到下一行的情况。
    文字层里标题字体与正文不同，借此在字体切换处断开；扫描件没有字体
    信息就不动。
    """
    chunks, cur = [], []
    i = 0
    while i < len(lines):
        l = lines[i]
        t = (l.get('text') or '').strip()
        if re.match(u'^[一二三四五六七八九十]+、', t) and len(t) <= 30:
            # 标题前的正文先收口，标题自己另起一块。
            if cur:
                chunks.append(cur)
                cur = []
            cur.append(l)
            # PDF 文字层常把"一、"和标题正文拆成同一基线上的两三个行
            # 片段，先把它们并进同一标题块。
            j = i + 1
            while j < len(lines) and \
                    abs(lines[j]['y0'] - l['y0']) <= 8:
                cur.append(lines[j])
                j += 1
            f1 = _line_font(l)
            f2 = _line_font(lines[j]) if j < len(lines) else ''
            if f1 and f2 and f1 != f2:
                chunks.append(cur)
                cur = []
            i = j
            continue
        cur.append(l)
        i += 1
    if cur:
        chunks.append(cur)
    return chunks


def _col_table_payload(leaves_):
    """把左右两栏分别变成块列表，交给 export 用无框两列表格排。"""
    left, right = [], []
    for leaf in leaves_:
        x0 = (leaf.get('bbox') or (0, 0, 0, 0))[0]
        (left if x0 < 1600 else right).append(leaf)
    def pack(leaves):
        out = []
        lines = _linearize(leaves)
        for chunk in _split_heading_chunks(lines):
            head_t = (chunk[0].get('text') or '').strip() if chunk else ''
            is_head = (bool(re.match(u'^[一二三四五六七八九十]+、', head_t))
                       and len(head_t) <= 30)
            for b in _blocks_from_lines(chunk, 1):
                if b[1]:
                    out.append(['subhead' if is_head else b[0], b[1]])
        return out
    return {'left': pack(left), 'right': pack(right)}


HEAD_PAT = re.compile(u'^(?:\\d+[.．、]|[一二三四五六七八九十]+、)\\s*')
SENT_END = re.compile(u'[。！？.!?；;]$')


def paper_blocks(trees, pages=None, columns=2):
    """按分区标签拼论文块：前置通栏 → 正文双栏 → 本文参考文献 → 他文丢弃。"""
    out = []
    tails = {}
    for p in sorted(trees):
        tr = trees[p]
        ls = leaves(tr['content'])
        front, body, refs = [], [], []
        for leaf in ls:
            role = leaf.get('role') or u'正文'
            if role in (u'他文续文', u'页眉'):
                continue
            if role in (u'题名', u'摘要/关键词', u'元数据'):
                front.append(leaf)
            elif role in (u'参考文献', u'注文'):
                refs.append(leaf)
            else:
                body.append(leaf)
        out.append(('page', str(p), None, None))
        if p == 1 and front:
            # 期刊首页的刊眉/栏目框在分区时被当家具剔除，但它在版面上占
            # 了约 60–70pt。把题名原来的顶端位置传给导出，补回页首留白，
            # 否则首页正文整体上移，页尾会空出一大块。
            tl = next((x for x in front if x.get('role') == u'题名'), None)
            if tl and tl.get('bbox'):
                out.append(('topspace', str(tl['bbox'][1]), [], None, 0))
        if front:
            out.extend(_blocks_from_lines(_linearize(front), p))
        if body:
            if p == 1 and columns and columns > 1 and len(body) >= 2:
                # 第 1 页下半页：用无框两列表格，避免"连续分节+分栏"
                # 把右栏抬到摘要区、页尾留白。页 2 起再另起双栏节。
                payload = _col_table_payload(body)
                out.append(('coltable', json.dumps(payload, ensure_ascii=False),
                            [], None, 0))
                rb = payload.get('right') or []
                tails[p] = rb[-1][1] if rb else ''
            else:
                bs = _blocks_from_lines(_linearize(body), p)
                out.extend(bs)
                if bs:
                    tails[p] = bs[-1][1] or ''
        if refs:
            out.extend(_blocks_from_lines(_linearize(refs), p, kind='note'))
        if p == 1 and columns and columns > 1 and len(trees) > 1:
            out.append(('cols2page', '', [], None, 0))

    # 按页拆段，处理"标题被页边界切成两半"：
    # 上一页末块是"3. …重点突"，下一页首块是"破。党和政府…"，
    # 把标题移整到下一页，破。并回标题，正文从"党和政府"起。
    segs, cur = [], None
    for b in out:
        if b[0] == 'page':
            cur = [b]
            segs.append(cur)
        elif cur is not None:
            cur.append(b)
    for i in range(len(segs) - 1):
        a, b = segs[i], segs[i + 1]
        last = a[-1] if a else None
        if last is None or last[0] not in ('body', 'head', 'h1', 'h2', 'h3', 'h4'):
            continue
        lt = (last[1] or '').strip()
        if not HEAD_PAT.match(lt) or SENT_END.search(lt):
            continue
        first = b[1] if len(b) > 1 else None
        if first is None or first[0] not in ('body', 'head'):
            continue
        ft = (first[1] or '').lstrip(u'　 ')
        m = re.match(u'^([^\\s。！？，、；：]{1,2})。', ft)
        if not m:
            continue
        frag = m.group(1) + u'。'
        new_last = list(last)
        new_last[0] = 'subhead'
        new_last[1] = lt + frag
        a.pop()
        rest = ft[m.end():].lstrip()
        if rest:
            fb = list(first)
            fb[1] = rest
            b[1] = fb
        else:
            b.pop(1)
        b.insert(1, tuple(new_last))

    # 页首续段：上一页末尾不是句末标点，下一页首块是正文 → 不缩进
    for i in range(1, len(segs)):
        prev_tail = tails.get(int(segs[i - 1][0][1]), '')
        if not prev_tail or SENT_END.search(prev_tail):
            continue
        seg = segs[i]
        for j in range(1, len(seg)):
            b = seg[j]
            if b[0] in ('cols2page', 'cols2', 'coltable'):
                continue
            if b[0] in ('body', 'head'):
                t = b[1] or ''
                if not t.startswith(u'') and not HEAD_PAT.match(t.strip()):
                    nb = list(b)
                    nb[1] = u'' + t
                    seg[j] = tuple(nb)
            break
    # 上面的跨页处理改的是拆页后的 segs，必须重新拼回扁平块序列。
    out = [b for seg in segs for b in seg]
    return out


def _line_frame(leaves_):
    """把一个栏里的叶子压成"视觉行"框：一行一个文字串，带原书 x/y。

    这里不再合并成段落——1:1 版面需要保留 PDF 的每一行；同一基线上的
    多个 span 先按 x 合并，避免"一、"和标题文字被拆成两行。
    """
    raw = []
    for leaf in leaves_:
        raw.extend(leaf.get('lines') or [])
    if not raw:
        return None
    rows, _lh = _rows(raw)
    lines = []
    for y0, y1, arr in rows:
        arr = sorted(arr, key=lambda l: l['x0'])
        text = ''
        prev_x1 = None
        for l in arr:
            t = l.get('text') or ''
            if not t:
                continue
            if prev_x1 is not None and l['x0'] - prev_x1 > 45 and text \
                    and not text.endswith(u' ') and not t.startswith(u' '):
                # 同一视觉行里跨栏片段/不同字段之间留一个空格，正文
                # 被 PDF 拆开的片段通常没有这么大间隙，不会误加。
                if re.search(u'[0-9A-Za-z]$', text) or \
                        re.match(u'[0-9A-Za-z]', t):
                    text += u' '
            text += t
            prev_x1 = l['x1']
        if not text:
            continue
        szs = [s.get('size') or 0 for l in arr
               for s in (l.get('spans') or []) if s.get('size')]
        lines.append({'text': text, 'x0': min(l['x0'] for l in arr),
                      'x1': max(l['x1'] for l in arr),
                      'y0': y0, 'y1': y1,
                      'size': _median(szs, 0.0)})
    if not lines:
        return None
    xs0 = sorted(l['x0'] for l in lines)
    xs1 = sorted(l['x1'] for l in lines)
    ys = sorted(l['y0'] for l in lines)
    diffs = [ys[i + 1] - ys[i] for i in range(len(ys) - 1)]
    pitch = _median([d for d in diffs if d > 20], 95.0)
    # 栏左缘取中位数，避开首行缩进；栏右缘取最大值，避免把整栏
    # 宽度估窄（行末标点/字宽差异会让中位数少几个磅）。
    return {'x0': _median(xs0), 'x1': max(xs1),
            'y0': ys[0], 'y1': max(l['y1'] for l in lines),
            'pitch': pitch, 'lines': lines}


def _merge_page_heading(frames):
    """跨页标题：上页末行"3. …重点突" + 下页首行"破。" → 合成下页一行。"""
    for i in range(len(frames) - 1):
        a, b = frames[i], frames[i + 1]
        fa = a.get('right') if a.get('right') and a['right'].get('lines') \
            else a.get('left')
        fb = b.get('left') if b.get('left') and b['left'].get('lines') \
            else b.get('right')
        if not fa or not fb or not fa.get('lines') or not fb.get('lines'):
            continue
        last, first = fa['lines'][-1], fb['lines'][0]
        lt = (last.get('text') or '').strip()
        if not HEAD_PAT.match(lt) or SENT_END.search(lt):
            continue
        ft = (first.get('text') or '').lstrip(u'　 ')
        m = re.match(u'^([^\\s。！？，、；：]{1,2})。', ft)
        if not m:
            continue
        first['text'] = lt + m.group(1) + u'。'
        # 原书标题通常缩进两字；上页半截行不带缩进，这里补回。
        first['x0'] = first.get('x0', fb.get('x0', 0)) + 120
        first['x1'] = max(first.get('x1', 0), fb.get('x1', 0))
        first['full'] = True
        fa['lines'].pop()
        if fa.get('lines'):
            fa['y1'] = max(l['y1'] for l in fa['lines'])
    return frames


def paper_line_blocks(trees, pages=None, columns=2):
    """论文 1:1 版面：先保留大框（栏/带），再在大框里保留每个视觉行。

    返回 page / vspace / linepage 块。linepage 的 payload 是左右两栏的
    视觉行，导出时每页一个无框三列表格，单元格里一行一个段落。
    """
    out = []
    frames = []
    for p in sorted(trees):
        tr = trees[p]
        ls = leaves(tr['content'])
        def _tiny_top(x):
            bb = x.get('bbox') or (0, 0, 0, 0)
            return bb[3] < 900 and len(_norm(_text_of(x))) <= 2

        def _split_leaves(leaves):
            """优先用真实中缝分栏；没有中缝才退回页面中线近似。"""
            body_lines = [l for x in leaves for l in (x.get('lines') or [])
                          if 400 < l['y0'] < 4600]
            g = _find_gutter(body_lines, 3400.0)
            if g is not None:
                lo, hi = g[0], g[1]
                ll = [x for x in leaves
                      if (x.get('bbox') or (0, 0, 0, 0))[3] > 0 and
                      (x.get('bbox') or (0, 0, 0, 0))[1] >= 0 and
                      (x.get('bbox') or (0, 0, 0, 0))[2] <= lo + 10]
                rr = [x for x in leaves
                      if (x.get('bbox') or (0, 0, 0, 0))[0] >= hi - 10]
                # 跨中缝的叶子按中心归到较近一栏
                used = set(map(id, ll + rr))
                for x in leaves:
                    if id(x) in used:
                        continue
                    bb = x.get('bbox') or (0, 0, 0, 0)
                    cx = (bb[0] + bb[2]) / 2.0
                    (ll if cx < (lo + hi) / 2.0 else rr).append(x)
                return ll, rr
            return ([x for x in leaves
                     if (x.get('bbox') or (0, 0, 0, 0))[0] < 1600],
                    [x for x in leaves
                     if (x.get('bbox') or (0, 0, 0, 0))[0] >= 1600])

        keep = [x for x in ls
                if (x.get('role') or u'正文') not in (u'他文续文', u'页眉')
                and not _tiny_top(x)]
        out.append(('page', str(p), None, None))
        # 不能按整篇文档预设单/双栏：同一篇论文可能封面单栏、正文双栏、
        # 参考文献又变单栏。逐页看文字框是否有真正的右栏。
        left_leaves, right_leaves = _split_leaves(keep)
        two = (bool(left_leaves) and bool(right_leaves)) if pages is not None \
            else columns > 1
        if not two:
            # 单栏页：整页文字是一个大框，里面按视觉行排；不能再套
            # 双栏三列表格，否则正文会被压进左半页。
            frame = _line_frame(keep)
            rec = {'left': frame, 'right': None, 'single': True}
            frames.append(rec)
            out.append(('linepage',
                        json.dumps({'page': p, 'cols': 1, 'left': frame},
                                   ensure_ascii=False), [], None, 0))
            continue
        front, body = [], []
        for leaf in keep:
            if (leaf.get('role') or u'正文') in (u'题名', u'摘要/关键词',
                                                u'元数据'):
                front.append(leaf)
            else:
                body.append(leaf)
        left_leaves, right_leaves = _split_leaves(body)
        frame = {'left': _line_frame(left_leaves),
                 'right': _line_frame(right_leaves),
                 'front': front}
        frames.append(frame)
        if p == 1 and front:
            # 首页刊眉被剔除后留下的页首空白：用空段落补，而不是改
            # 节的上边距（否则第 2 页也会被推下去）。
            tl = next((x for x in front if x.get('role') == u'题名'), None)
            if tl and tl.get('bbox'):
                gap = tl['bbox'][1] / 5000.0 * 813.0 - 32.4
                if gap > 4:
                    out.append(('vspace', str(round(gap, 1)), [], None, 0))
            out.extend(_blocks_from_lines(_linearize(front), p))
        payload = {'page': p,
                   'left': frame['left'], 'right': frame['right']}
        out.append(('linepage', json.dumps(payload, ensure_ascii=False),
                    [], None, 0))
    _merge_page_heading(frames)
    # 标题合并在 frames 上完成后，重新生成 linepage payload。
    idx = 0
    for b in out:
        if b[0] == 'linepage':
            p = int(json.loads(b[1])['page'])
            fr = frames[p - 1]
            payload = {'page': p,
                       'left': fr['left'], 'right': fr.get('right')}
            if fr.get('single'):
                payload['cols'] = 1
            out[idx] = ('linepage', json.dumps(payload, ensure_ascii=False),
                        [], None, 0)
        idx += 1
    return out


def _tree_node_payload(node, seen=None):
    """把分区树变成导出层可消费的树：band / columns / leaf。"""
    if seen is None:
        seen = set()
    role = node.get('role')
    if role in (u'他文续文', u'页眉'):
        return None
    if 'children' not in node:
        bb = node.get('bbox') or (0, 0, 0, 0)
        # 页眉被拆成单个"期""年"等短字时，语义标签可能判成正文；
        # 这种页顶极短叶子不能占正文的一行。
        if bb[3] < 900 and len(_norm(_text_of(node))) <= 2:
            return None
    children = node.get('children')
    if children:
        cs = []
        for c in children:
            x = _tree_node_payload(c, seen)
            if x is not None:
                cs.append(x)
        if not cs:
            return None
        return {'type': ('columns' if node.get('level') == 'columns'
                         else 'band'),
                'bbox': list(node.get('bbox') or (0, 0, 0, 0)),
                'children': cs}
    frame = _line_frame([node])
    if not frame or not frame.get('lines'):
        return None
    uniq = []
    for ln in frame.get('lines') or []:
        k = (round(ln.get('y0', 0), 1), round(ln.get('y1', 0), 1),
             round(ln.get('x0', 0), 1), round(ln.get('x1', 0), 1),
             ln.get('text') or '')
        if k in seen:
            continue
        seen.add(k)
        uniq.append(ln)
    if not uniq:
        return None
    bb = [frame.get('x0', 0), frame.get('y0', 0),
          frame.get('x1', 0), frame.get('y1', 0)]
    return {'type': 'leaf', 'role': role, 'bbox': bb,
            'x0': frame.get('x0'), 'x1': frame.get('x1'),
            'y0': frame.get('y0'), 'y1': frame.get('y1'),
            'lines': uniq}


def _first_columns(node, best=None):
    if node.get('level') == 'columns':
        b = node.get('bbox') or (0, 0, 0, 0)
        if b[3] - b[1] > 300:
            return node
    for c in node.get('children') or []:
        r = _first_columns(c, best)
        if r is not None:
            return r
    return None


def _leaf_payloads(leaves_, seen):
    out = []
    for lf in sorted(leaves_, key=lambda x: ((x.get('bbox') or (0, 0, 0, 0))[1],
                                             (x.get('bbox') or (0, 0, 0, 0))[0])):
        p = _tree_node_payload(lf, seen)
        if p is not None:
            out.append(p)
    return out


def _frame_leaf(leaves_):
    """把一个栏里的所有叶子压成一条视觉行叶子，避免栏内再堆小表/空段。"""
    f = _line_frame(leaves_)
    if not f or not f.get('lines'):
        return None
    return {'type': 'leaf', 'role': None,
            'bbox': [f.get('x0', 0), f.get('y0', 0),
                     f.get('x1', 0), f.get('y1', 0)],
            'x0': f.get('x0'), 'x1': f.get('x1'),
            'y0': f.get('y0'), 'y1': f.get('y1'),
            'lines': f.get('lines')}


def _frame_lines(lines_):
    if not lines_:
        return None
    node = {'level': 'leaf', 'lines': list(lines_),
            'bbox': _bbox(lines_)}
    return _frame_leaf([node])


def _segment_node(leaves_, page_w=3400.0):
    """一段没有被表格/插图打断的文字：单栏就压成一条行叶，双栏就分栏。"""
    leaves_ = [x for x in leaves_ if x.get('lines')]
    if not leaves_:
        return None
    all_lines = [l for x in leaves_ for l in (x.get('lines') or [])]
    if not all_lines:
        return None
    g = _find_gutter(all_lines, page_w)
    if g is not None:
        lo, hi, left, right, cross, w = g
        lls = [l for l in all_lines if l['x1'] <= lo]
        rls = [l for l in all_lines if l['x0'] >= hi]
        cross_ls = [l for l in all_lines
                    if l['x1'] > lo and l['x0'] < hi]
        if len(lls) >= 3 and len(rls) >= 3 and \
                not _hanging_refs(lls, rls) and \
                len(cross_ls) <= max(3, int(len(all_lines) * 0.25)):
            mid = (lo + hi) / 2.0
            for l in cross_ls:
                (lls if (l['x0'] + l['x1']) / 2.0 < mid else rls).append(l)
            lf = _frame_lines(lls)
            rf = _frame_lines(rls)
            if lf and rf:
                lb = lf.get('bbox') or (0, 0, 0, 0)
                rb = rf.get('bbox') or (0, 0, 0, 0)
                return {
                    'type': 'columns',
                    'bbox': [lb[0], min(lb[1], rb[1]),
                             rb[2], max(lb[3], rb[3])],
                    'children': [
                        {'type': 'band', 'bbox': list(lb), 'children': [lf]},
                        {'type': 'band', 'bbox': list(rb), 'children': [rf]},
                    ]}
    return _frame_leaf(leaves_)


def _leaf_hits_obj(leaf, objs):
    b = leaf.get('bbox') or (0, 0, 0, 0)
    lw = max(1.0, b[2] - b[0])
    lh = max(1.0, b[3] - b[1])
    for o in objs:
        ob = o.get('bbox') or (0, 0, 0, 0)
        iw = min(b[2], ob[2]) - max(b[0], ob[0])
        ih = min(b[3], ob[3]) - max(b[1], ob[1])
        if iw > 0 and ih > 0 and (iw * ih) / (lw * lh) > 0.35:
            return True
    return False


def _object_node(obj):
    bb = [float(x) for x in (obj.get('bbox') or (0, 0, 0, 0))]
    kind = obj.get('kind') or 'figure'
    if kind == 'table':
        return {'type': 'object', 'kind': 'table', 'bbox': bb,
                'grid': obj.get('grid') or {},
                'size': obj.get('size') or 0}
    return {'type': 'object', 'kind': 'figure', 'bbox': bb,
            'path': obj.get('path') or ''}


def _paper_object_flow(root, all_leaves, objs, page_w=3400.0):
    """页面被通栏表格/插图切成若干段：段内照常分栏，对象按原位插入。"""
    leaves_ = [x for x in all_leaves if not _leaf_hits_obj(x, objs)]
    items = []
    for x in leaves_:
        b = x.get('bbox') or (0, 0, 0, 0)
        items.append((b[1], 0, x))
    for o in objs:
        b = o.get('bbox') or (0, 0, 0, 0)
        items.append((b[1], 1, o))
    items.sort(key=lambda z: (z[0], z[1]))
    children, buf = [], []
    for _y, kind, item in items:
        if kind == 1:
            sn = _segment_node(buf, page_w)
            if sn is not None:
                children.append(sn)
            buf = []
            children.append(_object_node(item))
        else:
            buf.append(item)
    sn = _segment_node(buf, page_w)
    if sn is not None:
        children.append(sn)
    if not children:
        return None
    return {'type': 'band',
            'bbox': list(root.get('bbox') or (0, 0, 0, 5000)),
            'children': children}


def paper_unified_blocks(trees, pages=None, columns=2, objects=None):
    """统一版式树：有跨页栏的页归一成 顶部通栏 + 底部左右栏。

    这样第 6 页“左栏结束、右栏参考文献继续到页底”不会再被
    横带重复切开；仍由同一个 frametree 渲染器输出。
    objects: {页码: [{kind:'table'|'figure', bbox, grid/path, size}]}
    """
    objects = objects or {}
    out = []
    for p in sorted(trees):
        tr = trees[p]
        root = tr.get('content')
        if root is None:
            out.append(('page', str(p), None, None))
            continue
        all_leaves = [x for x in leaves(root)
                      if (x.get('role') or u'正文') not in (u'他文续文', u'页眉')]
        # 先过滤页顶单字页眉，再找覆盖大半页的真栏节点。
        all_leaves = [x for x in all_leaves
                      if not ((x.get('bbox') or (0, 0, 0, 0))[3] < 900 and
                              len(_norm(_text_of(x))) <= 2)]
        objs = objects.get(p) or []
        if objs:
            payload = _paper_object_flow(root, all_leaves, objs)
            out.append(('page', str(p), None, None))
            out.append(('frametree',
                        json.dumps({'page': p, 'node': payload},
                                   ensure_ascii=False), [], None, 0))
            continue
        colnode = _first_columns(root)
        seen = set()
        if colnode is not None:
            cb = colnode.get('bbox') or (0, 0, 0, 0)
            start_y = cb[1]
            ev = colnode.get('evidence') or {}
            gut = ev.get('gutter')
            if not gut:
                body_lines = [l for x in all_leaves
                              for l in (x.get('lines') or [])]
                g = _find_gutter(body_lines, 3400.0)
                gut = (g[0], g[1]) if g else (1600.0, 1700.0)
            mid = (gut[0] + gut[1]) / 2.0
            top = [x for x in all_leaves
                   if (x.get('bbox') or (0, 0, 0, 0))[1] < start_y - 20]
            body = [x for x in all_leaves
                    if (x.get('bbox') or (0, 0, 0, 0))[1] >= start_y - 20]
            # 跨中缝的叶子（页脚作者简介、通栏小结等）不能塞进左栏，
            # 否则左栏 bbox 会撑到整页，栏宽和表格宽度全错。
            full = [x for x in body
                    if (x.get('bbox') or (0, 0, 0, 0))[0] < gut[0] and
                    (x.get('bbox') or (0, 0, 0, 0))[2] > gut[1]]
            body_cols = [x for x in body if x not in full]
            left = [x for x in body_cols
                    if ((x.get('bbox') or (0, 0, 0, 0))[0] +
                        (x.get('bbox') or (0, 0, 0, 0))[2]) / 2.0 < mid]
            right = [x for x in body_cols if x not in left]
            tch = _leaf_payloads(top, seen)
            _ll = _frame_leaf(left)
            _rr = _frame_leaf(right)
            lch = [_ll] if _ll is not None else []
            rch = [_rr] if _rr is not None else []
            bch = _leaf_payloads(full, seen)
            ch = []
            if tch:
                ch.append({'type': 'band',
                           'bbox': list(_bbox_leaves(top)),
                           'children': tch})
            if lch and rch:
                ch.append({'type': 'columns',
                           'bbox': list(_bbox_leaves(body_cols)),
                           'children': [
                               {'type': 'band',
                                'bbox': list(_bbox_leaves(left)),
                                'children': lch},
                               {'type': 'band',
                                'bbox': list(_bbox_leaves(right)),
                                'children': rch}]})
            elif lch or rch:
                ch.append({'type': 'band',
                           'bbox': list(_bbox_leaves(body_cols)),
                           'children': lch or rch})
            if bch:
                ch.append({'type': 'band',
                           'bbox': list(_bbox_leaves(full)),
                           'children': bch})
            payload = {'type': 'band',
                       'bbox': list(root.get('bbox') or (0, 0, 0, 5000)),
                       'children': ch} if ch else None
        else:
            payload = _tree_node_payload(root, seen)
        out.append(('page', str(p), None, None))
        out.append(('frametree',
                    json.dumps({'page': p, 'node': payload},
                               ensure_ascii=False), [], None, 0))
    return out


def paper_tree_blocks(trees, pages=None, columns=2):
    """论文 1:1 的通用版面树：不按整页单/双栏，按 band/columns 递归。"""
    out = []
    for p in sorted(trees):
        out.append(('page', str(p), None, None))
        node = _tree_node_payload(trees[p]['content'])
        out.append(('frametree',
                    json.dumps({'page': p, 'node': node},
                               ensure_ascii=False), [], None, 0))
    return out


def format_tree(tree, max_preview=48):
    """把一棵页树写成便于人看的缩进文本。"""
    lines = []

    def walk(node, prefix=''):
        b = node.get('bbox')
        ev = node.get('evidence') or {}
        info = '%s%s n=%d bbox=(%.0f,%.0f,%.0f,%.0f)' % (
            prefix, node['level'], node.get('n', len(node.get('lines') or [])),
            b[0], b[1], b[2], b[3]) if b else prefix + node['level']
        if node.get('role'):
            info += '  【%s】' % node['role']
        if ev.get('cut') == 'v':
            info += '  gutter=%s w=%.0f left=%d right=%d cross=%d' % (
                ev.get('gutter'), ev.get('width', 0), ev.get('left', 0),
                ev.get('right', 0), ev.get('cross', 0))
        elif ev.get('cut') == 'h':
            info += '  bands=%d' % ev.get('bands', 0)
        lines.append(info)
        for c in node.get('children') or []:
            walk(c, prefix + '  ')
        if 'children' not in node:
            t = _text_preview(node.get('lines') or [], 2)
            if t:
                lines.append(prefix + '  | ' + t[:max_preview])
            szs = [l.get('size') or 0 for l in (node.get('lines') or [])
                   if l.get('size')]
            if szs:
                lines.append(prefix + '  | size≈%.1f lines=%d' % (
                    _median(szs), len(node.get('lines') or [])))

    if tree.get('furniture'):
        f = tree['furniture']
        lines.append('furniture n=%d | %s' % (f['n'], f['preview'][:max_preview]))
    walk(tree['content'])
    return u'\n'.join(lines)
