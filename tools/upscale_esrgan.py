# -*- coding: utf-8 -*-
"""把一张图用 RealESRGAN 超分放大（本机、一次一个任务）。

用法：python tools/upscale_esrgan.py <源图> [目标宽] [输出文件]

说明：跑之前面板和 ComfyUI 要开着 —— 这个脚本复用面板自己的 ComfyUI 连接、模型目录与产物目录。
"""
import os
import shutil
import sys
import time

import cv2
import numpy as np

# panel.py 在仓库根目录，本文件在 tools/ 下 → 上溯一层才 import 得到它
WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WS)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import panel  # noqa: E402

if len(sys.argv) < 2:
    print("用法: python tools/upscale_esrgan.py <源图> [目标宽] [输出文件]")
    raise SystemExit(2)
SRC = sys.argv[1]
TARGET_W = int(sys.argv[2]) if len(sys.argv) > 2 else 2752
MODEL = "RealESRGAN_x4.pth"
_base = os.path.splitext(os.path.basename(SRC))[0]

name = "upscale_src_%d.png" % int(time.time())
dst_in = os.path.join(panel.INPUT_DIR, name)
shutil.copyfile(SRC, dst_in)
print("  已放进输入目录:", name)

graph = {
    "1": {"class_type": "LoadImage", "inputs": {"image": name}},
    "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": MODEL}},
    "3": {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}},
    "4": {"class_type": "SaveImage", "inputs": {"images": ["3", 0], "filename_prefix": "QwenEdit/panel_fuse_up"}},
}
t0 = time.time()
rel, secs, _ = panel.submit_and_wait(graph, lambda m: None)
print("  超分完成 %.0fs -> %s" % (time.time() - t0, rel))

src = os.path.join(panel.OUTPUT_DIR, rel.replace("/", os.sep))
img = cv2.imdecode(np.fromfile(src, dtype=np.uint8), cv2.IMREAD_COLOR)
print("  原始输出:", img.shape[1], "x", img.shape[0])
if img.shape[1] > TARGET_W:                      # ×4 太大就缩到目标宽（保持锐度）
    h = int(img.shape[0] * TARGET_W / img.shape[1])
    img = cv2.resize(img, (TARGET_W, h), interpolation=cv2.INTER_AREA)
    print("  缩到:", img.shape[1], "x", img.shape[0])

out = sys.argv[3] if len(sys.argv) > 3 else os.path.join(os.path.dirname(os.path.abspath(SRC)),
                                                         _base + "_up2x.png")
cv2.imencode(".png", img)[1].tofile(out)
print("  成品:", out, "%.1f MB" % (os.path.getsize(out) / 1024 / 1024))

# 同时留一份到作品栏（output/QwenEdit），名字跟着源图走，免得每次覆盖同一个文件
keep = os.path.join(panel.OUTPUT_DIR, "QwenEdit", _base + "_up2x.png")
cv2.imencode(".png", img)[1].tofile(keep)
print("  作品栏一份:", keep)
