# -*- coding: utf-8 -*-
"""局域网取件服务：文字 / 图片 / 文件三种内容共用一个本地 HTTP 服务。

手机和电脑连同一个 Wi-Fi 后扫码访问：
    /t/<token>   一段文字
    /i/<token>   图片展示页（?raw=1 取原始字节用于显示，?dl=1 触发下载）
    /f/<token>   文件下载

安全上只做一件事：只认内存里登记过的 token，客户端传不了任何路径参数，
所以不存在目录穿越。每次内容更新换新 token，旧链接立刻失效。

大文件用 64KB 分块流式发送并支持 Range，几百 MB 也不会把内存吃掉。
"""

import html
import json
import mimetypes
import os
import secrets
import shutil
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config as cfg

CHUNK = 64 * 1024
MAX_UPLOAD = 2 * 1024 ** 3          # 单文件 2 GB，再大手机端也传不动
MAX_TEXT = 1024 * 1024              # 一段文字 1 MB 已经很夸张了
KEEP_ITEMS = 200                    # 内存里只留最近 200 条记录，防止长时间运行越攒越多


# ---------------------------------------------------------------- 工具


def human_size(n: int) -> str:
    """把字节数变成人能读的大小。"""
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{int(round(v))} B" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} GB"


def lan_ips() -> list[str]:
    """本机在局域网里的候选地址，第一个是系统默认出口。

    多网卡（VMware、Docker、Hyper-V）时会有多个，界面让用户点一下切换，
    否则选错虚拟网卡手机就连不上。
    """
    found: list[str] = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # UDP connect 不发包，只是让系统选出默认网卡
        found.append(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127.") or ip.startswith("169.254.") or ip in found:
                continue
            found.append(ip)
    except OSError:
        pass

    return found or ["127.0.0.1"]


# ---------------------------------------------------------------- 内容模型


@dataclass
class Item:
    """一份要被手机取走的内容。"""

    kind: str                   # text / image / file
    name: str = ""
    path: str = ""
    text: str = ""
    size: int = 0
    mime: str = ""
    temporary: bool = False     # 程序自己生成的临时文件，退出时要删

    @property
    def segment(self) -> str:
        return {"text": "t", "image": "i", "file": "f"}[self.kind]


def item_from_path(path: str, temporary: bool = False) -> Item:
    """按磁盘上的文件建一条内容，图片和其他文件自动区分。

    注意这里是「引用」而不是「复制」：手机下载时按这个路径去读原文件，
    所以软件期间原文件不能改名或删除。
    """
    st = os.stat(path)
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    kind = "image" if mime.startswith("image/") else "file"
    return Item(kind=kind, name=os.path.basename(path), path=path,
                size=st.st_size, mime=mime, temporary=temporary)


# ---------------------------------------------------------------- 文件名净化


_ILLEGAL = set('\\/:*?"<>|')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | \
            {f"LPT{i}" for i in range(1, 10)}


def safe_name(raw: str) -> str:
    """把手机传上来的文件名洗成能安全落盘的名字。

    这里传入的字符串已经过 urllib 解码，因此不再 unquote，否则文件名里的
    百分号会被二次解码，出现 %20 变空格的错乱。

    做四件事：只留最后一段（剥掉任何路径）、剔除 Windows 非法字符、
    拦保留名、限长。客户端无论如何都传不进目标目录之外的路径。
    """
    name = (raw or "").replace("/", "\\").split("\\")[-1]
    name = "".join(c for c in name if c not in _ILLEGAL and ord(c) >= 32)
    name = name.strip().strip(".")
    if not name:
        return "未命名"
    stem, ext = os.path.splitext(name)
    if stem.upper() in _RESERVED:
        stem = "_" + stem
    if not stem:
        stem = "未命名"
    return stem[:100] + ext[:16]


def unique_path(folder: str, name: str) -> str:
    """同名不覆盖，依次加 (1)(2)。手机相册里一堆 IMG_0001.jpg 是常态。"""
    path = os.path.join(folder, name)
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(name)
    for i in range(1, 500):
        cand = os.path.join(folder, f"{stem}({i}){ext}")
        if not os.path.exists(cand):
            return cand
    return os.path.join(folder, f"{stem}-{secrets.token_hex(3)}{ext}")


def _quiet_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _read_exact(stream, n: int) -> bytes:
    """BufferedIO 的 read 可能短读，必须循环收满。"""
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def _kind_label(kind: str) -> str:
    return {"image": "图片", "file": "文件", "text": "文字"}.get(kind, "文件")


# ---------------------------------------------------------------- 收件箱


@dataclass
class Received:
    """一条收到的记录。"""

    name: str
    path: str
    size: int
    kind: str            # image / file / text
    mime: str
    ts: float

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "size": self.size,
            "kind": self.kind,
            "label": _kind_label(self.kind),
            "time": time.strftime("%H:%M", time.localtime(self.ts)),
        }


