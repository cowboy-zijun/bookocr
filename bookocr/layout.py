# -*- coding: utf-8 -*-
"""版面重建。

1. 把 OCR 行按版式拼成段落（首行缩进、短行结尾、居中标题、目录页等启发式）。
2. 双栏页检测：沿中缝切分页面，左右两栏各自重新识别，再按列序输出，
   并把跨行条目合并成完整条目。
"""
import collections
import re

import numpy as np
import pymupdf

CJK = re.compile(u'[㐀-鿿]')
ONLY_NUM = re.compile(u'^[.\\s·\\-—－―~～\\[\\]\\(\\)（）0-9IVXLCM就]*$')
END_PUNCT = re.compile(u'[。，、；：！？”』）】]$')

PAGE_H = 5000.0          # 200dpi 渲染下的页高
TOP_ZONE = PAGE_H * 0.15
BOT_ZONE = PAGE_H * 0.93


def norm(s):
    return re.sub(u'[^\\u3400-\\u9fffA-Za-z0-9]', '', s)


def group_rows(lines):
    """同一视觉行上的检测框合并。"""
    rows = []
    for l in sorted(lines, key=lambda x: x['y0']):
        for row in rows:
            ry0 = min(x['y0'] for x in row)
            ry1 = max(x['y1'] for x in row)
            ov = min(l['y1'], ry1) - max(l['y0'], ry0)
            h = min(l['y1'] - l['y0'], ry1 - ry0)
            if h > 0 and ov > 0.45 * h:
                row.append(l)
                break
        else:
            rows.append([l])
    rows.sort(key=lambda r: min(x['y0'] for x in r))
    for r in rows:
        r.sort(key=lambda x: x['x0'])
    return rows


