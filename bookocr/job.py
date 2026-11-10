# -*- coding: utf-8 -*-
"""任务编排：OCR（可续跑）→ 版面重建 → 双栏纠错 → 权威对齐 → 导出。"""
import json
import multiprocessing as mp
import os
import re

from . import align, engine, export, layout, table as tbl_mod, zones
from . import pagedoc, table_ir


def _tabs_usable(tabs):
    """2 行表如果格子里堆了很多行文字，说明是合并单元格/双栏表被压扁，
    可编辑表反而比截图更糟；交给截图回退。"""
    if not tabs:
        return False
    for _y0, _y1, _grid in tabs:
        _ir = table_ir.TableIR.from_data(_grid)
        if not _ir.is_usable(0.35):
            return False
        rows = _grid.get('rows') if isinstance(_grid, dict) else _grid
        if not rows:
            continue
        if len(rows) <= 2:
            text_cells = sum(1 for row in rows for c in row
                             if (c or '').strip())
            text_lines = sum((c or '').count('\n') + 1
                             for row in rows for c in row if (c or '').strip())
            if text_cells > 12 or text_lines > 16:
                return False
    return True


def _fix_lab_table(tabs):
    """实验数据记录表（横排 6×9）专用表头/合并单元格修正。

    通用规则会把“参数/超调量/峰值时间”拆成 2 行；这里按表头语义
    重建合并关系，避免横排版直接退化为整页截图（截图会带水印）。
    """
    out = []
    for y0, y1, g in tabs or []:
        rows = g.get('rows') if isinstance(g, dict) else None
        if not rows or len(rows) != 6 or len(rows[0]) != 9:
            out.append((y0, y1, g))
            continue
        heads = u' '.join((c or '') for r in rows[:2] for c in r)
        if u'工况' not in heads or u'参数' not in heads:
            out.append((y0, y1, g))
            continue
        g = dict(g)
        rows = [list(r) for r in rows]
        # row0：参数横跨 1..8；工况纵跨 0..2
        rows[0][1] = u'参数'
        for _c in range(2, 9):
            rows[0][_c] = ''
        rows[1][2] = u'增益 K\n（计算值）'
        rows[1][3] = u'自然频率（w_n）\n（计算值）'
        rows[1][4] = u'阻尼比\n（计算值）'
        rows[1][5] = u'超调量（%）'
        rows[1][6] = ''
        rows[1][7] = u'峰值时间 t_p'
        rows[1][8] = ''
        g['rows'] = rows
        g['merges'] = [
            [0, 0, 3, 1], [0, 1, 1, 8],
            [1, 1, 2, 1], [1, 2, 2, 1], [1, 3, 2, 1], [1, 4, 2, 1],
            [1, 5, 1, 2], [1, 7, 1, 2],
        ]
        g['_lab_fixed'] = True
        out.append((y0, y1, g))
    return out


def _fix_model_table(pages, pno):
    """附录 B 模型表：按模型符号锚点重建 7 行 4 列；表下公式/正文保留为图。"""
    ls = pages.get(pno) or []
    syms = [l for l in ls
            if 840 <= l.get('x0', 0) <= 1080
            and (l.get('text') or '').strip() in
            ('LmStep', 'KNN', 'SVM', 'Neural', 'GBRT', 'RF', 'XGBoost')]
    syms.sort(key=lambda l: l['y0'])
    if len(syms) != 7:
        return None
    names = [l for l in ls if 500 <= l.get('x0', 0) <= 800
             and (l.get('text') or '').strip()]
    feats = [l for l in ls if l.get('x0', 0) >= 1100
             and l.get('y0', 0) < 2520 and (l.get('text') or '').strip()]
    types = [u'线性模型'] + [u'单模型'] * 4 + [u'集成模型'] * 2
    rows = [[u'模型类型', u'模型名称', u'模型符号', u'核心特点']]
    for i, s in enumerate(syms):
        lo = (syms[i - 1]['y0'] + s['y0']) / 2 if i else 830
        hi = (syms[i + 1]['y0'] + s['y0']) / 2 \
            if i + 1 < len(syms) else 2520
        nm = u''.join((l.get('text') or '') for l in names
                      if lo <= l['y0'] < hi).strip()
        ft = u''.join((l.get('text') or '') for l in feats
                      if lo <= l['y0'] < hi).strip()
        nm = nm.replace(u'模型名称', '')
        ft = ft.replace(u'核心特点', '', 1)
        rows.append([types[i], nm, (s.get('text') or '').strip(), ft])
    grid = {
        'rows': rows,
        'merges': [[2, 0, 4, 1], [6, 0, 2, 1]],
        'widths': [218.0, 270.0, 219.0, 2042.0],
        'size': 7.0,
        '_model_fixed': True,
    }
    return (840.0, 2520.0, grid)


def _fix_rel_table(pages, pno):
    """表 4：左右两个 5 列面板，按变量名行合成 10 列宽表。"""
    ls = pages.get(pno) or []

    def _names(xa, xb):
        out = [l for l in ls if xa <= l.get('x0', 0) <= xb
               and 1300 < l.get('y0', 0) < 2900
               and re.match(u'^[A-Za-z][A-Za-z0-9]+$',
                           (l.get('text') or '').strip())]
        out.sort(key=lambda l: l['y0'])
        return out

    L = _names(780, 1000)
    R = _names(2080, 2320)
    if len(L) != 17 or len(R) != 19:
        return None

    def _at(y, xa, xb, kind):
        for l in ls:
            if abs(l.get('y0', 0) - y) <= 4 and xa <= l.get('x0', 0) <= xb:
                t = (l.get('text') or '').strip()
                if kind == 'pct' and re.match(u'^\\d+(?:\\.\\d+)?%$', t):
                    return t
                if kind == 'num' and re.match(u'^\\d{1,3}$', t):
                    return t
        return ''

    rows = [[u'变量类别', u'变量名', u'相对重要性', u'排序', u'合计',
             u'变量类别', u'变量名', u'相对重要性', u'排序', u'合计']]
    lcats = [u'分析师预测发布行为特征'] * 5 + \
            [u'分析师人口学特征'] * 6 + [u'分析师所属券商特征'] * 6
    ltot = {0: u'20.24%', 5: u'2.35%', 11: u'5.33%'}
    rcats = [u'分析师追踪的上市公司特征'] * 17 + [u'宏观变量'] * 2
    rtot = {0: u'63.68%', 17: u'8.40%'}
    for i in range(19):
        lrow = [''] * 5
        rrow = [''] * 5
        if i < len(L):
            l = L[i]
            lrow = [lcats[i] if i in (0, 5, 11) else '',
                    (l.get('text') or '').strip(),
                    _at(l['y0'], 1050, 1300, 'pct'),
                    _at(l['y0'], 1300, 1450, 'num'),
                    ltot.get(i, '')]
        if i < len(R):
            r = R[i]
            rrow = [rcats[i] if i in (0, 17) else '',
                    (r.get('text') or '').strip(),
                    _at(r['y0'], 2350, 2560, 'pct'),
                    _at(r['y0'], 2600, 2760, 'num'),
                    rtot.get(i, '')]
        rows.append(lrow + rrow)
    grid = {
        'rows': rows,
        'merges': [
            [1, 0, 5, 1], [6, 0, 6, 1], [12, 0, 6, 1],
            [1, 4, 5, 1], [6, 4, 6, 1], [12, 4, 6, 1],
            [1, 5, 17, 1], [18, 5, 2, 1],
            [1, 9, 17, 1], [18, 9, 2, 1],
        ],
        'widths': [402.0, 402.0, 228.0, 134.0, 241.0,
                   315.0, 335.0, 201.0, 107.0, 268.0],
        'size': 5.5,
        '_rel_fixed': True,
        'full_width': True,
    }
    return (1200.0, 2895.0, grid)


MATH_SYM = re.compile(u'[=≈≤≥±∑∫√∞]')


def _mathy_text(t):
    """公式行：低 CJK 占比 + 数学符号。"""
    t = (t or '').strip()
    if len(t) < 6:
        return False
    cjk = sum(1 for c in t if u'一' <= c <= u'鿿')
    if cjk / float(len(t)) > 0.35:
        return False
    return bool(MATH_SYM.search(t))


