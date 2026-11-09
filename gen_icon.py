# -*- coding: utf-8 -*-
"""从 assets/link-master.png 生成 link.ico（7 个尺寸）与预览图。

母版是一张 900x900 的方形插画。Windows 会在不同场合挑不同的尺寸显示：
任务栏 32、Alt+Tab 48、资源管理器大图标 96~256、标题栏 16，
所以 ico 里必须把 16 / 24 / 32 / 48 / 64 / 128 / 256 全部打包，
只放一张大图的话系统缩出来的小图标会发虚。
"""

import os

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
MASTER = os.path.join(HERE, "assets", "link-master.png")
OUT_ICO = os.path.join(HERE, "link.ico")
OUT_PNG = os.path.join(HERE, "预览-图标.png")

SIZES = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]


def main() -> None:
    if not os.path.isfile(MASTER):
        raise SystemExit(f"缺少母版图：{MASTER}")

    master = Image.open(MASTER).convert("RGB")
    master.save(OUT_ICO, format="ICO", sizes=SIZES)

    got = sorted(Image.open(OUT_ICO).info.get("sizes", []))
    print("link.ico 内含尺寸:", got)
    if len(got) != len(SIZES):
        raise SystemExit(f"尺寸不全，只写进了 {len(got)} 个")

    # 预览图：左边 256 主图，右边把 48/32/16 按最近邻放大，模拟真实像素观感
    sheet = Image.new("RGB", (540, 300), "#f2f3f5")
    sheet.paste(master.resize((256, 256), Image.LANCZOS), (20, 22))
    x = 300
    for s in (48, 32, 16):
        icon = master.resize((s, s), Image.LANCZOS)
        sheet.paste(icon.resize((s * 4, s * 4), Image.NEAREST), (x, 22))
        x += s * 4 + 14
    sheet.save(OUT_PNG)
    print("预览图:", OUT_PNG)


if __name__ == "__main__":
    main()
