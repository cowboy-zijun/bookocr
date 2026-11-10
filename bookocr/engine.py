# -*- coding: utf-8 -*-
"""OCR 引擎。

对每页生成三份渲染（原始二值、两倍放大、灰度），分别识别后按行对齐、
逐字投票：多数一致的取多数，三份全不一致的字标为"可疑"（导出时标红）。

并行 + 断点续跑：每个进程写自己的分片文件，每页完成立即落盘；
已完成的页在下次运行时自动跳过。
"""
import collections
import json
import multiprocessing as mp
import os
import re
import time

import cv2
import numpy as np
import pymupdf

from .textclean import fix_symbol_text, is_watermark_line, merge_sub_lines

cv2.setNumThreads(1)
os.environ.setdefault("OMP_NUM_THREADS", "3")

CJK = re.compile(u'[㐀-鿿]')
BAD = re.compile(u'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]')  # XML 非法字符


def _to_arr(pix):
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n >= 3:
        return np.ascontiguousarray(a[:, :, :3])
    return np.ascontiguousarray(a[:, :, 0])


def render_variants(doc, pno, dpi=200):
    """三种不同渲染，制造相互独立的识别结果。

    只有"整页扫描底图"才拿嵌入位图当第一路：很多页面带一个小徽标/
    插图，如果直接取 get_images()[0]，OCR 会只认那张小图，整页文字
    全丢。没有整页底图时，用不同 DPI 的页面渲染凑三路，避免第一路
    和第三路是同一张图。
    """
    page = doc[pno]
    pw, ph = float(page.rect.width), float(page.rect.height)
    xref, best_area = 0, 0.0
    best_box = None
    try:
        for info in page.get_image_info(xrefs=True):
            b = info.get('bbox') or (0, 0, 0, 0)
            w, h = max(0.0, b[2] - b[0]), max(0.0, b[3] - b[1])
            area = w * h
            if info.get('xref') and area > best_area:
                xref, best_area, best_box = info['xref'], area, (w, h)
    except Exception:
        pass
    # 整页扫描：位图至少覆盖页面的 70% 宽和高
    embedded = bool(xref and best_box and
                    best_box[0] >= pw * 0.70 and
                    best_box[1] >= ph * 0.70)
    native = None
    if embedded:
        try:
            native = _to_arr(pymupdf.Pixmap(doc, xref))
        except Exception:
            native = None
            embedded = False
    if native is None:
        native = _to_arr(page.get_pixmap(dpi=dpi))
    out = [
        native,
        cv2.resize(native, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC),
    ]
    if embedded:
        out.append(_to_arr(page.get_pixmap(dpi=dpi)))
    else:
        # 页面渲染没有"原生位图"这一路：用略低 DPI 再渲染一次，
        # 让第三路与第一路不是同一张图（否则交叉投票等于只认两遍）。
        alt_dpi = max(36, int(round(dpi * 0.82)))
        if alt_dpi == dpi:
            alt_dpi = max(24, dpi - 8)
        try:
            out.append(_to_arr(page.get_pixmap(dpi=alt_dpi)))
        except Exception:
            out.append(cv2.convertScaleAbs(native, alpha=1.06, beta=3))
    return out


def _scale_boxes(res, from_shape, to_shape):
    sy = to_shape[0] / from_shape[0]
    sx = to_shape[1] / from_shape[1]
    items = []
    for box, text, score in (res or []):
        pts = [(float(p[0]) * sx, float(p[1]) * sy) for p in box]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        items.append({
            'text': text,
            'score': float(score),
            'x0': min(xs), 'x1': max(xs),
            'y0': min(ys), 'y1': max(ys),
            'yc': (min(ys) + max(ys)) / 2.0,
        })
    return items


