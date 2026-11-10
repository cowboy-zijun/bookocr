# -*- coding: utf-8 -*-
"""导出 Word。

版式对照原书：32 开页面、每页起新页（Word 第 N 页 = 原书第 N 页）、
黑体分级标题、宋体正文、楷体小字注文/目录/索引、可疑字标红、
页首灰色标注原书页码。
"""
import collections
import json
import math
import os
import re
import time

from docx import Document
from docx.enum.section import WD_SECTION, WD_ORIENT
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

CJK = re.compile(u'[㐀-鿿]')
H1 = re.compile(u'^第[一二三四五六七八九十百零]+[卷部编章篇]')
H2 = re.compile(u'^[一二三四五六七八九十]+、')
H3 = re.compile(u'^[Ａ-ＺA-Z][．.、]')
H4 = re.compile(u'^([ａ-ｚa-z]|[0-9]+)[．.、]')
NAMED = (u'序言', u'莱比锡宗教会议', u'圣布鲁诺', u'圣麦克斯', u'真正的社会主义',
         u'德意志意识形态', u'费尔巴哈', u'结束语', u'附录')

CONT = re.compile(u'^[，。、；：！？…—－]')   # 以标点开头的段是上一页/上一段的延续
# 句末引用角标：正文段落末尾的 [1]、〔1〕。注意是"句末"，
# 行首的 [1] 才是脚注。
CITE_END = re.compile(u'(?:\\[\\d{1,3}\\]|〔\\d{1,3}〕)\\s*$')
# 行首注号：脚注/页下注（[1] 〔1〕 ① ⑴）
NOTE_HEAD = re.compile(u'^\\s*(?:\\[\\d{1,3}\\]|〔\\d{1,3}〕|[①-⑳]|[⑴-⒇])')
# 表题（表1 / 附表2 / Table 1）。表题在原书里通常是单倍行距，
# 不能跟着正文的双倍行距排，否则表下面的正文会被挤到下一页。
CAPTION_BLOCK = re.compile(
    u'^(?:表|附表|续表|Table|TABLE)\\s*[0-9一二三四五六七八九十]')
BAD = re.compile(u'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]')  # XML 非法字符

CHAR_SCALE = 100     # 字符横向缩放（%），按原书实测字宽设置；100=不缩放
# 行级版面（frametree/linepage）的行距系数：1.0 = 完全按原书行距。
# 以前固定 0.90，虽然更容易塞进一页，但正文看起来明显偏挤。
TREE_LINE_SCALE = float(os.environ.get('BOOKOCR_TREE_LINE_SCALE', '0.95'))
RED = RGBColor(0xC0, 0x00, 0x00)
GRAY = RGBColor(0x90, 0x90, 0x90)
DARK = RGBColor(0x40, 0x40, 0x40)
NOTE_BLUE = RGBColor(0x2a, 0x4a, 0x6a)

ALIGN = {'c': WD_ALIGN_PARAGRAPH.CENTER, 'j': WD_ALIGN_PARAGRAPH.JUSTIFY,
         'l': WD_ALIGN_PARAGRAPH.LEFT, 'r': WD_ALIGN_PARAGRAPH.RIGHT}


def calibrate(blocks):
    """按本书每页字数分布自动定字号：让绝大多数页正好装满一个 Word 页。

    取每页字数的 p98 作为版心容量（忽略近乎空白的页），反推正文字号。
    页密的书用小字（如《德意志意识形态》约 11pt），页疏的书用大字
    （如毛选五卷约 15-16pt），行距取 1.6 倍。
    """
    import math
    cur = 0
    cnt = {}
    for b in blocks:
        if b[0] == 'page':
            cur = int(b[1])
            continue
        if b[0] in ('header', 'image', 'tablepage', 'tablegrid'):
            continue            # 页眉/插图/截图/表格不算版心文字
        if cur:
            cnt[cur] = cnt.get(cur, 0) + len(b[1])
    vals = sorted(v for v in cnt.values() if v >= 80)
    if not vals:
        return 11.0, 17.6
    cap = vals[min(len(vals) - 1, int(len(vals) * 0.98))]
    # 上限从 900 放宽到 1400：密排书（每页 1000 字上下）原来被硬压到 900，
    # 反推的字号必然偏大，几乎每页都会溢到下一页。
    cap = max(320, min(cap, 1400))
    size = math.sqrt(329.0 * 516.0 / (1.6 * cap))   # 版心 329x516pt
    size = max(10.0, min(16.0, size))
    size = round(size * 2) / 2
    return size, round(size * 1.6 * 2) / 2


def source_body_size(blocks):
    """原书的正文字号（按字符数加权中位数）。

    注意不能用 calibrate 算出的 Word 字号来比：那是按版心容量反推的
    排版字号，和 PDF 里写的磅值不是一回事。判标题层级必须拿原书字号比。
    """
    pairs = [(b[6], max(1, len(b[1] or ''))) for b in blocks
             if len(b) > 6 and b[6] and b[0] not in
             ('page', 'image', 'tablepage', 'tablegrid')]
    if not pairs:
        return 0.0
    pairs.sort()
    total = sum(w for _, w in pairs)
    acc = 0
    for s, w in pairs:
        acc += w
        if acc >= total / 2.0:
            return s
    return pairs[-1][0]


def source_note_size(blocks, body_size):
    """原书注文的实际字号（按字符加权中位数）。"""
    if not body_size:
        return 0.0
    vals = []
    for b in blocks:
        if b[0] in ('page', 'header', 'image', 'tablepage', 'tablegrid'):
            continue
        sz = b[6] if len(b) > 6 else 0
        if sz and sz < body_size * 0.85:
            vals.append((sz, max(1, len(b[1] or ''))))
    if not vals:
        return 0.0
    vals.sort()
    total = sum(w for _, w in vals)
    acc = 0
    for s, w in vals:
        acc += w
        if acc >= total / 2.0:
            return s
    return vals[-1][0]


def source_toc_size(blocks, body_size):
    """目录条目的实际字号（按字符加权中位数）。"""
    vals = [(b[6], max(1, len(b[1] or ''))) for b in blocks
            if b[0] == 'toc' and len(b) > 6 and b[6]]
    if not vals:
        return max(7.0, round(body_size * 0.86 * 2) / 2.0)
    vals.sort()
    total = sum(w for _, w in vals)
    acc = 0
    for s, w in vals:
        acc += w
        if acc >= total / 2.0:
            return round(s * 2) / 2.0
    return round(vals[-1][0] * 2) / 2.0


