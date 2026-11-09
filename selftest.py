# -*- coding: utf-8 -*-
"""自检：不启动界面，验证二维码容量、页面渲染、本地服务与文件传输。

跑法：python selftest.py
注意最后一步会清空 %TEMP%\\text2qr，跑之前别开着软件。
"""
import os
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image

import text2qr as t
from share_server import (Item, ShareServer, human_size, item_from_path,
                          render_file_page, render_image_page, render_text_page)

WORK = tempfile.mkdtemp(prefix="t2q-test-")


def http(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.status, dict(r.headers), r.read()


def code_of(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def check_qr(text, label):
    img = t.make_qr_image(text)
    print(f"  {label:14s} 字节={len(text.encode('utf-8')):5d}  图片={img.size[0]}x{img.size[1]}")
    return img


# ---------------------------------------------------------------- 夹具

JPEG = os.path.join(WORK, "海边 日落.jpg")          # 故意带中文和空格
Image.new("RGB", (320, 240), "#8fb8d8").save(JPEG, "JPEG", quality=92)
img_item = item_from_path(JPEG)

PDF = os.path.join(WORK, "年度报告.pdf")
pdf_bytes = b"%PDF-1.4\n" + bytes(range(256)) * 40
with open(PDF, "wb") as f:
    f.write(pdf_bytes)
file_item = item_from_path(PDF)


print("=== 尺寸格式化 ===")
assert human_size(0) == "0 B"
assert human_size(999) == "999 B"
assert human_size(1024) == "1.0 KB"
assert human_size(5 * 1024 * 1024) == "5.0 MB"
print("  B / KB / MB 换算正常")


print("\n=== 类型识别 ===")
assert img_item.kind == "image" and img_item.mime == "image/jpeg"
assert file_item.kind == "file" and file_item.mime == "application/pdf"
assert img_item.segment == "i" and file_item.segment == "f"
assert Item(kind="text").segment == "t"
print(f"  jpg → {img_item.kind}   pdf → {file_item.kind}")


print("\n=== 二维码容量 ===")
check_qr("hello", "英文短串")
check_qr("你好，这是一段测试文字。", "中文短句")
check_qr("短" * 200, "中文 200 字")          # 600 字节，正好是直接编码上限
check_qr("a" * 600, "英文 600 字节")
try:
    check_qr("超" * 3000, "超长文本")          # 9000 字节，应当抛出异常由界面兜住
    print("  !! 超长文本竟然成功了，需要检查")
except Exception as e:
    print(f"  超长文本按预期失败：{type(e).__name__}")


print("\n=== 页面渲染 ===")
page = render_text_page("<b>加粗</b> & 换行\n第二行")
assert "&lt;b&gt;加粗&lt;/b&gt;" in page, "HTML 未转义，存在注入风险"
assert "第二行" in page and "复制全部" in page
print("  文本页：转义正常，含复制按钮")

ipage = render_image_page(img_item, "tok123")
assert "/i/tok123?raw=1" in ipage and "/i/tok123?dl=1" in ipage
assert "长按" in ipage
print("  图片页：含原图直链与下载入口")

fpage = render_file_page(file_item, "tok123")
assert "年度报告.pdf" in fpage and "PDF" in fpage
assert "/f/tok123" in fpage
print("  文件页：文件名与类型徽章正常")


print("\n=== 本地服务 ===")
srv = ShareServer()
port = srv.start()
base = f"http://127.0.0.1:{port}"
print("  端口:", port)

# 文本
p = srv.publish(Item(kind="text", text="局域网取文字测试\n第二行"))
status, headers, body = http(base + p)
assert status == 200 and "局域网取文字测试" in body.decode("utf-8")
print("  文本路由正常")

# 图片：字节必须和源文件一模一样，证明没有二次压缩
p = srv.publish(img_item)
status, headers, body = http(base + p + "?raw=1")
assert status == 200 and headers["Content-Type"].startswith("image/")
with open(JPEG, "rb") as f:
    assert body == f.read(), "图片字节被改动，画质有损"
print(f"  图片原样送出 {len(body)} 字节（未转码）")

status, headers, body = http(base + p + "?dl=1")
assert "attachment" in headers["Content-Disposition"]
print("  图片下载头正常")

# 文件：中文名不能乱码
p = srv.publish(file_item)
status, headers, body = http(base + p)
assert status == 200 and body == pdf_bytes
cd = headers["Content-Disposition"]
assert "attachment" in cd and "filename*=UTF-8''" in cd
assert urllib.parse.quote("年度报告.pdf", safe="") in cd
print("  文件下载正常，中文名已按 RFC 5987 编码")

# Range：手机下载器会分段请求，不支持就会整个重下
total = len(pdf_bytes)
status, headers, body = http(base + p, {"Range": "bytes=100-199"})
assert status == 206, f"Range 未生效，返回 {status}"
assert len(body) == 100 and body == pdf_bytes[100:200]
assert headers["Content-Range"] == f"bytes 100-199/{total}"
print(f"  断点续传正常：bytes 100-199/{total}")

status, headers, body = http(base + p, {"Range": "bytes=-50"})
assert status == 206 and body == pdf_bytes[-50:]
print("  尾部区间（bytes=-50）正常")

# 失效与越权
assert code_of(base + "/t/deadbeef") == 404, "不存在的 token 应返回 404"
token = p.split("/")[-1]
assert code_of(base + "/i/" + token) == 404, "类型与内容不符应返回 404"
print("  错误 token / 类型不符均返回 404")

old = srv.publish(Item(kind="text", text="旧内容"))
new = srv.publish(Item(kind="text", text="新内容"))
assert code_of(base + old) == 404, "换内容后旧链接必须立刻失效"
assert "新内容" in http(base + new)[2].decode("utf-8")
print("  换内容后旧二维码立即失效")

# 原文件被移走
GONE = os.path.join(WORK, "gone.txt")
with open(GONE, "wb") as f:
    f.write(b"x")
gp = srv.publish(item_from_path(GONE))
os.remove(GONE)
assert code_of(base + gp) == 410, "原文件消失应返回 410"
print("  原文件被移走时返回 410（提示重新拖入）")

srv.stop()


# ---------------------------------------------------------------- 接收通道

import json
import time

import config as cfgmod
from share_server import MAX_UPLOAD, safe_name, unique_path


def post(url, data, ctype="application/octet-stream"):
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, dict(r.headers), r.read()


def post_code(url, data=b"", ctype="application/octet-stream"):
    try:
        return post(url, data, ctype)[0]
    except urllib.error.HTTPError as e:
        return e.code


print("\n=== 文件名净化 ===")
assert safe_name("../../etc/passwd") == "passwd", "路径穿越没挡住"
assert safe_name("C:\\Windows\\系统\\shell.dll") == "shell.dll", "盘符路径没剥掉"
assert safe_name('a<b>c:d"e|f?.txt') == "abcdef.txt", "非法字符没剔干净"
assert safe_name("CON.txt") == "_CON.txt", "Windows 保留名没拦"
assert safe_name("NUL") == "_NUL"
assert safe_name("   ") == "未命名"
assert safe_name("会议 纪要（终稿）.pdf") == "会议 纪要（终稿）.pdf", "合法中文名被改坏"
p1 = unique_path(WORK, "重复.txt")
with open(p1, "w", encoding="utf-8") as f:
    f.write("x")
p2 = unique_path(WORK, "重复.txt")
assert os.path.basename(p2) == "重复(1).txt", "重名没有自动加序号"
print("  穿越 / 盘符 / 非法字符 / 保留名 / 中文 / 重名 六种情况均正常")


print("\n=== 配置持久化 ===")
old_cfg = cfgmod.load()
target = os.path.join(WORK, "我的接收")
ok, err = cfgmod.set_save_dir(target)
assert ok, f"合法目录被拒：{err}"
assert cfgmod.load()["save_dir"] == target, "配置没有落盘"
blocker = os.path.join(WORK, "这是个文件不是目录")
with open(blocker, "w", encoding="utf-8") as f:
    f.write("x")
ok, err = cfgmod.set_save_dir(blocker)
assert not ok, "指向文件的路径必须被拒绝"
cfgmod.save({"save_dir": blocker})          # 模拟用户把文件夹删了
assert cfgmod.load()["save_dir"] != blocker, "坏路径没有回退到默认目录"
assert os.path.isdir(cfgmod.load()["save_dir"]), "回退目录不存在"
cfgmod.save({"save_dir": old_cfg["save_dir"]})   # 还原，别污染本机配置（直接写，不重复探测真目录）
assert cfgmod.load()["save_dir"] == old_cfg["save_dir"]
print(f"  记住 {target}")
print("  不可写 / 已失效的路径会自动回退，不会让程序起不来")


print("\n=== 接收通道 ===")
INBOX = os.path.join(WORK, "收件箱")
srv2 = ShareServer()
port2 = srv2.start()
base2 = f"http://127.0.0.1:{port2}"
inbox = srv2.attach_inbox(INBOX, None)
rtok = inbox.token
R = f"{base2}/r/{rtok}"
print("  端口:", port2, "token 长度:", len(rtok))

status, headers, body = http(R)
assert status == 200 and "发给电脑" in body.decode("utf-8")
assert "/r/" + rtok in body.decode("utf-8")
print("  接收页正常，页面里带本次 token")

assert post_code(f"{base2}/r/deadbeef/u?name=x.txt", b"hi") == 404, "错误 token 必须拒绝"
assert code_of(base2 + "/r/" + rtok + "/u") == 404, "不存在读取文件的 GET 入口"
print("  错误 token 返回 404，且没有任何读文件的 GET 入口")

payload = bytes(range(256)) * 1200          # 307200 字节
pname = "会议 纪要.pdf"
status, headers, body = post(f"{R}/u?name={urllib.parse.quote(pname)}", payload)
d = json.loads(body)
assert d["name"] == pname, f"文件名被改坏：{d['name']}"
day = os.path.join(INBOX, time.strftime("%Y-%m-%d"))
saved = os.path.join(day, pname)
assert os.path.isfile(saved), "文件没有落到按日期分的子目录里"
with open(saved, "rb") as f:
    assert f.read() == payload, "收到的字节和发出的不一致"
assert not os.path.exists(saved + ".part"), "残留了半截临时文件"
print(f"  上传 {len(payload)} 字节 → {time.strftime('%Y-%m-%d')}/{pname}（逐字节一致）")

status, headers, body = post(f"{R}/u?name={urllib.parse.quote(pname)}", payload)
assert json.loads(body)["name"] == "会议 纪要(1).pdf", "同名文件被覆盖了"
print("  重名自动变成「会议 纪要(1).pdf」，不覆盖原件")

txt = "待办清单\n一、买菜\n二、修灯"
status, headers, body = post(f"{R}/t", txt.encode("utf-8"))
tname = json.loads(body)["name"]
with open(os.path.join(day, tname), "rb") as f:
    raw = f.read()
assert raw.startswith(b"\xef\xbb\xbf"), "文字没有存成带 BOM 的 UTF-8，记事本会乱码"
assert raw[3:].decode("utf-8") == txt
assert tname == "待办清单.txt", f"文字文件名没按首行生成：{tname}"
print(f"  文字存成 {tname}（UTF-8 BOM，记事本可读）")

status, headers, body = post(
    f"{R}/u?name={urllib.parse.quote('../../逃逸.txt')}", b"x")
assert os.path.isfile(os.path.join(day, "逃逸.txt")), "净化后的文件名不对"
assert not os.path.exists(os.path.join(WORK, "逃逸.txt")), "文件被写到目标目录外面了"
print("  带 ../ 的文件名被剥成「逃逸.txt」，没有逃出目标目录")

with open(JPEG, "rb") as f:
    jpg_bytes = f.read()
status, headers, body = post(
    f"{R}/u?name={urllib.parse.quote('海边 日落.jpg')}", jpg_bytes, "image/jpeg")
assert json.loads(body)["name"] == "海边 日落.jpg"
print("  图片按原字节收下，扩展名和中文名都保留")

big = os.urandom(5 * 1024 * 1024)
status, headers, body = post(f"{R}/u?name=big.bin", big)
with open(os.path.join(day, "big.bin"), "rb") as f:
    assert f.read() == big, "5MB 分块写入有偏差"
print("  5 MB 文件分块写入，字节一致")

inbox.max_bytes = 1024
assert post_code(f"{R}/u?name=too-big.bin", b"x" * 4096) == 413
inbox.max_bytes = MAX_UPLOAD
assert not os.path.exists(os.path.join(day, "too-big.bin"))
print("  超过上限返回 413，且不留下任何文件")

status, headers, body = http(R + "/list")
d = json.loads(body)
assert d["count"] >= 4, f"计数不对：{d['count']}"
labels = {it["label"] for it in d["items"]}
assert labels == {"图片", "文件", "文字"}, f"类型标签不全：{labels}"
assert all("path" not in it for it in d["items"]), "列表里泄露了电脑上的绝对路径"
print(f"  列表返回 {d['count']} 条，含路径不外泄")

srv2.stop()
print("  服务停止后接收页不再可访问")


print("\n=== 临时目录 ===")
d = t.ensure_temp_dir()
assert d == os.path.join(tempfile.gettempdir(), "text2qr")
probe = os.path.join(d, "probe-test.png")
with open(probe, "wb") as f:
    f.write(b"x")
assert os.path.exists(probe)
t.purge_temp()
assert not os.path.exists(probe), "清理没有生效"
print(f"  路径随系统变化：{d}")
print("  清理只作用于 text2qr 子目录，未触碰系统临时目录其它文件")

shutil.rmtree(WORK, ignore_errors=True)
print("\n全部检查通过")