def ensemble(per_variant):
    """行对齐 + 逐字投票，返回统一坐标下的行。"""
    base = per_variant[0]
    merged = []
    for b in base:
        cands = [b]
        for other in per_variant[1:]:
            best, bestd = None, 1e18
            for o in other:
                d = abs(o['yc'] - b['yc'])
                if d < bestd:
                    best, bestd = o, d
            if best is not None and bestd < (b['y1'] - b['y0']) * 0.6:
                cands.append(best)
        texts = [c['text'] for c in cands]
        if len(texts) >= 2 and len(set(texts)) > 1 and len(set(len(t) for t in texts)) == 1:
            voted = []
            for pos in range(len(texts[0])):
                cnt = collections.Counter(t[pos] for t in texts)
                top = cnt.most_common()
                if len(top) > 1 and top[0][1] == top[1][1]:
                    voted.append(max(cands, key=lambda c: c['score'])['text'][pos])
                else:
                    voted.append(top[0][0])
            text = ''.join(voted)
            score = max(c['score'] for c in cands)
        else:
            bestc = max(cands, key=lambda c: c['score'])
            text, score = bestc['text'], bestc['score']
        merged.append({
            'text': BAD.sub('', text),
            'score': round(score, 4),
            'x0': round(b['x0'], 1), 'x1': round(b['x1'], 1),
            'y0': round(b['y0'], 1), 'y1': round(b['y1'], 1),
            'agree': len(set(texts)) == 1,
            'alts': texts,
        })
    merged.sort(key=lambda m: m['y0'])
    return merged


def _add_size(items, rect):
    """给 OCR 行估字号（交给 format 插件层，默认按框高）。"""
    from . import format as _fmt
    return _fmt.estimate_line_sizes(items, rect)


def ocr_page(ocr, doc, pno, dpi=200, variants=3):
    """识别一页。variants=3：三路交叉 + 逐字投票（可标红可疑字）；
    variants=1：标准模式，只认一遍（约 3 倍速，无红字标记）。

    坐标统一归一化到版面模块使用的标准空间（宽 3400、高 5000），
    与页面的实际尺寸无关（小页型书、大页型书同样适用）。
    """
    if variants <= 1:
        img = _to_arr(doc[pno].get_pixmap(dpi=dpi))
        res, _ = ocr(np.ascontiguousarray(img))
        items = _scale_boxes(res, img.shape, (5000.0, 3400.0))
        items = _add_size(items, doc[pno].rect)
        return [dict(l, alts=[], text=BAD.sub('', l['text'])) for l in items]
    imgs = render_variants(doc, pno, dpi)
    per = []
    for img in imgs:
        res, _ = ocr(img)
        per.append(_scale_boxes(res, img.shape, (5000.0, 3400.0)))
    return _add_size(ensemble(per), doc[pno].rect)


def _worker(shard, nshards, pdf_path, work_dir, dpi, stop_evt, page_range=None,
            variants=3):
    """进程入口：处理自己分到的页，写自己的分片文件（可续跑）。"""
    from rapidocr_onnxruntime import RapidOCR
    doc = pymupdf.open(pdf_path)
    ocr = RapidOCR(intra_op_num_threads=3, use_cls=False, rec_batch_num=16)
    outpath = os.path.join(work_dir, 'shard_%02d.jsonl' % shard)
    done = set()
    # 断点续跑要看工作目录里所有分片，不能只看自己这一片：并行数
    # 会随可用内存变化（6 进程变 5 进程），分页取模的归属就变了，
    # 只看自己的分片会把别的分片已识别过的页全部重跑一遍。
    import glob as _glob
    for _fp in _glob.glob(os.path.join(work_dir, 'shard_*.jsonl')):
        try:
            with open(_fp, encoding='utf-8') as f:
                for line in f:
                    try:
                        o = json.loads(line)
                        # 旧版本把“识别异常”也写成空结果，续跑时被永久跳过；
                        # 只有真正有文字、或明确标记 blank 的空白页才算完成。
                        if o.get('lines') or o.get('blank'):
                            done.add(o['page'])
                    except Exception:
                        pass
        except OSError:
            pass
    n = doc.page_count
    with open(outpath, 'a', encoding='utf-8') as f:
        for pno in range(shard, n, nshards):
            if (pno + 1) in done:
                continue
            if page_range is not None and (pno + 1) not in page_range:
                continue
            if stop_evt.is_set():
                break
            try:
                lines = ocr_page(ocr, doc, pno, dpi, variants)
            except Exception:
                # 偶发异常重试一次；仍失败就不落盘，下次续跑会重新识别，
                # 不能写空结果把这一页永久判成“已完成”。
                try:
                    lines = ocr_page(ocr, doc, pno, dpi, variants)
                except Exception:
                    continue
            rec = {'page': pno + 1, 'lines': lines}
            if not lines:
                rec['blank'] = True      # 真正的空白页：标记后不再重试
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            f.flush()
    doc.close()


