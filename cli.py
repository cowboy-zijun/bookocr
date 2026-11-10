# -*- coding: utf-8 -*-
"""书扫通 BookOCR 命令行入口。

示例：
    python cli.py --pdf 书.pdf
    python cli.py --pdf 书.pdf --out 结果.docx --align-file 权威文本.txt
"""
import argparse
import multiprocessing as mp
import os

from bookocr import job


def main():
    ap = argparse.ArgumentParser(
        description=u'书扫通 BookOCR —— 扫描图书整理工具（面向大学生的学习辅助）')
    ap.add_argument('--pdf', help=u'扫描版 PDF 路径')
    ap.add_argument('--out', help=u'输出 Word 路径（默认与 PDF 同名 + _整理版.docx）')
    ap.add_argument('--align-file', help=u'同一译本的权威电子文本 .txt（可选）')
    ap.add_argument('--align-auto', action='store_true',
                    help=u'自动联网搜索权威译本并对齐')
    ap.add_argument('--no-two-column', action='store_true',
                    help=u'关闭双栏纠错（仅图书模式）')
    ap.add_argument('--workers', type=int, default=None,
                    help=u'并行进程数（默认按本机 CPU/内存自动选择）')
    ap.add_argument('--pages',
                    help=u'页码范围，如 1-50、3、100-120,200；不填=整本')
    ap.add_argument('--mode', choices=['book', 'paper'], default='book',
                    help=u'文档类型：book=图书（默认），paper=论文/期刊')
    ap.add_argument('--title', help=u'文档标题（默认取 PDF 元数据或文件名）')
    ap.add_argument('--fast', action='store_true',
                    help=u'快速模式：单遍识别（约 3 倍速，不标红）')
    ap.add_argument('--check', metavar='DOCX',
                    help=u'只做页间接缝检验（句子拆断/句首标点/多余空格）')
    ap.add_argument('--fidelity', action='store_true',
                    help=u'导出后用 Word 实排结果做逐页保真度量（较慢）')
    ap.add_argument('--layout-lock', action='store_true',
                    help=u'试验：文字层纯正文页按原书视觉行锁定断行（L3）')
    args = ap.parse_args()

    if args.check:
        from bookocr import checker
        ok = checker.check_docx(args.check)
        raise SystemExit(0 if ok else 1)

    if not args.pdf:
        ap.error(u'请用 --pdf 指定输入文件，或 --check 指定要检验的 docx')

    pdf = args.pdf
    out = args.out or os.path.splitext(pdf)[0] + u'_整理版.docx'
    work = os.path.join(os.path.dirname(out) or '.', '_bookocr_工作目录')
    opts = {
        'two_column': (args.mode != 'paper') and not args.no_two_column,
        'align_mode': (u'auto' if args.align_auto
                       else u'manual' if args.align_file else u'off'),
        'align_file': args.align_file or '',
        'workers': args.workers,
        'title': args.title or '',
        'variants': 1 if args.fast else 3,
        'layout_model': True,
        'layout_lock': bool(args.layout_lock),
        'mode': args.mode,
    }
    if args.pages:
        import pymupdf
        from bookocr import parse_pages
        with pymupdf.open(pdf) as _doc:
            total = _doc.page_count
        sel = parse_pages(args.pages)
        if sel is None:
            ap.error(u'页码范围格式不对，示例：1-50 或 3 或 100-120,200')
        sel = {p for p in sel if 1 <= p <= total}
        if not sel:
            ap.error(u'页码范围超出总页数（这本书共 %d 页）' % total)
        opts['pages'] = sel
    j = job.Job(pdf, out, work, opts)
    print(u'开始处理：%s' % pdf)
    print(u'（Ctrl+C 可中断，已完成的页会自动保存，重跑续传）')
    try:
        j.run(log=lambda m: print(m),
              progress=lambda d, t: print(u'  进度 %d / %d 页' % (d, t), end='\r'))
    except KeyboardInterrupt:
        j.stop_evt.set()
        print(u'\n已中断：进度已保存。再次运行同一命令即可续跑。')
    print()
    if args.fidelity:
        from bookocr import fidelity
        fidelity.compare(pdf, out, log=lambda m: print(m),
                         src_pages=opts.get('pages'))


if __name__ == '__main__':
    mp.freeze_support()
    main()
