# -*- coding: utf-8 -*-
"""版式还原插件接口：字号估计 + 版面区域分类。

默认（不装任何额外依赖）：
- 字号：OCR 框高估计；文字层 PDF 用 spans 的精确字号
- 区域：按文字结构判标题/正文/脚注（layout/export 里完成）

可选版面模型（二选一，自动检测）：
- rapid-layout（ONNX，推荐）：`pip install rapid-layout`
- PP-Structure（PaddleOCR）：`pip install paddleocr paddlepaddle`
  （paddlepaddle 目前没有 Python 3.14 的轮子，本机用 rapid-layout）

启用后 `page_regions(pdf, pno)` 返回每页的 title/text/table/figure 等区域，
导出时“标题区”按标题排、“表格区”按表格排，不再只靠文字结构猜。
"""
_pp_engine = None
_layout_engine = None
_layout_kind = None


def estimate_line_sizes(items, rect):
    """给 OCR 行估字号（pt）：只用框高。

    宽度会被标点、两端空白、OCR 框留白带偏，同一段正文能差 20%，
    足以把正文误判成标题；框高稳定得多。
    """
    fh = rect.height / 5000.0
    for l in items:
        if l.get('text') and 'size' not in l:
            l['size'] = (l['y1'] - l['y0']) * fh
    return items


# ---------------------------------------------------------------- 版面模型
def pp_structure_available():
    try:
        import paddleocr  # noqa: F401
        return True
    except Exception:
        return False


def rapid_layout_available():
    try:
        import rapid_layout  # noqa: F401
        return True
    except Exception:
        return False


def layout_model_available():
    return rapid_layout_available() or pp_structure_available()


def enable_pp_structure(**kw):
    global _pp_engine, _layout_engine, _layout_kind
    from paddleocr import PPStructure
    _pp_engine = PPStructure(show_log=False, layout=True, **kw)
    _layout_engine = _pp_engine
    _layout_kind = 'paddle'
    return _pp_engine


def enable_layout_model():
    """优先 rapid-layout（ONNX、轻），其次 PP-Structure。返回使用的后端名。"""
    global _layout_engine, _layout_kind
    if _layout_engine is not None:
        return _layout_kind
    if rapid_layout_available():
        from rapid_layout import RapidLayout
        _layout_engine = RapidLayout()
        _layout_kind = 'rapid'
        return _layout_kind
    if pp_structure_available():
        enable_pp_structure()
        return _layout_kind
    return None


def layout_model_enabled():
    return _layout_engine is not None


def page_regions(pdf_path, pno, dpi=120):
    """版面区域：[(y0, y1, label)]（标准坐标，页高 5000）。

    统一后的 label：title / text / table / figure / header / footer / other。
    未启用或失败时返回 []，调用方按“没有额外信息”处理。
    """
    if _layout_engine is None:
        return []
    try:
        import cv2
        import numpy as np
        import pymupdf
        with pymupdf.open(pdf_path) as doc:
            pix = doc[pno - 1].get_pixmap(dpi=dpi)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                pix.height, pix.width, pix.n)
            if pix.n == 3:
                img = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            elif pix.n == 4:
                img = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
            else:
                img = cv2.cvtColor(arr[:, :, 0], cv2.COLOR_GRAY2BGR)
            h = float(pix.height)
            out = []
            if _layout_kind == 'rapid':
                res = _layout_engine(img)
                boxes = getattr(res, 'boxes', []) or []
                names = getattr(res, 'class_names', []) or []
                for box, name in zip(boxes, names):
                    if len(box) < 4:
                        continue
                    x1, y1, x2, y2 = box[:4]
                    out.append((min(y1, y2) / h * 5000.0,
                                max(y1, y2) / h * 5000.0,
                                _norm_label(name)))
            else:
                res = _layout_engine(img) or []
                for item in res:
                    box = item.get('bbox') or []
                    if len(box) != 4:
                        continue
                    out.append((min(box[1], box[3]) / h * 5000.0,
                                max(box[1], box[3]) / h * 5000.0,
                                _norm_label(item.get('type'))))
        return out
    except Exception:
        return []


def _norm_label(name):
    s = (name or '').lower()
    if 'title' in s:
        return 'title'
    if 'table' in s and 'caption' not in s:
        return 'table'
    if 'figure' in s or 'image' in s:
        return 'figure'
    if 'header' in s:
        return 'header'
    if 'footer' in s:
        return 'footer'
    if 'text' in s or 'reference' in s:
        return 'text'
    return 'other'


def region_of(y, regions):
    """某个标准坐标落在哪个区域里；没有区域信息时返回 None。"""
    for y0, y1, label in regions:
        if y0 <= y <= y1:
            return label
    return None