def heading_tiers(blocks, body_size, min_ratio=1.08):
    """全书标题字号分档。

    不同书的正文字号、标题字号都不同。用固定倍数（1.30/1.15）判级，
    遇到"文章标题 18pt、小节标题 14pt、正文 10.5pt"这种书，14pt 也超过
    1.30 倍，两级标题就都被判成一级（这是"大的字小、小的字大"的根因）。
    按全书实际用过的字号分档：最大的当一级标题，次大的当二级——最通用。
    """
    cnt = collections.Counter()
    page_count = 0
    for b in blocks:
        if b[0] == 'page':
            page_count += 1
            continue
        if b[0] in ('header', 'image', 'tablepage', 'tablegrid', 'table',
                    'note', 'toc', 'index'):
            continue
        sz = b[6] if len(b) > 6 else 0
        t = (b[1] or '').strip()
        # 文章标题可能超过 24 字；按字号分档时放宽到 60 字，
        # 但仍排除长正文行（正文行一般也不大于正文 1.08 倍）
        if not sz or not t or len(t) > 60:
            continue
        if t.endswith((u'。', u'！', u'？')):
            continue
        cnt[round(sz * 2) / 2.0] += 1
    # 期刊/单篇文章只有几页，标题字号可能只出现一次；短文档不能
    # 还要求"出现 3 次"，否则唯一的文章标题会进不了标题档。
    need = 1 if page_count <= 8 else max(3, page_count // 50)
    tiers = []
    for s in sorted(cnt):
        if cnt[s] < need or not body_size or s < body_size * min_ratio:
            continue
        if tiers and s - tiers[-1] <= max(0.6, s * 0.04):
            continue          # 12.0 与 12.1 是同一档，别拆成两级
        tiers.append(s)
    return sorted(tiers, reverse=True)


def tier_index(size, tiers):
    """字号属于第几档（0=一级标题）。不在任何档里返回 None。"""
    if not size or not tiers:
        return None
    for i, s in enumerate(tiers):
        if abs(size - s) <= max(0.6, s * 0.04):
            return i
    return None


def _note_marker_set(blocks):
    """找出本书的脚注标记字母（有的版本用 A、B、C 标脚注）。"""
    ms = set()
    for b in blocks:
        if b[0] != 'note':
            continue
        m = re.match(u'^([A-Z])(?:[\\s　]|$)', (b[1] or '').strip())
        if m:
            ms.add(m.group(1))
    return ms


def _strip_note_markers(text, mask, markers):
    """把标题里夹带的脚注标记字母去掉（"矛盾论 B"→"矛盾论"）。

    只删紧挨汉字、后面是行尾或标点的单个字母，英文词不受影响。
    """
    if not markers or not text or len(text) > 60:
        return text, mask
    drop = set()
    for i, ch in enumerate(text):
        if ch not in markers:
            continue
        # 标记前可能隔着一个空格（"矛　盾　论 B"），要跨过空格看前一个字
        j = i - 1
        while j >= 0 and text[j] in u'　 ':
            j -= 1
        prev = text[j] if j >= 0 else ''
        nxt = text[i + 1] if i + 1 < len(text) else ''
        if prev and (u'一' <= prev <= u'鿿'
                     or prev in u'）】”？。！，；：、') \
                and (not nxt or nxt in u'？。！，；：、）】”'):
            drop.add(i)
            drop.update(range(j + 1, i))     # 标记前的空格一起去掉
    if not drop:
        return text, mask
    t2 = ''.join(ch for i, ch in enumerate(text) if i not in drop)
    m2 = [mask[i] if mask and i < len(mask) else False
          for i in range(len(text)) if i not in drop]
    return t2, m2


# PDF 文字层的坏字符：字体缺少 Unicode 映射时，数字会变成这些符号。
# 原样留着会误导引用，统一标红，让用户知道要对照原书。
SUSPECT_CHARS = set(u'£±´¸¹¤¦¨§')


def _mark_broken(text, mask):
    if not text:
        return text, mask
    bad = [i for i, c in enumerate(text) if c in SUSPECT_CHARS]
    if not bad:
        return text, mask
    m = list(mask) if mask else [False] * len(text)
    while len(m) < len(text):
        m.append(False)
    for i in bad:
        m[i] = True
    return text, m


def make_style(size, tiers=None, small=None, toc_sz=None):
    f = size / 11.0

    def sc(x):
        return max(8, round(x * f * 2) / 2)

    # 注文字号用原书实测值（脚注普遍是正文的 0.76—0.8 倍）。
    # 但扫描件的字号是估的，有时注文和正文估得一样大（12.5/12.5），
    # 这时必须强制压到正文的 88% 以下，否则正文和注文在版面上分不开。
    if small:
        note_sz = max(6.5, min(round(small * 2) / 2.0,
                               round(size * 0.88 * 2) / 2.0))
    else:
        note_sz = sc(10.5)
    toc_size = max(6.5, round(toc_sz * 2) / 2.0) if toc_sz else sc(10.5)

    # 标题字号直接用原书量出来的字号（tiers 从大到小），
    # 不再用"正文 +6/+4/+2"的固定加法：加法会把 18pt 的文章标题
    # 压成 16pt、把小节标题抬得和文章标题一样大，层级就失真了。
    tiers = list(tiers or [])

    def hsize(i, fallback):
        if len(tiers) > i:
            return max(8, round(tiers[i] * 2) / 2.0)
        return fallback

    # 行距一律用"磅值"而不是倍数：倍数是按字体的行高算的（宋体约 1.3 倍），
    # 估算高度时对不上，Word 实际排出来会比估算高一两成，页页溢出。
    def lspt(mult, sz):
        return round(sz * mult, 1)

    return {
        'h1': dict(ea=u'黑体', size=hsize(0, max(13, round(size + 6))), align='c',
                   before=26, after=14, ls=lspt(1.4, hsize(0, size + 6))),
        'h2': dict(ea=u'黑体', size=hsize(1, max(12, round(size + 4))), align='c',
                   before=20, after=10, ls=lspt(1.4, hsize(1, size + 4))),
        'h3': dict(ea=u'黑体', size=hsize(2, max(11, round(size + 2))), align='c',
                   before=14, after=7, ls=lspt(1.35, hsize(2, size + 2))),
        'h4': dict(ea=u'黑体', size=hsize(3, max(10, round(size + 1))), align='l',
                   before=10, after=5, ls=lspt(1.3, hsize(3, size + 1))),
        'body': dict(ea=u'宋体', size=size, align='j', before=0, after=0,
                     ind=round(size * 2), ls=lspt(1.5, size)),
        'header': dict(ea=u'宋体', size=8.5, align='c', before=0, after=0,
                       ls=lspt(1.1, 8.5)),
        'table': dict(ea=u'宋体', size=9.5, align='l', before=0, after=0,
                      ls=lspt(1.15, 9.5)),
        'note': dict(ea=u'楷体', size=note_sz, align='j', before=0, after=0,
                     left=12, ls=lspt(1.45, note_sz)),
        # 原书右对齐的引文出处/署名（“《我的奋斗》第385页”）。单独成段、
        # 右对齐，不能并进下一段正文。
        'cite': dict(ea=u'宋体', size=size, align='r', before=0, after=0,
                     ls=lspt(1.5, size)),
        'toc': dict(ea=u'楷体', size=toc_size, align='l', before=0, after=0,
                    left=8, ls=lspt(1.6, toc_size)),
        'tochead': dict(ea=u'黑体', size=round(toc_size + 1.0),
                        align='l', before=3, after=1, left=0,
                        ls=lspt(1.5, toc_size + 1.0)),
        'index': dict(ea=u'楷体', size=sc(10), align='l', before=0, after=0,
                      left=0, ls=lspt(1.3, sc(10))),
    }


def classify(kind, text, hlevel, verified, page, appendix_from=0,
             size=None, body_size=None, region=None, tier=None,
             region_known=False):
    if kind == 'page':
        return 'page'
    if kind == 'header':
        return 'header'          # 原书页眉：灰色小字，与正文区分
    if kind == 'image':
        return 'image'           # 原书插图
    if kind == 'tablepage':
        return 'tablepage'       # 数据页整页截图
    if kind == 'table':
        return 'table'           # 表格数据行：小字、无缩进
    if kind == 'tablegrid':
        return 'tablegrid'       # 通用表格检测出的真表格
    if kind == 'note':
        return 'note'            # 脚注/参考文献（小字号行）
    if kind == 'cite':
        return 'cite'            # 原书右对齐的引文出处/署名
    t = text.strip()
    n = len(t)
    # 引用角标：段末的 [1] 是正文的一部分，行首的 [1] 才是脚注。
    # 扫描件里带角标的行，OCR 框往往偏高，字号被估大，
    # 会被后面的字号规则误判成标题——这就是"正文突然变大字"的来源。
    if kind != 'toc' and CITE_END.search(t):
        return 'body'
    if kind not in ('toc', 'header') and NOTE_HEAD.match(t) and n >= 4:
        return 'note'
    # 有字号就用“相对正文的倍数”判层级（扫描件由 OCR 框估算，
    # 文字层是精确值）；没有字号再退回按文字结构猜。
    ratio = (size / body_size) if (size and body_size) else 0.0
    # 标题必须"站得住位置"：版面模型说有标题区，或原文里是居中短行。
    # 从版心左边起排的正文行，字号再大也不当标题——这是框的约束。
    if region_known:
        geo_head = (region == 'title')
    else:
        geo_head = (kind == 'head')
    # 期刊文章大标题常常通栏，不满足"左右留白都超过两个字"的居中判定，
    # 但字号是正文的两倍；这种宽大标题也要按标题排。
    large_title = (ratio >= 1.45 and n <= 60
                   and not t.endswith((u'。', u'！', u'？')))
    # 版面模型给出的区域是最可靠的信号：标题区按标题排，表格区按小字排
    if region == 'title' and kind != 'toc':
        return 'h1' if (ratio >= 1.30 or n <= 18) else 'h2'
    if region == 'table' and kind not in ('toc', 'note'):
        return 'table'
    if kind == 'toc':
        # 目录内部也有层级：章名比条目大一档，“目录”二字再大一档。
        # 不靠字号（扫描件没有字号），靠中文书通用的“第X章/第X节”结构。
        if re.match(u'^目\\s*录$', t):
            return 'h1'
        if re.match(u'^第[一二三四五六七八九十百零]+[章篇编]', t):
            return 'tochead'
        if region == 'title' or ratio >= 1.12:
            return 'tochead'      # 目录里字号明显大的行 / 标题区 = 章一级
        if re.match(u'^第[一二三四五六七八九十百零]+节', t):
            return 'toc'          # 目录里的"第X节"是条目，不是正文标题
        return 'toc'
    if kind == 'index':
        return 'index'
    if page <= 3 and n <= 14:
        # 封面短文字（书名/作者）按小字；但作者行常比正文略大，
        # 不能把它压成索引小字，交给后面的标题/正文字号规则。
        if not (size and body_size and size > body_size * 1.05):
            return 'index'
    if appendix_from and page >= appendix_from:
        return 'index'
    # 整行被引号包住的是人物对话 / 引文，绝不可能是标题。
    # 小说里对话短行多，以前会被字号分档规则当成标题，字号突然变大。
    if t[:1] in u'“‘"「『' or t[-1:] in u'”’"」』':
        if size and body_size and size < body_size * 0.85:
            return 'note'
        return 'body'
    # 明显比正文小的行按注文排（脚注/参考文献）。有的 PDF 没有脚注分隔线，
    # 版面重建认不出注文，这些行会被当正文，按 1.5 倍行距排——一页注文
    # 就会被撑高三成，整本书大面积溢出。
    if size and body_size and size < body_size * 0.85 and n > 4:
        return 'note'
    # 全文字号分档：最大的那档=一级标题，次大=二级（每本书按自己的字号来）。
    # 有版面模型时，区域必须也是"标题区"才认：扫描件的字号是框高估的，
    # 小说里一行对话/歌词偶尔会偏高，光看字号会把它们排成大标题。
    _maxn = 60 if large_title else 24
    if tier is not None and n <= _maxn and (geo_head or large_title) \
            and not t.endswith((u'。', u'！', u'？')):
        return ('h1', 'h2', 'h3', 'h4')[min(tier, 3)]
    # 按原文相对字号定标题级别（最通用的一条）。
    # 只在短行上生效：OCR 框高有 ±20% 噪声，长正文行偶尔会偏高，
    # 若不加长度限制，整段正文会被误判成标题。
    if ratio and n <= _maxn and (geo_head or large_title) \
            and not t.endswith((u'。', u'！', u'？')):
        if ratio >= 1.30:
            return 'h1'
        if ratio >= 1.15:
            return 'h2'
        if ratio >= 1.07:
            return 'h3'
    if n and n <= 50 and not t.endswith(u'。'):
        if hlevel == 3:
            return 'h3'
        if hlevel >= 4:
            return 'h4'
        if H1.match(t):
            return 'h1'
        if t in NAMED or (H2.match(t) and n <= 30):
            return 'h2'
        if H3.match(t):
            return 'h3'
        if H4.match(t) and n <= 30:
            return 'h4'
        if (kind == 'head' and n <= 24 and u'注' not in t and u'——' not in t
                and not re.search(u'[。，；：]', t) and len(CJK.findall(t)) >= 2):
            # kind=head 是 layout 已经按"居中 + 短行 + 无句末标点"判出的
            # 结构性标题；即使版面模型给的是 text 区，也不该降回正文
            # （"断裂"这种两字小标题就会被吞掉）。
            return 'h3'
    if not verified and n <= 400 and (u'编者注' in t or u'译者注' in t):
        return 'note'
    return 'body'


def _set_page_frame(p, x_pt, y_pt, w_pt, h_pt=None, h_rule='auto'):
    """把这一段钉在页面的指定位置（页首页码/页眉/页底注文用）。

    用 Word 的"图文框"（framePr，锚定到页面），这一段就不占正文高度，
    正文从真正的上边距开始——版心和原书量出来的框完全重合，
    标尺读出来的上边距就是原书的 3.66cm，而不是被页码行挤掉的 3.2cm。
    """
    pPr = p._p.get_or_add_pPr()
    fr = OxmlElement('w:framePr')
    fr.set(qn('w:w'), str(int(round(w_pt * 20))))
    fr.set(qn('w:h'), str(int(round((h_pt if h_pt else 20.0) * 20))))
    fr.set(qn('w:hRule'), h_rule)
    fr.set(qn('w:wrap'), 'none')
    fr.set(qn('w:vAnchor'), 'page')
    fr.set(qn('w:hAnchor'), 'margin')
    fr.set(qn('w:x'), str(int(round(x_pt * 20))))
    fr.set(qn('w:y'), str(int(round(y_pt * 20))))
    pPr.insert(0, fr)


def _set_font(run, size=12, color=None, bold=False, ea=u'宋体'):
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.name = 'Times New Roman'
    run._element.rPr.rFonts.set(qn('w:eastAsia'), ea)
    if color is not None:
        run.font.color.rgb = color
    if CHAR_SCALE != 100:
        # 字符缩放：字高不变（字号仍是原书的磅值），只把字压扁一点，
        # 这样每行能排下和原书一样多的字，换行位置才会一致。
        rpr = run._element.get_or_add_rPr()
        for e in rpr.findall(qn('w:w')):
            rpr.remove(e)
        el = OxmlElement('w:w')
        el.set(qn('w:val'), str(int(CHAR_SCALE)))
        sz = rpr.find(qn('w:sz'))
        if sz is not None:
            sz.addprevious(el)
        else:
            rpr.append(el)


def _styles_for_block(text, style_maps=None, page=None, cursors=None):
    """按页内游标把块文本映射回逐字样式；找不到返回 None。"""
    if not style_maps or page is None or page not in style_maps:
        return None
    norm = u''.join(ch for ch in (text or '') if not ch.isspace())
    if not norm:
        return None
    s_text, s_styles = style_maps[page]
    cur = (cursors or {}).get(page, 0)
    pos = s_text.find(norm, cur)
    if pos < 0:
        pos = s_text.find(norm)
    if pos < 0:
        return None
    out, j, prev = [], pos, None
    for ch in text:
        if ch.isspace():
            if prev is None and j < len(s_styles):
                prev = s_styles[j]
            out.append(prev)
            continue
        if j >= len(s_styles):
            return None
        out.append(s_styles[j])
        prev = s_styles[j]
        j += 1
    if cursors is not None:
        cursors[page] = j
    return out


_RUNIN_HEAD = re.compile(
    u'^(\\d+(?:\\.\\d+)+\\s+\\S{2,24}?\\s+)(\\S.*)$', re.S)


def _emit_body_maybe_runin(p, text, mask, ea, size, color, styles=None,
                           style_scale=1.0):
    """行内小标题（"2.3.3 参数优化与模型训练 由于…"）：只把前半截加粗，
    不另起段落，行数/分页不变。"""
    m = _RUNIN_HEAD.match(text)
    if m and len(text) >= 40 and len(m.group(1)) <= 40:
        cut = len(m.group(1))
        head = m.group(1)
        _emit_mask(p, head, mask[:cut], ea, size, color, True,
                   styles[:cut] if styles else None, style_scale)
        _emit_body_with_notes(p, text[cut:], mask[cut:], ea, size, color,
                              styles[cut:] if styles else None, style_scale)
        return
    _emit_body_with_notes(p, text, mask, ea, size, color, styles, style_scale)


def _emit_mask(p, text, mask, ea, size, color, bold, styles=None,
               style_scale=1.0):
    """按可疑字掩码 + 逐字样式发 run。

    styles 是 layout.line_styles 的逐字元组（size,font,sup,sub,bold,
    italic）；没有就保持原来的整段一个字号。有 styles 时按样式变化切
    run，上标/下标用 Word 的 vertAlign 还原。
    """
    if not styles or len(styles) != len(text):
        buf, bufsus = '', None
        for i, c in enumerate(text):
            sus = mask[i] if i < len(mask) else False
            if sus != bufsus:
                if buf:
                    _set_font(p.add_run(buf), size=size,
                              color=RED if bufsus else color,
                              bold=bold, ea=ea)
                buf, bufsus = '', sus
            buf += c
        if buf:
            _set_font(p.add_run(buf), size=size,
                      color=RED if bufsus else color, bold=bold, ea=ea)
        return

    def _emit_run(buf, sus, sty):
        if not buf:
            return
        sz, font, sup, sub, sb, si = sty
        use_sz = (sz * style_scale) if sz else size
        if not use_sz or use_sz < 4:
            use_sz = size
        # 上下标本身在 PDF 里就是小字号；不要再按正文字号放大
        _ea = font if font in (u'宋体', u'黑体', u'楷体', u'仿宋') else ea
        run = p.add_run(buf)
        _set_font(run, size=use_sz, color=RED if sus else color,
                  bold=(bold or sb), ea=_ea)
        if font and font not in (u'宋体', u'黑体', u'楷体', u'仿宋'):
            run.font.name = font
        if sup:
            run.font.superscript = True
        if sub:
            run.font.subscript = True
        if si:
            run.font.italic = True

    buf, key = '', None
    for i, c in enumerate(text):
        sus = mask[i] if i < len(mask) else False
        sty = styles[i]
        k = (sus, sty)
        if k != key:
            if buf:
                _emit_run(buf, key[0], key[1])
            buf, key = '', k
        buf += c
    if buf:
        _emit_run(buf, key[0], key[1])


def _emit_body_with_notes(p, text, mask, ea, size, color, styles=None,
                          style_scale=1.0):
    """权威文本的脚注内联在正文里（［注：…］），用小号楷体呈现。"""
    i = 0
    while True:
        j = text.find(u'［注', i)
        if j < 0:
            _emit_mask(p, text[i:], mask[i:], ea, size, color, False,
                       styles[i:] if styles else None, style_scale)
            break
        k = text.find(u'］', j)
        if k < 0:
            _emit_mask(p, text[i:], mask[i:], ea, size, color, False,
                       styles[i:] if styles else None, style_scale)
            break
        if j > i:
            _emit_mask(p, text[i:j], mask[i:j], ea, size, color, False,
                       styles[i:j] if styles else None, style_scale)
        _set_font(p.add_run(text[j:k + 1]), size=size - 2.5,
                  color=NOTE_BLUE, ea=u'楷体')
        i = k + 1


def _img_height_pt(path, width_cm):
    """图片按宽度排版后的实际高度（pt）。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return width_cm * 28.35 * (im.height / max(1, im.width))
    except Exception:
        return width_cm * 28.35 * 1.4


def _text_width(text, size):
    """按字宽估算一行文字占的宽度：汉字全角，西文/数字约半角。"""
    w = 0.0
    for ch in text:
        w += size if ord(ch) >= 0x2E80 else size * 0.55
    return w


def _fit_line_width(run, text, fs, avail):
    """只给中文/中英混排行做轻微负字距，不压英文正文。

    旧逻辑对所有行按 0.52×字号估算西文宽度，这个系数对 Times New Roman
    偏高；英文长行会被算出“超宽”，再被强行压 -1~-2pt/字，看起来字挤
    成一团。英文行交给 Word 自然排版；中文行才需要负字距来保持原书断行。
    """
    text = text or ''
    cjk = sum(1 for ch in text if ord(ch) >= 0x2E80)
    if cjk < max(4, len(text) * 0.25):
        return
    nat = 0.0
    for ch in text:
        nat += fs * (0.52 if ord(ch) < 0x2E80 else 1.0)
    target = avail * 0.97
    if nat <= target:
        return
    sp = max(-2.5, (target - nat) / max(1, len(text)))
    rPr = run._element.get_or_add_rPr()
    sp_el = OxmlElement('w:spacing')
    sp_el.set(qn('w:val'), str(int(round(sp * 20))))
    sz = rPr.find(qn('w:sz'))
    if sz is not None:
        sz.addprevious(sp_el)
    else:
        rPr.append(sp_el)


def _emit_bottom_notes(doc, group, k, lk, geom, text_w, note_style):
    """把一页的注文钉在页底，而不是紧跟在正文最后一行后面。

    原书的页下注是从版心底部往上排的；如果只按普通段落接在正文后面，
    正文短的时候注文会停在半页高，下面空一大块。这里用 Word 的图文框
    （framePr，锚定页面）把整组注文放到原书注文区的起始高度；前面再放
    一个等高的空白占位段，让正文分页仍按“正文 + 注文区”的总高度计算。
    """
    size = max(7.0, note_style['size'] * k)
    lsp = max(8.0, note_style['ls'] * lk)
    left = note_style.get('left', 0.0) * k
    width = max(40.0, text_w - left)
    lines = 0
    for b in group:
        w = _text_width(b[1], size) * CHAR_SCALE / 100.0
        lines += max(1, int(math.ceil(w / width)))
    h = lines * lsp + 4.0

    page_h = (geom or {}).get('page_h', 842.0)
    bottom_m = (geom or {}).get('bottom', 0.0)
    # 导出块是 8 元组 [kind,text,mask,verified,hlevel,y0,size,bbox]；
    # 兼容外部调用传进来的 5 元组（y0 在第 4 项）。
    _yi = 5 if group and len(group[0]) > 5 else 3
    _y0 = group[0][_yi] if group and len(group[0]) > _yi else None
    if geom and _y0 is not None:
        y = float(_y0) / 5000.0 * page_h
    else:
        y = page_h - bottom_m - h
    # 注文区不能高过版心底：原书 y 可能是 OCR 框，偶尔会落到正文中间；
    # 夹到版心底端，至少不会盖住正文。
    y = max(0.0, min(y, page_h - bottom_m - h - 2.0))
    y = max(0.0, min(y, page_h - h - 6.0))

    # 关键：注文框不参与正文流，必须在正文末尾补一个“等高占位段”，
    # 否则正文会一直排到页底，和 frame 里的注文重叠（示例书第 1 页、
    # 第 35 页都复现过）。占位段的精确行高 = 注文高度。
    spacer = doc.add_paragraph()
    spf = spacer.paragraph_format
    spf.space_before = Pt(0)
    spf.space_after = Pt(0)
    spf.line_spacing = Pt(max(1.0, h))
    spacer.add_run('').font.size = Pt(1)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    pf = p.paragraph_format
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)
    pf.line_spacing = Pt(lsp)
    _set_page_frame(p, left, y, width, h_pt=h, h_rule='atLeast')
    for gi, b in enumerate(group):
        if gi:
            p.add_run().add_break()
        _emit_mask(p, b[1], b[2] or [], u'楷体', size, None, False)


def _est_height(blist, page_no, body_size, body_ls, STYLE, appendix_from,
                text_w=329.0, src_body=None, tiers=None, geom=None):
    """估算一页内容在 Word 里占的高度（pt），用于压缩超页的页。

    text_w 是版心宽度：不同书、不同开本差别很大，估算必须用真实宽度，
    否则会误判"超页"，把本来排得下的页缩字号。
    """
    h = 16.0      # 页首“原书第 N 页”标注
    for b in blist:
        kind, text = b[0], b[1]
        verified = b[3] if len(b) > 3 else None
        hlevel = b[4] if len(b) > 4 else 0
        bsize = b[6] if len(b) > 6 else 0
        role = classify(kind, text, hlevel, verified, page_no, appendix_from,
                        size=bsize, body_size=src_body or body_size,
                        tier=tier_index(bsize, tiers))
        if role in ('image', 'tablepage'):
            ibox = b[7] if len(b) > 7 else None
            if geom and ibox:
                hpt = (ibox[3] - ibox[1]) / 5000.0 * geom['page_h']
                h += min(hpt, geom['page_h'] * 0.9) + 4
            else:
                h += _img_height_pt(text, 11.4 if role == 'tablepage' else 10.8) + 12
            continue
        if role == 'tablegrid':
            try:
                rows = json.loads(text)
            except Exception:
                rows = []
            ncols = max(len(r) for r in rows) if rows else 1
            cell_w = max(12.0, (text_w - 6) / max(1, ncols))
            th = 4.0
            for r in rows:
                lines = 1
                for c in r:
                    lines = max(lines, math.ceil(len(c or '') / max(2.0, cell_w / 9.0)))
                th += lines * 10.5 + 2
            h += th
            continue
        cf = STYLE[role]
        s = cf['size']
        if role == 'header':
            h += 0.0                 # 页眉并进页首标注行，不额外占高
            continue
        sc = CHAR_SCALE / 100.0      # 字符缩放后行宽按同比例缩
        if role == 'body':
            w = _text_width(text, s) * sc + (cf['ind'] if cf.get('ind') else 0)
            h += max(1, math.ceil(w / text_w)) * body_ls + cf['before'] + cf['after']
        elif role in ('h1', 'h2', 'h3', 'h4'):
            h += cf['ls'] + cf['before'] + cf['after']
        else:
            width = text_w - cf.get('left', 0)
            h += max(1, math.ceil(_text_width(text, s) * sc / width)) * cf['ls'] \
                + cf['before'] + cf['after']
    return h


def _set_section_columns(section, num, space_pt=22.0):
    """给 Word 节设置分栏数（双栏期刊用）。"""
    sectPr = section._sectPr
    cols = sectPr.find(qn('w:cols'))
    if cols is None:
        cols = OxmlElement('w:cols')
        sectPr.append(cols)
    cols.set(qn('w:num'), str(int(num)))
    cols.set(qn('w:space'), str(int(space_pt * 20)))


def _set_section_orientation(sec, geom, landscape):
    """按原书页面方向设置 Word 节；横排页整页截图时用横排节。"""
    if not geom:
        return
    if landscape:
        sec.orientation = WD_ORIENT.LANDSCAPE
        sec.page_width = Pt(geom['page_h'])
        sec.page_height = Pt(geom['page_w'])
        sec.left_margin = Pt(0)
        sec.right_margin = Pt(0)
        sec.top_margin = Pt(10)
        sec.bottom_margin = Pt(0)
    else:
        sec.orientation = WD_ORIENT.PORTRAIT
        sec.page_width = Pt(geom['page_w'])
        sec.page_height = Pt(geom['page_h'])
        sec.left_margin = Pt(geom['left'])
        sec.right_margin = Pt(geom['right'])
        sec.top_margin = Pt(geom['top'])
        sec.bottom_margin = Pt(geom['bottom'])


def _emit_locked_page(doc, lines, cur_page, geom, body_size, body_ls, k,
                      style_maps, style_cursors, style_scale, text_w):
    """L3 锁行：按原书段落分组，段内用软换行保留视觉断行。"""
    groups = []
    for line in lines:
        text = line.get('text') or ''
        if not text.strip():
            continue
        bid = line.get('bid')
        if not groups or bid is None or groups[-1][0] != bid:
            groups.append([bid, []])
        groups[-1][1].append(line)
    for _bid, glines in groups:
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.space_before = Pt(0)
        pf.space_after = Pt(0)
        pf.first_line_indent = Pt(0)
        # 软换行按整段计行距；留 5% 余量，避免最后一个软换行把整页
        # 顶到下一页（逐行成段时没有这个整段行距累积）。
        pf.line_spacing = Pt(max(8.0, body_ls * k * 0.95))
        first = glines[0]
        if geom:
            pw = geom['page_w']
            _x0s = [(_l.get('x0') or 0.0) for _l in glines]
            _minx = min(_x0s) if _x0s else 0.0
            left = _minx / 3400.0 * pw - geom.get('left', 0.0)
            if left > 0:
                pf.left_indent = Pt(left)
            _fi = ((first.get('x0') or 0.0) - _minx) / 3400.0 * pw
            if _fi > 0.5:
                pf.first_line_indent = Pt(_fi)
            cx = ((first.get('x0') or 0.0) + (first.get('x1') or 0.0)) / 2.0
            if len(glines) == 1 and len(first.get('text') or '') <= 30 \
                    and abs(cx - 1700.0) < 260.0:
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            else:
                p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        else:
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        for j, line in enumerate(glines):
            text = line.get('text') or ''
            mask = line.get('mask') or [False] * len(text)
            sty = _styles_for_block(text, style_maps, cur_page, style_cursors)
            _emit_mask(p, text, mask, u'宋体', body_size * k, None, False,
                       sty, style_scale)
            if j < len(glines) - 1:
                p.add_run().add_break()


def export_docx(blocks, out_path, title=None, notes=None, appendix_from=0,
                page_scale=None, page_numbers=None, regions=None, geom=None,
                lead_scale=None, columns=1, style_maps=None,
                landscape_pages=None, locked_pages=None, page_sections=None,
                page_geometries=None):
    """blocks: [kind, text, mask, verified, hlevel]（含 page 标记）。

    appendix_from: 附录/注释/索引区起始页（自动识别），此后的正文块
    按索引小字排版——避免出现"同一篇文章前后字体不同"。

    geom: 原书实测版式（layout.page_geometry 的返回值）。给了就按原书
    纸张、页边距、字号、行距 1:1 排——Word 标尺读出来和原书一样。
    """
    # XML 不允许控制字符（NULL、\x0b、\x0c 等）：OCR 偶尔会产出，
    # 写入 Word 会整个崩溃。导出前统一清洗（续段标记  合法，保留）。
    blocks = [list(b) for b in blocks]
    for b in blocks:
        b[1] = BAD.sub('', b[1])
    if title:
        title = BAD.sub('', title)
    if notes:
        notes = [BAD.sub('', t) for t in notes]
    # 标题里夹带的脚注标记字母（"矛盾论 B"）先清掉，再判层级
    markers = _note_marker_set(blocks)
    if markers:
        for b in blocks:
            if b[0] == 'head':
                b[1], b[2] = _strip_note_markers(b[1], b[2], markers)
    # 文字层坏字符（£± 之类）标红：宁可显眼，也不能让用户当成原文引用
    for b in blocks:
        if b[0] not in ('page', 'image', 'tablepage', 'tablegrid'):
            b[1], b[2] = _mark_broken(b[1], b[2])
    body_size, body_ls = calibrate(blocks)
    src_body = source_body_size(blocks)
    tiers = heading_tiers(
        blocks, src_body,
        min_ratio=1.08 if (geom or {}).get('exact_sizes', True) else 1.28)
    if geom and geom.get('body_size'):
        # 原书实测值优先：正文字号、行距都按原书来
        body_size = round(geom['body_size'] * 2) / 2.0
        body_ls = round(geom.get('pitch') or body_size * 1.5, 1)
        if not tiers:
            tiers = [geom['body_size']]
    global CHAR_SCALE
    CHAR_SCALE = int(round((geom or {}).get('scale', 1.0) * 100))
    note_sz = source_note_size(blocks, src_body)
    toc_sz = source_toc_size(blocks, src_body)
    # 文字层 span 的字号是 PDF 原值；geom 把超大扫描页缩到 A4 时，
    # 样式字号也要按同一比例缩，否则上标/小字会突然变大。
    style_scale = (body_size / src_body) if src_body else 1.0
    STYLE = make_style(body_size, tiers, small=note_sz, toc_sz=toc_sz)
    doc = Document()
    # python-docx 默认 doNotCompress：中文标点不做挤压，每行少排一两个字，
    # 整本书每页都多折一行。改成中文书通行的"标点压缩"，
    # 关掉中西文之间的自动加空，行宽才和原书一致。
    try:
        _st = doc.settings.element

        def _set_setting(tag, val):
            for e in _st.findall(qn(tag)):
                e.set(qn('w:val'), val)
                return
            e = OxmlElement(tag)
            e.set(qn('w:val'), val)
            _st.append(e)

        _set_setting('w:characterSpacingControl', 'compressPunctuation')
        _set_setting('w:autoSpaceDE', '0')
        _set_setting('w:autoSpaceDN', '0')
    except Exception:
        pass
    st = doc.styles['Normal']
    st.font.name = 'Times New Roman'
    st.font.size = Pt(body_size)
    st.element.rPr.rFonts.set(qn('w:eastAsia'), u'宋体')
    st.paragraph_format.space_after = Pt(0)
    MARK_H = 13.0            # 页首“原书第 N 页”那行的实际占高
    if geom:
        # 1:1 复刻原书纸张和版心。页眉文字并进页首标注行，放在上边距里，
        # 所以上边距要扣掉这一行的高度，正文才从原书的位置开始。
        # 页脚多留 8pt：原书版心高度量的是"字的墨迹范围"，最后一行只占
        # 半个行高；Word 的固定行距每行都要满额，不留这点余量每页会少排
        # 一行，整本书就会大面积溢出。
        bottom_m = max(20.0, geom['bottom'] - 8.0)
        # 论文行级版面：原书脚注（作者简介/通讯作者）常在版心底线
        # 以下；按测量的下边距会把最后一行顶到下一页，留 20pt 即可。
        if any(b[0] in ('linepage', 'frametree') for b in blocks):
            bottom_m = 20.0
        for s in doc.sections:
            s.page_width = Pt(geom['page_w'])
            s.page_height = Pt(geom['page_h'])
            s.left_margin = Pt(geom['left'])
            s.right_margin = Pt(geom['right'])
            s.top_margin = Pt(geom['top'])
            s.bottom_margin = Pt(bottom_m)
        text_w = geom['page_w'] - geom['left'] - geom['right']
        avail_h = geom['page_h'] - geom['top'] - bottom_m
    else:
        for s in doc.sections:
            s.page_width = Cm(14.8)
            s.page_height = Cm(21.0)
            s.left_margin = s.right_margin = Cm(1.6)
            s.top_margin = Cm(1.4)
            s.bottom_margin = Cm(1.4)
        text_w = 329.0
        avail_h = 516.0
    # 整本都是双栏文字层页（期刊/报纸）：按原书双栏排，别压成单栏。
    # 论文模式会插入 cols2 标记：第 1 页标题/摘要单栏，正文处再切双栏，
    # 所以不能一开始就把整节设成双栏。
    _hybrid_cols = any((b[0] in ('cols2', 'coltable', 'cols2page',
                                 'linepage', 'frametree'))
                       for b in blocks)
    if columns and columns > 1 and not _hybrid_cols:
        for s in doc.sections:
            _set_section_columns(s, columns)
        avail_h = 516.0 - 8.0

    # 不生成封面/说明页：Word 第 1 页就是原书第 1 页，
    # 原书目录上的页码可以直接对应。

    # 按原书页分组，估算高度；超出页面的页按比例缩小字号，
    # 保证“一页原书 = 一页 Word”。
    pages = []
    _cur = None
    for b in blocks:
        if b[0] == 'page':
            _cur = (int(b[1]), [])
            pages.append(_cur)
            continue
        if _cur is None:
            _cur = (0, [])
            pages.append(_cur)
        _cur[1].append(b)
    AVAIL = avail_h
    page_k = {}       # 字号缩放
    page_lk = {}      # 行距缩放（优先压行距，字号尽量不动）
    for _pno, _blist in pages:
        _est = _est_height(_blist, _pno, body_size, body_ls, STYLE,
                           appendix_from, text_w=text_w,
                           src_body=src_body, tiers=tiers,
                           geom=geom) * 1.03
        if _est <= AVAIL:
            page_k[_pno] = 1.0
            page_lk[_pno] = 1.0
        else:
            # 按原书版式排时这一轮什么都不动：估算总比 Word 实际保守，
            # 提前压行距会把本来排得下的页也压紧，正文缩在框的上半截，
            # 下面空出一大块。真排不下由分页验收按实际结果再压那几页。
            if geom:
                page_k[_pno] = 1.0
                page_lk[_pno] = 1.0
            else:
                lk = max(0.90, AVAIL / _est)
                page_lk[_pno] = lk
                rest = _est * lk
                page_k[_pno] = 1.0 if rest <= AVAIL else \
                    max(0.70, (AVAIL / rest) ** 0.5)
    if page_scale:
        for _p, _f in page_scale.items():
            page_k[_p] = page_k.get(_p, 1.0) * _f
    if lead_scale:
        # 只压行距、不动字号：字号保持一致比行距略紧更重要
        for _p, _f in lead_scale.items():
            page_lk[_p] = page_lk.get(_p, 1.0) * _f

    counts = collections.Counter()
    cur_page = 0
    k = 1.0
    plain = []
    pm_para = None       # 页首“原书第 N 页”那一段，页眉并进这一行
    prev_head = None     # 上一个标题段（用于把折行的标题并回去）
    prev_cap = False     # 上一段是表题（表题折行也按单倍行距排）
    started = False      # 是否已经开始输出页面（第一页不加段前分页）
    skip_next_break = False   # 刚分节换页，下一页标记不再加分页符
    skip_note_ix = set()      # 已并入页底注文框的后续 note 块
    style_cursors = {}
    _cur_land = False
    _skip_locked = False
    _page_tw, _page_avail = text_w, avail_h
    for bi, b in enumerate(blocks):
        if bi in skip_note_ix:
            continue
        kind, text, mask, verified, hlevel = b[0], b[1], b[2], b[3], b[4]
        if _skip_locked and kind != 'page':
            continue
        bsize = b[6] if len(b) > 6 else 0
        if kind == 'vspace':
            try:
                h = float(text)
                p = doc.add_paragraph()
                pf = p.paragraph_format
                pf.space_before = Pt(0)
                pf.space_after = Pt(0)
                pf.line_spacing = Pt(max(1.0, h))
                p.add_run('').font.size = Pt(1)
            except Exception:
                pass
            continue
        if kind == 'frametree':
            try:
                data = json.loads(text)
                lp = int(data.get('page') or 1)
                pk = page_k.get(lp, 1.0)
                pg_w = (geom or {}).get('page_w', 595.0)
                pg_h = (geom or {}).get('page_h', 813.0)
                scale = pg_w / 3400.0
                page_left = (geom or {}).get('left', 0.0)
                origin_root = page_left / scale if scale else 0.0
                right_root = ((page_left + text_w) / scale
                              if scale else 3400.0)
                root = data.get('node')

                def _table(container, widths):
                    tbl = container.add_table(rows=1, cols=len(widths))
                    tbl.autofit = False
                    tblPr = tbl._tbl.tblPr
                    tblW = tblPr.find(qn('w:tblW'))
                    if tblW is None:
                        tblW = OxmlElement('w:tblW')
                        tblPr.append(tblW)
                    tblW.set(qn('w:type'), 'dxa')
                    tblW.set(qn('w:w'), str(int(round(sum(widths) * 20))))
                    mar = OxmlElement('w:tblCellMar')
                    for side in ('left', 'right', 'top', 'bottom'):
                        el = OxmlElement('w:' + side)
                        el.set(qn('w:w'), '0')
                        el.set(qn('w:type'), 'dxa')
                        mar.append(el)
                    look = tblPr.find(qn('w:tblLook'))
                    if look is not None:
                        look.addprevious(mar)
                    else:
                        tblPr.append(mar)
                    for ci, w in enumerate(widths):
                        tbl.columns[ci].width = Pt(w)
                        tbl.cell(0, ci).width = Pt(w)
                    # 树级多栏表允许跨页；禁止跨页会把整页栏顶到下一页，
                    # 出现“第2页只剩一个页眉字”的空白页。
                    return tbl

                def _pad(container, pt):
                    if pt <= 1.0:
                        return
                    p = container.add_paragraph()
                    pf = p.paragraph_format
                    pf.space_before = Pt(0)
                    pf.space_after = Pt(0)
                    pf.line_spacing = Pt(min(pt, 40.0))
                    p.add_run('').font.size = Pt(1)

                def _obj_table(container, node, avail_w, origin_x, pk):
                    grid = node.get('grid') or {}
                    rows = grid.get('rows') or []
                    merges = grid.get('merges') or []
                    if not rows:
                        return
                    ncols = max(len(r) for r in rows)
                    bb = node.get('bbox') or (0, 0, 0, 0)
                    avail = max(20.0, avail_w)
                    wpt = max(12.0, (bb[2] - bb[0]) * scale)
                    if wpt > avail:
                        wpt = avail
                    raw_w = grid.get('widths') or []
                    if len(raw_w) == ncols and sum(raw_w) > 0:
                        widths = [w / float(sum(raw_w)) * wpt for w in raw_w]
                    else:
                        weights = []
                        for ci in range(ncols):
                            wmax = max((len(r[ci]) if ci < len(r) else 0)
                                       for r in rows)
                            weights.append(max(2.0, float(wmax)))
                        tot = sum(weights) or 1.0
                        widths = [wpt * w / tot for w in weights]
                    off = max(0.0, (bb[0] - origin_x) * scale)
                    if off + wpt > avail + 1.0:
                        off = 0.0
                    tbl = container.add_table(rows=len(rows), cols=ncols)
                    tbl.autofit = False
                    tbl.alignment = WD_TABLE_ALIGNMENT.LEFT
                    tblPr = tbl._tbl.tblPr
                    tw = tblPr.find(qn('w:tblW'))
                    if tw is None:
                        tw = OxmlElement('w:tblW')
                        tblPr.append(tw)
                    tw.set(qn('w:type'), 'dxa')
                    tw.set(qn('w:w'), str(int(round(sum(widths) * 20))))
                    ind = OxmlElement('w:tblInd')
                    ind.set(qn('w:w'), str(int(round(off * 20))))
                    ind.set(qn('w:type'), 'dxa')
                    tblPr.append(ind)
                    layout = OxmlElement('w:tblLayout')
                    layout.set(qn('w:type'), 'fixed')
                    tblPr.append(layout)
                    mar = OxmlElement('w:tblCellMar')
                    for side, val in (('top', 0), ('left', 20),
                                      ('bottom', 0), ('right', 20)):
                        el = OxmlElement('w:' + side)
                        el.set(qn('w:w'), str(val))
                        el.set(qn('w:type'), 'dxa')
                        mar.append(el)
                    tblPr.append(mar)
                    borders = OxmlElement('w:tblBorders')
                    for edge in ('top', 'left', 'bottom', 'right',
                                 'insideH', 'insideV'):
                        el = OxmlElement('w:' + edge)
                        if edge in ('left', 'right', 'insideV'):
                            el.set(qn('w:val'), 'nil')
                        else:
                            el.set(qn('w:val'), 'single')
                            el.set(qn('w:sz'), '6')
                            el.set(qn('w:space'), '0')
                            el.set(qn('w:color'), '000000')
                        borders.append(el)
                    tblPr.append(borders)
                    for ci, w in enumerate(widths):
                        tbl.columns[ci].width = Pt(w)
                    # 每个单元格都要写宽度：只写第一行时，纵向合并后的
                    # 引文格会退回默认宽度，整列文字被挤成两行，表格变高
                    for _row in tbl.rows:
                        for ci, w in enumerate(widths):
                            _row.cells[ci].width = Pt(w)
                    covered = set()
                    span_w = {}
                    for m in merges:
                        if len(m) < 4:
                            continue
                        r0, c0, rs, cs = [int(x) for x in m[:4]]
                        r1 = min(len(rows) - 1, r0 + max(1, rs) - 1)
                        c1 = min(ncols - 1, c0 + max(1, cs) - 1)
                        if r0 < 0 or c0 < 0 or r0 >= len(rows) or c0 >= ncols:
                            continue
                        span_w[(r0, c0)] = sum(
                            widths[c0:min(ncols, c1 + 1)])
                        try:
                            tbl.cell(r0, c0).merge(tbl.cell(r1, c1))
                        except Exception:
                            pass
                        for rr in range(r0, r1 + 1):
                            for cc in range(c0, c1 + 1):
                                if (rr, cc) != (r0, c0):
                                    covered.add((rr, cc))
                    aligns = grid.get('align') or []
                    fs = max(6.5, float(node.get('size') or body_size) * pk)
                    for ri, row in enumerate(rows):
                        for ci in range(ncols):
                            if (ri, ci) in covered:
                                continue
                            cell = tbl.cell(ri, ci)
                            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                            cp = cell.paragraphs[0]
                            pf = cp.paragraph_format
                            pf.space_before = Pt(0)
                            pf.space_after = Pt(0)
                            pf.line_spacing = 1.0
                            al = 'center'
                            if ri < len(aligns) and ci < len(aligns[ri]):
                                al = aligns[ri][ci] or 'center'
                            cp.alignment = (WD_ALIGN_PARAGRAPH.LEFT
                                            if al == 'left'
                                            else WD_ALIGN_PARAGRAPH.CENTER)
                            val = row[ci] if ci < len(row) else ''
                            run = cp.add_run(str(val))
                            run.font.size = Pt(fs)
                            run.font.name = 'Times New Roman'
                            run._element.rPr.rFonts.set(qn('w:eastAsia'),
                                                        u'宋体')
                            # 原书表格的每一行都是按格宽排好的；宋体略宽，
                            # 末尾一两个字会被挤到下一行，表格因此变高。
                            # 按最长的一行做轻微负字距，保住原书断行。
                            cw = span_w.get((ri, ci), widths[ci])
                            avail = max(8.0, cw - 2.0)
                            lns = str(val).split(u'\n') if val else ['']
                            nat = max(sum(
                                fs * (0.55 if ord(ch) < 0x2e80 else 1.0)
                                for ch in ln) for ln in lns)
                            if nat > avail * 0.99:
                                sp = max(-0.45, (avail * 0.99 - nat) /
                                         max(1, max(len(ln) for ln in lns)))
                                rPr = run._element.get_or_add_rPr()
                                sp_el = OxmlElement('w:spacing')
                                sp_el.set(qn('w:val'),
                                          str(int(round(sp * 20))))
                                sz = rPr.find(qn('w:sz'))
                                if sz is not None:
                                    sz.addprevious(sp_el)
                                else:
                                    rPr.append(sp_el)
                    # 表格后留一个 1pt 空段，避免相邻表格/页边界粘连
                    p = container.add_paragraph()
                    p.paragraph_format.space_before = Pt(0)
                    p.paragraph_format.space_after = Pt(0)
                    p.paragraph_format.line_spacing = Pt(1)
                    p.add_run('').font.size = Pt(1)

                def _obj_figure(container, node, avail_w, origin_x, pk):
                    path = node.get('path') or ''
                    if not path or not os.path.exists(path):
                        return
                    bb = node.get('bbox') or (0, 0, 0, 0)
                    wpt = max(12.0, (bb[2] - bb[0]) * scale)
                    if wpt > avail_w:
                        wpt = avail_w
                    p = container.add_paragraph()
                    pf = p.paragraph_format
                    pf.space_before = Pt(0)
                    pf.space_after = Pt(0)
                    pf.line_spacing = 1.0
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    try:
                        p.add_run().add_picture(path, width=Pt(wpt))
                    except Exception:
                        pass

                def _object(container, node, avail_w, origin_x, right_x, pk):
                    if node.get('kind') == 'table':
                        _obj_table(container, node, avail_w, origin_x, pk)
                    else:
                        _obj_figure(container, node, avail_w, origin_x, pk)

                def _leaf(container, node, avail_w, origin_x, right_x):
                    lines = node.get('lines') or []
                    prev_y = None
                    for j, ln in enumerate(lines):
                        cp = container.add_paragraph()
                        pf = cp.paragraph_format
                        pf.space_before = Pt(0)
                        pf.space_after = Pt(0)
                        fs = max(6.5, float(ln.get('size') or body_size) * pk)
                        dy = 0.0
                        if j > 0:
                            dy = (ln.get('y0', 0) - prev_y) / 5000.0 * pg_h
                        body_here = body_ls * pk
                        if fs < body_size * pk * 0.88:
                            # 脚注/小字不再套正文的双倍行距：它们本来只占
                            # 一半高度，套正文行距既浪费版面，又会迫使
                            # 整页把正文行距压得很挤（用户看到的“字太挤”）。
                            tree_ls = min(body_here,
                                          max(fs * 1.12,
                                              dy if dy > 0 else fs * 1.25))
                        else:
                            tree_ls = body_here * TREE_LINE_SCALE
                        pf.line_spacing = Pt(max(8.0, tree_ls,
                                                 fs * 1.05))
                        if j > 0:
                            # 只补源行距比本行行距多出来的部分；不要用
                            # 正文 pitch 去补脚注，否则脚注又被撑回双倍。
                            extra = max(0.0, dy - tree_ls)
                            if extra > 0.8:
                                pf.space_before = Pt(extra)
                        indent = max(0.0, (ln.get('x0', 0) -
                                           origin_x) * scale)
                        if indent > 0.4:
                            pf.first_line_indent = Pt(indent)
                        gap = (right_x - ln.get('x1', 0)) * scale
                        full = gap <= max(6.0, fs * 0.6)
                        left_gap = indent
                        line_w = max(0.0, (ln.get('x1', 0) -
                                           ln.get('x0', 0)) * scale)
                        centered = (left_gap > 8.0 and gap > 8.0 and
                                    abs(left_gap - gap) <
                                    max(10.0, 0.12 * avail_w) and
                                    line_w < avail_w * 0.92)
                        if centered:
                            pf.first_line_indent = Pt(0)
                            cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
                        else:
                            cp.alignment = (
                                WD_ALIGN_PARAGRAPH.DISTRIBUTE if full
                                else WD_ALIGN_PARAGRAPH.JUSTIFY)
                        run = cp.add_run(ln.get('text') or '')
                        run.font.size = Pt(fs)
                        run.font.name = 'Times New Roman'
                        run._element.rPr.rFonts.set(qn('w:eastAsia'), u'宋体')
                        avail = max(4.0, avail_w - indent)
                        _fit_line_width(run, ln.get('text') or '', fs, avail)
                        prev_y = ln.get('y0', prev_y or 0)

                def _render(container, node, avail_w, origin_x, right_x):
                    if not node:
                        return
                    typ = node.get('type')
                    if typ == 'object':
                        _object(container, node, avail_w, origin_x,
                                right_x, pk)
                        return
                    if typ == 'leaf':
                        _leaf(container, node, avail_w, origin_x, right_x)
                        return
                    ch = node.get('children') or []
                    if not ch:
                        return
                    if typ == 'columns':
                        col_w, gut_w, widths_all = [], [], []
                        for ci, c in enumerate(ch):
                            bb = c.get('bbox') or (0, 0, 0, 0)
                            w = max(8.0, (bb[2] - bb[0]) * scale)
                            col_w.append(w)
                            widths_all.append(w)
                            if ci + 1 < len(ch):
                                nb = ch[ci + 1].get('bbox') or bb
                                g = max(8.0, (nb[0] - bb[2]) * scale)
                                gut_w.append(g)
                                widths_all.append(g)
                        total = sum(widths_all)
                        if total > avail_w and total > 0:
                            z = avail_w / total
                            widths_all = [w * z for w in widths_all]
                            col_w = [w * z for w in col_w]
                        tbl = _table(container, widths_all)
                        for ci, c in enumerate(ch):
                            tci = ci * 2
                            if tci >= len(widths_all):
                                break
                            cell = tbl.cell(0, tci)
                            # 新建单元格自带一个空段落；不清掉会白占一行，
                            # 整版双栏页就容易多出几行，被挤到下一页。
                            if cell.paragraphs and \
                                    not cell.paragraphs[0].text.strip():
                                cell._element.remove(
                                    cell.paragraphs[0]._p)
                            bb = c.get('bbox') or (0, 0, 0, 0)
                            _render(cell, c, col_w[ci], bb[0], bb[2])
                        return
                    # band：纵向叠放；用源 y 间距补空
                    prev_bottom = None
                    for c in ch:
                        bb = c.get('bbox') or (0, 0, 0, 0)
                        if prev_bottom is not None:
                            gap = (bb[1] - prev_bottom) / 5000.0 * pg_h
                            _pad(container, max(0.0, gap))
                        _render(container, c, avail_w, origin_x, right_x)
                        prev_bottom = bb[3]

                if root:
                    if root.get('type') == 'leaf':
                        _leaf(doc, root, text_w, origin_root, right_root)
                    else:
                        _render(doc, root, text_w, origin_root, right_root)
            except Exception:
                pass
            plain.append(u'[第%s页版面树]' % (data.get('page')
                                               if 'data' in locals() else '?'))
            continue
        if kind == 'linepage':
            try:
                data = json.loads(text)
                lp = int(data.get('page') or 1)
                pk = page_k.get(lp, 1.0)
                plk = page_lk.get(lp, 1.0)
                pg_h = (geom or {}).get('page_h', 813.0)
                scale = ((geom or {}).get('page_w', 595.0) / 3400.0)
                lf = data.get('left') or {}
                rf = data.get('right') or {}
                if data.get('cols') == 1:
                    # 单栏论文：逐行做普通段落，不再套表格（表格整行
                    # 不能跨页时会被顶到下一页，留下整页空白）。
                    frame = lf
                    tw = text_w
                    lines = (frame or {}).get('lines') or []
                    prev_y = None
                    left_edge = (geom or {}).get('left', 0.0)
                    top_edge = (geom or {}).get('top', 0.0)
                    for j, ln in enumerate(lines):
                        cp = doc.add_paragraph()
                        pf = cp.paragraph_format
                        pf.space_before = Pt(0)
                        pf.space_after = Pt(0)
                        fs = max(6.5, float(ln.get('size') or body_size) * pk)
                        pf.line_spacing = Pt(max(8.0, body_ls * pk, fs * 1.05))
                        if j == 0:
                            extra = max(0.0, ln.get('y0', 0) / 5000.0 *
                                        pg_h - top_edge)
                            # 第 2 页起，页标那一段本身占一行（段前分页）；
                            # 不扣掉它，每页都会被顶下约一行，最后一页
                            # 还会多出一页只印一行脚的空白页。
                            if lp > 1:
                                extra = max(0.0, extra - body_ls * pk)
                        else:
                            dy = (ln.get('y0', 0) - prev_y) / 5000.0 * pg_h
                            extra = max(0.0, dy - body_ls * pk)
                        if extra > 0.8:
                            pf.space_before = Pt(extra)
                        indent = max(0.0, ln.get('x0', 0) * scale - left_edge)
                        if indent > 0.4:
                            pf.first_line_indent = Pt(indent)
                        gap = (frame.get('x1', 0) - ln.get('x1', 0)) * scale
                        full = gap <= max(6.0, body_size * pk * 0.6)
                        cp.alignment = (WD_ALIGN_PARAGRAPH.DISTRIBUTE
                                        if full else WD_ALIGN_PARAGRAPH.JUSTIFY)
                        run = cp.add_run(ln.get('text') or '')
                        run.font.size = Pt(fs)
                        run.font.name = 'Times New Roman'
                        run._element.rPr.rFonts.set(qn('w:eastAsia'), u'宋体')
                        avail = max(4.0, tw - indent)
                        _fit_line_width(run, ln.get('text') or '', fs, avail)
                        prev_y = ln.get('y0', prev_y or 0)
                    plain.append(u'[第%s页正文]' % data.get('page'))
                    continue
                lw = max(30.0, (lf.get('x1', 1600) -
                                lf.get('x0', 287)) * scale)
                rw = max(30.0, (rf.get('x1', 3100) -
                                rf.get('x0', 1740)) * scale)
                gut = 18.0
                if lf and rf:
                    gut = max(8.0, (rf.get('x0', 1740) -
                                    lf.get('x1', 1600)) * scale)
                total = lw + gut + rw
                if total > text_w and total > 0:
                    z = text_w / total
                    lw, gut, rw = lw * z, gut * z, rw * z
                tbl = doc.add_table(rows=1, cols=3)
                tbl.autofit = False
                tblPr = tbl._tbl.tblPr
                tblW = tblPr.find(qn('w:tblW'))
                if tblW is None:
                    tblW = OxmlElement('w:tblW')
                    tblPr.append(tblW)
                tblW.set(qn('w:type'), 'dxa')
                tblW.set(qn('w:w'), str(int(round((lw + gut + rw) * 20))))
                mar = OxmlElement('w:tblCellMar')
                for side in ('left', 'right', 'top', 'bottom'):
                    el = OxmlElement('w:' + side)
                    el.set(qn('w:w'), '0')
                    el.set(qn('w:type'), 'dxa')
                    mar.append(el)
                look = tblPr.find(qn('w:tblLook'))
                if look is not None:
                    look.addprevious(mar)
                else:
                    tblPr.append(mar)
                widths = [lw, gut, rw]
                for ci in range(3):
                    tbl.columns[ci].width = Pt(widths[ci])
                    tbl.cell(0, ci).width = Pt(widths[ci])
                # 行不能被 Word 拆到下一页；源页能放下，线级排也必须放下
                trPr = tbl.rows[0]._tr.get_or_add_trPr()
                cs = OxmlElement('w:cantSplit')
                trPr.append(cs)
                pad = float(data.get('pad') or 0.0)
                for ci, frame in ((0, lf), (2, rf)):
                    cell = tbl.cell(0, ci)
                    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
                    lines = (frame or {}).get('lines') or []
                    firstp = cell.paragraphs[0]
                    if not lines:
                        firstp.paragraph_format.space_before = Pt(0)
                        firstp.paragraph_format.space_after = Pt(0)
                        continue
                    prev_y = None
                    for j, ln in enumerate(lines):
                        cp = firstp if j == 0 else cell.add_paragraph()
                        pf = cp.paragraph_format
                        pf.space_before = Pt(0)
                        pf.space_after = Pt(0)
                        fs = max(6.5, float(ln.get('size') or body_size) * pk)
                        line_ls = body_ls * pk * TREE_LINE_SCALE
                        pf.line_spacing = Pt(max(8.0, line_ls, fs * 1.05))
                        if j == 0:
                            extra = pad
                        else:
                            dy = (ln.get('y0', 0) - prev_y) / 5000.0 * pg_h
                            # 行距已经压到 0.90 后，不要再按原 pitch
                            # 补差额，否则压缩被“段前距”全部抵消。
                            extra = max(0.0, dy - body_ls * pk)
                        if extra > 0.8:
                            pf.space_before = Pt(extra)
                        indent = max(0.0, (ln.get('x0', 0) -
                                           frame.get('x0', 0)) * scale)
                        if indent > 0.4:
                            pf.first_line_indent = Pt(indent)
                        gap = (frame.get('x1', 0) - ln.get('x1', 0)) * scale
                        full = gap <= max(6.0, body_size * pk * 0.6)
                        cp.alignment = (WD_ALIGN_PARAGRAPH.DISTRIBUTE
                                        if full else WD_ALIGN_PARAGRAPH.JUSTIFY)
                        run = cp.add_run(ln.get('text') or '')
                        run.font.size = Pt(fs)
                        run.font.name = 'Times New Roman'
                        run._element.rPr.rFonts.set(qn('w:eastAsia'), u'宋体')
                        # 宋体比原书方正字体宽：中文行才做轻微负字距，
                        # 英文行交给 Word 自然排版，避免被压得字挤成一团。
                        avail = max(4.0, (lw if ci == 0 else rw) - indent)
                        _fit_line_width(run, ln.get('text') or '', fs, avail)
                        prev_y = ln.get('y0', prev_y or 0)
            except Exception:
                pass
            plain.append(u'[第%s页正文]' % (data.get('page') if 'data' in locals()
                                             else '?'))
            continue
        if kind == 'topspace':
            # 论文首页：按题名在原 PDF 里的顶端位置补页首留白（刊眉被
            # 剔除后，若不补，标题和摘要整体上移，页尾会空一大截）。
            if geom:
                try:
                    y = float(text) / 5000.0 * geom['page_h']
                    top = max(geom['top'], min(y, geom['page_h'] * 0.35))
                    doc.sections[0].top_margin = Pt(top)
                except Exception:
                    pass
            continue
        if kind == 'cols2':
            # 论文第 1 页：到这里结束"标题/摘要单栏"，下面正文改双栏
            sec = doc.add_section(WD_SECTION.CONTINUOUS)
            if geom:
                sec.page_width = Pt(geom['page_w'])
                sec.page_height = Pt(geom['page_h'])
                sec.left_margin = Pt(geom['left'])
                sec.right_margin = Pt(geom['right'])
                sec.top_margin = Pt(geom['top'])
                sec.bottom_margin = Pt(max(20.0, geom['bottom'] - 8.0))
            _set_section_columns(sec, 2)
            continue
        if kind == 'cols2page':
            # 第 1 页用无框两列表格排正文；从这里另起一页的双栏节
            sec = doc.add_section(WD_SECTION.NEW_PAGE)
            if geom:
                sec.page_width = Pt(geom['page_w'])
                sec.page_height = Pt(geom['page_h'])
                sec.left_margin = Pt(geom['left'])
                sec.right_margin = Pt(geom['right'])
                sec.top_margin = Pt(geom['top'])
                sec.bottom_margin = Pt(max(20.0, geom['bottom'] - 8.0))
            _set_section_columns(sec, 2)
            skip_next_break = True
            continue
        if kind == 'coltable':
            try:
                data = json.loads(text)
                # 三列：左栏 / 空栏距 / 右栏。无框表格默认单元格左右
                # 边距 0.19cm 会把两栏压窄；这里清零，另用空列保出原书
                # 约 18pt 的栏间距，栏宽和换行位置才与原页一致。
                tbl = doc.add_table(rows=1, cols=3)
                tbl.autofit = False
                tw = text_w
                gutter = 18.0
                col_w = max(30.0, (tw - gutter) / 2.0)
                widths = [col_w, gutter, col_w]
                tblPr = tbl._tbl.tblPr
                tblW = tblPr.find(qn('w:tblW'))
                if tblW is not None:
                    tblW.set(qn('w:type'), 'dxa')
                    tblW.set(qn('w:w'), str(int(round(tw * 20))))
                mar = OxmlElement('w:tblCellMar')
                for side in ('left', 'right'):
                    el = OxmlElement('w:' + side)
                    el.set(qn('w:w'), '0')
                    el.set(qn('w:type'), 'dxa')
                    mar.append(el)
                look = tblPr.find(qn('w:tblLook'))
                if look is not None:
                    look.addprevious(mar)
                else:
                    tblPr.append(mar)
                for ci in range(3):
                    tbl.columns[ci].width = Pt(widths[ci])
                    tbl.cell(0, ci).width = Pt(widths[ci])
                for ci, key in ((0, 'left'), (2, 'right')):
                    cell = tbl.cell(0, ci)
                    first = True
                    last_p, last_t = None, ''
                    for bkind, btext in data.get(key) or []:
                        cp = cell.paragraphs[0] if first else cell.add_paragraph()
                        first = False
                        pf = cp.paragraph_format
                        pf.space_before = Pt(0)
                        pf.space_after = Pt(0)
                        pf.line_spacing = Pt((body_ls * k if body_ls else
                                              body_size * 1.45) * lk)
                        if bkind in ('body', 'head', 'subhead'):
                            pf.first_line_indent = Pt(body_size * k * 2)
                        run = cp.add_run(btext)
                        run.font.size = Pt(max(6.5, body_size * k))
                        run.font.name = 'Times New Roman'
                        run._element.rPr.rFonts.set(qn('w:eastAsia'), u'宋体')
                        if bkind == 'subhead':
                            # 一级标题的宋体全角字比原书标题字体宽，栏内
                            # 差约 15pt；收紧 0.7pt/字即可排成一行。
                            rPr = run._element.get_or_add_rPr()
                            sp = OxmlElement('w:spacing')
                            sp.set(qn('w:val'), '-14')
                            sz = rPr.find(qn('w:sz'))
                            if sz is not None:
                                sz.addprevious(sp)
                            else:
                                rPr.append(sp)
                        last_p, last_t = cp, (btext or '').strip()
                    # 第 1 页正文在表格里被页边界截断，最后一行的段落
                    # 若没说完，用"分散对齐"撑到右边界（和原书书页一致）。
                    # 但只剩三五个字时分散会把字距拉得很夸张，宁可留白。
                    if last_p is not None and not re.search(
                            u'[。！？.!?；;]$', last_t):
                        fs = max(6.5, body_size * k)
                        cpl = max(1, int(widths[ci] /
                                         max(1.0, fs * CHAR_SCALE / 100.0)))
                        tail_chars = (len(last_t) - 1) % cpl + 1
                        if tail_chars >= max(8, int(cpl * 0.4)):
                            try:
                                last_p.alignment = \
                                    WD_ALIGN_PARAGRAPH.DISTRIBUTE
                            except Exception:
                                pass
            except Exception:
                pass
            plain.append(u'[第1页正文]')
            continue
        if kind == 'page':
            cur_page = int(text)
            if page_geometries and cur_page in page_geometries:
                geom = page_geometries[cur_page]
            k = page_k.get(cur_page, 1.0)
            lk = page_lk.get(cur_page, 1.0)
            _is_land = bool(landscape_pages and cur_page in landscape_pages)
            if _is_land != _cur_land:
                if not started:
                    _set_section_orientation(doc.sections[0], geom, _is_land)
                else:
                    _sec = doc.add_section(WD_SECTION.NEW_PAGE)
                    _set_section_orientation(_sec, geom, _is_land)
                    # 分节本身已经换页，别再插段前分页，否则会多一页
                    skip_next_break = True
                _cur_land = _is_land
            if _is_land and geom:
                _page_tw = geom['page_h']
                _page_avail = geom['page_w'] - 10
            else:
                _page_tw, _page_avail = text_w, avail_h
            if started and not skip_next_break:
                # 用"段前分页"而不是插一个分页符：显式分页符会在每页底部
                # 多出一个空的"分页符"行，占掉一行正文的高度
                brk = doc.add_paragraph()
                brk.paragraph_format.page_break_before = True
                brk.paragraph_format.space_before = Pt(0)
                brk.paragraph_format.space_after = Pt(0)
                brk.paragraph_format.line_spacing = Pt(1)
                brk.add_run('').font.size = Pt(1)
            started = True
            skip_next_break = False
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(3)
            p.paragraph_format.line_spacing = Pt(10)
            _pn = (page_numbers or {}).get(cur_page)
            _sec = (page_sections or {}).get(cur_page)
            if _pn is not None and str(_pn) != str(cur_page):
                # 印刷页码和 PDF 页序不一致时两个都写：期刊附录常从 1
                # 重新编号，只写“原书第1页”会让人以为整本乱号。
                if _sec:
                    _plabel = u'原书第 %s 页（%s第 %s 页）' % (
                        cur_page, _sec, _pn)
                else:
                    _plabel = u'原书第 %s 页（印刷 %s）' % (cur_page, _pn)
            elif _pn is not None:
                _plabel = u'原书第 %s 页' % _pn
            else:
                _plabel = u'原书第 %s 页' % cur_page
            if geom:
                # 页码行钉在原书页码所在的位置（页顶或页底），不占正文高度。
                # 只有这一页真的识别到印刷页码时才用全局 number_y；正文页
                # 没有页码时，number_y 可能来自附录页，落在正文里会压字，
                # 这时放到上边距。
                if cur_page in (page_numbers or {}):
                    _ny = geom.get('number_y')
                    # 检测到的“页码 y”可能其实是正文里的编号（例如
                    # “1. 实验目的”），若落在版心内就不能用；这种页
                    # 把页标放到页顶极小的安全区。
                    _in_body = (_ny is not None
                                and geom.get('top', 0) - 4 < _ny
                                < geom['page_h'] - geom.get('bottom', 0) + 4)
                    if _ny is not None and not _in_body:
                        _ypt = _ny
                    elif geom.get('number_bottom'):
                        _ypt = max(4.0, geom['page_h'] - geom['bottom'] + 2.0)
                    else:
                        _ypt = max(4.0, min(20.0, geom.get('top', 70) * 0.2))
                elif geom.get('number_bottom'):
                    # 全书页码在页底：没识别到号码的页也放在页底边距，
                    # 不要跑到页顶压住正文。
                    _ypt = max(4.0, geom['page_h'] - geom['bottom'] + 2.0)
                else:
                    _ypt = max(4.0, min(20.0, geom.get('top', 70) * 0.2))
                if _is_land:
                    _ypt = 2.0
                _set_page_frame(p, 0.0, max(2.0, _ypt), _page_tw)
                if geom.get('number_right') is False:
                    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            _set_font(p.add_run(u'— %s —' % _plabel), size=7.5, color=GRAY)
            pm_para = p
            prev_head = None
            prev_cap = False
            plain.append(u'\n===== 第 %s 页 =====\n' % text)
            if locked_pages and cur_page in locked_pages:
                _emit_locked_page(doc, locked_pages[cur_page], cur_page,
                                  geom, body_size, body_ls, k,
                                  style_maps, style_cursors, style_scale,
                                  text_w)
                _skip_locked = True
            else:
                _skip_locked = False
            continue
        if kind == 'note':
            # 同一页连续的注文块一起钉到页底（多条注文用换行分隔）
            j = bi
            group = []
            while j < len(blocks) and blocks[j][0] == 'note':
                group.append(blocks[j])
                j += 1
            skip_note_ix.update(range(bi + 1, j))
            _emit_bottom_notes(doc, group, k, lk, geom, text_w, STYLE['note'])
            counts['note'] += len(group)
            plain.append(''.join(x[1] for x in group))
            continue
        if kind == 'subhead':
            # 跨页被切开的"3. …"标题：内容已并回同一段，放到下一页页首。
            # 双栏栏宽只差几个磅，略收紧字距即可排成一行，避免"破。"单字
            # 掉到下一行；字号、字体仍与原书正文一致。
            p = doc.add_paragraph()
            pf = p.paragraph_format
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            pf.space_before = Pt(0)
            pf.space_after = Pt(0)
            pf.line_spacing = Pt(body_ls * k)
            pf.first_line_indent = Pt(body_size * k * 2)
            pf.keep_with_next = True
            run = p.add_run(text)
            run.font.size = Pt(max(6.5, body_size * k))
            run.font.name = 'Times New Roman'
            run._element.rPr.rFonts.set(qn('w:eastAsia'), u'宋体')
            rPr = run._element.get_or_add_rPr()
            sp = OxmlElement('w:spacing')
            sp.set(qn('w:val'), '-7')       # -0.35pt
            sz = rPr.find(qn('w:sz'))
            if sz is not None:
                sz.addprevious(sp)
            else:
                rPr.append(sp)
            plain.append(text)
            continue
        is_cont = text.startswith(u'')      # 跨页延续段：不缩进
        if is_cont:
            text = text[1:]
            if mask:
                mask = mask[1:]
        region = None
        region_known = bool(regions and cur_page in regions)
        if region_known:
            from . import format as _fmt
            by0 = b[5] if len(b) > 5 else None
            if by0 is not None:
                region = _fmt.region_of(by0, regions.get(cur_page, []))
        role = classify(kind, text, hlevel, verified, cur_page, appendix_from,
                        size=bsize, body_size=src_body, region=region,
                        tier=tier_index(bsize, tiers),
                        region_known=region_known)
        # 表题及其折行（"Table 1:" 下面还有一行标题）按单倍行距排：
        # 原书表题是单倍行距，套正文的双倍行距会把表下的正文挤到下一页
        if role == 'body' and (
                CAPTION_BLOCK.match(text)
                or (prev_cap and len(text) <= 160
                    and not re.search(u'[。！？.!?]$', text))):
            role = 'cap'
        prev_cap = (role == 'cap')
        counts[role] += 1
        if role not in ('h1', 'h2', 'h3', 'h4'):
            prev_head = None
        if role == 'cap':
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            pf = p.paragraph_format
            pf.space_before = Pt(0)
            pf.space_after = Pt(1)
            pf.line_spacing = Pt(max(10.0, (bsize or 12.0) * 1.25))
            _set_font(p.add_run(text), size=max(8.0, body_size * k),
                      ea=u'宋体')
            plain.append(text)
            continue
        if role in ('image', 'tablepage'):
            # 插图/数据页截图：按原书上的矩形定尺寸，居中，不参与文字样式
            ibox = b[7] if len(b) > 7 else None
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(0)
            try:
                if geom and ibox:
                    # 图的宽高都照原书矩形来：图占多大地方就留多大地方，
                    # 图下面的正文接着排，不会被挤到下一页去
                    wpt = max(1.0, (ibox[2] - ibox[0]) / 3400.0 * geom['page_w'])
                    hpt = max(1.0, (ibox[3] - ibox[1]) / 5000.0 * geom['page_h'])
                    sc = min(1.0, text_w / wpt, (avail_h - 4) / hpt)
                    # 这一页被判定超页时，图片也跟着缩，否则文字压小了、
                    # 图片仍按原大占着，页面还是放不下（图注会被挤到下一页）
                    if k < 1.0:
                        sc *= max(0.55, k)
                    p.add_run().add_picture(text, width=Pt(wpt * sc),
                                            height=Pt(hpt * sc))
                    # 不要设"固定行距"：行距比图矮时 Word 会把图顶到行外，
                    # 图和上下文就对不齐了。单倍行距由图片自己撑高。
                    p.paragraph_format.line_spacing = 1.0
                elif geom and role == 'tablepage':
                    # 整页截图没有原矩形可依：按版心宽度等比缩放；
                    # 横排页按横排版心宽度，图才不会被压成一条。
                    _wcm = _page_tw / 28.3465 - 0.2
                    _hpt = _img_height_pt(text, _wcm)
                    if _hpt > _page_avail - 4:
                        _wcm = _wcm * (_page_avail - 4) / _hpt
                    # 整页截图不要再跟 page_scale 一起缩：它已经按页高
                    # 等比缩过；再乘 k 会缩成半页大，四周全是空白。
                    if k < 1.0 and role != 'tablepage':
                        _wcm *= max(0.55, k)
                    p.add_run().add_picture(text, width=Cm(_wcm))
                else:
                    _w = (11.4 if role == 'tablepage' else 10.8)
                    # 图片不能比版心宽：按原书版式排时版心只有 10.2cm，
                    # 11.4cm 的截图会被挤到下一页，这一页就"超页"了
                    _w = min(_w, text_w / 28.3465 - 0.2)
                    if k < 1:
                        _w = max(7.0, _w * k)
                    p.add_run().add_picture(text, width=Cm(_w))
            except Exception:
                pass
            plain.append(u'[插图]' if role == 'image' else u'[数据页]')
            continue
        if role == 'tablegrid':
            # 通用表格检测出的表格：重建为 Word 真表格（带边框、可编辑）
            data = json.loads(text)
            if isinstance(data, dict):
                rows = data.get('rows') or []
                merges = data.get('merges') or []
                widths0 = data.get('widths') or []
            else:
                rows = data
                merges, widths0 = [], []
            ibox = b[7] if len(b) > 7 else None
            _cap = data.get('_caption') if isinstance(data, dict) else ''
            if _cap:
                _cp = doc.add_paragraph()
                _cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
                _cp.paragraph_format.space_before = Pt(2)
                _cp.paragraph_format.space_after = Pt(2)
                _set_font(_cp.add_run(_cap), size=9.0)
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(4)
            try:
                ncols = max(len(r) for r in rows) if rows else 1
                tbl = doc.add_table(rows=len(rows), cols=ncols)
                tbl.style = 'Table Grid'
                tbl.autofit = False
                _tp0 = tbl._tbl.tblPr
                for _e in _tp0.findall(qn('w:tblCellMar')):
                    _tp0.remove(_e)
                _mar = OxmlElement('w:tblCellMar')
                for _side, _w in (('left', 20), ('right', 20),
                                  ('top', 10), ('bottom', 10)):
                    _el = OxmlElement('w:' + _side)
                    _el.set(qn('w:w'), str(_w))
                    _el.set(qn('w:type'), 'dxa')
                    _mar.append(_el)
                _tp0.append(_mar)
                # 表格总宽照原书表格的左右边界（不超过版心）。
                # 有矢量框线的表格直接用原书每列宽度；文字对齐猜出来的
                # 表格没有列宽信息，才按每列最多字数分配。
                if _is_land:
                    tw = max(100.0, _page_tw - 10.0)
                elif geom and ibox:
                    tw = min(text_w,
                             (ibox[2] - ibox[0]) / 3400.0 * geom['page_w'])
                else:
                    tw = text_w
                if geom and ibox:
                    # 横排表的 x0 要换算到横排页宽；表宽按原表 bbox，
                    # 表左缩进也按 bbox，否则 9 列会从页面最左边溢出。
                    _pgw = geom['page_h'] if _is_land else geom['page_w']
                    _tw_src = (ibox[2] - ibox[0]) / 3400.0 * _pgw
                    tw = min(_page_tw if _is_land else text_w, _tw_src)
                    if isinstance(data, dict) and (
                            data.get('_rel_fixed') or data.get('_model_fixed')
                            or data.get('_lab_fixed')) and widths0:
                        # 专用表重建时 bbox 可能只覆盖左半/部分列，
                        # 用原表列宽合计定宽，避免 10 列被压窄。
                        tw = min(_page_tw if _is_land else text_w,
                                 sum(widths0) / 3400.0 * _pgw)
                    _ind = ibox[0] / 3400.0 * _pgw
                    if not _is_land:
                        _ind -= geom.get('left', 0.0)
                    if _ind > 1:
                        _tp = tbl._tbl.tblPr
                        for _e in _tp.findall(qn('w:tblInd')):
                            _tp.remove(_e)
                        _iel = OxmlElement('w:tblInd')
                        _iel.set(qn('w:w'), str(int(_ind * 20)))
                        _iel.set(qn('w:type'), 'dxa')
                        _tp.append(_iel)
                if isinstance(data, dict) and data.get('full_width'):
                    tw = text_w
                    _tp = tbl._tbl.tblPr
                    for _e in _tp.findall(qn('w:tblInd')):
                        _tp.remove(_e)
                if not _is_land and ncols >= 8 and len(rows) >= 15:
                    # 10 列双栏宽表：按整幅版心铺开，否则每列只剩几毫米，
                    # 表头和数字全被拆成竖排。
                    tw = text_w
                    _tp = tbl._tbl.tblPr
                    for _e in _tp.findall(qn('w:tblInd')):
                        _tp.remove(_e)
                if len(widths0) == ncols and geom and sum(widths0) > 0:
                    widths = [w / 3400.0 * geom['page_w'] for w in widths0]
                    ssum = sum(widths)
                    widths = [w * tw / ssum for w in widths]
                else:
                    weights = []
                    for ci in range(ncols):
                        wmax = max((len(r[ci]) if ci < len(r) else 0)
                                   for r in rows) if rows else 1
                        weights.append(max(2.0, float(wmax)))
                    tot = sum(weights) or 1.0
                    widths = [max(12.0, tw * w / tot) for w in weights]
                    ssum = sum(widths)
                    widths = [w * tw / ssum for w in widths]
                # 原书列宽是按原书字号量的；Word 里表格字号可能更大
                # （10.5pt 的 "ControlVar" 在 8pt 列宽里会被拆成
                # "ControlV/ar"）。按单元格实际文字宽度给窄列加宽，再从
                # 宽列里匀出来，总宽不变。
                _cell_sz = max(6.5, (data.get('size') if isinstance(data, dict)
                                    else 0) or 9.0)
                _cell_sz = max(6.5, _cell_sz * k)
                _desired = []
                for ci in range(ncols):
                    _wm = 0.0
                    for r in rows:
                        val = str(r[ci]) if ci < len(r) else ''
                        for _ln in val.split('\n'):
                            _wm = max(_wm, _text_width(_ln, _cell_sz))
                    _desired.append(_wm + 6.0)
                widths = [max(widths[ci],
                              min(_desired[ci], tw * 0.38))
                          for ci in range(ncols)]
                _ss = sum(widths) or 1.0
                if _ss > tw:
                    widths = [w * tw / _ss for w in widths]
                for ci in range(ncols):
                    tbl.columns[ci].width = Pt(widths[ci])
                for _row in tbl.rows:
                    for ci in range(ncols):
                        _row.cells[ci].width = Pt(widths[ci])
                # 合并单元格（原书表格里常见的"一个标签跨几行"）
                covered = set()
                for _m in merges:
                    if len(_m) < 4:
                        continue
                    r0, c0, rs, cs = [int(x) for x in _m[:4]]
                    r1 = min(len(rows) - 1, r0 + max(1, rs) - 1)
                    c1 = min(ncols - 1, c0 + max(1, cs) - 1)
                    if r0 < 0 or c0 < 0 or r0 >= len(rows) or c0 >= ncols:
                        continue
                    try:
                        tbl.cell(r0, c0).merge(tbl.cell(r1, c1))
                    except Exception:
                        pass
                    for rr in range(r0, r1 + 1):
                        for cc in range(c0, c1 + 1):
                            if (rr, cc) != (r0, c0):
                                covered.add((rr, cc))
                for ri, r in enumerate(rows):
                    for ci in range(ncols):
                        if (ri, ci) in covered:
                            continue
                        cell = tbl.cell(ri, ci)
                        val = r[ci] if ci < len(r) else ''
                        cp = cell.paragraphs[0]
                        cp.paragraph_format.space_before = Pt(1)
                        cp.paragraph_format.space_after = Pt(1)
                        cp.paragraph_format.line_spacing = Pt(
                            max(6.5, _cell_sz * 1.05))
                        cp.paragraph_format.first_line_indent = Pt(0)
                        cp.paragraph_format.left_indent = Pt(0)
                        # 单元格里的 \n 是原书断行：一个 run 写进去，
                        # python-docx 会转成软换行，不额外占段间距
                        run = cp.add_run(str(val))
                        run.font.size = Pt(_cell_sz)
                        run.font.name = 'Times New Roman'
                        run._element.rPr.rFonts.set(
                            qn('w:eastAsia'), u'宋体')
                # 把表格移到占位段落之前（python-docx 只能加在文档末尾）
                p._element.addprevious(tbl._element)
            except Exception:
                pass
            plain.append(u'[表格]')
            continue
        # 标题在原文里被折成两行（"四湘赣边界的割据局面在" + "湘鄂赣三省的地位"），
        # 版面重建会产生两个同级的标题块；各自带"段前段后"就会多出
        # 四五十磅，整页溢出。上一行占了大半行宽、这一行又不以序号开头，
        # 就当同一个标题续行，并回上一段。
        if role in ('h1', 'h2', 'h3', 'h4') and prev_head \
                and prev_head['role'] == role \
                and abs(prev_head['src'] - (bsize or 0)) <= 0.4 \
                and not re.search(u'[。！？；：]$', prev_head['text']) \
                and not re.match(u'^[一二三四五六七八九十百０-９0-9（(第]', text) \
                and len(text) <= 30 and len(prev_head['text']) >= 6 \
                and _text_width(prev_head['text'], prev_head['size']) \
                >= 0.5 * text_w:
            _set_font(prev_head['para'].add_run(text),
                      size=max(8, prev_head['size'] * prev_head['k']),
                      ea=STYLE[role]['ea'])
            prev_head['text'] += text
            plain.append(text)
            continue
        if role == 'header' and pm_para is not None \
                and not (geom or {}).get('number_bottom'):
            # 原书页眉并进页首标注行，放在上边距里，不占正文高度；
            # 单独占一行的话每页要多挤十几磅，正文就排不回原书的行数。
            _set_font(pm_para.add_run(u'　' + text), size=7.5, color=GRAY)
            plain.append(text)
            continue
        cf = STYLE[role]
        _sty = _styles_for_block(text, style_maps, cur_page, style_cursors)
        p = doc.add_paragraph()
        pf = p.paragraph_format
        p.alignment = ALIGN[cf['align']]
        pf.space_before = Pt(cf['before'] * k * lk)
        pf.space_after = Pt(cf['after'] * k * lk)
        pf.line_spacing = Pt((body_ls * k if role == 'body' else cf['ls']) * lk)
        # 延续段不缩进——原书排版惯例（新段才缩进）
        if cf.get('ind') and not (role == 'body' and (is_cont or CONT.match(text.lstrip(u'　 ')))):
            pf.first_line_indent = Pt(cf['ind'] * k)
        if cf.get('left'):
            pf.left_indent = Pt(cf['left'] * k)
        if role == 'header':
            # 原书页眉：灰色小字居中，一眼与正文区分
            _set_font(p.add_run(text), size=8.5, color=GRAY, ea=u'宋体')
        elif role in ('h1', 'h2', 'h3', 'h4'):
            pf.keep_with_next = True
            if _sty:
                _emit_mask(p, text, mask, cf['ea'],
                           max(8, cf['size'] * k), None, False,
                           _sty, style_scale)
            else:
                _set_font(p.add_run(text), size=max(8, cf['size'] * k),
                          ea=cf['ea'])
            prev_head = {'role': role, 'para': p, 'size': cf['size'],
                         'src': bsize or 0, 'text': text, 'k': k}
        elif not verified:
            base = DARK if role == 'index' else None
            if role == 'body':
                _emit_body_maybe_runin(p, text, mask, u'宋体',
                                       body_size * k, base,
                                       styles=_sty, style_scale=style_scale)
            elif _sty:
                _emit_mask(p, text, mask, cf['ea'],
                           max(7, cf['size'] * k), base, False,
                           _sty, style_scale)
            else:
                _emit_mask(p, text, mask, cf['ea'], max(7, cf['size'] * k),
                           base, False)
        elif role == 'body':
            _emit_body_maybe_runin(p, text, mask, u'宋体',
                                   body_size * k, None,
                                   styles=_sty, style_scale=style_scale)
        else:
            if _sty:
                _emit_mask(p, text, mask, cf['ea'],
                           max(7, cf['size'] * k),
                           DARK if role in ('note', 'index') else None,
                           False, _sty, style_scale)
            else:
                _set_font(p.add_run(text), size=max(7, cf['size'] * k),
                          ea=cf['ea'],
                          color=DARK if role in ('note', 'index') else None)
        plain.append(text)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    doc.save(out_path)
    return dict(counts)


def _docx_to_pdf(docx_path, pdf_path):
    """用本机 Word 把 docx 转成 PDF（分页验收用）。"""
    import subprocess
    ps = ("$w=New-Object -ComObject Word.Application;$w.Visible=$false;"
          "$w.DisplayAlerts=0;$d=$w.Documents.Open('%s',$false,$true);"
          "$d.SaveAs([ref]'%s',[ref]17);$d.Close($false);$w.Quit()"
          % (docx_path, pdf_path))
    flags = 0x08000000 if os.name == 'nt' else 0
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   check=True, timeout=1800, creationflags=flags)


class PaginationResult(set):
    """page_count / marker_count / labels 附在 set 上，兼容旧的布尔/迭代用法。"""

    def __init__(self, over=(), page_count=0, marker_count=0, labels=None):
        super().__init__(over)
        self.page_count = int(page_count)
        self.marker_count = int(marker_count)
        self.labels = labels or []


def measure_pagination(docx_path):
    """验收：Word 实际排版后，每个“原书第 N 页”是否正好占一个 Word 页。

    返回超出（占了两页及以上）的页标序号（第几个页标 = 原书第几页）；
    用序号而不是页码，是因为前置页和正文可能各自从 1 编号，页码会重复。
    Word/PDF 环境不可用时返回 None。
    """
    import tempfile
    try:
        import pymupdf
    except Exception:
        return None
    fd, tmp = tempfile.mkstemp(suffix='.pdf')
    os.close(fd)
    try:
        # Word 偶尔会因为正在启动/弹窗导致第一次调用失败：重试一次，
        # 还失败就明确返回 None（由调用方提示用户），绝不假装核对过。
        for attempt in (0, 1):
            try:
                _docx_to_pdf(docx_path, tmp)
                break
            except Exception:
                if attempt:
                    raise
                time.sleep(3)
        doc = pymupdf.open(tmp)
        labels = []
        for pg in doc:
            # 页码可能是阿拉伯数字、中文数字，也可能是 ASCII 罗马数字
            # （ix / x / xi）。旧正则只认阿拉伯数字和 Unicode 罗马数字，
            # 前置页的 ASCII 罗马页码会让“第几个页标”整体错位，后面的
            # 压缩就会压错页，溢出页永远压不下去。
            m = re.search(u'原书第\\s*([^\\s]+?)\\s*页', pg.get_text())
            labels.append(m.group(1) if m else None)
        over = set()
        pos = [i for i, n in enumerate(labels) if n is not None]
        for k, i in enumerate(pos):
            end = pos[k + 1] if k + 1 < len(pos) else len(labels)
            if end - i > 1:
                over.add(k + 1)       # 第 k+1 个页标，即原书第 k+1 页
        return PaginationResult(over, doc.page_count, len(pos), labels)
    except Exception:
        return None
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
