# 书扫通 BookOCR

把**扫描版教材、旧书、期刊论文、实验报告**整理成可编辑、可检索、按原书分页的 Word 文档。

```text
扫描 PDF  →  书扫通  →  可编辑 Word
                         Word 第 N 页 = 原书第 N 页
```

本地处理，不上传 PDF，不需要 token。

---

## 一句话定位

**不是 PDF 阅读器，也不是文献管理器，而是“扫描书 → Word”的本地转换工具。**

适合那些工作流**终点是 Word**的人：要改教材、重排讲义、引用原书段落、把旧资料变成校对稿。

## 不是什么

- **不是 Zotero**：Zotero 管文献、笔记、引用；书扫通只负责把扫描件变成可编辑稿。
- **不是 ABBYY / OCRmyPDF**：它们产出可搜索 PDF；书扫通产出带分页、表格、脚注的 DOCX。
- **不是古书整理工具**：竖排、异体字、双行小字、批注是另一条产品线。
- **不是“一键完美”**：复杂表、公式、双栏期刊仍有边界，需要人工复核。

## 目标用户

| 用户 | 需求 | 现状匹配 |
|---|---|---|
| 研究生 / 论文作者 | 扫描书大段引用、保留原书页码、脚注 | 高 |
| 教师 / 教材讲义编者 | 旧教材、实验指导书改成可编辑讲义 | 高 |
| 编辑 / 校对公司 / 打印店 | 旧书、资料转 Word 校对稿/重排稿 | 中高 |
| 小型图书馆 / 课题组 | 本地、不上传、少量书整理 | 中 |

不适合：只读论文、只做笔记、只需要可搜索 PDF、需要批量几百本无人值守的场景。

## 当前能力

- **文字层优先**：PDF 自带文字层的直接提取，零 OCR 错字；扫描页才走 OCR。
- **三路交叉识别**：同一页三种渲染分别识别，再逐字投票；不一致的字标红。
- **按原书分页**：量原书纸张、版心、字号、行距；`PageDoc` 按页归属，页标账本检查页数。
- **行内样式**：文字层 span 的字号、字体、上标/下标保留；Symbol 私用区转 Unicode 希腊字母。
- **表格**：`TableIR` 统一 rows/merges/widths/quality；三线表、双面板表、合并表尽量还原为 Word 真表，低质量回退截图。
- **横排页**：宽>高的表格/图按横排节保留方向。
- **脚注**：正文末行之下、字号偏小、行首注号综合判定，固定在页底。
- **标题**：居中标题、编号标题、行内小标题保留层级。
- **插图**：文字层里的原始嵌入图优先，避免把页面水印一起截进去。
- **L3 锁行（试验）**：文字层纯正文页按原书段落分组，段内软换行，保留原书断行。
- **保真度量**：`--fidelity` 把 docx 再排成 PDF，逐页比尺寸/行数/首末行/像素相似度。
- **对象级回归**：`python -m bookocr.regress` 检查页标数、表格形状、插图数。

## 已知边界（诚实说明）

- 扫描件模糊时错字率约 **2%–4%**；红字只是“高嫌疑”，黑字也可能错。
- 复杂合并表/新结构表可能回退为整页图；p12 双面板表只在**整本处理**时稳定，单页选择会退默认 32 开。
- p22 模型表是 8×4 真表，但表下公式/说明仍是图。
- 公式按区域截图保留，不转 MathML/LaTeX。
- 扫描期刊双栏仍以书末索引为主，未泛化到期刊正文。
- 页码按附录/参考文献分段加段名，但非连续节选仍可能倾向连续序列。
- L3 锁行是试验模式，默认关闭；不锁表格/图/脚注页。

## 安装与使用

### 便携版（推荐给非技术用户）

解压后双击 `启动书扫通.vbs`。不写注册表，删除文件夹即卸载。

### 本体 / 源码

要求 Windows + Python 3.10+。首次双击 `start.bat` 会创建环境、安装依赖。

图形界面流程：

1. 选择 PDF；
2. 选“图书”或“论文/期刊”；
3. 可选：选权威文本 `.txt`；
4. 开始；可暂停，进度已保存；
5. 完成后打开结果。

### 命令行

```powershell
.venv\Scripts\python.exe cli.py --pdf 书.pdf
.venv\Scripts\python.exe cli.py --pdf 论文.pdf --mode paper
.venv\Scripts\python.exe cli.py --pdf 书.pdf --out 结果.docx --fidelity
.venv\Scripts\python.exe cli.py --pdf 书.pdf --layout-lock
```

常用参数：

| 参数 | 作用 |
|---|---|
| `--mode paper` | 期刊/论文模式：通栏标题 + 双栏正文 |
| `--fidelity` | 导出后做逐页保真度量（较慢，需 Word） |
| `--layout-lock` | 文字层纯正文页 L3 锁行（试验） |
| `--pages 1-20` | 只处理指定页 |
| `--fast` | 单路 OCR，快但错字多；只用于试跑 |
| `--format html/json/all` | 额外输出 IR HTML/JSON；Word 仍导出 |

## 多个出口

Word 是当前主出口，但不是唯一出口。`--format json/html` 会把同一份
PageDoc/TableIR 转成结构化 IR 或独立 HTML：

```powershell
.venv\Scripts\python.exe cli.py --pdf 书.pdf --format json
.venv\Scripts\python.exe cli.py --pdf 书.pdf --format html
.venv\Scripts\python.exe cli.py --pdf 书.pdf --format all
```

JSON 带页号、页几何、块 bbox、表格 merges、来源和置信度；HTML 用于阅读/检索。
Word 仍会导出。

## 对象级回归

```powershell
python -m bookocr.regress 输出.docx --pages 38 ^
  --tables 5x7,10x9,9x8,20x10,8x4,13x7 --images 32
```

当前基线见 [docs/回归集.md](docs/回归集.md)；IR 结构见 [docs/IR.md](docs/IR.md)。

## 目录

```text
bookocr/
├── start.bat / 启动书扫通.vbs
├── main.py                 GUI
├── cli.py                  命令行
├── bookocr/
│   ├── engine.py           文字层/OCR/插图/续跑
│   ├── layout.py           版面重建、页码、样式
│   ├── table.py            表格识别
│   ├── table_ir.py         表格 IR（rows/merges/quality）
│   ├── pagedoc.py          按页归属/对象账本/页几何
│   ├── textclean.py        字体私用区/水印/上下标清洗
│   ├── export.py           Word 导出
│   ├── job.py              流程编排
│   ├── regress.py          对象级回归
│   ├── fidelity.py         docx→PDF 保真度量
│   └── checker.py          页间接缝检查
├── examples/
└── docs/
```

## 许可证

本项目采用 **GNU Affero General Public License v3.0（AGPL-3.0）**，见 [LICENSE](LICENSE)。

依赖说明：

- **PyMuPDF** 为 AGPL-3.0 / 商业双许可；本项目按 AGPL-3.0 分发。
- RapidOCR、rapid_layout 的代码与模型权重、便携版 runtime、字体等第三方组件，
  在打包/再分发前需逐一核对各自许可证；本仓库不包含模型权重和第二方 runtime。
- 示例 PDF、用户处理的 PDF/DOCX、工作目录缓存不进入仓库。

如果你要在闭源/商业产品中使用本项目，需要自行替换 AGPL 依赖或取得相应商业许可。
