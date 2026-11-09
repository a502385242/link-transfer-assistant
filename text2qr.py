# -*- coding: utf-8 -*-
"""林克传输助手 —— 电脑和手机之间双向传东西。

电脑 → 手机：
  短文本（<= 600 字节）→ 直接编码进二维码，手机离线可读，不依赖网络
  长文本              → 局域网链接，手机打开网页复制
  图片                → 局域网链接，手机打开后长按保存原图
  其他文件            → 局域网链接，手机点一下下载原文件

手机 → 电脑：
  扫码打开接收页，发文字 / 照片 / 任意文件，按日期存进指定文件夹。

图片和文件体积远超二维码容量（上限约 2.9 KB），只能走局域网；
短文本这一条路径刻意保持不联网，因此不启动任何服务。
"""

import ctypes
import mimetypes
import os
import queue
import secrets
import subprocess
import tempfile
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import qrcode
from PIL import Image, ImageDraw, ImageFont, ImageGrab, ImageTk
from qrcode.constants import ERROR_CORRECT_M

import config as cfg
import dragdrop
from share_server import (Item, Received, ShareServer, human_size, item_from_path,
                          lan_ips)

APP_TITLE = "林克传输助手"
DIRECT_LIMIT = 600      # 超过这个字节数就不再塞进二维码，格子会密到扫不出来
QR_MAX_PX = 420

BG = "#fafafa"
CARD = "#ffffff"
LINE = "#e2e2e2"
DIM = "#8a8a8a"
BODY = "#5a5a5a"
ERR = "#c0392b"
LINK = "#2563eb"

UI_FONT = ("Microsoft YaHei UI", 10)
HINT_FONT = ("Microsoft YaHei UI", 9)

_RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS


# ---------------------------------------------------------------- 临时目录


def temp_dir() -> str:
    """程序自己的临时目录，固定挂在系统临时目录下。

    故意不在代码里写死 C:\\Users\\...：tempfile 每次运行都向系统问一次，
    换电脑、换用户名、换盘符都会自动指向那台机器的正确位置。
    """
    return os.path.join(tempfile.gettempdir(), "text2qr")


def ensure_temp_dir() -> str:
    d = temp_dir()
    os.makedirs(d, exist_ok=True)
    return d


def purge_temp() -> None:
    """只清 text2qr 这一层子目录，不向上、不用通配符，绝不碰系统临时目录里的别的文件。"""
    d = temp_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return
    for name in names:
        p = os.path.join(d, name)
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass


# ---------------------------------------------------------------- 小工具