def sanitize_shards(work_dir):
    """清掉分片里“同一页出现两段完全不同的文字”的记录。

    换过并行数会留下重复页（同一页、同一本书，文字几乎一样，无害）；
    混入过另一本书时，同一页的两份文字会差得很远。返回丢弃的页数。
    """
    import difflib
    import glob
    files = sorted(glob.glob(os.path.join(work_dir, 'shard_*.jsonl')))
    if not files:
        return 0
    page_items = {}
    for fp in files:
        try:
            with open(fp, encoding='utf-8', errors='replace') as f:
                for line in f:
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    t = ''.join(l.get('text', '')
                                for l in (o.get('lines') or []))
                    page_items.setdefault(o.get('page'), []).append(
                        (fp, line, t))
        except Exception:
            continue
    bad = set()
    for pg, items in page_items.items():
        texts = [t for _, _, t in items if t]
        if len(texts) < 2:
            continue
        sim = difflib.SequenceMatcher(None, texts[0][:200],
                                      texts[-1][:200]).ratio()
        if sim < 0.55:
            bad.add(pg)
    if not bad:
        return 0
    for fp in files:
        try:
            with open(fp, encoding='utf-8', errors='replace') as f:
                lines = f.read().splitlines()
        except Exception:
            continue
        keep = []
        for line in lines:
            try:
                pg = json.loads(line).get('page')
            except Exception:
                keep.append(line)
                continue
            if pg not in bad:
                keep.append(line)
        with open(fp, 'w', encoding='utf-8') as f:
            for line in keep:
                f.write(line + '\n')
    return len(bad)


def run_ocr(pdf_path, work_dir, n_pages, workers=4, dpi=200,
            progress=None, stop_evt=None, page_range=None, variants=3):
    """并行 OCR；返回 {页码: 行列表}。每页即写即存。

    page_range: 可选，1 起始页码的集合，只处理这些页。
    variants: 3=三路交叉（可标红），1=标准快速模式。
    """
    os.makedirs(work_dir, exist_ok=True)
    if stop_evt is None:
        stop_evt = mp.Event()
    procs = []
    for i in range(workers):
        p = mp.Process(target=_worker,
                       args=(i, workers, pdf_path, work_dir, dpi, stop_evt,
                             page_range, variants))
        p.start()
        procs.append(p)
    last = 0
    seen, offsets = set(), {}
    while any(p.is_alive() for p in procs):
        if progress:
            done = _count_done_incremental(work_dir, seen, offsets)
            if done != last:
                progress(done, n_pages)
                last = done
        time.sleep(0.6)
    for p in procs:
        p.join()
    return load_pages(work_dir)


def crop_region(pdf_path, pno, bbox, out_dir, dpi=200, suffix='table',
                doc=None):
    """按标准坐标 (x0,y0,x1,y1) 截取页面的一块区域（表格/插图截图）。"""
    _own = doc is None
    if doc is None:
        doc = pymupdf.open(pdf_path)
    page = doc[pno - 1]
    rx = page.rect.width / 3400.0
    ry = page.rect.height / 5000.0
    x0, y0, x1, y1 = bbox
    rect = pymupdf.Rect(x0 * rx, y0 * ry, x1 * rx, y1 * ry)
    pix = page.get_pixmap(clip=rect, dpi=dpi)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, 'p%04d_%s.png' % (pno, suffix))
    pix.save(path)
    if _own:
        doc.close()
    return path


