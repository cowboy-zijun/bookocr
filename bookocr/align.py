# -*- coding: utf-8 -*-
"""权威文本对齐。

当用户提供同一译本的权威电子文本（.txt）时，把 OCR 结果逐段与它对齐：
- 单调指针 + 局部模糊比对（避免漂移）；
- "已输出区间覆盖率"守卫（防止指针跳过头之后重复输出）；
- 第二遍：把被 OCR 拆碎的段落拼起来重试；
- 第三遍：对仍未匹配的段落做全局检索——已在权威文本中出现过的（如内联
  脚注）即视为重复丢弃，新增内容则采用权威原文。
"""
import re

from rapidfuzz import fuzz

CJK = re.compile(u'[㐀-鿿]')
DROP = re.compile(u'[^\\u3400-\\u9fffA-Za-z0-9]')
WIN_BACK = 400
WIN_FWD = 9000
# 版面 JSON/占位块不参与文本对齐。frametree/linepage 里是整页 JSON，
# 若按正文去模糊匹配，既极慢又会把版面数据替换成权威文本。
PASS_KINDS = ('page', 'header', 'image', 'tablepage', 'tablegrid',
              'frametree', 'linepage', 'vspace', 'cols2', 'coltable',
              'cols2page')


def norm(s):
    return DROP.sub('', s)


def load_canonical(path):
    """读入权威文本，同时记录其标题层级（行首 # 个数）。"""
    parts, heads, pos = [], [], 0
    with open(path, encoding='utf-8', errors='replace') as f:
        raw = f.read()
    raw = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', raw)
    raw = re.sub(r'&[a-zA-Z]+;', ' ', raw)
    raw = raw.replace('**', '').replace('*', '')
    for line in raw.split('\n'):
        s = line.strip()
        m = re.match(u'^(#{1,6})\\s*(.*)$', s)
        lvl, body = 0, s
        if m:
            lvl, body = len(m.group(1)), m.group(2)
        body = body.replace('#', '').strip()
        if not body:
            continue
        start = pos
        parts.append(body)
        pos += len(body)
        if lvl:
            heads.append((start, pos, lvl))
        parts.append('\n')
        pos += 1
    canon = ''.join(parts)
    canon_n = norm(canon)
    idx = []
    keep = re.compile(u'[\\u3400-\\u9fffA-Za-z0-9]')
    for i, ch in enumerate(canon):
        if keep.match(ch):
            idx.append(i)
    idx.append(len(canon))
    return canon, canon_n, idx, heads


def threshold(n):
    if n < 100:
        return 84
    if n < 300:
        return 78
    return 72


def head_level(a, b, heads):
    best = 0
    span = max(b - a, 1)
    for s, e, lvl in heads:
        if e <= a or s >= b:
            continue
        ov = min(b, e) - max(a, s)
        if ov >= 0.6 * (e - s) and ov >= 0.45 * span and (best == 0 or lvl < best):
            best = lvl
    return best


def _simp(text):
    import zhconv
    return zhconv.convert(text, 'zh-cn')


def _simp_mask(text, mask):
    import zhconv
    out_t, out_m = [], []
    for i, c in enumerate(text):
        cc = zhconv.convert(c, 'zh-cn')
        if len(cc) != 1:
            cc = c
        out_t.append(cc)
        out_m.append(mask[i] if i < len(mask) else False)
    return ''.join(out_t), out_m


def _block(kind, text, mask, verified, hl, y0, sz, bb):
    """统一的导出层块：[kind, text, mask, verified, hlevel, y0, size, bbox]。"""
    return [kind, text, mask, verified, hl, y0, sz, bb]


def _tail8(block):
    """取 8 字段块的 (y0, size, bbox)，用于替换正文时保留版式信息。"""
    return (block[5] if len(block) > 5 else None,
            block[6] if len(block) > 6 else 0,
            block[7] if len(block) > 7 else None)


def _coverage(emitted, a, b):
    """已输出区间 [a,b) 的覆盖率。"""
    tot = b - a
    if tot <= 0:
        return 1.0
    ov = 0
    for s, e in emitted:
        if e <= a or s >= b:
            continue
        ov += min(b, e) - max(a, s)
    return ov / float(tot)


