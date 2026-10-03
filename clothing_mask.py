# -*- coding: utf-8 -*-
"""衣服区域遮罩：躯干 + 手臂区域，但排除头脸和手（手用肤色分割定位）。
目的：换衣时只重绘衣服区域，脸/手/脚/背景从原图原样贴回，避免多手指、多脚。"""
import json
import os

import cv2
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))


def _insightface_root():
    """insightface 模型目录：环境变量 > panel_config.json 的 insightface_dir > 同目录 models/insightface。"""
    p = (os.environ.get("INSIGHTFACE_ROOT") or "").strip()
    if p:
        return p
    try:
        with open(os.path.join(_HERE, "panel_config.json"), "r", encoding="utf-8") as fh:
            v = (json.load(fh) or {}).get("insightface_dir") or ""
        if v:
            return v
    except (OSError, ValueError):
        pass
    return os.path.join(_HERE, "models", "insightface")


ROOT = _insightface_root()


def face_box(img):
    from insightface.app import FaceAnalysis
    app = FaceAnalysis(name="buffalo_l", root=ROOT, allowed_modules=["detection"],
                       providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=-1, det_size=(640, 640))
    faces = app.get(img)
    if not faces:
        return None
    f = max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
    return [float(v) for v in f.bbox]


def skin_hands(img, fb):
    """只保护『手』：手是四肢末端，用"离脸中心最远的那一段"定位；
    整块紧凑的肤色块也直接判为手。胸前/手臂的皮肤不保护，好让衣服能盖上去。"""
    h, w = img.shape[:2]
    ycrcb = cv2.cvtColor(img, cv2.COLOR_BGR2YCrCb)
    skin = cv2.inRange(ycrcb, (0, 133, 77), (255, 180, 130))
    skin = cv2.morphologyEx(skin, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8), iterations=1)
    skin = cv2.morphologyEx(skin, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    if fb:
        x1, y1, x2, y2 = [int(v) for v in fb]
        fw, fh = x2 - x1, y2 - y1
        cv2.rectangle(skin, (max(0, int(x1 - fw * 0.6)), max(0, int(y1 - fh * 0.9))),
                      (min(w, int(x2 + fw * 0.6)), min(h, int(y2 + fh * 0.30))), 0, -1)
    cx = (x1 + x2) / 2.0 if fb else w / 2.0
    cy = (y1 + y2) / 2.0 if fb else h * 0.25
    ys, xs = np.mgrid[0:h, 0:w]
    dist = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2).astype(np.float32)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(skin, 8)
    keep = np.zeros_like(skin)
    cnt = 0
    for i in range(1, n):
        area = stats[i, cv2.CC_STAT_AREA]
        if area < (h * w) * 0.0004:
            continue
        bw, bh = stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT]
        long_side, short_side = max(bw, bh), max(1, min(bw, bh))
        fill = area / float(bw * bh)
        sel = labels == i
        if fill > 0.42 and long_side < min(h, w) * 0.28:
            keep[sel] = 255                     # 紧凑的小块：就是一只手
            cnt += 1
            continue
        d = dist[sel]
        thr = d.min() + 0.68 * (d.max() - d.min())
        hand_pixels = sel & (dist >= thr)
        if hand_pixels.sum() < 40:
            continue
        keep[hand_pixels] = 255
        cnt += 1
    k = max(3, (int(min(h, w) * 0.012) | 1))
    keep = cv2.dilate(keep, np.ones((k, k), np.uint8), iterations=1)
    return keep, cnt


