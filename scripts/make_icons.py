#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成应用图标 ICON.PNG(64x64) / ICON_256.PNG(256x256)。

纯标准库（zlib/struct/math）手写光栅 + PNG 编码，不依赖 Pillow/numpy：
    python3 scripts/make_icons.py

设计：深蓝圆角底 + 叠放的照片卡（相册缩略图）+ 右下角绿色向下箭头（落地备份）。
"""

import math
import os
import struct
import zlib

S = 1024                                  # 超采样画布，最后盒式缩小抗锯齿
OUT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TOP = (43, 124, 240)                      # #2b7cf0
BOTTOM = (17, 85, 184)                    # #1155b8
GREEN = (52, 199, 89)                     # #34c759
YELLOW = (255, 205, 92)

buf = bytearray(S * S * 4)                # RGBA，初始全透明


# ---------------------------------------------------------------- 光栅化
def _over(i, r, g, b, a):
    """把 (r,g,b,a) 以 source-over 混合到 buffer 的第 i 个像素。"""
    if a <= 0:
        return
    da = buf[i + 3]
    if a >= 255 and da in (0, 255):
        buf[i], buf[i + 1], buf[i + 2], buf[i + 3] = r, g, b, a
        return
    sa = a / 255.0
    if da == 0:
        buf[i], buf[i + 1], buf[i + 2] = r, g, b
        buf[i + 3] = a
        return
    da_f = da / 255.0
    oa = sa + da_f * (1 - sa)
    if oa <= 0:
        return
    for k, c in enumerate((r, g, b)):
        dst = buf[i + k]
        buf[i + k] = int(round((c * sa + dst * da_f * (1 - sa)) / oa))
    buf[i + 3] = int(round(oa * 255))


def fill(y0, y1, xrange_fn, color, alpha=255):
    """按行填充；color 可以是 (r,g,b) 或 callable(y) -> (r,g,b)。"""
    for y in range(max(0, int(y0)), min(S - 1, int(y1)) + 1):
        xs, xe = xrange_fn(y)
        if xs is None:
            continue
        x0 = max(0, int(math.ceil(xs)))
        x1 = min(S - 1, int(math.floor(xe)))
        if x1 < x0:
            continue
        c = color(y) if callable(color) else color
        base = y * S
        for x in range(x0, x1 + 1):
            _over((base + x) * 4, c[0], c[1], c[2], alpha)


def rounded_rect(x0, y0, x1, y1, r):
    r = min(r, (x1 - x0) / 2.0, (y1 - y0) / 2.0)

    def fn(y):
        inset = 0.0
        if y < y0 + r:
            dy = (y0 + r) - y
            inset = r - math.sqrt(max(0.0, r * r - dy * dy))
        elif y > y1 - r:
            dy = y - (y1 - r)
            inset = r - math.sqrt(max(0.0, r * r - dy * dy))
        return x0 + inset, x1 - inset

    return fn


def circle(cx, cy, r):
    def fn(y):
        dy = y - cy
        if abs(dy) > r:
            return None, None
        d = math.sqrt(max(0.0, r * r - dy * dy))
        return cx - d, cx + d

    return fn


def triangle(p1, p2, p3):
    pts = (p1, p2, p3)

    def fn(y):
        xs = []
        for i in range(3):
            a, b = pts[i], pts[(i + 1) % 3]
            if a[1] == b[1]:
                continue
            if min(a[1], b[1]) <= y <= max(a[1], b[1]):
                t = (y - a[1]) / float(b[1] - a[1])
                xs.append(a[0] + t * (b[0] - a[0]))
        if len(xs) < 2:
            return None, None
        return min(xs), max(xs)

    return fn


# ---------------------------------------------------------------- 缩小 + 编码
def resize(src, n):
    factor = S // n
    out = bytearray(n * n * 4)
    area = factor * factor
    for y in range(n):
        for x in range(n):
            r = g = b = a = 0
            for dy in range(factor):
                row = (y * factor + dy) * S
                for dx in range(factor):
                    i = (row + x * factor + dx) * 4
                    al = src[i + 3]
                    if al:
                        r += src[i] * al
                        g += src[i + 1] * al
                        b += src[i + 2] * al
                        a += al
            j = (y * n + x) * 4
            if a:
                out[j] = r // a
                out[j + 1] = g // a
                out[j + 2] = b // a
                out[j + 3] = a // area
    return out


# ---------------------------------------------------------------- 绘制
def draw():
    # 背景：圆角 + 垂直渐变
    def grad(y):
        t = y / (S - 1.0)
        return (int(TOP[0] + (BOTTOM[0] - TOP[0]) * t),
                int(TOP[1] + (BOTTOM[1] - TOP[1]) * t),
                int(TOP[2] + (BOTTOM[2] - TOP[2]) * t))

    fill(0, S - 1, rounded_rect(0, 0, S - 1, S - 1, int(S * 0.22)), grad)

    # 后面两张淡出的卡片（"一叠缩略图"）
    fill(230, 690, rounded_rect(220, 230, 770, 690, 44), (255, 255, 255), 70)
    fill(265, 725, rounded_rect(185, 265, 735, 725, 44), (255, 255, 255), 110)

    # 主卡片
    fill(300, 760, rounded_rect(150, 300, 700, 760, 44), (255, 255, 255))

    # 卡片内的"缩略图"：天空 / 太阳 / 两层山
    fill(330, 730, rounded_rect(180, 330, 670, 730, 26), (214, 232, 255))
    fill(380, 480, circle(550, 430, 50), YELLOW)
    fill(500, 730, triangle((180, 730), (400, 500), (560, 700)), (30, 110, 200))
    fill(560, 730, triangle((380, 730), (540, 560), (670, 730)), (20, 80, 160))

    # 右下角"落地备份"徽标：白描边 + 绿圆 + 向下箭头
    cx, cy, r = 720, 730, 190
    fill(cy - r - 18, cy + r + 18, circle(cx, cy, r + 18), (255, 255, 255))
    fill(cy - r, cy + r, circle(cx, cy, r), GREEN)
    aw = int(r * 0.16)
    fill(cy - int(r * 0.62), cy + int(r * 0.12),
         rounded_rect(cx - aw, cy - int(r * 0.62), cx + aw, cy + int(r * 0.12), aw),
         (255, 255, 255))
    fill(cy + int(r * 0.02), cy + int(r * 0.62),
         triangle((cx - int(r * 0.46), cy + int(r * 0.02)),
                  (cx + int(r * 0.46), cy + int(r * 0.02)),
                  (cx, cy + int(r * 0.62))), (255, 255, 255))


def add_srgb_chunk(png_bytes):
    """在 IHDR 之后插入 sRGB + gAMA，提升第三方解码器兼容性（fnOS 官方图标也带）。"""
    import struct, zlib

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr_end = 8 + 8 + 13 + 4          # 签名 + (len+tag) + IHDR + CRC
    extra = chunk(b"sRGB", b"\x00") + chunk(b"gAMA", struct.pack(">I", 45455))
    return png_bytes[:ihdr_end] + extra + png_bytes[ihdr_end:]


def write(path, size, data):
    if not path.endswith(".png") and not path.endswith(".PNG"):
        path = path
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)
        raw += data[y * stride:(y + 1) * stride]

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    blob = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))
    blob = add_srgb_chunk(blob)
    with open(path, "wb") as f:
        f.write(blob)
    print("写出 %s (%dx%d, %d 字节)" % (path, size, size, len(blob)))


def main():
    draw()
    big = resize(buf, 256)
    small = resize(buf, 64)

    # 应用根目录：fnOS 应用中心列表取 /app-center-static/icon/<app>/icon.png，
    # 实际映射到 ICON.PNG；与 fn-ups-manager 一致，它也是 256x256。
    write(os.path.join(OUT_DIR, "ICON.PNG"), 256, big)
    write(os.path.join(OUT_DIR, "ICON_256.PNG"), 256, big)

    # 桌面入口图标（app/ui/config 里的 images/icon_{0}.png）
    img_dir = os.path.join(OUT_DIR, "app", "ui", "images")
    os.makedirs(img_dir, exist_ok=True)
    write(os.path.join(img_dir, "icon_64.png"), 64, small)
    write(os.path.join(img_dir, "icon_256.png"), 256, big)
    write(os.path.join(img_dir, "icon.png"), 256, big)


if __name__ == "__main__":
    main()
