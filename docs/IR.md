# Page-faithful Document IR

Word 只是渲染器之一。书扫通的中间层是带页面几何、来源和置信度的文档 IR。

## 结构

```json
{
  "ir_version": 1,
  "metadata": {"source": "book.pdf", "generator": "bookocr"},
  "pages": [{
    "no": 12,
    "width": 595.3,
    "height": 841.9,
    "geometry": {"page_w": 595.3, "page_h": 841.9, "left": 63.8},
    "label": "12",
    "blocks": [{
      "id": "p12b3",
      "type": "heading",
      "level": 1,
      "bbox": [90.6, 120.0, 509.0, 135.0],
      "runs": [{"text": "稳健性检验", "size": 10.4, "bold": true}],
      "source": "text_layer",
      "confidence": 0.98
    }, {
      "id": "p12b4",
      "type": "table",
      "bbox": [80.0, 150.0, 515.0, 400.0],
      "source": "table_ir",
      "confidence": 0.81,
      "table": {"rows": [], "merges": [], "widths": [], "_quality": 0.81}
    }],
    "reading_order": ["p12b3", "p12b4"]
  }]
}
```

## 字段约定

- `pages[].no`：原书 PDF 页序；
- `geometry`：页面/版心几何；
- `blocks[]`：
  - `type`: `heading` / `paragraph` / `footnote` / `quote` / `caption` /
    `table` / `figure` / `toc`；
  - `bbox`: 原书标准坐标（3400×5000）下的 `[x0,y0,x1,y1]`；无法定位时为 `null`；
  - `runs`: 文字 + 字号/字体/上下标/粗斜体/置信度；
  - `source`: `text_layer` / `ocr` / `table_ir` / `image`；
  - `confidence`: 0–1；OCR 红字会降低对应 run 的置信度；
  - `table`: TableIR 的 rows/merges/widths/quality；
- `reading_order`：本页块 id 的阅读顺序。

## 渲染器

当前：

- `render_docx()`：现有 `export.py`，输出 Word；
- `render_html()`：`python -m bookocr.doc_ir` 无 CLI；通过 `--format html` 输出；
- `render_json()`：`--format json` 输出 IR。

计划：

- `render_latex()`：公式/自动排版；
- 插件/知识库直接读 JSON。

## 边界

- 文字块的 bbox 是从源行匹配出来的，复杂表格/公式页可能为 `null`；
- 置信度目前主要来自红字 mask 与表格 quality，不是完整概率校准；
- 扫描页的 OCR 候选/三路结果尚未逐字写入 IR；
- p12 单页几何、p22 表下图、扫描双栏仍是已知限制。