def clothing_mask(img, wide=1.95, tall=4.7, gen=None):
    """返回 (mask, 说明)。gen 给出生成图时，会用"生成图与原图的差异"自动补全衣服真正被改动的范围。"""
    h, w = img.shape[:2]
    mask = np.zeros((h, w), np.uint8)
    notes = []
    fb = face_box(img)
    if not fb:
        # 没有脸就退回"画面中部横带"，仍排除上下边缘
        cv2.rectangle(mask, (int(w * 0.15), int(h * 0.3)), (int(w * 0.85), int(h * 0.85)), 255, -1)
        notes.append("没检测到人脸 → 用画面中部横带")
    else:
        x1, y1, x2, y2 = fb
        fw, fh = x2 - x1, y2 - y1
        tx1, tx2 = max(0, int(x1 - fw * wide)), min(w, int(x2 + fw * wide))
        ty1, ty2 = max(0, int(y2 - fh * 0.12)), min(h, int(y2 + fh * tall))
        cv2.rectangle(mask, (tx1, ty1), (tx2, ty2), 255, -1)
        notes.append(f"躯干+手臂带 ({tx2-tx1}×{ty2-ty1})")
        # 排除头脸
        cv2.rectangle(mask, (max(0, int(x1 - fw * 0.7)), max(0, int(y1 - fh * 1.1))),
                      (min(w, int(x2 + fw * 0.7)), min(h, int(y2 + fh * 0.02))), 0, -1)
        notes.append("已扣掉头脸")
    # —— 数据驱动补全：把"生成图相对原图真正改动过"的大块并进遮罩 ——
    # 这样裙摆/袖子边缘不会残留原衣服，而手（生成图里基本没动）自然落回原像素。
    if gen is not None:
        try:
            g = gen if gen.shape[:2] == (h, w) else cv2.resize(gen, (w, h), interpolation=cv2.INTER_LANCZOS4)
            diff = np.abs(g.astype(np.int16) - img.astype(np.int16)).max(axis=2).astype(np.uint8)
            diff = cv2.blur(diff, (7, 7))
            dd = (diff > 16).astype(np.uint8) * 255
            dd = cv2.morphologyEx(dd, cv2.MORPH_CLOSE, np.ones((13, 13), np.uint8), iterations=2)
            dd = cv2.morphologyEx(dd, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
            ref = cv2.dilate(mask, np.ones((41, 41), np.uint8))     # 只接受贴着躯干带的块
            n2, l2, s2, _ = cv2.connectedComponentsWithStats(dd, 8)
            add = np.zeros_like(dd)
            for i in range(1, n2):
                if s2[i, cv2.CC_STAT_AREA] < (h * w) * 0.002:
                    continue
                sel = l2 == i
                if not (ref[sel] > 0).any():
                    continue
                add[sel] = 255
            if add.any():
                before = float((mask > 0).mean()) * 100
                mask = cv2.bitwise_or(mask, add)
                notes.append(f"按生成差异补全 {before:.0f}%→{float((mask>0).mean())*100:.0f}%")
        except Exception as e:
            notes.append(f"差异补全跳过({type(e).__name__})")

    hands, hcnt = skin_hands(img, fb)
    if hcnt:
        mask = cv2.bitwise_and(mask, cv2.bitwise_not(hands))
        notes.append(f"已保护 {hcnt} 处肤色区（手/裸露皮肤）")
    # 排除画面最下方 2%（鞋所在）
    mask[int(h * 0.98):, :] = 0
    if mask.sum() == 0:
        return None, " · ".join(notes) + " -> 遮罩为空"
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((19, 19), np.uint8))
    # 羽化小一点，减少遮罩边缘残留的原衣服像素
    mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=max(4, min(h, w) // 110))
    cov = (mask > 30).mean() * 100
    return mask, " · ".join(notes) + f" · 覆盖 {cov:.0f}%"


def composite(orig, gen, mask):
    """把生成图按遮罩贴回原图（遮罩外一律用原图像素）"""
    if orig.shape[:2] != gen.shape[:2]:
        gen = cv2.resize(gen, (orig.shape[1], orig.shape[0]), interpolation=cv2.INTER_LANCZOS4)
    a = (mask.astype(np.float32) / 255.0)[:, :, None]
    return np.clip(orig.astype(np.float32) * (1 - a) + gen.astype(np.float32) * a, 0, 255).astype(np.uint8)


def protect_mask(img):
    """『不许被重绘』的区域：头脸 + 裸露皮肤（含手）+ 画面底部(鞋)。
    用于"整幅重绘换衣"之后，把脸/手/脚按原像素贴回来。"""
    h, w = img.shape[:2]
    keep = np.zeros((h, w), np.uint8)
    fb = face_box(img)
    notes = []
    if fb:
        x1, y1, x2, y2 = [float(v) for v in fb]
        fw, fh = x2 - x1, y2 - y1
        cv2.rectangle(keep, (max(0, int(x1 - fw * 0.55)), max(0, int(y1 - fh * 0.85))),
                      (min(w, int(x2 + fw * 0.55)), min(h, int(y2 + fh * 0.30))), 255, -1)
        notes.append("头脸")
    ycrcb = cv2.cvtColor(img, cv2.COLOR_BGR2YCrCb)
    skin = cv2.inRange(ycrcb, (0, 133, 77), (255, 180, 130))
    skin = cv2.morphologyEx(skin, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8), iterations=2)
    skin = cv2.morphologyEx(skin, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    if fb:
        x1, y1, x2, y2 = [int(v) for v in fb]
        fw, fh = x2 - x1, y2 - y1
        cv2.rectangle(skin, (max(0, int(x1 - fw * 0.6)), max(0, int(y1 - fh * 1.0))),
                      (min(w, int(x2 + fw * 0.6)), min(h, int(y2 + fh * 0.35))), 0, -1)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(skin, 8)
    cnt = 0
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] < (h * w) * 0.0004:
            continue
        keep[labels == i] = 255
        cnt += 1
    if cnt:
        notes.append(f"{cnt} 处裸露皮肤(含手)")
    keep[int(h * 0.985):, :] = 255
    notes.append("鞋")
    k = max(3, (int(min(h, w) * 0.010) | 1))
    keep = cv2.GaussianBlur(cv2.dilate(keep, np.ones((k, k), np.uint8)), (0, 0),
                            sigmaX=max(3, min(h, w) // 140))
    return keep, " · ".join(notes)


if __name__ == "__main__":
    # 命令行自测：python clothing_mask.py <图片路径> [输出预览路径]
    import sys
    if len(sys.argv) < 2:
        print("用法: python clothing_mask.py <图片路径> [输出预览路径]")
        raise SystemExit(2)
    src = sys.argv[1]
    img = cv2.imdecode(np.fromfile(src, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        print("读不到图片:", src)
        raise SystemExit(1)
    m, note = clothing_mask(img)
    print("说明:", note)
    out = img.copy()
    if m is not None:
        ov = np.zeros_like(img); ov[:, :, 2] = 255
        a = (m.astype(np.float32) / 255)[:, :, None] * 0.5
        out = (img * (1 - a) + ov * a).astype(np.uint8)
    dst = sys.argv[2] if len(sys.argv) > 2 else os.path.join(_HERE, "mask_preview.png")
    ok, buf = cv2.imencode(os.path.splitext(dst)[1] or ".png", out)
    if ok:
        buf.tofile(dst)
    print("预览:", dst)