class Inbox:
    """手机往电脑写东西的收件箱。

    token 每次启动重新生成，程序关掉链接即刻作废。除了这三个写入口，
    服务端不提供任何读取接口 —— 拿到链接的人只能往目录里塞文件，
    取不走电脑上已有的任何内容。
    """

    def __init__(self, root: str, notify=None):
        self.token = secrets.token_hex(16)
        self.root = root
        self.notify = notify
        self.items: list[Received] = []
        self.total = 0
        self.lock = threading.Lock()
        self._active = 0
        self.max_active = 3
        self.max_bytes = MAX_UPLOAD

    # ---- 并发闸门：同一时刻最多 3 个上传，避免手机连点把磁盘打满

    def begin(self) -> bool:
        with self.lock:
            if self._active >= self.max_active:
                return False
            self._active += 1
            return True

    def end(self) -> None:
        with self.lock:
            self._active = max(0, self._active - 1)

    # ---- 目录

    def set_root(self, path: str) -> None:
        self.root = path

    def day_dir(self) -> str:
        folder = cfg.today_dir(self.root)
        os.makedirs(folder, exist_ok=True)
        return folder

    # ---- 写入

    def _register(self, path: str) -> Received:
        st = os.stat(path)
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        kind = "image" if mime.startswith("image/") else "file"
        rec = Received(name=os.path.basename(path), path=path, size=st.st_size,
                       kind=kind, mime=mime, ts=time.time())
        return self._push(rec)

    def _push(self, rec: Received) -> Received:
        with self.lock:
            self.items.append(rec)
            self.total += 1
            if len(self.items) > KEEP_ITEMS:
                del self.items[:len(self.items) - KEEP_ITEMS]
        if self.notify is not None:
            try:
                self.notify(rec)       # 在服务线程里调用，绝不能碰界面控件
            except Exception:
                pass
        return rec

    def store_upload(self, raw_name: str, stream, length: int) -> tuple[bool, str, Received | None]:
        """边收边写盘，几百 MB 的文件也不会进内存。"""
        if length > self.max_bytes:
            return False, "文件超过 2 GB 上限", None

        try:
            folder = self.day_dir()
        except OSError as e:
            return False, f"目录不可用：{e.strerror or e}", None

        try:
            free = shutil.disk_usage(folder).free
        except OSError:
            free = 0
        if free and free < length + 64 * 1024 * 1024:
            return False, "电脑磁盘剩余空间不足", None

        final = unique_path(folder, safe_name(raw_name))
        part = final + ".part"
        _quiet_remove(part)             # 上次异常中断留下的半截文件

        written = 0
        try:
            with open(part, "wb") as f:
                while written < length:
                    chunk = stream.read(min(CHUNK, length - written))
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
        except OSError as e:
            _quiet_remove(part)
            return False, f"写入失败：{e.strerror or e}", None

        if written != length:
            _quiet_remove(part)         # 手机中途断网，不留下残缺文件
            return False, "传输中断，文件不完整", None

        try:
            os.replace(part, final)
        except OSError as e:
            _quiet_remove(part)
            return False, f"保存失败：{e.strerror or e}", None

        return True, "", self._register(final)

    def store_text(self, raw: bytes) -> tuple[bool, str, Received | None]:
        text = raw.decode("utf-8", "replace").replace("\x00", "")
        if not text.strip():
            return False, "内容为空", None

        try:
            folder = self.day_dir()
        except OSError as e:
            return False, f"目录不可用：{e.strerror or e}", None

        first = ""
        for line in text.splitlines():
            if line.strip():
                first = line.strip()
                break
        stem = "".join(c for c in first if c not in _ILLEGAL and ord(c) >= 32)
        stem = stem.strip().strip(".")[:20].strip() or "文字"
        if stem.upper() in _RESERVED:
            stem = "_" + stem

        final = unique_path(folder, stem + ".txt")
        try:
            with open(final, "wb") as f:
                f.write(b"\xef\xbb\xbf")        # UTF-8 BOM，记事本双击不乱码
                f.write(text.encode("utf-8"))
        except OSError as e:
            return False, f"写入失败：{e.strerror or e}", None

        rec = Received(name=os.path.basename(final), path=final,
                       size=os.path.getsize(final), kind="text",
                       mime="text/plain", ts=time.time())
        return True, "", self._push(rec)

    # ---- 读取（仅供电脑端界面和手机端列表用，不给文件内容）

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [r.to_dict() for r in reversed(self.items)]


