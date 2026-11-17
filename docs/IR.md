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

## 证据契约（硬约束）

每个 `block` 必须能回答“它在原书哪里”：

- `id`：页内唯一；
- `provenance.page` / `provenance.bbox`；
- `source`：`text_layer` / `ocr` / `table_ir` / `image`；
- `engine`：实际产生这条证据的层；
- `confidence`：0–1，低置信度不隐藏；
- 表格带 `table.rows/merges/widths`，图带 `path` 或 `bbox`。

`bbox` 无法反查时写 `null`，同时写 `bbox_reason`，并把 confidence 降到 0.5；**不允许假装有 bbox**。

## 失败可见

`PageIR` 新增：

```json
{
  "missing": [{
    "text": "页顶被过滤的一行",
    "bbox": [x0,y0,x1,y1],
    "confidence": 0.0,
    "reason": "filtered_or_unmatched"
  }],
  "quality": {
    "block_count": 7,
    "missing_count": 2,
    "min_confidence": 0.5,
    "fallbacks": ["page_image"]
  }
}
```

`DocumentIR.metadata` 汇总：

```json
{
  "quality": {"pages": 8, "blocks": 47, "missing_lines": 31,
               "fallback_pages": 1, "issues": 32},
  "validation": [...]
}
```

规则：

- 源行没有被任何 block / 表格 bbox / 图 bbox 覆盖 → 进 `missing`，confidence=0；
- 整页截图 block 标 `fallback: "page_image"`，confidence=0；
- `validate()` 把缺 bbox、缺 engine、坏表格、整页截图未标记等列为问题；
- 导出时打印 `IR 证据检查`，有缺失/整页图时打印 `[低置信度]`。

JSON 里 `missing` 不是“已修复”，是**明确记录失败**；下游不能把它当正文。

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
