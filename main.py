# -*- coding: utf-8 -*-
"""书扫通 BookOCR 入口：默认打开图形界面；--cli 走命令行。

启动出错时把详情写入 启动错误日志.txt（界面模式下无控制台，必须落盘）。
"""
import os
import sys

# 桌面快捷方式用"真 pythonw"（uv 基础解释器）启动：它不带本项目 .venv
# 的第三方包，这里手动挂载，否则点了"开始"才报缺库（如 rapidfuzz）。
_here = os.path.dirname(os.path.abspath(__file__))
_sp = os.path.join(_here, '.venv', 'Lib', 'site-packages')
if os.path.isdir(_sp):
    if _sp not in sys.path:
        sys.path.insert(0, _sp)
    if hasattr(os, 'add_dll_directory'):
        for _d in ('', 'cv2', 'onnxruntime', 'onnxruntime\\capi',
                   'rapidfuzz', 'pymupdf'):
            _dd = os.path.join(_sp, _d) if _d else _sp
            if os.path.isdir(_dd):
                try:
                    os.add_dll_directory(_dd)
                except OSError:
                    pass

import multiprocessing as mp


def main():
    if '--cli' in sys.argv:
        # cli.py 自己不认识 --cli；它是 main.py 的分流开关，先摘掉。
        sys.argv.remove('--cli')
        import cli
        cli.main()
    else:
        from bookocr.gui import App
        App().mainloop()


if __name__ == '__main__':
    mp.freeze_support()
    try:
        main()
    except Exception:
        import traceback
        log = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           u'启动错误日志.txt')
        with open(log, 'w', encoding='utf-8') as f:
            traceback.print_exc(file=f)
        try:
            from tkinter import messagebox
            messagebox.showerror(u'书扫通启动失败',
                                 u'启动出错，详情已写入：\n%s' % log)
        except Exception:
            pass
