# -*- coding: utf-8 -*-
"""书扫通 BookOCR 图形界面（向导式，面向不熟悉电脑操作的同学）。

三步：①选文件 → ②（一般不用改）→ ③点开始。完成后一键打开结果。
"""
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

STAGE_NAMES = [
    (u'检测文字层', u'读取文字'),
    (u'OCR', u'文字识别（三路交叉核对）'),
    (u'版面', u'整理版面'),
    (u'双栏', u'修复双栏页面'),
    (u'插图', u'安插插图'),
    (u'对齐', u'与权威文本核对'),
    (u'导出', u'生成 Word'),
]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(u'书扫通 BookOCR')
        self.geometry('700x700')
        self.minsize(640, 560)
        self.job = None
        self.thread = None
        self.q = queue.Queue()
        self.paused = False

        style = ttk.Style(self)
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('.', font=('Microsoft YaHei UI', 10))
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 17, 'bold'))
        style.configure('Sub.TLabel', font=('Microsoft YaHei UI', 9),
                        foreground='#777777')
        style.configure('Step.TLabel', font=('Microsoft YaHei UI', 11, 'bold'),
                        foreground='#1a6fbf')
        style.configure('TButton', padding=(10, 5))
        style.configure('Big.TButton', font=('Microsoft YaHei UI', 13, 'bold'),
                        padding=(24, 10))
        style.configure('Go.TButton', font=('Microsoft YaHei UI', 13, 'bold'),
                        padding=(24, 10), background='#2e8b57', foreground='white')
        style.map('Go.TButton',
                  background=[('active', '#266f46'), ('disabled', '#a9c9b6')])
        style.configure('Done.TLabel', font=('Microsoft YaHei UI', 12, 'bold'),
                        foreground='#2e8b57')

        pad = dict(padx=14, pady=4)
        # ------------------------------------------------------------ 头部
        head = ttk.Frame(self)
        head.pack(fill='x', **pad)
        ttk.Label(head, text=u'书扫通 BookOCR', style='Title.TLabel').pack(anchor='w')
        ttk.Label(head, text=u'大学生的学习辅助工具：把教材、经典著作、学术专著的扫描 PDF '
                              u'整理成干净、可编辑的 Word。',
                  style='Sub.TLabel').pack(anchor='w')
        ttk.Label(head, text=u'转简体、标出可疑字、按原书纸张与版心 1:1 分页。',
                  style='Sub.TLabel').pack(anchor='w')

        # ------------------------------------------------------ ① 选文件
        s1 = ttk.LabelFrame(self, text=u'①  选择 PDF 文件')
        s1.pack(fill='x', **pad)
        inner = ttk.Frame(s1)
        inner.pack(fill='x', padx=10, pady=8)
        self.pdf_var = tk.StringVar()
        ttk.Entry(inner, textvariable=self.pdf_var).pack(
            side='left', fill='x', expand=True)
        self.pick_btn = ttk.Button(inner, text=u'浏览…', style='Big.TButton',
                                   command=self.pick_pdf)
        self.pick_btn.pack(side='left', padx=(8, 0))

        row2 = ttk.Frame(s1)
        row2.pack(fill='x', padx=10, pady=(0, 4))
        ttk.Label(row2, text=u'标题（可不填）：').pack(side='left')
        self.title_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.title_var).pack(
            side='left', fill='x', expand=True)
        outrow = ttk.Frame(s1)
        outrow.pack(fill='x', padx=10, pady=(0, 8))
        ttk.Label(outrow, text=u'输出到：').pack(side='left')
        self.out_var = tk.StringVar()
        ttk.Entry(outrow, textvariable=self.out_var).pack(
            side='left', fill='x', expand=True)
        ttk.Button(outrow, text=u'浏览…', command=self.pick_out).pack(
            side='left', padx=(8, 0))
        prow = ttk.Frame(s1)
        prow.pack(fill='x', padx=10, pady=(0, 8))
        ttk.Label(prow, text=u'页码范围（可不填）：').pack(side='left')
        self.pages_var = tk.StringVar()
        ttk.Entry(prow, textvariable=self.pages_var, width=20).pack(side='left')
        ttk.Label(prow, text=u'例如 1-50、100-120,200；留空 = 整本',
                  style='Sub.TLabel').pack(side='left', padx=(8, 0))

        # ------------------------------------------------------ ② 选项
        s2 = ttk.LabelFrame(self, text=u'②  处理选项（一般不用改）')
        s2.pack(fill='x', **pad)
        opts = ttk.Frame(s2)
        opts.pack(fill='x', padx=10, pady=6)
        self.simp_var = tk.BooleanVar(value=True)
        self.twocol_var = tk.BooleanVar(value=True)
        self.simp_btn = self._make_toggle(opts, u'繁体转简体', self.simp_var)
        self.simp_btn.pack(side='left', padx=(0, 16))
        self.twocol_btn = self._make_toggle(opts, u'双栏页修复', self.twocol_var)
        self.twocol_btn.config(width=20)
        self.twocol_btn.pack(side='left', padx=(0, 4))
        self.twocol_note = ttk.Label(opts, text=u'（仅图书）',
                                     style='Sub.TLabel')
        self.twocol_note.pack(side='left', padx=(0, 12))
        ttk.Label(opts, text=u'文档类型：').pack(side='left')
        self.doctype_var = tk.StringVar(value=u'图书')
        self.doctype_box = ttk.Combobox(
            opts, textvariable=self.doctype_var, state='readonly', width=10,
            values=[u'图书', u'论文/期刊'])
        self.doctype_box.pack(side='left', padx=(0, 16))
        self.doctype_box.bind('<<ComboboxSelected>>', self._on_doctype)
        ttk.Label(opts, text=u'质量：').pack(side='left')
        self.quality_var = tk.StringVar(value=u'标准（快）')
        self.quality_box = ttk.Combobox(
            opts, textvariable=self.quality_var, state='readonly', width=14,
            values=[u'标准（快）', u'精细（慢·可标红）'])
        self.quality_box.pack(side='left', padx=(0, 16))
        ttk.Label(s2, text=u'质量说明：精细=三遍交叉核对+标红（慢，适合精读引用）；'
                           u'标准=一遍识别（快约 3 倍，不标红）。',
                  style='Sub.TLabel').pack(anchor='w', padx=10, pady=(0, 6))

        self.adv_visible = False
        self.adv_btn = ttk.Button(s2, text=u'高级设置 ▸', command=self.toggle_adv)
        self.adv_btn.pack(anchor='w', padx=10, pady=(0, 4))
        self.clear_btn = ttk.Button(s2, text=u'清空缓存（重新识别）',
                                    command=self.clear_cache)
        self.clear_btn.pack(anchor='w', padx=10, pady=(0, 6))
        self.adv = ttk.Frame(s2)
        self.adv_inner = ttk.Frame(self.adv)
        self.adv_inner.pack(fill='x', padx=10, pady=(0, 8))
        ttk.Label(self.adv_inner, text=u'权威文本核对：').grid(
            row=0, column=0, sticky='w')
        self.align_mode_var = tk.StringVar(value=u'不核对')
        self.align_box = ttk.Combobox(
            self.adv_inner, textvariable=self.align_mode_var, state='readonly',
            width=10, values=[u'不核对', u'手动选文件', u'自动搜索'])
        self.align_box.grid(row=0, column=1, sticky='w')
        self.align_box.bind('<<ComboboxSelected>>', self._on_align_mode)
        self.align_entry = ttk.Entry(self.adv_inner, width=40)
        self.align_entry.grid(row=1, column=0, columnspan=2, sticky='we',
                              pady=(4, 0))
        self.align_btn = ttk.Button(self.adv_inner, text=u'浏览…',
                                    command=self.pick_align)
        self.align_btn.grid(row=1, column=2, padx=(6, 0), pady=(4, 0))
        ttk.Label(self.adv_inner,
                  text=u'手动=选同一译本的 .txt；自动=联网搜索权威文本并自动对齐（经典著作成功率较高，'
                       u'依赖当前网络能否访问互联网）。',
                  style='Sub.TLabel').grid(row=2, column=0, columnspan=3,
                                           sticky='w')
        self.adv_inner.columnconfigure(0, weight=1)
        self._on_align_mode()
        self._on_doctype()

        # ------------------------------------------------------ ③ 开始
        s3 = ttk.Frame(self)
        s3.pack(fill='x', **pad)
        self.start_btn = ttk.Button(s3, text=u'③  开始整理', style='Go.TButton',
                                    command=self.start)
        self.start_btn.pack(side='left')
        self.stop_btn = ttk.Button(s3, text=u'暂停', style='Big.TButton',
                                   command=self.pause, state='disabled')
        self.stop_btn.pack(side='left', padx=(10, 0))
        ttk.Label(s3, text=u'完成后会自动检验页间衔接并写入下方日志',
                  style='Sub.TLabel').pack(side='left', padx=(20, 0))

        # ------------------------------------------------------ 进度
        prog = ttk.Frame(self)
        prog.pack(fill='x', **pad)
        # 滚动动画条：只表示"正在干活"，不显示百分比/倒计时（盯进度徒增焦虑）
        self.prog = ttk.Progressbar(prog, mode='indeterminate', maximum=100)
        self.prog.pack(fill='x')
        self.status = tk.StringVar(value=u'等待开始')
        ttk.Label(prog, textvariable=self.status).pack(anchor='w', pady=(2, 0))

        # ------------------------------------------------------ 结果
        self.done = ttk.Frame(self)
        self.done_lbl = ttk.Label(self.done, style='Done.TLabel')
        self.open_btn = ttk.Button(self.done, text=u'打开整理好的 Word',
                                   style='Big.TButton', command=self.open_result)

        # ------------------------------------------------------ 日志
        log = ttk.LabelFrame(self, text=u'详细进度（可折叠）')
        log.pack(fill='both', expand=True, **pad)
        self.logbox = tk.Text(log, height=7, font=('Consolas', 9), state='disabled',
                              wrap='none')
        self.logbox.pack(fill='both', expand=True, padx=6, pady=4)

        self.after(100, self.poll)

    def _make_toggle(self, parent, name, var, on=u'开', off=u'关'):
        """开/关切换按钮（不用勾选框——部分系统上勾号渲染成叉号，容易误解）。"""
        btn = ttk.Button(parent, width=18)

        def refresh():
            btn.config(text=u'%s：%s' % (name, on if var.get() else off))

        def flip():
            var.set(not var.get())
            refresh()

        btn.config(command=flip)
        btn._refresh = refresh
        refresh()
        return btn

    # ------------------------------------------------------------ 交互
    def toggle_adv(self):
        if self.adv_visible:
            self.adv.pack_forget()
            self.adv_btn.config(text=u'高级设置 ▸')
        else:
            self.adv.pack(fill='x')
            self.adv_btn.config(text=u'高级设置 ▾')
        self.adv_visible = not self.adv_visible

    def pick_pdf(self):
        f = filedialog.askopenfilename(filetypes=[(u'PDF 文件', '*.pdf')])
        if f:
            self.pdf_var.set(f)
            base = os.path.splitext(os.path.basename(f))[0]
            if not self.title_var.get() or re_uuid(self.title_var.get()):
                self.title_var.set(base)
            if not self.out_var.get():
                self.out_var.set(os.path.splitext(f)[0] + u'_整理版.docx')

    def pick_out(self):
        f = filedialog.asksaveasfilename(
            defaultextension='.docx',
            initialfile=self.out_var.get() or u'整理版.docx',
            filetypes=[(u'Word 文件', '*.docx')])
        if f:
            self.out_var.set(f)

    def pick_align(self):
        f = filedialog.askopenfilename(filetypes=[(u'文本文件', '*.txt')])
        if f:
            self.align_entry.delete(0, 'end')
            self.align_entry.insert(0, f)

    def _on_align_mode(self, event=None):
        """核对方式变化时，仅"手动选文件"启用文件输入框。"""
        st = 'normal' if self.align_mode_var.get() == u'手动选文件' else 'disabled'
        self.align_entry.config(state=st)
        self.align_btn.config(state=st)

    def _on_doctype(self, event=None):
        """论文模式走版面树，双栏修复只属于图书模式。"""
        paper = u'论文' in self.doctype_var.get()
        if paper:
            self.twocol_var.set(False)
            self.twocol_btn.config(state='disabled')
            self.twocol_note.config(text=u'（论文模式不使用）')
        else:
            self.twocol_btn.config(state='normal')
            self.twocol_note.config(text=u'（仅图书）')
        try:
            self.twocol_btn._refresh()
        except Exception:
            pass

    def open_result(self):
        if os.path.exists(self.out_path):
            os.startfile(self.out_path)  # noqa
        else:
            messagebox.showinfo(u'提示', u'结果文件还没有生成')

    # ------------------------------------------------------------ 运行
    def clear_cache(self):
        """删除当前输出位置旁的识别缓存，下次从头识别。"""
        pdf = self.pdf_var.get().strip()
        out = self.out_var.get().strip()
        if not out and pdf:
            out = os.path.splitext(pdf)[0] + u'_整理版.docx'
        if not out:
            messagebox.showinfo(u'清空缓存', u'请先选择 PDF 文件。')
            return
        work = os.path.join(os.path.dirname(os.path.abspath(out)),
                            '_bookocr_工作目录')
        if not os.path.isdir(work):
            messagebox.showinfo(u'清空缓存', u'这个输出位置还没有识别缓存。')
            return
        if not messagebox.askyesno(
                u'清空缓存',
                u'确定删除识别缓存吗？\n\n%s\n\n删除后这本书需要重新识别。'
                % work):
            return
        import shutil
        shutil.rmtree(work, ignore_errors=True)
        self.status.set(u'缓存已清空，下次点“开始整理”会重新识别。')

    def start(self):
        pdf = self.pdf_var.get().strip()
        if not pdf or not os.path.exists(pdf):
            messagebox.showerror(u'还没选文件',
                                 u'请先点“浏览…”选择要整理的 PDF 文件。')
            return
        out = self.out_var.get().strip() \
            or os.path.splitext(pdf)[0] + u'_整理版.docx'
        if not out.lower().endswith('.docx'):
            out += '.docx'
        # 页码范围（可不填）：只整理其中若干页，适合先抽查一段
        from . import parse_pages
        pages = parse_pages(self.pages_var.get())
        if pages is None:
            messagebox.showerror(
                u'页码范围',
                u'格式不对。可以这样填：\n1-50\n3\n100-120,200')
            return
        if pages:
            import pymupdf
            try:
                with pymupdf.open(pdf) as _doc:
                    total = _doc.page_count
            except Exception:
                total = 0
            pages = {p for p in pages if 1 <= p <= total}
            if not pages:
                messagebox.showerror(
                    u'页码范围',
                    u'没有落在 1–%d 页之间的页码。' % total)
                return
            # 抽查用的输出别覆盖整本结果：自动加"第X-Y页"
            base_out = os.path.splitext(pdf)[0] + u'_整理版.docx'
            if os.path.abspath(out) == os.path.abspath(base_out):
                out = (os.path.splitext(pdf)[0]
                       + u'_第%d-%d页_整理版.docx' % (min(pages), max(pages)))
        self.out_var.set(out)
        self.out_path = out
        work = os.path.join(os.path.dirname(os.path.abspath(out)),
                            '_bookocr_工作目录')
        _paper = u'论文' in self.doctype_var.get()
        opts = {
            'two_column': (not _paper) and self.twocol_var.get(),
            'align_mode': (u'manual' if self.align_mode_var.get() == u'手动选文件'
                           else u'auto' if self.align_mode_var.get() == u'自动搜索'
                           else u'off'),
            'align_file': self.align_entry.get().strip(),
            'title': self.title_var.get().strip(),
            'variants': 3 if u'精细' in self.quality_var.get() else 1,
            'layout_model': True,
            'mode': 'paper' if _paper else 'book',
        }
        if pages:
            opts['pages'] = pages
        self.status.set(u'正在准备…')
        # 点"开始"这一刻进度条就滚起来；识别引擎的加载也放后台线程，
        # 否则加载的十几秒会卡住界面、动画冻结
        self.prog.config(mode='indeterminate')
        self.prog.start(12)
        self.update_idletasks()
        self._args = (pdf, self.out_path, work, opts)
        self.job = None               # 旧的暂停任务作废，等后台建新任务
        self.paused = False
        self.start_btn.config(state='disabled')
        self.stop_btn.config(state='normal')
        self.pick_btn.config(state='disabled')
        self.done.pack_forget()
        self._clear_log()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            pdf, out, work, opts = self._args
            from . import job          # 重库延迟导入放后台，主窗口不卡
            self.job = job.Job(pdf, out, work, opts)
            ok = self.job.run(log=self._log, progress=self._progress)
            self.q.put(('done', ok))
        except Exception as e:
            import traceback
            self._log(u'[错误] ' + traceback.format_exc())
            self.q.put(('fail', str(e)))

    def pause(self):
        """暂停：当前页完成后停止，进度已保存；再点“开始整理”即可续跑。"""
        if self.job:
            self.job.stop_evt.set()
            self.stop_btn.config(state='disabled')
            self.status.set(u'正在暂停…（当前页完成后停止，进度已保存，点“开始整理”即可续跑）')
        else:
            self.status.set(u'识别引擎还在启动，稍等一下…')

    # ------------------------------------------------------------ 回调
    def _log(self, msg):
        self.q.put(('log', msg))
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   '..', u'运行日志.txt'), 'a', encoding='utf-8') as f:
                f.write('%s  %s\n' % (time.strftime('%m-%d %H:%M:%S'), msg))
        except Exception:
            pass

    def _progress(self, done, total):
        self.q.put(('prog', done, total))

    def _clear_log(self):
        self.logbox.config(state='normal')
        self.logbox.delete('1.0', 'end')
        self.logbox.config(state='disabled')

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == 'log':
                    self.logbox.config(state='normal')
                    self.logbox.insert('end', msg[1] + '\n')
                    self.logbox.see('end')
                    self.logbox.config(state='disabled')
                    for key, name in STAGE_NAMES:
                        if key in msg[1]:
                            self.status.set(name + u'…')
                            break
                elif msg[0] == 'prog':
                    # 只报"已存好多少页"，不报进度百分比、不报剩余时间
                    self.status.set(
                        u'正在识别，已保存 %d 页。不用盯着——随时可以关掉窗口，'
                        u'下次打开会自动接着跑。' % msg[1])
                elif msg[0] == 'done':
                    self._finish(msg[1])
                elif msg[0] == 'fail':
                    self._finish(False)
                    messagebox.showerror(u'出错了', msg[1])
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def _finish(self, ok):
        self.prog.stop()
        self.prog.config(mode='determinate', value=100)
        self.start_btn.config(state='normal')
        self.stop_btn.config(state='disabled')
        self.stop_btn.config(text=u'暂停')
        self.pick_btn.config(state='normal')
        if ok:
            self.status.set(u'完成！')
            self.done_lbl.config(text=u'✅ 整理完成：%s' % self.out_path)
            self.done_lbl.pack(side='left', padx=(14, 10), pady=8)
            self.open_btn.pack(side='left', pady=8)
            self.done.pack(fill='x')
        else:
            self.status.set(u'已暂停。下次点“开始整理”会自动接着上次的进度。')


def re_uuid(s):
    import re
    return re.match(u'^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$', s) is not None
