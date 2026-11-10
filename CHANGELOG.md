# Changelog

## v0.1.0-alpha — 2026-11-10

首个公开 alpha。

- PageDoc：按页归属、页号/页标校验、对象账本、按页尺寸分组几何。
- TableIR：统一 rows/merges/widths/quality；表格插 Word 前归一化。
- 文字清洗层：Symbol 私用区、旋转水印、独立上下标行。
- p12 双面板表：20×10 可编辑表（整本处理）。
- p22 模型表：8×4 可编辑表；表下公式/说明保留为图。
- 实验报告 6×9 横排表；文字/嵌入图水印清洗。
- `--fidelity`、`--layout-lock`（试验）。
- 对象级回归：`python -m bookocr.regress`。
- Document IR：`--format json/html` 输出结构化 JSON/独立 HTML；Word 仍是主出口。

### 已知边界

- p12 单页选择会退默认 32 开几何；
- p22 表下内容为图；
- 公式不转 MathML/LaTeX；
- 扫描期刊双栏未泛化；
- 页码按节严格取号未完成。
