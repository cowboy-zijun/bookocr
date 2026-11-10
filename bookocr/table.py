# -*- coding: utf-8 -*-
"""通用表格识别 —— 先归纳统一特征，再只对候选区做精细处理。

表格的统一特征（有无框线都成立）：
  1) 连续多行（>=3）共享同一组列结构（列起点水平对齐）；
  2) 行内被"大间隙"切成 >=2 个单元格；
  3) 数据区内有规律的数字（数字/百分比/年份/编号），或首行是文字表头；
  4) 上下文常带表题（表1 / Table 1）或表头行。

两段式流程（避免整页硬算、也避免对正文做无谓识别）：
  第一段（廉价扫描，全页）：只找"连续数据行"——每行至少两个
      含数字的单元格，且连续出现 >=3 行。正文段落几乎不会满足；
      图表坐标轴、目录页码也只有单列数字，不满足。
  第二段（仅候选区）：核对列对齐、向上吸收表头行、查表题，
      合格才还原成矩阵；框线只是辅助线索，不作为判定条件。
"""
import os
import re

_DEBUG = bool(os.environ.get('BOOKOCR_TDBG'))


def _dbg(msg):
    if _DEBUG:
        print('[tbl] ' + msg)


NUM_CELL = re.compile(
    u'^[\\s\\d.,;:%()（）\\[\\]【】\\-–—−+±*＊<>≤≥/=~～、，．。]+$')
CAPTION = re.compile(u'^(表|附表|续表|Table|TABLE|Supplementary\\s+Table)'
                     u'\\s*[0-9一二三四五六七八九十]')
DOT_ONLY = re.compile(u'^[.．·…\\s]+$')


def _clusters(xs, tol):
    """一维聚类：相邻间隔 <= tol 的归为一簇，返回 [(中心, 成员), ...]"""
    if not xs:
        return []
    xs = sorted(xs)
    groups = [[xs[0]]]
    for x in xs[1:]:
        if x - groups[-1][-1] <= tol:
            groups[-1].append(x)
        else:
            groups.append([x])
    return [(sum(g) / len(g), g) for g in groups]


def _norm_len(t):
    return len(re.sub(u'[^\\u3400-\\u9fffA-Za-z0-9]', '', t))


def _is_num(t):
    """单元格是否"有规律数字"：含数字且数字占比够高（允许附带短词）。"""
    t = t.strip()
    if not t:
        return False
    d = sum(1 for ch in t if ch.isdigit())
    if d == 0:
        return False
    return d / float(len(t)) >= 0.3 or NUM_CELL.match(t) is not None