# ---------------------------------------------------------------- 手机端页面

BASE_CSS = """
:root { color-scheme: light; }
* { -webkit-tap-highlight-color: transparent; }
body { margin:0; background:#fff; color:#1a1a1a;
       font:17px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif; }
header { position:sticky; top:0; display:flex; align-items:center; justify-content:space-between;
         padding:12px 20px; background:#fff; border-bottom:1px solid #ececec;
         font-size:13px; color:#8a8a8a; }
button { border:0; border-radius:8px; padding:8px 16px; font-size:14px;
         background:#1a1a1a; color:#fff; }
button:active { opacity:.7; }
main { padding:20px; }
.btn { display:block; margin:18px 0 0; padding:13px; border-radius:10px; text-align:center;
       background:#1a1a1a; color:#fff; text-decoration:none; font-size:16px; }
.btn:active { opacity:.75; }
.tip { margin:14px 0 0; font-size:13px; color:#9a9a9a; text-align:center; line-height:1.6; }
"""

TOAST_CSS = """
#tip { position:fixed; left:50%; bottom:34px; transform:translate(-50%,20px);
       background:rgba(0,0,0,.82); color:#fff; font-size:14px; padding:9px 18px;
       border-radius:20px; opacity:0; transition:.25s; pointer-events:none; }
#tip.on { opacity:1; transform:translate(-50%,0); }
"""