def extract_page_images(pdf_path, pno, out_dir, dpi=170, doc=None):
    """提取一页里的插图，返回 [(y0, 图片路径, 矩形)]（y0 为标准空间坐标）。

    过滤：小图标/装饰（<250×180）、整页扫描背景（>3000 宽或 >4200 高）、
    落在页眉页脚区里的图。图片用区域渲染方式截取，与原书像素一致。

    doc: 可传入已打开的文档，整本书逐页提取时复用，避免每页重开
    一个上百 MB 的 PDF（807 页的书能省好几分钟）。
    """
    _own = doc is None
    if doc is None:
        doc = pymupdf.open(pdf_path)
    page = doc[pno - 1]
    rect = page.rect
    sx = 3400.0 / rect.width
    sy = 5000.0 / rect.height
    _has_text = len(page.get_text('text').strip()) > 100
    # 本页文字行（页坐标）：用于判断图下方是否有紧邻的图注。
    # 图注是短行（<60 字符）；正文里引用 "Figure 2" 的长句不算。
    cap_lines = []
    for block in page.get_text('dict').get('blocks', []):
        if block.get('type') != 0:
            continue
        for line in block.get('lines', []):
            t = ''.join(s['text'] for s in line.get('spans', [])).strip()
            if len(t) < 60 and t.startswith(('Figure', 'Table', u'图')):
                cap_lines.append((line['bbox'][1], t))
    out = []
    seen = set()
    for i, info in enumerate(page.get_image_info(xrefs=True)):
        bb = info['bbox']
        w = (bb[2] - bb[0]) * sx
        h = (bb[3] - bb[1]) * sy
        y0 = bb[1] * sy
        y1 = bb[3] * sy
        if _has_text:
            # 文字层页里的小图可能正是公式/方框图（179×55 这种），
            # 只排除真正的图标；扫描页仍用原来的 250×180 门槛。
            if w < 120 or h < 35:
                continue
        else:
            if w < 250 or h < 180:
                continue                    # 小图标/装饰
        if w > 3000 or h > 4200:
            continue                    # 整页扫描背景（扫描页）
        if y1 < 750 or y0 > 4650:
            continue                    # 页眉页脚区
        if y0 < 0 or y1 < 1000:
            continue                    # 页顶出血图/徽标装饰（页面前 20%）
        # 小图且下方没有紧邻图注 → 正文嵌图/装饰，不提取（用户：没事别出来）
        if (bb[3] - bb[1]) < 0.25 * rect.height:
            has_cap = any(cy >= bb[1] and cy <= bb[3] + 250
                          for cy, _ct in cap_lines)
            if not has_cap:
                # 公式/方框图通常很宽很扁，或只有一行高；文字层页放行，
                # 扫描页仍按“没事别出来”过滤。
                _shape_ok = (_has_text and h >= 35 and
                             (w / max(1.0, h) >= 1.35 or h >= 60))
                if not _shape_ok:
                    continue
        xref = info.get('xref', 0)
        if xref and xref in seen:
            continue                    # 同一张图重复放置
        if xref:
            seen.add(xref)
        path = os.path.join(out_dir, 'p%04d_img%02d.png' % (pno, i))
        # 文字层里的公式/方框图通常就是原始嵌入图；直接取 xref 的像素，
        # 不会把 PDF 页面上斜排水印一起渲染进来。大图/照片仍按页面矩形
        # 裁切，保留原书的裁切和位置。
        _use_embedded = bool(_has_text and xref and
                             (h < 180 or (w / max(1.0, h)) >= 1.35))
        if _use_embedded:
            try:
                _im = doc.extract_image(xref)
                _ext = _im.get('ext') or 'png'
                path = os.path.join(
                    out_dir, 'p%04d_img%02d.%s' % (pno, i, _ext))
                with open(path, 'wb') as _f:
                    _f.write(_im['image'])
                out.append((y0, path, (bb[0] * sx, bb[1] * sy,
                                       bb[2] * sx, bb[3] * sy)))
                continue
            except Exception:
                path = os.path.join(out_dir, 'p%04d_img%02d.png' % (pno, i))
        # dpi 自适应：目标宽度约 1000px，大图不用超清渲染（省时省空间）
        wpt = bb[2] - bb[0]
        use_dpi = min(dpi, 1000 * 72 / wpt) if wpt > 0 else dpi
        try:
            page.get_pixmap(clip=bb, dpi=use_dpi).save(path)
        except Exception:
            continue
        # 带上图片在原书上的矩形（标准坐标），导出时按这个矩形定尺寸，
        # 图占多大地方就留多大地方，下面的正文才不会被挤走
        out.append((y0, path, (bb[0] * sx, bb[1] * sy,
                               bb[2] * sx, bb[3] * sy)))
    if _own:
        doc.close()
    out.sort(key=lambda x: x[0])
    return out


