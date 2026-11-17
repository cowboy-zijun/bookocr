# -*- coding: utf-8 -*-
"""Human review ledger — no AI, no database.

The Word exporter is not allowed to silently rewrite a document.  Ambiguous
things (missing source lines, low-confidence blocks, page-image fallback) are
written to a sidecar proposal list with page/bbox/evidence.  A human can
accept, reject or edit each item; only accepted/edited values are applied on
the next run.
"""
from __future__ import annotations

import html
import json
import os

try:
    import pymupdf
except Exception:  # pragma: no cover
    pymupdf = None


REVIEW_VERSION = 1


def _crop(pdf_path, page_no, bbox, assets_dir, name):
    if pymupdf is None or not pdf_path or not os.path.exists(pdf_path):
        return ''
    try:
        doc = pymupdf.open(pdf_path)
        pg = doc[page_no - 1]
        scale = pg.rect.width / 3400.0
        r = pymupdf.Rect(0, 0, pg.rect.width, pg.rect.height)
        if bbox and len(bbox) == 4:
            r = pymupdf.Rect(bbox[0] * scale, bbox[1] * scale,
                             bbox[2] * scale, bbox[3] * scale)
            r = r & pg.rect
        if r.is_empty or r.width < 2 or r.height < 2:
            r = pg.rect
        os.makedirs(assets_dir, exist_ok=True)
        path = os.path.join(assets_dir, name + '.png')
        pg.get_pixmap(dpi=140, clip=r).save(path)
        doc.close()
        return path
    except Exception:
        return ''


def build_review(ir, pdf_path, out_dir, max_items=200):
    """Turn IR evidence problems into human-review proposals."""
    assets = os.path.join(out_dir, 'review_assets')
    items = []
    for p in ir.pages:
        for i, m in enumerate(p.missing or []):
            items.append({
                'id': 'p%dm%d' % (p.no, i + 1),
                'kind': 'missing_line',
                'page': p.no,
                'bbox': m.get('bbox'),
                'proposed': m.get('text') or '',
                'confidence': 0.0,
                'reason': m.get('reason') or 'source line not represented by a block',
                'status': 'pending',
                'human_value': None,
            })
        for b in p.blocks:
            if b.fallback == 'page_image':
                items.append({
                    'id': b.id + '_fb',
                    'kind': 'page_fallback',
                    'page': p.no,
                    'bbox': b.bbox,
                    'proposed': '整页截图（page_image）',
                    'confidence': 0.0,
                    'reason': '该页内容未完全转为可编辑文字/对象',
                    'status': 'pending',
                    'human_value': None,
                })
            elif b.type in ('paragraph', 'heading') and b.confidence < 0.8:
                items.append({
                    'id': b.id,
                    'kind': 'low_conf_block',
                    'page': p.no,
                    'bbox': b.bbox,
                    'proposed': b.text(),
                    'confidence': b.confidence,
                    'reason': '低置信度 block（OCR/样式/bbox 可疑）',
                    'status': 'pending',
                    'human_value': None,
                })
        if len(items) >= max_items:
            break
    items = items[:max_items]
    for it in items:
        name = it['id'].replace('/', '_')
        path = _crop(pdf_path, it['page'], it.get('bbox'), assets, name)
        it['asset'] = os.path.relpath(path, out_dir).replace('\\', '/') if path else ''
    return {
        'review_version': REVIEW_VERSION,
        'source': os.path.basename(pdf_path or ''),
        'items': items,
    }


def _write_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def write_review(review, out_dir):
    """Write review.json + self-contained review.html; return paths."""
    os.makedirs(out_dir, exist_ok=True)
    jp = os.path.join(out_dir, 'review.json')
    hp = os.path.join(out_dir, 'review.html')
    _write_json(jp, review)
    title = html.escape('书扫通 · 人工复核')
    cards = []
    for it in review.get('items') or []:
        st = it.get('status') or 'pending'
        img = ''
        if it.get('asset'):
            img = '<img src="%s" alt="page crop">' % html.escape(it['asset'])
        cards.append('''
<div class="item" data-id="%s" data-kind="%s">
  <div class="crop">%s</div>
  <div class="body">
    <div class="meta">第 %s 页 · %s · confidence=%.2f</div>
    <div class="proposed">%s</div>
    <div class="reason">%s</div>
    <div class="actions">
      <button data-act="accepted">接受</button>
      <button data-act="rejected">拒绝</button>
      <input class="edit" placeholder="人工修正值（可选）">
      <button data-act="edited">采纳修正</button>
    </div>
    <div class="status">%s</div>
  </div>
</div>''' % (
            html.escape(it['id']), html.escape(it.get('kind') or ''),
            img, it.get('page'), html.escape(it.get('kind') or ''),
            float(it.get('confidence') or 0),
            html.escape((it.get('proposed') or '')[:800]),
            html.escape(it.get('reason') or ''),
            html.escape(st)))
    page = '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s</title>
