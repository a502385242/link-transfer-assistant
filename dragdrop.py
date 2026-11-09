# -*- coding: utf-8 -*-
"""接收资源管理器的文件拖放。

tkinter 本身不支持系统拖放，这里直接调 Win32：
先给窗口 DragAcceptFiles，再子类化窗口过程截获 WM_DROPFILES。
不引入第三方库，PyInstaller 打包后不用额外带 dll 或 tcl 脚本。

一条硬规矩：窗口过程里绝对不碰 Tk。此时 Tcl 正处在自己的消息循环内部，
调 after() / 改控件都会让解释器重入，结果是进程直接段错误。
所以这里只把路径攒进 pending 列表，由主循环定时取走。

Tk 的顶层窗口在 Windows 上其实有两层（Tk 自己的 wrapper + 内层窗口），
拖放的命中测试落在哪一层不确定，所以两层都挂，谁收到都能处理。
"""

import ctypes
from ctypes import wintypes

WM_DROPFILES = 0x0233
GWLP_WNDPROC = -4

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)

_LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, wintypes.LPARAM)

# 32 位 Python 没有 SetWindowLongPtrW，只有 SetWindowLongW
_SetWndProc = getattr(_user32, "SetWindowLongPtrW", None) or _user32.SetWindowLongW
_SetWndProc.restype = ctypes.c_void_p
_SetWndProc.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_void_p)

_user32.CallWindowProcW.restype = _LRESULT
_user32.CallWindowProcW.argtypes = (ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM)

_user32.DefWindowProcW.restype = _LRESULT
_user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM)

_user32.GetParent.restype = wintypes.HWND
_user32.GetParent.argtypes = (wintypes.HWND,)

_shell32.DragAcceptFiles.argtypes = (wintypes.HWND, wintypes.BOOL)
_shell32.DragQueryFileW.argtypes = (wintypes.HANDLE, wintypes.UINT,
                                    wintypes.LPWSTR, wintypes.UINT)
_shell32.DragQueryFileW.restype = wintypes.UINT
_shell32.DragFinish.argtypes = (wintypes.HANDLE,)


def _extract(hdrop) -> list[str]:
    """从 HDROP 里取出全部路径。纯 Win32 调用，不涉及 Tcl，可以在窗口过程里安全执行。"""
    total = _shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
    paths: list[str] = []
    for i in range(total):
        need = _shell32.DragQueryFileW(hdrop, i, None, 0)
        if need <= 0:
            continue
        buf = ctypes.create_unicode_buffer(need + 1)
        _shell32.DragQueryFileW(hdrop, i, buf, need + 1)
        if buf.value:
            paths.append(buf.value)
    return paths


class FileDrop:
    """挂上文件拖放，落下的路径进 pending，由调用方轮询取走。"""

    def __init__(self, widget):
        self.pending: list[list[str]] = []
        self.errors: list[BaseException] = []
        self._procs: dict[int, object] = {}
        self._olds: dict[int, int] = {}

        inner = widget.winfo_id()
        for hwnd in {inner, _user32.GetParent(inner)}:
            if hwnd:
                self._install(hwnd)

    def _install(self, hwnd: int) -> None:
        _shell32.DragAcceptFiles(hwnd, True)
        # 回调对象必须一直留着引用，被回收就变成野指针，窗口消息直接崩
        proc = _WNDPROC(self._make_handler(hwnd))
        old = _SetWndProc(hwnd, GWLP_WNDPROC, ctypes.cast(proc, ctypes.c_void_p))
        self._procs[hwnd] = proc
        self._olds[hwnd] = old

    def _make_handler(self, hwnd: int):
        def handler(h, msg, wparam, lparam):
            if msg == WM_DROPFILES:
                try:
                    paths = _extract(wparam)
                    if paths:
                        self.pending.append(paths)
                except BaseException as e:      # 记录下来交给主循环，不在窗口过程里抛
                    self.errors.append(e)
                finally:
                    # 不管处理成没成功都要告诉系统释放，否则每次拖放泄漏一块内存
                    _shell32.DragFinish(wparam)
                return 0

            old = self._olds.get(hwnd)
            if old:
                return _user32.CallWindowProcW(old, h, msg, wparam, lparam)
            return _user32.DefWindowProcW(h, msg, wparam, lparam)

        return handler

    def detach(self) -> None:
        """退出前把窗口过程换回去。不然窗口销毁时还会回调到 Python，进程会在收尾阶段崩掉。"""
        for hwnd, old in list(self._olds.items()):
            if old:
                try:
                    _SetWndProc(hwnd, GWLP_WNDPROC, old)
                except Exception:
                    pass
        self._procs.clear()
        self._olds.clear()
