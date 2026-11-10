# -*- coding: utf-8 -*-
"""保真度量：把导出的 docx 再排成 PDF，逐页和原书比。

不追求“像素完全一样”（可编辑 Word 做不到），而是给每页一个可比较的
分数，用来发现“半页空、页眉被当正文、横排页被压扁、表格错位、整页
截图”这类回归。指标：
- 页尺寸差（pt）
- 有文字层的页：原书/输出行数与首末行位置差（pt）
- 像素相似度：两页灰度图缩放后 1 - 平均绝对差/255
"""
import os
import tempfile


def _page_lines(page):
    out = []
    for b in page.get_text('dict').get('blocks', []):
        if b.get('type') != 0:
            continue
        for l in b.get('lines', []):
            t = ''.join(s.get('text') or '' for s in l.get('spans', []))
            if t.strip():
                out.append(l['bbox'])
    return out


def _similarity(a, b):
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    try:
        w = min(a.shape[1], b.shape[1], 1200)
        ha = max(1, int(a.shape[0] * w / max(1, a.shape[1])))
        hb = max(1, int(b.shape[0] * w / max(1, b.shape[1])))
        aa = cv2.resize(a, (w, ha), interpolation=cv2.INTER_AREA)
        bb = cv2.resize(b, (w, hb), interpolation=cv2.INTER_AREA)
        h = max(aa.shape[0], bb.shape[0])
        if aa.shape[0] != h:
            aa = cv2.resize(aa, (w, h), interpolation=cv2.INTER_AREA)
        if bb.shape[0] != h:
            bb = cv2.resize(bb, (w, h), interpolation=cv2.INTER_AREA)
        d = float(np.mean(np.abs(aa.astype('float32') -
                                 bb.astype('float32')))) / 255.0
        return max(0.0, 1.0 - d)
    except Exception:
        return None


def _gray(page, dpi=110):
    try:
        import numpy as np
        pix = page.get_pixmap(dpi=dpi)
        a = np.frombuffer(pix.samples, dtype=np.uint8)
        if pix.n >= 3:
            a = a.reshape(pix.height, pix.width, pix.n)[:, :, :3]
            import cv2
            return cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)
        return a.reshape(pix.height, pix.width)
    except Exception:
        return None


def compare(pdf_path, docx_path, log=None, dpi=110, src_pages=None):
    """返回 {'pages': [...], 'worst': [...], 'summary': {...}} 或 None。

    src_pages: 只处理/只选了这些原书页（1 起始）；输出 PDF 的页序按
    选中页依次对应。不传则整本逐页比。
    """
    from . import export
    import pymupdf

    tmp = tempfile.mktemp(suffix='.pdf')
    try:
        export._docx_to_pdf(docx_path, tmp)
        src = pymupdf.open(pdf_path)
        out = pymupdf.open(tmp)
        if src_pages:
            src_idx = [p for p in sorted(src_pages) if 1 <= p <= src.page_count]
        else:
            src_idx = list(range(1, src.page_count + 1))
        n = min(len(src_idx), out.page_count)
        pages = []
        for i in range(n):
            sp = src[src_idx[i] - 1]
            op = out[i]
            sr, orr = sp.rect, op.rect
            sl, ol = _page_lines(sp), _page_lines(op)
            rec = {
                'page': src_idx[i],
                'size_src': [round(sr.width, 1), round(sr.height, 1)],
                'size_out': [round(orr.width, 1), round(orr.height, 1)],
                'size_diff': round(abs(sr.width - orr.width) +
                                   abs(sr.height - orr.height), 1),
                'lines_src': len(sl),
                'lines_out': len(ol),
                'top_diff': (round(abs(min(l[1] for l in sl) -
                                      min(l[1] for l in ol)), 1)
                             if sl and ol else None),
                'bottom_diff': (round(abs(max(l[3] for l in sl) -
                                         max(l[3] for l in ol)), 1)
                                if sl and ol else None),
            }
            ga, gb = _gray(sp, dpi), _gray(op, dpi)
            rec['similarity'] = (round(_similarity(ga, gb), 4)
                                 if ga is not None and gb is not None else None)
            pages.append(rec)
        worst = sorted([p for p in pages if p['similarity'] is not None],
                       key=lambda p: p['similarity'])[:8]
        summary = {
            'pages_src': len(src_idx),
            'pages_out': out.page_count,
            'mean_similarity': (round(sum(p['similarity'] for p in pages
                                          if p['similarity'] is not None) /
                                      max(1, sum(1 for p in pages
                                                 if p['similarity'] is not None)), 4)
                                if any(p['similarity'] is not None
                                       for p in pages) else None),
            'page_count_ok': len(src_idx) == out.page_count,
        }
        if log:
            log(u'保真度量：原书 %d 页，输出 %d 页，平均像素相似度 %s'
                % (len(src_idx), out.page_count, summary['mean_similarity']))
            for p in worst:
                log(u'  第 %d 页：相似度 %.3f，页尺寸差 %.1fpt，行 %d/%d'
                    % (p['page'], p['similarity'], p['size_diff'],
                       p['lines_src'], p['lines_out']))
        src.close()
        out.close()
        return {'pages': pages, 'worst': worst, 'summary': summary}
    except Exception as e:
        if log:
            log(u'保真度量失败：%s' % e)
        return None
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