def resource_path(name: str) -> str:
    """兼容源码运行与 PyInstaller 打包后的资源路径。"""
    base = getattr(__import__("sys"), "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def shorten(name: str, limit: int = 20) -> str:
    """文件名太长会把卡片撑破，中间省略但保住扩展名。"""
    if len(name) <= limit:
        return name
    base, ext = os.path.splitext(name)
    keep = max(6, limit - len(ext) - 1)
    return base[:keep] + "…" + ext


def make_qr_image(data: str, px: int = QR_MAX_PX) -> Image.Image:
    """整数倍缩放，保证每个格子边界锐利——模糊的二维码手机很难识别。"""
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    total_modules = qr.modules_count + qr.border * 2
    qr.box_size = max(3, min(14, px // total_modules))
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def make_thumb(item: Item, size: int = 64) -> Image.Image:
    """图片给真缩略图，其他文件画一个扩展名方块。"""
    if item.kind == "image":
        try:
            with Image.open(item.path) as im:
                im = im.convert("RGB")
                im.thumbnail((size, size), _RESAMPLE)
                canvas = Image.new("RGB", (size, size), "#f2f2f2")
                canvas.paste(im, ((size - im.width) // 2, (size - im.height) // 2))
                return canvas
        except Exception:
            pass  # 读不出来的图（损坏、格式怪异）退化成方块

    canvas = Image.new("RGB", (size, size), "#f2f2f2")
    draw = ImageDraw.Draw(canvas)
    ext = (os.path.splitext(item.name)[1].lstrip(".") or "FILE").upper()[:4]
    try:
        font = ImageFont.truetype("segoeui.ttf", 14)
    except Exception:
        font = ImageFont.load_default()
    left, top, right, bottom = draw.textbbox((0, 0), ext, font=font)
    draw.text(((size - (right - left)) / 2 - left,
               (size - (bottom - top)) / 2 - top),
              ext, fill="#8a8a8a", font=font)
    return canvas


# ---------------------------------------------------------------- 界面


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.server = ShareServer()
        self.item: Item | None = None      # 图片/文件模式下当前内容
        self._photo = None
        self._thumb = None
        self._pending = None
        self._ips = lan_ips()
        self._ip_index = 0
        self._extra = 0                    # 被忽略的多余文件数
        self._drop = None

        self._page = "send"                # 当前是哪个页：send / recv
        self.save_dir = cfg.load()["save_dir"]
        self.inbox_q: queue.Queue = queue.Queue()   # 服务线程 → 主线程的唯一通道
        self._received: list = []
        self._row_photos: list = []        # 行内缩略图必须留引用，否则被 GC 掉变空白
        self._photo_recv = None

        self._setup_window()
        self._build()
        self._hook_drop()
        self._draw_placeholder()
        self._poll_inbox()                 # 一直在跑，接收页没打开时也能收下文件

    # ---- 窗口

    def _setup_window(self) -> None:
        root = self.root
        root.title(APP_TITLE)
        root.configure(bg=BG)
        root.minsize(480, 700)
        self._center(520, 756)
        icon = resource_path("link.ico")
        if os.path.exists(icon):
            try:
                root.iconbitmap(icon)
            except tk.TclError:
                pass

    def _center(self, w: int, h: int) -> None:
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        h = min(h, sh - 80)  # 笔记本 768 高的屏幕也不要把窗口顶出屏幕外
        self.root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - 24)}")

    # ---- 布局

    def _build(self) -> None:
        outer = tk.Frame(self.root, bg=BG, padx=18, pady=14)
        outer.pack(fill="both", expand=True)

        self._build_tabs(outer)

        self.page_send = tk.Frame(outer, bg=BG)
        self.page_recv = tk.Frame(outer, bg=BG)
        self.page_send.pack(fill="both", expand=True)

        self._build_send(self.page_send)
        self._build_recv(self.page_recv)
        self._paint_tabs()

    def _build_tabs(self, parent: tk.Frame) -> None:
        bar = tk.Frame(parent, bg="#f0f0f0", bd=0)
        bar.pack(fill="x", pady=(0, 12))
        self.tab_send = tk.Label(bar, text="电脑 → 手机", bg="#1a1a1a", fg="#ffffff",
                                 font=UI_FONT, pady=6, cursor="hand2")
        self.tab_recv = tk.Label(bar, text="手机 → 电脑", bg="#f0f0f0", fg="#6a6a6a",
                                 font=UI_FONT, pady=6, cursor="hand2")
        self.tab_send.pack(side="left", expand=True, fill="x")
        self.tab_recv.pack(side="left", expand=True, fill="x")
        self.tab_send.bind("<Button-1>", lambda e: self._switch("send"))
        self.tab_recv.bind("<Button-1>", lambda e: self._switch("recv"))

    def _paint_tabs(self) -> None:
        on, off = "#1a1a1a", "#f0f0f0"
        fg_on, fg_off = "#ffffff", "#6a6a6a"
        self.tab_send.configure(bg=on if self._page == "send" else off,
                                fg=fg_on if self._page == "send" else fg_off)
        self.tab_recv.configure(bg=on if self._page == "recv" else off,
                                fg=fg_on if self._page == "recv" else fg_off)

    def _switch(self, page: str) -> None:
        if self._page == page:
            return
        self._page = page
        self._paint_tabs()
        if page == "send":
            self.page_recv.pack_forget()
            self.page_send.pack(fill="both", expand=True)
            self.refresh()
        else:
            self.page_send.pack_forget()
            self.page_recv.pack(fill="both", expand=True)
            self._open_inbox()

    # ---- 发送页

    def _build_send(self, wrap: tk.Frame) -> None:
        self.title_label = tk.Label(wrap, text="粘贴文字，或拖入图片 / 文件",
                                    bg=BG, fg=DIM, font=HINT_FONT, anchor="w")
        self.title_label.pack(fill="x")

        self.box = tk.Frame(wrap, bg=CARD, highlightbackground=LINE,
                            highlightthickness=1, bd=0, height=150)
        self.box.pack(fill="x", expand=False, pady=(6, 0))
        self.box.pack_propagate(False)

        # --- 文字视图
        self.text = tk.Text(self.box, wrap="char", bd=0, relief="flat",
                            font=UI_FONT, bg=CARD, fg="#1a1a1a",
                            insertbackground="#1a1a1a", padx=12, pady=10,
                            selectbackground="#ffe27a", selectforeground="#1a1a1a",
                            highlightthickness=0)
        self.text.pack(fill="both", expand=True)
        self.text.bind("<<Modified>>", self._on_modified)
        self.text.bind("<Control-a>", self._select_all)
        # 绑虚拟事件而不是 <Control-v>：右键粘贴、Shift+Insert 也能走到这里
        self.text.bind("<<Paste>>", self._on_paste)
        self.text.focus_set()

        # 占位提示叠在 Text 上，不往 Text 里塞假文字，免得污染真实内容
        self.ph = tk.Frame(self.box, bg=CARD)
        tk.Label(self.ph, text="拖入图片 / 文件，或在这里粘贴文字",
                 bg=CARD, fg="#9a9a9a", font=HINT_FONT).pack()
        tk.Label(self.ph, text="Ctrl+V 可以直接粘贴截图",
                 bg=CARD, fg="#bcbcbc", font=HINT_FONT).pack(pady=(6, 0))
        self._bind_focus(self.ph)

        # --- 图片 / 文件视图
        self.card = tk.Frame(self.box, bg=CARD)
        self.card_inner = tk.Frame(self.card, bg=CARD, padx=12, pady=12)
        self.card_inner.pack(fill="both", expand=True)

        self.thumb_label = tk.Label(self.card_inner, bg=CARD, bd=0)
        self.thumb_label.pack(side="left")

        info = tk.Frame(self.card_inner, bg=CARD)
        info.pack(side="left", fill="both", expand=True, padx=(12, 0))
        self.name_label = tk.Label(info, text="", bg=CARD, fg="#1a1a1a",
                                   font=UI_FONT, anchor="w", justify="left")
        self.name_label.pack(fill="x", pady=(8, 0))
        self.meta_label = tk.Label(info, text="", bg=CARD, fg=DIM,
                                   font=HINT_FONT, anchor="w")
        self.meta_label.pack(fill="x", pady=(4, 0))

        close = tk.Label(self.card_inner, text="×", bg=CARD, fg="#b0b0b0",
                         font=("Segoe UI", 15), cursor="hand2")
        close.pack(side="right", anchor="n", padx=(8, 0))
        close.bind("<Button-1>", lambda e: self._clear_item())

        # --- 二维码
        qr_holder = tk.Frame(wrap, bg=CARD, highlightbackground=LINE,
                             highlightthickness=1, bd=0)
        qr_holder.pack(fill="x", expand=False, pady=(16, 0))
        self.qr_label = tk.Label(qr_holder, bg=CARD, bd=0)
        self.qr_label.pack(expand=True, padx=10, pady=10)

        # --- 状态
        self.status = tk.Label(wrap, text="等待输入", bg=BG, fg=DIM,
                               font=HINT_FONT, anchor="center", justify="center")
        self.status.pack(fill="x", pady=(12, 0))

        self.addr = tk.Label(wrap, text="", bg=BG, fg=LINK,
                             font=HINT_FONT, anchor="center", cursor="hand2")
        self.addr.bind("<Button-1>", self._on_addr_click)

        self._sync_placeholder()

    # ---- 接收页

    def _build_recv(self, page: tk.Frame) -> None:
        holder = tk.Frame(page, bg=CARD, highlightbackground=LINE,
                          highlightthickness=1, bd=0)
        holder.pack(fill="x")
        self.rv_qr = tk.Label(holder, bg=CARD, bd=0)
        self.rv_qr.pack(expand=True, pady=12)

        tk.Label(page, text="手机扫码，把东西发给电脑", bg=BG, fg=BODY,
                 font=UI_FONT).pack(fill="x", pady=(10, 0))
        tk.Label(page, text="文字、图片、文件都行，手机需与电脑连同一 WiFi",
                 bg=BG, fg=DIM, font=HINT_FONT).pack(fill="x", pady=(3, 0))

        row = tk.Frame(page, bg=BG)
        row.pack(fill="x", pady=(14, 0))
        tk.Label(row, text="保存到", bg=BG, fg=DIM, font=HINT_FONT).pack(side="left")
        self.path_label = tk.Label(row, text="", bg=CARD, fg="#4a4a4a", font=HINT_FONT,
                                   anchor="w", padx=8, pady=4, cursor="hand2",
                                   highlightbackground=LINE, highlightthickness=1)
        self.path_label.pack(side="left", fill="x", expand=True, padx=(8, 8))
        self.path_label.bind("<Button-1>", lambda e: self._open_folder())
        change = tk.Label(row, text="更改", bg=BG, fg=LINK, font=HINT_FONT, cursor="hand2")
        change.pack(side="right")
        change.bind("<Button-1>", lambda e: self._pick_dir())

        head = tk.Frame(page, bg=BG)
        head.pack(fill="x", pady=(14, 0))
        tk.Label(head, text="最近接收", bg=BG, fg=DIM, font=HINT_FONT).pack(side="left")
        self.rv_count = tk.Label(head, text="", bg=BG, fg="#bcbcbc", font=HINT_FONT)
        self.rv_count.pack(side="right")

        self.rv_list = tk.Frame(page, bg=CARD, highlightbackground=LINE,
                                highlightthickness=1, bd=0)
        self.rv_list.pack(fill="both", expand=True, pady=(6, 0))

        self.rv_status = tk.Label(page, text="切换到这一页才会开启接收",
                                  bg=BG, fg=DIM, font=HINT_FONT)
        self.rv_status.pack(fill="x", pady=(10, 0))

        self.rv_addr = tk.Label(page, text="", bg=BG, fg=LINK, font=HINT_FONT,
                                cursor="hand2")
        self.rv_addr.bind("<Button-1>", self._on_addr_click)

    def _open_inbox(self) -> None:
        """进入接收页：启动服务、生成二维码、把已收到的列出来。"""
        try:
            self.server.start()
        except OSError as e:
            self._set_rv_status(f"本地服务启动失败：{e}", ERR)
            return
        if self.server.inbox is None:
            self.server.attach_inbox(self.save_dir, self.inbox_q.put)

        self._seed_recent()

        url = self.server.base_url(self.current_ip()) + "/r/" + self.server.inbox.token
        try:
            img = make_qr_image(url, 250)
        except Exception as e:
            self._set_rv_status(f"二维码生成失败：{e}", ERR)
            return
        self._photo_recv = ImageTk.PhotoImage(img)
        self.rv_qr.configure(image=self._photo_recv)

        self._update_path_row()
        self._render_received()
        self._set_rv_status("已开启接收 · 收到的东西按日期存进上面的文件夹", BODY)
        self._show_rv_addr(url)

    def _seed_recent(self) -> None:
        """第一次进接收页时，把当天文件夹里已有的文件列出来。

        否则重开一次软件「最近接收」就空了，明明文件都还在磁盘上。
        """
        if self._received:
            return
        folder = cfg.today_dir(self.save_dir)
        try:
            names = [n for n in os.listdir(folder)
                     if os.path.isfile(os.path.join(folder, n))]
        except OSError:
            return

        entries = []
        for n in names:
            p = os.path.join(folder, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            entries.append((st.st_mtime, n, p, st.st_size))
        entries.sort(reverse=True)

        for mtime, n, p, size in entries[:6]:
            mime = mimetypes.guess_type(n)[0] or ""
            if n.lower().endswith(".txt"):
                kind = "text"
            elif mime.startswith("image/"):
                kind = "image"
            else:
                kind = "file"
            self._received.append(Received(name=n, path=p, size=size,
                                           kind=kind, mime=mime, ts=mtime))

    def _update_path_row(self) -> None:
        self.path_label.configure(text=self.save_dir)

    def _show_rv_addr(self, url: str) -> None:
        text = f"{url}    点此换网卡" if len(self._ips) > 1 else url
        self.rv_addr.configure(text=text)
        if not self.rv_addr.winfo_manager():
            self.rv_addr.pack(fill="x", pady=(3, 0))

    def _set_rv_status(self, text: str, color: str) -> None:
        self.rv_status.configure(text=text, fg=color)

    def _pick_dir(self) -> None:
        d = filedialog.askdirectory(title="选择接收文件夹", initialdir=self.save_dir)
        if not d:
            return
        d = os.path.abspath(d)
        ok, err = cfg.set_save_dir(d)
        if not ok:
            messagebox.showerror(APP_TITLE, f"这个文件夹用不了：\n{err}")
            return
        self.save_dir = d
        if self.server.inbox is not None:
            self.server.inbox.set_root(d)
        self._update_path_row()
        self._set_rv_status(f"已改为保存到 {d}", BODY)

    def _open_folder(self) -> None:
        d = self.save_dir
        if not os.path.isdir(d):
            self._set_rv_status("目录不存在了，点「更改」重新选一个", ERR)
            return
        try:
            os.startfile(d)
        except OSError as e:
            self._set_rv_status(f"打开失败：{e}", ERR)

    def _open_item(self, path: str) -> None:
        """点列表中的一条，在资源管理器里定位到它。"""
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        except OSError:
            self._open_folder()

    def _render_received(self) -> None:
        for child in self.rv_list.winfo_children():
            child.destroy()
        self._row_photos = []

        items = self._received
        if not items:
            tk.Label(self.rv_list, text="还没有收到东西", bg=CARD, fg="#bcbcbc",
                     font=HINT_FONT).pack(expand=True)
            self.rv_count.configure(text="")
            return

        self.rv_count.configure(text=f"共 {len(items)} 个 · 点条目定位文件")
        for rec in items[:3]:                      # 只列最近 3 条，再多会把底部地址行挤出窗口
            row = tk.Frame(self.rv_list, bg=CARD, padx=10, pady=7, cursor="hand2")
            row.pack(fill="x")

            try:
                thumb = make_thumb(Item(
                    kind="image" if rec.kind == "image" else "file",
                    name=rec.name, path=rec.path), 32)
            except Exception:
                thumb = make_thumb(Item(kind="file", name=rec.name, path=""), 32)
            photo = ImageTk.PhotoImage(thumb)
            self._row_photos.append(photo)         # 不留引用图会变白块

            ic = tk.Label(row, image=photo, bg=CARD, bd=0)
            ic.pack(side="left")

            info = tk.Frame(row, bg=CARD)
            info.pack(side="left", fill="x", expand=True, padx=(9, 0))
            name = tk.Label(info, text=shorten(rec.name, 26), bg=CARD, fg="#1a1a1a",
                            font=UI_FONT, anchor="w")
            name.pack(fill="x")
            label = {"image": "图片", "file": "文件", "text": "文字"}.get(rec.kind, "文件")
            meta = tk.Label(info, text=f"{label} · {human_size(rec.size)}",
                            bg=CARD, fg=DIM, font=HINT_FONT, anchor="w")
            meta.pack(fill="x")

            when = tk.Label(row, text=time.strftime("%H:%M", time.localtime(rec.ts)),
                            bg=CARD, fg="#bcbcbc", font=HINT_FONT)
            when.pack(side="right")

            for w in (row, ic, info, name, meta, when):
                w.bind("<Button-1>", lambda e, p=rec.path: self._open_item(p))

    def _poll_inbox(self) -> None:
        """服务线程只往队列里塞，界面更新全在这里做。

        在 HTTP 处理线程里直接碰 tkinter 控件会让 Tcl 重入，进程直接死，
        这和之前拖放踩的坑是同一个。
        """
        got = None
        while True:
            try:
                rec = self.inbox_q.get_nowait()
            except queue.Empty:
                break
            got = rec
            self._received.insert(0, rec)
            if len(self._received) > 60:
                del self._received[60:]

        if got is not None:
            if got.kind == "text":
                try:
                    with open(got.path, "r", encoding="utf-8-sig") as f:
                        self.root.clipboard_clear()
                        self.root.clipboard_append(f.read())
                except OSError:
                    pass
            if self._page == "recv":
                self._render_received()
                note = f"刚收到 {got.name}"
                if got.kind == "text":
                    note += "（已复制到剪贴板）"
                self._set_rv_status(note, BODY)
            else:
                self._set_status(f"手机发来 {got.name}，已存好", BODY)

        self.root.after(250, self._poll_inbox)

    def _bind_focus(self, widget: tk.Widget) -> None:
        """占位提示盖在 Text 上面会吞掉点击，点它要能把焦点交回去。"""
        widget.bind("<Button-1>", lambda e: self.text.focus_set())
        for child in widget.winfo_children():
            self._bind_focus(child)

    def _hook_drop(self) -> None:
        try:
            self.root.update_idletasks()  # 先让窗口真正创建出来，winfo_id 才有句柄
            self._drop = dragdrop.FileDrop(self.root)
        except Exception:
            self._drop = None
            return  # 非 Windows 或接口异常时静默降级，粘贴这条路径仍然可用
        self._poll_drop()

    def _poll_drop(self) -> None:
        """窗口过程只负责攒路径，真正的界面操作放在这里做。"""
        if self._drop is None:
            return
        while self._drop.pending:
            self._accept_paths(self._drop.pending.pop(0))
        while self._drop.errors:
            self._drop.errors.pop(0)
            self._set_status("拖放没有成功，请改用 Ctrl+V 或重新拖一次", ERR)
        self.root.after(120, self._poll_drop)

    # ---- 视图切换

    def _show_card(self) -> None:
        self.text.pack_forget()
        self.ph.place_forget()
        self.card.pack(fill="both", expand=True)

    def _show_text(self) -> None:
        self.card.pack_forget()
        self.text.pack(fill="both", expand=True)
        self._sync_placeholder()

    def _sync_placeholder(self) -> None:
        if self.item is not None:
            return
        if self.text.get("1.0", "end-1c").strip():
            self.ph.place_forget()
        else:
            self.ph.place(relx=0.5, rely=0.5, anchor="center")

    # ---- 输入事件

    def _select_all(self, _e=None):
        self.text.tag_add("sel", "1.0", "end-1c")
        return "break"

    def _on_modified(self, _e=None) -> None:
        if not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        self._sync_placeholder()
        # 输入停顿 300ms 才重算，避免每敲一个字都重建二维码
        if self._pending:
            self.root.after_cancel(self._pending)
        self._pending = self.root.after(300, self.refresh)

    def _on_paste(self, _e=None):
        item = self._clipboard_item()
        if item is None:
            return None  # 剪贴板里是文字，交回 Text 自己粘贴
        self._extra = 0
        self._set_item(item)
        return "break"

    def _clipboard_item(self) -> Item | None:
        """一个函数覆盖两种情况：剪贴板里是位图（截图），或者复制来的文件。"""
        try:
            data = ImageGrab.grabclipboard()
        except Exception:
            return None
        if isinstance(data, Image.Image):
            return self._save_clip_image(data)
        if isinstance(data, list):
            for p in data:
                if isinstance(p, str) and os.path.isfile(p):
                    try:
                        return item_from_path(p)
                    except OSError:
                        continue
        return None

    def _save_clip_image(self, img: Image.Image) -> Item | None:
        """剪贴板位图没有对应的文件，只能落一次盘才能发给手机。"""
        ts = time.strftime("%H%M%S")
        path = os.path.join(ensure_temp_dir(), f"clip-{ts}-{secrets.token_hex(3)}.png")
        try:
            img.save(path, "PNG")
        except OSError as e:
            self._set_status(f"保存截图失败：{e}", ERR)
            return None
        return Item(kind="image", name=f"截图 {ts}.png", path=path,
                    size=os.path.getsize(path), mime="image/png", temporary=True)

    # ---- 拖放

    def _accept_paths(self, paths: list[str]) -> None:
        files = [p for p in paths if os.path.isfile(p)]
        if not files:
            self._set_status("文件夹暂不支持，请拖入具体文件", ERR)
            return
        self._extra = max(0, len(files) - 1)
        self._load_path(files[0])

    def _load_path(self, path: str) -> None:
        try:
            item = item_from_path(path)
        except OSError as e:
            self._set_status(f"读取失败：{e}", ERR)
            return
        self._set_item(item)

    # ---- 内容切换

    def _set_item(self, item: Item) -> None:
        if self.item is not None and self.item.temporary:
            self._remove_temp(self.item)
        self.item = item
        self.text.delete("1.0", "end")
        self.text.edit_modified(False)
        self._thumb = ImageTk.PhotoImage(make_thumb(item))
        self.thumb_label.configure(image=self._thumb)
        self.name_label.configure(text=shorten(item.name, 22))
        self.meta_label.configure(
            text=f"{'图片' if item.kind == 'image' else '文件'} · {human_size(item.size)}")
        self.title_label.configure(text="图片" if item.kind == "image" else "文件")
        self._show_card()
        self.refresh()

    def _clear_item(self) -> None:
        if self.item is not None:
            self._remove_temp(self.item)
        self.item = None
        self._extra = 0
        self._show_text()
        self.title_label.configure(text="粘贴文字，或拖入图片 / 文件")
        self._hide_addr()
        self.refresh()
        self.text.focus_set()

    @staticmethod
    def _remove_temp(item: Item) -> None:
        """只删自己写进 text2qr 目录的临时文件，用户的原文件永远不碰。"""
        if not item.temporary or not item.path:
            return
        try:
            if os.path.dirname(os.path.abspath(item.path)) == os.path.abspath(temp_dir()):
                os.remove(item.path)
        except OSError:
            pass

    # ---- 地址行

    def current_ip(self) -> str:
        return self._ips[self._ip_index % len(self._ips)]

    def _on_addr_click(self, _e=None) -> None:
        if len(self._ips) <= 1:
            return
        self._ip_index = (self._ip_index + 1) % len(self._ips)
        if self._page == "recv":
            self._open_inbox()      # 接收页换网卡就是重建二维码
        else:
            self.refresh()

    def _show_addr(self, url: str) -> None:
        text = f"{url}    点此换网卡" if len(self._ips) > 1 else url
        self.addr.configure(text=text)
        if not self.addr.winfo_manager():
            self.addr.pack(fill="x", pady=(4, 0))

    def _hide_addr(self) -> None:
        if self.addr.winfo_manager():
            self.addr.pack_forget()

    # ---- 刷新

    def refresh(self) -> None:
        self._pending = None
        if self.item is not None:
            self._refresh_item()
        else:
            self._refresh_text()

    def _refresh_text(self) -> None:
        self._hide_addr()
        # Text 末尾总带一个隐式换行，粘贴时还可能多带一个；不剥掉的话
        # 手机扫出来的文字末尾会莫名多一行空行。
        text = self.text.get("1.0", "end-1c").rstrip("\r\n")

        if not text.strip():
            self._draw_placeholder()
            self._set_status("等待输入", DIM)
            return

        if len(text.encode("utf-8")) <= DIRECT_LIMIT:
            self._render_qr(text, f"已编码进二维码 · {len(text)} 字 · 手机可直接读取，无需联网")
            return

        try:
            self.server.start()
        except OSError as e:
            self._set_status(f"本地服务启动失败：{e}", ERR)
            return
        path = self.server.publish(Item(kind="text", text=text))
        url = self.server.base_url(self.current_ip()) + path
        self._show_addr(url)
        self._render_qr(url, f"本地链接 · {len(text)} 字 · 手机需与电脑连同一 Wi-Fi")

    def _refresh_item(self) -> None:
        item = self.item
        if not os.path.isfile(item.path):
            self._draw_placeholder()
            self._hide_addr()
            self._set_status("原文件已不存在，请重新拖入", ERR)
            return

        item.size = os.path.getsize(item.path)
        self.meta_label.configure(
            text=f"{'图片' if item.kind == 'image' else '文件'} · {human_size(item.size)}")

        try:
            self.server.start()
        except OSError as e:
            self._set_status(f"本地服务启动失败：{e}", ERR)
            return

        path = self.server.publish(item)
        url = self.server.base_url(self.current_ip()) + path
        self._show_addr(url)

        if item.kind == "image":
            note = "手机扫码后长按图片保存到相册"
        else:
            note = "手机扫码后点「下载到手机」"
        note += "\n传输期间请勿移动或改名原文件"
        if self._extra:
            note += f"\n已忽略另外 {self._extra} 个文件"
        self._render_qr(url, note)

    def _render_qr(self, payload: str, note: str) -> None:
        try:
            img = make_qr_image(payload)
        except Exception as e:  # 极端长文本或编码异常
            self._set_status(f"生成失败：{e}", ERR)
            return
        self._photo = ImageTk.PhotoImage(img)
        self.qr_label.configure(image=self._photo)
        self._set_status(note, BODY)

    def _draw_placeholder(self) -> None:
        """空状态：一块浅灰占位，避免界面看起来像坏了，也避免布局跳动。"""
        img = Image.new("RGB", (QR_MAX_PX, QR_MAX_PX), "#ffffff")
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, QR_MAX_PX - 1, QR_MAX_PX - 1], fill="#f4f4f4", outline="#e0e0e0")
        self._photo = ImageTk.PhotoImage(img)
        self.qr_label.configure(image=self._photo)

    def _set_status(self, text: str, color: str) -> None:
        self.status.configure(text=text, fg=color)

    # ---- 退出

    def shutdown(self) -> None:
        """收尾。先把窗口过程换回去，否则窗口销毁时还会回调 Python，进程会在退出阶段崩。"""
        if self._drop is not None:
            self._drop.detach()
            self._drop = None
        self.server.stop()
        purge_temp()

    def on_close(self) -> None:
        self.shutdown()
        self.root.destroy()


def main() -> None:
    # 高分屏下不做 DPI 感知，字会糊
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    purge_temp()  # 清掉上次崩溃/强杀留下的残留，最坏情况也只活到这次启动
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    try:
        root.mainloop()
    finally:
        app.shutdown()  # 走 Esc / Alt+F4 / 异常退出时兜底
        purge_temp()


if __name__ == "__main__":
    main()