def _page_rows(lines, page_w, body_size):
    """把一页切成"视觉行 + 行内单元格"，并标注每行是否有引导点段。"""
    if not body_size:
        sizes = sorted(l.get('size') or 0 for l in lines if l.get('size'))
        body_size = sizes[len(sizes) // 2] if sizes else 0.0
    # 脚注区（字号/位置/注号/分隔线综合判定）：脚注不参与表格识别。
    # 整页小字的附表页没有正常正文 → 不会被判成脚注，照常识别。
    from . import layout as _layout
    fn_y = _layout.footnote_start(lines, body_size)

    frags = []
    for l in lines:
        if l.get('sep'):
            continue
        if fn_y is not None and l['y0'] >= fn_y:
            continue
        spans = l.get('spans')
        if spans:
            for s in spans:
                t = s.get('text', '').strip()
                if t:
                    frags.append({'y': (s['y0'] + s['y1']) / 2.0,
                                  'y0': s['y0'], 'y1': s['y1'],
                                  'x0': s['x0'], 'x1': s['x1'], 't': t})
        else:
            # 扫描件走 OCR，没有文字层的 spans；把这一行本身当作一个“单元格”。
            # 同一视觉行上的几个 OCR 框（各占一格）会被下面的聚类合成一行，
            # 否则整页永远只有一列，表格检测对扫描件等于失效。
            t = (l.get('text') or '').strip()
            if t:
                frags.append({'y': (l['y0'] + l['y1']) / 2.0,
                              'y0': l['y0'], 'y1': l['y1'],
                              'x0': l['x0'], 'x1': l['x1'], 't': t})
    if len(frags) < 6:
        return [], 0.0, 0.0
    heights = sorted(f['y1'] - f['y0'] for f in frags)
    line_h = heights[len(heights) // 2] or 60.0
    yrows = _clusters([f['y'] for f in frags], line_h * 0.5)
    cw = sorted((f['x1'] - f['x0']) / max(1, _norm_len(f['t']))
                for f in frags if _norm_len(f['t']) >= 1)
    char_w = cw[len(cw) // 2] if cw else 60.0
    # 单元格边界：约 1.35 字宽的间隙。取 2 会把相邻两个数字单元格
    # 粘在一起（真实案例："45.06%19.57%"），取更小则表头词间空格
    # 会被切开
    gap_thr = char_w * 1.35

    rows = []                      # [(y_center, [(text, x0), ...], has_dots)]
    for cy, ys in yrows:
        lo, hi = min(ys) - line_h * 0.5, max(ys) + line_h * 0.5
        sp = sorted((f for f in frags if lo <= f['y'] <= hi),
                    key=lambda f: f['x0'])
        if not sp:
            continue
        segs = [[sp[0]]]
        for prev, cur in zip(sp, sp[1:]):
            if cur['x0'] - prev['x1'] > gap_thr:
                segs.append([cur])
            else:
                segs[-1].append(cur)
        has_dots = any(DOT_ONLY.match(''.join(x['t'] for x in sg))
                       for sg in segs)
        segs = [sg for sg in segs
                if not DOT_ONLY.match(''.join(x['t'] for x in sg))]
        if not segs:
            continue
        # 双栏页的左右栏行：段间距超过页宽 35% → 不是表格行
        bad = False
        for a, b in zip(segs, segs[1:]):
            if b[0]['x0'] - a[-1]['x1'] > page_w * 0.35:
                bad = True
                break
        if bad:
            continue
        rows.append((cy, [(''.join(x['t'] for x in sg), sg[0]['x0'])
                          for sg in segs], has_dots))
    return rows, line_h, char_w


def _pdf_segments(page, page_w=3400.0, page_h=5000.0):
    """把 PDF 里的框线收集成横/竖线段（标准坐标）。

    学术 PDF 的表格几乎都是用矢量细线画的：先量出这些线，再按线围成的
    格子放文字，比"猜列对齐"准得多。扫描件没有矢量线，这里返回空，
    由下面的文字版检测接手。
    """
    rect = page.rect
    if not rect.width or not rect.height:
        return [], []
    sx = page_w / rect.width
    sy = page_h / rect.height
    vs, hs = [], []
    for d in page.get_drawings():
        for it in d.get('items') or []:
            kind = it[0]
            if kind == 're':
                r = it[1]
                w = r.width * sx
                h = r.height * sy
                if w <= 18 and h >= 40:
                    vs.append(((r.x0 + r.x1) / 2.0 * sx,
                               r.y0 * sy, r.y1 * sy))
                elif h <= 18 and w >= 40:
                    hs.append(((r.y0 + r.y1) / 2.0 * sy,
                               r.x0 * sx, r.x1 * sx))
            elif kind == 'l':
                p1, p2 = it[1], it[2]
                x0, y0 = p1.x * sx, p1.y * sy
                x1, y1 = p2.x * sx, p2.y * sy
                if abs(x1 - x0) <= 18 and abs(y1 - y0) >= 40:
                    vs.append(((x0 + x1) / 2.0, min(y0, y1), max(y0, y1)))
                elif abs(y1 - y0) <= 18 and abs(x1 - x0) >= 40:
                    hs.append(((y0 + y1) / 2.0, min(x0, x1), max(x0, x1)))
    return vs, hs


def _idx(bounds, v):
    """v 落在 bounds 的哪一格（0 基）；落在边界外返回 None。"""
    if not bounds or v < bounds[0] - 8 or v > bounds[-1] + 8:
        return None
    for i in range(len(bounds) - 1):
        if v <= bounds[i + 1]:
            return i
    return len(bounds) - 2


def _is_cjk(ch):
    return u'㐀' <= ch <= u'鿿'


def _cell_text(frags, line_h):
    """单元格内保留原书断行：一行一行放回去，不做语义上的段落合并。

    表格里的每一项往往就是一行，行距和正文折行一样，靠"空行"分不出
    项与项；硬合并会把"institutional plasticity; strong informal
    networks"粘成一句。原书怎么断行，Word 里就怎么断，版面才对得上。
    """
    if not frags:
        return ''
    frags = sorted(frags, key=lambda f: (f[1], f[0]))
    lines, cur, ly = [], [], None
    for f in frags:
        if ly is not None and f[1] - ly > line_h * 0.45:
            lines.append(cur)
            cur = []
        cur.append(f)
        ly = f[1]
    if cur:
        lines.append(cur)
    out = []
    for line in lines:
        line = sorted(line, key=lambda f: f[0])
        s, prev_x1 = '', None
        for f in line:
            t = (f[4] or '').strip()
            if not t:
                continue
            if s and prev_x1 is not None and f[0] - prev_x1 > 8 \
                    and not (_is_cjk(s[-1]) and _is_cjk(t[0])):
                s += ' '
            s += t
            prev_x1 = f[2]
        if s:
            out.append(s)
    return u'\n'.join(out)


_OCR_ENGINE = None


def _ocr_cell(page, box, dpi=220):
    """把一个单元格区域重新 OCR 一遍（只给扫描表格用）。

    整页 OCR 常把相邻两格读成一行；按格子切开单独认，能保证
    每一格的文字不串列。表格页数不多，多花这点时间值得。
    """
    global _OCR_ENGINE
    try:
        import numpy as np
        import pymupdf
        from rapidocr_onnxruntime import RapidOCR
    except Exception:
        return ''
    if _OCR_ENGINE is None:
        _OCR_ENGINE = RapidOCR(intra_op_num_threads=2, use_cls=False,
                               rec_batch_num=16)
    rect = page.rect
    rx = rect.width / 3400.0
    ry = rect.height / 5000.0
    x0, y0, x1, y1 = box
    clip = pymupdf.Rect(max(0.0, x0 * rx + 1.5), max(0.0, y0 * ry + 1.5),
                        min(rect.width, x1 * rx - 1.5),
                        min(rect.height, y1 * ry - 1.5))
    if clip.width <= 1 or clip.height <= 1:
        return ''
    try:
        pix = page.get_pixmap(clip=clip, dpi=dpi)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)
        if pix.n == 4:
            img = img[:, :, :3]
        res, _ = _OCR_ENGINE(img)
    except Exception:
        return ''
    lines = []
    for item in res or []:
        try:
            box_, text, score = item[0], item[1], item[2]
        except Exception:
            continue
        if float(score) < 0.3:
            continue
        t = (text or '').strip()
        if t:
            lines.append((min(p[1] for p in box_),
                          min(p[0] for p in box_), t))
    lines.sort()
    return u'\n'.join(t for _y, _x, t in lines)


def _split_ocr_fragment(f, xs):
    """OCR 有时把相邻几格读成一整行（中间用 | 或空格隔开）。

    按这一行与各列的重叠宽度把文字分回各列，尽量在空格处下刀。
    文字层的 span 不会走这里（一格一个 span，本来就是准的）。
    """
    x0, y0, x1, y1, t = f[:5]
    t = (t or '').replace('|', ' ')
    if not t.strip():
        return [f]
    spans = []
    for i in range(len(xs) - 1):
        lo, hi = max(x0, xs[i]), min(x1, xs[i + 1])
        if hi - lo > 0:
            spans.append((i, hi - lo))
    if len(spans) <= 1:
        return [f]
    total = sum(w for _i, w in spans)
    if total <= 0 or len(t) < len(spans) * 2:
        return [f]
    out, pos, acc = [], 0, 0.0
    for k, (ci, w) in enumerate(spans):
        acc += w
        if k == len(spans) - 1:
            end = len(t)
        else:
            target = int(round(len(t) * acc / total))
            target = min(max(target, pos + 1), len(t) - 1)
            cands = [c for c in range(max(pos + 1, target - 12),
                                      min(len(t) - 1, target + 12) + 1)
                     if t[c - 1].isspace() or t[c].isspace()]
            end = min(cands, key=lambda c: abs(c - target)) if cands else target
        piece = t[pos:end].strip()
        if piece:
            out.append((xs[ci] + 1, y0, xs[ci + 1] - 1, y1, piece, True))
        pos = end
    return out or [f]


def _line_components(vs, hs, tol=12.0):
    """把相交的横竖线并成一个个"线团"。

    一页上可能同时有页眉装饰框、页码小花边和正文里的真表格；
    如果把全页的线放在一起取外框，几个相距很远的小框会围出一个
    整页大的假表格（期刊页就是这样被误判的）。先按相交关系分组，
    每组单独还原，装饰框面积太小会被后面的面积门槛自然淘汰。
    """
    nv, nh = len(vs), len(hs)
    parent = list(range(nv + nh))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, v in enumerate(vs):
        for j, h in enumerate(hs):
            if h[1] - tol <= v[0] <= h[2] + tol \
                    and v[1] - tol <= h[0] <= v[2] + tol:
                union(i, nv + j)
    comps = {}
    for i in range(nv + nh):
        comps.setdefault(find(i), []).append(i)
    out = []
    for ids in comps.values():
        cvs = [vs[i] for i in ids if i < nv]
        chs = [hs[i - nv] for i in ids if i >= nv]
        out.append((cvs, chs))
    return out


def _tables_from_segments(vs, hs, lines, page_w=3400.0, page_h=5000.0,
                          tol=12.0, page=None, cell_ocr=False):
    """按"线团"逐组还原表格；一页可以有多张表。"""
    out = []
    for cvs, chs in _line_components(vs, hs, tol):
        if len(cvs) < 2 or len(chs) < 2:
            continue
        out.extend(_one_table_from_segments(
            cvs, chs, lines, page_w, page_h, tol, page, cell_ocr))
    return out


def _one_table_from_segments(vs, hs, lines, page_w=3400.0, page_h=5000.0,
                             tol=12.0, page=None, cell_ocr=False):
    """用一组相交的横/竖线还原一张表格（带合并单元格）。

    vs: [(x, y0, y1)]，hs: [(y, x0, x1)]，都是标准坐标。
    """
    if len(vs) < 2 or len(hs) < 2:
        return []
    # 用一套线去筛另一套线，去掉页眉横线、页面边框等无关线条
    hx0 = min(h[1] for h in hs)
    hx1 = max(h[2] for h in hs)
    vs = [v for v in vs if hx0 - 30 <= v[0] <= hx1 + 30]
    if len(vs) < 2:
        return []
    vy0 = min(v[1] for v in vs)
    vy1 = max(v[2] for v in vs)
    # 竖线常在横线内侧留 1—2pt 的缝（上边框比竖线高一点点），
    # 容差太小会把表格最上面一条横线滤掉，表头行就丢了
    hs = [h for h in hs if vy0 - 30 <= h[0] <= vy1 + 30]
    if len(hs) < 2:
        return []
    xs = [c for c, _m in _clusters([v[0] for v in vs], tol)]
    ys = [c for c, _m in _clusters([h[0] for h in hs], tol)]
    _dbg('grid xs=%d ys=%d  yvals=%s' %
         (len(xs), len(ys), [round(h[0], 1) for h in hs]))
    if len(xs) < 3 or len(ys) < 3:
        return []
    bw = xs[-1] - xs[0]
    bh = ys[-1] - ys[0]
    if bw < page_w * 0.15 or bh < page_h * 0.04:
        return []
    if (len(xs) - 1) * (len(ys) - 1) < 4:
        return []

    # 单元格文字：文字层用 span（一格一个），扫描件用 OCR 行框
    frags = []
    for l in lines:
        spans = l.get('spans')
        if spans:
            for s in spans:
                t = (s.get('text') or '').strip()
                if t:
                    frags.append((s['x0'], s['y0'], s['x1'], s['y1'],
                                  s.get('text') or '', False))
        else:
            t = (l.get('text') or '').strip()
            if t:
                frags.append((l['x0'], l['y0'], l['x1'], l['y1'], t, True))
    # 扫描件的 OCR 行可能跨几格：按列重叠拆回各格
    frags = [g for f in frags
             for g in (_split_ocr_fragment(f, xs) if f[5] else [f])]
    inside = [f for f in frags
              if xs[0] - 10 <= (f[0] + f[2]) / 2.0 <= xs[-1] + 10
              and ys[0] - 10 <= (f[1] + f[3]) / 2.0 <= ys[-1] + 10]
    _dbg('vec inside=%d frags=%d' % (len(inside), len(frags)))
    if len(inside) < 4:
        return []
    heights = sorted(f[3] - f[1] for f in inside if f[3] > f[1])
    line_h = heights[len(heights) // 2] if heights else 60.0

    ncol, nrow = len(xs) - 1, len(ys) - 1
    cell = [[[] for _ in range(ncol)] for _ in range(nrow)]
    for f in inside:
        ci = _idx(xs, (f[0] + f[2]) / 2.0)
        ri = _idx(ys, (f[1] + f[3]) / 2.0)
        if ci is not None and ri is not None:
            cell[ri][ci].append(f)

    def has_rule(y, lo, hi):
        pad = (hi - lo) * 0.10
        for hy, hx0_, hx1_ in hs:
            if abs(hy - y) <= 12 and hx0_ <= lo + pad and hx1_ >= hi - pad:
                return True
        return False

    # 某一列在两条横线之间没有横线 → 上下两格是合并单元格
    groups_by_col, merges = [], []
    for ci in range(ncol):
        groups, start = [], 0
        for j in range(1, nrow):
            if has_rule(ys[j], xs[ci], xs[ci + 1]):
                groups.append((start, j - 1))
                start = j
        groups.append((start, nrow - 1))
        groups_by_col.append(groups)
        for a, b in groups:
            if b > a:
                merges.append([a, ci, b - a + 1, 1])

    grid = [['' for _ in range(ncol)] for _ in range(nrow)]
    for ci, groups in enumerate(groups_by_col):
        for a, b in groups:
            fs = [f for rr in range(a, b + 1) for f in cell[rr][ci]]
            txt = _cell_text(fs, line_h)
            if txt:
                grid[a][ci] = txt
    # 扫描件：整页 OCR 常把相邻几格读成一行，按格子再认一遍
    if cell_ocr and page is not None:
        for ci, groups in enumerate(groups_by_col):
            for a, b in groups:
                if not grid[a][ci]:
                    continue
                txt = _ocr_cell(page, (xs[ci], ys[a], xs[ci + 1],
                                       ys[b + 1]))
                if txt:
                    grid[a][ci] = txt
    _dbg('vec grid filled=%d' % sum(1 for r in grid for c in r if c))
    if not any(any(r) for r in grid):
        return []
    # 图表坐标轴也可能围出格子：至少要有 3 个非空格、且分布在 2 列以上，
    # 才当表格；纯坐标轴/装饰框没有这样的文字分布
    _filled = [(ri, ci) for ri, r in enumerate(grid)
               for ci, c in enumerate(r) if c]
    if len(_filled) < 3 or len({ci for _ri, ci in _filled}) < 2:
        return []
    # 页面结构门：整页大的"网格"通常不是表格，而是页框/页眉装饰框
    # 和正文被线框切出来的假格子；单元格里塞进整栏正文的也直接否掉。
    _ncell = ncol * nrow
    _max_chars = max((len(c) for r in grid for c in r), default=0)
    _max_lines = max((c.count(u'\n') + 1 for r in grid for c in r if c),
                     default=0)
    if bw >= page_w * 0.90 and bh >= page_h * 0.85 and len(_filled) < 20:
        return []
    if _max_chars > 1200 or _max_lines > 30:
        return []
    if _ncell >= 12 and len(_filled) / float(_ncell) < 0.35:
        return []

    # 去掉全空的装饰行（表头与表体之间的留白行），合并单元格跨过的行保留
    covered = set()
    for a, ci, rs, cs in merges:
        covered.update(range(a + 1, a + rs))
    keep = [r for r in range(nrow) if any(grid[r]) or r in covered]
    if not keep:
        return []
    remap = {r: i for i, r in enumerate(keep)}
    if len(keep) != nrow:
        grid = [grid[r] for r in keep]
        new_m = []
        for a, ci, rs, cs in merges:
            if a not in remap:
                continue
            b = a + rs - 1
            if b not in remap:
                continue
            new_m.append([remap[a], ci, remap[b] - remap[a] + 1, cs])
        merges = new_m
    widths = [xs[i + 1] - xs[i] for i in range(ncol)]
    sizes = []
    for l in lines:
        for s in (l.get('spans') or []):
            t = (s.get('text') or '').strip()
            if not t:
                continue
            cx = (s['x0'] + s['x1']) / 2.0
            cy = (s['y0'] + s['y1']) / 2.0
            if xs[0] <= cx <= xs[-1] and ys[0] <= cy <= ys[-1] \
                    and s.get('size'):
                sizes.append(float(s['size']))
    return [(ys[0], ys[-1], {
        'rows': grid, 'merges': merges, 'widths': widths,
        'bbox': [xs[0], ys[0], xs[-1], ys[-1]],
        'size': _median(sizes, 0.0)})]


def _median(vals, default=0.0):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return default
    return vals[len(vals) // 2]


def _region_frags(lines, x0, y0, x1, y1, pad=45.0):
    """取区域内的文字片段：(x0,y0,x1,y1,text,is_ocr,size)。"""
    out = []
    for l in lines:
        spans = l.get('spans')
        if spans:
            for s in spans:
                t = (s.get('text') or '').strip()
                if not t or re.match(u'^[—–\\-－_·.．]{2,}$', t):
                    continue
                cx = (s['x0'] + s['x1']) / 2.0
                cy = (s['y0'] + s['y1']) / 2.0
                if x0 - pad <= cx <= x1 + pad and y0 - pad <= cy <= y1 + pad:
                    out.append((s['x0'], s['y0'], s['x1'], s['y1'],
                                s.get('text') or '', False,
                                s.get('size') or 0))
        else:
            t = (l.get('text') or '').strip()
            if not t or re.match(u'^[—–\\-－_·.．]{2,}$', t):
                continue
            cx = (l['x0'] + l['x1']) / 2.0
            cy = (l['y0'] + l['y1']) / 2.0
            if x0 - pad <= cx <= x1 + pad and y0 - pad <= cy <= y1 + pad:
                out.append((l['x0'], l['y0'], l['x1'], l['y1'],
                            t, True, l.get('size') or 0))
    return out


def _cell_alignment(frags, x0, x1):
    """一格文字的对齐：左对齐（引文）/居中（表头、短标签）。

    一格里的文字可能被 PDF 拆成多个 span（引文角标 (DN8) 是单独
    span），必须先按视觉行合并，否则会把同一行当成两行，误判成居中。
    """
    if len(frags) <= 1 or x1 <= x0:
        return 'center'
    hs = [f[3] - f[1] for f in frags if f[3] > f[1]]
    line_h = _median(hs, 60.0)
    lines, cur, ly = [], [], None
    for f in sorted(frags, key=lambda z: (z[1], z[0])):
        if ly is not None and f[1] - ly > line_h * 0.45:
            lines.append(cur)
            cur = []
        cur.append(f)
        ly = f[1]
    if cur:
        lines.append(cur)
    if len(lines) <= 1:
        return 'center'
    lefts = [min(f[0] for f in ln) for ln in lines]
    rights = [max(f[2] for f in ln) for ln in lines]
    w = float(x1 - x0)
    if max(lefts) - min(lefts) <= max(6.0, w * 0.06) and \
            max(rights) - min(rights) >= w * 0.12:
        return 'left'
    return 'center'


def _column_bounds(hs, frags, page_w, align_tol=18.0):
    """无竖线的三线表：从横线端点 + 文字横向空隙推列边界。"""
    if not hs:
        return []
    lo = min(h[1] for h in hs)
    hi = max(h[2] for h in hs)
    if frags:
        lo = min(lo, min(f[0] for f in frags))
        hi = max(hi, max(f[2] for f in frags))
    if hi - lo < page_w * 0.12:
        return []
    ivs = sorted((f[0], f[2]) for f in frags)
    merged = []
    for a, b in ivs:
        if merged and a <= merged[-1][1] + 0.1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    char_w = _median([(f[2] - f[0]) / max(1, _norm_len(f[4]))
                      for f in frags if _norm_len(f[4]) >= 1], 36.0)
    gaps = []
    for a, b in zip(merged, merged[1:]):
        if b[0] - a[1] >= max(13.0, char_w * 0.75):
            gaps.append((a[1] + b[0]) / 2.0)
    # 规则线端点优先（它是真实分栏位置），文字空隙只补规则线没覆盖的列
    cand = [(x, 1) for x in
            [v for h in hs for v in (h[1], h[2])]] + \
           [(x, 0) for x in gaps]
    cand.sort()
    groups = []
    for x, pri in cand:
        if groups and x - groups[-1][-1][0] <= align_tol:
            groups[-1].append((x, pri))
        else:
            groups.append([(x, pri)])
    bounds = []
    for g in groups:
        rules = [x for x, pri in g if pri]
        gaps_ = [x for x, pri in g if not pri]
        bounds.append(rules[0] if rules else gaps_[0])
    bounds = sorted(set([lo] + [x for x in bounds if lo + 6 < x < hi - 6] +
                        [hi]))
    min_w = max(28.0, char_w * 0.85)
    out = [bounds[0]]
    for x in bounds[1:-1]:
        if x - out[-1] >= min_w:
            out.append(x)
    if hi - out[-1] < min_w:
        out[-1] = hi
    else:
        out.append(hi)
    return out


def _has_rule(hs, y, lo, hi, tol=12.0, pad=15.0):
    for hy, hx0, hx1 in hs:
        if abs(hy - y) <= tol and hx0 <= lo + pad and hx1 >= hi - pad:
            return True
    return False


def _label_anchored_rows(frags, bounds, ys, line_h, ncol):
    """三线表：横线只有顶线/表头线/底线时，用首列标签推断数据行。

    绝大多数三线表（回归结果、指标对比）首列是一行一个标签；横线只画
    表头，数据行之间没有线。旧逻辑把整张表当成 2 行（表头 + 一个大
    数据行），所有单元格塞进一个格子用换行堆叠，列一多就错位。
    这里用首列的 y 聚类当行锚点，把同一水平行上的各列文字归到一行。
    首列是纵向合并单元格（附录变量定义那种）时锚点数会明显少于数据
    列，返回 None，交给原来的规则/截图回退。
    """
    if len(ys) != 3:
        return None
    header_bottom = ys[1]
    body = [f for f in frags
            if (f[1] + f[3]) / 2.0 > header_bottom - line_h * 0.4]
    if len(body) < 6:
        return None
    counts = []
    for ci in range(ncol):
        counts.append(sum(1 for f in body
                          if _idx(bounds, (f[0] + f[2]) / 2.0) == ci))
    # 行锚点不要用首列：首列常把“场景/指标”写成两三行，会把一行拆成
    # 几行；挑“有字且行数最少”的那一列当锚点（指标列/变量列通常一行
    # 一个数据行）。首列是纵向合并单元格时，最少列会明显少于其它列，
    # 下面的比例闸门会拒掉。
    cand = [(cnt, ci) for ci, cnt in enumerate(counts) if cnt >= 3]
    if not cand:
        return None
    anchor_col = min(cand)[1]
    cols = [f for f in body
            if _idx(bounds, (f[0] + f[2]) / 2.0) == anchor_col]
    cols.sort(key=lambda f: (f[1] + f[3]) / 2.0)
    groups = []
    for f in cols:
        cy = (f[1] + f[3]) / 2.0
        if groups and cy - groups[-1][1] <= line_h * 0.7:
            groups[-1][0].append(f)
            groups[-1][1] = cy
        else:
            groups.append([[f], cy])
    anchors = [g[1] for g in groups]
    if len(anchors) < 3:
        return None
    # 合并单元格/多行定义表：最多列的行数远多于锚点时不硬拆。
    if max(counts) > max(6, len(anchors) * 2.8):
        return None
    nrow = len(anchors) + 1
    cells = [[[] for _ in range(ncol)] for _ in range(nrow)]
    for f in frags:
        cy = (f[1] + f[3]) / 2.0
        if cy > header_bottom - line_h * 0.4:
            continue
        ci = _idx(bounds, (f[0] + f[2]) / 2.0)
        if ci is not None:
            cells[0][ci].append(f)
    for f in body:
        ci = _idx(bounds, (f[0] + f[2]) / 2.0)
        if ci is None:
            continue
        cy = (f[1] + f[3]) / 2.0
        # 归到“它上方最近的行锚点”，而不是几何最近点：回归表的第二行
        # 常是系数下面的 t 值，它和下一行标签几乎等距，按最近点会串到
        # 下一行。行锚点是标签行，t 值总在标签之下、下一标签之上。
        k = None
        for _i, _a in enumerate(anchors):
            if cy >= _a - line_h * 0.5:
                k = _i
            else:
                break
        if k is None or cy - anchors[k] > line_h * 3.2:
            continue
        cells[k + 1][ci].append(f)
    grid = [['' for _ in range(ncol)] for _ in range(nrow)]
    align = [['center' for _ in range(ncol)] for _ in range(nrow)]
    for ri in range(nrow):
        for ci in range(ncol):
            fs = cells[ri][ci]
            if not fs:
                continue
            grid[ri][ci] = _cell_text(
                [(f[0], f[1], f[2], f[3], f[4], False) for f in fs],
                line_h)
            align[ri][ci] = _cell_alignment(fs, bounds[ci], bounds[ci + 1])
    # 去掉整列空白的边界列（横线常比文字宽，会在最左/最右多出一列）
    keep = [ci for ci in range(ncol)
            if any(grid[ri][ci] for ri in range(nrow))]
    if len(keep) < 2:
        return None
    grid = [[row[ci] for ci in keep] for row in grid]
    align = [[row[ci] for ci in keep] for row in align]
    widths = [bounds[ci + 1] - bounds[ci] for ci in keep]
    filled = [(ri, ci) for ri, row in enumerate(grid)
              for ci, c in enumerate(row) if c]
    if len(filled) < 4 or len({ci for _ri, ci in filled}) < 2:
        return None
    sizes = [f[6] for f in frags if f[6]]
    size = _median(sizes, 0.0)
    return (ys[0], ys[-1], {
        'rows': grid, 'merges': [], 'widths': widths,
        'align': align, 'size': size,
        'bbox': [bounds[keep[0]], ys[0], bounds[keep[-1] + 1], ys[-1]],
    })


def _build_ruled_table(hs, lines, page_w=3400.0, page_h=5000.0,
                      region=None):
    """用横线 + 文字还原三线表（可带纵向合并单元格）。

    hs: [(y, x0, x1)]，只包含属于这张表的横线。region: (x0,y0,x1,y1)。
    返回 (y0, y1, data) 或 None。
    """
    if len(hs) < 2:
        return None
    hs = sorted(hs)
    # 只保留足够长的横线：正文下划线、图例短线不参与表格行
    table_w0 = max(h[2] - h[1] for h in hs)
    hs = [h for h in hs
          if h[2] - h[1] >= max(120.0, table_w0 * 0.10)]
    if len(hs) < 2:
        return None
    y0 = min(h[0] for h in hs)
    y1 = max(h[0] for h in hs)
    if y1 - y0 < page_h * 0.025:
        return None
    rx0 = min(h[1] for h in hs)
    rx1 = max(h[2] for h in hs)
    if region:
        rx0 = max(rx0, region[0] - 5)
        rx1 = min(rx1, region[2] + 5)
    frags = _region_frags(lines, rx0, y0, rx1, y1)
    if len(frags) < 4:
        return None
    # 行边界：横线 y 聚类
    ys = [c for c, _g in _clusters([h[0] for h in hs], 9.0)]
    if len(ys) < 3:
        return None
    # 列边界：用横线端点 + 文字横向空隙
    bounds = _column_bounds(hs, frags, page_w)
    if len(bounds) < 3:
        return None
    ncol, nrow = len(bounds) - 1, len(ys) - 1
    if ncol < 2 or nrow < 2:
        return None
    line_h = _median([f[3] - f[1] for f in frags if f[3] > f[1]], 60.0)
    # 三线表（只有表头一条内线）：用首列标签补数据行，别把整张表压成 2 行。
    _logical = _label_anchored_rows(frags, bounds, ys, line_h, ncol)
    if _logical:
        return _logical
    # 文字片段落到格子里
    cell = [[[] for _ in range(ncol)] for _ in range(nrow)]
    for f in frags:
        cx = (f[0] + f[2]) / 2.0
        cy = (f[1] + f[3]) / 2.0
        ci = _idx(bounds, cx)
        ri = _idx(ys, cy)
        if ci is None or ri is None:
            continue
        cell[ri][ci].append(f[:6])
    line_h = _median([f[3] - f[1] for f in frags if f[3] > f[1]], 60.0)
    # 每一列按"本列有没有横线"分组合并；没横线 → 上下是同一格
    groups_by_col, merges = [], []
    for ci in range(ncol):
        groups, start = [], 0
        for j in range(1, nrow):
            if _has_rule(hs, ys[j], bounds[ci], bounds[ci + 1]):
                groups.append((start, j - 1))
                start = j
        groups.append((start, nrow - 1))
        groups_by_col.append(groups)
        for a, b in groups:
            if b > a:
                merges.append([a, ci, b - a + 1, 1])
    grid = [['' for _ in range(ncol)] for _ in range(nrow)]
    align = [['center' for _ in range(ncol)] for _ in range(nrow)]
    for ci, groups in enumerate(groups_by_col):
        for a, b in groups:
            fs = [f for rr in range(a, b + 1) for f in cell[rr][ci]]
            if not fs:
                continue
            grid[a][ci] = _cell_text(
                [(f[0], f[1], f[2], f[3], f[4], False) for f in fs],
                line_h)
            align[a][ci] = _cell_alignment(fs, bounds[ci], bounds[ci + 1])
    filled = [(ri, ci) for ri, row in enumerate(grid)
              for ci, c in enumerate(row) if c]
    if len(filled) < 4 or len({ci for _ri, ci in filled}) < 2:
        return None
    if len(filled) / float(ncol * nrow) < 0.12:
        return None
    sizes = [f[6] for f in frags if f[6]]
    size = _median(sizes, 0.0)
    widths = [bounds[i + 1] - bounds[i] for i in range(ncol)]
    return (ys[0], ys[-1], {
        'rows': grid, 'merges': merges, 'widths': widths,
        'align': align, 'size': size,
        'bbox': [bounds[0], ys[0], bounds[-1], ys[-1]],
    })


def _hs_in_region(hs, region, page_w, page_h):
    if not region:
        return list(hs)
    rx0, ry0, rx1, ry1 = region
    out = []
    for h in hs:
        y, x0, x1 = h
        if ry0 - 110 <= y <= ry1 + 110:
            ov = min(x1, rx1 + 20) - max(x0, rx0 - 20)
            # 合并列的行分隔线可能只横跨一列（460pt），不能按整表宽度
            # 要求重叠；只要不是页眉装饰短线就保留。
            if ov >= 100.0:
                out.append(h)
    return out


def _layout_region_bbox(page, lines, reg, page_w, page_h):
    """版面模型只给 y 范围；用横线/文字补出表格的左右边界。"""
    y0, y1 = float(reg[0]), float(reg[1])
    _vs, hs = _pdf_segments(page, page_w, page_h)
    iny = [h for h in hs if y0 - 80 <= h[0] <= y1 + 80]
    if iny:
        x0 = min(h[1] for h in iny)
        x1 = max(h[2] for h in iny)
    else:
        ls = [l for l in lines
              if y0 - 30 <= (l['y0'] + l['y1']) / 2.0 <= y1 + 30]
        if not ls:
            return (0.0, y0, page_w, y1)
        x0 = min(l['x0'] for l in ls)
        x1 = max(l['x1'] for l in ls)
    ls = [l for l in lines
          if y0 - 30 <= (l['y0'] + l['y1']) / 2.0 <= y1 + 30]
    if ls:
        x0 = min(x0, min(l['x0'] for l in ls))
        x1 = max(x1, max(l['x1'] for l in ls))
    return (x0 - 8.0, y0 - 8.0, x1 + 8.0, y1 + 8.0)


def table_from_region(page, lines, region=None, page_w=3400.0,
                      page_h=5000.0):
    """按版面模型给的表格区域还原一张表；无区域时用横线自动聚类。"""
    _vs, hs = _pdf_segments(page, page_w, page_h)
    hs = _hs_in_region(hs, region, page_w, page_h)
    return _build_ruled_table(hs, lines, page_w, page_h, region)


def _rule_groups(hs, page_w, page_h):
    """把横线按纵向邻近 + 横向重叠分组；一组对应一张表。

    分组只用"足够长"的横线当骨架，但每组要把组内较短的合并列分隔线
    也带进来（否则纵向合并会被拆错）。
    """
    all_hs = sorted(hs)
    seeds = sorted([h for h in hs
                    if h[2] - h[1] >= max(200.0, page_w * 0.14)
                    and h[0] >= page_h * 0.12])
    groups, cur = [], []
    for h in seeds:
        if not cur:
            cur = [h]
            continue
        prev = cur[-1]
        gap = h[0] - prev[0]
        ov = min(h[2], prev[2]) - max(h[1], prev[1])
        if gap <= page_h * 0.10 and ov >= 0.55 * min(
                h[2] - h[1], prev[2] - prev[1]):
            cur.append(h)
        else:
            if len(cur) >= 3:
                groups.append(cur)
            cur = [h]
    if len(cur) >= 3:
        groups.append(cur)
    out = []
    for grp in groups:
        gy0, gy1 = grp[0][0], grp[-1][0]
        gx0, gx1 = min(h[1] for h in grp), max(h[2] for h in grp)
        full = []
        for h in all_hs:
            if not (gy0 - 12 <= h[0] <= gy1 + 12):
                continue
            ov = min(h[2], gx1) - max(h[1], gx0)
            if ov >= max(100.0, (gx1 - gx0) * 0.08):
                full.append(h)
        out.append(full)
    return out


def rule_tables(page, lines, page_w=3400.0, page_h=5000.0,
                exclude=None):
    """无版面模型时的三线表检测：横线成组 → 还原成矩阵。"""
    _vs, hs = _pdf_segments(page, page_w, page_h)
    out = []
    for grp in _rule_groups(hs, page_w, page_h):
        gy0, gy1 = grp[0][0], grp[-1][0]
        if exclude:
            skip = False
            for r in exclude:
                # 兼容两种输入：版面模型 (y0,y1,label) 和带 bbox 的
                # (x0,y0,x1,y1,label)。只排除 figure，不能把 text/header
                # 区里的真表格一起排掉。
                if len(r) >= 5:
                    _rx0, ry0, _rx1, ry1, label = r[:5]
                elif len(r) >= 4:
                    _rx0, ry0, _rx1, ry1 = r[:4]
                    label = 'figure'
                elif len(r) >= 3:
                    ry0, ry1, label = r[0], r[1], r[2]
                else:
                    continue
                if label not in ('figure', 'tableimg'):
                    continue
                if ry0 - 20 <= gy0 and gy1 <= ry1 + 20:
                    skip = True
                    break
            if skip:
                continue
        t = _build_ruled_table(grp, lines, page_w, page_h)
        if t:
            out.append(t)
    return out


def vector_tables(page, lines, page_w=3400.0, page_h=5000.0):
    """从 PDF 矢量框线还原表格（文字层 PDF 的表格）。"""
    vs, hs = _pdf_segments(page, page_w, page_h)
    return _tables_from_segments(vs, hs, lines, page_w, page_h)


def _image_segments(page, page_w=3400.0, page_h=5000.0, dpi=200):
    """扫描页：从页面图像里量横/竖框线（印刷表格的框线）。

    只在文字层没有矢量线的页上调用，而且调用前先用 OCR 行判断
    "这页像不像表格"，避免每页都渲染一遍图片。
    """
    try:
        import cv2
        import numpy as np
    except Exception:
        return [], []
    try:
        pix = page.get_pixmap(dpi=dpi)
    except Exception:
        return [], []
    h, w = pix.height, pix.width
    if not h or not w:
        return [], []
    if pix.n == 1:
        gray = np.frombuffer(pix.samples, dtype=np.uint8).reshape(h, w)
    else:
        buf = np.frombuffer(pix.samples, dtype=np.uint8).reshape(h, w, pix.n)
        if pix.n == 4:
            gray = cv2.cvtColor(buf, cv2.COLOR_RGBA2GRAY)
        else:
            gray = cv2.cvtColor(buf, cv2.COLOR_RGB2GRAY)
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                               cv2.THRESH_BINARY_INV, 15, 10)
    hk = cv2.getStructuringElement(cv2.MORPH_RECT,
                                   (max(30, int(w * 0.04)), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT,
                                   (1, max(30, int(h * 0.04))))
    hor = cv2.morphologyEx(bw, cv2.MORPH_OPEN, hk)
    ver = cv2.morphologyEx(bw, cv2.MORPH_OPEN, vk)
    sx = page_w / float(w)
    sy = page_h / float(h)

    def comps(img, horizontal):
        n, _lab, stats, _cent = cv2.connectedComponentsWithStats(img, 8)
        out = []
        for i in range(1, n):
            x, y, ww, hh, _area = stats[i]
            if horizontal:
                if ww >= w * 0.04 and hh <= max(4, h * 0.006):
                    out.append(((y + hh / 2.0) * sy,
                                x * sx, (x + ww) * sx))
            else:
                if hh >= h * 0.04 and ww <= max(4, w * 0.006):
                    out.append(((x + ww / 2.0) * sx,
                                y * sy, (y + hh) * sy))
        return out

    return comps(ver, False), comps(hor, True)


def image_tables(page, lines, page_w=3400.0, page_h=5000.0):
    vs, hs = _image_segments(page, page_w, page_h)
    return _tables_from_segments(vs, hs, lines, page_w, page_h, tol=16.0,
                                 page=page, cell_ocr=True)


def _looks_tabular(lines, page_w=3400.0, body_size=None):
    """廉价预判：页面上有没有"被切成多格"的行，或有表题。"""
    for l in lines:
        if CAPTION.match((l.get('text') or '').strip()):
            return True
    rows, _h, _cw = _page_rows(lines, page_w, body_size)
    multi = sum(1 for _cy, segs, _d in rows if len(segs) >= 2)
    return multi >= 3


def page_tables_page(page, lines, body_size=None,
                     page_w=3400.0, page_h=5000.0, log=None,
                     regions=None, text_fallback=True):
    """一页表格检测：矢量框线 → 版面模型区域/三线表 → 图像框线 →
    文字对齐，逐级退让。"""
    tabs = vector_tables(page, lines, page_w=page_w, page_h=page_h)
    if tabs and regions:
        # 模型已判定为 figure 的区域，即使里面有矩形框线也不是表格
        figs = [r for r in regions if len(r) >= 3 and r[2] == 'figure']
        if figs:
            tabs = [t for t in tabs
                    if not any(r[0] - 30 <= t[0] and t[1] <= r[1] + 30
                               for r in figs)]
    if tabs:
        return tabs
    # 版面模型已经指出表格区：只在这些区域里按横线还原。三线表没有
    # 竖线，旧的行列聚类会把合并单元格拆错，必须用横线定行、文字空隙定列。
    has_table_region = False
    if regions:
        out = []
        for reg in regions:
            if len(reg) < 3 or reg[2] != 'table':
                continue
            has_table_region = True
            bb = _layout_region_bbox(page, lines, reg, page_w, page_h)
            t = table_from_region(page, lines, region=bb,
                                  page_w=page_w, page_h=page_h)
            if t:
                out.append(t)
        if out:
            out.sort(key=lambda x: x[0])
            return out
        if has_table_region:
            # 版面模型说是表格，但横线不足（纯文字表）。交给文字对齐，
            # 不再走无区域的横线聚类，避免把图框当表。
            t = page_tables(lines, page_w=page_w, body_size=body_size,
                            page_h=page_h, log=log)
            if t:
                return t
            return []
    # 没有版面模型/没有表格区：横线成组也能可靠找回三线表。
    # 只排除模型明确标成 figure 的区域；text/header 区不能排除，
    # 否则正文里的真表格会被跳过。
    fig_ex = []
    for r in (regions or []):
        if len(r) >= 3 and r[2] == 'figure':
            fig_ex.append((0.0, r[0], page_w, r[1], 'figure'))
    tabs = rule_tables(page, lines, page_w=page_w, page_h=page_h,
                       exclude=fig_ex)
    if tabs:
        return tabs
    # 扫描件没有矢量线：只有"看起来像表格"的页才渲染图像量框线
    if _looks_tabular(lines, page_w, body_size):
        tabs = image_tables(page, lines, page_w=page_w, page_h=page_h)
        if tabs:
            return tabs
    if not text_fallback:
        return []
    return page_tables(lines, page_w=page_w, body_size=body_size,
                       page_h=page_h, log=log)


def page_figures(page, lines, regions=None, page_w=3400.0, page_h=5000.0):
    """找整幅插图区域：版面模型 figure 区 + 页内大位图。

    返回 [(x0,y0,x1,y1,label)]（标准坐标）。矢量图/位图都按区域截页，
    比单独抠内嵌位图更完整（坐标轴、标注、图例都在）。
    """
    rect = page.rect
    if not rect.width or not rect.height:
        return []
    sx = page_w / rect.width
    sy = page_h / rect.height
    infos = []
    try:
        for info in page.get_image_info(xrefs=True):
            b = info.get('bbox') or (0, 0, 0, 0)
            infos.append((b[0] * sx, b[1] * sy, b[2] * sx, b[3] * sy))
    except Exception:
        pass
    drawings = []
    try:
        for d in page.get_drawings():
            r = d.get('rect')
            if r is None:
                continue
            drawings.append((r.x0 * sx, r.y0 * sy, r.x1 * sx, r.y1 * sy))
    except Exception:
        pass
    out = []

    def add(bb, label='figure'):
        x0, y0, x1, y1 = bb
        if x1 - x0 < page_w * 0.10 or y1 - y0 < page_h * 0.025:
            return
        x0, y0 = max(0.0, x0), max(0.0, y0)
        x1, y1 = min(page_w, x1), min(page_h, y1)
        if x1 <= x0 or y1 <= y0:
            return
        for a in out:
            if min(a[2], x1) - max(a[0], x0) > 0.6 * min(
                    a[2] - a[0], x1 - x0):
                return
        out.append((x0, y0, x1, y1, label))

    fig_regions = [r for r in (regions or [])
                   if len(r) >= 3 and r[2] == 'figure']
    for reg in fig_regions:
        ry0, ry1 = reg[0], reg[1]
        # 页顶刊头/徽标不算插图（论文首页的期刊标志常被模型标成 figure）
        if ry1 < 1100:
            continue
        boxes = [b for b in infos
                 if (b[1] + b[3]) / 2.0 >= ry0 - 20 and
                 (b[1] + b[3]) / 2.0 <= ry1 + 20]
        if not boxes:
            boxes = [b for b in drawings
                     if (b[1] + b[3]) / 2.0 >= ry0 - 20 and
                     (b[1] + b[3]) / 2.0 <= ry1 + 20 and
                     (b[2] - b[0]) * (b[3] - b[1]) >= 4000]
        if boxes:
            bb = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                  max(b[2] for b in boxes), max(b[3] for b in boxes))
        else:
            ls = [l for l in lines
                  if ry0 - 30 <= (l['y0'] + l['y1']) / 2.0 <= ry1 + 30]
            if not ls:
                continue
            bb = (min(l['x0'] for l in ls), ry0,
                  max(l['x1'] for l in ls), ry1)
        add(bb)
    # 没有 figure 标签的页：大到足以当插图的位图也收进来（论文里的
    # 模型图、流程图常被扫描成整张位图）。表区里的位图跳过。
    table_regs = [r for r in (regions or [])
                  if len(r) >= 3 and r[2] == 'table']
    for b in infos:
        w, h = b[2] - b[0], b[3] - b[1]
        if w < 250 or h < 180 or w > 3000 or h > 4200:
            continue
        if b[3] < 750 or b[1] > 4650 or b[1] < 1000:
            continue
        cy = (b[1] + b[3]) / 2.0
        if any(ry0 - 20 <= cy <= ry1 + 20 for ry0, ry1, _lb in table_regs):
            continue
        if any(ry0 - 20 <= cy <= ry1 + 20
               for ry0, ry1, _lb in fig_regions):
            continue
        add(b)
    return out


def _grammar_veto(grid, tab_w, y0, y1, page_w, page_h,
                  has_header, has_caption, numeric=False):
    """无框文字表的"语法审查"：找证据证明它其实是正文。

    只做否决，不做正向识别。返回否决理由；通过返回 None。
    """
    cells = [c.strip() for r in grid for c in r if c and c.strip()]
    if not cells:
        return u'没有文字'
    bh = y1 - y0
    # 版式门：铺满版心 / 横跨整页的连续正文
    if bh > page_h * 0.72:
        return u'区域铺满版心'
    if tab_w > page_w * 0.90 and bh > page_h * 0.50:
        return u'横跨整页的连续正文'
    # 句末标点：正文行末常见，表格格内少见；有表题时放宽（问卷表）
    sents = sum(1 for c in cells if re.search(u'[。！？.!?]$', c))
    if not has_caption and sents >= 2 \
            and sents / float(len(cells)) > 0.35:
        return u'格内句末标点多'
    # 跨行接续：上一行末尾是逗号/顿号，或下一行以接续词开头
    pairs = cont = 0
    for a, b in zip(grid, grid[1:]):
        ac = [c.strip() for c in a if c and c.strip()]
        bc = [c.strip() for c in b if c and c.strip()]
        if not ac or not bc:
            continue
        pairs += 1
        prev, nxt = ac[-1], bc[0]
        if prev.endswith((u'，', u'、', u',', u'、')) \
                or re.match(u'^(的|了|着|而|但|因此|所以|然而|同时|此外)',
                            nxt):
            cont += 1
    if pairs and (cont >= 2 or cont / float(pairs) >= 0.30):
        return u'相邻行可以接成一句话'
    # 格子长度：正文格子长；表格格子通常是短语
    lens = sorted(len(c) for c in cells)
    med = lens[len(lens) // 2]
    if not has_caption and med > 80:
        return u'格子文字过长，像正文'
    # 无表题的纯文字表：首行必须是短表头，否则不认
    first = [c.strip() for c in (grid[0] if grid else []) if c.strip()]
    short_head = bool(first) and all(len(c) <= 40 for c in first) \
        and not any(re.search(u'[。！？.!?]$', c) for c in first)
    if not has_caption and not (has_header and short_head) and not numeric:
        return u'没有表题，首行也不像短表头'
    return None


def page_tables(lines, page_w=3400.0, body_size=None,
                page_h=5000.0, log=None):
    """检测一页里的表格。lines：该页行列表（含 spans）。
    返回 [(y0, y1, rows)]，rows 为 [[单元格, ...], ...]（列按 x 排序）。
    """
    if not lines:
        return []
    rows, line_h, char_w = _page_rows(lines, page_w, body_size)
    if len(rows) < 3:
        return []
    align_tol = max(25.0, char_w * 1.2)

    # ---------------------------------------------------------------
    # 第一段：找"连续表格行"候选区。两路证据：
    #   is_data：一行有 >=2 个数字格（统计表）
    #   is_text：一行被大空隙切成 >=2 个文字格（文字性表格，如
    #            对比表/清单表），且不是整句正文
    # ---------------------------------------------------------------
    is_data, is_text = [], []
    for cy, segs, has_dots in rows:
        nums = sum(1 for t, _x in segs if _is_num(t))
        is_data.append(nums >= 2)
        good = [t for t, _x in segs if t.strip()]
        # 文字表格的格子通常是短语，不会以句号/问号收尾；正文行
        # 即使被切成两块，结尾也会带句读。这一条挡掉绝大多数正文。
        is_text.append(len(good) >= 2 and not any(
            re.search(u'[。！？.!?]$', t.strip()) for t in good))
    use = [a or b for a, b in zip(is_data, is_text)]
    # 数据行之间允许夹 1-3 行非数据行（表头/问题文字/分组标签），
    # 例如"企业名（单格行）+ 数据行"交替排列的清单表
    runs, cur = [], []
    for i, flag in enumerate(use):
        if not flag:
            continue
        if cur and i - cur[-1] <= 4:
            cur.append(i)
        else:
            if len(cur) >= 2:
                runs.append(cur)
            cur = [i]
    if len(cur) >= 2:
        runs.append(cur)

    # 允许"表头/问题行"这种非数字行夹在数据行中间：相邻数据段之间
    # 间隔不超过 3 行、纵向距离不超过 4.5 行高 → 合并为同一个表格
    #（真实的问卷表常为"问题文字 + 两行数据"重复排列）
    merged = []
    for r in runs:
        if merged:
            prev = merged[-1]
            ygap = rows[r[0]][0] - rows[prev[-1]][0]
            gap_rows = r[0] - prev[-1] - 1
            if ygap <= line_h * 4.5 and gap_rows <= 3:
                prev.extend(range(prev[-1] + 1, r[0]))
                prev.extend(r)
                continue
        merged.append(list(r))
    runs = [r for r in merged if len(r) >= 3]
    _dbg('rows=%d data=%d text=%d runs=%d' %
         (len(rows), sum(is_data), sum(is_text), len(runs)))
    if not runs:
        return []

    # 表题位置（"表1 …"/"Table 1 …"），用于候选区上下文证据
    caps = []
    for l in lines:
        t = (l.get('text') or '').strip()
        if CAPTION.match(t):
            caps.append(l['y0'])

    # ---------------------------------------------------------------
    # 第二段：仅候选区——列对齐校验 + 吸收表头 + 表题/长度门槛
    # ---------------------------------------------------------------
    out = []
    for run in runs:
        r0, r1 = run[0], run[-1]
        cy0 = rows[r0][0]
        cy1 = rows[r1][0]
        _dbg('cand run %d..%d n=%d' % (r0, r1, len(run)))
        # 目录页特征：候选区内大量"引导点"行 → 不是表格。
        # 引导点可能和页码粘在同一个单元格里（". . . .1"），所以按
        # 单元格里的点字符数量判断，而不只看独立点段
        dot_rows = 0
        for i in run:
            if rows[i][2] or any(
                    sum(1 for ch in t if ch in '.．·…') >= 3
                    for t, _x in rows[i][1]):
                dot_rows += 1
        if dot_rows >= 6:
            continue
        # 数据行的单元格数应稳定；图表标签/图例的行长短参差。
        # 只统计数据行——问题行/表头行本来就只有一格
        counts = [len(rows[i][1]) for i in run if use[i]]
        mode_n = max(set(counts), key=counts.count)
        if counts.count(mode_n) < max(3, len(counts) * 0.5):
            continue
        # 向上吸收表头：先越过紧邻的"问题/说明"单段行，直到遇到
        # 多段表头行为止（问卷表常是"表头 → 问题文字 → 数据行"）
        up = []
        j = r0 - 1
        while j >= 0 and len(up) < 4:
            cy, segs, _hd = rows[j]
            gap_up = rows[j + 1][0] - cy
            # 第一行（可能直接是表头）容差放宽——表头与数据之间常有
            # 较大的行距；之后逐行收紧，只越过紧邻的问题/说明行
            limit = line_h * (4.5 if not up else 1.6)
            if gap_up > limit:
                break
            up.append(j)
            if len(segs) >= 2:
                break
            j -= 1
        region = list(reversed(up)) + list(range(r0, r1 + 1))
        # 表题（"Table 2: …"/"表1 …"）在表格框外，是单格行；别把它
        # 吸进表格当第一行。表格自己的表头是多格的，从这里开始。
        while region and len(rows[region[0]][1]) < 2:
            region.pop(0)
        if len(region) < 3:
            continue
        region_rows = [rows[i] for i in region]

        # 列聚类：只用"数据行"的单元格左边界定列（表头/问题行常居中
        # 或缩进，不能参与定列，否则会多出空列）
        xs = [x0 for i in run if use[i] for _t, x0 in rows[i][1]]
        cols = _clusters(xs, align_tol)
        if len(cols) < 2:
            continue

        row_assign = []
        for _cy, segs, _hd in region_rows:
            assigns = []
            for t, x0 in segs:
                best = min(range(len(cols)),
                           key=lambda ci: abs(x0 - cols[ci][0]))
                # 所有单元格都归到最近的列（问题行/表头即使缩进也不丢），
                # 但"列证据"只统计对齐的单元格（见下）
                assigns.append((best, t,
                                abs(x0 - cols[best][0]) <= align_tol))
            row_assign.append(assigns)
        used = {}
        for assigns in row_assign:
            for ci, _t, ok in assigns:
                if ok:
                    used[ci] = used.get(ci, 0) + 1
        # 列保留按"数据行"数量衡量：问题行/表头行天生只占少数列，
        # 不能用总行数当分母（否则问卷表/企业清单会被整表否决）
        n_ok = sum(1 for i in run if use[i])
        keep = sorted(ci for ci, c in used.items()
                      if c >= max(2, n_ok // 2))
        _dbg('cand keep=%s n_ok=%d' % (keep, n_ok))
        if len(keep) < 2:
            continue
        # 双栏防护：恰好 2 列且列距超过页宽 40% → 中缝伪表格
        if len(keep) == 2:
            gap = cols[keep[1]][0] - cols[keep[0]][0]
            if gap > page_w * 0.4:
                continue

        grid = []
        data_flags = []
        for ri, assigns in enumerate(row_assign):
            cells = {}
            for ci, t, _ok in assigns:
                if ci in keep:
                    cells[ci] = (cells.get(ci, '') + ' ' + t).strip()
            grid.append([cells.get(ci, '') for ci in keep])
            data_flags.append(use[region[ri]])
        # 实心率只看表格行（问题行/表头行本来就只有一格）
        filled = sum(1 for f, r in zip(data_flags, grid) if f
                     for c in r if c)
        if filled < max(6, n_ok * len(keep) * 0.5):
            continue

        # 证据门槛：
        #   - 首行是"表头"（文字单元格为主）→ 强证据
        #   - 或区域附近有表题 → 强证据
        #   - 或数据行 >=4（有规律数字连续成行成列）→ 足够
        first = grid[0] if grid else []
        text_cells = sum(1 for c in first if c and not _is_num(c))
        has_header = text_cells >= max(1, len(keep) // 2) and \
            len(grid) >= 3
        has_caption = any(cy0 - line_h * 3 <= y <= cy1 + line_h * 4
                          for y in caps)
        _dbg('cand first=%s header=%s cap=%s' %
             ([c[:20] for c in first[:4]], has_header, has_caption))
        all_data = all(is_data[i] for i in run)
        if not (has_header or has_caption or (all_data and len(run) >= 4)):
            continue
        # 纯文字表格的证据要更硬：有表题，或"3 列以上 + 至少 3 行 +
        # 首行像表头"。仅靠列对齐就认，会把双栏排版/清单误当表格。
        if not any(is_data[i] for i in run) and not has_caption \
                and not (has_header and len(keep) >= 3 and len(run) >= 3):
            continue
        # 图表坐标轴：<=3 列、几乎全数字、无表头也无表题 → 不是表格
        if len(keep) <= 3:
            cells = [c for r in grid for c in r if c]
            nums = sum(1 for c in cells if _is_num(c))
            if cells and nums >= len(cells) * 0.7 \
                    and not has_header and not has_caption:
                continue

        y0 = rows[region[0]][0] - line_h * 0.6
        y1 = rows[region[-1]][0] + line_h * 0.6
        tab_w = cols[keep[-1]][0] - cols[keep[0]][0]
        reason = _grammar_veto(grid, tab_w, y0, y1, page_w, page_h,
                               has_header, has_caption, numeric=all_data)
        if reason:
            _dbg('grammar veto: %s' % reason)
            if log:
                log(u'表格候选被语法审查否掉：%s' % reason)
            continue
        out.append((y0, y1, grid))
    return out