def median_char_w(lines):
    ws = sorted((l['x1'] - l['x0']) / len(norm(l['text']))
                for l in lines if len(norm(l['text'])) >= 12)
    return ws[len(ws) // 2] if ws else 78.0


def mark_mask(l):
    """三路识别无多数一致的字 = 可疑。

    只标汉字和数字：标点符号的分歧多为渲染差异造成的假警报。
    注意：三路一致地错成同一个字时无法标记（无外部参照的固有限制）。
    """
    alts = l.get('alts') or []
    t = l['text']
    if len(alts) < 2:
        return [False] * len(t)
    m = []
    for i in range(len(t)):
        if not (CJK.match(t[i]) or t[i].isdigit()):
            m.append(False)
            continue
        votes = [a[i] for a in alts if i < len(a)]
        if len(votes) < 2:
            m.append(False)
            continue
        top = collections.Counter(votes).most_common()
        m.append(top[0][1] < 2 or (len(top) > 1 and top[0][1] == top[1][1]))
    return m


def _dedupe_row(row):
    """去掉同一视觉行里被 OCR 重复识别出来的窄框。

    扫描件偶尔会在正文行左侧再给一个很窄的框，里面的文字却是整行的
    开头部分（第 36 页第二条脚注就出现过）。这种框和长框横向重叠、
    文字又完全被长框包含，直接丢弃，否则导出时同一句会重复两遍。
    """
    kept = []
    for i, l in enumerate(row):
        t = norm(l.get('text') or '')
        if len(t) < 4:
            kept.append(l)
            continue
        dup = False
        for j, o in enumerate(row):
            if i == j:
                continue
            ot = norm(o.get('text') or '')
            if t not in ot:
                continue
            lw = l['x1'] - l['x0']
            ow = o['x1'] - o['x0']
            # 保留文字更全、框更宽的那一条；文字和框都一样时保留靠前的
            if not (len(ot) > len(t) or ow > lw + 1.0
                    or (abs(ow - lw) <= 1.0 and j < i)):
                continue
            if min(l['x1'], o['x1']) - max(l['x0'], o['x0']) > 0:
                dup = True
                break
        if not dup:
            kept.append(l)
    return kept or row


def merge_row(row, cw):
    txt, mask = '', []
    prev_x1 = None
    for l in _dedupe_row(row):
        t = l['text']
        if not t.strip():
            continue
        m = mark_mask(l)
        ct = t
        cm = [m[i] if i < len(m) else False for i in range(len(ct))]
        ct = ct.strip()
        cm = cm[-len(ct):] if len(ct) else []
        if not ct:
            continue
        if prev_x1 is not None and (l['x0'] - prev_x1) > cw * 1.5:
            txt += ' '
            mask.append(False)
        txt += ct
        mask.extend(cm)
        prev_x1 = l['x1']
    # 汉字之间的空格：
    # - 单个空格：多半是 PDF 文字层/列间隙的噪声，删掉；
    # - 连续两个及以上：常是“章节号 + 标题”或行内小标题与正文之间的
    #   有意分隔，保留一个，否则“2.3.3 参数优化与模型训练 由于…”会被
    #   粘成“训练由于…”，编号和标题就看不出来了。
    out_c, out_m = [], []
    i = 0
    while i < len(txt):
        ch = txt[i]
        if ch == ' ':
            j = i
            while j < len(txt) and txt[j] == ' ':
                j += 1
            pv = out_c[-1] if out_c else ''
            nx = txt[j] if j < len(txt) else ''
            if pv and CJK.match(pv) and nx and CJK.match(nx):
                if j - i >= 2:
                    out_c.append(' ')
                    out_m.append(False)
                i = j
                continue
            out_c.append(' ')
            out_m.append(False)
            i = j
            continue
        out_c.append(ch)
        out_m.append(mask[i] if i < len(mask) else False)
        i += 1
    return ''.join(out_c), out_m


def build_blocks(pages, two_col_pages=None, with_lines=False):
    """整本书：OCR 行 -> 段落块。返回 [kind, text, mask]，kind ∈
    page/head/body/toc。

    two_col_pages：已按栏重排的双栏页集合（这些页的行是"左栏接右栏"，
    行尾不对齐右边界，不能用短行启发式切段）。
    with_lines=True 时额外返回 {页号: [视觉行]}，供 L3 锁行使用。
    """
    two_col_pages = two_col_pages or set()
    # 所有页的坐标都已换算到 3400×5000（见 engine.extract_text_pages / OCR）。
    # 不能拿"文字最低点"当页高：没有页脚的页会把它算成 4380，
    # 页眉带跟着缩水，同一本书有的页认得出页眉、有的认不出。
    page_h = 5000.0
    # 页眉带要随书变：很多书的页眉印在页面 12%—15% 处（如毛选在 13.6%），
    # 固定 450 的窄条会整条漏掉，页眉就混进正文、还被当成标题。
    HEAD_ZONE = max(450.0, page_h * 0.18)
    # 判定和计数必须用同一条带：放宽到 1.6 倍会把目录页开头几行正文
    # （正好与页眉同字）当成页眉删掉。页眉天生比正文靠上，这条带够用。
    HEAD_MATCH = HEAD_ZONE
    page_w = max((l['x1'] for ls in pages.values() for l in ls), default=3400.0)
    body_size = doc_body_size(pages)
    small_thr = body_size * 0.8 if body_size else 0.0

    # 第一遍：每页合并成视觉行（页眉常在 PDF 里被拆成多个小片段，
    # 必须先合并成行再判断，否则"年第+期+华正学"这类页眉永远认不出）
    page_rows = {}
    page_fn = {}
    for p, ls in pages.items():
        fn_y = footnote_start(ls, body_size,
                              marker_rule=(p not in two_col_pages))
        page_fn[p] = fn_y
        # 只丢页面底部的纯数字页脚；分隔线伪行（sep）不是内容，丢弃
        ls = [l for l in ls
              if not l.get('sep')
              and not (l['y0'] > BOT_ZONE and ONLY_NUM.match(l['text']))]
        cw = median_char_w(ls) if ls else 78.0
        rows = []
        for r in group_rows(ls):
            t, m = merge_row(r, cw)
            # 纯标点行（PDF 文字层常把引号/逗号单独排成一行）是噪声，丢弃
            if t and norm(t):
                rows.append((t, m, r))
        page_rows[p] = rows

    def row_sz(r):
        ss = sorted(l.get('size') or 0 for l in r if l.get('size'))
        return ss[len(ss) // 2] if ss else 0.0

    # 每页正文块的顶端：与正文字号相当的长行里最靠上的一条。页眉排在
    # 正文块之上；只选了少量页时，奇偶页两种页眉可能各只出现一次，
    # 跨页重复计数认不出来，靠这个位置关系兜底。
    body_top_by_page = {}
    for p, rows in page_rows.items():
        tops = []
        for t, m, r in rows:
            if len(norm(t)) < 8:
                continue
            sz = row_sz(r)
            if body_size and sz and sz < body_size * 0.92:
                continue
            tops.append(min(l['y0'] for l in r))
        if tops:
            body_top_by_page[p] = min(tops)
    _bt = sorted(body_top_by_page.values())
    body_top = _bt[len(_bt) // 2] if _bt else 0.0

    # 页眉判定：只统计页面最顶部窄条（页高前 9%）里跨页重复的"行"。
    def head_norm(t):
        # 去掉数字：页眉常带页码（不同页数字不同），去数字后才能
        # 跨页比对出"同一页眉"
        return re.sub(u'\\d+', '', norm(t))

    top_counter = collections.Counter()
    small_counter = collections.Counter()
    for p, rows in page_rows.items():
        for t, m, r in rows:
            y0 = min(l['y0'] for l in r)
            if y0 < HEAD_ZONE:
                nt = head_norm(t)
                if len(nt) >= 2:
                    top_counter[nt] += 1
                    # 又靠顶、又比正文小的行，出现两次就基本能断定是页眉。
                    # 大书里"仅书名版"的页眉（目录页那种）常只出现两三次，
                    # 按 3 次的门槛会漏掉，目录页就会混进假的"条目"。
                    ss = sorted(l.get('size') or 0 for l in r if l.get('size'))
                    sz = ss[len(ss) // 2] if ss else 0.0
                    if body_size and sz and sz < body_size * 0.85 \
                            and y0 < HEAD_ZONE * 0.9:
                        small_counter[nt] += 1
    # 小册子（期刊论文等只有几页）页眉出现 2 次即可认定；大书仍要 3 次
    need = 2 if len(pages) <= 12 else 3
    head_keys = {t for t, c in top_counter.items() if c >= need}
    head_keys |= {t for t, c in small_counter.items() if c >= 2}
    for t, c in top_counter.items():
        if c >= need:
            for t2 in top_counter:
                if t2 != t and len(t2) >= 5 and t2 in t:
                    head_keys.add(t2)

    def row_is_head(t, r, p=None):
        nt = norm(t)
        if not nt:
            return True
        if re.match(r'^\d{1,4}$', nt):
            return True          # 纯数字行=页码，无论出现在哪都丢
        y0 = min(l['y0'] for l in r)
        # 页眉几乎都排得比正文小；章首页的大标题即使和页眉同字也不能删
        small = True
        if body_size:
            rz = row_sz(r)
            small = (not rz) or rz < body_size * 0.95
        nt2 = head_norm(t)
        # 与页眉完全一致：只在页顶附近才算。正文里的章标题常与页眉文字
        # 完全相同（页眉就印着章名），以前“无论位置”会把目录里的章标题、
        # 正文开头的章标题当成页眉删掉，字号层级就乱了。
        # 完全等于页眉文字的行：只要在页面最顶部（前 11%），字号和正文
        # 一样大也认。有的书页眉印得和正文一样大（"090中国扶贫脱贫史"），
        # 只按字号小判断会漏掉，页眉就混进每一页的正文开头。
        if nt2 in head_keys and y0 < HEAD_MATCH \
                and (small or y0 < HEAD_ZONE * 0.6):
            return True
        # 兜底：比正文块顶端还高、字号比正文小、又不带句读的短行，就是页眉。
        # 只选了几页时（奇偶页两种页眉各出现一次），跨页重复计数失效，
        # 没有这条页眉就会混进正文开头。
        bt = body_top_by_page.get(p, body_top)
        # OCR 的字号估计经常抖动：正文第一行可能被估得偏小，正文块
        # 顶端又被后面的粗体行抬高，于是正文第一行会被这条“兜底”
        # 误判成页眉，整句被塞进灰色页眉里（第 34 页就丢过一句）。
        # 兜底只认真正贴页顶的短行，不再跟着 bt 无限放宽。
        if small and bt and y0 < bt * 0.7 and y0 < HEAD_ZONE * 0.55 \
                and len(nt2) <= 24 \
                and not re.search(u'[。！？；：]', t):
            return True
        # 包含关系只在页顶窄条内成立（避免正文句子被误删）
        if y0 >= HEAD_ZONE or not small:
            return False
        return any(len(nt2) >= 4 and nt2 in k for k in head_keys)

    blocks = []
    body_lines = {}
    for p in sorted(pages):
        rows_all = page_rows.get(p) or []
        # 目录页的“目录”二字、章名、条目都要保留，但页眉/页码照样得去掉
        # ——很多书的目录页顶部印着“目 录 ３”。row_is_head 要求字号比正文小，
        # 大号的“目录”二字不会被误删。
        head_rows = [x for x in rows_all if row_is_head(x[0], x[2], p)]
        rows = [x for x in rows_all if not row_is_head(x[0], x[2], p)]
        if len(rows) < 2:
            rows = rows_all
            head_rows = []
        if not rows:
            # 空白页 / OCR 失败的页：页码标记也要保留，
            # 否则这一页从文档里消失，后面所有页码全部前移。
            blocks.append(('page', str(p), None, None))
            continue
        blocks.append(('page', str(p), None, None))
        # 页眉块：紧随页码标记、正文之前；导出时灰色小字，与正文区分
        if head_rows:
            ht = ' '.join(x[0] for x in head_rows)
            hm = [b for x in head_rows for b in x[1]]
            hs = sorted(l.get('size') or 0 for x in head_rows for l in x[2]
                        if l.get('size'))
            blocks.append(('header', ht, hm,
                           min(min(l['y0'] for l in x[2]) for x in head_rows),
                           hs[len(hs) // 2] if hs else 0))
        body = [l for x in rows for l in x[2]]
        cw = median_char_w(body) if body else 78.0
        lefts = collections.Counter(round(l['x0'] / cw) for l in body if len(norm(l['text'])) >= 10)
        margin = lefts.most_common(1)[0][0] * cw if lefts else 0.0
        # 右边界取"行尾分布"的 p90——max 会被页眉/少量长行拉偏，
        # 导致正常行全被判成"短行"、每行一段
        ends = sorted(r[-1]['x1'] for t, m, r in rows)
        right = ends[min(len(ends) - 1, int(len(ends) * 0.9))] if ends else page_w
        # 目录页：大量"序号、标题"条目（如 1、xxx）或带引导点/页码的条目
        toclike = len(rows) >= 8 and sum(
            1 for t, m, r in rows
            if '·' in t or re.search(u'\\d{1,4}$', t) or re.match(u'^\\d{1,3}[、.．]', t)
        ) >= max(5, len(rows) * 0.5)
        # 句末标点：缩进切段只在上一行"说完了"时成立——长引文/块引用
        # 的每行都带缩进，若见缩进就切会把一句引文拆成每行一段
        SENT_END_CH = tuple(u'。！？：…”』」）】.!?:"\')')
        cur, prev, prev_t, prev_len, out_rows = [], None, '', 0, []
        prev_size = None
        prev_y1 = None
        fn_y = page_fn.get(p)
        _lh = _median_line_h([l for x in rows for l in x[2]])
        for t, m, r in rows:
            ry0 = min(l['y0'] for l in r)
            ry1 = max(l['y1'] for l in r)
            in_note = fn_y is not None and ry0 >= fn_y
            cur_in_note = bool(cur) and fn_y is not None and \
                min(l['y0'] for x in cur for l in x[3]) >= fn_y
            # 脚注区里：换注号（①→②）或明显空白处才断段。字号抖动、
            # 续行缩进、上一行偏短都不能把一条注文拆成两三段。
            if in_note and cur_in_note:
                new_note = bool(FOOT_MARK.match(t.strip()))
                big_gap = bool(prev_y1) and (ry0 - prev_y1) > _lh * 1.15
                if new_note or big_gap:
                    out_rows.append(cur)
                    cur = []
            # 字号突变处强制断段：正文→表格/脚注、正文→标题等不同层级
            # 的文字不能并成一段（表格单元格字号通常明显小于正文）
            rsz = [l.get('size') or 0 for l in r if l.get('size')]
            rsize = sorted(rsz)[len(rsz) // 2] if rsz else 0
            if not (in_note and cur_in_note) and cur and prev_size and rsize and \
                    abs(rsize - prev_size) > max(prev_size, rsize) * 0.18:
                out_rows.append(cur)
                cur = []
            if rsize:
                prev_size = rsize
            # 正文与脚注区的交界强制断段（否则脚注会被并进上一段正文）
            if page_fn.get(p) is not None and cur:
                cur_above = min(l['y0'] for x in cur for l in x[3]) \
                    < page_fn[p]
                if cur_above != (ry0 < page_fn[p]):
                    out_rows.append(cur)
                    cur = []
            x0 = r[0]['x0']
            prev_done = (not prev_t) or prev_t.rstrip().endswith(SENT_END_CH)
            indented = (x0 - margin) > cw * 1.2 and prev_done
            cur_len = r[-1]['x1'] - r[0]['x0']
            # 上一行明显短 → 通常是一段结束（或标题行）。但长引文每行
            # 都短且等宽，不能因此逐行拆段——所以要求"上句已说完"或
            # "当前行明显变长（标题后面接正文）"
            grew = prev_len and cur_len > prev_len * 1.35 + cw
            short_prev = (p not in two_col_pages and prev is not None
                          and (right - prev) > cw * 1.6
                          and (prev_done or grew))
            centered = (x0 - margin) > cw * 2.0 and (right - r[-1]['x1']) > cw * 2.0
            # 篇章扉页：整页只有一两行、字号明显大于正文（"第一次国内革命
            # 战争时期"）。这种页的字距常被 PDF 排得散开，居中判定不可靠，
            # 直接用"稀疏 + 大字号"认标题。
            sparse_title = (len(rows) <= 2 and rsize and body_size
                            and rsize > body_size * 1.25
                            and len(t) <= 30 and not END_PUNCT.search(t))
            # 左对齐的编号小节标题（"1. 实验目的""2. 实验原理"）字号
            # 常与正文一样，居中判定抓不到；短、无句读、编号后接汉字
            # 就单独成段，避免和上一段/下一段粘成一句。
            numbered = bool(re.match(
                u'^\\s*\\d{1,2}(?:\\.\\d{1,2})*[.、]\\s*[\\u4e00-\\u9fffA-Za-z]',
                t)) and len(t) <= 30 and not END_PUNCT.search(t)
            heading = (centered and len(t) <= 22 and not END_PUNCT.search(t)) \
                or (toclike and len(t) <= 30) or sparse_title or numbered
            # 右对齐的短行（引文出处“《我的奋斗》第385页”、署名等）必须
            # 单独成段。它们通常没有句末标点，若按普通正文处理，会和下一
            # 段正文并成一句（“《我的奋斗》第385页希特勒很快……”）。
            right_cite = (not heading and len(t) <= 40
                          and t.lstrip().startswith(u'《')
                          and (x0 - margin) > cw * 6
                          and (right - r[-1]['x1']) < cw * 4)
            # 参考文献/注释标题必须单独起段：期刊里它常常紧跟在正文
            # 末句后面，若并成一段，后面整块参考文献都会被当正文。
            if cur and re.match(u'^(参考文献|注释|附注)[：:]?\\s*', t):
                out_rows.append(cur)
                cur = []
            if toclike:
                out_rows.append([(t, m, 'toc', r)])
                continue
            # 同一视觉行被大空隙切成多块 → 表格行。表格行必须和上面的
            # 表题分开成段，否则表题和整张表并成一个块，导出时表格替换
            # 只认块首坐标，表题会连同旧表格文字一起留下，出现"表格
            # 重建了、上面还糊着一份原表"的双份。
            _rx = sorted(r, key=lambda l: l['x0'])
            row_gap = 0.0
            for _a, _b in zip(_rx, _rx[1:]):
                row_gap = max(row_gap, _b['x0'] - _a['x1'])
            if len(_rx) >= 2 and row_gap > cw * 2.0 and cur:
                out_rows.append(cur)
                cur = []
            if right_cite and cur:
                out_rows.append(cur)
                cur = []
            if (indented or short_prev) and cur \
                    and not (in_note and cur_in_note):
                out_rows.append(cur)
                cur = []
            if heading:
                if cur:
                    out_rows.append(cur)
                    cur = []
                out_rows.append([(t, m, 'head', r)])
            elif right_cite:
                out_rows.append([(t, m, 'cite', r)])
            else:
                cur.append((t, m, 'body', r))
            prev = r[-1]['x1']
            prev_t = t
            prev_len = cur_len
            prev_y1 = ry1
        if cur:
            out_rows.append(cur)
        # 换行产物合并：下一段以 ，。、； 等标点开头 → 是上一段的延续
        CONT = re.compile(u'^[，。、；：]')
        merged_rows = []
        for g in out_rows:
            if (merged_rows and g and merged_rows[-1]
                    and g[0][2] == 'body' and merged_rows[-1][0][2] == 'body'
                    and CONT.match(g[0][0].lstrip(u'　 '))):
                merged_rows[-1].extend(g)
            else:
                merged_rows.append(g)
        out_rows = merged_rows
        for g in out_rows:
            # 脚注/参考文献：优先用 footnote_start 判定的"页底脚注区"；
            # 兜底再用小字号（只在页面有正常正文时启用——整页小字的
            # 附表页不能把小字当脚注）
            kind = g[0][2]
            if kind == 'body':
                gy = min(l['y0'] for x in g for l in x[3])
                in_zone = page_fn.get(p) is not None and gy >= page_fn[p]
                small = False
                # 双栏页的脚注常与正文同高并排在不同栏，纵向判定不可靠，
                # 用字号兜底；单栏页只有存在脚注区时才用小字规则
                if small_thr and (page_fn.get(p) is not None
                                  or p in two_col_pages):
                    gs = [l.get('size') or 0 for x in g for l in x[3]
                          if l.get('size')]
                    small = bool(gs) and \
                        sorted(gs)[len(gs) // 2] < small_thr
                if in_zone or small:
                    # 页顶的小字是报头/页眉残留（如期刊刊头），按页眉样式
                    kind = 'header' if gy < HEAD_ZONE else 'note'
            # 块携带所在行 y0：插图按原书上下位置安插用
            ss = sorted(l.get('size') or 0 for x in g for l in x[3]
                        if l.get('size'))
            _bid = len(blocks)
            blocks.append((kind,
                           ''.join(x[0] for x in g),
                           [b for x in g for b in x[1]],
                           min(l['y0'] for x in g for l in x[3]),
                           ss[len(ss) // 2] if ss else 0))
            _pl = body_lines.setdefault(p, [])
            for x in g:
                _r = x[3]
                if not _r:
                    continue
                _x0 = min(l['x0'] for l in _r)
                _x1 = max(l['x1'] for l in _r)
                _y0 = min(l['y0'] for l in _r)
                _sz = sorted(l.get('size') or 0 for l in _r
                             if l.get('size'))
                _pl.append({'text': x[0], 'mask': list(x[1] or []),
                            'kind': kind, 'bid': _bid,
                            'x0': _x0, 'x1': _x1,
                            'y0': _y0,
                            'size': _sz[len(_sz) // 2] if _sz else 0})
    # 跨页延续处理（原书排版惯例：段落跨页时，下一页的开头只是接续，
    # 不是新段，因此不缩进；行首也绝不出现 ，。、； 这类标点）。
    # 做法：①句首标点挪回上一段末尾；②无法挪移的（句子中间断开）以及
    # 挪移后的剩余段，加延续标记 （导出时不缩进）。
    CONT = u''
    PUNCT = u'，。、；：？！'
    SENT_END = (u'。', u'！', u'？', u'：')

    def _head_like(t):
        # 短行且没有句读——章名/标题/书名这类，不参与“跨页续段”，
        # 否则“第二章”会把下一行的章名当成续行，字号和层级一起丢掉。
        s = (t or '').strip()
        return 0 < len(s) <= 24 and not re.search(u'[。！？：；，]', s)

    out = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b[0] not in ('body', 'index'):
            out.append(b)
            i += 1
            continue
        # 找下一个正文块：跳过 page 标记与页眉/图片块（不参与续段配对）
        j = i + 1
        while j < len(blocks) and blocks[j][0] in ('page', 'header', 'image'):
            j += 1
        nxt = None
        if j < len(blocks) and blocks[j][0] in ('body', 'index'):
            nxt = blocks[j]
        if nxt is not None:
            t2 = nxt[1]
            strip = len(t2) - len(t2.lstrip(PUNCT))
            if strip and not b[1].rstrip().endswith(SENT_END) \
                    and not (_head_like(b[1]) or _head_like(t2)):
                # 句首标点挪回本段末尾；剩余部分原位改写为续段。循环不
                # 跳过它——它会作为"上一段"继续处理，续段链才不断。
                moved = t2[:strip]
                out.append((b[0], b[1] + moved, b[2] + [False] * strip, b[3],
                            b[4] if len(b) > 4 else 0))
                rest = t2[strip:]
                m2 = nxt[2][strip:] if nxt[2] else [False] * len(rest)
                blocks[j] = (nxt[0], CONT + rest, m2, nxt[3],
                             nxt[4] if len(nxt) > 4 else 0)
                i += 1
                continue
            if not b[1].rstrip().endswith(SENT_END) \
                    and not t2.startswith(PUNCT) \
                    and not (_head_like(b[1]) or _head_like(t2)):
                # 句子在页间断开：接续段加标记，不缩进
                out.append(b)
                blocks[j] = (nxt[0], CONT + t2,
                             nxt[2] if nxt[2] else [False] * len(t2),
                             nxt[3], nxt[4] if len(nxt) > 4 else 0)
                i += 1
                continue
        out.append(b)
        i += 1
    return (out, body_lines) if with_lines else out


# ---------------------------------------------------------------- 脚注
FOOT_MARK = re.compile(
    u'^[①-⑳❶-❿➀-➉㈠-㈩⑴-⒇⒈-⒛]'                 # 圈号 ① ⑴ ⒈ …
    u'|^[\\[〔【(（]\\s*\\d{1,3}\\s*[\\]〕】)）]'      # [1] 〔1〕 （1）
    u'|^\\d{1,3}(?=[A-Za-z\\u3400-\\u9fff])'          # 13See / 3参见（数字紧接文字）
    u'|^[*†‡§¶]+\\s*'                                  # * † ‡
    u'|^注\\s*[:：]|^Source[s]?\\s*[:：]'
    u'|^Notes?\\s*[:：]')
SEP_TEXT = re.compile(u'^[-—_–—=·.]{4,}$')          # 文字型分隔线


def _median_line_h(lines):
    hs = sorted(l['y1'] - l['y0'] for l in lines
                if (l.get('y1') or 0) > (l.get('y0') or 0))
    return hs[len(hs) // 2] if hs else 60.0


def footnote_start(lines, body_size=None, marker_rule=True):
    """返回脚注区起始 y（该 y 以下为脚注/参考文献/页下注）；无则 None。

    归纳的脚注公共特征（按可靠度排序）：
    1) 位于正文末行之下（页面底部），且后面没有正文；
    2) 字号明显小于正文（页脚小字）；
    3) 行首有注号：①⑴⒈[1]〔1〕*(†) 或 "13See" 这类数字紧接文字；
    4) 上方有分隔线（文字型 —— 或 PDF 里的横线，后者由 engine 以
       sep 行注入）；
    5) 内容是成段的说明/引文，不是表格（行内不被切成多列）。
    整页都是小字的页（附录整页统计表）没有正常字号正文 → 不判脚注。
    """
    if not lines:
        return None
    if not body_size:
        ss = sorted(l.get('size') or 0 for l in lines if l.get('size'))
        body_size = ss[len(ss) // 2] if ss else 0.0
    # 扫描件 OCR 的字号是框高估的：真实页下注常被估成正文的 88%—95%，
    # 85% 的门槛会把它们当正文。这里放宽到 92%，后面还有“注号 + 页底空行”
    # 与“内容形态”两道守卫，不会因为放宽就大面积误判。
    small_thr = body_size * 0.92 if body_size else 0.0
    page_h = max((l['y1'] for l in lines), default=5000.0)

    def _pagenum(l):
        t = norm(l.get('text') or '')
        return bool(t) and t.isdigit() and len(t) <= 4 \
            and l['y0'] > page_h * 0.85

    normal = [l for l in lines
              if not l.get('sep') and not _pagenum(l)
              and (not small_thr or (l.get('size') or 0) >= small_thr)]
    if len(normal) < 5:
        return None                    # 全小字页（附表）不判脚注
    line_h = _median_line_h(lines)

    # 扫描件的 OCR 字号噪声很大：脚注和正文常被估成同一号（25/26），
    # 只按“小字”永远找不到注文，脚注就被并进正文最后一段。这里再用
    # 版面结构兜底：页底、行首有注号、且与上一行之间有明显空行。
    # 真正的脚注区总有这道空白（原书用来和正文隔开），正文换段没有
    # 这么大的行距；数字开头的正文（如“19世纪……”）在中上部不认。
    plain = [l for l in lines if not l.get('sep') and not _pagenum(l)]
    if marker_rule and len(plain) >= 6:
        tops = sorted(l['y0'] for l in plain)
        gaps = sorted(b - a for a, b in zip(tops, tops[1:]) if b > a)
        pitch = gaps[len(gaps) // 2] if gaps else line_h * 1.5
        for i, l in enumerate(plain):
            if i == 0:
                continue
            t = (l.get('text') or '').strip()
            if not FOOT_MARK.match(t):
                continue
            # “13See”这类数字紧接文字的注号在正文中也会出现，要求更靠页底
            digitish = bool(re.match(
                u'^\\d{1,3}(?=[A-Za-z\\u3400-\\u9fff])', t))
            if l['y0'] < page_h * (0.72 if digitish else 0.55):
                continue
            prev = plain[i - 1]
            # 注文区的空白行距至少是正常行距的 1.2 倍；普通换段远小于此。
            # 扫描件 OCR 的 y 框会抖动，1.35 倍常把贴得很紧的页下注漏掉。
            if l['y0'] - prev['y1'] < max(line_h * 1.2, pitch * 0.70):
                continue
            return l['y0']

    body_end = max(l['y1'] for l in normal)

    cand = []
    for l in lines:
        if l.get('sep') or _pagenum(l):
            continue
        if l['y0'] < body_end - line_h * 0.3:
            continue
        t = (l.get('text') or '').strip()
        is_small = small_thr and (l.get('size') or 0) < small_thr
        if is_small or FOOT_MARK.match(t):
            cand.append(l)
    if not cand:
        return None
    has_mark = any(FOOT_MARK.match((l.get('text') or '').strip())
                   for l in cand)
    if len(cand) < 2 and not has_mark:
        return None

    # 表格守卫：脚注是成段文字（一行 1-2 段）；行内有 >=3 段的是表格
    cw = sorted((f['x1'] - f['x0']) / max(1, len(norm(f['text'])))
                for l in cand for f in (l.get('spans') or [])
                if norm(f.get('text', '')))
    char_w = cw[len(cw) // 2] if cw else 60.0

    def seg_count(l):
        sp = sorted(l.get('spans') or [], key=lambda s: s['x0'])
        if not sp:
            return 1
        n, prev = 1, None
        for s in sp:
            if prev is not None and s['x0'] - prev > char_w * 2.0:
                n += 1
            prev = s['x1']
        return n

    counts = sorted(seg_count(l) for l in cand)
    # 带注号的块直接认定为脚注（参考文献常被排版成多段，不能只看段数）
    if counts[len(counts) // 2] >= 3 and not has_mark:
        return None

    start = min(l['y0'] for l in cand)
    # 分隔线：候选区上方 4 行高内
    for l in lines:
        if not (l.get('sep') or SEP_TEXT.match((l.get('text') or '').strip())):
            continue
        if start - line_h * 4 <= l['y0'] <= start + line_h:
            start = min(start, l['y0'])
    return start


# ---------------------------------------------------------------- 双栏
def gutter_of(lines):
    """双栏页的中缝 x；单栏页返回 None。用中部覆盖率最低区域定位，
    并验证左右确实各有一列密集文字。"""
    body = [l for l in lines if l['y0'] > 700]
    if len(body) < 12:
        return None
    W = 3400.0
    BINS = 85
    size = W / BINS
    cov = [0] * BINS
    for l in body:
        a = max(0, int(l['x0'] / size))
        b = min(BINS - 1, int(l['x1'] / size))
        for i in range(a, b + 1):
            cov[i] += 1
    lo, hi = int(BINS * 0.34), int(BINS * 0.66)
    i = min(range(lo, hi), key=lambda k: cov[k])
    split = None
    if cov[i] <= max(2, len(body) * 0.10):
        a = i
        while a - 1 >= 0 and cov[a - 1] <= max(1, cov[i]):
            a -= 1
        b = i
        while b + 1 < BINS and cov[b + 1] <= max(1, cov[i]):
            b += 1
        split = (a + b + 1) / 2.0 * size
        left = [l for l in body if l['x1'] <= split]
        right = [l for l in body if l['x0'] >= split]
        if not (len(left) >= 5 and len(right) >= 5 and
                len(body) - len(left) - len(right) <= 3):
            split = None
    if split is not None:
        return split
    # 兜底：首页"摘要/标题通栏 + 正文双栏"时中间被通栏行盖住，
    # 覆盖率法失效——改用"行尾/行首贴缝"计分：双栏页大量左栏行的
    # 行尾贴着中缝、右栏行的行首贴着中缝；通栏行跨缝则扣分。
    # 表格页数字占比高，跳过（表格列间隙不是中缝）。
    txt = ''.join(l['text'] for l in body)
    digs = sum(1 for c in txt if c.isdigit() or c in '%.()')
    if txt and digs / float(len(txt)) > 0.2:
        return None
    # 方法二（完整行页）：只认"长行"证据——正文栏的行是整行文字，
    # 表格单元格都是短碎片，列起点也分散
    longs = [l for l in body if len(l['text']) >= 25]
    if len(longs) >= 20:
        best_score, best_g = 0, None
        for g in range(int(W * 0.30), int(W * 0.70), 20):
            lefts = [l for l in longs
                     if l['x1'] <= g + 40 and l['x0'] < g - 100]
            rights = [l for l in longs if l['x0'] >= g - 40]
            if len(lefts) < 8 or len(rights) < 8:
                continue
            ls0 = min(l['x0'] for l in lefts)
            rs0 = min(l['x0'] for l in rights)
            lnear = sum(1 for l in lefts if abs(l['x0'] - ls0) <= 120)
            rnear = sum(1 for l in rights if abs(l['x0'] - rs0) <= 120)
            score = min(lnear, rnear)
            if score > best_score:
                best_score, best_g = score, g
        if best_score >= 8:
            return best_g

    # 方法三（碎片化文字层的期刊首页：摘要通栏 + 正文双栏）：
    # 行尾贴缝 + 行首贴缝的密度法。只对低数字占比页启用——图表页的
    # 坐标轴/图例数字多，会被数字占比挡掉
    txt = ''.join(l['text'] for l in body)
    digs = sum(1 for c in txt if c.isdigit() or c in '%.()')
    if not txt or digs / float(len(txt)) > 0.08 or len(body) < 40:
        return None
    best_score, best_g = 0, None
    for g in range(int(W * 0.30), int(W * 0.70), 20):
        ends = sum(1 for l in body
                   if len(l['text']) >= 8 and g - 220 <= l['x1'] <= g + 40)
        starts = sum(1 for l in body
                     if len(l['text']) >= 8 and g - 40 <= l['x0'] <= g + 220)
        score = min(ends, starts)
        if score > best_score:
            best_score, best_g = score, g
    return best_g if best_score >= 10 else None


_ROMAN_VAL = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}
_UNI_ROMAN = {u'Ⅰ': 1, u'Ⅱ': 2, u'Ⅲ': 3, u'Ⅳ': 4, u'Ⅴ': 5, u'Ⅵ': 6,
              u'Ⅶ': 7, u'Ⅷ': 8, u'Ⅸ': 9, u'Ⅹ': 10, u'Ⅺ': 11, u'Ⅻ': 12,
              u'Ⅼ': 50, u'Ⅽ': 100, u'Ⅾ': 500, u'Ⅿ': 1000}


def _roman_to_int(s):
    """ASCII 罗马数字转整数；非规范写法（IIII、VX）返回 None。"""
    s = s.upper()
    if not re.fullmatch(r'[IVXLCDM]{1,9}', s):
        return None
    total, prev = 0, 0
    for ch in reversed(s):
        v = _ROMAN_VAL[ch]
        if v < prev:
            total -= v
        else:
            total += v
            prev = v
    if total <= 0 or total > 3000:
        return None
    chk, n = '', total
    for v, sym in ((1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'),
                   (100, 'C'), (90, 'XC'), (50, 'L'), (40, 'XL'),
                   (10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I')):
        while n >= v:
            chk += sym
            n -= v
    return total if chk == s else None


def printed_numbers(pages, _repair=True):
    """从页眉/页脚里找印刷页码，建立 {PDF页: 印刷页} 映射。

    页码位置因书而异（页顶/页底、左右交替、带书名/装饰线），所以不按
    固定位置取，而是把页面上下两条窄带里的所有数字都当候选，再用
    “页码应当逐页 +1”的连续性挑出真正的那一串：年份、版次、ISBN
    这类离群数字会被连续性淘汰；某章重新编号时允许断点，只是扣分。
    """
    CN = {u'零': 0, u'〇': 0, u'一': 1, u'二': 2, u'两': 2, u'三': 3,
          u'四': 4, u'五': 5, u'六': 6, u'七': 7, u'八': 8, u'九': 9}

    def cn2int(s):
        if not s:
            return None
        if all(c in CN for c in s):
            return int(''.join(str(CN[c]) for c in s))
        # 十/百 组合（简单版：三十一、一百零二）
        total, section, num = 0, 0, 0
        for c in s:
            if c in CN:
                num = CN[c]
            elif c == u'十':
                section += (num or 1) * 10
                num = 0
            elif c == u'百':
                section += (num or 1) * 100
                num = 0
            else:
                return None
        return total + section + num if (section or num) else None

    # 坐标统一换算到 3400×5000 的空间（不管原页面多大），页高就是 5000。
    # 不能拿"文字最低点"当页高：有的页没有页脚，算出来只有 4380，
    # 检测带会跟着缩到 876，同一本书的页码候选就时有时无。
    page_h = 5000.0
    # 页码不一定贴在最边上：毛选的页眉在页面 13%—14% 处，固定 480 的
    # 窄带会把页码整条漏掉（于是全书都用 PDF 页序，对不上原书页码）。
    TOP = max(480.0, page_h * 0.20)
    BOT = min(4520.0, page_h * 0.88)
    bs = doc_body_size(pages)

    def collect(small_only):
        cand = {}
        for p, lines in pages.items():
            # 先把同一视觉行上的片段拼起来，再取数字：PDF 文字层常把
            # "１１"拆成两个各占一行的"１"，分开看每个都只是 1，
            # 连续性永远对不上，那一整段页码就会全丢。
            rows = []
            for l in lines:
                y = l.get('y0') or 0
                if TOP < y < BOT:
                    continue                 # 只看页顶/页底两条带
                # 页眉/页脚都比正文小；先只认小字行，正文里的数字就进不来
                if small_only and bs:
                    sz = l.get('size') or 0
                    if sz and sz >= bs * 0.95:
                        continue
                for row in rows:
                    if abs(row[0] - y) <= 6.0:
                        row[1].append(l)
                        break
                else:
                    rows.append([y, [l]])
            opts = []
            for y, rl in rows:
                rl.sort(key=lambda z: z.get('x0') or 0)
                edge = 0
                if y < TOP * 0.75 or y > page_h - (page_h - BOT) * 0.75:
                    edge += 1                # 越靠边越像页码
                x0 = min((z.get('x0') or 0) for z in rl)
                x1 = max((z.get('x1') or 3400) for z in rl)
                if x0 < 260 or x1 > 3140:
                    edge += 1
                t = ''.join((z.get('text') or '') for z in rl).replace(u' ', u'')
                # 只认“独立的”1-4 位数字：ISBN 这类长串不能被切成几段
                for m in re.finditer(r'(?<!\d)\d{1,4}(?!\d)', t):
                    n = int(m.group(0))
                    if 1 <= n <= 3000:
                        opts.append((n, edge, str(n)))
                m = re.search(u'第([一二三四五六七八九十百零〇两]{1,5})页', t)
                if m:
                    n = cn2int(m.group(1))
                    if n:
                        opts.append((n, edge, str(n)))
                # 罗马数字页码：前置页常用 Ⅰ、Ⅱ 或 I、II、iv
                for m in re.finditer(u'[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫⅬⅭⅮⅯ]+', t):
                    n = sum(_UNI_ROMAN.get(c, 0) for c in m.group(0))
                    if 1 <= n <= 3000:
                        opts.append((n, edge, m.group(0)))
                if len(t) <= 12:
                    letters = re.sub(u'[^A-Za-z]', u'', t)
                    if 1 <= len(letters) <= 7:
                        n = _roman_to_int(letters)
                        if n:
                            opts.append((n, max(0, edge - 1), letters))
            if opts:
                best = {}
                for n, edge, disp in opts:
                    if n not in best or edge > best[n][0]:
                        best[n] = (edge, disp)
                cand[p] = best
        return cand

    cand = collect(True)
    # 有的书页码和正文一样大：小字过滤后候选太少，就退回不过滤
    if sum(1 for v in cand.values() if v) < max(2, len(pages) * 0.3):
        cand = collect(False)
    if not cand:
        return {}

    def step_pen(d):
        # 页码应当每页 +1；连续性比“靠边”重要，错一页扣得比边角加分多
        if d == 0:
            return 0.0
        return min(30.0, 3.0 * abs(d))

    def _solve(cand):
        pages_sorted = sorted(cand)
        layers = {}          # page -> {n: (score, prev_page, prev_n)}
        prev_pages = []
        for p in pages_sorted:
            layer = {}
            for n, (edge, _disp) in cand[p].items():
                best = (edge * 2.0, None, None)
                for pp in prev_pages[-60:]:
                    for pn, (sc, _, _) in layers[pp].items():
                        exp = p - pp
                        val = sc + edge * 2.0 - step_pen(n - pn - exp)
                        if val > best[0]:
                            best = (val, pp, pn)
                layer[n] = best
            layers[p] = layer
            prev_pages.append(p)
        last_p = pages_sorted[-1]
        key = max(((last_p, n) for n in layers[last_p]),
                  key=lambda k: layers[k[0]][k[1]][0])
        out = {}
        while key is not None:
            p, n = key
            out[p] = cand[p][n][1]
            _, pp, pn = layers[p][n]
            key = (pp, pn) if pp is not None else None
        return out

    out = _solve(cand)
    pages_sorted = sorted(cand)
    # 字号和正文一样的页码（"711"被并进页眉行、或整页正文与页码同号）
    # 会被小字门槛漏掉。只对“已知页码两侧正好缺一页”的页补非小字候选，
    # 用前后页的 +1 连续性做目标，避免把年份/正文数字当成页码。
    if _repair:
        missing = [p for p in sorted(pages) if p not in out]
        if missing:
            allc = collect(False)
            cand2 = {p: dict(v) for p, v in cand.items()}
            changed = False
            for p in missing:
                prevs = [q for q in out if q < p and str(out[q]).isdigit()]
                nexts = [q for q in out if q > p and str(out[q]).isdigit()]
                target = None
                if prevs and nexts:
                    a, b = max(prevs), min(nexts)
                    if int(out[a]) + (p - a) == int(out[b]) - (b - p):
                        target = int(out[a]) + (p - a)
                elif prevs:
                    a = max(prevs)
                    target = int(out[a]) + (p - a)
                elif nexts:
                    b = min(nexts)
                    target = int(out[b]) - (b - p)
                if target is not None and target in (allc.get(p) or {}):
                    cand2.setdefault(p, {})[target] = allc[p][target]
                    changed = True
            if changed:
                out = _solve(cand2)
                pages_sorted = sorted(cand2)

    # 被“重新编号”断点丢掉的整段（如前置页 Ⅰ—Ⅻ），单独再排一次，
    # 否则这些页会没有页码、退回 PDF 页序。
    runs, cur = [], []
    for p in pages_sorted:
        if p in out:
            if cur:
                runs.append(cur)
                cur = []
        else:
            cur.append(p)
    if cur:
        runs.append(cur)
    for run in runs:
        if len(run) >= len(pages_sorted):
            continue
        sub = {p: pages[p] for p in run}
        out.update(printed_numbers(sub, _repair=False))
    if _repair:
        out = _fill_offsets(out, pages_sorted)
    return out


def detect_page_sections(pages, mapping):
    """识别独立页码段（附录/参考文献/索引重新从 1 编号）。

    返回 {PDF页: '附录'/'参考文献'/'人名索引'}，只给“印刷号与 PDF 页序
    不一致”的段；导出成“原书第32页（附录第1页）”，避免看起来整本乱号。
    """
    if not mapping:
        return {}
    ps = sorted(p for p in mapping if str(mapping[p]).isdigit())
    if not ps:
        return {}
    runs, cur, prev = [], [], None
    for p in ps:
        n = int(mapping[p])
        if cur and (p - prev[0] > 1 or n - prev[1] > 3 or n <= prev[1]):
            runs.append(cur)
            cur = []
        cur.append((p, n))
        prev = (p, n)
    if cur:
        runs.append(cur)
    out = {}
    for run in runs:
        sec = ''
        for p, _n in run[:3]:
            t = ' '.join((l.get('text') or '') for l in
                         (pages.get(p) or [])[:10])
            if u'附录' in t or 'Appendix' in t:
                sec = u'附录'
                break
            if u'参考文献' in t or 'References' in t:
                sec = u'参考文献'
                break
            if u'人名索引' in t or u'索引' in t[:40]:
                sec = u'人名索引'
                break
        if sec:
            for p, _n in run:
                out[p] = sec
    return out


def number_position(pages, mapping):
    """印刷页码出现在页面的什么位置：归一化 y 和左右侧。

    页码有的在页顶、有的在页底、有的左右交替；导出时页码行必须钉在
    原书那个位置，不然标尺一看就知道放错了。
    返回 {'y': 归一化y, 'x': 归一化x} 或 None。
    """
    ys, xs = [], []
    for p, disp in mapping.items():
        t = str(disp)
        if not t.isdigit():
            continue
        for l in pages.get(p) or []:
            y = l.get('y0') or 0
            if 1200 < y < 4000:
                continue                 # 只看页顶/页底两条带
            txt = (l.get('text') or '').replace(u' ', u'')
            # 页码可能补零（"002"）：按数值比，不按字符串比
            if any(x.isdigit() and int(x) == int(t)
                   for x in re.findall(u'\\d{1,4}', txt)):
                ys.append(y)
                xs.append(((l.get('x0') or 0) + (l.get('x1') or 3400)) / 2.0)
    if not ys:
        return None
    ys.sort()
    xs.sort()
    return {'y': ys[len(ys) // 2], 'x': xs[len(xs) // 2]}


def _fill_offsets(out, pages_sorted):
    """用"PDF 页 − 印刷页 = 常数"的规律修补个别页的乱号。

    书的页码是按页 +1 递增的，PDF 页与印刷页的差在同一段里是常数。
    个别页的页眉可能缺字（"３３８"被拆成"３"和"３８"），单页看起来是
    乱号；拿全书的多数偏移去校正，比逐页猜可靠得多。书内真的重新编过
    号的独立段（连续 3 页以上、偏移自洽）原样保留。
    """
    vals = {p: int(str(v)) for p, v in out.items() if str(v).isdigit()}
    if len(vals) < 4:
        return out
    main_off, _ = collections.Counter(
        n - p for p, n in vals.items()).most_common(1)[0]
    good = sorted(p for p, n in vals.items() if n - p == main_off)
    lo, hi = good[0], good[-1]
    res = dict(out)
    # 主编号段内部缺号的页：按主偏移补上（页眉缺失也能标对页码）
    for p in pages_sorted:
        if lo < p < hi and p not in out:
            res[p] = str(p + main_off)
    bad = [p for p in pages_sorted if p in vals and vals[p] - p != main_off]
    blocks = []
    for p in bad:
        if blocks and p == blocks[-1][-1] + 1:
            blocks[-1].append(p)
        else:
            blocks.append([p])
    for blk in blocks:
        sub = collections.Counter(vals[p] - p for p in blk)
        off2, c2 = sub.most_common(1)[0]
        inner = lo < blk[0] and blk[-1] < hi
        if len(blk) >= 3 and c2 >= max(2, len(blk) * 0.6):
            # 书里另行编号的一段（如附录），按其自身偏移重标
            for p in blk:
                res[p] = str(p + off2)
        elif inner:
            for p in blk:
                res[p] = str(p + main_off)
        else:
            # 正文段之外的零散乱号（封面、版权页噪声）：宁可不标
            for p in blk:
                res.pop(p, None)
    return res


def page_geometry(pages, page_w, page_h):
    """量出原书的真实版式：页边距、正文字号、行距、首行缩进（单位 pt）。

    Word 的标尺读的就是这些数：左边距多少厘米、字多大、行距多少。
    与其在 Word 里拿标尺一格一格量，不如直接从 PDF 的行框反推——
    PDF 里的坐标本来就精确到 0.1pt，量出来和标尺是同一套单位。
    """
    body = doc_body_size(pages)
    if not body:
        return None
    lefts, rights, tops, bots, gaps, advs = [], [], [], [], [], []
    for p, ls in pages.items():
        rows = []
        for l in sorted(ls, key=lambda x: (x['y0'], x['x0'])):
            t = (l.get('text') or '').strip()
            if not t:
                continue
            for r in rows:
                if abs(r['y0'] - l['y0']) <= 8:
                    r['x0'] = min(r['x0'], l['x0'])
                    r['x1'] = max(r['x1'], l['x1'])
                    r['text'] += t
                    r['sz'].append(l.get('size') or 0)
                    break
            else:
                rows.append({'y0': l['y0'], 'y1': l['y1'], 'x0': l['x0'],
                             'x1': l['x1'], 'text': t,
                             'sz': [l.get('size') or 0]})
        # 字号匹配的容差要按比例：扫描件的字号是 OCR 框高估的，同一页
        # 正文能在 ±15% 里跳；写死 0.4pt 会把大半正文行排除在外，
        # 量出来的上下边距就成了"部分行"的边距，版心被压得又扁又窄。
        _tol = max(0.4, body * 0.15)
        bodyrows = [r for r in rows
                    if r['sz']
                    and abs(sorted(r['sz'])[len(r['sz']) // 2] - body) < _tol
                    and len(r['text']) >= 8]
        if not bodyrows:
            continue
        tops.append(min(r['y0'] for r in bodyrows))
        bots.append(max(r['y1'] for r in bodyrows))
        lefts.extend(r['x0'] for r in bodyrows)
        rights.extend(r['x1'] for r in bodyrows)
        for r in bodyrows:
            n = len(r['text'])
            t = r['text']
            # 只取整行的正文：目录行有引导点、数字页码，字宽算不准
            if n >= 15 and u'⋯' not in t and u'…' not in t:
                cjk = sum(1 for c in t if u'一' <= c <= u'鿿')
                if cjk >= n * 0.7:
                    advs.append((r['x1'] - r['x0']) / float(n))
        ys = sorted(r['y0'] for r in bodyrows)
        for a, b in zip(ys, ys[1:]):
            # 行距范围按字号算，不写死 10–25pt：扫描件的页面是扫描分辨率，
            # 同一本书的行距可能是 50pt 上下，写死就会全部被过滤掉，
            # 最后退回"字号×1.5"，排出来每页底下空一大截。
            d = (b - a) / 5000.0 * page_h
            if body * 0.9 <= d <= body * 2.8:
                gaps.append(b - a)
    if not lefts:
        return None

    def q(v, x):
        v = sorted(v)
        return v[min(len(v) - 1, int(len(v) * x))]

    L, R = q(lefts, 0.05), q(rights, 0.95)
    # 上下边距取"最满的页"（10%/90% 分位），不取中位数：
    # 章节末页只排了半页，用中位数会把框底抬高一两行，
    # 结果每一页底下都空着一截。取最满的页，框才包得住整页的字。
    T = q(tops, 0.10)
    B = q(bots, 0.90)
    pitch = sorted(gaps)[len(gaps) // 2] if gaps else 0
    # 原书的实际字宽：一行 28 个字占 283.9pt，字号却是 10.5pt——
    # 说明字是被压扁排的（0.96 倍）。Word 里用"字符缩放"复现，
    # 字号保持 10.5（标尺读数和原书一致），但每行能排下同样多的字，
    # 换行位置才对得上，页数才能一一对应。
    scale = 1.0
    if advs and body:
        eff = sorted(advs)[len(advs) // 2] / 3400.0 * page_w
        # 允许放大到 1.2：有的书字距拉得很开（这本小说每个字 29.5pt，
        # 字号才 26pt），不放大就会在同样宽度里多塞字、每页少排几行，
        # 页面底下空一大块。
        scale = max(0.85, min(1.2, eff / body))
    # 扫描件的"页面尺寸"常常是扫描分辨率，不是书的实际开本
    # （比如 1000×1621pt 的 6 寸阅读器截图，字号 27pt）。超过 A4 的
    # 一律等比缩到 A4 以内：框、字号、行距同比例缩，版面比例不变，
    # Word 里还是一本正常大小的书，不会变成 35×57cm 的巨页。
    # 常见纸型：A4 宽 595，US Letter 宽 612。以前一律按 595 截，
    # 34 页 Letter 论文会被整体压到 595×770，行宽少 4%，Word 里
    # 英文行频繁多折一行，只能靠压字距/字号硬塞，正文就变得很挤。
    MAXW, MAXH = 612.0, 842.0
    k = min(1.0, MAXW / page_w, MAXH / page_h)
    pw, ph = page_w * k, page_h * k
    return {
        'page_w': pw, 'page_h': ph,
        'left': L / 3400.0 * pw,
        'right': (3400.0 - R) / 3400.0 * pw,
        'top': T / 5000.0 * ph,
        'bottom': (5000.0 - B) / 5000.0 * ph,
        'body_size': body * k,
        'pitch': (pitch / 5000.0 * ph) if pitch else body * 1.5 * k,
        'indent': 2 * body * k,
        'scale': scale,
        'scan_scale': k,
    }


def geom_plausible(g):
    """量出来的框是否合理：防止把图册、特殊版式书、双栏页量歪了还硬套。

    返回 (是否可用, 原因)。量歪了就退回默认 32 开，并在日志里说清楚，
    总比按一个错误框排出满篇错位强。
    """
    if not g:
        return False, u'没有找到正文行'
    pw, ph = g['page_w'], g['page_h']
    tw = pw - g['left'] - g['right']
    th = ph - g['top'] - g['bottom']
    if not (150 <= pw <= 1700 and 150 <= ph <= 2400):
        return False, u'页面尺寸异常'
    for k in ('left', 'right', 'top', 'bottom'):
        if g[k] < 0:
            return False, u'%s 边距为负' % k
    if tw < pw * 0.30 or tw > pw * 0.98:
        return False, u'版心宽度异常（%.0f%%）' % (100.0 * tw / pw)
    if th < ph * 0.40:
        return False, u'版心高度异常（%.0f%%）' % (100.0 * th / ph)
    bs = g['body_size']
    # 字号按"占页高的比例"判断，不按绝对磅值：扫描件的页面尺寸
    # 往往是扫描分辨率，同一本书能差两三倍
    if not (4.0 <= bs <= 72.0) or not (ph * 0.006 <= bs <= ph * 0.09):
        return False, u'正文字号异常（%.1fpt / 页高 %.0fpt）' % (bs, ph)
    if not (bs * 1.0 <= g['pitch'] <= bs * 2.6):
        return False, u'行距异常（%.1f）' % g['pitch']
    return True, u''


def doc_body_size(pages):
    """全文字号（按字符数加权中位数）：正文标准字号。脚注/参考文献
    比它小，用这个值把"页脚小字"与正文区分开（单页可能全是小字，
    不能只看本页）。"""
    pairs = []
    for ls in pages.values():
        for l in ls:
            s = l.get('size')
            if s:
                pairs.append((s, max(1, len(l.get('text') or ''))))
    if not pairs:
        return 0.0
    pairs.sort()
    total = sum(w for _, w in pairs)
    acc = 0
    for s, w in pairs:
        acc += w
        if acc >= total / 2.0:
            return s
    return pairs[-1][0]


def reorder_two_column(lines, gutter, gap_pt=3.0, body_size=None):
    """文字层双栏页：按中缝把每行切成左右两栏，先左栏后右栏。

    右栏坐标平移回左栏坐标系（x 减去两栏左边距之差），y 顺延到
    左栏之后——这样后续段落拼装把它当成"接在左栏后面"的单栏文字，
    阅读顺序正确，且不会被 group_rows 按同 y 合并而串行。
    页眉页脚带（页顶/页底）原样保留，不参与分栏。返回新行列表；
    不适用时返回 None。
    """
    top_band, bottom_band = 550.0, 4550.0
    # 正文字号（全文加权中位数）：小字号行（脚注/参考文献）不参与分栏
    if not body_size:
        sizes = sorted(l.get('size') or 0 for l in lines
                       if bottom_band > l['y0'] > 120.0 and l.get('size'))
        body_size = sizes[len(sizes) // 2] if sizes else 0.0
    small_thr = body_size * 0.90 if body_size else 0.0
    # 页眉很矮的期刊，正文第一行可能从页高 8% 就开始（y≈427），
    # 固定 550 的"页眉带"会把最上面几行正文原样留下，左右栏就串行。
    # 用"成句的长行"反推正文真正的起点，再把带上沿压到它上面。
    _cand = [l for l in lines
             if 120.0 <= l['y0'] <= bottom_band
             and len((l.get('text') or '').strip()) >= 15
             and (l['x0'] < gutter - 200 or l['x0'] > gutter + 200)]
    if _cand:
        _bs = min(l['y0'] for l in _cand)
        top_band = min(top_band, max(150.0, _bs - 15.0))
    # 正文栏区的起点：正文号字的左栏行最早出现的 y。它以上是标题/摘要
    # 等前置内容（字号偏小），不参与分栏
    body_lines = [l for l in lines
                  if top_band <= l['y0'] <= bottom_band
                  and (not small_thr or
                       small_thr <= (l.get('size') or 0)
                       <= body_size * 1.15)]
    left_bodies = [l for l in body_lines if l['x0'] < gutter - 200]
    body_start = min((l['y0'] for l in left_bodies), default=top_band)
    # 双栏页的“页底注号”不一定横跨两栏：若只认纵向位置，右栏下半页
    # 正文会被当成脚注。双栏页仍按字号判注文，不用单栏的结构兜底。
    fn_y = footnote_start(lines, body_size, marker_rule=False)
    left, right, notes, wide = [], [], [], []
    for l in lines:
        if l.get('sep'):
            continue                    # 横线分隔符不参与分栏
        spans = l.get('spans') or []
        if not spans:
            return None
        if l['y0'] < top_band or l['y0'] > bottom_band:
            continue
        # 脚注区：统一按 footnote_start 判定（字号 + 位置 + 注号 +
        # 分隔线；整页小字的附表页不会被误判）
        if fn_y is not None and l['y0'] >= fn_y:
            notes.append(l)
            continue
        if small_thr and (l.get('size') or 0) < small_thr:
            if l['y0'] < body_start:
                wide.append(l)          # 标题/摘要等前置内容：原样保留
            else:
                notes.append(l)         # 兜底：小字行
            continue
        # 通栏行（摘要、标题等横跨两栏）：原样保留，不切分
        if l['x0'] < gutter - 50 and l['x1'] > gutter + 50 and \
                (l['x1'] - l['x0']) > 3400.0 * 0.6:
            wide.append(l)
            continue
        # 先按整行归属判断（行首贴近中缝右侧=右栏行），避免中缝估计
        # 偏右时把右栏行首的字误切到左栏
        if l['x0'] >= gutter - 60:
            right.append(spans)
            continue
        if l['x1'] <= gutter + 60:
            left.append(spans)
            continue
        ls, rs = [], []
        for s in spans:
            cx = (s['x0'] + s['x1']) / 2.0
            (ls if cx < gutter else rs).append(s)
        if ls:
            left.append(ls)
        if rs:
            right.append(rs)
    if len(left) < 6 or len(right) < 6:
        return None
    lmin = min(s['x0'] for sp in left for s in sp)
    rmin = min(s['x0'] for sp in right for s in sp)
    dx = rmin - lmin
    ly1 = max(s['y1'] for sp in left for s in sp)
    dy = ly1 + 30.0

    def mk(spans, dx, dy):
        spans2, text, prev_x1 = [], '', None
        for s in spans:
            s2 = dict(s)
            s2['x0'] = s['x0'] - dx
            s2['x1'] = s['x1'] - dx
            s2['y0'] = s['y0'] + dy
            s2['y1'] = s['y1'] + dy
            if prev_x1 is not None and (s2['x0'] - prev_x1) > gap_pt:
                text += ' '
            text += s2['text']
            prev_x1 = s2['x1']
            spans2.append(s2)
        szs = sorted(s.get('size') or 0 for s in spans if s.get('size'))
        size = szs[len(szs) // 2] if szs else 0.0
        return {
            'text': text.strip(),
            'score': 1.0,
            'x0': spans2[0]['x0'],
            'x1': max(x['x1'] for x in spans2),
            'y0': min(x['y0'] for x in spans2),
            'y1': max(x['y1'] for x in spans2),
            'agree': True,
            'alts': [],
            'spans': spans2,
            'size': size,          # 保留字号：脚注/正文判定要用
        }

    out = [l for l in lines
           if l['y0'] < top_band or l['y0'] > bottom_band]
    out.extend(wide)
    left.sort(key=lambda sp: min(s['y0'] for s in sp))
    right.sort(key=lambda sp: min(s['y0'] for s in sp))
    for sp in left:
        out.append(mk(sp, 0.0, 0.0))
    ry1 = max(s['y1'] for sp in right for s in sp) if right else ly1
    for sp in right:
        out.append(mk(sp, dx, dy))
    # 页脚小字排到两栏文字之后（阅读顺序：左栏→右栏→脚注）
    if notes:
        ny0 = (ry1 + dy) + 40.0
        nmin = min(l['y0'] for l in notes)
        for l in notes:
            l2 = dict(l)
            l2['y0'] = l['y0'] - nmin + ny0
            l2['y1'] = l['y1'] - nmin + ny0
            l2['spans'] = [dict(s, y0=s['y0'] - nmin + ny0,
                                y1=s['y1'] - nmin + ny0)
                           for s in (l.get('spans') or [])]
            out.append(l2)
    return out


def _font_cn(name):
    """PDF 字体名常被当成 latin-1 解码；能还原就还原，再映射到中文名。"""
    s = name or ''
    try:
        s = s.encode('latin1').decode('utf-8')
    except Exception:
        pass
    low = s.lower()
    for key, out in (
            ('simsun', u'宋体'), (u'宋体', u'宋体'), ('song', u'宋体'),
            ('simhei', u'黑体'), (u'黑体', u'黑体'), ('hei', u'黑体'),
            ('kaiti', u'楷体'), (u'楷体', u'楷体'), ('kai', u'楷体'),
            ('fangsong', u'仿宋'), (u'仿宋', u'仿宋'),
            ('times', 'Times New Roman'),
            ('arial', 'Arial'), ('helvetica', 'Arial')):
        if key in low:
            return out
    return s


def line_styles(l):
    """把一行里 span 的字号/字体/上下标展开成逐字样式。

    文字层的 span 本来就有精确 size/font/bbox；之前 build_blocks 只留
    text+mask，导出整段一个字号，上标引用（7pt）会变成正文大小，注号
    ①、公式下标也会被拉平。这里只做“行内样式索引”，不改文字。
    """
    spans = l.get('spans') or []
    text = l.get('text') or ''
    if not spans or not text:
        return None
    lsz = float(l.get('size') or 0)
    lh = max(1.0, float(l.get('y1', 0)) - float(l.get('y0', 0)))
    default = (lsz, '', False, False, False, False)
    out = [default] * len(text)
    cursor = 0
    prev = default
    for s in spans:
        st = (s.get('text') or '').strip()
        if not st:
            continue
        idx = text.find(st, cursor)
        if idx < 0:
            idx = text.find(st)
        if idx < 0:
            continue
        sz = float(s.get('size') or lsz or 0)
        _smid = (float(s.get('y0', 0)) + float(s.get('y1', 0))) / 2.0
        _lmid = (float(l.get('y0', 0)) + float(l.get('y1', 0))) / 2.0
        small = bool(lsz and sz < lsz * 0.86)
        # 上标：小字 + 重心在行中线之上；下标：小字 + 在行中线之下。
        # 只按 bbox 是否超出页顶太严，引用 [37] 常常只高一点点。
        sup = bool(small and _smid < _lmid - lh * 0.04)
        sub = bool(small and _smid > _lmid + lh * 0.04)
        flags = int(s.get('flags') or 0)
        bold = bool(flags & 16) or ('bold' in (s.get('font') or '').lower())
        italic = bool(flags & 2) or ('italic' in (s.get('font') or '').lower())
        style = (sz or lsz, _font_cn(s.get('font') or ''), sup, sub,
                 bold, italic)
        for k in range(len(st)):
            if idx + k < len(out):
                out[idx + k] = style
        cursor = idx + len(st)
        prev = style
    # 文字之间的空格/标点用前一个 span 的样式
    for i, ch in enumerate(text):
        if out[i] == default and ch.isspace():
            out[i] = prev
    return out


def build_style_maps(pages):
    """{页号: (去掉空白的全文, 逐字样式)}，供导出时按块文本回查。

    block 文本是行文本拼出来的，去掉空白后基本是 pages 文本的子串；
    导出按页内游标顺序查找，命中就按 span 样式发 run，找不到就退回
    原来的整段样式（如权威文本替换后的块）。
    """
    maps = {}
    for p, lines in pages.items():
        chars, styles = [], []
        for l in sorted(lines, key=lambda x: (x.get('y0') or 0,
                                              x.get('x0') or 0)):
            st = line_styles(l)
            if st is None:
                continue
            for ch, sty in zip(l.get('text') or '', st):
                if ch.isspace():
                    continue
                chars.append(ch)
                styles.append(sty)
        if chars:
            maps[p] = (''.join(chars), styles)
    return maps


def rebuild_two_column(pdf_path, pages, pno, split, ocr):
    """对双栏页：沿中缝切成两半分别识别，左栏先、右栏后，
    跨行条目合并成完整条目。返回块列表（不含 page 标记）。"""
    doc = pymupdf.open(pdf_path)
    pix = doc[pno - 1].get_pixmap(dpi=160)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n >= 3:
        img = np.ascontiguousarray(img[:, :, :3])
    doc.close()
    sx = img.shape[1] / 3400.0
    cut = int(split * sx)
    pad = int(60 * sx)
    halves = [img[:, :max(cut, 1)], img[:, max(cut - pad, 0):]]
    lines = []
    for half in halves:
        res, _ = ocr(np.ascontiguousarray(half))
        lines.extend(sorted((res or []), key=lambda r: min(p[1] for p in r[0])))
    # 行文本
    texts = []
    for box, text, score in lines:
        t = re.sub(u'(?<=[\\u3400-\\u9fff])[ \\t]+(?=[\\u3400-\\u9fff])', '', text.strip())
        if t and min(p[1] for p in box) >= 700 * (img.shape[0] / 5000.0):
            texts.append(t)
    # 合并成条目：遇句号断条；"X画" 独立成条；页码行丢弃
    out, buf = [], None
    SEC = re.compile(u'^[一二三四五六七八九十]+画$')
    for t in texts:
        if not t or re.match(u'^[0-9]{1,4}$', t):
            continue
        if SEC.match(t):
            if buf:
                out.append(buf)
                buf = None
            out.append(t)
            continue
        buf = t if buf is None else buf + t
        if buf.endswith(u'。'):
            out.append(buf)
            buf = None
    if buf:
        out.append(buf)
    # 扫描件系统性形近误认（真实案例验证过的高置信规则）
    RULES = [
        (u'[0-9][ \\t]*[真買直夏員员贝臭頁]', u'页'),
        (u'阶极', u'阶级'),
        (u'餐产', u'资产'),
        (u'瓷产', u'资产'),
    ]
    out = [['index', t, [False] * len(t)] for t in out]
    for b in out:
        for pat, rep in RULES:
            b[1] = re.sub(pat, rep, b[1])
        b[2] = [False] * len(b[1])
    return out
