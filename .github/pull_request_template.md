## 改动

- 解决了什么问题：
- 影响范围：版面 / 表格 / 导出 / OCR / 文档

## 验证

- [ ] `python -m compileall -q bookocr cli.py main.py`
- [ ] `python -m bookocr.regress ...` 通过
- [ ] 没有提交 `.venv/`、`runtime/`、模型、用户 PDF/DOCX

## 已知边界

（复杂表/公式/双栏是否仍有限制）