def extract_text_pages(pdf_path, page_range=None):
    """PDF 自带文字层的页：直接提取，免 OCR（快、零错字）。

    坐标统一换算到版面重建所用的空间（宽 3400、高 5000），
    首行缩进等信息随行框保留。返回 {页码: 行列表}。
    """
    doc = pymupdf.open(pdf_path)
    pages = {}
    for pno in range(doc.page_count):
        if page_range is not None and (pno + 1) not in page_range:
            continue
        page = doc[pno]
        rect = page.rect
        sx = 3400.0 / rect.width
        sy = 5000.0 / rect.height
        lines = []
        for block in page.get_text('dict').get('blocks', []):
            if block.get('type') != 0:
                continue
            for line in block.get('lines', []):
                # 斜向/竖排水印（如学校/机构名）不是正文：
                # 文字层的 dir=(0.707,0.707)，不删会变成 38pt 大字。
                if is_watermark_line(line):
                    continue
                # 行内 span 按 x 排序：上下标/公式碎片在 PDF 里可能乱序，
                # 不排序会把下标 n 排到句末。
                _spans = sorted(line.get('spans', []),
                                key=lambda s: (s.get('bbox') or (0, 0, 0, 0))[0])
                text = ''
                prev_x1 = None
                spans = []
                for s in _spans:
                    st = fix_symbol_text(s['text'])
                    if not st.strip():
                        # 全角空格是中文书里"一　国内的政治状况""第一件　将农民…"
                        # 的分隔符，丢了标题会粘成一串；普通空格不补（位置
                        # 信息已经能判断词间空隙）。
                        if u'　' in st and text and \
                                not text.endswith(u'　'):
                            text += u'　'
                        elif len(st) >= 2 and text and \
                                not text.endswith(' '):
                            # 连续两个半角空格：行内小标题与正文之间的有意
                            # 分隔（"2.3.3 参数优化与模型训练  由于…"）。
                            # 后面去空格只去单空格，这里补两个，merge_row
                            # 再收成一个，标题就不会和正文粘死。
                            text += '  '
                            prev_x1 = (s.get('bbox') or (0, 0, 0, 0))[2]
                        continue
                    sb = s.get('bbox') or (0, 0, 0, 0)
                    if prev_x1 is not None and (sb[0] - prev_x1) > 3.0:
                        text += ' '
                    text += st
                    prev_x1 = sb[2]
                    spans.append({
                        'text': st.strip(),
                        'font': s.get('font') or '',
                        'x0': round(sb[0] * sx, 1),
                        'x1': round(sb[2] * sx, 1),
                        'y0': round(sb[1] * sy, 1),
                        'y1': round(sb[3] * sy, 1),
                        'size': round(float(s.get('size') or 0), 2),
                    })
                text = text.strip()
                if not text:
                    continue
                # PDF 文字层常在标点后带一个空格（"。 在"、"毛 泽 东"），
                # 中文排版里这些都是多余的：每行多占一两个字宽，整页
                # 就会多折一行，一本书下来页页溢出。
                # 注意只去半角空格：全角空格"　"是中文书里有意排的分隔
                # （"一　国内的政治状况"），不能删。
                text = re.sub(
                    u'(?<=[\\u4e00-\\u9fff\\u3000-\\u303f\\uff01-\\uffef'
                    u'“”‘’—…])(?<! ) '
                    u'(?=[\\u4e00-\\u9fff\\u3000-\\u303f\\uff01-\\uffef'
                    u'“”‘’—…])(?! )', u'', text)
                bb = line['bbox']
                sizes = sorted(s['size'] for s in spans if s['size'])
                size = sizes[len(sizes) // 2] if sizes else 0.0
                lines.append({
                    'text': text,
                    'score': 1.0,
                    'x0': round(bb[0] * sx, 1), 'x1': round(bb[2] * sx, 1),
                    'y0': round(bb[1] * sy, 1), 'y1': round(bb[3] * sy, 1),
                    'agree': True,
                    'alts': [],
                    'spans': spans,
                    'size': size,
                })
        # 脚注分隔线：PDF 里画的横线（页面下半部、宽度适中）。
        # 以 sep 伪行注入，供 layout.footnote_start 识别脚注起点；
        # 正式排版时会被丢弃，不会出现在 Word 里。
        try:
            for dr in page.get_drawings():
                for it in dr.get('items', []):
                    if it[0] == 'l':
                        p1, p2 = it[1], it[2]
                        if abs(p1.y - p2.y) <= 2 and \
                                25 <= abs(p1.x - p2.x) <= rect.width * 0.55 \
                                and p1.y > rect.height * 0.5:
                            xa, xb = sorted((p1.x, p2.x))
                            lines.append({
                                'text': u'———', 'score': 1.0,
                                'x0': round(xa * sx, 1), 'x1': round(xb * sx, 1),
                                'y0': round(p1.y * sy, 1),
                                'y1': round(p1.y * sy, 1) + 1,
                                'agree': True, 'alts': [], 'spans': [],
                                'size': 0, 'sep': True,
                            })
                    elif it[0] == 're':
                        rr = it[1]
                        if rr.height <= 3 and \
                                25 <= rr.width <= rect.width * 0.55 \
                                and rr.y0 > rect.height * 0.5:
                            lines.append({
                                'text': u'———', 'score': 1.0,
                                'x0': round(rr.x0 * sx, 1),
                                'x1': round(rr.x1 * sx, 1),
                                'y0': round(rr.y0 * sy, 1),
                                'y1': round(rr.y0 * sy, 1) + 1,
                                'agree': True, 'alts': [], 'spans': [],
                                'size': 0, 'sep': True,
                            })
        except Exception:
            pass
        # 有的 PDF 文字层没有坐标（每个字都是 0×0 的框、字号 0）：版面重建
        # 用不了，但字的顺序是对的。丢掉的话这些页会退到 OCR，把封面书名
        # 读成"卷第毛泽东选集"。按原顺序拼成一行保留，标 repaired，
        # 让上层知道这是"可用的文字层"。
        if lines and all(l['x1'] <= l['x0'] and l['y1'] <= l['y0']
                         for l in lines):
            parts = []
            for l in lines:
                t = l['text'].strip()
                if t:
                    parts.append(t)
                elif u'　' in l['text'] and parts:
                    parts.append(u'　')
            if parts:
                # 放在页面中部：放在页顶会被当成页眉删掉，放页底会被当页码
                lines = [{
                    'text': ''.join(parts), 'score': 1.0,
                    'x0': 0.0, 'x1': round(rect.width * sx, 1),
                    'y0': round(rect.height * sy * 0.40, 1),
                    'y1': round(rect.height * sy * 0.50, 1),
                    'agree': True, 'alts': [], 'spans': [],
                    'size': 0.0, 'repaired': True,
                }]
        lines = merge_sub_lines(lines)
        pages[pno + 1] = lines
    doc.close()
    return pages


def _count_done_incremental(work_dir, seen, offsets):
    """增量统计已完成页：只读分片新增的完整行，避免每 0.6 秒重扫整本。

    seen: 已见页码集合，offsets: {文件名: 已读字节偏移}，跨调用复用。
    进度只需近似值，因此允许分片里个别坏行被跳过。
    """
    try:
        names = os.listdir(work_dir)
    except OSError:
        return len(seen)
    for fn in names:
        if not fn.startswith('shard_') or not fn.endswith('.jsonl'):
            continue
        path = os.path.join(work_dir, fn)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        off = offsets.get(fn, 0)
        if size < off:                 # 文件被重写/截断
            off = 0
        if size == off:
            continue
        try:
            with open(path, 'rb') as f:
                f.seek(off)
                data = f.read()
        except OSError:
            continue
        cut = data.rfind(b'\n')        # 只消费完整行，半行留到下次
        if cut < 0:
            continue
        block = data[:cut + 1]
        offsets[fn] = off + len(block)
        for line in block.splitlines():
            try:
                seen.add(json.loads(line)['page'])
            except Exception:
                pass
    return len(seen)


def count_done(work_dir):
    seen = set()
    for fn in os.listdir(work_dir):
        if not fn.startswith('shard_') or not fn.endswith('.jsonl'):
            continue
        with open(os.path.join(work_dir, fn), encoding='utf-8') as f:
            for line in f:
                try:
                    seen.add(json.loads(line)['page'])
                except Exception:
                    pass
    return len(seen)


def load_pages(work_dir):
    pages = {}
    for fn in os.listdir(work_dir):
        if not fn.startswith('shard_') or not fn.endswith('.jsonl'):
            continue
        with open(os.path.join(work_dir, fn), encoding='utf-8') as f:
            for line in f:
                try:
                    r = json.loads(line)
                    pages[r['page']] = r['lines']
                except Exception:
                    pass
    return pages
