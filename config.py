# -*- coding: utf-8 -*-
"""配置持久化：目前只存接收目录。

存到 %APPDATA%\\林克传输助手\\config.json —— 同一台电脑重开软件还在，
换电脑时 %APPDATA% 会指向新机器的位置，不会带着别人的路径跑。

目录读不到、没权限、被删掉时一律回退到默认目录，绝不因为配置坏了启动失败。
"""

import json
import os
import shutil
import tempfile
import time

APP_DIR = "林克传输助手"
LEGACY_APP_DIRS = ("文字转二维码",)   # 改名前的老目录，启动时自动迁移
CONFIG_NAME = "config.json"
DEFAULT_FOLDER_NAME = "手机接收"


# ---------------------------------------------------------------- 路径


def config_dir() -> str:
    base = os.getenv("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_DIR)


def config_path() -> str:
    return os.path.join(config_dir(), CONFIG_NAME)


def _migrate_legacy() -> None:
    """改名前的配置搬过来，免得用户设过的保存路径凭空消失。

    只在「新位置还没有配置」时搬一次，之后各走各的，不会反复覆盖。
    """
    try:
        if os.path.isfile(config_path()):
            return
        base = os.getenv("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
        for old in LEGACY_APP_DIRS:
            old_path = os.path.join(base, old, CONFIG_NAME)
            if os.path.isfile(old_path):
                os.makedirs(config_dir(), exist_ok=True)
                shutil.copy2(old_path, config_path())
                return
    except OSError:
        pass  # 迁移失败就按没有配置处理，功能不受影响


def default_save_dir() -> str:
    """优先「我的文档」，没有就退到用户主目录。

    中文 Windows 的文档夹实际叫「文档」，两个名字都要试。
    """
    home = os.path.expanduser("~")
    for name in ("Documents", "文档"):
        docs = os.path.join(home, name)
        if os.path.isdir(docs):
            return os.path.join(docs, DEFAULT_FOLDER_NAME)
    return os.path.join(home, DEFAULT_FOLDER_NAME)


def today_dir(root: str) -> str:
    """按日期分子目录，否则几千个文件会堆在一个文件夹里。"""
    return os.path.join(root, time.strftime("%Y-%m-%d"))


# ---------------------------------------------------------------- 可写性


PROBE_NAME = ".text2qr-write-test"


def ensure_writable(path: str, strict: bool = False) -> tuple[bool, str]:
    """判断目录能不能写。

    目录已经存在时默认不做写探测 —— 每次开软件都往用户文件夹里造一个临时文件，
    万一删不掉就留成垃圾。只有目录是这次新建的，或者调用方明确要求（改路径时），
    才真的写一次，并且用固定文件名，最多留一个。
    """
    if not path:
        return False, "目录为空"

    existed = os.path.isdir(path)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        return False, f"无法创建目录：{e.strerror or e}"

    if existed and not strict:
        # 上次探测文件没删干净（程序被强杀之类）时顺手收拾掉，名字是固定的，不会误伤
        stale = os.path.join(path, PROBE_NAME)
        if os.path.isfile(stale):
            try:
                os.remove(stale)
            except OSError:
                pass
        return True, ""

    probe = os.path.join(path, PROBE_NAME)
    try:
        with open(probe, "wb") as f:
            f.write(b"ok")
    except OSError as e:
        return False, f"目录不可写：{e.strerror or e}"
    try:
        os.remove(probe)
    except OSError:
        pass  # 删不掉不算失败，反正名字固定，不会越攒越多
    return True, ""


# ---------------------------------------------------------------- 读写


def load() -> dict:
    """读配置。任何异常都当成「没有配置」，不让它影响启动。"""
    _migrate_legacy()
    path = config_path()
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except (OSError, ValueError):
            data = {}

    save_dir = data.get("save_dir") or ""
    ok, _ = ensure_writable(save_dir) if save_dir else (False, "")
    if not ok:
        save_dir = default_save_dir()          # 路径失效（删了 / 换盘 / 拔了移动硬盘）
        ok, _ = ensure_writable(save_dir)
        if not ok:
            save_dir = os.path.join(tempfile.gettempdir(), DEFAULT_FOLDER_NAME)
            os.makedirs(save_dir, exist_ok=True)

    data["save_dir"] = save_dir
    return data


def save(cfg: dict) -> None:
    """写配置。写不进去只影响「下次记住」，不该打断当前使用。"""
    try:
        os.makedirs(config_dir(), exist_ok=True)
        tmp = config_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, config_path())
    except OSError:
        pass


def set_save_dir(new_dir: str) -> tuple[bool, str]:
    """改接收目录。先真写一次验证，通过才落盘，避免存进一个不能用的路径。"""
    ok, err = ensure_writable(new_dir, strict=True)
    if not ok:
        return False, err
    cfg = load()
    cfg["save_dir"] = new_dir
    save(cfg)
    return True, ""