def _shell(title: str, left: str, right: str, body: str,
           css: str = "", script: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{html.escape(title)}</title>
<style>{BASE_CSS}{css}</style>
</head><body>
<header><span>{left}</span><span>{right}</span></header>
{body}
{script}
</body></html>"""


def render_text_page(text: str) -> str:
    """手机端看文字。只做一件事：完整显示，并提供一个复制按钮。"""
    count = len(text)
    body = html.escape(text)
    script = """<div id="tip"></div>
<script>
function toast(msg) {
  var el = document.getElementById('tip');
  el.textContent = msg; el.classList.add('on');
  clearTimeout(window._t); window._t = setTimeout(function(){ el.classList.remove('on'); }, 1400);
}
function copyAll() {
  var t = document.getElementById('t').textContent;
  // 局域网 http 不是安全上下文，navigator.clipboard 在多数手机浏览器里不可用，
  // 所以必须有选区 + execCommand 的兜底，否则按钮点了没反应。
  if (navigator.clipboard) {
    navigator.clipboard.writeText(t).then(function(){ toast('已复制'); }, fallback);
  } else { fallback(); }
  function fallback() {
    var r = document.createRange(); r.selectNodeContents(document.getElementById('t'));
    var s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
    try { document.execCommand('copy'); toast('已复制'); }
    catch (e) { toast('请长按文字手动复制'); }
  }
}
</script>"""
    return _shell(
        "文字", f"{count} 字", '<button onclick="copyAll()">复制全部</button>',
        f'<main><pre id="t" style="margin:0;white-space:pre-wrap;word-break:break-word;'
        f'font:inherit;">{body}</pre></main>',
        css=TOAST_CSS, script=script)


def render_image_page(item: Item, token: str) -> str:
    """手机端看图片。主路径是长按保存，下载按钮用于浏览器直接落盘。"""
    css = """
.shot { display:flex; align-items:center; justify-content:center; padding:4px 0 0; }
.shot img { max-width:100%; height:auto; border-radius:10px; display:block; }
"""
    script = f"""<script>
var p = document.getElementById('pic');
p.addEventListener('load', function () {{
  document.getElementById('dim').textContent = p.naturalWidth + ' × ' + p.naturalHeight;
}});
</script>"""
    body = f"""<main>
<div class="shot"><img id="pic" src="/i/{token}?raw=1" alt="图片"></div>
<a class="btn" href="/i/{token}?dl=1">下载原图</a>
<p class="tip">长按图片可直接保存到相册<br>点上面按钮则下载原文件</p>
</main>"""
    return _shell("图片", '<span id="dim">图片</span>', human_size(item.size),
                  body, css=css, script=script)


def render_file_page(item: Item, token: str) -> str:
    """手机端下载文件。微信内置浏览器可能拦下载，所以提示用浏览器打开。"""
    ext = (os.path.splitext(item.name)[1].lstrip(".") or "FILE").upper()[:5]
    css = """
.file { border:1px solid #ececec; border-radius:12px; padding:22px 18px; text-align:center; }
.badge { width:52px; height:52px; margin:0 auto 12px; border-radius:8px; background:#f2f2f2;
         display:flex; align-items:center; justify-content:center;
         font-size:12px; font-weight:600; color:#7a7a7a; letter-spacing:.5px; }
.name { margin:0; font-size:15px; word-break:break-all; line-height:1.5; }
.meta { margin:6px 0 0; font-size:13px; color:#9a9a9a; }
"""
    body = f"""<main>
<div class="file">
  <div class="badge">{html.escape(ext)}</div>
  <p class="name">{html.escape(item.name)}</p>
  <p class="meta">{human_size(item.size)}</p>
</div>
<a class="btn" href="/f/{token}">下载到手机</a>
<p class="tip">无法下载时，点右上角选择「在浏览器打开」</p>
</main>"""
    return _shell(item.name, "来自电脑", human_size(item.size), body, css=css)


# ---------------------------------------------------------------- 手机接收页

RECEIVE_CSS = """
textarea { width:100%; box-sizing:border-box; height:104px; padding:12px 13px; font:inherit;
           border:1px solid #ececec; border-radius:10px; background:#fafafa; color:#1a1a1a;
           resize:none; outline:none; display:block; }
textarea:focus { border-color:#d0d0d0; background:#fff; }
.go { display:block; width:100%; margin:10px 0 0; padding:13px; border-radius:10px;
      background:#1a1a1a; color:#fff; font-size:16px; border:0; }
.go:active { opacity:.75; }
.or { display:flex; align-items:center; gap:10px; margin:18px 0 12px; }
.or span { flex:1; height:1px; background:#ececec; }
.or i { font-style:normal; font-size:12px; color:#b0b0b0; }
.picks { display:flex; gap:10px; }
.pick { flex:1; height:72px; border:1px dashed #dcdcdc; border-radius:10px; display:flex;
        flex-direction:column; align-items:center; justify-content:center; gap:7px;
        font-size:13px; color:#5a5a5a; }
.pick:active { background:#fafafa; }
.prog { margin-top:12px; background:#fafafa; border:1px solid #ececec; border-radius:10px;
        padding:10px 12px; display:none; }
.prog.on { display:block; }
.pn { display:flex; justify-content:space-between; gap:12px; font-size:12px; color:#5a5a5a; }
.pn span:first-child { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.bar { margin-top:8px; height:3px; background:#e6e6e6; border-radius:2px; overflow:hidden; }
#pb { height:3px; width:0; background:#1a1a1a; }
.sec { display:flex; justify-content:space-between; align-items:baseline; margin:20px 0 2px;
       font-size:12px; color:#9a9a9a; }
.row { display:flex; align-items:center; gap:10px; padding:10px 0; border-top:1px solid #f2f2f2; }
.ic { width:34px; height:34px; border-radius:6px; background:#f2f2f2; flex:none; overflow:hidden;
      display:flex; align-items:center; justify-content:center;
      font-size:9px; color:#8a8a8a; letter-spacing:.4px; }
.ic img { width:100%; height:100%; object-fit:cover; display:block; }
.ri { flex:1; min-width:0; }
.rn { margin:0; font-size:14px; word-break:break-all; line-height:1.4; }
.rm { margin:2px 0 0; font-size:12px; color:#a8a8a8; }
.ok { font-size:12px; color:#639922; flex:none; }
.empty { margin:22px 0; text-align:center; font-size:13px; color:#b8b8b8; }
"""

RECEIVE_BODY = """
<main>
<textarea id="ta" placeholder="粘贴或输入要发到电脑的文字……"></textarea>
<button class="go" onclick="sendText()">发送到电脑</button>

<div class="or"><span></span><i>或者上传</i><span></span></div>
<div class="picks">
  <label class="pick">
    <svg width="19" height="19" viewBox="0 0 18 18" fill="none" stroke="#6a6a6a" stroke-width="1.2"><rect x="2" y="3.5" width="14" height="11" rx="2"/><circle cx="6.4" cy="7.4" r="1.3"/><path d="M2.6 12.2L6.8 8.4l3.2 3 2.3-2 2.9 2.6"/></svg>
    相册 / 拍照
    <input id="pa" type="file" accept="image/*,video/*" multiple hidden>
  </label>
  <label class="pick">
    <svg width="19" height="19" viewBox="0 0 18 18" fill="none" stroke="#6a6a6a" stroke-width="1.2"><path d="M4.4 1.8h5.4l3.8 3.8v10.6H4.4z"/><path d="M9.6 1.8v4h4"/></svg>
    选择文件
    <input id="pf" type="file" multiple hidden>
  </label>
</div>
<p class="tip">原图原文件直传，不压缩画质<br>一次可选多个，逐个发送</p>

<div class="prog" id="prog">
  <div class="pn"><span id="pn">文件</span><span id="pp">0%</span></div>
  <div class="bar"><div id="pb"></div></div>
</div>

<div class="sec"><span id="cnt">还没有收到东西</span><span>保存在电脑上</span></div>
<div id="list"><p class="empty">发出的东西会按顺序列在这里</p></div>
</main>
<div id="tip"></div>
"""

RECEIVE_JS = """
<script>
var B = "/r/__TOKEN__", thumbs = {}, busy = false;
function $(i) { return document.getElementById(i); }
function toast(m) {
  var e = $("tip"); e.textContent = m; e.className = "on";
  clearTimeout(window._t);
  window._t = setTimeout(function () { e.className = ""; }, 1800);
}
function esc(s) {
  return String(s).replace(/[&<>"]/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
  });
}
function sz(n) {
  var u = ["B", "KB", "MB", "GB"], i = 0, v = n;
  while (v >= 1024 && i < 3) { v /= 1024; i++; }
  return (i ? v.toFixed(1) : Math.round(v)) + " " + u[i];
}
function icon(it) {
  if (thumbs[it.name]) return '<div class="ic"><img src="' + thumbs[it.name] + '"></div>';
  var t = it.kind === "text" ? "TXT" : (it.name.split(".").pop() || "FILE").toUpperCase().slice(0, 4);
  return '<div class="ic">' + esc(t) + '</div>';
}
function render(d) {
  var items = d.items || [], h = "", i;
  for (i = 0; i < items.length; i++) {
    var it = items[i];
    h += '<div class="row">' + icon(it) + '<div class="ri"><p class="rn">' + esc(it.name) +
         '</p><p class="rm">' + esc(it.label) + " · " + sz(it.size) + " · " + esc(it.time) +
         '</p></div><span class="ok">已保存</span></div>';
  }
  $("list").innerHTML = h || '<p class="empty">发出的东西会按顺序列在这里</p>';
  $("cnt").textContent = d.count ? "本次已收到 " + d.count + " 项" : "还没有收到东西";
}
function poll() {
  fetch(B + "/list", { cache: "no-store" }).then(function (r) {
    if (!r.ok) throw 0;
    return r.json();
  }).then(function (d) {
    $("dot").style.color = "#639922";
    $("dot").textContent = "● 已连接";
    render(d);
  }).catch(function () {
    $("dot").style.color = "#c0392b";
    $("dot").textContent = "● 已断开";
  });
}
function sendText() {
  var t = $("ta").value.replace(/[\\s\\u3000]+$/, "");
  if (!t) { toast("先输入点什么"); return; }
  fetch(B + "/t", { method: "POST", body: t }).then(function (r) {
    if (!r.ok) throw 0;
    $("ta").value = "";
    toast("已发到电脑");
    poll();
  }).catch(function () { toast("发送失败，确认电脑这边还开着"); });
}
function upload(f, i, n) {
  return new Promise(function (res, rej) {
    var x = new XMLHttpRequest();
    x.open("POST", B + "/u?name=" + encodeURIComponent(f.name || "未命名"));
    x.upload.onprogress = function (e) {
      if (!e.lengthComputable) return;
      var p = Math.round(e.loaded / e.total * 100);
      $("prog").className = "prog on";
      $("pn").textContent = i + "/" + n + " · " + f.name;
      $("pp").textContent = p + "%";
      $("pb").style.width = p + "%";
    };
    x.onload = function () {
      if (x.status === 200) {
        try {
          var d = JSON.parse(x.responseText);
          if (d.name && f.type && f.type.indexOf("image/") === 0) {
            thumbs[d.name] = URL.createObjectURL(f);
          }
        } catch (e) {}
        res();
      } else {
        rej(x.responseText || ("HTTP " + x.status));
      }
    };
    x.onerror = function () { rej("网络中断，检查是否同一 WiFi"); };
    x.send(f);
  });
}
function pick(id) {
  var inp = $(id), files = inp.files, arr, i = 0;
  if (!files || !files.length) return;
  if (busy) { toast("上一个还在传"); inp.value = ""; return; }
  busy = true;
  arr = Array.prototype.slice.call(files);
  (function next() {
    if (i >= arr.length) {
      $("prog").className = "prog"; busy = false; inp.value = "";
      toast("已发到电脑"); poll(); return;
    }
    upload(arr[i], i + 1, arr.length).then(function () { i++; next(); }, function (err) {
      $("prog").className = "prog"; busy = false; inp.value = "";
      toast(String(err).slice(0, 40)); poll();
    });
  })();
}
$("pa").addEventListener("change", function () { pick("pa"); });
$("pf").addEventListener("change", function () { pick("pf"); });
poll();
setInterval(poll, 2500);
</script>
"""


def render_receive_page(token: str) -> str:
    """手机端接收页。只有一个写入口，页面上不提供任何浏览电脑文件的能力。"""
    script = RECEIVE_JS.replace("__TOKEN__", token)
    return _shell("发给电脑", "发给电脑",
                  '<span id="dot" style="color:#639922">● 已连接</span>',
                  RECEIVE_BODY, css=RECEIVE_CSS + TOAST_CSS, script=script)



# ---------------------------------------------------------------- 服务


def _disposition(name: str) -> str:
    """中文文件名不能直接塞进 filename=，否则手机存下来是乱码。"""
    safe = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in (name or "file"))
    return f"attachment; filename=\"{safe}\"; filename*=UTF-8''{urllib.parse.quote(name or 'file', safe='')}"


class ShareServer:
    """按需启动的本地服务。内容换了就换新 token，旧二维码自动失效。"""

    def __init__(self) -> None:
        self._routes: dict[str, Item] = {}
        self._lock = threading.Lock()
        self._srv: ThreadingHTTPServer | None = None
        self.inbox: Inbox | None = None
        self.port = 0

    def attach_inbox(self, root: str, notify=None) -> Inbox:
        """打开接收通道。token 每次启动重新生成，关掉软件即作废。"""
        self.inbox = Inbox(root, notify)
        return self.inbox

    # ---- 生命周期

    def start(self) -> int:
        if self._srv is not None:
            return self.port
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "text2qr"

            def log_message(self, *args):  # 静默，命令行窗口不该被访问日志刷屏
                pass

            def do_HEAD(self):
                self._dispatch(head_only=True)

            def do_GET(self):
                self._dispatch(head_only=False)

            def do_POST(self):
                parts = urllib.parse.urlsplit(self.path)
                seg = parts.path.strip("/").split("/")
                if seg and seg[0] == "r" and len(seg) == 3 and seg[2] in ("u", "t"):
                    return self._receive(seg[1], seg[2], urllib.parse.parse_qs(parts.query))
                self.close_connection = True
                return self._plain(404, "Not Found")

            def _dispatch(self, head_only: bool) -> None:
                parts = urllib.parse.urlsplit(self.path)
                seg = parts.path.strip("/").split("/")
                if seg and seg[0] == "r":
                    return self._inbox(seg[1:], head_only)
                if len(seg) != 2:
                    return self._plain(404, "Not Found")
                kind, token = seg
                item = outer.lookup(token)
                if item is None or item.segment != kind:
                    return self._plain(404, "链接已失效，请重新扫码")

                query = urllib.parse.parse_qs(parts.query)
                if kind == "t":
                    return self._html(render_text_page(item.text), head_only)
                if kind == "i":
                    if "raw" in query:
                        return self._send_file(item, head_only, attachment=False)
                    if "dl" in query:
                        return self._send_file(item, head_only, attachment=True)
                    return self._html(render_image_page(item, token), head_only)
                return self._send_file(item, head_only, attachment=True)

            # ---- 接收模式

            def _inbox(self, rest: list[str], head_only: bool) -> None:
                inbox = outer.inbox
                if not rest or inbox is None:
                    return self._plain(404, "Not Found")
                if not secrets.compare_digest(rest[0], inbox.token):
                    return self._plain(404, "链接已失效，请在电脑上重新扫码")
                if len(rest) == 1:
                    return self._html(render_receive_page(inbox.token), head_only)
                if len(rest) == 2 and rest[1] == "list":
                    return self._json({"count": inbox.total, "items": inbox.snapshot()})
                return self._plain(404, "Not Found")

            def _receive(self, token: str, what: str, query: dict) -> None:
                inbox = outer.inbox
                if inbox is None or not secrets.compare_digest(token, inbox.token):
                    self.close_connection = True   # 没读 body，必须断开，否则连接状态错乱
                    return self._plain(404, "链接已失效，请在电脑上重新扫码")

                try:
                    length = int(self.headers.get("Content-Length", ""))
                except (TypeError, ValueError):
                    self.close_connection = True
                    return self._plain(411, "缺少长度信息")

                limit = inbox.max_bytes if what == "u" else MAX_TEXT
                if length < 0:
                    self.close_connection = True
                    return self._plain(400, "长度非法")
                if length > limit:
                    self._drain(length, 4 * 1024 * 1024)
                    self.close_connection = True
                    return self._plain(413, "内容太大，已拒绝")

                if not inbox.begin():
                    self._drain(length, 4 * 1024 * 1024)
                    self.close_connection = True
                    return self._plain(429, "正在接收其它文件，请稍后再试")

                try:
                    if what == "u":
                        name = (query.get("name") or ["未命名"])[0]
                        ok, msg, rec = inbox.store_upload(name, self.rfile, length)
                    else:
                        ok, msg, rec = inbox.store_text(_read_exact(self.rfile, length))
                finally:
                    inbox.end()

                if not ok or rec is None:
                    self.close_connection = True
                    return self._plain(500, msg or "保存失败")
                self._json({"ok": True, "name": rec.name, "size": rec.size})

            # ---- 响应

            def _drain(self, length: int, cap: int) -> None:
                """把没打算要的请求体读掉一部分再关连接。

                不读就 close，客户端往往还没收到响应就先吃到 RST，
                表现为「明明该提示太大，却只看到网络错误」。
                """
                remain = min(length, cap)
                try:
                    while remain > 0:
                        chunk = self.rfile.read(min(CHUNK, remain))
                        if not chunk:
                            break
                        remain -= len(chunk)
                except OSError:
                    pass

            def _json(self, obj: dict) -> None:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _plain(self, code: int, msg: str) -> None:
                body = msg.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _html(self, page: str, head_only: bool) -> None:
                body = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if not head_only:
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            def _send_file(self, item: Item, head_only: bool, attachment: bool) -> None:
                try:
                    total = os.path.getsize(item.path)
                except OSError:
                    return self._plain(410, "文件已被移动或删除，请在电脑上重新拖入")

                ctype = item.mime or "application/octet-stream"

                if total == 0:
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", "0")
                    if attachment:
                        self.send_header("Content-Disposition", _disposition(item.name))
                    self.end_headers()
                    return

                start, end, partial = 0, total - 1, False
                rng = self.headers.get("Range", "")
                if rng.startswith("bytes="):
                    spec = rng[6:].split(",")[0].strip()
                    a, _, b = spec.partition("-")
                    try:
                        if a:
                            start = int(a)
                            end = int(b) if b else total - 1
                        else:  # 只给了尾部长度，取最后 N 字节
                            start = max(0, total - int(b))
                        if start >= total or start > end:
                            raise ValueError
                        end = min(end, total - 1)
                        partial = True
                    except ValueError:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{total}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return

                length = end - start + 1
                self.send_response(206 if partial else 200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                if attachment:
                    self.send_header("Content-Disposition", _disposition(item.name))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if head_only:
                    return

                try:
                    with open(item.path, "rb") as f:
                        f.seek(start)
                        remain = length
                        while remain > 0:
                            chunk = f.read(min(CHUNK, remain))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remain -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # 手机中途取消下载，正常情况

        srv = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        srv.daemon_threads = True
        self._srv = srv
        self.port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return self.port

    def stop(self) -> None:
        if self._srv is None:
            return
        try:
            self._srv.shutdown()
            self._srv.server_close()
        except Exception:
            pass
        self._srv = None

    # ---- 内容

    def publish(self, item: Item) -> str:
        """把内容挂到一个新 token 上，返回二维码里要放的路径。"""
        token = secrets.token_hex(8)
        with self._lock:
            self._routes.clear()
            self._routes[token] = item
        return f"/{item.segment}/{token}"

    def lookup(self, token: str) -> Item | None:
        with self._lock:
            return self._routes.get(token)

    def base_url(self, host: str | None = None) -> str:
        return f"http://{host or lan_ips()[0]}:{self.port}"