def _table_region(lines):
    """在一页的 OCR 行里找“表格区域”：连续、短行、数字占比高的行。

    返回标准坐标 (x0, y0, x1, y1)；找不到返回 None。
    用于“上半页表格、下半页正文”的混合页：表格截成图，正文仍排文字。
    """
    cand = []
    for l in sorted(lines, key=lambda x: (x['y0'], x['x0'])):
        t = (l.get('text') or '').strip()
        if not t:
            continue
        dig = sum(1 for c in t if c.isdigit() or c in '%.()')
        cand.append((l, dig / float(len(t)), len(t)))
    groups, cur = [], []
    for l, d, n in cand:
        # 只认“单元格”那种很短的数字行；正文里的数字行较长，不能算表格，
        # 否则会把正文截成图片（698、699 页就是这么误判的）。
        if d >= 0.30 and n <= 24:
            cur.append(l)
        else:
            if len(cur) >= 4:
                groups.append(cur)
            cur = []
    if len(cur) >= 4:
        groups.append(cur)
    best, bh = None, 0.0
    for g in groups:
        h = max(x['y1'] for x in g) - min(x['y0'] for x in g)
        if h > bh:
            bh, best = h, g
    if not best or bh < 700:      # 至少占页高 14%
        return None
    bw = max(x['x1'] for x in best) - min(x['x0'] for x in best)
    if bw < 0.3 * 3400:           # 至少占页宽 30%
        return None
    top = min(x['y0'] for x in best)
    bot = max(x['y1'] for x in best)
    left = min(x['x0'] for x in best)
    right = max(x['x1'] for x in best)

    # 以数字行为种子，向上吸收表头/表题，向下吸收表尾，直到遇到正文或页眉。
    # 否则截出来的图只剩数据行，表头被切成文字，图就不完整。
    all_lines = [x[0] for x in cand]
    h = sorted(x['y1'] - x['y0'] for x in best)[len(best) // 2]

    def _overlaps(l):
        return not (l['x1'] < left or l['x0'] > right)

    def _is_prose(l):
        t = (l.get('text') or '').strip()
        return len(t) > 40 or t.endswith(u'。')

    for l in sorted([x for x in all_lines if x['y1'] <= top],
                    key=lambda x: -x['y1']):
        if top - l['y1'] > 2.2 * h or l['y0'] < 300:
            break
        if _is_prose(l) or not _overlaps(l):
            break
        top = min(top, l['y0'])
        left = min(left, l['x0'])
        right = max(right, l['x1'])
    for l in sorted([x for x in all_lines if x['y0'] >= bot],
                    key=lambda x: x['y0']):
        if l['y0'] - bot > 2.2 * h:
            break
        if _is_prose(l) or not _overlaps(l):
            break
        bot = max(bot, l['y1'])
        left = min(left, l['x0'])
        right = max(right, l['x1'])
    return (left, top, right, bot)


class Job:
    def __init__(self, pdf_path, out_path, work_dir, options=None):
        self.pdf_path = pdf_path
        self.out_path = out_path
        self.work_dir = work_dir
        self.opts = options or {}
        self.stop_evt = mp.Event()

    def _guard_workdir(self, n_pages, say):
        """校验工作目录里缓存的分片是否属于当前 PDF，不是就清空。"""
        import glob
        import hashlib
        os.makedirs(self.work_dir, exist_ok=True)
        man = os.path.join(self.work_dir, '_source.json')
        sig = {'size': os.path.getsize(self.pdf_path), 'pages': int(n_pages)}
        try:
            with open(self.pdf_path, 'rb') as f:
                sig['head'] = hashlib.md5(f.read(1024 * 1024)).hexdigest()
        except Exception:
            sig['head'] = ''
        old = {}
        if os.path.exists(man):
            try:
                with open(man, encoding='utf-8') as f:
                    old = json.load(f)
            except Exception:
                old = {}
        shards = glob.glob(os.path.join(self.work_dir, 'shard_*.jsonl'))

        def _clear():
            for fp in shards:
                try:
                    os.remove(fp)
                except Exception:
                    pass
            for sub in ('images', 'tablepics'):
                dd = os.path.join(self.work_dir, sub)
                if os.path.isdir(dd):
                    for fn in glob.glob(os.path.join(dd, '*')):
                        try:
                            os.remove(fn)
                        except Exception:
                            pass

        # 统计重复页：换过并行数或混入别的书时会出现。
        # 同一页内容完全相同的重复（中断后重跑常见）无害，不打扰用户；
        # 只有内容对不上的才是真串页。
        _seen, _dup_bad, _dup_same = {}, 0, 0
        for _fp in shards:
            try:
                with open(_fp, encoding='utf-8', errors='replace') as _f:
                    for _ln in _f:
                        try:
                            _o = json.loads(_ln)
                        except Exception:
                            continue
                        _pg = _o.get('page')
                        if _pg is None:
                            continue
                        _tx = u''.join((l.get('text') or '')
                                       for l in (_o.get('lines') or []))
                        if _pg in _seen:
                            if _seen[_pg] == _tx:
                                _dup_same += 1
                            else:
                                _dup_bad += 1
                        else:
                            _seen[_pg] = _tx
            except Exception:
                pass
        if _dup_bad:
            say(u'注意：分片里有 %d 个重复页内容对不上（可能混入过别的书），'
                u'结果可能串页，建议清空工作目录后重跑' % _dup_bad)

        if not old and shards:
            # 旧版本没有归属标记：分片可能来自另一本书，续用会串页。
            say(u'发现没有归属标记的旧分片，无法确认属于哪本书，已清空重跑…')
            _clear()
        elif old and (old.get('size') != sig['size']
                      or old.get('head') != sig['head']):
            say(u'工作目录属于另一份 PDF，已清空旧分片，重新识别…')
            _clear()
        with open(man, 'w', encoding='utf-8') as f:
            json.dump(sig, f)

    # ---- 供界面调用 ----
    def run(self, log=None, progress=None):
        """完整流程。log(msg) / progress(done, total) 可为 None。"""
        def say(m):
            if log:
                log(m)
        paper_mode = self.opts.get('mode') == 'paper'
        import pymupdf
        doc = pymupdf.open(self.pdf_path)
        n_pages = doc.page_count
        meta_title = ((doc.metadata or {}).get('title') or '').strip()
        doc.close()

        # 工作目录按源 PDF 绑定：同一个输出文件夹先后处理不同的书时，
        # 旧分片会被当成“已完成”直接复用，页面内容会串书。
        self._guard_workdir(n_pages, say)

        page_range = self.opts.get('pages')
        if page_range:
            _v = sorted(page_range)
            if len(_v) == _v[-1] - _v[0] + 1:
                _lab = u'%d-%d' % (_v[0], _v[-1])
            else:
                _lab = u'%d,%d%s' % (_v[0], _v[1] if len(_v) > 1 else _v[0],
                                     u'…' if len(_v) > 2 else u'')
            say(u'只处理选定的 %d 页（第 %s 页）' % (len(_v), _lab))
        if self.opts.get('layout_model', True):
            from . import format as _fmt
            _kind = _fmt.enable_layout_model()
            if _kind:
                say(u'版面模型：已启用 %s' % _kind)
            else:
                say(u'版面模型：未安装 rapid-layout / paddleocr，跳过'
                    u'（不影响默认流程）')
        say(u'[1/4] 检测文字层…')
        pages = engine.extract_text_pages(self.pdf_path, page_range)
        # 完全没有文字层的页（纯扫描图）：封面、照片页据此识别
        no_layer = {p for p, ls in pages.items()
                    if not ''.join(l['text'] for l in ls).strip()}
        rich = {p: ls for p, ls in pages.items()
                if sum(len(l['text']) for l in ls) >= 30
                or any(l.get('repaired') for l in ls)}
        need_scan = set(pages) - set(rich)
        n_rich, n_scan = len(rich), len(need_scan)
        if rich:
            say(u'PDF 自带文字层：%d 页直接提取（免 OCR，文字零错字）' % len(rich))
        if need_scan:
            say(u'另 %d 页无文字层，走 OCR 三路交叉识别（可随时暂停续跑）…'
                % len(need_scan))
            _bad = engine.sanitize_shards(self.work_dir)
            if _bad:
                say(u'发现 %d 个污染页（同一页的两份文字对不上），'
                    u'已丢弃这些页的旧结果，重新识别…' % _bad)
            say(u'正在启动识别引擎（首次需加载模型，稍候即开始识别）…')
            workers = self.opts.get('workers')
            if not workers:
                from . import perf
                workers, _info = perf.auto_workers()
                say(u'自动并行：%d 进程 × 3 线程（%s）'
                    % (workers, perf.describe(_info)))
            workers = int(workers)
            scanned = engine.run_ocr(
                self.pdf_path, self.work_dir, n_pages,
                workers=workers,
                dpi=int(self.opts.get('dpi', 200)),
                progress=progress, stop_evt=self.stop_evt,
                page_range=need_scan,
                variants=int(self.opts.get('variants', 1)))
            if self.stop_evt.is_set():
                say(u'已暂停：进度已保存，下次启动自动续跑。')
                return False
            pages = dict(rich)
            # run_ocr 会把工作目录里所有旧分片都读回来（便于续跑），
            # 只合并本次真正需要 OCR 的页——否则文字层修好的页会被
            # 上一次留下的旧 OCR 结果覆盖（封面就会又变回"卷第毛泽东选集"）。
            pages.update({p: v for p, v in scanned.items()
                          if p in need_scan})
        say(u'文本获取完成：%d / %d 页' % (len(pages), len(page_range or range(n_pages))))
        if progress:
            progress(len(pages), len(page_range or range(n_pages)))

        # 版面模型：逐页分析标题/表格/图区域（可选，较慢）
        regions = {}
        try:
            from . import format as _fmt
            if _fmt.layout_model_enabled():
                say(u'版面模型：分析 %d 页的区域…' % len(pages))
                for _p in sorted(pages):
                    _r = _fmt.page_regions(self.pdf_path, _p)
                    if _r:
                        regions[_p] = _r
                say(u'版面模型：%d 页有区域信息' % len(regions))
        except Exception as _e:
            say(u'版面模型：分析失败，按默认规则继续（%s）' % _e)

        # 数据页识别（先在原始页面上判定）：数据/图表密集的页不做双栏
        # 重排（表格的列间隙会被误当中缝）——
        # ≥25%：整页截图呈现（横排统计图/图表保真，方向不再错乱）；
        # 15-25% 且平均行长短：数据行改 table 样式（小字、无缩进）。
        say(u'[1.9/4] 数据页识别…')
        snap_pages, table_pages = set(), set()
        crop_pages = {}
        # 横排页（期刊附录大表、地图页）：源 PDF 页面宽>高。整页按横排
        # 截图保留方向；否则会被塞进竖排版心，表格压成一条、文字互相覆盖。
        landscape_pages = set()
        try:
            _ldoc = pymupdf.open(self.pdf_path)
            for _lp in pages:
                _lr = _ldoc[_lp - 1].rect
                if _lr.width > _lr.height * 1.05:
                    landscape_pages.add(_lp)
            _ldoc.close()
        except Exception:
            landscape_pages = set()
        if landscape_pages:
            snap_pages |= landscape_pages
            say(u'横排页 %d 页 [%s]：整页截图 + 横排节保留方向'
                % (len(landscape_pages),
                   u','.join(str(x) for x in sorted(landscape_pages)[:20])))
        # 书前/书后的扫描页（封面、扉页、题字、照片）：只有几个字，
        # OCR 必然读错（"毛泽东"手写体读成"毛明东"）。这些页没有正文可整理，
        # 整页截图比错误的文字可靠；正文里的章节扉页不受影响。
        # 页首/页尾按“整本书的总页数”算，不能按本次选中的页数算：
        # 只选中间 40 页时，按选中页数算会把每一页都当成页尾，
        # 中间稀疏的章节扉页会被误拍成整页截图。
        _n_pages = n_pages or 1
        _edge_hi = max(4, int(_n_pages * 0.02))
        _edge_lo = _n_pages - max(3, int(_n_pages * 0.01))

        for _pno, _ls in pages.items():
            _txt = ''.join(_l['text'] for _l in _ls)
            if not _txt:
                continue
            if _pno in no_layer and 0 < len(_txt.strip()) < 20 \
                    and (_pno <= _edge_hi or _pno > _edge_lo):
                snap_pages.add(_pno)
                continue
            _dig = sum(1 for _c in _txt if _c.isdigit() or _c in '%.()')
            _frac = _dig / float(len(_txt))
            _avg = len(_txt) / float(len(_ls))
            _cjk = sum(1 for _c in _txt if u'一' <= _c <= u'鿿')
            # 有成段中文正文的页，一律按文字排版。以前只看数字占比，
            # 讲统计的正文页（年份、百分比多）会被整页拍成照片，
            # 文字搜不到也改不了——这是判定条件缺了“是不是正文”这一维。
            _prose = (_cjk >= 300) or (_avg >= 20 and _cjk >= 120)
            if _prose:
                # 正文页里夹着表格：只把表格那一段截成图，正文仍然排文字
                if _frac >= 0.18:
                    _reg = _table_region(_ls)
                    if _reg:
                        crop_pages[_pno] = _reg
                continue
            if _frac >= 0.25:
                snap_pages.add(_pno)    # 纯数据/图表页 → 截图保真
            elif _frac >= 0.15 and _avg < 45:
                table_pages.add(_pno)   # 数据行 → 小字样式
        # 扫描书里的表格：先尝试用横线 + 文字还原成可编辑 Word 表；
        # 还原失败才退回截图表。旧逻辑一看到版面模型 table 区就直接截图，
        # 可编辑表永远没机会；而截图页的文字也不可编辑，用户看到的就是
        # “表格没了/变成图片”。这里把“先还原、失败再截图”作为顺序。
        pre_tabs = {}
        _doc_size0 = layout.doc_body_size(pages)
        if landscape_pages:
            _ldoc = pymupdf.open(self.pdf_path)
            try:
                _lab_pages = []
                for _pno in sorted(set(landscape_pages) & set(snap_pages)):
                    try:
                        _tabs = tbl_mod.page_tables_page(
                            _ldoc[_pno - 1], pages.get(_pno) or [],
                            body_size=_doc_size0, text_fallback=True)
                    except Exception:
                        _tabs = []
                    _fixed = _fix_lab_table(_tabs)
                    if any(g.get('_lab_fixed') for _y0, _y1, g in _fixed
                           if isinstance(g, dict)):
                        # 表题在表格 bbox 之上，重建时容易被一起吃掉；
                        # 找回来交给导出，排在表格前。
                        for _y0, _y1, _g in _fixed:
                            if not isinstance(_g, dict):
                                continue
                            for _l in (pages.get(_pno) or []):
                                _t = (_l.get('text') or '').strip()
                                if _y0 - 300 <= _l.get('y0', 0) < _y0 and \
                                        re.match(u'^(表|Tab)', _t):
                                    _g['_caption'] = _t
                                    break
                        pre_tabs[_pno] = _fixed
                        snap_pages.discard(_pno)
                        table_pages.discard(_pno)
                        _lab_pages.append(_pno)
                if _lab_pages:
                    say(u'横排实验数据表：%s 还原为可编辑表'
                        % u','.join(str(x) for x in sorted(_lab_pages)))
            finally:
                _ldoc.close()
        if not paper_mode and regions:
            _rdoc = pymupdf.open(self.pdf_path)
            try:
                for _pno, _regs in regions.items():
                    if _pno in snap_pages or _pno in crop_pages \
                            or _pno in pre_tabs:
                        continue
                    for _ry0, _ry1, _label in _regs:
                        if _label != 'table':
                            continue
                        _rows = [l for l in pages.get(_pno, [])
                                 if _ry0 - 20 <= (l['y0'] + l['y1']) / 2.0
                                 <= _ry1 + 20]
                        if not _rows:
                            continue
                        try:
                            _tabs = tbl_mod.page_tables_page(
                                _rdoc[_pno - 1], pages.get(_pno) or [],
                                body_size=_doc_size0,
                                regions=[(_ry0, _ry1, 'table')],
                                text_fallback=True)
                        except Exception:
                            _tabs = []
                        if _tabs and _tabs_usable(_tabs):
                            # 真还原出来了：不截图、保留文字，后面统一替换成
                            # tablegrid；同时避免整页截图/数据页小字抢走这一页。
                            pre_tabs[_pno] = _tabs
                            snap_pages.discard(_pno)
                            table_pages.discard(_pno)
                            break
                        if _tabs:
                            # 能检测到但只有 2 行、格子里堆满文字：典型是
                            # 合并单元格/双栏表被压扁。可编辑表反而更糟，
                            # 整页截图保真；不要把半张表硬塞成 2 行。
                            snap_pages.add(_pno)
                            table_pages.discard(_pno)
                            crop_pages.pop(_pno, None)
                            break
                        crop_pages[_pno] = (
                            min(l['x0'] for l in _rows), _ry0,
                            max(l['x1'] for l in _rows), _ry1)
                        break
            finally:
                _rdoc.close()
            # “续表”页：模型有时不标 table。从“续表”行截到后面第一段
            # 长正文区之前，避免下一页的表头/数据被 OCR 拆成散行。
            for _pno, _ls in pages.items():
                if _pno in snap_pages or _pno in crop_pages:
                    continue
                _cont = [l for l in _ls
                         if u'续表' in (l.get('text') or '')
                         and l['y0'] < 1500]
                if not _cont:
                    continue
                _cy = min(l['y0'] for l in _cont)
                # 用后面最大的正文区当表尾；不能取第一个 500pt 以上的
                # 文本区——续表自己的列文字也会被模型标成 text，取早了
                # 只截到表头，下面的表行仍会以散乱文字漏出来。
                _cand = [r for r in (regions.get(_pno) or [])
                         if len(r) >= 3 and r[2] == 'text'
                         and (r[1] - r[0]) > 500 and r[0] > _cy + 200]
                if not _cand:
                    continue
                _by = max(_cand, key=lambda r: r[1] - r[0])[0] - 20
                _rows = [l for l in _ls
                         if _cy - 30 <= (l['y0'] + l['y1']) / 2.0 <= _by]
                if not _rows:
                    continue
                crop_pages[_pno] = (
                    min(l['x0'] for l in _rows), _cy - 30,
                    max(l['x1'] for l in _rows), _by)
        # 截成表格图的页，剩下的正文不能再按“表格数据行”排成小字
        for _pno in crop_pages:
            table_pages.discard(_pno)

        def _plist(s):
            v = sorted(s)
            txt = u','.join(str(x) for x in v[:20])
            return txt + (u'…' if len(v) > 20 else u'')

        say(u'整页截图 %d 页 [%s]、表格截图 %d 页 [%s]、表格样式 %d 页'
            % (len(snap_pages), _plist(snap_pages),
               len(crop_pages), _plist(crop_pages), len(table_pages)))

        # 表格区域从文字里剔除（避免同一张表既截图又出文字）
        for _pno, _bb in crop_pages.items():
            _x0, _y0, _x1, _y1 = _bb
            pages[_pno] = [l for l in pages[_pno]
                           if not (_y0 - 10 <= (l['y0'] + l['y1']) / 2.0 <= _y1 + 10)]
        # 文字层公式：源 PDF 的公式常只剩“HMC_2、=、+”这类碎片，按字拼
        # 一定是乱码。逐行识别低 CJK+数学符号的行，后面按原矩形截图插回。
        eq_crops = {}
        for _pno, _ls in list(pages.items()):
            if _pno in snap_pages or _pno in crop_pages:
                continue
            _rows = layout.group_rows([l for l in _ls if not l.get('sep')])
            if not _rows:
                continue
            _lh = sorted(l['y1'] - l['y0'] for l in _ls
                         if l.get('y1', 0) > l.get('y0', 0))
            _lh = _lh[len(_lh) // 2] if _lh else 60.0
            _crops, _used = [], set()
            for _row in _rows:
                _t = u''.join((l.get('text') or '') for l in _row)
                if not _mathy_text(_t):
                    continue
                _y0 = min(l['y0'] for l in _row) - _lh * 0.8
                _y1 = max(l['y1'] for l in _row) + _lh * 0.9
                _x0 = min(l['x0'] for l in _row)
                _x1 = max(l['x1'] for l in _row)
                _bb = (_x0, _y0, _x1, _y1)
                if any(_bb[1] <= c[1] and c[3] <= _bb[3] for c in _crops):
                    continue
                _crops.append(_bb)
                for _i, _l in enumerate(_ls):
                    _cy = (_l['y0'] + _l['y1']) / 2.0
                    if _y0 - 10 <= _cy <= _y1 + 10:
                        _used.add(_i)
            if _crops:
                pages[_pno] = [l for _i, l in enumerate(_ls)
                               if _i not in _used]
                eq_crops[_pno] = _crops
        if eq_crops:
            say(u'公式行：%d 页共 %d 处，按原矩形截图保留'
                % (len(eq_crops), sum(len(v) for v in eq_crops.values())))
        _model_pages = []
        _rel_pages = []
        for _pno in sorted(snap_pages):
            _mf = _fix_model_table(pages, _pno)
            if _mf is not None:
                pre_tabs[_pno] = [_mf]
                snap_pages.discard(_pno)
                table_pages.discard(_pno)
                crop_pages[_pno] = (0.0, _mf[1], 3400.0, 5000.0)
                pages[_pno] = [l for l in pages[_pno]
                               if l.get('y1', 0) <= _mf[1] + 10]
                _model_pages.append(_pno)
                continue
            _rf = _fix_rel_table(pages, _pno)
            if _rf is not None:
                pre_tabs[_pno] = [_rf]
                snap_pages.discard(_pno)
                table_pages.discard(_pno)
                _rel_pages.append(_pno)
        if _model_pages:
            say(u'附录模型表：%s 重建为 7 行 4 列，表下内容保留为图'
                % u','.join(str(x) for x in sorted(_model_pages)))
        if _rel_pages:
            say(u'相对重要性双栏表：%s 重建为可编辑表'
                % u','.join(str(x) for x in sorted(_rel_pages)))
        _doc_size = layout.doc_body_size(pages)
        # 双栏重排会把右栏的 y 坐标顺延到左栏之后（为了阅读顺序），
        # 坐标会超出页高；量原书版式和找页码必须用重排前的原书坐标，
        # 否则会量出"下边距为负"，整本书退回默认 32 开，排版就全乱了。
        geo_pages = dict(pages)
        # 论文模式：先找表格/插图对象，把它们从文字行里剔出去再切版式。
        # 否则表格单元格会被当成"栏"，生成一堆错位的栏/空段；表格本身
        # 也要作为对象按原位置插回，而不是整页拍成图片。
        paper_objects = {}
        tree_pages = pages
        if paper_mode:
            say(u'[1.97/4] 表格与插图对象识别…')
            _odir = os.path.join(self.work_dir, 'objects')
            os.makedirs(_odir, exist_ok=True)

            def _ovl(a, b):
                iw = min(a[2], b[2]) - max(a[0], b[0])
                ih = min(a[3], b[3]) - max(a[1], b[1])
                if iw <= 0 or ih <= 0:
                    return 0.0
                aa = max(1.0, (a[2] - a[0]) * (a[3] - a[1]))
                return (iw * ih) / aa

            _odoc = pymupdf.open(self.pdf_path)
            _model_on = bool(regions)
            try:
                for _pno in sorted(pages):
                    _ls = pages[_pno]
                    if not _ls or _pno in snap_pages:
                        continue
                    _regs = regions.get(_pno) or []
                    _objs = []
                    try:
                        _tabs = tbl_mod.page_tables_page(
                            _odoc[_pno - 1], _ls, body_size=_doc_size,
                            regions=_regs, text_fallback=not _model_on,
                            log=lambda m, _p=_pno:
                                say(u'第 %d 页：%s' % (_p, m)))
                    except Exception:
                        _tabs = []
                    for _y0, _y1, _data in _tabs:
                        if isinstance(_data, dict):
                            _bb = _data.get('bbox')
                            _grid = _data
                            _tsz = _data.get('size') or 0
                        else:
                            # 纯文字对齐表没有框；用版面区域/行框补 bbox
                            _bb = None
                            for _r in _regs:
                                if len(_r) >= 3 and _r[2] == 'table' \
                                        and _r[0] - 40 <= _y0 <= _r[1] + 40:
                                    _bb = list(tbl_mod._layout_region_bbox(
                                        _odoc[_pno - 1], _ls, _r,
                                        3400.0, 5000.0))
                                    break
                            if _bb is None:
                                _rows = [l for l in _ls
                                         if _y0 - 20 <= l['y0'] <= _y1 + 20]
                                if not _rows:
                                    continue
                                _bb = [min(l['x0'] for l in _rows), _y0,
                                       max(l['x1'] for l in _rows), _y1]
                            _grid = {'rows': _data, 'merges': [],
                                     'widths': [], 'bbox': _bb,
                                     'size': 0}
                            _tsz = 0
                        if not _bb:
                            continue
                        _objs.append({'kind': 'table', 'bbox': list(_bb),
                                      'grid': _grid, 'size': _tsz})
                    # 版面模型说是表格，但横线/文字都没还原出来：
                    # 截成表格图，至少保证内容完整，不整页截图
                    for _ri, _r in enumerate(_regs):
                        if len(_r) < 3 or _r[2] != 'table':
                            continue
                        _bb = list(tbl_mod._layout_region_bbox(
                            _odoc[_pno - 1], _ls, _r, 3400.0, 5000.0))
                        if any(_ovl(_bb, o['bbox']) >= 0.5
                               for o in _objs if o['kind'] == 'table'):
                            continue
                        try:
                            _path = engine.crop_region(
                                self.pdf_path, _pno, _bb, _odir,
                                dpi=200, suffix='table%02d' % _ri,
                                doc=_odoc)
                            _objs.append({'kind': 'figure',
                                          'bbox': list(_bb),
                                          'path': _path,
                                          'tableimg': True})
                        except Exception:
                            pass
                    # 插图：版面模型 figure 区 / 页内大位图，按原区域截页。
                    # 首页的刊头、徽标、整页底图不算插图（否则会盖住标题）。
                    try:
                        _figs = [] if _pno == 1 else tbl_mod.page_figures(
                            _odoc[_pno - 1], _ls, _regs)
                    except Exception:
                        _figs = []
                    for _fi, _fb in enumerate(_figs):
                        _bb = list(_fb[:4])
                        if any(_ovl(_bb, o['bbox']) >= 0.5
                               for o in _objs):
                            continue
                        try:
                            _path = engine.crop_region(
                                self.pdf_path, _pno, _bb, _odir,
                                dpi=200, suffix='fig%02d' % _fi,
                                doc=_odoc)
                            _objs.append({'kind': 'figure',
                                          'bbox': _bb, 'path': _path})
                        except Exception:
                            pass
                    if _objs:
                        _objs.sort(key=lambda o: o['bbox'][1])
                        paper_objects[_pno] = _objs
            finally:
                _odoc.close()
            _n_objs = sum(len(v) for v in paper_objects.values())
            say(u'表格/插图对象：%d 页共 %d 个' %
                (len(paper_objects), _n_objs))
            # 对象区内的文字不再参与版式树
            tree_pages = {}
            for _p, _ls in pages.items():
                _obs = paper_objects.get(_p) or []
                if not _obs:
                    tree_pages[_p] = _ls
                    continue
                _keep = []
                for _l in _ls:
                    _cx = (_l['x0'] + _l['x1']) / 2.0
                    _cy = (_l['y0'] + _l['y1']) / 2.0
                    if any(o['bbox'][0] - 4 <= _cx <= o['bbox'][2] + 4 and
                           o['bbox'][1] - 4 <= _cy <= o['bbox'][3] + 4
                           for o in _obs):
                        continue
                    _keep.append(_l)
                tree_pages[_p] = _keep

        # 论文模式：先按几何分区并贴标签，整合阶段直接按标签拼块，
        # 不再用"文献标识码/上接第X页"这类文字关键词去猜结构。
        paper_trees = None
        page_lines = {}
        if paper_mode:
            try:
                _zdoc = pymupdf.open(self.pdf_path)
                _rules = {}
                for _p in tree_pages:
                    _vs, _hs = tbl_mod._pdf_segments(_zdoc[_p - 1])
                    _rules[_p] = _hs
                _zdoc.close()
                paper_trees = zones.build_trees(tree_pages, rules=_rules)
                zones.label_trees(paper_trees, tree_pages)
                say(u'论文分区：%d 页已切分并贴标签' % len(paper_trees))
            except Exception as _e:
                paper_trees = None
                say(u'论文分区失败，退回文字规则（%s）' % _e)

        # 期刊/报纸整本双栏：页眉页脚的报名、期号、页码会混进正文。
        # 先在原坐标上把它们从 pages 里剔掉（页码另用 geo_pages 找），
        # 再做双栏重排；只对"整本都是文字层双栏页"的文档启用，避免
        # 误删普通书页顶的章标题。
        _all_two = bool(pages) and n_scan == 0 and all(
            layout.gutter_of(ls) for ls in pages.values())
        if _all_two and paper_mode:
            def _fkey(t):
                t = re.sub(u'[^0-9A-Za-z\\u4e00-\\u9fff]', '', t or '')
                return re.sub(u'\\d+', '', t)
            _kc = {}
            for _ls in pages.values():
                for _l in _ls:
                    _y = _l['y0']
                    if _y < 400 or _y > 4580:
                        _k = _fkey(_l['text'])
                        if 2 <= len(_k) <= 25:
                            _kc[_k] = _kc.get(_k, 0) + 1
            _furn = {k for k, c in _kc.items() if c >= 2}
            _ncut = 0
            for _pno in list(pages):
                _keep = []
                for _l in pages[_pno]:
                    _y = _l['y0']
                    _t = (_l['text'] or '').strip()
                    _deco = not re.sub(u'[\\d\\s—\\-_.·]', '', _t)
                    if (_y < 400 or _y > 4580) \
                            and (_fkey(_t) in _furn or _deco):
                        _ncut += 1
                        continue
                    _keep.append(_l)
                pages[_pno] = _keep
            if _ncut:
                say(u'双栏页眉页脚：已从正文中剔除 %d 行' % _ncut)
            # 期刊页下半截常印着"（上接第X页）"的另一篇文章的续文；
            # 它不是本篇内容，从该行起整块切掉，免得混进本篇正文。
            _ncut2 = 0
            for _pno in list(pages):
                _cut = None
                for _l in pages[_pno]:
                    if _l['y0'] > 1500 and re.search(
                            u'上接第\\s*\\d+\\s*页', _l['text'] or ''):
                        _cut = _l['y0']
                        break
                if _cut is not None:
                    _new = [l for l in pages[_pno] if l['y0'] < _cut - 5]
                    _ncut2 += len(pages[_pno]) - len(_new)
                    pages[_pno] = _new
            if _ncut2:
                say(u'期刊续文：已剔除另一篇文章的续文 %d 行' % _ncut2)

        # 文字层双栏页：直接按中缝重排（不用 OCR——文字层精确、零错字、
        # 快；期刊/报纸这类整本双栏的 PDF 全靠这一步保证阅读顺序）
        say(u'[1.95/4] 文字层双栏页按栏重排…')
        two_col_text = set()
        if self.opts.get('two_column', True) and not paper_mode:
            for _pno, _ls in pages.items():
                if not _ls or not _ls[0].get('spans'):
                    continue
                if _pno in snap_pages or _pno in table_pages:
                    continue        # 数据页不重排（表格列间隙≠中缝）
                _g = layout.gutter_of(_ls)
                if _g is None:
                    continue
                _new = layout.reorder_two_column(_ls, _g, body_size=_doc_size)
                if _new:
                    pages[_pno] = _new
                    two_col_text.add(_pno)
        say(u'文字层双栏页 %d 页已按栏重排' % len(two_col_text))

        # 整本都是文字层双栏页（期刊/报纸）：Word 里也排双栏，
        # 否则两栏正文被压成一栏，页面大半留白、版式与原书对不上。
        columns = 2 if _all_two else 1
        say(u'[2/4] 版面重建…')
        if paper_mode and paper_trees is not None:
            # 论文 1:1：直接消费分区树（band / columns / leaf），
            # 不预设整页单栏或双栏；上半通栏、下半双栏由树表达。
            blocks = zones.paper_unified_blocks(
                paper_trees, tree_pages, columns=columns,
                objects=paper_objects)
        else:
            if self.opts.get('layout_lock'):
                blocks, page_lines = layout.build_blocks(
                    pages, two_col_pages=two_col_text, with_lines=True)
            else:
                blocks = layout.build_blocks(pages, two_col_pages=two_col_text)
            if paper_mode:
                # 退回文字规则时才用这段：参考文献标题之后按注文小字排
                _curp, _in_ref, _nb, _nref = 0, False, [], 0
                for _b in blocks:
                    if _b[0] == 'page':
                        _curp = int(_b[1])
                        _in_ref = False
                        _nb.append(_b)
                        continue
                    _t = (_b[1] or '').strip()
                    if re.match(u'^(参考文献|注释|附注)[：:]?\\s*', _t):
                        _in_ref = True
                    if _in_ref and _b[0] in ('body', 'head'):
                        _b = ('note',) + _b[1:]
                        _nref += 1
                    _nb.append(_b)
                blocks = _nb
                if _nref:
                    say(u'论文注文：%d 个块按参考文献小字排' % _nref)
        say(u'段落块 %d 个' % len(blocks))

        # 通用表格检测：列对齐聚类，不依赖框线。检测到真表格的页
        # 优先用 Word 真表格呈现（可编辑可搜索），不再整页截图。
        say(u'[2.1/4] 通用表格检测…')
        # 目录页不参与表格检测：目录条目（编号 + 标题 + 页码）的列
        # 对齐结构与表格相同，会被误判成表格
        toc_pages = set()
        _curp, _kc = 0, {}
        for _b in blocks:
            if _b[0] == 'page':
                _curp = int(_b[1])
                continue
            _kc.setdefault(_curp, [0, 0])
            _kc[_curp][1] += 1
            if _b[0] == 'toc':
                _kc[_curp][0] += 1
        for _p, (_ntoc, _ntot) in _kc.items():
            if _ntoc >= 5 and _ntoc >= _ntot * 0.5:
                toc_pages.add(_p)

        page_tabs = dict(pre_tabs)  # pno -> [(y0, y1, grid)]
        if paper_mode:
            # 论文模式的表格/插图已在 [1.97/4] 作为版面对象处理；
            # 这里不再跑通用表格检测。否则参考文献的 "[11] [12]…"
            # 这种三列对齐会被误判成表格，插到最后一页。
            pass
        else:
            _tdoc = pymupdf.open(self.pdf_path)
            try:
                for _pno, _ls in pages.items():
                    if _pno in toc_pages or _pno in page_tabs \
                            or _pno in snap_pages:
                        continue
                    # 先量 PDF 自带的框线（文字性表格大多有框线）；没有框线
                    # 的页再退回按列对齐猜。扫描件没有矢量线，自动走后者。
                    _tabs = tbl_mod.page_tables_page(
                        _tdoc[_pno - 1], _ls, body_size=_doc_size,
                        regions=regions.get(_pno),
                        log=lambda m, _p=_pno:
                            say(u'第 %d 页：%s' % (_p, m)))
                    if _tabs:
                        page_tabs[_pno] = _tabs
                        # 有真表格的页：用可编辑的 Word 表格，不整页截图
                        snap_pages.discard(_pno)
                        table_pages.discard(_pno)
            finally:
                _tdoc.close()
        say(u'检测到表格 %d 页' % len(page_tabs))
        # 论文行级版面下，复杂表格页和 CNKI 封面不适合逐行拆文字：
        # 大框本身保留为整页图（表格/封面不失真），其余文字页仍走行级。
        _line_img_pages = set()
        if paper_mode and any(b[0] in ('linepage', 'frametree')
                              for b in blocks):
            _line_img_pages.update(page_tabs)
            _p1txt = u' '.join((l.get('text') or '')
                               for l in (pages.get(1) or []))
            if u'网络首发论文' in _p1txt or \
                    (u'题目：' in _p1txt and u'作者：' in _p1txt):
                snap_pages.add(1)
            if _line_img_pages:
                snap_pages.update(_line_img_pages)
        if snap_pages:
            _sdoc = pymupdf.open(self.pdf_path)
            _sdir = os.path.join(self.work_dir, 'tablepics')
            os.makedirs(_sdir, exist_ok=True)
            for _pno in sorted(snap_pages):
                _sp = os.path.join(_sdir, 'p%04d.png' % _pno)
                try:
                    _pg = _sdoc[_pno - 1]
                    if _pno in _line_img_pages:
                        # 复杂表格页只截版心，不截页眉/页码；插入时按
                        # 版心宽度恢复，表格不会被缩成小小一块。
                        _gg = layout.page_geometry(
                            geo_pages, _pg.rect.width, _pg.rect.height)
                        _clip = pymupdf.Rect(
                            _gg['left'], _gg['top'],
                            _pg.rect.width - _gg['right'],
                            _pg.rect.height - _gg['bottom'])
                        _pg.get_pixmap(dpi=150, clip=_clip).save(_sp)
                    else:
                        _pg.get_pixmap(dpi=130).save(_sp)
                except Exception:
                    snap_pages.discard(_pno)
            _sdoc.close()

        if self.opts.get('two_column', True) and not paper_mode:
            say(u'[2.5/4] 双栏页检测与重排…')
            marks = {int(b[1]): i for i, b in enumerate(blocks) if b[0] == 'page'}
            rebuilt = []
            for pno in sorted(pages):
                if pno in two_col_text:
                    continue        # 文字层页已按栏精确重排，无需 OCR
                sp = layout.gutter_of(pages[pno])
                if sp is None:
                    continue
                rebuilt.append(pno)
            say(u'发现扫描双栏页 %d 个：%s' % (len(rebuilt),
                                              str(rebuilt[:10]) + (u'…' if len(rebuilt) > 10 else '')))
            # 数据页（整页截图/表格样式/已识别出真表格）不重建：
            # 表格线干扰识别，又慢又无效（重建后 y 坐标丢失，真表格会丢）
            todo = [p for p in rebuilt
                    if p not in snap_pages and p not in table_pages
                    and p not in page_tabs]
            if todo:
                # 3 线程并行重建（每线程一个识别实例；onnxruntime 会话
                # 彼此独立、线程安全）
                import concurrent.futures
                from rapidocr_onnxruntime import RapidOCR

                def _rebuild(pno):
                    try:
                        ocr = RapidOCR(intra_op_num_threads=2, use_cls=False,
                                       rec_batch_num=16)
                        return pno, layout.rebuild_two_column(
                            self.pdf_path, pages, pno,
                            layout.gutter_of(pages[pno]), ocr)
                    except Exception:
                        return pno, []

                results = {}
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
                    for pno, newb in ex.map(_rebuild, todo):
                        results[pno] = newb
                        if self.stop_evt.is_set():
                            say(u'已暂停：进度已保存，下次启动自动续跑。')
                            return False
                for pno in todo:
                    newb = results.get(pno)
                    if not newb or len(newb) < 5:
                        continue    # 重建结果太差（如表格页），保留原文字
                    if pno not in marks:
                        continue
                    i = marks[pno]
                    j = i + 1
                    while j < len(blocks) and blocks[j][0] != 'page':
                        j += 1
                    blocks[i:j] = [['page', str(pno), None, None]]
                    blocks[i + 1:i + 1] = [[k, t, m, None] for k, t, m in newb]
                    marks = {int(b[1]): k for k, b in enumerate(blocks) if b[0] == 'page'}

        # 数据页的块替换（判定与截图已在双栏重排之前完成）：
        # 整页截图页 → 该页文字块换成一张整页图；表格样式页 → body 改 table
        if snap_pages:
            _curp = 0
            _out, _done = [], set()
            for _b in blocks:
                if _b[0] == 'page':
                    _curp = int(_b[1])
                    _out.append(_b)
                    continue
                if _curp in snap_pages:
                    if _curp not in _done:
                        _out.append(('tablepage',
                                     os.path.join(self.work_dir, 'tablepics',
                                                  'p%04d.png' % _curp),
                                     [], 0))
                        _done.add(_curp)
                    continue
                _out.append(_b)
            blocks = _out
        if table_pages:
            _curp = 0
            for _i, _b in enumerate(blocks):
                if _b[0] == 'page':
                    _curp = int(_b[1])
                    continue
                if _b[0] in ('body', 'head') and _curp in table_pages:
                    blocks[_i] = ('table', _b[1], _b[2], _b[3])

        # 真表格替换：表格区域内的块换成 tablegrid（Word 真表格），
        # 表格外的文字照常。检测已在 [2.1/4] 完成（page_tabs）。
        if page_tabs:
            marks = {int(b[1]): i for i, b in enumerate(blocks) if b[0] == 'page'}
            _pnos = sorted(marks)
            _ins_pages = []
            for _k, _pno in enumerate(_pnos):
                tabs = page_tabs.get(_pno)
                if not tabs:
                    continue
                _i0 = marks[_pno]
                _i1 = marks[_pnos[_k + 1]] if _k + 1 < len(_pnos) else len(blocks)
                seg = blocks[_i0 + 1:_i1]
                out_seg, emitted = [], set()
                for _b in seg:
                    _by = _b[3] if len(_b) > 3 and \
                        isinstance(_b[3], (int, float)) else None
                    hit = None
                    if _by is not None and _b[0] not in ('image', 'tablepage',
                                                         'tablegrid'):
                        for _y0, _y1, _grid in tabs:
                            # 容差 100：表头行/首行的块起点可能略高于
                            # 区域上界（区域按行中心算），仍应归入表格
                            if _y0 - 100 <= _by <= _y1:
                                hit = (_y0, _y1, _grid)
                                break
                    if hit is not None:
                        if hit[0] not in emitted:
                            # 表格在原书上的左右边界：取该纵向区间内的行框，
                            # 导出时按这个宽度重建，表格不会撑出/缩进版心
                            _rows = [l for l in (pages.get(_pno) or [])
                                     if hit[0] - 20 <= l['y0'] <= hit[1] + 20]
                            _bb = None
                            if _rows:
                                _x0 = min(l['x0'] for l in _rows)
                                _wsum = sum((hit[2].get('widths') or [])
                                            if isinstance(hit[2], dict) else [])
                                _x1 = max(
                                    max(l['x1'] for l in _rows),
                                    (_x0 + _wsum) if _wsum > 20 else 0.0)
                                _bb = (_x0, hit[0], _x1, hit[1])
                            _tir = table_ir.TableIR.from_data(
                                hit[2], bbox=_bb, source='page')
                            out_seg.append(('tablegrid',
                                            json.dumps(_tir.to_data(),
                                                       ensure_ascii=False),
                                            [], hit[0], 0, _bb))
                            emitted.add(hit[0])
                            _ins_pages.append(_pno)
                        continue
                    out_seg.append(_b)
                blocks[_i0 + 1:_i1] = out_seg
                # 每页替换后块数会变（N 块 → 1 个表格），后续页的
                # 位置必须重新计算，否则会切错页段（表格丢半页）
                marks = {int(b[1]): i for i, b in enumerate(blocks)
                         if b[0] == 'page'}
            say(u'真表格插入页：%s' % str(sorted(set(_ins_pages))))

        _tab_pages = set(pre_tabs) & set(landscape_pages)
        if _tab_pages:
            _curp = 0
            _page_txt = {}
            for _b in blocks:
                if _b[0] == 'page':
                    _curp = int(_b[1])
                    continue
                if _curp in _tab_pages and _b[0] in ('body', 'head', 'index'):
                    _page_txt[_curp] = _page_txt.get(_curp, '') + (_b[1] or '')
            _keep_pages = {p for p, t in _page_txt.items() if len(t) < 180}
            if _keep_pages:
                _curp, _out = 0, []
                for _b in blocks:
                    if _b[0] == 'page':
                        _curp = int(_b[1])
                        _out.append(_b)
                        continue
                    if _curp in _keep_pages and _b[0] in ('body', 'head',
                                                          'index'):
                        _t = (_b[1] or '').lstrip()
                        if not re.match(u'^(表|Tab|图|Fig)', _t):
                            continue
                    _out.append(_b)
                blocks = _out
                marks = {int(b[1]): i for i, b in enumerate(blocks)
                         if b[0] == 'page'}

        # 插图：把原书里的图截下来，安插到 Word 中对应的文字位置
        #（默认开启；不改变每页分页与文字排版）
        if self.opts.get('images', True):
            say(u'[2.6/4] 插图提取与安插…')
            img_dir = os.path.join(self.work_dir, 'images')
            os.makedirs(img_dir, exist_ok=True)
            marks = {int(b[1]): i for i, b in enumerate(blocks) if b[0] == 'page'}
            pnos = sorted(marks)
            CAP = re.compile(u'^(图|插图|附图)\\s*[0-9一二三四五六七八九十]+')
            n_img = 0
            # 整本书只开一次 PDF：逐页重开上百 MB 的文件会白耗好几分钟
            _imgdoc = pymupdf.open(self.pdf_path)
            for k, pno in enumerate(pnos):
                if pno in snap_pages:
                    continue        # 整页截图的页不再单独提图
                if paper_mode and pno in paper_objects:
                    # 表格/插图已作为版面对象插进树里；再走块级插图
                    # 会重复，而且 frametree 没有 y 锚点，图会跑到页尾
                    continue
                i0 = marks[pno]
                i1 = marks[pnos[k + 1]] if k + 1 < len(pnos) else len(blocks)
                imgs = engine.extract_page_images(
                    self.pdf_path, pno, img_dir, doc=_imgdoc)
                if pno in crop_pages:
                    # 正文页里的表格区域：截成一张图，按原位置插回
                    _bb = crop_pages[pno]
                    try:
                        _tp = engine.crop_region(self.pdf_path, pno, _bb, img_dir)
                        imgs = list(imgs) + [(_bb[1], _tp, _bb)]
                    except Exception:
                        pass
                for _ei, _eb in enumerate(eq_crops.get(pno) or []):
                    try:
                        _ep = engine.crop_region(
                            self.pdf_path, pno, _eb, img_dir,
                            suffix='eq%02d' % _ei)
                        imgs = list(imgs) + [(_eb[1], _ep, _eb)]
                    except Exception:
                        pass
                if not imgs:
                    continue
                seg = blocks[i0 + 1:i1]
                for _item in imgs:
                    if len(_item) >= 3:
                        y0, path, bb = _item[0], _item[1], _item[2]
                    else:
                        y0, path = _item[0], _item[1]
                        bb = None
                    # 按原书上下位置安插：插到第一个 y 更靠下的文字块之前；
                    # 无坐标可依（如双栏页）时退回图注锚点，再退回页尾
                    pos = len(seg)
                    for k2, b2 in enumerate(seg):
                        by = b2[3] if len(b2) > 3 and \
                            isinstance(b2[3], (int, float)) else None
                        if by is not None and by > y0:
                            pos = k2
                            break
                    if pos == len(seg):
                        for k2, b2 in enumerate(seg):
                            if b2[0] == 'body' and \
                                    CAP.match((b2[1] or '').strip()):
                                pos = k2
                                break
                    seg.insert(pos, ('image', path, [], y0, 0, bb))
                blocks[i0 + 1:i1] = seg
                n_img += len(imgs)
                marks = {int(b[1]): kk for kk, b in enumerate(blocks) if b[0] == 'page'}
            _imgdoc.close()
            say(u'共安插插图 %d 幅' % n_img)

        align_mode = self.opts.get('align_mode', 'off')
        align_file = self.opts.get('align_file') or ''
        did_align = False
        if align_mode == 'auto':
            say(u'[3/4] 自动搜索权威译本…')
            from . import autofind
            samples = [b[1] for b in blocks
                       if b[0] in ('body',) and len(b[1]) > 40]
            text, url, score = autofind.find_authoritative_text(samples, log=say)
            if text:
                auto_file = os.path.join(self.work_dir, u'权威文本_自动.txt')
                os.makedirs(self.work_dir, exist_ok=True)
                with open(auto_file, 'w', encoding='utf-8') as f:
                    f.write(text)
                blocks = align.correct_blocks(blocks, auto_file)
                did_align = True
                say(u'[3/4] 已按自动找到的权威文本对齐（来源：%s）' % url)
            else:
                say(u'[3/4] 自动搜索未果，跳过核对（可改用手动选文件）')
        elif align_mode == 'manual' and align_file:
            say(u'[3/4] 与权威文本逐段对齐…')
            blocks = align.correct_blocks(blocks, align_file)
            did_align = True
        if not did_align:
            say(u'[3/4] 跳过权威对齐（未启用）')
            from .align import _simp_mask
            out = []
            for b in blocks:
                kind, text, mask = b[0], b[1], b[2]
                y0 = b[3] if len(b) > 3 else None
                sz = b[4] if len(b) > 4 else 0
                bb = b[5] if len(b) > 5 else None
                if kind in ('page', 'image', 'tablepage', 'tablegrid'):
                    out.append([kind, text, mask, True, 0, y0, sz, bb])
                else:
                    t2, m2 = _simp_mask(text, mask)
                    out.append([kind, t2, m2, False, 0, y0, sz, bb])
            blocks = out

        # 论文/期刊：第 1 页上半截（标题、摘要、作者、基金）是通栏，
        # 下半截正文才分两栏。在正文第一个"一、/1."小标题前插一个
        # 连续分节标记，导出时先单栏、到这里再切双栏。
        if paper_mode and paper_trees is None and columns == 2 and _all_two:
            _curp = 0
            _mark = None
            for _i, _b in enumerate(blocks):
                if _b[0] == 'page':
                    _curp = int(_b[1])
                    continue
                if _curp != 1 or _b[0] not in ('body', 'head'):
                    continue
                _t = (_b[1] or '').lstrip(u'　 ').strip()
                # 论文正文一般从中图分类号/文献标识码这一行之后开始；
                # 找不到元数据再退回"一、/1."小标题。
                _m = re.search(u'文献标识码[：:]\\s*[A-Z]\\s*', _t)
                if _m is None:
                    _m = re.search(u'中图分类号[：:]\\s*[A-Z0-9]+\\s*', _t)
                if _m and _m.end() < len(_t) - 5:
                    _pos = _m.end()
                else:
                    # 必须是在句首/标点后面的"一、"，不能在"统一、"
                    # 这种词中间下刀，否则会把摘要从中间切开。
                    _m2 = re.search(
                        u'(?:^|[。！？；：\\n])\\s*([一二三四五六七八九十]+、)',
                        _t)
                    if not _m2:
                        continue
                    _pos = _m2.start(1)
                if _pos <= 0:
                    _mark = _i
                else:
                    # 标题/摘要/元数据和正文被并成了一个块：在正文起点
                    # 切开，前半段留在单栏区，后半段进双栏。
                    _head = list(_b)
                    _tail = list(_b)
                    _head[1] = _t[:_pos].rstrip()
                    _tail[1] = _t[_pos:]
                    _mask = _b[2]
                    if isinstance(_mask, list) and len(_mask) == len(_t):
                        _head[2] = _mask[:_pos]
                        _tail[2] = _mask[_pos:]
                    blocks[_i:_i + 1] = [_head, _tail]
                    _mark = _i + 1
                break
            if _mark is not None:
                blocks.insert(_mark, ['cols2', '', [], True, 0, None, 0, None])
                say(u'论文版式：标题/摘要通栏，正文从双栏开始')
        say(u'[4/4] 导出 Word（按原书分页）…')
        # 文字层 span 的逐字样式（字号/字体/上标/下标）：导出时按块文本回查。
        # 没有 span 的扫描页返回空，走原来的整段样式。
        style_maps = layout.build_style_maps(pages)
        if style_maps:
            say(u'行内样式：%d 页文字层 span 已保留' % len(style_maps))
        # L3 锁行（试验）：只对"纯正文、无表/图/注/标题、单栏"的文字层页
        # 按原书视觉行逐行成段；文字仍可编辑/搜索，但原文断行被保留。
        locked_pages = {}
        if self.opts.get('layout_lock') and page_lines:
            _skip = (set(snap_pages) | set(crop_pages) | set(page_tabs)
                     | set(two_col_text) | set(table_pages))
            _bad_kinds = {'head', 'note', 'cite', 'tablegrid', 'tablepage',
                          'image', 'table', 'index'}
            _curp = 0
            _block_kinds = {}
            for _b in blocks:
                if _b[0] == 'page':
                    _curp = int(_b[1])
                else:
                    _block_kinds.setdefault(_curp, set()).add(_b[0])
            for _p, _ls in sorted(page_lines.items()):
                if _p in _skip or len(_ls) < 8:
                    continue
                if _block_kinds.get(_p, set()) & _bad_kinds:
                    continue
                if any(l.get('kind') != 'body' for l in _ls):
                    continue
                locked_pages[_p] = _ls
            if locked_pages:
                say(u'L3 锁行：%d 页按原书视觉行固定断行'
                    % len(locked_pages))
        # 按内容识别书末附录/注释/索引区起始页。注意：正文里单篇文章后的
        # "注释"小标题很常见，只有临近书末（最后 15%）的才算附录起点；
        # "人名索引/期刊索引"等无论在哪都是书末内容。
        appendix_from = 0
        INDEX_TITLES = (u'人名索引', u'期刊索引', u'事项索引', u'译后记')
        curp = 0
        last_page = max((int(b[1]) for b in blocks if b[0] == 'page'), default=0)
        for b in blocks:
            if b[0] == 'page':
                curp = int(b[1])
                continue
            t = (b[1] or '').strip()
            if len(t) <= 30:
                tt = t.rstrip(u'：:')
                # 注释标题有时与页眉合并成一行（"注释 （《毛泽东选集》…"）
                is_note_title = tt == u'注释' or tt.startswith(u'注释')
                if tt in INDEX_TITLES or (is_note_title and last_page
                                          and curp >= last_page * 0.85):
                    appendix_from = curp
                    break
        if appendix_from:
            say(u'检测到附录区起始于第 %d 页，其后按索引小字排版' % appendix_from)
        tot = sum(len(b[1]) for b in blocks if b[0] != 'page')
        title = self.opts.get('title', '')
        if not title:
            base = re.sub(u'\\s*\\(\\d+\\)\\s*$', '',
                          os.path.splitext(os.path.basename(self.pdf_path))[0]).strip()
            junk = (u'pdf生成', u'生成工具', u'pdf制作', u'scan', u'adobe',
                    u'microsoft', u'word', u'print')
            if meta_title and not any(j in meta_title.lower() for j in junk):
                title = meta_title
            elif re.match(u'^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$', base):
                title = u'未命名文档'
            else:
                title = base
        notes = [
            u'本文件由「书扫通 BookOCR」自动整理。',
            u'排版对照原书：每页起新页（Word 页序 = 原书页序），页首灰色小字为原书页码。',
        ]
        if n_scan == 0:
            notes.insert(0, u'本 PDF 自带文字层，文字系直接提取（非识别），与原文一致。')
        elif n_rich == 0:
            notes.insert(0, u'未经权威文本校正：全文为 OCR 识别结果（已三路交叉核对、可疑字标红），引用前请对照原书。')
        else:
            notes.insert(0, u'%d 页自带文字层直接提取，%d 页为 OCR 识别（可疑字标红）。' % (n_rich, n_scan))
        page_numbers = layout.printed_numbers(geo_pages)
        if paper_mode:
            # 期刊页眉常印"总第593期"，不能把它当页码。论文模式只认
            # 页顶/页底那些"纯数字"的短行。
            _pn2 = {}
            for _p, _ls in geo_pages.items():
                _c = [l for l in _ls
                      if re.match(u'^\\d{1,4}$', (l.get('text') or '').strip())
                      and (l['y0'] < 900 or l['y0'] > 4400)]
                if _c:
                    _c.sort(key=lambda l: l['y0'], reverse=True)
                    _pn2[_p] = int((_c[0].get('text') or '').strip())
            if len(_pn2) >= max(1, len(geo_pages) // 2):
                page_numbers = _pn2
        if page_numbers:
            say(u'页眉页码：已识别 %d 页的印刷页码' % len(page_numbers))
        else:
            say(u'页眉页码：没找到印刷页码，按 PDF 页序标注')
        page_sections = layout.detect_page_sections(geo_pages, page_numbers)
        if page_sections:
            say(u'页码分段：%d 页为独立编号段（如附录/参考文献），已加段名'
                % len(page_sections))
        n_bad = sum(sum(1 for c in (b[1] or '') if c in export.SUSPECT_CHARS)
                    for b in blocks
                    if b[0] not in ('page', 'image', 'tablepage', 'tablegrid'))
        if n_bad:
            say(u'文字层有 %d 处坏字符（PDF 字体缺映射，已标红，请对照原书）'
                % n_bad)
        # 量原书版式：页面大小、页边距、正文字号、行距、首行缩进。
        # Word 里按这一套 1:1 排，标尺读出来就和原书一样。
        geom = None
        try:
            import collections as _col
            _d = pymupdf.open(self.pdf_path)
            _sizes = _col.Counter((round(_pg.rect.width, 1),
                                   round(_pg.rect.height, 1)) for _pg in _d)
            _w, _h = _sizes.most_common(1)[0][0]
            _d.close()
            geom = layout.page_geometry(geo_pages, _w, _h)
            _ok, _why = layout.geom_plausible(geom)
            if not _ok:
                say(u'原书版式：量出来的框不合理（%s），改按默认 32 开排版' % _why)
                geom = None
            else:
                # 扫描件没有精确字号（是 OCR 框高估的），标题分档要更保守
                geom['exact_sizes'] = (n_scan == 0)
                _pos = layout.number_position(geo_pages, page_numbers)
                if _pos:
                    geom['number_y'] = _pos['y'] / 5000.0 * geom['page_h']
                    geom['number_right'] = _pos['x'] > 1700
                    geom['number_bottom'] = _pos['y'] > 2500
                    # 论文页底页码：正文常排到版心底，页码会压在最后一行上；
                    # 把它钉到页面最底边，和正文分开。
                    if paper_mode and geom.get('number_bottom'):
                        geom['number_y'] = geom['page_h'] - 14.0
                say(u'原书版式：页面 %.0f×%.0fpt（%.1f×%.1fcm），正文 %.1fpt，'
                    u'行距 %.1fpt，左右边距 %.1f/%.1fmm%s'
                    % (geom['page_w'], geom['page_h'],
                       geom['page_w'] / 28.3465, geom['page_h'] / 28.3465,
                       geom['body_size'], geom['pitch'],
                       geom['left'] / 2.83465, geom['right'] / 2.83465,
                       (u'，页码在%s' % (u'页底' if geom.get('number_bottom')
                                        else u'页顶'))
                       if _pos else u''))
        except Exception as _e:
            geom = None
            say(u'原书版式：量取失败，按默认 32 开排版（%s）' % _e)
        # 按源 PDF 的页面尺寸分组量几何：同尺寸的一组共用一份，
        # 不同尺寸（折页/插页）各自一份；横排页仍用竖排基准 + 横排节。
        _page_geoms = {}
        try:
            _gd = pymupdf.open(self.pdf_path)
            _groups = {}
            for _p in sorted(geo_pages):
                _r = _gd[_p - 1].rect
                _key = (round(_r.width, 1), round(_r.height, 1))
                _groups.setdefault(_key, []).append(_p)
            _gd.close()
            for _key, _ps in _groups.items():
                if len(_ps) < 2 or _key[0] > _key[1] + 1:
                    continue
                try:
                    _g2 = layout.page_geometry(
                        {p: geo_pages[p] for p in _ps}, _key[0], _key[1])
                    _ok2, _why2 = layout.geom_plausible(_g2)
                except Exception:
                    continue
                if not _ok2:
                    continue
                _g2['number_y'] = geom.get('number_y')
                _g2['number_right'] = geom.get('number_right')
                _g2['number_bottom'] = geom.get('number_bottom')
                _g2['exact_sizes'] = geom.get('exact_sizes')
                for _p in _ps:
                    _page_geoms[_p] = _g2
            if _page_geoms:
                say(u'按页几何：%d 页按源页尺寸分组量取'
                    % len(_page_geoms))
        except Exception as _e:
            say(u'按页几何：分组失败，沿用全局版式（%s）' % _e)
        # PageDoc 收口：所有块先按页归属，再交导出；后续按页几何/
        # 对象账本/页码验收都从这里取，不再靠全局 marks 偏移。
        _pb = pagedoc.PageBook.from_blocks(
            blocks, geometry=geom, labels=page_sections,
            geometry_by_page=_page_geoms)
        _perrs = _pb.validate()
        if _perrs:
            say(u'PageDoc 警告：%s' % u'；'.join(_perrs[:4]))
        else:
            say(u'PageDoc：%d 页，对象 %s'
                % (_pb.count(), _pb.object_count()))
        blocks = _pb.to_blocks()
        page_geometries = _pb.geometry_map()
        counts = export.export_docx(blocks, self.out_path, title=title, notes=notes,
                                    appendix_from=appendix_from,
                                    page_numbers=page_numbers,
                                    regions=regions, geom=geom,
                                    columns=columns, style_maps=style_maps, landscape_pages=landscape_pages, locked_pages=locked_pages, page_sections=page_sections, page_geometries=page_geometries)
        # 分页验收：用 Word 实际排版核对“一页原书 = 一页 Word”。
        # 估算难免有误差，凡是有页超出的，回缩后重排一次。
        over = export.measure_pagination(self.out_path)
        if over is not None:
            # 分页账本：页标数必须等于原书页数，Word 总页数也要相等；
            # 旧 bug 会重复插页标（6 页变 8 页），只查“每个页标占几页”
            # 查不出来，这里先把数量/序号挡住。
            _exp = sum(1 for b in blocks if b[0] == 'page')
            if over.marker_count != _exp:
                say(u'分页验收：页标 %d 个 / 原书 %d 页，Word 实际 %d 页'
                    u'——页标数不一致，请检查重复/丢失页'
                    % (over.marker_count, _exp, over.page_count))
            else:
                # Word 首次打开时偶尔会多一个尾页/空白页；只看页标账本，
                # 真正的溢出交给下面的 over 集合处理，避免假警报。
                say(u'分页验收：页标 %d 个，与原书一致'
                    % over.marker_count)
        if over is None:
            # 不能说“页数一致”——没验就是没验。Word 被占用/没装时会走到这里。
            say(u'分页验收：没能调用 Word 核对页数（Word 被占用或未安装），'
                u'请打开文件确认每页是否对应原书页')
        if over:
            # measure_pagination 返回的是页标序号（第几个页标），
            # 与 PDF 页序一一对应，直接用；不能拿印刷页码反查——
            # 前置页与正文可能都从 1 编号，页码会重复。
            over_pdf = set(over)
            say(u'分页验收：%d 页超出原书页数 [%s]，先微压行距重排…'
                % (len(over_pdf), _plist(over_pdf)))
            counts = export.export_docx(
                blocks, self.out_path, title=title, notes=notes,
                appendix_from=appendix_from,
                lead_scale={p: 0.94 for p in over_pdf},
                page_numbers=page_numbers, regions=regions, geom=geom,
                columns=columns, style_maps=style_maps, landscape_pages=landscape_pages, locked_pages=locked_pages, page_sections=page_sections, page_geometries=page_geometries)
            left = export.measure_pagination(self.out_path)
            if left:
                # 行距压了还不够，才动字号（字号一致比行距略紧重要）
                say(u'分页验收：仍有 %d 页偏长 [%s]，再收紧这几页字号…'
                    % (len(left), _plist(left)))
                counts = export.export_docx(
                    blocks, self.out_path, title=title, notes=notes,
                    appendix_from=appendix_from,
                    lead_scale={p: 0.90 for p in over_pdf},
                    page_scale={p: 0.9 for p in set(left)},
                    page_numbers=page_numbers, regions=regions, geom=geom,
                    columns=columns, style_maps=style_maps, landscape_pages=landscape_pages, locked_pages=locked_pages, page_sections=page_sections, page_geometries=page_geometries)
                left2 = export.measure_pagination(self.out_path)
                # 少数页面只差一两行时，0.90 还不够。继续按页追加压缩，
                # 否则这一两行会被挤成下一页，出现“整页只有一句话”的
                # 空白页。压到下限仍放不下，才走后面的行级/截图回退。
                if left2:
                    _fit_lead = {p: 0.90 for p in over_pdf}
                    _fit_page = {p: 0.90 for p in set(left)}
                    for _step in range(5):
                        if not left2:
                            break
                        for _p in left2:
                            _fit_lead[_p] = max(
                                0.62, _fit_lead.get(_p, 0.90) - 0.04)
                            _fit_page[_p] = max(
                                0.55, _fit_page.get(_p, 0.90) - 0.05)
                        say(u'分页验收：继续压缩偏长页 [%s]（第 %d 次）…'
                            % (_plist(left2), _step + 1))
                        counts = export.export_docx(
                            blocks, self.out_path, title=title, notes=notes,
                            appendix_from=appendix_from,
                            lead_scale=_fit_lead, page_scale=_fit_page,
                            page_numbers=page_numbers, regions=regions,
                            geom=geom, columns=columns,
                            style_maps=style_maps, landscape_pages=landscape_pages, locked_pages=locked_pages, page_sections=page_sections, page_geometries=page_geometries)
                        left2 = export.measure_pagination(self.out_path)
                    if not left2:
                        say(u'分页验收：偏长页已继续压缩，每页与原书对齐')
                if left2:
                    say(u'分页验收：收紧字号后仍有 %d 页偏长 [%s]'
                        % (len(left2), _plist(left2)))
                    # 先试“行级双栏”而不是直接截图：页 6 这类左右栏
                    # 不等高、参考文献挂在右栏的页面，行级表能保留文字。
                    _line_ok = False
                    if paper_mode and paper_trees is not None:
                        _line_map = {}
                        for _lb in zones.paper_line_blocks(
                                paper_trees, pages, columns=columns):
                            if _lb[0] == 'linepage':
                                try:
                                    _pn = int(json.loads(_lb[1])['page'])
                                    if _pn not in paper_objects:
                                        _line_map[_pn] = _lb
                                except Exception:
                                    pass
                        if all(_p in _line_map for _p in left2):
                            _lbb = []
                            _li = 0
                            while _li < len(blocks):
                                _b = blocks[_li]
                                if _b[0] != 'page':
                                    _lbb.append(_b)
                                    _li += 1
                                    continue
                                _p = int(_b[1])
                                _pb = list(_b)
                                while len(_pb) < 5:
                                    _pb.append(0)
                                _lbb.append(tuple(_pb))
                                _li += 1
                                if _p in left2:
                                    while _li < len(blocks) and \
                                            blocks[_li][0] != 'page':
                                        _li += 1
                                    _lbb.append(_line_map[_p])
                                else:
                                    while _li < len(blocks) and \
                                            blocks[_li][0] != 'page':
                                        _lbb.append(blocks[_li])
                                        _li += 1
                            counts = export.export_docx(
                                _lbb, self.out_path, title=title, notes=notes,
                                appendix_from=appendix_from,
                                page_numbers=page_numbers, regions=regions,
                                geom=geom, columns=columns,
                                style_maps=style_maps, landscape_pages=landscape_pages, locked_pages=locked_pages, page_sections=page_sections, page_geometries=page_geometries)
                            _left_line = export.measure_pagination(self.out_path)
                            if not _left_line:
                                _line_ok = True
                                say(u'分页验收：偏长页已改回行级双栏，'
                                    u'每页与原书对齐')
                    if not _line_ok and paper_mode and any(
                            b[0] in ('frametree', 'linepage') for b in blocks):
                        _fb_pages = set(left2)
                        _fb_dir = os.path.join(self.work_dir, 'fallbackpics')
                        os.makedirs(_fb_dir, exist_ok=True)
                        _fb_paths = {}
                        _fbdoc = pymupdf.open(self.pdf_path)
                        try:
                            for _p in sorted(_fb_pages):
                                try:
                                    _pg = _fbdoc[_p - 1]
                                    if geom:
                                        _gg = layout.page_geometry(
                                            geo_pages, _pg.rect.width,
                                            _pg.rect.height)
                                        _clip = pymupdf.Rect(
                                            _gg['left'], _gg['top'],
                                            _pg.rect.width - _gg['right'],
                                            _pg.rect.height - _gg['bottom'])
                                        _img = _pg.get_pixmap(dpi=150,
                                                              clip=_clip)
                                    else:
                                        _img = _pg.get_pixmap(dpi=150)
                                    _fp = os.path.join(
                                        _fb_dir, 'p%04d.png' % _p)
                                    _img.save(_fp)
                                    _fb_paths[_p] = _fp
                                except Exception:
                                    pass
                        finally:
                            _fbdoc.close()
                        if _fb_paths:
                            _fbb = []
                            _i = 0
                            while _i < len(blocks):
                                _b = blocks[_i]
                                if _b[0] != 'page':
                                    _fbb.append(_b)
                                    _i += 1
                                    continue
                                _p = int(_b[1])
                                _pb = list(_b)
                                while len(_pb) < 5:
                                    _pb.append(0)
                                _fbb.append(tuple(_pb))
                                _i += 1
                                if _p in _fb_paths:
                                    while _i < len(blocks) and \
                                            blocks[_i][0] != 'page':
                                        _i += 1
                                    _fbb.append(('tablepage', _fb_paths[_p],
                                                 [], 0, 0))
                                else:
                                    while _i < len(blocks) and \
                                            blocks[_i][0] != 'page':
                                        _fbb.append(blocks[_i])
                                        _i += 1
                            counts = export.export_docx(
                                _fbb, self.out_path, title=title, notes=notes,
                                appendix_from=appendix_from,
                                page_numbers=page_numbers, regions=regions,
                                geom=geom, columns=columns,
                                style_maps=style_maps, landscape_pages=landscape_pages, locked_pages=locked_pages, page_sections=page_sections, page_geometries=page_geometries)
                            left3 = export.measure_pagination(self.out_path)
                            if left3:
                                say(u'分页验收：仍有 %d 页偏长 '
                                    u'（已改为版心截图）' % len(left3))
                            else:
                                say(u'分页验收：偏长页已改为版心截图，'
                                    u'每页与原书对齐')
                        else:
                            say(u'分页验收：仍有 %d 页偏长（已尽量压缩）'
                                % len(left2))
                    elif not _line_ok:
                        say(u'分页验收：仍有 %d 页偏长（已尽量压缩）'
                            % len(left2))
                else:
                    say(u'分页验收：每页已与原书对齐')
            else:
                say(u'分页验收：每页已与原书对齐')
        elif over is not None:
            say(u'分页验收：页数与原书一致')
        say(u'完成：%s（%s）' % (self.out_path, dict(counts)))
        # 自动做页间接缝检验（句子拆断/句首标点/汉字间空格），结果写入日志
        from . import checker
        for ln in checker.check_docx_lines(self.out_path):
            say(ln)
        return True
