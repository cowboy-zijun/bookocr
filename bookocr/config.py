# -*- coding: utf-8 -*-
"""运行配置：默认值参考（GUI/CLI 会显式传参，供脚本调用时取默认值）。"""

DEFAULTS = {
    # ---- OCR 引擎 ----
    "workers": 4,                # 并行进程数（每个进程 3 线程）
    "threads": 3,                # 每个 OCR 进程的线程数
    "variants": 3,               # 交叉识别的份数（3 = 原始/放大/灰度；红字标记依赖 3）
    "use_simplified": True,      # 繁体转简体
    "dpi": 200,                  # 灰度渲染分辨率
    "text_score": 0.5,           # OCR 最低置信度

    # ---- 对齐（权威文本）----
    "align": False,              # 是否与权威文本逐段对齐
    "align_file": "",            # 权威文本 .txt 路径
    "match_short": 84,           # 短段相似度阈值
    "match_mid": 78,             # 中段
    "match_long": 72,            # 长段

    # ---- 双栏 ----
    "two_column": True,          # 自动检测双栏页并切缝重排

    # ---- 导出 ----
    "page_per_page": True,       # 一页对一页（Word 第 N 页 = 原书第 N 页）
    "body_font": "宋体",
    "body_size": 11,
    "mark_uncertain": True,      # 可疑字标红
}
