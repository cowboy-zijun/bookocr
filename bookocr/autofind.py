# -*- coding: utf-8 -*-
"""自动搜索权威译本。

从 OCR 文本里取几个"独特句"作为探针，用搜索引擎的精确短语查询找到
候选页面，下载、抽取正文，再按 n-gram 覆盖率打分，挑最好的作为权威
文本用于后续对齐。全程不需要任何 API 密钥；网络不可用时友好退出。
"""
import html
import re
import urllib.parse
import urllib.request

import time

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) BookOCR/1.0'
TIMEOUT = 8
BUDGET = 60.0          # 自动搜索的总时间预算（秒），超时即放弃

_TAG = re.compile(r'<(script|style)[^>]*>.*?</\1>', re.S | re.I)
_ANY = re.compile(r'<[^>]+>')
_WS = re.compile(r'\s+')
_NORM = re.compile(u'[^\\u3400-\\u9fffA-Za-z0-9]')


def norm(s):
    return _NORM.sub('', s)


def _fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read()
    for enc in ('utf-8', 'gb18030', 'big5'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace')


def _clean_html(s):
    s = _TAG.sub(' ', s)
    s = _ANY.sub(' ', s)
    s = html.unescape(s)
    return _WS.sub(' ', s)


def _search_links(probe):
    q = urllib.parse.quote(u'"%s"' % probe)
    for engine in (
        'https://www.bing.com/search?q=%s&setlang=zh-hans' % q,
        'https://www.baidu.com/s?wd=%s' % q,
    ):
        try:
            s = _fetch(engine)
        except Exception:
            continue
        links = re.findall(r'href="(https?://[^"]+)"', s)
        out = []
        for u in links:
            u = html.unescape(u)
            if any(x in u.lower() for x in
                   ('bing.com', 'baidu.com', 'microsoft', 'msn.com')):
                continue
            if u not in out:
                out.append(u)
        if out:
            return out
    return []


def find_authoritative_text(samples, log=None):
    """samples: 若干 OCR 正文片段（越长越有代表性）。
    返回 (text, source_url, score)；找不到时返回 (None, None, 0)。"""
    def say(m):
        if log:
            log(m)

    say(u'[自动搜索] 从已识别正文中挑选探针…')
    t0 = time.time()

    def over_budget():
        return time.time() - t0 > BUDGET

    probes = []
    for s in sorted(samples, key=len, reverse=True):
        s = s.strip()
        if len(s) >= 24 and s[:60] not in probes:
            probes.append(s[:60])
        if len(probes) >= 3:
            break
    if not probes:
        say(u'[自动搜索] 没有足够长的正文做探针，跳过核对')
        return None, None, 0

    n_samples = set()
    for s in samples:
        ns = norm(s)
        if len(ns) >= 15:
            for i in range(0, len(ns) - 15, 15):
                n_samples.add(ns[i:i + 15])
    if len(n_samples) < 5:
        return None, None, 0

    candidates = []
    for p in probes:
        if over_budget():
            break
        try:
            links = _search_links(p)
        except Exception as e:
            say(u'[自动搜索] 搜索失败：%s' % e)
            continue
        if links:
            say(u'[自动搜索] 探针「%s…」搜到 %d 个候选页面' % (p[:16], len(links)))
        candidates.extend(links)
        if len(candidates) >= 8:
            break
    if not candidates:
        say(u'[自动搜索] 没搜到任何候选页面（当前网络可能无法访问互联网，'
            u'或已超时）。可改用"手动选文件"。')
        return None, None, 0

    best = (None, None, 0.0)
    seen = set()
    for u in candidates[:8]:
        if over_budget():
            say(u'[自动搜索] 超过时间预算，改用已找到的最佳候选')
            break
        if u in seen or not u.lower().startswith('http'):
            continue
        seen.add(u)
        try:
            text = _clean_html(_fetch(u))
        except Exception as e:
            say(u'[自动搜索] 下载失败：%s（%s）' % (u[:60], e))
            continue
        nt = norm(text)
        if len(nt) < 500:
            continue
        hit = sum(1 for g in n_samples if g in nt)
        score = hit / float(len(n_samples))
        say(u'[自动搜索] 候选 %s 覆盖率 %.0f%%（%d/%d 个特征片段命中）'
            % (u[:70], 100 * score, hit, len(n_samples)))
        if score > best[2]:
            best = (text, u, score)
    if best[0] is None or best[2] < 0.5:
        say(u'[自动搜索] 候选页面质量不足（最高覆盖率 %.0f%%），跳过核对'
            % (100 * best[2]))
        return None, None, 0
    say(u'[自动搜索] 选用 %s（覆盖率 %.0f%%）' % (best[1][:80], 100 * best[2]))
    return best
