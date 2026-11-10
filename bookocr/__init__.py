# -*- coding: utf-8 -*-
"""书扫通 BookOCR —— 面向大学生的学习辅助工具。

把教材、经典著作、学术专著的扫描 PDF 变成干净、可读、按原书版式分页的
Word 文档：三路交叉识别 + 可疑字标红 + 繁体转简体 + 权威文本对齐 +
双栏纠错 + 断点续跑。
"""

__version__ = "1.0.0"


def parse_pages(spec):
    """把"1-50"、"3"、"1-10,20-30" 这类页码范围解析成页号集合。

    返回 set；空字符串返回空集合（=整本）；格式不对返回 None。
    兼容中文逗号、顿号、全角波浪号。
    """
    import re
    s = (spec or u'').strip()
    if not s:
        return set()
    s = (s.replace(u'，', u',').replace(u'、', u',').replace(u'；', u',')
          .replace(u'－', u'-').replace(u'—', u'-').replace(u'～', u'-')
          .replace(u'~', u'-'))
    out = set()
    for part in re.split(u'[,，\\s]+', s):
        if not part:
            continue
        m = re.match(u'^(\\d+)\\s*-\\s*(\\d+)$', part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            out.update(range(a, b + 1))
        elif part.isdigit():
            out.add(int(part))
        else:
            return None
    return out