<style>
body{font-family:"Microsoft YaHei",sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem}
.item{display:flex;gap:1rem;border:1px solid #ddd;padding:1rem;margin:1rem 0}
.crop img{max-width:280px;max-height:340px;border:1px solid #eee}
.body{flex:1}.meta{color:#888;font-size:.85rem}
.proposed{white-space:pre-wrap;margin:.5rem 0;font-size:1.02rem}
.reason{color:#a60;font-size:.86rem;margin-bottom:.6rem}
.actions{display:flex;gap:.5rem;flex-wrap:wrap}
button{padding:.35rem .8rem;cursor:pointer}
.edit{min-width:260px;padding:.35rem}
.status{color:#246;font-size:.85rem;margin-top:.4rem}
#out{width:100%%;height:8rem;margin-top:1rem}
</style></head><body>
<h1>%s</h1>
<p>逐条看原图，决定接受/拒绝/修正。导出的 review.json 交给命令行
<code>--review</code>；只采纳已决定项，未决定项保持 pending。</p>
<div id="items">%s</div>
<button id="export">导出 review.json</button>
<textarea id="out" placeholder="这里会出现 review.json"></textarea>
<script>
const items=[];
document.querySelectorAll('.item').forEach(el=>{
  const id=el.dataset.id, kind=el.dataset.kind;
  const st=el.querySelector('.status');
  const edit=el.querySelector('.edit');
  items.push({id,kind,status:'pending',human_value:null});
  const cur=()=>items.find(x=>x.id===id);
  el.querySelectorAll('button').forEach(b=>b.onclick=()=>{
    const act=b.dataset.act; cur().status=act;
    cur().human_value=(act==='edited')?(edit.value||null):null;
    st.textContent='已选择：'+act+((cur().human_value)?('：'+cur().human_value):'');
  });
});
document.getElementById('export').onclick=()=>{
  const data={review_version:1,source:%s,items};
  const txt=JSON.stringify(data,null,2);
  document.getElementById('out').value=txt;
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([txt],{type:'application/json'}));
  a.download='review.json'; a.click();
};
</script></body></html>''' % (title, title, ''.join(cards),
                              json.dumps(review.get('source') or '',
                                         ensure_ascii=False))
    with open(hp, 'w', encoding='utf-8') as f:
        f.write(page)
    return jp, hp


def load_decisions(path):
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    items = data.get('items') if isinstance(data, dict) else data
    out = []
    for it in items or []:
        if it.get('status') in ('accepted', 'edited'):
            out.append(it)
    return out


def _text_block(text, bbox):
    # Same shape as layout/PageDoc body blocks; bbox is kept as evidence.
    return ('body', text, [False] * len(text), None, 0, None, None, bbox)


def apply_to_pagebook(pb, review_path):
    """Apply accepted/edited values to PageDoc blocks before Word export.

    Direct pNbK ids map to PageDoc blocks.  missing_line items have no block,
    so accepted text is appended to that page with its bbox (explicitly marked
    in the returned log, never silent).
    """
    items = load_decisions(review_path)
    applied, skipped = [], []
    for it in items:
        sid = it.get('id') or ''
        val = it.get('human_value')
        if it.get('kind') == 'missing_line':
            if it.get('status') == 'accepted' and val is None:
                val = it.get('proposed')
            if not val:
                skipped.append((sid, 'missing_line 无文本'))
                continue
            pg = pb.page(int(it.get('page') or 0))
            if pg is None:
                skipped.append((sid, '页不存在'))
                continue
            pg.blocks.append(_text_block(val, it.get('bbox')))
            applied.append((sid, 'missing_line -> block'))
            continue
        if it.get('kind') == 'low_conf_block':
            if it.get('status') == 'accepted' and val is None:
                applied.append((sid, '确认原值'))
                continue
            if not val:
                skipped.append((sid, '无修正值'))
                continue
            import re
            m = re.match(r'^p(\d+)b(\d+)$', sid)
            if not m:
                skipped.append((sid, '非普通 block id（论文树/对象）'))
                continue
            pg = pb.page(int(m.group(1)))
            if pg is None:
                skipped.append((sid, '页不存在'))
                continue
            idx = int(m.group(2)) - 1
            if idx < 0 or idx >= len(pg.blocks):
                skipped.append((sid, 'block 越界'))
                continue
            b = pg.blocks[idx]
            b = list(b)
            b[1] = str(val)
            b[2] = [False] * len(str(val))
            pg.blocks[idx] = tuple(b)
            applied.append((sid, '低置信 block 已替换'))
            continue
        if it.get('kind') == 'page_fallback':
            if it.get('status') == 'rejected':
                skipped.append((sid, '拒绝整页截图：当前导出器尚未去掉该页图'))
            else:
                applied.append((sid, '保留整页截图（已人工确认）'))
            continue
    return applied, skipped