def correct_blocks(blocks, canon_path):
    """对段落块逐段对齐，返回统一的 8 字段块。

    与 job 的未对齐路径保持一致：索引 5=y0、6=字号、7=图框，
    否则启用对齐后插图尺寸/位置和字号会丢失。
    """
    canon, canon_n, idx, heads = load_canonical(canon_path)
    emitted = []
    ptr = 0
    out = []
    for b in blocks:
        kind, text, mask = b[0], b[1], b[2]
        y0 = b[3] if len(b) > 3 else None
        sz = b[4] if len(b) > 4 else 0
        bb = b[5] if len(b) > 5 else None
        if kind in PASS_KINDS:
            # 页码标记/页眉/插图/整页截图/表格/版面 JSON 不参与对齐
            # （页眉文字在权威文本里会命中，若对齐会被当成"重复"误删；
            # frametree/linepage 是整页 JSON，更不能按正文替换）
            out.append(_block(kind, text, mask, True, 0, y0, sz, bb))
            continue
        on = norm(_simp(text))
        if len(on) < 8:
            t2, m2 = _simp_mask(text, mask)
            out.append(_block(kind, t2, m2, False, 0, y0, sz, bb))
            continue
        lo = max(0, ptr - WIN_BACK)
        hi = min(len(canon_n), ptr + WIN_FWD)
        al = fuzz.partial_ratio_alignment(on, canon_n[lo:hi])
        d_start, d_end = lo + al.dest_start, lo + al.dest_end
        cov = _coverage(emitted, d_start, d_end)
        good = (al.score >= threshold(len(on))
                and (d_end - d_start) >= len(on) * 0.6)
        if good and cov > 0.75:
            continue                       # 已经出现过 → 重复，丢弃
        if good:
            a = idx[min(d_start, len(idx) - 1)]
            end = idx[min(d_end, len(idx) - 1)]
            repl = canon[a:end].strip()
            if CJK.search(repl):
                repl = re.sub(u'\\s+', '', repl)
            if repl:
                out.append(_block(kind, repl, [False] * len(repl), True,
                                  head_level(d_start, d_end, heads),
                                  y0, sz, bb))
                emitted.append((d_start, d_end))
                ptr = max(ptr, d_end)
                continue
        t2, m2 = _simp_mask(text, mask)
        out.append(_block(kind, t2, m2, False, 0, y0, sz, bb))
    return _second_pass(out, canon, canon_n, idx, heads, emitted, ptr)


def _second_pass(out, canon, canon_n, idx, heads, emitted, ptr_end):
    """把连续未匹配的段拼起来重试（OCR 常把一段拆成多行）。"""
    prev_end = 0
    out2 = []
    i, n = 0, len(out)
    while i < n:
        verified = out[i][3]
        if verified or out[i][0] == 'page':
            out2.append(out[i])
            i += 1
            continue
        j = i
        while j < n and not out[j][3] and out[j][0] != 'page':
            j += 1
        run = out[i:j]
        on = norm(_simp(''.join(r[1] for r in run)))
        done = False
        if len(on) >= 24:
            lo = max(0, prev_end - 3000)
            hi = min(len(canon_n), ptr_end + WIN_FWD)
            if hi - lo >= len(on) * 0.5:
                al = fuzz.partial_ratio_alignment(on, canon_n[lo:hi])
                ds, de = lo + al.dest_start, lo + al.dest_end
                cov = _coverage(emitted, ds, de)
                if al.score >= 86 and (de - ds) >= len(on) * 0.6 and cov < 0.5:
                    a = idx[min(ds, len(idx) - 1)]
                    end = idx[min(de, len(idx) - 1)]
                    repl = canon[a:end].strip()
                    if CJK.search(repl):
                        repl = re.sub(u'\\s+', '', repl)
                    if repl:
                        out2.append(_block(run[0][0], repl,
                                           [False] * len(repl), True,
                                           head_level(ds, de, heads),
                                           *_tail8(run[0])))
                        emitted.append((ds, de))
                        prev_end = de
                        done = True
        if not done:
            out2.extend(run)
        i = j
    return _third_pass(out2, canon, canon_n, idx, heads, emitted)


def _third_pass(out, canon, canon_n, idx, heads, emitted):
    """全局检索：仍在权威文本里的 → 采用原文或判重丢弃。"""
    kw = 12
    kmer = {}
    for i in range(0, len(canon_n) - kw + 1, 2):
        kmer.setdefault(canon_n[i:i + kw], []).append(i)

    def find_global(on):
        if len(on) < kw:
            return 0, 0, 0
        for off in range(0, len(on) - kw + 1, 3):
            for p in kmer.get(on[off:off + kw], ())[:6]:
                s = max(0, p - off - 300)
                e = min(len(canon_n), p - off + len(on) + 300)
                al = fuzz.partial_ratio_alignment(on, canon_n[s:e])
                if al.score >= 88 and (al.dest_end - al.dest_start) >= len(on) * 0.6:
                    return al.score, s + al.dest_start, s + al.dest_end
        return 0, 0, 0

    out3 = []
    for b6 in out:
        kind, text, mask, verified, hl = b6[:5]
        if verified or kind == 'page' or len(text) < 12:
            out3.append(b6)
            continue
        sc, ds, de = find_global(norm(_simp(text)))
        if sc:
            if _coverage(emitted, ds, de) > 0.55:
                continue
            a = idx[min(ds, len(idx) - 1)]
            end = idx[min(de, len(idx) - 1)]
            repl = canon[a:end].strip()
            if CJK.search(repl):
                repl = re.sub(u'\\s+', '', repl)
            if repl:
                out3.append(_block(kind, repl, [False] * len(repl), True,
                                   head_level(ds, de, heads), *_tail8(b6)))
                emitted.append((ds, de))
                continue
        out3.append(b6)
    return out3
