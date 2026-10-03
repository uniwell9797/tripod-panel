# -*- coding: utf-8 -*-
"""本地网页面板：选图 → 点一下 → 出结果。
支持：换衣 / 换发型 / 老照片修复上色 / 照片动起来
启动: python panel.py    然后浏览器打开 http://127.0.0.1:8899
"""
import hashlib
import io as _io
import json
import mimetypes
import os
import random
import re
import shutil
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

try:
    from clothing_mask import (clothing_mask as _clothing_mask, composite as _composite,
                               face_box as _face_box, protect_mask as _protect_mask)
except Exception:
    _clothing_mask = None
    _composite = None
    _face_box = None
    _protect_mask = None

# ---- 路径与配置：**不写死任何本机路径**（公开版铁律）------------------------------
# 优先级：环境变量 PANEL_<KEY> > 同目录 panel_config.json > 自动探测。
# 各项含义见 config.example.json 与 README；panel_config.json 属于本机配置，不要提交到仓库。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "panel_config.json")
PORT = int(os.environ.get("PANEL_PORT") or 8899)   # 可用 PANEL_PORT 另起一个实例做验证，不动正在跑的那个
RUNS_DIR = os.path.join(BASE_DIR, "runs")          # 每次生成一条记录：输入图 + 参数 + 输出


def _cfg_raw(key, default=None):
    """读一个配置项：环境变量 PANEL_<KEY> > panel_config.json > 默认值。

    ⚠ 必须用 utf-8-sig：见 load_config() 的说明 —— 带 BOM 的配置以前会被静默忽略，
    第三方第三轮复验指出这里（以及 _cfg_val）没跟着一起修。
    """
    v = os.environ.get("PANEL_" + key.upper())
    if v not in (None, ""):
        return v
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as fh:
            v = (json.load(fh) or {}).get(key, default)
        return default if v is None else v
    except (OSError, ValueError):
        return default


def _detect_comfy_dir():
    """自动探测 ComfyUI 目录（必须含 main.py）。找不到就给个占位路径，
    运行时会报"找不到 ComfyUI"，提示用户去 config.json 里配。"""
    cands = [os.environ.get("COMFYUI_DIR") or "", os.environ.get("COMFY_DIR") or "",
             os.path.join(BASE_DIR, "ComfyUI"), os.path.join(BASE_DIR, "..", "ComfyUI"),
             os.path.expanduser("~/ComfyUI"), "/opt/ComfyUI"]
    if os.name == "nt":
        cands += [r"C:\ComfyUI", r"D:\ComfyUI"]
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "main.py")):
            return os.path.abspath(c)
    return os.path.abspath(cands[2])


def _detect_python(comfy_dir):
    """跑 ComfyUI 用哪个解释器：优先 ComfyUI 同级/父级的 .venv（Windows 取 pythonw，不要 python）。"""
    up = os.path.dirname(os.path.abspath(comfy_dir))
    names = ("pythonw.exe", "python.exe") if os.name == "nt" else ("python3", "python")
    for root in (comfy_dir, up, os.path.dirname(up)):
        for venv in (".venv", "venv", "env"):
            for n in names:
                p = os.path.join(root, venv, "Scripts" if os.name == "nt" else "bin", n)
                if os.path.isfile(p):
                    return p
    return shutil.which("pythonw") or shutil.which("python3") or shutil.which("python") or sys.executable


def _disk_list():
    """列几个跟本程序有关的盘符（不写死 C:/D:）——设置页显示剩余空间用。"""
    roots = []
    for p in (COMFY_DIR, INPUT_DIR, OUTPUT_DIR, BASE_DIR):
        try:
            drv = os.path.splitdrive(os.path.abspath(p))[0]
            if drv and (drv + os.sep) not in roots:
                roots.append(drv + os.sep)
        except Exception:
            pass
    if not roots and os.name == "nt":
        import string as _string
        roots = [c + ":\\" for c in _string.ascii_uppercase if os.path.exists(c + ":\\")][:3]
    return roots


COMFY = os.environ.get("PANEL_COMFY") or "http://127.0.0.1:8188"   # 下面读完配置会按 comfy_port 重算
COMFY_DIR = os.path.abspath(str(_cfg_raw("comfy_dir", "") or _detect_comfy_dir()))
# ⚠ ComfyUI 必须用 pythonw.exe 启动，不能是 python.exe：venv 的 python.exe 是"中转器"，
#   它会再起一个真正的解释器，而那个子进程会**新建一个控制台** → 屏幕上冒出一个终端黑窗。
#   pythonw 下连子进程都不会有控制台；输出照样进 comfyui_panel.log（下面 ensure_comfy 里重定向了）。
COMFY_PY = os.path.abspath(str(_cfg_raw("comfy_python", "") or _detect_python(COMFY_DIR)))
COMFY_MAIN = os.path.join(COMFY_DIR, "main.py")
EXTRA_PATHS = os.path.join(COMFY_DIR, "extra_model_paths.yaml")
# 数据目录：默认跟 ComfyUI 同目录（标准布局）；分开部署（数据在另一个盘）就在 config.json 里指。
USER_DIR = os.path.abspath(str(_cfg_raw("user_dir", "") or os.path.join(COMFY_DIR, "user")))
INPUT_DIR = os.path.abspath(str(_cfg_raw("input_dir", "") or os.path.join(COMFY_DIR, "input")))
OUTPUT_DIR = os.path.abspath(str(_cfg_raw("output_dir", "") or os.path.join(COMFY_DIR, "output")))
# 模型根目录（可以多个，对应 extra_model_paths.yaml 里合并进来的那些）
MODEL_ROOTS = _cfg_raw("model_roots", "") or [os.path.join(COMFY_DIR, "models")]
if isinstance(MODEL_ROOTS, str):
    MODEL_ROOTS = [x for x in MODEL_ROOTS.split(os.pathsep) if x]
MODEL_ROOTS = [os.path.abspath(str(p)) for p in MODEL_ROOTS]
# 换脸要用的 insightface 目录（buffalo_l + inswapper_128.onnx）；权重按各自许可证自备
FACE_ROOT = str(_cfg_raw("insightface_dir", "") or os.path.join(MODEL_ROOTS[0], "insightface"))
INSWAPPER = os.path.join(FACE_ROOT, "inswapper_128.onnx")



# ---- 可选：第三方 ComfyUI 节点类名（不同节点包叫法不同，所以不写死，用 config.json 覆盖）----
# 对应功能：fp8 权重加载 / 扩图画面合成 / 提示词增强（扩写·反推）。
# 没配时相关功能会给出明确中文提示，而不是"神秘失败"。
NODE_FP8_LOADER = str(_cfg_raw("node_fp8_loader", "") or "").strip()
NODE_OUTPAINT_CANVAS = str(_cfg_raw("node_outpaint_canvas", "") or "").strip()
NODE_OUTPAINT_COMPOSITE = str(_cfg_raw("node_outpaint_composite", "") or "").strip()
NODE_PROMPT_ENHANCE = str(_cfg_raw("node_prompt_enhance", "") or "").strip()
def load_config():
    """读配置。

    ⚠ 用 utf-8-sig：Windows 记事本 / PowerShell 5.1 存出来的 JSON 常带 UTF-8 BOM，
    以前用 utf-8 读会抛异常、被 except 静默吞掉 —— 整份 panel_config.json 当没写、
    悄悄退回默认值（2026-10-03 第三方复验期间我自己就踩到了这个坑）。
    JSON 语法错也**必须说出来**，不能装哑巴。
    """
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as fh:
            return json.load(fh) or {}
    except OSError:
        return {}
    except ValueError as e:
        try:
            print(f"[panel] panel_config.json 里有 JSON 语法错误（{e}），这次先按默认值跑；"
                  f"请检查这个文件：{CONFIG_PATH}", flush=True)
        except Exception:
            pass
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---- 访问模式 / 端口（同类软件设置页那几项）--------------------------------
# local＝只绑 127.0.0.1（默认，最安全）；lan＝绑 0.0.0.0，同一个局域网里别的设备能开。
# 改完要重启面板才生效 —— 绑定地址是进程起来那一刻定下的。
def _cfg_val(key, default=None):
    # utf-8-sig：容忍记事本/PowerShell 5.1 写出的 BOM（第三方第三轮复验指出这里漏改）
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as fh:
            v = (json.load(fh) or {}).get(key, default)
            return default if v is None else v
    except (OSError, ValueError):
        return default


def lan_ip():
    """本机在局域网里的地址（拿不到就回 127.0.0.1）"""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))          # 不会真发包，只为让系统选出出网网卡
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass


def lan_url():
    """局域网访问地址。lan 模式下带 token —— 第三方审查指出：不加这层，
    同网段任何人都能直接开面板并调用 /raw、/api/config 等接口。"""
    base = f"http://{lan_ip()}:{PORT}/new"
    try:
        if ACCESS_MODE == "lan" and PANEL_TOKEN:
            return base + "?token=" + PANEL_TOKEN
    except NameError:
        pass
    return base


ACCESS_MODE = str(_cfg_val("access_mode", "local")).strip().lower()
if ACCESS_MODE not in ("local", "lan"):
    ACCESS_MODE = "local"
OPEN_COMFY = bool(_cfg_val("open_comfy", False))
HOST = "0.0.0.0" if ACCESS_MODE == "lan" else "127.0.0.1"
try:
    COMFY_PORT = int(_cfg_val("comfy_port", 8188))
except (TypeError, ValueError):
    COMFY_PORT = 8188
if not os.environ.get("PANEL_COMFY"):
    COMFY = f"http://127.0.0.1:{COMFY_PORT}"

# ---- 有副作用的 GET 接口（防 CSRF：这些即使没有 Origin/Referer 也要挡）--------
_SIDE_EFFECT_GET = ("/api/open", "/api/reveal", "/api/lastframe", "/api/comfy_start",
                    "/api/unload", "/api/power", "/api/delete_file", "/api/hide_file",
                    "/api/unhide_file", "/api/delete_run", "/api/pose/save",
                    # 第五轮复验补：这两个也是有副作用 / 重 IO 的 GET
                    "/api/models/reveal",   # 会用资源管理器定位文件
                    "/api/models/hash")     # 会把整个模型读一遍算 SHA-256

# ---- 局域网模式的最小鉴权令牌（第三方审查建议：lan 模式不能裸奔）--------------
# 规则：来自 127.0.0.1 的请求永远放行（桌面外壳 / 本机浏览器）；**其它来源在 lan 模式下必须带对 token**。
# token 优先取环境变量 PANEL_TOKEN，其次取配置里的 lan_token，都没有就生成一个并存进配置（重启后不变）。
PANEL_TOKEN = (os.environ.get("PANEL_TOKEN") or "").strip()
if ACCESS_MODE == "lan":
    if not PANEL_TOKEN:
        PANEL_TOKEN = str(_cfg_val("lan_token", "") or "").strip()
    if not PANEL_TOKEN:
        PANEL_TOKEN = hashlib.sha256(
            f"{time.time()}{os.getpid()}{random.random()}".encode()).hexdigest()[:16]
        try:
            _c = load_config()
            _c["lan_token"] = PANEL_TOKEN
            save_config(_c)
        except Exception:
            pass


def copy_to_save_dir(rel):
    """按设置把结果另存一份到用户指定的文件夹（文件名带时间和类型）"""
    cfg = load_config()
    dst_dir = (cfg.get("save_dir") or "").strip()
    if not dst_dir:
        return None
    src = os.path.join(OUTPUT_DIR, rel)
    if not os.path.exists(src):
        return None
    try:
        os.makedirs(dst_dir, exist_ok=True)
        base = os.path.basename(rel)
        dst = os.path.join(dst_dir, f"{time.strftime('%Y%m%d_%H%M%S')}_{base}")
        with open(src, "rb") as a, open(dst, "wb") as b:
            b.write(a.read())
        return dst
    except OSError:
        return None

TASK_NAMES = {"vton": "换衣", "hair": "换发型", "faceswap": "换脸", "restore": "老照片修复上色",
              "scene": "改背景/换场景", "motion": "照片动起来", "talkaudio": "按音频说话",
              "flf": "首尾帧/首帧动画", "free": "自由出图", "caption": "读图出描述",
              "tts": "配音 Breeze-TTS", "longcat": "数字人 LongCat", "outpaint": "扩图",
              "multiangle": "多角度", "pose": "动作姿态", "batch": "批量处理",
              "remove": "移除对象", "prompttool": "提示词工具", "poserender": "姿势人偶",
              "inpaint": "局部重绘（涂抹）", "fuse": "多图合成一个镜头"}

CAPTION_PROMPT = ("用中文详细描述这张图片：人物（长相、年龄、发型、表情）、服装（款式/颜色/材质/图案）、"
                  "姿势动作、手里拿的东西、背景环境、光线氛围、镜头与构图。"
                  "如果画面里有文字、水印、角标也一并指出。只输出描述本身，不要客套话。")

# 动作/姿势模板：选一个就把这段要求拼进提示词（实测「站着/跳着 → 坐着」有效）
POSE_PRESETS = {
    "sit_chair": "坐在一张椅子上：臀部坐实椅面，上身挺直略前倾，双手自然放在大腿或膝盖上，双腿并拢、双脚平踩地面",
    "sit_floor": "盘腿坐在地面上：双小腿交叉盘起，上身挺直，双手自然放在膝盖上，脚背贴地",
    "sit_cross": "坐在椅子上翘着腿（二郎腿）：一条腿的小腿搭在另一条腿的膝盖上，上身略后靠，双手自然放在腿上",
    "sit_side": "侧身坐在椅子或沙发边缘：双腿并拢斜向同一侧，上身微侧，双手放在腿上，身体重心落在一侧臀部",
    "sit_lounge": "慵懒地靠在椅背上坐着：上身放松后仰，一只手搭在扶手上，双腿自然伸展",
    "jump": "整个人跳起来：双脚同时离地、双膝弯曲收腿，双臂上举或向两侧张开，身体舒展，头发与衣角因腾空上扬",
    "jump_pose": "原地跳跃摆造型：一只脚离地收起、另一只脚点地，一只手向上举比出姿势，另一只手向侧面伸展",
    "squat": "蹲下：双膝深弯、臀部靠近脚跟，双手自然放在膝盖上，上身略前倾，身体重心稳定",
    "kneel": "单膝跪地：一条腿的膝盖着地，另一条腿屈膝立起，上身挺直，双手放在立起的那条腿的膝盖上",
    "lie": "仰面平躺：身体舒展躺平，双臂自然放在身体两侧或腹前，面朝上，双腿并拢伸直",
    "lie_side": "侧躺着：身体侧向一边、双腿并拢微屈，一只手肘撑起头部，另一只手自然放在身前",
    "prone": "趴着（俯卧）：身体贴地，用手肘撑起上身并抬头看向前方，小腿微微翘起",
    "walk": "正在走路：双腿前后交替迈步（一只脚在前、一只脚在后），手臂自然前后摆动，身体略前倾，有行进感",
    "run": "正在跑步：身体前倾，一只脚落地、另一只脚向后抬起，双臂弯曲前后摆动，动作有速度感",
    "turn": "身体朝向不变，头转过来回眸看向镜头（回头），脖子自然扭转，肩膀跟随轻微转动",
    "back": "转身背对镜头（背影）：看不到正脸，只见后脑勺、后背与侧脸轮廓，衣领与后背线条清晰",
    "hands_waist": "双手叉腰：双手放在腰间、手肘向外张开，站姿挺拔自信",
    "arms_crossed": "双手抱在胸前（抱臂）：双臂交叠，肩膀放松，站姿挺拔",
    "wave": "一只手举起挥手打招呼，另一只手自然下垂或插兜，面带微笑看向镜头",
    "heart": "双手在胸前或脸侧比心（手指并拢做出心形），看向镜头微笑",
    "hands_up": "双手高举过头顶，身体舒展，抬头或看向镜头",
    "stretch": "伸懒腰：双手向上伸展交握，身体微微后仰，肩膀放松",
    "bend": "弯腰前倾：上身前倾、臀部略向后坐，双手自然下垂或扶着膝盖",
    "lean_wall": "背靠墙面站立：身体微侧，一只脚向后踩墙，双手自然放下或插兜",
    "photo_pose": "摆拍姿势：一只手轻抚头发，身体重心放在一条腿上、另一条腿微屈，头微侧，看向镜头微笑",
    "hands_pocket": "双手插兜站立：双肩放松，身体重心偏向一侧，头微侧看向镜头",
    "fly_kick": "腾空飞腿（凌空踢腿）：整个人跳在空中，一条腿高高向前踢出、腿伸直绷脚背，另一条腿屈膝收在身下，"
                "双臂张开保持平衡，身体侧向或正面腾空，头发与衣角随动作飞扬",
    "kick": "站立侧踢：一条腿向侧面高高抬起踢出、腿伸直，另一条腿支撑站稳，双手护在身前，身体略倾向反方向",
    "punch": "出拳：一只手握拳向前打出并转腰发力，另一只手收在脸侧防守，双腿分开成弓步或马步站稳",
    "martial": "武术起手式：双脚分开略宽于肩、膝盖微屈呈马步，一手成掌前伸、一手收在腰间，上身挺直有气势",
    "dance_spin": "转身跳舞：身体旋转带动裙摆与头发飞散，一只手向上扬起、另一只手向侧面打开，脚步交叉",
    "ballet": "芭蕾舞姿：单脚踮起站立，另一条腿向后或向侧抬起并绷直脚尖，双臂优雅地划出弧线，身体挺拔舒展",
    "cheer": "啦啦队跳跃：跳起时双腿向两侧分开、绷直脚尖，双臂高举或向两侧张开成欢呼姿势，笑容灿烂",
    "cartwheel": "侧手翻动作：身体侧向腾空翻转，一只手撑地、双腿在空中分开成倒立姿势，动作连贯有力量感",
    # ---- 艺术写真摆姿 ----
    "art_cheek": "艺术写真摆姿：一只手轻托脸颊或下巴，手肘自然支起，头微侧、眼神柔和看向镜头，肩膀放松一侧略低",
    "art_face": "双手捧脸或指尖轻贴脸颊两侧，头微低、眼睛看向镜头或低头含笑，姿态柔美",
    "art_hair": "一只手抬起撩拨头发（手指穿过发丝），另一只手自然放下，头微侧，下巴略抬，气质自信",
    "art_chin": "手背轻抵下巴、指尖虚握，上身微微前倾，眼神直视镜头，显得沉思而优雅",
    "art_chest": "双手交叠放在胸前或锁骨前，肩膀打开，上身挺拔，头微侧含笑，端庄优雅",
    "art_waist_side": "一只手轻放腰间、另一只手自然下垂，身体侧转四分之三，重心落在一条腿上、另一条腿微屈，S 形曲线",
    "art_look_back": "侧身站立，头向后转回看镜头（回眸），下颌线清晰，肩膀略转，长发随动作扬起",
    "art_look_down": "微微低头垂眼，下巴收起，眼神落在下方，睫毛细长，安静温柔的氛围",
    "art_close_eyes": "仰起头微微闭眼，像在感受光线或微风，脖颈拉长，表情宁静",
    "art_side_profile": "身体完全侧向镜头（正侧面），只露侧脸轮廓，下巴微抬，姿态干净利落",
    "art_floor_long": "坐在或半躺在地面上，双腿向一侧伸展、膝盖微屈，一只手向后撑地，身体拉长显腿长",
    "art_hug_knees": "坐在地上抱膝：双膝屈起贴近胸口，双臂环抱小腿，下巴靠在膝盖上，眼神看向镜头",
    "art_step": "一只脚向前迈出半步、脚尖点地，身体重心在后腿，一只手轻搭在胯上，像在行走中被定住的一瞬间",
    "art_lean_rail": "身体斜靠在栏杆、墙壁或门框上，一只脚点地、另一只脚交叉在前，手臂自然搭靠，姿态慵懒时髦",
    "art_hat": "一只手扶着帽檐或墨镜，另一只手插兜，头微低、目光从帽檐下看向镜头",
    "art_skirt": "一只手轻轻提起裙摆一角，身体微微旋转，裙摆与头发飘动，像抓拍的动态瞬间",
    "art_flower": "双手捧着一束花或一朵花靠近脸侧，闻花或低头欣赏，表情柔和，光线柔和",
    "art_book": "双手捧着一本书或物件放在胸前或膝上，低头阅读状，安静文艺",
    "art_camera": "双手举着相机贴近眼睛拍照，或把相机挂在胸前双手轻扶，头微侧看向镜头",
    "art_sit_steps": "坐在台阶或阶梯上：一手撑在身后、一手放在膝上，双腿一前一后自然错开，上身挺直略侧",
    "art_bridge": "身体舒展地伸展手臂：一只手向上举高、另一只手向侧下方伸展，身体形成对角线，抬头看向举手方向",
    "art_over_shoulder": "背对镜头站立，头转过来从肩上方看向镜头，露侧脸与眼神，一只手抬起撩发或放在肩上",
    # ---- 举铁 / 健身动作 ----
    "gym_squat": "健身深蹲：肩上扛着杠铃（或徒手），双脚与肩同宽，屈膝下蹲至大腿接近水平，背部挺直、核心收紧，目光向前",
    "gym_deadlift": "硬拉起始姿势：双手正握杠铃置于小腿前，屈髋屈膝、背部平直挺胸，目视前方，全身绷紧",
    "gym_bench": "躺在卧推凳上做卧推：双手握住杠铃置于胸前上方，手臂微屈，双脚踩实地面，背部贴紧凳面",
    "gym_curl": "站姿哑铃弯举：一只手握哑铃屈肘上举至肩前，另一只手自然垂下持哑铃，身体稳定不晃动",
    "gym_lateral": "站姿哑铃侧平举：双臂微屈向两侧抬起至与肩同高，肩膀下沉，身体挺直",
    "gym_overhead": "双手把哑铃或杠铃举过头顶（推举），手臂上举伸直或微屈，核心收紧，抬头看向上方",
    "gym_pullup": "单杠引体向上：双手宽握横杆，身体悬空、双腿并拢微屈，下巴向上够向横杆，背部发力收紧",
    "gym_pushup": "俯卧撑：双手撑地略宽于肩，身体保持一条直线、核心收紧，屈肘下降或撑起，头略抬看向前方",
    "gym_plank": "平板支撑：前臂与脚尖撑地，身体从头到脚保持一条直线，臀部不塌不翘，目光看向地面",
    "gym_lunge": "弓步蹲：一条腿向前跨出屈膝成九十度，后腿膝盖接近地面，上身挺直，双手叉腰或持哑铃",
    "gym_crunch": "仰卧卷腹：仰躺在地垫上，双膝屈起、双手抱头或放耳侧，上背部离地卷起，腹部发力",
    "gym_rope": "战绳训练：双腿分开微蹲成运动姿，双手各握一根战绳交替上下甩动，绳子呈波浪形，表情专注有力",
    "gym_punchbag": "拳击沙袋：戴拳套，一只手出拳打向沙袋、另一只手护在脸侧，双腿分开站成格斗步，身体转腰发力",
    "gym_stretch": "健身拉伸：坐姿或站姿做拉伸，一只手抓住另一只手的肘部向侧方拉伸，身体侧倾，肌肉线条舒展",
    "gym_kettlebell": "壶铃摇摆：双手握住壶铃在双腿间摆动并向前甩起至胸口高度，屈髋发力，背部挺直，重心稳定",
    "gym_run_treadmill": "在跑步机上跑步：身体略前倾，一腿蹬地一腿抬起，双臂弯曲摆动，额头冒汗，运动装束",
    # ---- 瑜伽 ----
    "yoga_tree": "瑜伽树式：单腿站立，另一只脚的脚掌贴在小腿内侧或大腿内侧，双手在胸前合十或高举过头顶合十，"
                 "脊柱挺直、目视前方，身体稳定平衡",
    "yoga_warrior2": "瑜伽战士二式：双脚大步分开，前腿屈膝使大腿与地面平行、膝盖对准脚尖，后腿伸直脚掌外展，"
                     "双臂向两侧平举与肩同高，目光看向前手指尖",
    "yoga_warrior1": "瑜伽战士一式：前腿屈膝成弓步，后腿伸直脚跟压地，髋部朝前，双臂向上举过头顶合十或平行上举，"
                     "上身挺直微微后仰，目视上方",
    "yoga_warrior3": "瑜伽战士三式：一条腿站立支撑，上身与另一条腿同时向两端伸直成水平的一条线，"
                     "双臂向前伸展，身体像字母 T 一样平衡",
    "yoga_triangle": "瑜伽三角式：双脚分开站立，上身向一侧侧倾，一只手向下够到脚踝或小腿、另一只手向上伸直，"
                     "双臂成一条竖线，头转向举手方向",
    "yoga_downdog": "瑜伽下犬式：双手与双脚撑地，臀部高高向上顶起，身体呈倒 V 形，"
                    "手臂与背部成一条直线，脚跟尽量踩地，头在双臂之间朝下",
    "yoga_cobra": "瑜伽眼镜蛇式：俯卧在地，双手撑在胸侧，用手臂力量把上身撑起、胸腔打开向前上方，"
                  "髋部与腿贴地，肩膀下沉，头微抬看向前上方",
    "yoga_updog": "瑜伽上犬式：俯卧撑起，双臂伸直支撑，胸腔完全打开向上，大腿与膝盖离地，"
                  "脚背贴地，身体形成向前的弧线",
    "yoga_child": "瑜伽婴儿式：跪坐在地上，臀部坐向脚跟，上身前趴、额头贴地，双臂向前伸展或放在身体两侧，"
                  "背部放松延展",
    "yoga_cat": "瑜伽猫牛式（四点跪撑）：双手双膝撑地，大腿与手臂垂直于地面；"
                "猫式时背部向上拱起、低头收下巴；牛式时腰部下沉、胸口打开、抬头",
    "yoga_bridge": "瑜伽桥式：仰卧在地，双膝屈起双脚踩地靠近臀部，用手臂撑地把臀部与下背抬离地面，"
                   "肩膀与头贴地，身体从肩到膝成一条斜线",
    "yoga_boat": "瑜伽船式：坐在地上，上身微微后仰，双腿并拢伸直向上抬起成 V 字形，"
                 "双臂向前平伸与地面平行，核心收紧保持平衡",
    "yoga_pigeon": "瑜伽鸽子式：一条腿屈膝放在身前、小腿横放，另一条腿向后伸直贴在垫上，"
                   "髋部摆正、上身前伸或挺直，双手撑地或放在前膝上",
    "yoga_lotus": "瑜伽莲花坐：盘腿坐在地上，双脚脚背放在对侧大腿上，脊柱挺直、肩膀放松，"
                  "双手放在膝盖上结印或合十，闭眼静坐冥想",
    "yoga_forward": "瑜伽站立前屈：双腿伸直站立，上身从髋部向下折叠，双手向下够到脚背、脚踝或地面，"
                    "头自然垂向小腿，背部延展放松",
    "yoga_side_angle": "瑜伽侧角式：双脚分开成弓步，前腿屈膝，一只手撑在前脚旁的垫上，另一只手臂向斜上方伸直，"
                       "身体侧向打开成一条斜线",
    "yoga_dancer": "瑜伽舞王式：单腿站立，另一条腿向后屈膝抬起，同侧手向后抓住脚踝或脚背把腿拉高，"
                   "另一只手向前上方伸展，身体前倾保持平衡",
    "yoga_shoulderstand": "瑜伽肩倒立：仰卧后用肩膀和上背支撑，把双腿与髋部垂直向上抬起，"
                          "双手撑住下背，身体成一条竖线，脚尖朝上",
    "yoga_savasana": "瑜伽摊尸式：仰卧躺平，双臂自然放在身体两侧、掌心向上，双腿微微分开，"
                     "全身放松，闭眼呼吸平静",
    "yoga_sun": "瑜伽拜日式起手：站立合十，双臂从两侧向上举过头顶合十，上身微微后仰，"
                "胸口打开，目视上方，动作舒展",
}
POSE_CLAUSE = ("\n\n【姿势要求】把图1人物的姿势改成：{p}。"
               "人物长相、发型、服装、背景、光线、镜头角度保持不变；"
               "必须按新姿势重新画四肢与身体重心，不要保留原来的站姿或动作。")
POSE_REF_CLAUSE = ("\n\n【姿势参考】图{n}是姿势参考图：只取这张图里人物的姿势"
                   "（四肢位置、身体重心、朝向、是站/坐/跳/躺）。"
                   "不要参考这张图的长相、发型、服装和背景。"
                   "让图1这个人做出图{n}的姿势，图1人物的身份、发型、服装、背景保持不变。")


CAPTION_PURPOSES = {
    "plain": ("全面描述（什么都写）", CAPTION_PROMPT,
              "{d}"),
    "garment": ("服装款式（拿它去给别人换衣）",
                "只描述这张图片里的服装：款式、颜色、材质、图案、领型、袖长、衣长、版型，"
                "以及纽扣/拉链/蕾丝/腰带/口袋等细节。不要描述人物长相、姿势和背景。只输出描述本身。",
                "只输出一个人：图1的这个人，穿上{ref}这套衣服，款式/颜色/材质/图案必须完全一致——{d}"),
    "garment_only": ("服装本身（不提模特和背景）",
                     "描述这套服装本身，用「这是一件/一套…」的写法：款式、颜色、材质、图案、细节。"
                     "不要提模特、姿势、背景。只输出描述本身。",
                     "只输出一个人：图1的这个人，穿上这套衣服——{d}"),
    "bg": ("背景环境（拿它去换背景）",
           "只描述这张图片的背景环境：地点、陈设、色调、光线方向、氛围。不要描述人物和服装。只输出描述本身。",
           "把图1人物身后的背景换成{ref}这个场景，人物与服装保持不变：{d}"),
    "hair": ("发型（拿它去给别人换发型）",
             "只描述这张图片里人物的发型：长度、刘海、卷直、发色、分缝、发际线、蓬松度、发尾形状。只输出描述本身。",
             "把图1人物的发型改成{ref}这样：{d}。脸和其余部分保持不变。"),
    "face": ("人物长相特征",
             "只描述这张图片里人物的长相：脸型、五官、肤色、妆容、年龄感。只输出描述本身。",
             "只输出一个人，长相按{ref}这些特征：{d}"),
}

QWEN_UNET = "qwen_image_2.1_int8_convrot.safetensors"
# 模型档位：10GB 卡上按需切换（Q4 只吃 3.9GB，能和别的任务共存）
QWEN_TIERS = {
    "int8": {"unet": "qwen_image_2.1_int8_convrot.safetensors", "gguf": False,
             "label": "INT8 官方权重（推荐，质量最好）"},
    "q8": {"unet": "qwen-image-2.1-Q8_0_fixed.gguf", "gguf": True,
           "label": "Q8 GGUF（省一点显存）"},
    "q4": {"unet": "qwen_image_2.1_Q4_K_M.gguf", "gguf": True,
           "label": "Q4 GGUF（最省显存 3.9G，速度快）"},
    "fp8": {"unet": "qwen_image_2.1_rin247_fp8.safetensors", "gguf": False,
            "label": "FP8 rin247（备用：之前测过会花屏）"},
}
TIER = {"qwen": "q4"}   # 默认 Q4：实测 int8 在 10GB 卡上会 OOM 崩掉 ComfyUI


def tier_unet(tier=None):
    """返回 (模型文件名, 是否用 GGUF 加载)"""
    t = QWEN_TIERS.get((tier or TIER["qwen"]).lower()) or QWEN_TIERS["int8"]
    return t["unet"], t["gguf"]


# 所有"基座模型加载器"节点类型（自动降级时只改这些）
UNET_LOADER_TYPES = ("UNETLoader", "UnetLoaderGGUF", "UnetLoaderGGUFDynamicVRAM") + ((NODE_FP8_LOADER) if NODE_FP8_LOADER else ())


def unet_loader_node(unet_name, gguf=None):
    """按**模型文件本身**决定用哪个加载器 —— 不让"档位"和"加载器"有机会对不上（用户要求：全自动）。
      · .gguf                              → UnetLoaderGGUF
      · 文件名里带 fp8（带 FP8 scale 的 checkpoint）→ 第三方 FP8 加载器（见 config 的 node_fp8_loader）
      · 其它 safetensors（INT8 官方权重等）        → UNETLoader
    踩过的坑：扩图把 INT8 权重塞给 FP8 加载器，必报
    "Unsupported FP8 layer/scale: transformer_blocks.0.attn.to_k"，而界面默认档位就是 INT8 → 扩图必失败。"""
    low = str(unet_name or "").lower()
    is_gguf = low.endswith(".gguf") if gguf is None else bool(gguf)
    if is_gguf:
        return {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": unet_name}}
    if "fp8" in low:
        if not NODE_FP8_LOADER:
            raise RuntimeError("这是 FP8 权重，需要第三方 FP8 加载器节点："
                               "请在 panel_config.json 里填 node_fp8_loader（节点类名）")
        return {"class_type": NODE_FP8_LOADER, "inputs": {"unet_name": unet_name}}
    return {"class_type": "UNETLoader", "inputs": {"unet_name": unet_name, "weight_dtype": "default"}}


def tier_chain(preferred=None):
    """自动档位顺序：先用户/默认那档，跑不起来就逐级换，最后一定落在最兼容的 Q4 GGUF。
    （用户原话："哪一个图放进去了，就全部可以" —— 选模型这种活不该让人来干。）"""
    order = ["int8", "q8", "q4"]
    p = str(preferred if preferred is not None else TIER.get("qwen") or "auto").strip().lower()
    if p in order:
        return order[order.index(p):]
    if p in ("auto", "", "default"):
        return order
    return [p] + order                    # fp8 之类被显式选中时：先试它，再按默认链兜底


def retarget_base_model(graph, tier):
    """把图里的 Qwen 基座加载器换成指定档位（只认 Qwen 的加载器节点，别误伤视频模型）"""
    unet, gguf = tier_unet(tier)
    hit = 0
    for node in (graph or {}).values():
        if not isinstance(node, dict) or node.get("class_type") not in UNET_LOADER_TYPES:
            continue
        nm = str((node.get("inputs") or {}).get("unet_name") or "")
        if "qwen" not in nm.lower():
            continue
        node.update(unet_loader_node(unet, gguf))
        hit += 1
    return hit


_RETRY_HINTS = ("out of memory", "oom", "allocation", "vram", "cuda error", "unsupported fp8",
                "failed to load", "cannot load", "can't load", "deserializ", "invalid model",
                "no such file", "not found", "显存", "内存")


def _model_error(e):
    low = str(e).lower()
    return any(k in low for k in _RETRY_HINTS)


# LoRA 放哪个目录：config.json 的 lora_dir > 第一个存在的 <模型根>/loras
def _pick_lora_dir():
    v = str(_cfg_raw("lora_dir", "") or "").strip()
    if v:
        return os.path.abspath(v)
    for r in MODEL_ROOTS:
        p = os.path.join(r, "loras")
        if os.path.isdir(p):
            return p
    return os.path.join(MODEL_ROOTS[0], "loras")


LORA_DIR = _pick_lora_dir()
LORA_SLOTS = {"v": []}          # 本任务生效的附加 LoRA：[(名字, 强度), ...]


def list_loras():
    try:
        return sorted(f for f in os.listdir(LORA_DIR) if f.lower().endswith((".safetensors", ".sft", ".ckpt")))
    except OSError:
        return []


def apply_loras(g, model_ref, nid=80):
    """把用户选的附加 LoRA 串到模型后面，返回新的模型引用（没选就原样返回）"""
    ref = model_ref
    for name, strength in LORA_SLOTS["v"]:
        if not name:
            continue
        g[str(nid)] = {"class_type": "LoraLoaderModelOnly",
                       "inputs": {"model": ref, "lora_name": name, "strength_model": float(strength)}}
        ref = [str(nid), 0]
        nid += 1
    return ref
QWEN_CLIP = "qwen3vl_8b_w4a8.safetensors"
QWEN_VAE = "qwen_image_2.1_vae_bf16.safetensors"
H3_CLIP = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
H3_VAE = "minimax_h3_video_vae_fp16.safetensors"
H3_AVAE = "minimax_h3_audio_vae_fp32.safetensors"
H3_UNET_GGUF = "minimax_h3_fl2va_pruned_fp8_Q4_0.gguf"
H3_UNET_REF_GGUF = "minimax-h3-ref2va-Q4_0.gguf"
# ref2v 用这份 int8 融合权重：用户自己验证过的干净工作流用的就是它。
# 面板原来用 minimax-h3-ref2va-Q4_0.gguf（Q4 量化），实测人物皮肤上会浮出一层细密"编织纹"
# （与 9/28 干净成片对照 + 1:1 放大对比确认是 ref2v 这条路的锅），故换掉。
H3_UNET_INT8 = "minimax_h3_fused_refdelta_r1024_turbo8_mystic07_int8_convrot.safetensors"
_H3_PICK = {}


def h3_int8_ok():
    """ComfyUI 里有没有那份 int8 融合权重。用户自己那些干净工作流（首末帧驱动 / 图生视频 /
    老照片动起来）用的都是它；面板原来统一用 Q4_0 GGUF，实测 ref2v 那条会让皮肤浮出细密编织纹。只问一次。"""
    if "ok" not in _H3_PICK:
        try:
            names = comfy_get("/models/diffusion_models") or []
        except Exception:
            names = []
        _H3_PICK["ok"] = H3_UNET_INT8 in names
    return _H3_PICK["ok"]


def h3_unet_node(gguf_fallback):
    """H3 权重节点：按**档位 + 显存**挑，而不是无脑用 19.5GB 的 int8 融合版。

    2026-10-03 实测：10G 卡 + 同类软件占着显存时跑「照片动起来」，int8 融合权重 staged 19995MB、
    每步 236 秒，最后 ComfyUI 被系统结束（任务撞上 10061 连接被拒）。
    规则：显式 q4 → 10.6GB 的 Q4 GGUF｜显式 int8 → int8 融合版｜auto → 空闲显存 <4GB 才降级到 Q4
    并在日志说明；<10GB 只给"会很慢"的提醒，不擅自降质量（用户自己那些干净工作流就是用 int8
    在这张 10G 卡上跑的）。"""
    _t = (TIER.get("video") or "auto").strip().lower()
    _free = comfy_vram_free()
    _gguf = {"class_type": "UnetLoaderGGUFDynamicVRAM",
             "inputs": {"unet_name": gguf_fallback, "weight_dtype": "default"}}
    if _t in ("q4", "gguf"):
        _H3_PICK["note"] = "H3：按你选的档位用 Q4 GGUF（10.6GB，省显存）"
        return _gguf
    if _t != "int8" and _free and _free < 4 * 1024 ** 3 and h3_int8_ok():
        _H3_PICK["note"] = ("H3：int8 融合权重要 19.5GB，现在只空闲 %.1fGB → 自动改用 10.6GB 的 Q4 GGUF"
                            "（Q4 皮肤上可能浮细纹；要用 int8 就先把同类软件关掉）" % (_free / 1024 ** 3))
        return _gguf
    if _free and _free < 10 * 1024 ** 3:
        if _t == "int8":
            _H3_PICK["note"] = ("H3：你选了 int8 融合权重（19.5GB），当前空闲只有 %.1fGB —— "
                                "会非常慢甚至卡崩，建议先关同类软件再跑" % (_free / 1024 ** 3))
        else:
            _H3_PICK["note"] = ("H3：用 int8 融合权重（19.5GB），当前空闲只有 %.1fGB —— 会非常慢甚至卡崩，"
                                "建议先关同类软件；也可以在档位里选 Q4 GGUF" % (_free / 1024 ** 3))
    if h3_int8_ok():
        return {"class_type": "UNETLoader",
                "inputs": {"unet_name": H3_UNET_INT8, "weight_dtype": "default"}}
    return _gguf

# Viggle Turbo（DMD 蒸馏 LoRA）：6 步代替 40 步、关 CFG。必须用它自带的节点做「运行时旁路」，
# 因为往 int8 权重里合并会引入约 4 倍噪声（作者注释原话）。实测冷启动 42s → 24s。
VIGGLE_LORA = "Qwen-Image-2.1-viggle-turbo-v0.2.1-6step-lora-r128.safetensors"
VIGGLE_SIGMAS = "1.0, 0.9375, 0.875, 0.75, 0.5, 0.25"

JOBS = {}
LOCK = threading.Lock()
JOB_LOCK = threading.Lock()    # 任务串行：一次只跑一个，避免内存叠加导致整机挂死
SEQ_LOCK = threading.Lock()
JOB_SEQ = [0]
CURRENT_JID = {"id": None}     # 正在跑的那个（供 submit_and_wait 检查取消）


class JobCancelled(Exception):
    pass


def register_job(jid, first_log, task=""):
    """登记一个任务：排队号 + 状态机（queued → running → done/error/cancelled）"""
    with SEQ_LOCK:
        JOB_SEQ[0] += 1
        seq = JOB_SEQ[0]
    JOBS[jid] = {"status": "queued", "seq": seq, "log": [first_log], "result": None,
                 "result_name": None, "seconds": None, "error": None, "cancel": False,
                 "task": task, "task_name": TASK_NAMES.get(task, task),
                 "created": time.time(), "started": None, "ended": None}
    return seq


def queue_position(jid):
    """前面还有几个排队中的"""
    me = JOBS.get(jid) or {}
    n = 0
    for j in JOBS.values():
        if j.get("status") == "queued" and j.get("seq", 0) < me.get("seq", 0):
            n += 1
    return n


def launch_job(jid, task, files, params):
    """起一个工作线程：拿不到锁就排队，拿到锁前检查是否被取消"""
    def _run():
        if JOB_LOCK.locked():
            pos = queue_position(jid) + 1
            JOBS[jid]["log"].append(f"排队中：前面还有 {pos - 1} 个任务")
        with JOB_LOCK:
            j = JOBS.get(jid)
            if not j:
                return
            if j.get("cancel"):
                j["status"] = "cancelled"
                j["log"].append("已取消（还没轮到就取消了）")
                j["ended"] = time.time()
                return
            j["status"] = "running"
            j["started"] = time.time()
            CURRENT_JID["id"] = jid
            try:
                run_job(jid, task, files, params)
            except JobCancelled:
                j["status"] = "cancelled"
                j["log"].append("已停止")
            finally:
                j["ended"] = time.time()
                CURRENT_JID["id"] = None

    threading.Thread(target=_run, daemon=True).start()


def cancel_job(jid):
    j = JOBS.get(jid)
    if not j:
        return "没有这个任务"
    st = j.get("status")
    if st == "queued":
        j["cancel"] = True
        j["status"] = "cancelled"
        j["log"].append("已取消排队")
        j["ended"] = time.time()
        return "已取消排队"
    if st == "running":
        j["cancel"] = True
        j["log"].append("正在停止（打断采样）...")
        try:
            comfy_post("/interrupt", {})
            comfy_post("/queue", {"clear": True})
        except Exception:
            pass
        return "已发出停止指令"
    return f"这个任务已经 {st}，不用停"


LAST_FAMILY = {"v": None}


# ---------------- ComfyUI 控制 ----------------
def port_open(port=8188):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_comfy():
    if port_open():
        return True
    # ComfyUI 的监听：只有「访问模式=lan 且允许局域网直连」时才放开，否则一律只绑本机
    comfy_listen = HOST if (ACCESS_MODE == "lan" and OPEN_COMFY) else "127.0.0.1"
    print(f"[panel] 启动 ComfyUI（端口 {COMFY_PORT} · 监听 {comfy_listen} · --cache-none --lowvram）...", flush=True)
    # 输出写进日志文件：以前丢到 DEVNULL，ComfyUI 崩了就完全查不到原因（踩过）
    logp = os.path.join(BASE_DIR, "comfyui_panel.log")
    out, fh = subprocess.DEVNULL, None
    try:
        fh = open(logp, "ab", buffering=0)
        fh.write(("\n\n===== 面板拉起 ComfyUI %s =====\n"
                  % time.strftime("%Y-%m-%d %H:%M:%S")).encode("utf-8"))
        out = fh
    except OSError:
        fh = None
    popen_hidden([COMFY_PY, COMFY_MAIN, "--port", str(COMFY_PORT), "--listen", comfy_listen,
                      "--extra-model-paths-config", EXTRA_PATHS,
                      "--user-directory", USER_DIR, "--input-directory", INPUT_DIR,
                      "--output-directory", OUTPUT_DIR,
                      "--cache-none", "--lowvram", "--disable-auto-launch"],
                     cwd=COMFY_DIR, stdout=out, stderr=out,
                     # BREAKAWAY_FROM_JOB：面板被结束/重启时别把 ComfyUI 一起带走
                     # （Windows 作业对象会连子进程一起杀 —— 之前每重启一次面板 ComfyUI 就死一次）
                     creationflags=(getattr(subprocess, "DETACHED_PROCESS", 0)
                                    | 0x01000000           # CREATE_BREAKAWAY_FROM_JOB
                                    | NO_WINDOW))          # ⚠ 必须补上，否则它会顶掉 _hidden_kw 的隐藏
    if fh:
        try:
            fh.close()          # 子进程有自己的一份句柄，父进程这份要关掉，别泄漏
        except OSError:
            pass
    for _ in range(120):
        if port_open():
            print("[panel] ComfyUI 已就绪", flush=True)
            return True
        cj = CURRENT_JID.get("id")
        if cj and (JOBS.get(cj) or {}).get("cancel"):
            raise JobCancelled("等 ComfyUI 启动时被取消")
        time.sleep(2)
    return False


def comfy_get(path):
    return json.load(urllib.request.urlopen(COMFY + path, timeout=120))


def comfy_post(path, payload):
    req = urllib.request.Request(COMFY + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=60)
        body = r.read().decode("utf-8", "replace").strip()
        # ComfyUI 有些接口（比如 /free）成功时返回空 body ——
        # 以前直接 json.load 会抛 JSONDecodeError，导致「释放显存」永远 500（踩过）
        return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:800]
        raise RuntimeError(f"ComfyUI 拒绝({e.code}): {detail}") from None


def free_models(family):
    """切换模型家族前卸载模型，防止内存爆掉"""
    if LAST_FAMILY["v"] in (None, family):
        LAST_FAMILY["v"] = family
        return
    try:
        comfy_post("/free", {"unload_models": True, "free_memory": True})
        time.sleep(5)
        print(f"[panel] 已卸载模型（{LAST_FAMILY['v']} -> {family}）", flush=True)
    except Exception as e:
        print("[panel] free 失败:", e, flush=True)
    LAST_FAMILY["v"] = family


# ---------------- 工作流构造 ----------------
def to_turbo(g, pos_ref, latent_ref, seed=None):
    """把整张图改成 Viggle Turbo 走法：LoRA 运行时旁路 + 6 步 + 关 CFG。
    pos_ref/latent_ref 形如 ["20", 0] / ["90", 0]。"""
    g["70"] = {"class_type": "ViggleTurboLora",
               "inputs": {"model": ["1", 0], "lora_name": VIGGLE_LORA, "strength": 1.0}}
    g["71"] = {"class_type": "BasicGuider", "inputs": {"model": ["70", 0], "conditioning": pos_ref}}
    g["72"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}}
    g["73"] = {"class_type": "ViggleTurboSigmas", "inputs": {"latent": latent_ref, "nodes": VIGGLE_SIGMAS}}
    g["74"] = {"class_type": "RandomNoise",
               "inputs": {"noise_seed": seed if seed is not None else random.randint(1, 2 ** 31 - 1)}}
    g["75"] = {"class_type": "SamplerCustomAdvanced", "inputs": {
        "noise": ["74", 0], "guider": ["71", 0], "sampler": ["72", 0], "sigmas": ["73", 0],
        "latent_image": latent_ref}}
    g.pop("30", None)
    g["40"]["inputs"]["samples"] = ["75", 0]
    return g


def qwen_scene_graph(person, refs, positive, negative, bg_model=None, steps=25, cfg=1.0,
                     grow=12, feather=28):
    """换背景/换场景专用：**只重画背景，人物区域用原图像素贴回去**。

    为什么要这么做（用户反馈"脸永远会变形"）：
      普通编辑图是整张图 denoise=1.0 重绘 —— 人物连脸一起被重新生成，必然漂。
      这里两道保险：
        ① 抠图模型给出人物 mask → 反转成背景 mask → SetLatentNoiseMask：采样只动背景；
        ② 采完之后再用同一张人物 mask 把原图人物贴回（ImageCompositeMasked）——
           即使模型背景画得花，人物（含脸）也是原图一个像素都不差。
    """
    bg_model = bg_model or BG_MODEL
    _u, _gguf = tier_unet()
    g = {
        "1": unet_loader_node(_u, _gguf),          # 加载器按文件本身选（GGUF / FP8 / INT8 各自对应）
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": QWEN_CLIP, "type": "qwen_image",
                                                     "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": QWEN_VAE}},
        # 人物图（第一张，缩到 1536 以内，后面采样/贴回都用这一张，保证尺寸一致）
        "10": {"class_type": "LoadImage", "inputs": {"image": person}},
        "11": {"class_type": "ImageScaleToMaxDimension",
               "inputs": {"image": ["10", 0], "upscale_method": "lanczos", "largest_size": 1536}},
        # 人物 / 背景 mask
        "12": {"class_type": "LoadBackgroundRemovalModel", "inputs": {"bg_removal_name": bg_model}},
        "13": {"class_type": "RemoveBackground", "inputs": {"bg_removal_model": ["12", 0], "image": ["11", 0]}},
        "14": {"class_type": "InvertMask", "inputs": {"mask": ["13", 0]}},            # 背景
        "15": {"class_type": "GrowMask", "inputs": {"mask": ["14", 0], "expand": int(grow),
                                                    "tapered_corners": True}},
        "16": {"class_type": "FeatherMask", "inputs": {"mask": ["15", 0], "left": int(feather),
                                                       "top": int(feather), "right": int(feather),
                                                       "bottom": int(feather)}},
        "17": {"class_type": "VAEEncode", "inputs": {"pixels": ["11", 0], "vae": ["3", 0]}},
        "18": {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["17", 0], "mask": ["16", 0]}},
    }
    # 参考图（可选）挂在 image2..
    pos_in = {"clip": ["2", 0], "vae": ["3", 0], "image1": ["11", 0], "prompt": positive}
    neg_in = {"clip": ["2", 0], "vae": ["3", 0], "image1": ["11", 0], "prompt": negative}
    nid = 30
    for i, name in enumerate([r for r in (refs or []) if r], start=2):
        if i > 3:
            break
        g[str(nid)] = {"class_type": "LoadImage", "inputs": {"image": name}}
        g[str(nid + 1)] = {"class_type": "ImageScaleToMaxDimension",
                           "inputs": {"image": [str(nid), 0], "upscale_method": "lanczos",
                                      "largest_size": 1024}}
        pos_in[f"image{i}"] = [str(nid + 1), 0]
        neg_in[f"image{i}"] = [str(nid + 1), 0]
        nid += 2
    g["20"] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": pos_in}
    g["21"] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": neg_in}
    g["40"] = {"class_type": "KSampler", "inputs": {
        "model": ["1", 0], "positive": ["20", 0], "negative": ["21", 0], "latent_image": ["18", 0],
        "seed": random.randint(1, 2 ** 31 - 1), "steps": int(steps), "cfg": float(cfg),
        "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}}
    g["41"] = {"class_type": "VAEDecode", "inputs": {"samples": ["40", 0], "vae": ["3", 0]}}
    # 人物贴回：mask=人物前景（羽化一点点，接缝自然），source=缩放后的人物原图
    g["42"] = {"class_type": "FeatherMask", "inputs": {"mask": ["13", 0], "left": 6, "top": 6,
                                                       "right": 6, "bottom": 6}}
    g["43"] = {"class_type": "ImageCompositeMasked", "inputs": {
        "destination": ["41", 0], "source": ["11", 0], "x": 0, "y": 0,
        "resize_source": False, "mask": ["42", 0]}}
    g["60"] = {"class_type": "SaveImage", "inputs": {"images": ["43", 0],
                                                     "filename_prefix": "QwenEdit/panel_scene"}}
    return g


def qwen_graph(image_names, positive, negative, width_hint=None, seed=None, unet=None,
               steps=20, cfg=2.5, size=None, batch=1, turbo=False):
    """image_names 为空 = 文生图（纯提示词出图）；给了图 = 编辑/多图参考。
    steps/cfg 可调（官方推荐 40 步 / cfg 1.0，cfg>1 负向提示词才生效）。"""
    _u, _gguf = tier_unet()
    if unet:
        _u = unet
    g = {
        "1": unet_loader_node(_u, _gguf),          # 加载器按文件本身选（GGUF / FP8 / INT8 各自对应）
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": QWEN_CLIP, "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": QWEN_VAE}},
    }
    img_ids = []
    nid = 10
    for name in image_names:
        g[str(nid)] = {"class_type": "LoadImage", "inputs": {"image": name}}
        g[str(nid + 1)] = {"class_type": "ImageScaleToMaxDimension",
                           "inputs": {"image": [str(nid), 0], "upscale_method": "lanczos",
                                      "largest_size": 1536 if nid == 10 else 1024}}
        img_ids.append(str(nid + 1))
        nid += 2
    if img_ids:
        g["90"] = {"class_type": "VAEEncode", "inputs": {"pixels": [img_ids[0], 0], "vae": ["3", 0]}}
        latent = ["90", 0]
        pos_inputs = {"clip": ["2", 0], "vae": ["3", 0], "image1": [img_ids[0], 0], "prompt": positive}
        neg_inputs = {"clip": ["2", 0], "vae": ["3", 0], "image1": [img_ids[0], 0], "prompt": negative}
        for i, iid in enumerate(img_ids[1:], start=2):
            pos_inputs[f"image{i}"] = [iid, 0]
    else:
        w, h = (size or (1024, 1024))
        g["91"] = {"class_type": "EmptySD3LatentImage",
                   "inputs": {"width": int(w), "height": int(h), "batch_size": int(batch)}}
        latent = ["91", 0]
        pos_inputs = {"clip": ["2", 0], "prompt": positive}
        neg_inputs = {"clip": ["2", 0], "prompt": negative}
    g["20"] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": pos_inputs}
    g["21"] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": neg_inputs}
    g["30"] = {"class_type": "KSampler", "inputs": {
        "model": apply_loras(g, ["1", 0], nid=80), "positive": ["20", 0], "negative": ["21", 0], "latent_image": latent,
        "seed": seed if seed is not None else random.randint(1, 2 ** 31 - 1),
        "steps": int(steps), "cfg": float(cfg), "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}}
    g["40"] = {"class_type": "VAEDecode", "inputs": {"samples": ["30", 0], "vae": ["3", 0]}}
    g["50"] = {"class_type": "SaveImage", "inputs": {"images": ["40", 0], "filename_prefix": "QwenEdit/panel_out"}}
    if turbo:
        to_turbo(g, ["20", 0], latent, seed)
    return g


def qwen_graph21(ref_images, prompt, negative, resolution=768, seed=None, unet=None, steps=20, cfg=2.5,
                 turbo=False, ref_res=768, max_refs=8):
    """Qwen-Image-2.1 原生多参考节点（TextEncodeQwenImage21）：参考图最多 max_refs 张。
    输出尺寸跟第一张参考图走；没有参考图时按 resolution×resolution。

    ⚠ 参考图必须先缩小再喂编码器：视觉 token 数 ≈ (边长/16)²，8 张原尺寸 1344 的图 = 5 万多 token，
    实测跑到 7 分钟还出不来、内存飙高，**曾经把整机压死**（2026-10-02）。官方/同类软件的做法是
    "参考图编码精度 768"，所以这里：第 1 张最多 1024（它决定输出尺寸），第 2..N 张一律缩到 768。"""
    _u, _gguf = tier_unet()
    if unet:
        _u = unet
    g = {
        "1": unet_loader_node(_u, _gguf),          # 加载器按文件本身选（GGUF / FP8 / INT8 各自对应）
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": QWEN_CLIP, "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": QWEN_VAE}},
    }
    enc = {"clip": ["2", 0], "vae": ["3", 0], "prompt": prompt,
           "negative_prompt": negative or " ", "resolution": int(resolution)}
    imgs = [r for r in (ref_images or []) if r]
    if len(imgs) > max_refs:
        imgs = imgs[:max_refs]
    nid = 100          # ⚠ 参考图节点从 100 起编号：一张图占两个节点（LoadImage + 缩放），
                       #   从 10 起的话第 6 张就会撞上文本编码器 20 号（踩过：400 return_type_mismatch）
    for i, name in enumerate(imgs, start=1):
        g[str(nid)] = {"class_type": "LoadImage", "inputs": {"image": name}}
        size = min(1024, max(512, int(resolution))) if i == 1 else int(ref_res)
        g[str(nid + 1)] = {"class_type": "ImageScaleToMaxDimension",
                           "inputs": {"image": [str(nid), 0], "upscale_method": "lanczos",
                                      "largest_size": size}}
        enc[f"images.image_{i}"] = [str(nid + 1), 0]
        nid += 2
    g["20"] = {"class_type": "TextEncodeQwenImage21", "inputs": enc}
    mref21 = apply_loras(g, ["1", 0])
    g["30"] = {"class_type": "KSampler", "inputs": {
        "model": mref21, "positive": ["20", 0], "negative": ["20", 1], "latent_image": ["20", 2],
        "seed": seed if seed is not None else random.randint(1, 2 ** 31 - 1),
        "steps": int(steps), "cfg": float(cfg), "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}}
    g["40"] = {"class_type": "VAEDecode", "inputs": {"samples": ["30", 0], "vae": ["3", 0]}}
    g["50"] = {"class_type": "SaveImage", "inputs": {"images": ["40", 0], "filename_prefix": "QwenEdit/panel_out"}}
    if turbo:
        to_turbo(g, ["20", 0], ["20", 2], seed)
    return g


def h3_motion_graph(image, prompt, width, height, length, seed=None, last_image=None):
    g = {
        "1": {"class_type": "CLIPLoader", "inputs": {"clip_name": H3_CLIP, "type": "minimax", "device": "default"}},
        "2": {"class_type": "VAELoader", "inputs": {"vae_name": H3_VAE}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": H3_AVAE}},
        "4": h3_unet_node(H3_UNET_GGUF),
        "5": {"class_type": "MiniMaxH3SigmaShift", "inputs": {"model": ["4", 0], "shift_video": 12.0, "shift_audio": 3.0}},
        "6": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {
            "clip": ["1", 0], "vae": ["2", 0], "first_frame": ["16", 0],
            "prompt": prompt, "width": width, "height": height, "length": length}},
        "7": {"class_type": "BasicGuider", "inputs": {"model": ["5", 0], "conditioning": ["6", 0]}},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "9": {"class_type": "BasicScheduler", "inputs": {"model": ["5", 0], "scheduler": "simple", "steps": 4, "denoise": 1.0}},
        "10": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed if seed is not None else random.randint(1, 2 ** 31 - 1)}},
        "11": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["10", 0], "guider": ["7", 0], "sampler": ["8", 0], "sigmas": ["9", 0], "latent_image": ["6", 1]}},
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["2", 0]}},
        "13": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["11", 0], "vae": ["3", 0]}},
        "14": {"class_type": "CreateVideo", "inputs": {"images": ["12", 0], "audio": ["13", 0], "fps": 24,
                                                       "bit_depth": "auto", "color_space": "sRGB", "codec": "none"}},
        "15": {"class_type": "SaveVideo", "inputs": {"video": ["14", 0], "filename_prefix": "video/panel_motion",
                                                     "format": "auto", "codec": "auto"}},
        "16": {"class_type": "LoadImage", "inputs": {"image": image}},
    }
    if last_image:
        g["17"] = {"class_type": "LoadImage", "inputs": {"image": last_image}}
        g["6"]["inputs"]["last_frame"] = ["17", 0]
    return g


def h3_ref_graph(ref_images, prompt, width, height, length, audio=None, seed=None,
                 save_prefix="video/panel_ref"):
    """H3 参考图版（ReferenceToVideo）：可塞多张参考图锁人物长相，也可以塞一段音频让它照着开口。
    ref_images 第一张当主图，后面几张用来锁脸。"""
    g = {
        "1": {"class_type": "CLIPLoader", "inputs": {"clip_name": H3_CLIP, "type": "minimax", "device": "default"}},
        "2": {"class_type": "VAELoader", "inputs": {"vae_name": H3_VAE}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": H3_AVAE}},
        "4": h3_unet_node(H3_UNET_REF_GGUF),
        "5": {"class_type": "MiniMaxH3SigmaShift", "inputs": {"model": ["4", 0], "shift_video": 12.0, "shift_audio": 3.0}},
        "6": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
            "clip": ["1", 0], "vae": ["2", 0], "audio_vae": ["3", 0],
            "prompt": prompt, "width": width, "height": height, "length": length,
            "ref_image_size": "match"}},
        "7": {"class_type": "BasicGuider", "inputs": {"model": ["5", 0], "conditioning": ["6", 0]}},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "9": {"class_type": "BasicScheduler", "inputs": {"model": ["5", 0], "scheduler": "simple", "steps": 4, "denoise": 1.0}},
        "10": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed if seed is not None else random.randint(1, 2 ** 31 - 1)}},
        "11": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["10", 0], "guider": ["7", 0], "sampler": ["8", 0], "sigmas": ["9", 0], "latent_image": ["6", 1]}},
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["2", 0]}},
        "13": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["11", 0], "vae": ["3", 0]}},
        "14": {"class_type": "CreateVideo", "inputs": {"images": ["12", 0], "audio": ["13", 0], "fps": 24,
                                                       "bit_depth": "auto", "color_space": "sRGB", "codec": "none"}},
        "15": {"class_type": "SaveVideo", "inputs": {"video": ["14", 0], "filename_prefix": save_prefix,
                                                     "format": "auto", "codec": "auto"}},
    }
    nid = 20
    for i, name in enumerate([x for x in ref_images if x]):
        g[str(nid)] = {"class_type": "LoadImage", "inputs": {"image": name}}
        g["6"]["inputs"][f"ref_images.ref_image_{i}"] = [str(nid), 0]
        nid += 1
    if audio:
        g[str(nid)] = {"class_type": "LoadAudio", "inputs": {"audio": audio}}
        g["6"]["inputs"]["ref_audios.ref_audio_0"] = [str(nid), 0]
    return g


# ---------------- 配音：Breeze-TTS 2（本机；可用文字描述音色，也可给参考音克隆）----------------
BREEZE_VOICE_DEFAULT = ("一位三十岁左右的中文女声，音色清亮温和，语速偏慢，语气平静、带一点讲述感。")


def tts_graph(text, mode="design", instruction="", ref_audio=None, ref_text="", seed=42,
              prefix="audio/panel_tts"):
    """design＝用文字描述音色（不需要参考音）；clone＝给一段参考音克隆音色。"""
    g = {"1": {"class_type": "BreezeTTS2LoadModel", "inputs": {
        "model": "int8 hybrid (recommended)", "dtype": "auto", "device": "auto",
        "attention": "auto", "decode_mode": "eager", "download_if_missing": True}}}
    common = {"text": text, "max_new_tokens": 1500, "temperature": 0.9, "top_k": 50, "top_p": 1.0,
              "repetition_penalty": 1.1, "depth_temperature": 0.9, "depth_top_k": 50,
              "depth_top_p": 1.0, "seed": seed}
    if mode == "clone" and ref_audio:
        g["10"] = {"class_type": "LoadAudio", "inputs": {"audio": ref_audio}}
        g["2"] = {"class_type": "BreezeTTS2VoiceClone", "inputs": dict(
            common, breeze_model=["1", 0], reference_audio=["10", 0],
            reference_text=ref_text or "", cfg_scale=1.0)}
    else:
        g["2"] = {"class_type": "BreezeTTS2VoiceDesign", "inputs": dict(
            common, breeze_model=["1", 0],
            instruction=instruction or BREEZE_VOICE_DEFAULT, cfg_scale=4.0)}
    g["3"] = {"class_type": "SaveAudio", "inputs": {"audio": ["2", 0], "filename_prefix": prefix}}
    return g


# ---------------- 数字人：LongCat Avatar（本机 int8；照片＋音频 → 对口型说话视频）----------------
LC_NEG = ("Close-up, Bright tones, overexposed, static, blurred details, subtitles, style, works, "
          "paintings, images, static, overall gray, worst quality, low quality, JPEG compression residue, "
          "ugly, incomplete, extra fingers, poorly drawn hands, poorly drawn faces, deformed, disfigured, "
          "misshapen limbs, fused fingers, still picture, messy background, three legs, "
          "many people in the background, walking backwards")
LC_VAE = "LongCat-Video-Avatar-vae.safetensors"
LC_LORA = "longcat-avatar-dmd_lora.safetensors"   # Avatar v1.5 必须挂这个蒸馏 LoRA，否则节点直接报错
LC_CLIP = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
LC_WHISPER = "whisper-large-v3.safetensors"


def longcat_graph(image_name, audio_name, prompt="", resolution="480p", seed=None, steps=8, fps=25,
                  prefix="video/panel_longcat"):
    """照抄本机已验证的 `longcat-avatar1.5_本机int8_可跑.json` 的接线。
    省掉了它的人声分离支路（Kim_Vocal_2.onnx）——我们的台词本来就是干净人声，不需要再分离。"""
    return {
        "1": {"class_type": "LongCat_Video_SM_Model", "inputs": {
            "inference_weight_mode": "official_int8_sharded", "attention_mode": "sdpa",
            "auto_download_missing_weights": False, "vae": LC_VAE, "lora": LC_LORA}},
        "8": {"class_type": "CLIPLoader", "inputs": {"clip_name": LC_CLIP, "type": "wan", "device": "default"}},
        "5": {"class_type": "LongCat_Video_SM_Encode", "inputs": {
            "text_encoder_root": "LongCat-Video", "auto_download_missing_text_encoder": False,
            "offload_device": "cpu", "prompt": prompt, "negative_prompt": LC_NEG, "clip": ["8", 0]}},
        "27": {"class_type": "LongCat_Video_SM_WhisperModel", "inputs": {"audio_encoder": LC_WHISPER}},
        "15": {"class_type": "LoadAudio", "inputs": {"audio": audio_name}},
        "22": {"class_type": "LongCat_Video_SM_Audio", "inputs": {
            "audio_encoder": ["27", 0], "audio": ["15", 0], "save_fps": fps,
            "audio_type": "para", "p_box": ""}},
        "7": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "2": {"class_type": "LongCat_Video_SM_Sampler", "inputs": {
            "model": ["1", 0], "te_cond": ["5", 0], "au_cond": ["22", 0], "image": ["7", 0],
            "stage_1": "ai2v", "resolution": resolution,
            "seed": seed if seed is not None else random.randint(1, 2 ** 31 - 1), "steps": steps,
            "text_guidance_scale": 1.0, "audio_guidance_scale": 1.0, "ref_img_index": 10,
            "mask_frame_range": 3, "block_num": 1, "mux_audio_path": "", "offload_device": "cpu",
            "debug_mode": False, "mux_audio": ["15", 0]}},
        "3": {"class_type": "CreateVideo", "inputs": {"images": ["2", 0], "fps": fps,
                                                      "audio": ["15", 0], "codec": "auto"}},
        "4": {"class_type": "SaveVideo", "inputs": {"video": ["3", 0], "filename_prefix": prefix,
                                                    "format": "mp4", "codec": "h264"}},
    }


def audio_seconds(path):
    """读音频时长（秒）。wav 用标准库，其它格式返回 0 由调用方兜底。"""
    try:
        import wave
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return 0.0


def fit_length(seconds, fps=24, fallback=124):
    """H3 的帧数必须是 17n+5；按音频时长挑一个够长的，上限 345 帧（≈14 秒）。"""
    if not seconds or seconds <= 0:
        return fallback
    need = int(seconds * fps) + 1
    n = 1
    while 17 * n + 5 < need:
        n += 1
    return min(17 * n + 5, 345)


VTON_TMPL = """把参考图里的服装{extra}换到人物照片上，输出一张新的单人照片。
图1：人物照片——她是要保留的人。脸型、五官、肤色、身材比例、姿势、镜头角度、背景与光线全部沿用图1，不得换人、不得改变姿势与背景。
图2：服装参考图——这是唯一正确的服装来源。
换装要求（必须严格执行）：
1) 先把图1人物身上原来的衣服完全去掉，不要保留图1任何原有的衣物、颜色、花纹或款式；
2) 然后让她穿上图2中的整套服装，款式、颜色、面料质感、图案、蕾丝或刺绣、领型、袖长、衣长、版型必须与图2完全一致，不得改成别的款式，不得混搭图1原来的衣服；
3) 图2里是"上衣+长裤"就输出上衣+长裤、是"上装+裙子"就输出裙子；裤长/裙长必须与图2一致（图2是长裤就必须是到脚踝的长裤，不许改成短裤或短裙），下装完整覆盖腿部；
4) 服装自然贴合身体：肩线、腰线、袖口、下摆位置符合人体结构，褶皱与光影自然。
【重要】图1人物的姿势与图2模特很可能完全不同，这是正常的。
请把图2这套衣服"重新穿在"图1这个人的姿势与体态上：衣褶、垂坠感、衣摆与裤脚的走向，都要按图1这个人的姿势、四肢位置和重力方向重新生成；
严禁照搬图2里模特的身体动作、手臂位置、腿部动作，也严禁照搬图2里衣服褶皱的形状与走向（那属于那个模特的姿势）。衣服的"款式与颜色"照图2，"穿出来的形态"照图1。
{extra_line}
严格约束：只输出一个人物照片，不要保留拼图或分格布局；不出现文字、水印、logo；不增减肢体，只有两条手臂两只手，手指结构正常；脸不变形、不模糊。
画面为写实摄影质感，清晰自然。"""

VTON_NEG = "保留原来的衣服，原来的衣服，旧衣服，混搭，衣服没换，款式不一致，颜色错误，图案错误，换人，改变脸型，改变五官，身材变形，多余的手，多余的手指，手部畸形，衣服与身体分离，衣服悬空，透视错误，拼图，分格画面，边框，文字，水印，logo，模糊，噪点，低俗，裸露"

HAIR_TMPL = """只把这个人的发型换掉，其他一切都不许动。
图1：人物照片——这是要保留的人。脸型、五官、肤色、身材比例、姿势、镜头角度、背景、光线，以及**身上穿的服装**全部沿用图1，一律不得改变。
{ref_line}
换发型要求（严格执行）：
1) 只改头发：长度、厚薄、刘海有无、卷还是直、发色、分缝位置、发际线形状，按上面的要求来；
2) 只参考"头发"这一个东西。严禁把参考图里的衣服、姿势、背景、灯光、首饰搬到输出里；
3) 新发型要贴合图1的头骨与脸型，发丝走向、蓬松度、光影自然，发际线衔接不生硬，不要有头盔感或假发感；
4) 图1的服装、手脚、姿势、背景必须与图1保持一致，不许顺手改掉。
严格约束：只输出一个人的照片，不要拼图或分格布局；不出现文字、水印、logo；脸不变形、不模糊、不换人。
画面为写实摄影质感，清晰自然。"""

HAIR_NEG = ("改变服装，换衣服，参考图的衣服，参考图的姿势，参考图的背景，改变姿势，改变背景，换人，"
            "改变脸型，改变五官，头盔感，假发感，头发糊成一团，发丝粘连，多余的手，拼图，分格画面，"
            "边框，文字，水印，logo，模糊，噪点")

RESTORE_POS = """修复这张老照片并做自然上色：
1) 去除划痕、霉斑、折痕、噪点与压缩块，修补破损与缺失区域；
2) 提升清晰度与细节：五官、发丝、衣物纹理、背景细节都清晰可辨，不糊、不塑料感；
3) 自然上色：肤色为亚洲人自然肤色，衣服、天空、树木、建筑、地面颜色符合年代与场景常识，颜色协调、饱和度克制，不做成艳俗的彩色；
4) 保持原照片的构图、人物数量、人物位置、姿势与表情完全不变，不增删任何人物或物体；
5) 保持老照片的颗粒与年代质感。"""
RESTORE_NEG = "改变构图，增删人物，改变表情，改变姿势，脸部变形，五官错位，多余的手指，多手多脚，塑料感，过度磨皮，过度锐化，艳俗配色，文字，水印，logo，模糊"

SCENE_TMPL = """把人物照片里的这个人放到新的场景中，输出一张新的单人照片。
图1：人物照片——要保留的人。她的脸型、五官、肤色、发型、身材比例、姿势全部沿用图1，不得换人、不得改变姿势和体态。
{fimg}
场景要求：{scene}
要求：
1) 背景与环境的透视、光线方向、色温要与人物的姿势和体积协调，人物脚踩地面或坐姿有支撑，影子方向一致；
2) 人物边缘与新背景的衔接自然，不要出现生硬的剪影边缘或白边；
3) 保留人物的服装不变（除非场景说明里另有要求）；
4) 画面为写实摄影质感，清晰自然，不出现文字、水印、logo。
严格约束：只输出一张单人照片，不要分格或拼图；不增减肢体，只有两条手臂两只手，手指结构正常；脸不变形、不模糊、不换人。"""

SCENE_NEG = "换人，改变脸型，改变五官，改变发型，身材变形，多余的手，多余的手指，手部畸形，人体扭曲，悬空，脚不着地，影子方向错误，剪影边缘，白边，抠图感，分格画面，拼图，边框，文字，水印，logo，模糊，噪点"

MOTION_PRESETS = {
    "wind": ("微风持续吹过：树叶与枝条明显摆动、地上的树影跟着晃动，衣角与头发被风吹得飘动，旗帜/布幔/火苗这类该飘该跳的一起动起来。",
             "1) 环境在动：风一阵一阵吹过，树叶与枝条**明显摆动起伏**（幅度要看得出来），旗帜、布幔、晾晒的衣物随风飘动，地上的树影跟着晃动。\n"
             "2) 人在动：所有人保持原来的站/坐姿势，像活人一样自然呼吸，胸口与肩膀轻微起伏，陆续自然眨眼，有人嘴角轻轻上扬微笑，头发与衣角被风吹动。\n"
             "3) 画面里如果有火苗、烛光、水面，火苗自然跳动、水面泛起细微波纹。"),
    "dance": ("画面中的人物在原地跳一段轻快的舞：双脚点踏、双手随节拍抬起打开收回、身体重心左右移动、肩膀与头部随节奏起伏。",
              "1) 人物**真的跳起来**：双脚在原地点踏（脚跟脚尖交替），双手随节拍抬起、打开、收回，胯部与肩膀左右摆动，头部随节奏小幅点动。\n"
              "2) 动作幅度明显、连贯有节奏、速度均匀，整段都在跳；衣物随动作自然摆动。\n"
              "3) 其他人保持原来的姿势，只有自然的呼吸、眨眼与轻微摆动。"),
    "walk": ("画面中的人物向前走动：抬腿迈步、手臂自然前后摆动、重心平稳移动，走出几步后自然停住。",
             "1) 人物**真的走起来**：抬腿、迈步、落脚，左右腿交替，手臂自然前后摆动，身体重心随步伐平稳前移；走几步后自然站住。\n"
             "2) 走动时衣物与头发随步伐摆动，脚步与地面接触自然，不要滑步、不要像贴纸一样整体平移。\n"
             "3) 其他人保持原来的姿势，只有自然呼吸与眨眼。"),
    "turn": ("画面中的人物转头看向镜头（或转身）：脖子与上身自然带动头部转动，转到位后停住。",
             "1) 人物**真的转头**：脖子与肩膀带动头部向左或向右自然转动，最后看向镜头；如果是转身，腰与上身一起转过去。\n"
             "2) 转动连贯、速度自然、不跳帧；脸在转动过程中始终清晰、不糊、不变形、不换脸。\n"
             "3) 其他人保持原来的姿势，只有自然呼吸与眨眼。"),
    "nod": ("画面中的人物对着镜头自然点头或轻摇：头部上下点动或左右轻摇，肩膀与上身随之小幅摆动。",
            "1) 人物**真的在动**：头部上下点头两三次，或左右轻轻摇两三次，脖子自然带动，肩膀与上身跟着小幅摆动，像在跟人打招呼、说话。\n"
            "2) 动作幅度看得出来、节奏自然；脸始终清晰、不变形、不换脸。\n"
            "3) 其他人保持原来的姿势，只有自然呼吸与眨眼。"),
    "talk": ("画面中的人物对着镜头张嘴说话，嘴唇自然开合、下颌随语速移动，边讲边有很小的手势。",
             "1) 人物开始讲话：嘴唇随说话自然开合、下颌上下移动，牙齿与舌头在口腔内自然出现但不夸张。\n"
             "2) 讲话时规律眨眼，眉毛随语气轻微起伏，头部只有极小幅的点动。\n"
             "3) 其余人物与环境保持原有状态，只有轻微的自然微动。"),
    "seated": ("画面中坐着的人物做轻快律动：上身随节拍左右摇摆，肩膀一起一伏，双手轻轻抬起又放下，腿保持原来的坐姿。",
               "1) 人物坐在原处做律动：上半身随节拍左右轻摆，肩膀交替起伏，双手自然抬起、放下或轻拍膝盖，头随节奏点动。\n"
               "2) 坐姿与椅子的位置始终不变，双脚不移动、不站起来；动作连贯有节奏，不跳帧、不突变。\n"
               "3) 衣物随身体摆动自然晃动；背景、椅子、桌面与其余物品保持原样。"),
}

# 会"动到人身上"的动作模板：这些动作在"锁背景"里做不出来（那只把原图一小块挪来挪去），必须整幅重绘。
PERSON_ACTIONS = ("dance", "walk", "turn", "nod", "talk", "seated")

MOTION_TMPL = """subject_definitions:
<Picture 1> 是一张老照片。画面里的人物长相、服装、人数与场景沿用 <Picture 1>：还是同一批人、同一张脸、同一套衣服、同一个地方。

summary:
[reference generation] 一个连续镜头：这张老照片真的活了起来。{action}

integrated_multimodal_description:
{action_detail}
动作要求：上面的动作要**真的做出来**——幅度看得出来、持续整段，不要只做几毫米的轻微晃动，也不许把某一块画面整体平移或旋转来冒充动作。
身份一致：每个人还是原来那张脸（五官、脸型、年龄、发型不变），衣服的颜色与款式不变；不管身体怎么动、头怎么转，脸都要清晰、稳定、不变形、不糊、不换脸。
镜头：摄影机固定不动，不推拉、不摇移，画面尺寸不变；背景仍是同一个地方、同一批陈设，只允许自然微动（树叶、旗帜、衣物、光影）。
画面里不出现文字、字幕、水印、光斑、烟雾或新增物体。
overall_soundscape: 轻微的环境声，没有人说话。
non_diegetic_music: 无。
视觉风格：保持老照片的自然色彩与年代质感，24fps，画面平稳清晰，无文字无字幕无水印。"""

TALK_AUDIO_TMPL = """subject_definitions:
<Picture 1> 是人物照片。画面中的人物、服装、发型、姿势、站位、背景与构图严格沿用 <Picture 1>，不得换人、不得增减人数、不得移动位置。
参考音频：这段音频就是这个人物自己的声音，请让画面中的人物按这段音频开口说话。

summary:
[reference generation] 一个连续镜头：照片里的人对着镜头说话，口型跟着音频的节奏开合。

integrated_multimodal_description:
1) 讲话：嘴唇按音频的节奏自然开合，下颌随语速上下移动，脸颊与下巴肌肉随之轻动，必要时露出牙齿。
2) 表情与身体：规律眨眼，眉毛随语气轻微起伏，头部只有极小幅的点动，肩膀与胸口随呼吸自然起伏；边讲可以有很小的手势。
3) 音频里没有说话的部分（停顿、呼吸），人物闭嘴、自然呼吸并眨眼。
面部硬性要求：脸必须始终清晰稳定不变形——五官位置不变、脸型不变、眼睛鼻子嘴巴比例不变、不能糊、不能扭曲、不能变成别人的脸。
镜头固定不动，构图、人物位置与背景保持完全不变。画面里不出现文字、字幕、水印、光斑或新增物体。
overall_soundscape: 只有这个人说话的声音与轻微环境声。
non_diegetic_music: 无。
视觉风格：写实摄影质感，24fps，画面平稳清晰，无文字无字幕无水印。"""


# ---------------- 任务执行 ----------------
def subject_mask(img):
    """用本机抠图模型（BiRefNet）取"画面主体"的 mask。
    为什么不只靠人脸检测：插画、背影、商品、侧脸这些检测不到人脸，
    老逻辑就会退化成"只有树叶在动"——人一点没动（这是真实踩过的坑）。
    失败返回 None，由调用方兜底。"""
    try:
        name = f"subj_{int(time.time() * 1000)}.png"
        cv2.imwrite(os.path.join(INPUT_DIR, name), img)
        rel, _sec, _pid = submit_and_wait(matting_graph(name), lambda m: None)
        rgba = cv2.imdecode(np.fromfile(os.path.join(OUTPUT_DIR, rel), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if rgba is None or rgba.ndim != 3 or rgba.shape[2] != 4:
            return None
        return cv2.resize(rgba[:, :, 3], (img.shape[1], img.shape[0]), interpolation=cv2.INTER_NEAREST)
    except Exception:
        return None


def build_motion_mask(img, focus="auto"):
    """找出画面里"该动的东西"。

    focus="person"：只取主体/人物 —— 跳、走、说话、坐姿律动这些必须动到人身上，
                    绝不能因为图里有一片草地就去动草地。
    focus="scene"/"auto"：主体 ∪ 树叶 ∪ 人物（风吹微动那种）。
    返回 (mask, 说明)
    """
    h, w = img.shape[:2]
    notes = []
    # 0) 主体：本机抠图模型（插画、背影、商品都认得出）
    subj_m = np.zeros((h, w), np.uint8)
    subj = subject_mask(img)
    if subj is not None:
        s = ((subj > 16).astype(np.uint8)) * 255
        s = cv2.morphologyEx(s, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8), iterations=2)
        n0, l0, s0, _ = cv2.connectedComponentsWithStats(s, 8)
        for i in range(1, n0):
            if s0[i, cv2.CC_STAT_AREA] > (h * w) * 0.004:
                subj_m[l0 == i] = 255
        notes.append(f"主体 {subj_m.mean() / 255 * 100:.0f}%" if subj_m.any() else "主体没抠出来")
    else:
        notes.append("抠图模型没取到主体")

    # 1) 树叶：绿色分割
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    green = cv2.inRange(hsv, (25, 40, 40), (95, 255, 255))
    green = cv2.morphologyEx(green, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8), iterations=2)
    green = cv2.morphologyEx(green, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8), iterations=1)
    num, labels, stats, _ = cv2.connectedComponentsWithStats(green, 8)
    keep = np.zeros_like(green)
    for i in range(1, num):
        if stats[i, cv2.CC_STAT_AREA] > (h * w) * 0.003:
            keep[labels == i] = 255
    if keep.sum() > 0:
        notes.append(f"树叶区域 {keep.mean() / 255 * 100:.0f}%")

    # 2) 人脸/人物：insightface 检测人脸后向外扩展成人物区域
    person = np.zeros((h, w), np.uint8)
    nfaces = 0
    try:
        from insightface.app import FaceAnalysis
        app = FaceAnalysis(name="buffalo_l", root=FACE_ROOT,
                           allowed_modules=["detection"], providers=["CPUExecutionProvider"])
        app.prepare(ctx_id=-1, det_size=(640, 640))
        faces = app.get(img)
        nfaces = len(faces)
        for f in faces:
            x1, y1, x2, y2 = [int(v) for v in f.bbox]
            fw, fh = x2 - x1, y2 - y1
            if fw < 24:
                continue
            bx1 = max(0, x1 - int(fw * 1.6))
            bx2 = min(w, x2 + int(fw * 1.6))
            by1 = max(0, y1 - int(fh * 1.2))
            by2 = min(h, y2 + int(fh * 3.4))
            cv2.rectangle(person, (bx1, by1), (bx2, by2), 255, -1)
        if faces:
            notes.append(f"人脸 {len(faces)} 个")
    except Exception as e:
        notes.append(f"人脸检测跳过({type(e).__name__})")

    if focus == "person":
        mask = cv2.bitwise_or(subj_m, person)
        notes.append("只要主体/人物")
    else:
        mask = cv2.bitwise_or(cv2.bitwise_or(keep, person), subj_m)
    if mask.sum() == 0:
        return None, "没找到可动的区域（主体/人脸/树叶都没取到）"

    def _cov(m):
        return float((m > 0).mean())

    # —— 遮罩太大就收窄，否则"锁背景"会退化成整幅重绘 ——
    if _cov(mask) > 0.45 and keep.any() and _cov(keep) < _cov(mask):
        n2, l2, s2, _ = cv2.connectedComponentsWithStats(keep, 8)
        if n2 > 1:
            big = 1 + int(np.argmax(s2[1:, cv2.CC_STAT_AREA]))
            leaf1 = np.where(l2 == big, 255, 0).astype(np.uint8)
            if _cov(leaf1) < _cov(mask):
                notes.append(f"可动区域过大({_cov(mask) * 100:.0f}%)→收窄为最大一块树叶({_cov(leaf1) * 100:.0f}%)")
                mask = leaf1
    if _cov(mask) > 0.45 and nfaces:
        n3, l3, s3, _ = cv2.connectedComponentsWithStats(person, 8)
        if n3 > 1:
            big = 1 + int(np.argmax(s3[1:, cv2.CC_STAT_AREA]))
            p1 = np.where(l3 == big, 255, 0).astype(np.uint8)
            if _cov(p1) < _cov(mask):
                notes.append(f"→再收窄为最大的一位人物({_cov(p1) * 100:.0f}%)")
                mask = p1
    notes.append(f"最终遮罩 {_cov(mask) * 100:.0f}%")
    mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=max(3, max(h, w) // 150))
    return mask, " · ".join(notes)


def locked_motion(src_path, prompt, out_prefix, length=124, short_edge=640, focus="auto"):
    """锁背景动效：只把『该动的区域』裁出来交给 H3 生成动作，再贴回静止原图。
    返回 (输出视频绝对路径, 说明文字)"""
    img = cv2.imdecode(np.fromfile(src_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("读不到源图")
    H, W = img.shape[:2]
    mask, note = build_motion_mask(img, focus=focus)
    if mask is None:
        raise RuntimeError(f"无法锁定背景：{note}")
    ys, xs = np.where(mask > 20)
    pad = 48
    x1, x2 = max(0, xs.min() - pad), min(W, xs.max() + pad)
    y1, y2 = max(0, ys.min() - pad), min(H, ys.max() + pad)
    cw, ch = x2 - x1, y2 - y1
    # 尺寸对齐到 32 的倍数
    cw, ch = max(32, cw // 32 * 32), max(32, ch // 32 * 32)
    x2, y2 = min(W, x1 + cw), min(H, y1 + ch)
    crop = img[y1:y2, x1:x2].copy()
    cmask = mask[y1:y2, x1:x2]
    # 缩放到 H3 合适的尺寸（短边 short_edge，32 的倍数）
    scale = short_edge / min(crop.shape[0], crop.shape[1])
    tw = max(32, int(round(crop.shape[1] * scale / 32)) * 32)
    th = max(32, int(round(crop.shape[0] * scale / 32)) * 32)
    crop_small = cv2.resize(crop, (tw, th), interpolation=cv2.INTER_AREA)
    crop_name = f"lockcrop_{int(time.time())}.png"
    cv2.imwrite(os.path.join(INPUT_DIR, crop_name), crop_small)
    return {"crop_name": crop_name, "tw": tw, "th": th, "crop": crop, "cmask": cmask,
            "box": (x1, y1, x2, y2), "img": img, "note": note, "length": length}


def compose_locked(spec, video_path, out_path, use_flow=True):
    """把生成出来的"运动"贴回静止原图，输出成片。

    use_flow=True（默认，也是正确的做法）：H3 生成的那段**只当运动来源**——用光流估出它的位移场，
    再把**原照片的像素**按这个位移搬动。这样贴回区域里始终是原照片本身：
      首帧＝原图（一个像素都不差）→ 后面帧是原照片自己在动。
    旧做法（use_flow=False）是把 H3 **重画**的像素直接贴回去，而 H3 每帧都会重新生成纹理，
    贴回的那块必然和原照片不一样 → 看上去就是"半张照片在扭、在闪、边界发虚"。只在光流算不出来时兜底用。
    """
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    frames = []
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        frames.append(fr)
    cap.release()
    if not frames:
        raise RuntimeError("生成的那段视频读不出来")
    img, (x1, y1, x2, y2) = spec["img"], spec["box"]
    cmask = spec["cmask"].astype(np.float32) / 255.0
    cmask3 = np.repeat(cmask[:, :, None], 3, axis=2)
    h, w = y2 - y1, x2 - x1
    orig = spec["crop"].astype(np.float32)          # 原照片里那一块：要用它的像素，不用模型的
    th, tw = spec["th"], spec["tw"]
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    flows = None
    if use_flow and len(frames) > 1:
        try:
            small = [cv2.resize(f, (tw, th), interpolation=cv2.INTER_AREA) for f in frames]
            g0 = cv2.cvtColor(small[0], cv2.COLOR_BGR2GRAY)
            sx, sy = w / float(tw), h / float(th)
            flows = []
            for f in small:
                g1 = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
                fl = cv2.calcOpticalFlowFarneback(g0, g1, None, 0.5, 3, 21, 3, 5, 1.2, 0)
                fl = cv2.resize(fl, (w, h), interpolation=cv2.INTER_LINEAR)
                fl[:, :, 0] *= sx
                fl[:, :, 1] *= sy
                flows.append(fl)
        except Exception:
            flows = None
    tmp = os.path.join(tempfile.gettempdir(), f"locked_{int(time.time())}.mp4")
    vw = cv2.VideoWriter(tmp, cv2.VideoWriter_fourcc(*"mp4v"), fps, (img.shape[1], img.shape[0]))
    n = 0
    for i, fr in enumerate(frames):
        canvas = img.copy()
        if flows is not None:
            fl = flows[i]
            piece = cv2.remap(orig, gx + fl[:, :, 0], gy + fl[:, :, 1],
                              cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)
        else:
            piece = cv2.resize(fr, (w, h), interpolation=cv2.INTER_LANCZOS4).astype(np.float32)
        region = canvas[y1:y2, x1:x2].astype(np.float32)
        blended = region * (1 - cmask3) + piece * cmask3
        canvas[y1:y2, x1:x2] = np.clip(blended, 0, 255).astype(np.uint8)
        vw.write(canvas)
        n += 1
    vw.release()
    cmd = [FFMPEG_BIN, "-y", "-v", "error", "-i", tmp]
    if os.path.exists(video_path):
        cmd += ["-i", video_path, "-map", "0:v:0", "-map", "1:a:0?", "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", out_path]
    run_hidden(cmd, check=True)
    try:
        os.remove(tmp)
    except OSError:
        pass
    return n


GARMENT_EXTRACT_POS = """把这张图里的服装单独提取出来，做成一张服装商品图。
要求：
1) 只保留画中人物身上穿的那套服装（上衣/外套 + 裤装或裙装 + 内搭），把它完整、平展地呈现出来，像电商白底服装平铺图或挂拍图；
2) 完整保留这件衣服的一切细节：款式、颜色、面料质感、图案、蕾丝、刺绣、纽扣、拉链、领型、袖长、衣长、版型比例；
3) 画面里不要出现人：不要脸、不要头、不要手、不要脚、不要身体、不要任何人体部位，也不要出现第二个模特；
4) 背景换成干净的纯白色，画面中只有这一套衣服，衣服完整居中、不被裁切；
5) 写实商品摄影质感，清晰、干净，不出现文字、水印、logo。"""

GARMENT_EXTRACT_NEG = "人物，模特，人脸，头，手，手指，脚，身体，皮肤，人体，多人，背景杂物，家具，房间，文字，水印，logo，衣服被裁切，模糊，噪点，变形"


def extract_garment(src_name, save_as, turbo=False):
    """把服装图里的衣服抠出来做成白底服装图，返回新的 input 文件名"""
    graph = qwen_graph([src_name], GARMENT_EXTRACT_POS, GARMENT_EXTRACT_NEG, turbo=turbo)
    rel, secs, _ = submit_and_wait(graph, lambda m: None)
    src = os.path.join(OUTPUT_DIR, rel)
    img = cv2.imdecode(np.fromfile(src, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None, secs, rel
    dst = os.path.join(INPUT_DIR, save_as)
    ok, buf = cv2.imencode(".png", img)
    if ok:
        buf.tofile(dst)
    return save_as, secs, rel


# ---------------- 子进程一律不弹黑窗口（用户明确要求：运行时任何时候不许跳黑窗）----------------
NO_WINDOW = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW


def _hidden_kw(kw):
    """给子进程调用补上隐藏窗口（ffmpeg/powershell/taskkill 这些默认会闪一个黑窗）"""
    if os.name == "nt":
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0                                # SW_HIDE
            kw.setdefault("startupinfo", si)
        except Exception:
            pass
        if not kw.get("creationflags"):
            kw["creationflags"] = NO_WINDOW
    return kw


def run_hidden(cmd, **kw):
    return subprocess.run(cmd, **_hidden_kw(dict(kw)))


def popen_hidden(cmd, **kw):
    return subprocess.Popen(cmd, **_hidden_kw(dict(kw)))


def popen_gui(cmd, **kw):
    """给"本来就要弹窗"的程序用（打开资源管理器/定位文件）。
    注意：绝不能给它套 SW_HIDE —— 那会把资源管理器窗口一起藏起来，用户点「📂 出图」就当没反应（踩过）。"""
    return subprocess.Popen(cmd, **kw)



# ---------------- 抠图（去背景）：本地 BiRefNet / Lucida，不采样、不联网、不花钱 ----------------
BG_MODELS = ("birefnet.safetensors", "lucida.safetensors")
BG_MODEL = BG_MODELS[0]
BG_MIN_FG = 0.002      # 前景占比低于 0.2% 视为"没抠出来"（整张透明）→ 报错，不许静默交差


def matting_graph(image_name, model=BG_MODEL, prefix="QwenEdit/panel_cutout"):
    """抠图：LoadBackgroundRemovalModel → RemoveBackground(出前景 MASK) → 透明 PNG。

    坑（已查 ComfyUI 源码 comfy_extras/nodes_compositing.py）：JoinImageWithAlpha 内部写的是
    `alpha = 1.0 - mask` —— 它把 mask 当"要抠掉/变透明的区域"，而 RemoveBackground 给的是
    **前景** mask（前景=255）。直接接会正好反过来（把人挖空、把背景留下）。
    所以中间必须夹一个 InvertMask。
    """
    return {
        "1": {"class_type": "LoadBackgroundRemovalModel", "inputs": {"bg_removal_name": model}},
        "2": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "3": {"class_type": "RemoveBackground", "inputs": {"bg_removal_model": ["1", 0],
                                                           "image": ["2", 0]}},
        "4": {"class_type": "InvertMask", "inputs": {"mask": ["3", 0]}},
        "5": {"class_type": "JoinImageWithAlpha", "inputs": {"image": ["2", 0], "alpha": ["4", 0]}},
        "6": {"class_type": "SaveImage", "inputs": {"images": ["5", 0], "filename_prefix": prefix}},
    }


def cutout(src_name, save_as=None, model=BG_MODEL, white=False):
    """把一张图抠成透明 PNG（white=True 压成白底），返回 (input 文件名, 秒, comfy 相对路径)"""
    rel, secs, _pid = submit_and_wait(matting_graph(src_name, model), lambda m: None)
    img = cv2.imdecode(np.fromfile(os.path.join(OUTPUT_DIR, rel), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise RuntimeError(f"抠图结果读不出来：{rel}")
    if img.ndim != 3 or img.shape[2] != 4:
        raise RuntimeError("抠图没出 alpha 通道（模型没加载上？），不当成功交差")
    fg = float((img[:, :, 3] > 16).mean())
    if fg < BG_MIN_FG:
        raise RuntimeError(f"这张图没抠出前景（不透明像素仅 {fg * 100:.2f}%），换张图或换模型")
    rel_out = rel
    if white:
        a = img[:, :, 3:4].astype(np.float32) / 255.0
        img = np.clip(img[:, :, :3].astype(np.float32) * a + 255.0 * (1.0 - a), 0, 255).astype(np.uint8)
        # 白底版要另存到出图目录：否则界面拿到的还是上面那张透明 PNG（两回事）
        rel_out = f"QwenEdit/cutout_white_{int(time.time())}.png"
        ok, buf = cv2.imencode(".png", img)
        if not ok:
            raise RuntimeError("写白底 PNG 失败")
        buf.tofile(os.path.join(OUTPUT_DIR, rel_out))
    if not save_as:
        save_as = f"cutout_{int(time.time())}.png"
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("写 PNG 失败")
    buf.tofile(os.path.join(INPUT_DIR, save_as))
    return save_as, secs, rel_out


# ---------------- 多图合成一个镜头（fuse）：把 N 张照片融成"一张完整的照片" ----------------
# 背景（2026-10-03 用户实测）：把 9 张照片当"参考图"丢给编辑模型 + 一句笼统提示词，
# 模型只会挑 2~3 张当主角、其余缩成一点点。**构图权必须我们自己拿住**：
#   ① 用本机 BiRefNet 把"整栋耸立"的照片抠成透明 PNG；
#   ② 抠不出主体的（鸟瞰/公园这类大场景）降级成"远景雾带"；
#   ③ 我们自己把主体排成天际线（位置/大小/地平线全定死）拼成一张图；
#   ④ 把这张图当**唯一参考图**交给 Qwen 编辑模型，提示词只要求"统一天空/光线/去拼接痕迹"，
#      并写明"每栋的位置大小不许动" —— 它就没机会把谁缩小。
FUSE_POS = """把这张合成图整理成一张真实拍摄的照片。硬要求：
1) 画面里的每一个主体/建筑，位置、大小、数量都必须保持原样：不许把任何主体缩小、挪走、删掉，也不许新增主体；
2) 统一天空、云、光线与色调（默认黄昏蓝调：天空由深蓝渐变到地平线的暖橙余光）；所有主体的受光方向一致，玻璃/金属有统一的反光，夜景的灯光自然保留但不刺眼；
3) 地面/水面统一处理，去掉所有拼接痕迹、色块断层和硬边，边缘自然过渡；
4) 远处做空气透视（薄雾），近处主体保持清晰锐利、细节真实；
5) 写实摄影质感，不要文字、水印、边框、logo。"""

FUSE_NEG = "拼贴感，色块断层，硬边，接缝，重复的楼，被缩小成一点的楼，模糊，过曝，文字，水印，边框"


def _fx_tight(img):
    """按 alpha 去掉四周空白（留一点呼吸），返回裁剪后的 RGBA"""
    a = img[:, :, 3]
    ys, xs = np.where(a > 32)
    if len(ys) == 0:
        return img
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    pad = int((x1 - x0) * 0.04)
    return img[y0:y1, max(0, x0 - pad):min(img.shape[1], x1 + pad)]


def _fx_haze(img, k=0.35, blur=0):
    """空气透视：往淡灰蓝里混一点 + 可选模糊（远处城市用）"""
    if blur:
        img = cv2.GaussianBlur(img, (blur | 1, blur | 1), 0)
    haze = np.array([168, 158, 150], np.float32)
    return np.clip(img.astype(np.float32) * (1 - k) + haze * k, 0, 255).astype(np.uint8)


def _fx_fit_w(img, w):
    h = max(1, int(img.shape[0] * w / img.shape[1]))
    return cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)


def _clean_fuse_temp(max_age=3600):
    """清掉自己造的临时件（fuse_src_* / fuse_up_* / _fuse_cut_*）。

    ⚠ 只认这几个**专用前缀**，而且默认只删 1 小时前的 —— 2026-10-03 我用一条
    `^panel_17` 的宽匹配清理，把 input 里历史上传的副本删了 639 个（原图没动，但历史记录
    引用断了）。教训：清理只准按自己造的专用前缀来，绝不用宽匹配。
    """
    now = time.time()
    for d in (INPUT_DIR, OUTPUT_DIR, os.path.join(OUTPUT_DIR, "QwenEdit")):
        try:
            for f in os.listdir(d):
                if f.startswith(("fuse_src_", "fuse_up_", "_fuse_cut_")):
                    p = os.path.join(d, f)
                    if os.path.isfile(p) and now - os.path.getmtime(p) > max_age:
                        os.remove(p)
        except OSError:
            pass


def fuse_matte_all(names, say):
    """逐张抠图并分类：返回 (主体 RGBA 列表, 远景照片文件名列表)。

    ⚠ 一张一张来、之间歇几秒 —— 本项目的铁律：GPU 上不并发堆任务。
    """
    towers, scenes = [], []
    for i, nm in enumerate(names, start=1):
        try:
            say(f"抠图 {i}/{len(names)} …")
            # 中间图用专用前缀，读完立刻删掉：不然会一堆抠图堆进用户的「作品栏」
            rel, _secs, _pid = submit_and_wait(matting_graph(nm, prefix="QwenEdit/_fuse_cut"), lambda m: None)
            _p = os.path.join(OUTPUT_DIR, rel.replace("/", os.sep))
            img = cv2.imdecode(np.fromfile(_p, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
            try:
                if os.path.isfile(_p):
                    os.remove(_p)
            except OSError:
                pass
            if img is None or img.ndim != 3 or img.shape[2] != 4:
                scenes.append(nm)
                say(f"  {i}: 抠不出透明主体 → 当远景用")
                continue
            fg = float((img[:, :, 3] > 16).mean())
            t = _fx_tight(img)
            ar = t.shape[1] / max(1, t.shape[0])
            # 上限 0.80：整幅都是内容的图（九宫格拼图、满构图特写）不该当"主体"整块搬进来
            # （2026-10-03 端到端测试就撞上过：一张 9 格拼图前景 85%，被当成主楼整张贴进去）
            if 0.10 <= fg <= 0.80 and ar <= 1.6:
                towers.append(t)
                say(f"  {i}: 主体 ✓（前景 {fg * 100:.0f}% · 长宽比 {ar:.2f}）")
            else:
                scenes.append(nm)
                say(f"  {i}: 是大场景（前景 {fg * 100:.0f}% · 长宽比 {ar:.2f}）→ 当远景用")
        except Exception as e:
            scenes.append(nm)
            say(f"  {i}: 抠图没成（{type(e).__name__}）→ 当远景用")
        if i < len(names):
            time.sleep(6)
    return towers, scenes


def fuse_compose(towers, scenes, W, H, say, prefix="fuse_src"):
    """排版：黄昏天空渐变 + 远景雾带（用大场景照）+ 主体站齐 + 水面倒影。

    返回写进 INPUT_DIR 的文件名（供 LoadImage 用）。
    """
    W, H = int(W), int(H)
    horizon = int(H * 0.78)
    canvas = np.zeros((H, W, 3), np.uint8)
    for y in range(horizon):
        t = (y / max(1, horizon)) ** 1.25
        canvas[y, :] = ((1 - t) * np.array([92, 56, 36], np.float32)
                        + t * np.array([158, 176, 214], np.float32)).astype(np.uint8)
    canvas[horizon:, :] = (70, 68, 66)

    def band(path, w_frac, h_frac, x_frac, y_bottom, haze_k, blur):
        try:
            img = cv2.imdecode(np.fromfile(os.path.join(INPUT_DIR, path), dtype=np.uint8), cv2.IMREAD_COLOR)
        except Exception:
            return
        if img is None:
            return
        img = _fx_fit_w(img, max(64, int(W * w_frac)))
        hh = max(24, int(H * h_frac))
        y0 = int(img.shape[0] * 0.62)
        crop = img[y0:y0 + hh, :]
        if crop.size == 0:
            crop = img[:hh, :]
        crop = _fx_haze(crop, haze_k, blur)
        cw = crop.shape[1]
        a = np.ones((crop.shape[0], cw), np.float32)
        ft = max(2, int(crop.shape[0] * 0.45))
        fb = max(2, int(crop.shape[0] * 0.25))
        a[:ft, :] *= np.linspace(0, 1, ft).reshape(-1, 1)
        a[crop.shape[0] - fb:, :] = np.linspace(1, 0.65, fb).reshape(-1, 1)
        fx = max(2, int(cw * 0.06))
        a[:, :fx] *= np.linspace(0, 1, fx)
        a[:, cw - fx:] *= np.linspace(1, 0, fx)
        x = max(0, min(W - cw, int(W * x_frac)))
        y = max(0, min(H - crop.shape[0], int(y_bottom) - crop.shape[0]))
        roi = canvas[y:y + crop.shape[0], x:x + cw].astype(np.float32)
        canvas[y:y + crop.shape[0], x:x + cw] = (crop.astype(np.float32) * a[..., None]
                                                 + roi * (1 - a[..., None])).astype(np.uint8)

    # 远景：最多 3 条薄带（大场景照），越靠后越淡
    spots = [(1.00, 0.15, 0.00, horizon, 0.55, 3),
             (0.72, 0.10, 0.28, horizon - int(H * 0.045), 0.68, 5),
             (0.52, 0.09, 0.02, horizon - int(H * 0.02), 0.62, 4)]
    for k, sc in enumerate(scenes[:3]):
        wf, hf, xf, yb, hk, bl = spots[k]
        band(sc, wf, hf, xf, yb, hk, bl)
    if scenes:
        say(f"远景用掉 {min(3, len(scenes))} 张大场景照（其余 {max(0, len(scenes) - 3)} 张太挤，这次没上）")

    # 主体：按上传顺序站齐，高低错落
    if towers:
        pat = [0.95, 0.76, 0.86, 0.92, 0.70, 0.82, 0.88, 0.74, 0.90, 0.80]
        items = []
        for i, c in enumerate(towers):
            th = max(120, int(horizon * pat[i % len(pat)]))
            tw = max(50, int(c.shape[1] * th / c.shape[0]))
            items.append([c, tw, th])
        total = sum(it[1] for it in items)
        if total > W * 0.99:
            kk = W * 0.99 / total
            for it in items:
                it[1] = max(50, int(it[1] * kk))
                it[2] = max(120, int(it[2] * kk))
        total = sum(it[1] for it in items)
        x = max(0, int((W - total) / 2))
        for c, tw, th in items:
            small = cv2.resize(c, (tw, th), interpolation=cv2.INTER_AREA)
            y0 = max(0, horizon - th)
            alpha = small[:, :, 3].astype(np.float32) / 255.0
            fade = max(3, int(th * 0.04))
            alpha[th - fade:, :] *= np.linspace(1, 0.55, fade).reshape(-1, 1)
            alpha = alpha[..., None]
            x1 = min(W, x + tw)
            roi = canvas[y0:y0 + th, x:x1].astype(np.float32)
            canvas[y0:y0 + th, x:x1] = (small[:, :tw if x1 - x == tw else x1 - x, :3].astype(np.float32)
                                        * alpha[:, :x1 - x] + roi * (1 - alpha[:, :x1 - x])).astype(np.uint8)
            x += tw

    # 水面 + 弱倒影
    for y in range(horizon, H):
        t = (y - horizon) / max(1, H - horizon)
        canvas[y, :] = ((1 - t) * np.array([96, 88, 84], np.float32)
                        + t * np.array([46, 44, 44], np.float32)).astype(np.uint8)
    b = canvas[max(0, horizon - int(H * 0.22)):horizon, :].astype(np.float32)
    if b.size:
        refl = cv2.GaussianBlur(cv2.flip(b, 0), (0, 0), 2.0) * 0.30
        h2 = min(refl.shape[0], H - horizon)
        roi = canvas[horizon:horizon + h2, :].astype(np.float32)
        canvas[horizon:horizon + h2, :] = np.clip(roi * 0.72 + refl[:h2] * 0.85, 0, 255).astype(np.uint8)

    name = f"{prefix}_{int(time.time())}.png"
    ok, buf = cv2.imencode(".png", canvas)
    if not ok:
        raise RuntimeError("写合成图失败")
    buf.tofile(os.path.join(INPUT_DIR, name))
    say(f"排版完成：{W}x{H}，{len(towers)} 栋主体站在同一条地平线上")
    return name


def esrgan_upscale(rel, say, target_w=None, model="RealESRGAN_x4.pth"):
    """RealESRGAN ×4 超分（本机模型），再收到 target_w。失败就返回 None（由调用方按原图交差）。"""
    src = os.path.join(OUTPUT_DIR, rel.replace("/", os.sep))
    if not os.path.isfile(src):
        return None
    name = f"fuse_up_{int(time.time())}.png"
    import shutil as _sh
    _sh.copyfile(src, os.path.join(INPUT_DIR, name))
    graph = {
        "1": {"class_type": "LoadImage", "inputs": {"image": name}},
        "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": model}},
        "3": {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}},
        "4": {"class_type": "SaveImage", "inputs": {"images": ["3", 0],
                                                    "filename_prefix": "QwenEdit/panel_fuse_up"}},
    }
    try:
        rel_up, _secs, _pid = submit_and_wait(graph, lambda m: None)
        img = cv2.imdecode(np.fromfile(os.path.join(OUTPUT_DIR, rel_up.replace("/", os.sep)),
                                       dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception as e:
        say(f"超分这一步没成（{type(e).__name__}: {str(e)[:100]}）——这张按原尺寸交，不糊弄")
        return None
    if img is None:
        return None
    if target_w and img.shape[1] > target_w:
        img = cv2.resize(img, (int(target_w), int(img.shape[0] * target_w / img.shape[1])),
                         interpolation=cv2.INTER_AREA)
    out_rel = f"QwenEdit/fuse_up_{int(time.time())}.png"
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        return None
    buf.tofile(os.path.join(OUTPUT_DIR, out_rel))
    say(f"超分完成：{img.shape[1]}x{img.shape[0]}（RealESRGAN ×4）")
    return out_rel


def rgba_from_output(rel_img, say=None, prefix="QwenEdit/panel_rgba"):
    """「透明背景」（照同类软件那个开关的用途）：把 output 里的成图拷进 input，再用本机 BiRefNet
    抠成带 alpha 的 RGBA PNG。**不采样、不联网**，一个前向就完事。

    ⚠ 同类软件写的是"保存原生 RGBA PNG"（它自己的模型出 alpha）；我们这条是**出图后抠图**，
    所以界面上的说明不许写"原生"。抠不出 alpha / 前景太小一律抛错，由调用方保留原图并说明，
    绝不静默交一张透明空图。返回 (output 相对路径, 秒, 前景占比)。
    """
    import shutil as _sh
    src = os.path.join(OUTPUT_DIR, rel_img.replace("/", os.sep))
    if not os.path.isfile(src):
        base = os.path.basename(rel_img)
        cands = [os.path.join(OUTPUT_DIR, base), os.path.join(OUTPUT_DIR, "QwenEdit", base)]
        src = next((c for c in cands if os.path.isfile(c)), src)
    if not os.path.isfile(src):
        raise RuntimeError(f"找不到要抠的成图：{rel_img}")
    name = f"rgba{int(time.time())}_{os.path.basename(src)}"
    _sh.copyfile(src, os.path.join(INPUT_DIR, name))
    rel, secs, _pid = submit_and_wait(matting_graph(name, BG_MODEL, prefix), say or (lambda m: None))
    img = cv2.imdecode(np.fromfile(os.path.join(OUTPUT_DIR, rel), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise RuntimeError(f"抠图结果读不出来：{rel}")
    if img.ndim != 3 or img.shape[2] != 4:
        raise RuntimeError("抠图没出 alpha 通道（抠图模型没加载上？）")
    fg = float((img[:, :, 3] > 16).mean())
    if fg < BG_MIN_FG:
        raise RuntimeError(f"这张没抠出前景（不透明像素仅 {fg * 100:.2f}%）")
    return rel, secs, fg


POSE_MATCH_POS = """图1：人物照片，只提供这个人的身份（脸与发型）。
图2：服装照片，提供姿势、服装、场景、光线与构图。
任务：把图1这个人的脸和发型特征迁移到图2的人物身上，输出"图2这个姿势、这套衣服、这个场景下的这个人"。
要求：
1) 图2的服装必须完全不变：款式、颜色、面料、图案、蕾丝、纽扣、褶皱走向、垂感、长度一律沿用图2；
2) 图2的姿势、动作、手的位置、身体朝向、镜头角度、背景、光线、构图完全不变；
3) 只把脸换成图1这个人：脸型、五官、肤色按图1，自然融合到图2的头部，脖子与下颌衔接自然，发际线真实；
4) 图1里的衣服、背景一概不采用，只取这个人的脸和头；
5) 写实摄影质感，清晰自然，不出现文字、水印、logo。
严格约束：不改变图2的服装与姿势；不增减肢体；脸不变形、不模糊、不出现第二个人。"""

POSE_MATCH_NEG = "改变服装，衣服款式变化，改变姿势，改变动作，改变背景，改变光线，改变构图，多手多脚，多余手指，手部畸形，身体变形，换脸失败，脸变形，模糊，文字，水印，logo，第二个人"


def face_head_mask(img, grow=1.35):
    """服装图里"头部区域"的遮罩：只在这个范围里采用生成结果（换脸+换发型），其余一律用服装图原像素"""
    h, w = img.shape[:2]
    fb = _face_box(img) if _face_box is not None else None
    if not fb:
        return None, "服装图里没检测到人脸"
    x1, y1, x2, y2 = fb
    fw, fh = x2 - x1, y2 - y1
    mx1 = max(0, int(x1 - fw * 0.55 * grow))
    mx2 = min(w, int(x2 + fw * 0.55 * grow))
    my1 = max(0, int(y1 - fh * 0.75 * grow))
    my2 = min(h, int(y2 + fh * 0.55 * grow))     # 含脖子一点
    m = np.zeros((h, w), np.uint8)
    cv2.rectangle(m, (mx1, my1), (mx2, my2), 255, -1)
    m = cv2.GaussianBlur(m, (0, 0), sigmaX=max(4, min(h, w) // 90))
    return m, f"头部区 {mx1},{my1}-{mx2},{my2}"


_SWAPPER = {"model": None, "app": None}
# INSWAPPER / FACE_ROOT 已在文件顶部按配置算出（insightface_dir），这里不再写死路径


def _get_swapper():
    if _SWAPPER["model"] is None:
        import insightface
        from insightface.app import FaceAnalysis
        app = FaceAnalysis(name="buffalo_l", root=FACE_ROOT,
                           allowed_modules=["detection", "recognition"],
                           providers=["CPUExecutionProvider"])
        app.prepare(ctx_id=-1, det_size=(640, 640))
        _SWAPPER["app"] = app
        _SWAPPER["model"] = insightface.model_zoo.get_model(INSWAPPER, providers=["CPUExecutionProvider"])
    return _SWAPPER["app"], _SWAPPER["model"]


def faceswap_onto(base_name, face_name, identity=1.0):
    """把 face_name 这个人的脸换到 base_name 这张图的人身上（衣服/姿势/背景保持 base 原样）
    identity＝身份增强强度 0~1：1.0 完全照参考脸；调小＝把换过的脸按比例混回原脸（只在被改动的区域混）"""
    app, swapper = _get_swapper()

    def rd(n):
        return cv2.imdecode(np.fromfile(os.path.join(INPUT_DIR, n), dtype=np.uint8), cv2.IMREAD_COLOR)

    base, faceimg = rd(base_name), rd(face_name)
    if base is None or faceimg is None:
        raise RuntimeError("读图失败")
    bf = app.get(base)
    ff = app.get(faceimg)
    if not bf:
        raise RuntimeError("服装图里没检测到人脸")
    if not ff:
        raise RuntimeError("人物图里没检测到人脸")
    tf = max(bf, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
    sf = max(ff, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
    out = swapper.get(base, tf, sf, paste_back=True)
    try:
        ident = max(0.0, min(1.0, float(identity)))
    except (TypeError, ValueError):
        ident = 1.0
    if ident < 0.999 and out is not None:
        # 只把"被换脸动过的那一块"按强度混回原图，其余像素一个不动
        diff = np.abs(out.astype(np.int16) - base.astype(np.int16)).max(axis=2).astype(np.uint8)
        m = (diff > 12).astype(np.uint8) * 255
        if m.any():
            k = max(3, (int(max(base.shape[:2]) * 0.012) | 1))
            mm = cv2.GaussianBlur(m, (k, k), 0).astype(np.float32) / 255.0 * ident
            m3 = mm[:, :, None]
            out = (out.astype(np.float32) * m3 + base.astype(np.float32) * (1.0 - m3)).clip(0, 255).astype(np.uint8)
    rel = f"QwenEdit/panel_faceswap_{int(time.time())}.png"
    cv2.imwrite(os.path.join(OUTPUT_DIR, rel), out)
    return rel, f"已把脸换到服装图的人身上（{len(bf)} 个目标脸 / {len(ff)} 个来源脸 · 身份增强 {identity}）"


def face_sim(path_a, path_b):
    """两张图里最大人脸的相似度（越大越是同一个人）。用 insightface 的识别模型。"""
    try:
        app, _ = _get_swapper()
        a = cv2.imdecode(np.fromfile(path_a, dtype=np.uint8), cv2.IMREAD_COLOR)
        b = cv2.imdecode(np.fromfile(path_b, dtype=np.uint8), cv2.IMREAD_COLOR)
        if a is None or b is None:
            return 0.0, 0
        fa, fb = app.get(a), app.get(b)
        if not fa or not fb:
            return 0.0, len(fa)
        ga = max(fa, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
        gb = max(fb, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
        ea, eb = ga.normed_embedding, gb.normed_embedding
        return float(np.dot(ea, eb)), len(fa)
    except Exception:
        return 0.0, 0


def pose_face_lock(gen_rel, ref_names, say=None):
    """姿态成图『护脸』：姿势/表情/头发/背景全部沿用成图，只把脸换成人物参考图那张脸。

    为什么不用"头部像素贴回"（换场景那招）：姿态一换，头的位置、大小、朝向也跟着变，
    在本机实测过——硬贴会在头顶/边缘留下一块灰斑（源图背景被一起贴进去），而且错位。
    换脸模型是按成图自己的关键点定位并贴回的，跟姿势无关，出来是干净的。
    换完再量一次身份相似度：没变好就把新图删掉、保留原图，绝不把结果改坏。
    返回 (相对路径 或 原路径, 说明)
    """
    def _log(m):
        if say:
            try:
                say(m)
            except Exception:
                pass
    try:
        app, swapper = _get_swapper()
    except Exception as e:
        return gen_rel, f"换脸模型不可用（{type(e).__name__}），保持原图"
    base_path = gen_rel if os.path.isabs(gen_rel) else os.path.join(OUTPUT_DIR, gen_rel)
    base = cv2.imdecode(np.fromfile(base_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if base is None:
        return gen_rel, "读成图失败，保持原图"
    refs = [n for n in (ref_names or []) if n]
    srcs, ref0 = [], None
    for n in refs:
        p = n if os.path.isabs(n) else os.path.join(INPUT_DIR, os.path.basename(n))
        im = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)
        if im is None:
            continue
        fs = app.get(im)
        if fs:
            if ref0 is None:
                ref0 = p
            srcs.append(max(fs, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1])))
    if not srcs or ref0 is None:
        return gen_rel, "人物参考图里没检到脸，跳过护脸"
    _fw = max((s.bbox[2] - s.bbox[0]) for s in srcs)
    if _fw < 48:
        _log("⚠ ①人物参考图里那张脸只有约 %d 像素宽（多半是从作品栏拖了缩略图进来）—— "
             "锁脸能做的有限，建议换成原图/更清楚的正脸照" % int(_fw))
    tgt = app.get(base)
    if not tgt:
        return gen_rel, "成图里没检到脸，跳过护脸"
    if len(tgt) >= 2 and len(srcs) >= 2:
        pairs = list(zip(sorted(tgt, key=lambda x: x.bbox[0]), srcs))[:len(srcs)]
        note = f"双人：按左右顺序换 {len(pairs)} 张脸"
    else:
        pairs = [(max(tgt, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1])), srcs[0])]
        note = f"检测到 {len(tgt)} 张脸，只护了最大那张" if len(tgt) > 1 else "单人护脸"
    try:
        before, _n = face_sim(base_path, ref0)
    except Exception:
        before = 0.0
    out = base
    for tf, sf in pairs:
        try:
            out = swapper.get(out, tf, sf, paste_back=True)
        except Exception as e:
            return gen_rel, f"换脸失败（{type(e).__name__}），保持原图"
    if out is None:
        return gen_rel, "换脸返回空，保持原图"
    rel = f"QwenEdit/panel_poselock_{int(time.time() * 1000)}.png"
    dst = os.path.join(OUTPUT_DIR, rel)
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        cv2.imwrite(dst, out)
        after, _n2 = face_sim(dst, ref0)
    except Exception as e:
        _log(f"护脸写盘/复核失败（{type(e).__name__}），保持原图")
        return gen_rel, "护脸失败，保持原图"
    if after <= before + 0.02:
        try:
            os.remove(dst)
        except OSError:
            pass
        return gen_rel, f"换脸后身份相似度没提高（{before:.2f}→{after:.2f}），保持原图"
    # 换脸前那张（脸不对的）挪进两层子目录：作品栏只扫一层，所以它不会出现在「最近作品」里，
    # 免得同一张图上新出现"两张脸不一样的作品"，让人以为又修坏了。文件不删，还在硬盘上。
    try:
        import shutil
        hide = os.path.join(OUTPUT_DIR, "QwenEdit", "_中间", "护脸前")
        os.makedirs(hide, exist_ok=True)
        tgt = os.path.join(hide, os.path.basename(base_path))
        if os.path.exists(tgt):      # 同名中间图（ComfyUI 计数器会把名字用回来）→ 别覆盖，加个后缀
            root, ext = os.path.splitext(tgt)
            tgt = "%s_%d%s" % (root, int(time.time()), ext)
        shutil.move(base_path, tgt)
        _log("换脸前那张已挪到 QwenEdit/_中间/护脸前/（作品栏不显示；文件没删）")
    except Exception as e:
        _log(f"中间图挪动失败（{type(e).__name__}），留在原处（作品栏里会多一张脸不对的）")
    _log(f"护脸：身份相似度 {before:.2f} → {after:.2f}（{note}）")
    return rel, f"护脸 {before:.2f}→{after:.2f}"


def garment_sim(path_out, path_garment):
    """输出图的躯干区域与『衣服参考图』的颜色相似度（越大说明衣服越像参考服）"""
    try:
        a = torso_hist(path_out)
        b = torso_hist(path_garment)
        if a is None or b is None:
            return 0.0
        return float(cv2.compareHist(a.astype(np.float32), b.astype(np.float32), cv2.HISTCMP_CORREL))
    except Exception:
        return 0.0


def make_grid(paths, labels, out_path, cols=4, tile=420):
    """把多张候选拼成一张对比图"""
    tiles = []
    for p in paths:
        im = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)
        if im is None:
            continue
        r = tile / im.shape[0]
        tiles.append(cv2.resize(im, (max(1, int(im.shape[1] * r)), tile)))
    if not tiles:
        return None
    h = max(t.shape[0] for t in tiles)
    tiles = [cv2.copyMakeBorder(t, 0, h - t.shape[0], 0, 0, cv2.BORDER_CONSTANT, value=(255, 255, 255)) for t in tiles]
    while len(tiles) % cols:
        tiles.append(np.full_like(tiles[0], 255))
    rows = [np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)]
    grid = np.vstack(rows)
    for i, lab in enumerate(labels[:len(tiles)]):
        c, r = i % cols, i // cols
        cv2.putText(grid, lab, (c * tiles[0].shape[1] + 12, r * h + 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 6, cv2.LINE_AA)
        cv2.putText(grid, lab, (c * tiles[0].shape[1] + 12, r * h + 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (60, 220, 60), 2, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", grid, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    if ok:
        buf.tofile(out_path)
        return out_path
    return None


def torso_hist(path):
    """读图并给出『人物躯干区域』的色调直方图，用于判断衣服有没有真被换掉"""
    try:
        img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return None
    if img is None:
        return None
    h, w = img.shape[:2]
    roi = img[int(h * 0.22):int(h * 0.52), int(w * 0.28):int(w * 0.72)]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    m = ((hsv[..., 1] > 45) & (hsv[..., 2] > 40)).astype(np.uint8) * 255
    hist = cv2.calcHist([hsv], [0, 1], m, [30, 32], [0, 180, 0, 256])
    if hist.sum() < 10:
        return None
    return cv2.normalize(hist, hist).flatten()


def _hsv_hist_roi(img, y0, y1, x0, x1):
    """给一块按比例裁的区域算「饱和像素」的 HSV 直方图；样本太少返回 None"""
    h, w = img.shape[:2]
    roi = img[int(h * y0):int(h * y1), int(w * x0):int(w * x1)]
    if roi.size == 0:
        return None
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    m = ((hsv[..., 1] > 45) & (hsv[..., 2] > 40)).astype(np.uint8) * 255
    if int((m > 0).sum()) < 10:
        return None
    hist = cv2.calcHist([hsv], [0, 1], m, [30, 32], [0, 180, 0, 256])
    return cv2.normalize(hist, hist).flatten()


def change_score(out_path, person_path):
    """输出图与原始人物图的差异程度：越大说明衣服确实被换掉了。

    ⚠ 2026-10-03 修：原来只看纵向 22%~52% 那一块，对**半身/特写人像**来说那块正好是**脸**，
    衣服在画面下半 —— 于是衣服明明换得很彻底也被判成"几乎没改"。
    实测同一张换衣成图：官方 ROI 0.052（判 ❌ 不合格、0/4 全废），下半身 **0.655**、
    全图平均像素差 **29.4**（脸相似度 0.95，即衣服真的换了、脸也保住了）。
    现在改成：躯干 ROI 与下半身 ROI **取较大的那个**（样本不足就退回全图），
    这样"衣服改动"才真的代表衣服；合格线 0.08 不变。
    """
    try:
        a = cv2.imdecode(np.fromfile(out_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        b = cv2.imdecode(np.fromfile(person_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return 0.0
    if a is None or b is None:
        return 0.0
    best = None
    for roi in ((0.22, 0.52, 0.28, 0.72),      # 躯干（全身/半身构图时正确）
                (0.50, 1.00, 0.10, 0.90),      # 下半身（特写人像时衣服在这里）
                (0.00, 1.00, 0.00, 1.00)):     # 兜底：全图
        ha = _hsv_hist_roi(a, *roi)
        hb = _hsv_hist_roi(b, *roi)
        if ha is None or hb is None:
            continue
        v = max(0.0, min(1.0, 1.0 - float(cv2.compareHist(
            ha.astype(np.float32), hb.astype(np.float32), cv2.HISTCMP_CORREL))))
        best = v if best is None else max(best, v)
        if best >= 0.08:      # 已经能判"改动明显"，不用再往下算
            break
    return best if best is not None else 0.0


# ---------------- 历史记录（每次生成存一条，可回查"这张图是怎么来的"） ----------------
def new_run_id():
    return time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"


def run_dir_of(run_id):
    safe = re.sub(r"[^0-9A-Za-z_\-]", "", run_id or "")
    return os.path.join(RUNS_DIR, safe) if safe else ""


def record_run_start(task, files, params):
    """只记路径，不复制图片（省空间）：输入图本来就留在 input 目录里"""
    run_id = new_run_id()
    os.makedirs(os.path.join(RUNS_DIR, run_id), exist_ok=True)
    ins = {}
    for field, name in (files or {}).items():
        if name and os.path.exists(os.path.join(INPUT_DIR, name)):
            ins[field] = name
    meta = {"run_id": run_id, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "task": task, "task_name": TASK_NAMES.get(task, task),
            "params": {k: v for k, v in (params or {}).items() if k != "task"},
            "inputs": ins, "outputs": [], "result": None, "result_name": None,
            "note": None, "seconds": None, "error": None}
    try:
        with open(os.path.join(RUNS_DIR, run_id, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return run_id


def record_run_end(run_id, J):
    """把结果路径与分数写进记录，不复制结果文件"""
    d = run_dir_of(run_id)
    if not d:
        return
    path = os.path.join(d, "meta.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        meta = {"run_id": run_id}
    rel = urllib.parse.unquote((J.get("result") or "").replace("/file?p=", ""))
    outs = [rel] if rel and os.path.exists(os.path.join(OUTPUT_DIR, rel)) else []
    for c in (J.get("cands") or []):
        if c.get("rel") and c["rel"] not in outs:
            outs.append(c["rel"])
    meta.update({"outputs": outs, "result": rel, "result_name": J.get("result_name"),
                 "note": J.get("note"), "seconds": J.get("seconds"),
                 "error": J.get("error"), "cands": J.get("cands") or [],
                 "text": J.get("text") or "",
                 "prompt": J.get("prompt_pos") or "", "negative": J.get("prompt_neg") or "",
                 # 失败时把日志尾部（含调用栈那几行）也落盘 —— 出问题能直接查，不用猜
                 "log_tail": (J.get("log") or [])[-8:]})
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def list_runs(limit=300):
    out = []
    if not os.path.isdir(RUNS_DIR):
        return out
    for name in sorted(os.listdir(RUNS_DIR), reverse=True)[:limit]:
        p = os.path.join(RUNS_DIR, name, "meta.json")
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            continue
    return out


def sharpness(path):
    """清晰度（拉普拉斯方差）：纯色块/糊图的判别依据，正常照片 > 20，废图 < 12"""
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return 0.0
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def hair_change_score(out_path, person_path):
    """头发区域的变化程度：以脸框为基准往上/往两侧扩出一块"头发区"，比较直方图差异。
    换发型看这个（衣服不该变，所以不能用 change_score）。"""
    a = cv2.imdecode(np.fromfile(out_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    b = cv2.imdecode(np.fromfile(person_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if a is None or b is None or _face_box is None:
        return 0.0
    box = _face_box(b)
    if not box:
        return 0.0
    if a.shape[:2] != b.shape[:2]:
        a = cv2.resize(a, (b.shape[1], b.shape[0]), interpolation=cv2.INTER_LANCZOS4)
    x1, y1, x2, y2 = box
    fw, fh = x2 - x1, y2 - y1
    hx1 = max(0, int(x1 - 1.0 * fw))
    hx2 = min(b.shape[1], int(x2 + 1.0 * fw))
    hy1 = max(0, int(y1 - 1.5 * fh))
    hy2 = min(b.shape[0], int(y2 + 0.15 * fh))
    if hx2 - hx1 < 12 or hy2 - hy1 < 12:
        return 0.0
    ha = cv2.calcHist([cv2.cvtColor(a[hy1:hy2, hx1:hx2], cv2.COLOR_BGR2HSV)], [0, 1], None, [30, 32], [0, 180, 0, 256])
    hb = cv2.calcHist([cv2.cvtColor(b[hy1:hy2, hx1:hx2], cv2.COLOR_BGR2HSV)], [0, 1], None, [30, 32], [0, 180, 0, 256])
    cv2.normalize(ha, ha)
    cv2.normalize(hb, hb)
    corr = float(cv2.compareHist(ha.astype(np.float32), hb.astype(np.float32), cv2.HISTCMP_CORREL))
    return max(0.0, min(1.0, 1.0 - corr))


def trim_edges(name, pct=2.0):
    """把输入图四周裁掉 pct%（用来去掉别的软件加的「AI生成」标签 / 下载按钮这类界面元素）"""
    src = os.path.join(INPUT_DIR, name)
    img = cv2.imdecode(np.fromfile(src, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return name
    h, w = img.shape[:2]
    dx, dy = int(w * pct / 100.0), int(h * pct / 100.0)
    if dx < 1 and dy < 1:
        return name
    out = img[dy:h - dy, dx:w - dx]
    if out.size == 0:
        return name
    new_name = f"trim_{int(time.time() * 1000) % 1000000}_{name}"
    ok, buf = cv2.imencode(os.path.splitext(new_name)[1] or ".png", out)
    if not ok:
        return name
    buf.tofile(os.path.join(INPUT_DIR, new_name))
    return new_name


def upscale_image(rel, size_str):
    """把生成结果用 LANCZOS 放大到目标尺寸（省显存的 2K 出图法：先小图生成再放大）"""
    try:
        tw, th = [int(v) for v in str(size_str).lower().split("x")]
    except (ValueError, AttributeError):
        return None
    src = os.path.join(OUTPUT_DIR, rel)
    img = cv2.imdecode(np.fromfile(src, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None or max(tw, th) <= max(img.shape[:2]):
        return None
    out = cv2.resize(img, (tw, th), interpolation=cv2.INTER_LANCZOS4)
    rel2 = f"QwenEdit/panel_up_{int(time.time() * 1000) % 1000000}.png"
    ok, buf = cv2.imencode(".png", out)
    if not ok:
        return None
    buf.tofile(os.path.join(OUTPUT_DIR, rel2))
    return rel2


def focus_folder_window(title_hint):
    """把标题里含 title_hint 的资源管理器窗口拽到前台（explorer 自己不会抢焦点）"""
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        found = []
        PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _lp):
            if u.IsWindowVisible(hwnd):
                n = u.GetWindowTextLengthW(hwnd)
                if n > 0:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    u.GetWindowTextW(hwnd, buf, n + 1)
                    if title_hint and title_hint in buf.value:
                        found.append(hwnd)
            return True

        u.EnumWindows(PROC(cb), 0)
        for hwnd in found:
            u.ShowWindow(hwnd, 9)          # SW_RESTORE
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)    # 可能被系统拒绝，但窗口已在最前一批
        return len(found)
    except Exception:
        return 0


def open_folder(path):
    """打开文件夹并尽量拽到前台，返回 (方式, 命中窗口数)"""
    p = os.path.normpath(path)
    os.makedirs(p, exist_ok=True)
    how = "startfile"
    try:
        os.startfile(p)
    except Exception:
        how = "explorer"
        popen_gui(["explorer", p], close_fds=True)
    hit = 0
    for _ in range(6):                      # 给资源管理器一点时间把窗口开出来
        time.sleep(0.6)
        hit = focus_folder_window(os.path.basename(p))
        if hit:
            break
    return how, hit


def file_root(which):
    """文件浏览允许的几个根目录（白名单，防止乱删）"""
    cfg = load_config()
    _save = (cfg.get("save_dir") or "").strip()
    # ⚠ 第三方复验指出：只在写入时校验 save_dir 不够 —— 历史配置或手改的 config 里若已有敏感目录，
    #   读接口会照读。这里读入时再过一遍校验，不合法就当作"没配"。
    if _save:
        _ok, _why = check_save_dir(_save)
        if not _ok:
            _save = ""
    roots = {"output": os.path.join(OUTPUT_DIR, "QwenEdit"),
             "video": os.path.join(OUTPUT_DIR, "video"),
             "input": INPUT_DIR,
             "runs": RUNS_DIR,
             "captions": CAPTIONS_DIR,
             "save": _save}
    return roots.get(which, "")


def safe_path(root, rel):
    """把 rel 拼到 root 下，并确保结果**真的在 root 里面**。

    ⚠ 2026-10-03 第三方（Claude Code）发布前审查指出：以前是 `full.startswith(root)`，
    **没有分隔符边界** —— root 为 `D:\\x\\output` 时，`../output_backup/secret.png` 会被放行
    （同前缀的兄弟目录可穿越）。改成 commonpath 后仍然只是**字面路径**判断；
    第七轮复验又指出：根里若有一条指向外面的目录联接（junction/symlink），
    直接用 safe_path 的读接口还能穿出去，而走 _inside 的挡住了 —— 两层标准不一致。
    现在统一：**safe_path 内部就调 _inside（realpath 实现）**，全校验只有一层。
    """
    if not root:
        return ""
    root = os.path.normpath(os.path.abspath(root))
    rel = str(rel or "").replace("/", os.sep).replace("\\", os.sep)
    if os.path.isabs(rel):
        return ""
    full = os.path.normpath(os.path.join(root, rel))
    if not _inside(root, full):
        return ""
    return full


# ---- 上传名净化 / 另存目录校验（第三方审查指出的两处硬伤）--------------------
_UPLOAD_NAME_BAD = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fa5.\-]+")
# 另存目录不允许落在这些地方：系统目录、凭据/配置区（"+任意子目录"式的宽松白名单反而会挡住正常用法）
_SENSITIVE_DIR_PARTS = (".ssh", ".aws", ".azure", ".gnupg", ".config", ".docker", ".kube",
                        "appdata", "windows", "program files", "program files (x86)",
                        "programdata", "system volume information", "$recycle.bin")


def safe_upload_name(name, fallback="img"):
    """multipart 里的字段名会被拼进存放路径 → 必须净化。

    ⚠ 第三方审查实测：`name="..\\..\\..\\evil"` 能让 os.path.join 逃出 INPUT_DIR，
    覆盖任意已存在的图片/音频文件。这里只保留字母/数字/中文/下划线/点/连字符，去掉分隔符与前导点，限长 60。
    """
    n = _UPLOAD_NAME_BAD.sub("", str(name or ""))
    n = n.replace("\\", "").replace("/", "").strip(". ")
    return (n or fallback)[:60]


def check_save_dir(path):
    """「另存目录」只能指向一个**已存在**的普通目录，且不许是系统目录/凭据配置区。

    为什么必须校验：`/api/config` 能改 save_dir，而 `/raw?which=save` 会把这个目录当白名单根
    逐个回文件内容 —— 一旦允许把它指到 `C:\\Users\\<你>\\AppData` 这类地方，
    "另存目录"就等价于"任意文件读"（第三方审查里就是这么串起来的）。
    返回 (ok, 原因)。
    """
    p = str(path or "").strip()
    if not p:
        return True, ""                       # 清空 = 关闭另存，合法
    try:
        ap = os.path.abspath(p)
        # ⚠ 第三方复验指出：Windows 8.3 短名（C:\PROGRA~1、APPLIC~1）能绕开字符串黑名单。
        #   realpath 会把短名展开成长名（不存在的路径原样返回，下面 isdir 会挡掉）。
        real = os.path.realpath(ap)
    except Exception:
        return False, "路径不合法"
    if not os.path.isdir(real):
        return False, "这个目录不存在（先建好目录，或选一个已有的目录）"
    parts = [x for x in real.lower().replace("/", "\\").split("\\") if x]
    for bad in _SENSITIVE_DIR_PARTS:
        if bad in parts:
            return False, f"出于安全，另存目录不能指向系统目录或用户配置区（路径里含「{bad}」）"
    try:
        home = os.path.realpath(os.path.expanduser("~"))
        if real.lower().rstrip("\\") == home.lower().rstrip("\\"):
            return False, "出于安全，另存目录不能就是用户主目录本身（请选它下面的某个文件夹，或别的盘）"
    except Exception:
        pass
    return True, ""


def task_map():
    """文件名 → 是哪个功能生成的（读 runs/ 台账）。
    用来给「最近的作品」分类：服装 / 人物 / 其它。
    光看文件名分不出来——提取服装出图也叫 panel_out_*.png（实测：磁盘上没有一个 garment 命名的输出）。"""
    global _TASKMAP
    with _TASKMAP["lock"]:
        if _TASKMAP["data"] is not None and time.time() - _TASKMAP["at"] < 20:
            return _TASKMAP["data"]
    m = {}
    try:
        if os.path.isdir(RUNS_DIR):
            for d in os.listdir(RUNS_DIR):
                mp = os.path.join(RUNS_DIR, d, "meta.json")
                if not os.path.isfile(mp):
                    continue
                try:
                    with open(mp, encoding="utf-8") as fh:
                        j = json.load(fh)
                except Exception:
                    continue
                rn = j.get("result_name")
                if not rn:
                    continue
                t = (j.get("task") or "").lower()
                pr = j.get("params") or {}
                mode = (pr.get("free_mode") or pr.get("mode") or "").lower()
                m.setdefault(os.path.basename(rn), {"task": t, "mode": mode, "ts": j.get("timestamp") or ""})
    except OSError:
        pass
    with _TASKMAP["lock"]:
        _TASKMAP["data"] = m
        _TASKMAP["at"] = time.time()
    return m


_TASKMAP = {"data": None, "at": 0.0, "lock": threading.Lock()}
GARMENT_TASKS = {"vton", "garment", "batchvton", "batch_garment", "batch_vton"}

# ---------------- 作品的「删除 / 临时移掉」（右键菜单用） ----------------
# 删除：不直接抹掉，先挪进工作区的 _回收站\<日期>\（可撤销、可一键清空）—— 误删救得回来。
# 临时移掉：文件一个字节都不动，只把名字记进 _隐藏清单.json，列表里不再出现（想看了能一键恢复）。
RECYCLE_DIR = os.path.join(BASE_DIR, "_回收站")
RECYCLE_INDEX = os.path.join(RECYCLE_DIR, "index.json")
HIDDEN_PATH = os.path.join(BASE_DIR, "_隐藏清单.json")


def _json_load(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _json_save(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


def hidden_map():
    d = _json_load(HIDDEN_PATH, {})
    return d if isinstance(d, dict) else {}


def hidden_key(which, path):
    return "%s|%s" % (which, os.path.basename(path))


def set_hidden(which, path, on=True):
    d = hidden_map()
    k = hidden_key(which, path)
    if on:
        d[k] = time.strftime("%Y-%m-%d %H:%M:%S")
    else:
        d.pop(k, None)
    _json_save(HIDDEN_PATH, d)
    return k


def recycle_file(full):
    """文件挪进回收站（可撤销）。返回 (记录号, 说明)"""
    import shutil
    sub = os.path.join(RECYCLE_DIR, time.strftime("%Y%m%d"))
    os.makedirs(sub, exist_ok=True)
    rid = "%d_%s" % (int(time.time() * 1000), os.path.basename(full))
    dst = os.path.join(sub, rid)
    shutil.move(full, dst)
    idx = _json_load(RECYCLE_INDEX, {})
    if not isinstance(idx, dict):
        idx = {}
    idx[rid] = {"path": dst, "from": full, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _json_save(RECYCLE_INDEX, idx)
    return rid, "已删除（挪进回收站 %s）" % sub


def unrecycle_file(rid):
    """把回收站里的东西放回原处。返回 (放回后的路径 或 None, 说明)"""
    import shutil
    idx = _json_load(RECYCLE_INDEX, {})
    rec = (idx or {}).get(rid) or {}
    src, back = rec.get("path"), rec.get("from")
    if not src or not os.path.isfile(src):
        return None, "回收站里已经没有这条了"
    if not back:
        return None, "这条记录没记下原路径，只能手动去回收站里拿"
    try:
        os.makedirs(os.path.dirname(back), exist_ok=True)
        if os.path.exists(back):
            root, ext = os.path.splitext(back)
            back = "%s_放回%d%s" % (root, int(time.time()), ext)
        shutil.move(src, back)
    except OSError as e:
        return None, "放回失败：%s" % e
    idx.pop(rid, None)
    _json_save(RECYCLE_INDEX, idx)
    return back, "已放回：" + os.path.basename(back)


def recycle_stat():
    idx = _json_load(RECYCLE_INDEX, {})
    n, b = 0, 0
    for _rid, rec in (idx or {}).items():
        p = (rec or {}).get("path") or ""
        if p and os.path.isfile(p):
            n += 1
            try:
                b += os.path.getsize(p)
            except OSError:
                pass
    return n, b


def recycle_purge():
    """真删：把回收站整个清掉。返回 (删掉的个数, 释放字节, 错误)"""
    import shutil
    n, b = recycle_stat()
    try:
        if os.path.isdir(RECYCLE_DIR):
            shutil.rmtree(RECYCLE_DIR)
    except OSError as e:
        return n, b, str(e)
    return n, b, ""


def thumb_origin_map():
    """缩略图缓存文件名 -> 原图绝对路径。
    缓存名 = md5("原图路径|尺寸|mtime纳秒|字节数")（/thumb 里定的），所以能反查回原图。
    用途：用户从作品栏按住小图往①里拖时，Chrome 拖过去的是那张缩略图本身 —— 这里把它还原成原图。"""
    m = {}
    if not os.path.isdir(THUMB_DIR):
        return m
    try:
        have = set(os.listdir(THUMB_DIR))
    except OSError:
        return m
    cands = []
    try:
        for name in os.listdir(OUTPUT_DIR):
            p = os.path.join(OUTPUT_DIR, name)
            if os.path.isfile(p):
                cands.append(p)
            elif os.path.isdir(p):
                for n2 in os.listdir(p):
                    p2 = os.path.join(p, n2)
                    if os.path.isfile(p2):
                        cands.append(p2)
    except OSError:
        pass
    for p in cands:
        try:
            st = os.stat(p)
        except OSError:
            continue
        for size in (120, 140, 180, 420):
            key = hashlib.md5(("%s|%d|%d|%d" % (p, size, st.st_mtime_ns, st.st_size)).encode()).hexdigest()
            if key + ".jpg" in have:
                m[key] = p
    return m


def upgrade_small_upload(name):
    """这张上传图如果其实是面板自己的缩略图（≤320px 且和缓存内容一致），就换成原图。
    返回 (要用的文件名, 说明)；没换就返回 (原名, "")。"""
    try:
        full = os.path.join(INPUT_DIR, name)
        img = cv2.imdecode(np.fromfile(full, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return name, ""
        h, w = img.shape[:2]
        if max(h, w) > 320:                       # 正常尺寸的图，不碰
            return name, ""
        base = cv2.resize(img, (64, 64), interpolation=cv2.INTER_AREA).astype(np.float32)
        best = None
        for key, orig in thumb_origin_map().items():
            cp = os.path.join(THUMB_DIR, key + ".jpg")
            try:
                ca = cv2.imdecode(np.fromfile(cp, dtype=np.uint8), cv2.IMREAD_COLOR)
            except Exception:
                ca = None
            if ca is None:
                continue
            cmp64 = cv2.resize(ca, (64, 64), interpolation=cv2.INTER_AREA).astype(np.float32)
            mae = float(np.mean(np.abs(cmp64 - base)))
            if best is None or mae < best[0]:
                best = (mae, orig)
        if not best or best[0] > 4.0 or not os.path.isfile(best[1]):
            return name, ""
        import re as _re
        import shutil
        safe_base = _re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(best[1]))
        newname = "up_%d_%s" % (int(time.time() * 1000) % 100000000, safe_base)
        shutil.copy2(best[1], os.path.join(INPUT_DIR, newname))
        return newname, ("这张参考图其实是作品栏的 %dpx 缩略图，已自动换成原图 %s（脸清楚得多，锁脸才准）"
                         % (max(h, w), os.path.basename(best[1])))
    except Exception:
        return name, ""


def list_files(which, limit=48, offset=0, q="", sort="time", hidden=None):
    root = file_root(which)
    out = {"root": root, "files": [], "total": 0, "which": which}
    if not root or not os.path.isdir(root):
        return out
    tmap = task_map()
    _hid = hidden_map()
    out["hidden_total"] = len(_hid)          # 界面上的「已移掉 N」用这个数

    def _keep(p):
        """临时移掉的（只隐藏、文件还在）默认不出现；hidden=1 时反过来只看它们"""
        is_h = hidden_key(which, p) in _hid
        return is_h if hidden else (not is_h)

    items = []
    try:
        entries = os.listdir(root)
    except OSError:
        return out
    for name in entries:
        p = os.path.join(root, name)
        try:
            if os.path.isfile(p):
                if (not q or q.lower() in name.lower()) and _keep(p):
                    items.append(p)
            elif os.path.isdir(p):          # 往里看一层（老照片那些按记录分文件夹）
                for n2 in os.listdir(p):
                    p2 = os.path.join(p, n2)
                    if os.path.isfile(p2) and (not q or q.lower() in n2.lower()) and _keep(p2):
                        items.append(p2)
        except OSError:
            continue
    if sort == "name":
        items.sort(key=lambda x: os.path.basename(x).lower())
    else:
        items.sort(key=lambda x: os.path.getmtime(x), reverse=True)
    out["total"] = len(items)
    for p in items[offset:offset + limit]:
        try:
            st = os.stat(p)
        except OSError:
            continue
        ext = os.path.splitext(p)[1].lower()
        kind = ("image" if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp") else
                "video" if ext in (".mp4", ".webm", ".mov") else
                "audio" if ext in (".wav", ".mp3", ".flac", ".m4a", ".ogg") else
                "text" if ext in (".txt", ".json", ".md", ".csv") else "other")
        _base = os.path.basename(p)
        _tm = tmap.get(_base) or {}
        _task = _tm.get("task") or ""
        _mode = _tm.get("mode") or ""
        if _task in ("vton", "garment", "batchvton") or _mode in ("vton", "garment"):
            _grp = "garment"                       # 换衣 / 提取服装 / 批量换装
        elif _task == "batch" and _mode == "cutout":
            _grp = "other"                         # 批量抠图不是服装，别混进服装那一类
        elif _task in ("faceswap", "pose", "poserender", "hair", "motion",
                       "talk", "avatar", "restore", "flf"):
            _grp = "person"
        else:
            _grp = "other"
        out["files"].append({"name": _base,
                             "rel": os.path.relpath(p, root).replace("\\", "/"),
                             "size": st.st_size,
                             "mtime": time.strftime("%m-%d %H:%M", time.localtime(st.st_mtime)),
                             "task": _task, "mode": _mode, "grp": _grp,
                             "kind": kind})
    return out


# ---------------- 模型管理 ----------------
# 清单不另存一份：直接用 ComfyUI 自己的 /models 索引（它才知道 extra_model_paths 合并后的全貌），
# 这里只负责把名字定位成真实路径、量大小、校验文件头。
# MODEL_ROOTS 已在文件顶部按配置算出（config.json 的 model_roots；默认 <ComfyUI>/models）。
MODEL_TYPE_CN = {
    "checkpoints": "主模型", "diffusion_models": "扩散模型", "unet": "UNet", "loras": "LoRA",
    "vae": "VAE", "text_encoders": "文本/视觉编码器", "clip": "CLIP", "clip_vision": "视觉塔",
    "background_removal": "抠图模型", "upscale_models": "放大模型", "audio_encoders": "音频编码器",
    "frame_interpolation": "补帧", "longcat": "数字人 LongCat", "breezetts2": "配音 Breeze-TTS",
    "detection": "检测", "insightface": "换脸", "ipadapter": "IPAdapter",
    "clip_projections": "ClipProj", "controlnet": "ControlNet", "style_models": "风格模型",
    "model_patches": "模型补丁", "gguf": "GGUF 量化", "diffusers": "Diffusers",
}


def model_find(folder, name):
    """ComfyUI 的 /models 只给文件名，这里在几个模型根里定位真实路径。

    ⚠ 第三方复验（第二轮、第三轮）两次指出这里：
      ① name 里的反斜杠/`..` 能逃出模型根；
      ② **folder 本身就是根的一部分** —— 传 `../secret` 会把"根"挪走，同样能读到任意文件
         （/api/models/hash 会返回任意文件的 sha256+绝对路径，/api/models/reveal 还会去资源管理器打开它）。
    现在：folder 只允许单层、普通名字；name 走 safe_path()。
    """
    name = (name or "").replace("\\", os.sep).replace("/", os.sep)
    folder = (folder or "").replace("\\", "/").strip("/")
    if folder in ("", "."):
        folder = ""
    else:
        # 只保留最后一段，且必须是普通名字（不许 ..、不许分隔符、不许盘符）
        folder = os.path.basename(folder)
        if (not folder) or folder in (".", "..") or (".." in folder) or re.search(r'[:*?"<>|]', folder):
            return ""
    for root in MODEL_ROOTS:
        p = safe_path(os.path.join(root, folder) if folder else root, name)
        if p and os.path.isfile(p):
            return p
    return ""


def model_check(path):
    """safetensors 文件头校验（只读头几 KB，不整个读盘）：头 8 字节＝JSON 头长度。
    对应同类软件那句「文件头无效或权重不完整，请重新下载完整 safetensors 文件」。"""
    if not path or not os.path.isfile(path):
        return False, "文件不存在"
    size = os.path.getsize(path)
    if size < 1024:
        return False, f"只有 {size} 字节，明显没下完"
    if not path.lower().endswith(".safetensors"):
        return True, "非 safetensors，跳过文件头校验"
    try:
        with open(path, "rb") as fh:
            n = int.from_bytes(fh.read(8), "little")
            if not (0 < n < 200 * 1024 * 1024) or 8 + n > size:
                return False, f"文件头长度不合法（{n}）→ 不完整，建议重新下载"
            meta = json.loads(fh.read(n).decode("utf-8", "replace"))
    except Exception as e:
        return False, f"读文件头失败：{type(e).__name__}"
    if not isinstance(meta, dict):
        return False, "文件头不是合法 JSON"
    return True, f"完整 · {len([k for k in meta if k != '__metadata__'])} 个张量"


def model_sha256(path):
    h = hashlib.sha256()
    t0 = time.time()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(8 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest(), round(time.time() - t0, 1)


def model_list(folder, q="", limit=400):
    try:
        names = comfy_get("/models/" + urllib.parse.quote(folder)) or []
    except Exception:
        names = []
    items = []
    for nm in names:
        p = model_find(folder, nm)
        rec = {"name": nm, "size": 0, "mtime": "", "path": p, "ok": None, "msg": "定位不到文件"}
        if p:
            try:
                st = os.stat(p)
                rec["size"] = st.st_size
                rec["mtime"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
            except OSError:
                pass
            ok, msg = model_check(p)
            rec["ok"], rec["msg"] = ok, msg
        if q and q.lower() not in nm.lower():
            continue
        items.append(rec)
    items.sort(key=lambda r: (r["ok"] is False, -r["size"]))     # 坏文件排最前，方便一眼看到
    return {"folder": folder, "total": len(items), "items": items[:limit], "roots": MODEL_ROOTS}



# 同类软件自己设了 frame-ancestors 'none' 挡嵌套，代理时统一剥掉拦截头
# ---- 本机反向代理：把自己(8899)/原生ComfyUI(8188) 的页面嵌进同一个界面 ----
# 同类软件自己设了 frame-ancestors 'none' 挡嵌套，代理时统一剥掉拦截头
PROXY_ALLOW = {"127.0.0.1:8899", "127.0.0.1:8188"}
PROXY_DROP = ("x-frame-options", "content-security-policy", "content-security-policy-report-only",
              "transfer-encoding", "connection", "content-encoding", "content-length")


_MODELS_VER = [0.0, ""]          # [上次统计时间, 指纹]


def models_fingerprint():
    """模型目录指纹：模型文件数 + 最新改动时间。

    给前端用来判断"是不是刚装了模型"。必要性：**ComfyUI 前端只在页面加载时读一次模型清单**，
    装完模型不重载就会一直显示"缺失模型"（2026-10-03 用户真实踩到：文件装好了、服务端也认了，
    界面上还是红框，得手动 Ctrl+F5）。有了这个指纹，前端每次打开内嵌界面时对一下，
    不一致就自己重载，用户什么都不用按。
    结果缓存 10 秒，避免频繁 os.walk。
    """
    now = time.time()
    if _MODELS_VER[1] and now - _MODELS_VER[0] < 10:
        return _MODELS_VER[1]
    exts = (".safetensors", ".sft", ".gguf", ".ckpt", ".pt", ".pth", ".bin")
    roots = list(MODEL_ROOTS) or [os.path.join(COMFY_DIR, "models")]
    n = 0
    mt = 0.0
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dp, _dn, fn in os.walk(root):
            for f in fn:
                if f.lower().endswith(exts):
                    n += 1
                    try:
                        m = os.path.getmtime(os.path.join(dp, f))
                        if m > mt:
                            mt = m
                    except OSError:
                        pass
    ver = "%d-%d" % (n, int(mt))
    _MODELS_VER[0] = now
    _MODELS_VER[1] = ver
    return ver


def _need_node(cfg_key, feature_cn):
    """取第三方节点类名；没配就抛一句人话（而不是让模型那边神秘失败）。"""
    v = str(_cfg_raw(cfg_key, "") or "").strip()
    if not v:
        raise RuntimeError(f"「{feature_cn}」需要第三方 ComfyUI 节点：请在 panel_config.json 里填 {cfg_key}（节点类名）")
    return v


def proxy_fetch(hostport, path, method="GET", body=None, ctype=""):
    """返回 (status, headers列表, body)。只允许白名单里的本机端口。"""
    if hostport not in PROXY_ALLOW:
        return 403, [("Content-Type", "text/plain; charset=utf-8")], b"host not allowed"
    url = f"http://{hostport}{path}"
    req = urllib.request.Request(url, data=body, method=method)
    if ctype:
        req.add_header("Content-Type", ctype)
    req.add_header("Accept-Language", "zh-CN,zh;q=0.9")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
            hs = [(k, v) for k, v in r.headers.items() if k.lower() not in PROXY_DROP]
            return r.status, hs, data
    except urllib.error.HTTPError as e:
        data = e.read()
        hs = [(k, v) for k, v in (e.headers or {}).items() if k.lower() not in PROXY_DROP]
        return e.code, hs, data
    except Exception as e:
        msg = f"proxy error: {e}".encode("utf-8")
        return 502, [("Content-Type", "text/plain; charset=utf-8")], msg


DSH_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>AI 工作台（DSH 单窗）</title>
<style>
 html,body{margin:0;height:100%;background:#0d1014;color:#dfe6f2;
   font:14px/1.5 "Microsoft YaHei",system-ui,sans-serif}
 #bar{display:flex;gap:6px;align-items:center;padding:8px 10px;background:#141922;
   border-bottom:1px solid #232a36;position:sticky;top:0;z-index:9}
 #bar b{font-weight:600;margin-right:8px}
 .tb{padding:5px 12px;border-radius:8px;background:#1d2431;border:1px solid #2c3543;
   color:#cfd8e6;cursor:pointer;font-size:13px}
 .tb.on{background:#2b6cff;border-color:#2b6cff;color:#fff}
 .tb:hover{filter:brightness(1.15)}
 #addr{flex:1;margin-left:8px;font-size:12px;color:#7f8a9c;overflow:hidden;
   text-overflow:ellipsis;white-space:nowrap}
 #wrap{position:relative;height:calc(100% - 49px)}
 iframe{position:absolute;inset:0;width:100%;height:100%;border:0;background:#fff;display:none}
 iframe.on{display:block}
 #hint{padding:40px;color:#8b95a7;font-size:14px;line-height:1.9}
 code{background:#1b2230;padding:1px 6px;border-radius:5px;color:#ffd98a}
</style></head><body>
<div id="bar">
  <b>AI 工作台</b>
  <button class="tb on" data-u="/">🎨 我的面板</button>
  <button class="tb" data-u="/proxy/127.0.0.1:8188/">🧩 原生 ComfyUI</button>
  <span id="addr"></span>
</div>
<div id="wrap">
  <iframe class="on" src="/"></iframe>
  <iframe></iframe>
  <iframe></iframe>
</div>
<script>
 const tabs=[...document.querySelectorAll(".tb")], frm=[...document.querySelectorAll("iframe")];
 const say=t=>document.querySelector("#addr").textContent=t;
 const jget=async p=>{const r=await fetch(p+(p.includes("?")?"&":"?")+"_="+Date.now());return r.json();};
 tabs.forEach((b,i)=>b.onclick=async()=>{
   tabs.forEach(x=>x.classList.remove("on")); frm.forEach(x=>x.classList.remove("on"));
   b.classList.add("on"); frm[i].classList.add("on");
   if(!frm[i].src || frm[i].src==="about:blank") frm[i].src=b.dataset.u;
   say(b.dataset.u);
   
 });
 document.querySelector("#addr").textContent="/";
</script></body></html>
"""










def _procs(match):
    """列出命令行/路径命中 match 的进程 (pid, name, cmdline)。
       只用 PowerShell 精确取，绝不按进程名批量结束。"""
    ps = ("Get-CimInstance Win32_Process | Where-Object { ($_.CommandLine + '|' + $_.ExecutablePath) "
          f"-like '*{match}*' }} | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress")
    try:
        out = run_hidden(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=40).stdout.strip()
        if not out:
            return []
        arr = json.loads(out)
        if isinstance(arr, dict):
            arr = [arr]
        return [(int(r["ProcessId"]), r.get("Name") or "", r.get("CommandLine") or "") for r in arr]
    except Exception:
        return []


def panel_comfy_pids():
    """面板自己那套 ComfyUI 的 PID —— 认 main.py 的完整路径 + 端口 8188，
       这样绝不会误伤同类软件自己的 ComfyUI（，路径在同类软件目录下）。"""
    out = []
    for pid, _name, cl in _procs(COMFY_MAIN):
        if "8188" in cl and "main.py" in cl:
            out.append(pid)
    return out


def panel_comfy_stop():
    """把显卡从面板的 ComfyUI 手里收回来（切同类软件前必做）。有任务在跑就拒绝。"""
    busy = [j for j in JOBS.values() if j.get("status") in ("queued", "running")]
    if busy:
        return False, f"面板还有 {len(busy)} 个任务在跑/排队 —— 等它跑完再切同类软件，否则会打断出图"
    if not port_open(8188):
        return True, "面板的 ComfyUI 本来就没在跑"
    pids = panel_comfy_pids()
    if not pids:
        return True, "8188 在听但没找到面板 ComfyUI 进程（可能是别的东西占着）"
    for pid in pids:
        run_hidden(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    for _ in range(20):
        if not port_open(8188):
            break
        time.sleep(0.5)
    return True, f"已停掉面板的 ComfyUI（PID {', '.join(map(str, pids))}），显卡让给同类软件"


















QWEN_LORA_REMOVE = "Qwen-Image-2.1-Object-Remover-Bbox-Preview-5000.safetensors"
QWEN_LORA_BFS = "bfs_head_v1.1_qwen_2.1.safetensors"
# 动作姿态：VNCCS QI2.1 PoseStudio LoRA（同类软件默认 V1.1，V1 备选；强度 1.0 / 25 步 / CFG 1 / Euler+Simple）
QWEN_LORA_POSE = "VNCCS_QI2_PoseStudioV1.1.safetensors"
POSE_PROMPT = ("按图1的姿态重绘图2里的人：身体朝向、四肢位置、关节角度完全按图1；"
               "保留图2的容貌、发型与服装；白色背景，全身构图，光影自然。")
QWEN_UNET_Q4 = "qwen_image_2.1_Q4_K_M.gguf"
# 下面两段提示词是照抄同类软件自己跑通的记录（image_00052_.json）
BFS_PROMPT = ("head_swap: start with <image1> as the base image, keeping its lighting, environment, and background. "
              "Replace the facial identity in <image1> with the person from <image2>, strictly preserving the eye shape, "
              "eye color, eyebrows, nose, lips and facial proportions from <image2>. Copy the head rotation, eye direction "
              "and expression from <image1>. Keep the original hairstyle, hairline and forehead coverage from <image1>, "
              "do not transfer hair or bangs from <image2>. High quality, sharp details.")
REMOVE_PROMPT = ("移除红框里的物体，用周围的背景自然地补全，看不出修改痕迹；"
                 "红框以外的所有内容（人物、地面、光线、构图）保持完全不变。 "
                 "/ remove the object inside the red box and naturally fill in with the surrounding background; "
                 "keep everything outside the red box exactly unchanged.")


def qwen_graph21x(ref_images, prompt, negative="", resolution=768, seed=None, unet=None, steps=20, cfg=1.0,
                  lora=None, lora_strength=1.0, mask_image=None, gguf=False, prefix="QwenEdit/panel_out",
                  base_image=None):
    """通用版 Qwen-Image-2.1：可选 LoRA、可选遮罩（只重绘遮罩区）、可选 GGUF 加载。"""
    g = {}
    _u, _gguf = tier_unet()
    if unet:
        _u = unet
    g["1"] = unet_loader_node(_u, bool(gguf or _gguf))   # 加载器按文件本身选，不看档位
    mref = ["1", 0]
    if lora:
        g["60"] = {"class_type": "LoraLoaderModelOnly",
                   "inputs": {"model": mref, "lora_name": lora, "strength_model": float(lora_strength)}}
        mref = ["60", 0]
    g["2"] = {"class_type": "CLIPLoader", "inputs": {"clip_name": QWEN_CLIP, "type": "qwen_image", "device": "default"}}
    g["3"] = {"class_type": "VAELoader", "inputs": {"vae_name": QWEN_VAE}}
    enc = {"clip": ["2", 0], "vae": ["3", 0], "prompt": prompt,
           "negative_prompt": negative or " ", "resolution": int(resolution)}
    nid = 10
    for i, name in enumerate(ref_images, start=1):
        g[str(nid)] = {"class_type": "LoadImage", "inputs": {"image": name}}
        enc[f"images.image_{i}"] = [str(nid), 0]
        nid += 1
    g["20"] = {"class_type": "TextEncodeQwenImage21", "inputs": enc}
    if mask_image:
        # ⚠ 重绘的底图以前硬写"第一张参考图"（节点 10）→ 底图和参考图顺序绑死：
        #   想让"只把脸喂给模型"就没法做（底图也跟着变成脸）。现在可以单独用 base_image 指定。
        if base_image:
            g["80"] = {"class_type": "LoadImage", "inputs": {"image": base_image}}
            _base_px = ["80", 0]
        else:
            _base_px = ["10", 0]
        g["70"] = {"class_type": "LoadImageMask", "inputs": {"image": mask_image, "channel": "red"}}
        g["71"] = {"class_type": "VAEEncode", "inputs": {"pixels": _base_px, "vae": ["3", 0]}}
        g["72"] = {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["71", 0], "mask": ["70", 0]}}
        lat = ["72", 0]
    else:
        lat = ["20", 2]
    g["30"] = {"class_type": "KSampler", "inputs": {
        "model": mref, "positive": ["20", 0], "negative": ["20", 1], "latent_image": lat,
        "seed": seed if seed is not None else random.randint(1, 2 ** 31 - 1),
        "steps": int(steps), "cfg": float(cfg), "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}}
    g["40"] = {"class_type": "VAEDecode", "inputs": {"samples": ["30", 0], "vae": ["3", 0]}}
    g["50"] = {"class_type": "SaveImage", "inputs": {"images": ["40", 0], "filename_prefix": prefix}}
    return g


def pose_gen_with_fallback(pose_img, refs, pos, steps, cfg, strength, say,
                           prefix="QwenEdit/panel_pose"):
    """姿态出图。档位自动降级统一由 submit_and_wait 负责（不用在这里再兜一层）。"""
    g = qwen_graph21x([pose_img] + refs, pos, "", resolution=768, steps=steps, cfg=cfg,
                      lora=QWEN_LORA_POSE, lora_strength=strength, prefix=prefix)
    return submit_and_wait(g, say)


def outpaint_seam_fix(rel, src_abs, oL, oT, oR, oB, bw, bh, say=None, band=64):
    """扩图「画中画方框」修正：同类软件那两个节点的合成是硬边 —— 原图那一块贴回去，边界一眼一个方框。
    这里自己接管最后一步：
      · 四边都扩了 → 用泊松融合（cv2.seamlessClone）把原图贴回去，边界按梯度对齐，方框消失；
      · 有一边没扩（贴不下）→ 退化成"宽羽化 + 接缝配色"，至少不留硬边。
    原图深处像素仍然保持原样（融合只作用在边界一条带里）。返回新的相对路径（失败返回原 rel）。"""
    def _log(m):
        if say:
            try:
                say(m)
            except Exception:
                pass
    try:
        p = rel if os.path.isabs(rel) else os.path.join(OUTPUT_DIR, rel)
        canvas = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)
        src = cv2.imdecode(np.fromfile(src_abs, dtype=np.uint8), cv2.IMREAD_COLOR)
        if canvas is None or src is None:
            return rel
        sc = cv2.resize(src, (int(bw), int(bh)), interpolation=cv2.INTER_LANCZOS4)
        x0, y0 = int(oL), int(oT)
        if x0 + sc.shape[1] > canvas.shape[1] or y0 + sc.shape[0] > canvas.shape[0]:
            return rel
        mask = np.zeros(sc.shape[:2], np.uint8)
        cv2.rectangle(mask, (0, 0), (sc.shape[1] - 1, sc.shape[0] - 1), 255, -1)
        if band > 0 and min(sc.shape[0], sc.shape[1]) > band * 3:
            mask = cv2.erode(mask, np.ones((band, band), np.uint8))     # 只在边界一条带里融合
        if min(oL, oT, oR, oB) >= 2:
            out = cv2.seamlessClone(sc, canvas, mask, (x0 + sc.shape[1] // 2, y0 + sc.shape[0] // 2),
                                    cv2.NORMAL_CLONE)
            how = "泊松融合（seamlessClone）"
        else:
            out = canvas.copy()
            a = cv2.GaussianBlur(mask, (0, 0), sigmaX=max(6.0, band / 2.0)).astype(np.float32) / 255.0
            # 接缝配色：把原图那条带的均值差，按距离衰减地补到生成画面上，去掉明暗台阶
            ring = cv2.dilate(mask, np.ones((band * 2, band * 2), np.uint8)) - mask
            roi = out[y0:y0 + sc.shape[0], x0:x0 + sc.shape[1]].astype(np.float32)
            if ring.any():
                d = (sc.astype(np.float32)[ring > 0].mean(axis=0) - roi[ring > 0].mean(axis=0))
                roi = roi + d[None, None, :] * (1.0 - a)[:, :, None]
            blend = sc.astype(np.float32) * a[:, :, None] + roi * (1 - a[:, :, None])
            out[y0:y0 + sc.shape[0], x0:x0 + sc.shape[1]] = np.clip(blend, 0, 255).astype(np.uint8)
            how = "宽羽化＋接缝配色（有一边没扩，贴不下泊松）"
        dst_rel = f"QwenEdit/panel_outpaint_seam_{int(time.time() * 1000) % 100000000}.png"
        dst = os.path.join(OUTPUT_DIR, dst_rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        ok, buf = cv2.imencode(".png", out)
        if not ok:
            return rel
        buf.tofile(dst)
        _log(f"扩图接缝处理：{how}（融合带宽 {band}px）→ {os.path.basename(dst_rel)}")
        return dst_rel
    except Exception as e:
        _log(f"扩图接缝处理失败（{type(e).__name__}），用原始合成图")
        return rel


def outpaint_pattern_fix(rel, oL, oT, oR, oB, bw, bh, prompt, steps, say=None, band=90):
    """扩图二次「接缝重绘」：只重绘外圈时，新扩出来的地面花纹 / 地毯图案 / 墙板线不会自动跟原图对齐，
    边界上就看得出"花纹错位"（用户明确指出过）。这里沿原图边界做一条环形遮罩，局部重绘一次，
    让两边的花纹接上；**环外的像素一个都不动**。返回新的相对路径（失败/没把握就返回原图）。"""
    def _log(m):
        if say:
            try:
                say(m)
            except Exception:
                pass
    try:
        p = rel if os.path.isabs(rel) else os.path.join(OUTPUT_DIR, rel)
        full = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)
        if full is None:
            return rel
        H, W = full.shape[:2]
        x0, y0 = int(oL), int(oT)
        x1, y1 = x0 + int(bw), y0 + int(bh)
        m = np.zeros((H, W), np.uint8)
        # 外框（向外扩 band/2）减内框（向内缩 band/2）＝骑在接缝上的一条环带
        ax0, ay0 = max(2, x0 - band // 2), max(2, y0 - band // 2)
        ax1, ay1 = min(W - 3, x1 + band // 2), min(H - 3, y1 + band // 2)
        bx0, by0 = min(W - 3, x0 + band // 2), min(H - 3, y0 + band // 2)
        bx1, by1 = max(2, x1 - band // 2), max(2, y1 - band // 2)
        cv2.rectangle(m, (ax0, ay0), (ax1, ay1), 255, -1)
        if bx1 > bx0 and by1 > by0:
            cv2.rectangle(m, (bx0, by0), (bx1, by1), 0, -1)
        m = cv2.GaussianBlur(m, (0, 0), sigmaX=max(4.0, band / 5.0))
        if int((m > 8).sum()) < 1000:
            _log("接缝环带太小，跳过二次重绘")
            return rel
        mask_name = f"seam_mask_{int(time.time() * 1000) % 100000000}.png"
        cv2.imencode(".png", m)[1].tofile(os.path.join(INPUT_DIR, mask_name))
        # LoadImage 只认 ComfyUI 的 input 目录 —— 输出目录里的图必须先复制过去（踩过：
        # 直接喂名字会报 "Invalid image file"，400 校验失败）
        src_name = f"seam_src_{int(time.time() * 1000) % 100000000}.png"
        cv2.imencode(".png", full)[1].tofile(os.path.join(INPUT_DIR, src_name))
        seam_prompt = ("把这张图的拼接接缝修好：地面瓷砖/地毯的花纹方向与间距、木墙板的线条、"
                       "家具与地面的交界必须与周围自然连续、对齐，看不出是两块拼起来的。"
                       "不要改变画面的内容、构图与人物。" + ((" 原要求：" + prompt) if prompt else ""))
        seam_neg = ("花纹错位，图案不连续，接缝，方框，画中画，模糊，重影，重复的主体，多余的手脚，"
                    "改变构图，改变人物")
        _log(f"接缝二次重绘（环带宽 {band}px · {steps} 步）…")
        g = qwen_graph21x([src_name], seam_prompt, seam_neg, resolution=1024,
                          steps=steps, cfg=1.0, mask_image=mask_name,
                          prefix="QwenEdit/panel_outpaint_seam2")
        rel2, _s, _p = submit_and_wait(g, say)
        fp = rel2 if os.path.isabs(rel2) else os.path.join(OUTPUT_DIR, rel2)
        new = cv2.imdecode(np.fromfile(fp, dtype=np.uint8), cv2.IMREAD_COLOR)
        if new is None:
            return rel
        if new.shape[:2] != (H, W):
            new = cv2.resize(new, (W, H), interpolation=cv2.INTER_LANCZOS4)
        a = (m.astype(np.float32) / 255.0)[:, :, None]          # 只有环带用重绘结果
        out = (full.astype(np.float32) * (1 - a) + new.astype(np.float32) * a)
        dst_rel = f"QwenEdit/panel_outpaint_fixed_{int(time.time() * 1000) % 100000000}.png"
        dst = os.path.join(OUTPUT_DIR, dst_rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        ok, buf = cv2.imencode(".png", np.clip(out, 0, 255).astype(np.uint8))
        if not ok:
            return rel
        buf.tofile(dst)
        _log(f"接缝二次重绘完成 → {os.path.basename(dst_rel)}")
        return dst_rel
    except Exception as e:
        _log(f"接缝二次重绘失败（{type(e).__name__}: {str(e)[:90]}），保留上一版")
        return rel


def clean_box_lines(gen_rel, orig_name, box_pct):
    """把红框线从成图里擦掉：只在「框线那一条窄带」上用原图像素回贴，框内保留生成结果。"""
    gen_path = gen_rel if os.path.isabs(gen_rel) else os.path.join(OUTPUT_DIR, gen_rel)
    gen = cv2.imdecode(np.fromfile(gen_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    orig = cv2.imdecode(np.fromfile(
        orig_name if os.path.isabs(orig_name) else os.path.join(INPUT_DIR, orig_name),
        dtype=np.uint8), cv2.IMREAD_COLOR)
    if gen is None or orig is None:
        return False
    gen = cv2.resize(gen, (orig.shape[1], orig.shape[0]))
    h, w = orig.shape[:2]
    x0, y0, x1, y1 = [int(round(v / 100.0 * (w if i % 2 == 0 else h))) for i, v in enumerate(box_pct)]
    t = max(3, w // 220)
    pad = t * 2
    outer = np.zeros((h, w), np.uint8)
    cv2.rectangle(outer, (max(0, x0 - pad), max(0, y0 - pad)),
                  (min(w - 1, x1 + pad), min(h - 1, y1 + pad)), 255, -1)
    inner = np.zeros((h, w), np.uint8)
    if x1 - pad > x0 + pad and y1 - pad > y0 + pad:
        cv2.rectangle(inner, (x0 + pad, y0 + pad), (x1 - pad, y1 - pad), 255, -1)
    band = cv2.bitwise_and(outer, cv2.bitwise_not(inner))
    out = np.where(band[..., None] > 0, orig, gen)
    cv2.imencode(".png", out)[1].tofile(gen_path)
    return True

def paste_mask_back(gen_rel, src_name, mask_name, feather=8):
    """选区内用生成结果、选区外用原图像素（遮罩边缘羽化过渡）。
    这样"没涂到的地方一个像素都不会变"，不用赌 VAE 往返。返回新文件相对路径，失败返回 None。"""
    gen_path = gen_rel if os.path.isabs(gen_rel) else os.path.join(OUTPUT_DIR, gen_rel)
    gen = cv2.imdecode(np.fromfile(gen_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    src = cv2.imdecode(np.fromfile(os.path.join(INPUT_DIR, os.path.basename(src_name)),
                                   dtype=np.uint8), cv2.IMREAD_COLOR)
    msk = cv2.imdecode(np.fromfile(os.path.join(INPUT_DIR, os.path.basename(mask_name)),
                                   dtype=np.uint8), cv2.IMREAD_COLOR)
    if gen is None or src is None or msk is None:
        return None
    if msk.shape[:2] != gen.shape[:2]:
        msk = cv2.resize(msk, (gen.shape[1], gen.shape[0]), interpolation=cv2.INTER_NEAREST)
    if src.shape[:2] != gen.shape[:2]:
        src = cv2.resize(src, (gen.shape[1], gen.shape[0]), interpolation=cv2.INTER_LANCZOS4)
    m = msk[:, :, 2].astype(np.float32) / 255.0      # LoadImageMask channel="red" 取的就是红通道
    if feather and feather > 0:
        k = int(feather) * 2 + 1
        m = cv2.GaussianBlur(m, (k, k), 0)
    m3 = m[:, :, None]
    out = (gen.astype(np.float32) * m3 + src.astype(np.float32) * (1.0 - m3)).clip(0, 255).astype(np.uint8)
    dst = os.path.join(OUTPUT_DIR, "QwenEdit", f"panel_inpaint_back_{int(time.time())}.png")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    ok, buf = cv2.imencode(".png", out)
    if not ok:
        return None
    buf.tofile(dst)
    return os.path.relpath(dst, OUTPUT_DIR).replace("\\", "/")


def looks_like_garment_shot(name, thresh=0.45):
    """这张参考图是不是已经是「白底服装商品图」——是的话就不用再抠一遍（省一次 150 秒的生成）"""
    try:
        p = name if os.path.isabs(name) else os.path.join(INPUT_DIR, os.path.basename(name))
        im = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)
        if im is None:
            return False
        g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
        return float((g > 235).mean()) > thresh
    except Exception:
        return False


def make_redbox_and_mask(src_path, box_pct, stem):
    """在图上画红框（LoRA 训练时就是这个样式）+ 生成遮罩图（白=重绘）。
       box_pct = (x0,y0,x1,y1) 百分比 0-100。返回 (红框图名, 遮罩图名)，都在 ComfyUI input 下。"""
    img = cv2.imdecode(np.fromfile(
        src_path if os.path.isabs(src_path) else os.path.join(INPUT_DIR, src_path),
        dtype=np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    x0, y0, x1, y1 = [int(round(v / 100.0 * (w if i % 2 == 0 else h))) for i, v in enumerate(box_pct)]
    x0, x1 = max(0, min(x0, x1)), min(w - 1, max(x0, x1))
    y0, y1 = max(0, min(y0, y1)), min(h - 1, max(y0, y1))
    boxed = img.copy()
    cv2.rectangle(boxed, (x0, y0), (x1, y1), (0, 0, 255), max(3, w // 220))
    # 遮罩：框内白、框外黑，并且稍微外扩一点再羽化，接缝更自然
    pad = max(6, int(min(w, h) * 0.012))
    m = np.zeros((h, w), np.uint8)
    cv2.rectangle(m, (max(0, x0 - pad), max(0, y0 - pad)), (min(w - 1, x1 + pad), min(h - 1, y1 + pad)), 255, -1)
    m = cv2.GaussianBlur(m, (0, 0), max(3, min(w, h) * 0.008))
    b_name = f"{stem}_box.png"
    m_name = f"{stem}_mask.png"
    cv2.imencode(".png", boxed)[1].tofile(os.path.join(INPUT_DIR, b_name))
    cv2.imencode(".png", m)[1].tofile(os.path.join(INPUT_DIR, m_name))
    return b_name, m_name


def make_head_mask(src_path, stem, grow=0.28, feather=0.02):
    """给 BFS 换脸用：只重绘头部区域（脸+额头+头发），羽化边缘。返回 (底图副本名, 遮罩名)。"""
    try:
        import clothing_mask
    except ImportError as e:
        # 模块级那次 import 是带兜底的（_clothing_mask=None），这里若不兜，缺文件时会甩一句
        # "ModuleNotFoundError: No module named 'clothing_mask'"，用户看不懂也不想看。
        raise RuntimeError(f"缺少 clothing_mask.py（BFS 护脸靠它定位头部）→ 报错详情：{e}")
    img = cv2.imdecode(np.fromfile(
        src_path if os.path.isabs(src_path) else os.path.join(INPUT_DIR, src_path),
        dtype=np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    fb = clothing_mask.face_box(img)
    if not fb:
        fb = [w * 0.32, h * 0.12, w * 0.68, h * 0.45]      # 认不出脸就按中间偏上来
    x1, y1, x2, y2 = fb
    fw, fh = x2 - x1, y2 - y1
    # 脸框往上扩（含头发）+ 左右扩
    nx1 = int(max(0, x1 - fw * grow))
    nx2 = int(min(w - 1, x2 + fw * grow))
    ny1 = int(max(0, y1 - fh * (grow + 0.45)))
    ny2 = int(min(h - 1, y2 + fh * 0.28))
    m = np.zeros((h, w), np.uint8)
    cv2.rectangle(m, (nx1, ny1), (nx2, ny2), 255, -1)
    m = cv2.GaussianBlur(m, (0, 0), max(4, min(w, h) * feather))
    b_name = f"{stem}_base.png"
    m_name = f"{stem}_headmask.png"
    cv2.imencode(".png", img)[1].tofile(os.path.join(INPUT_DIR, b_name))
    cv2.imencode(".png", m)[1].tofile(os.path.join(INPUT_DIR, m_name))
    return b_name, m_name, (nx1, ny1, nx2, ny2)


# ffmpeg / ffprobe：优先 config.json 里指的目录，其次 PATH，最后找同目录/上一级的 bin。
# 视频后期（拼接/字幕/水印/配乐）和 GIF/封面的转码都要用，找不到会在日志里明确说缺什么。
def _find_media_bin(name):
    exe = name + (".exe" if os.name == "nt" else "")
    for key in (name, name + "_bin"):
        v = str(_cfg_raw(key, "") or "").strip()
        if v:
            p = v if os.path.isdir(v) else os.path.dirname(v)
            cand = os.path.join(p, exe) if os.path.isdir(v) else v
            if os.path.isfile(cand):
                return cand
    w = shutil.which(name)
    if w:
        return w
    for c in (os.path.join(BASE_DIR, "bin", exe),
              os.path.join(BASE_DIR, "..", "bin", exe),
              os.path.join(BASE_DIR, "ffmpeg", exe)):
        if os.path.isfile(c):
            return os.path.abspath(c)
    return exe          # 交给 PATH 兜底：至少报错信息里能看到它叫什么


FFMPEG_BIN = _find_media_bin("ffmpeg")
FPROBE_BIN = _find_media_bin("ffprobe")


def probe_video(path):
    """拿宽高和时长"""
    try:
        out = run_hidden([FPROBE_BIN, "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=width,height,duration",
                              "-of", "json", path], capture_output=True, text=True, timeout=90).stdout
        s = (json.loads(out).get("streams") or [{}])[0]
        return (int(s.get("width") or 0), int(s.get("height") or 0), float(s.get("duration") or 0))
    except Exception:
        return 0, 0, 0.0


def video_post(opt):
    """视频后期（纯 CPU，不占显卡）：多段拼接 + 字幕 + 配乐 + 变速 + 升帧 + 统一尺寸。

    ⚠ 第六轮复验指出：clips / music 原来是"用户可控路径直接喂 ffmpeg"，只判了 os.path.isfile ——
    传 `C:\\Users\\<你>\\私密.mp4` 就能把任意本机媒体拼进结果、落到出图目录再取回。
    现在逐项走唯一入口（只允许面板自己的 input / 出图目录）。
    """
    _roots = [OUTPUT_DIR, INPUT_DIR, os.path.join(OUTPUT_DIR, "video"), CAPTIONS_DIR]
    clips = []
    for p in (opt.get("clips") or []):
        if not p:
            continue
        fp = resolve_user_path(p, _roots)
        if not fp:
            raise RuntimeError(f"这一段视频不在面板目录里（为安全只允许用面板自己的视频）：{os.path.basename(str(p))}")
        clips.append(fp)
    if not clips:
        raise RuntimeError("先选至少一段视频")
    text = (opt.get("text") or "").strip()
    try:
        speed = max(0.25, min(4.0, float(opt.get("speed") or 1.0)))
    except ValueError:
        speed = 1.0
    try:
        fps = int(opt.get("fps") or 0)
    except ValueError:
        fps = 0
    music = (opt.get("music") or "").strip()
    if music:
        _m = resolve_user_path(music, _roots)
        if not _m:
            raise RuntimeError("配乐文件不在面板目录里（为安全只允许用面板自己的音频）")
        music = _m
    size = (opt.get("size") or "").strip().lower()
    W = H = 0
    if "x" in size:
        try:
            W, H = [int(v) for v in size.split("x")[:2]]
        except ValueError:
            W = H = 0
    if not W or not H:
        W, H, _ = probe_video(clips[0])
        if not W:
            W, H = 1080, 1920
    W += W % 2
    H += H % 2
    tmp = tempfile.mkdtemp(prefix="vpost_")
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w", encoding="utf-8") as fh:
        for c in clips:
            fh.write("file '%s'\n" % c.replace("\\", "/").replace("'", "'\\''"))
    vf = [f"scale={W}:{H}:force_original_aspect_ratio=decrease",
          f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2", "setsar=1"]
    if speed != 1.0:
        vf.append(f"setpts=PTS/{speed}")
    if fps:
        vf.append(f"fps={fps}")
    if text:
        tf = os.path.join(tmp, "cap.txt")
        with open(tf, "w", encoding="utf-8") as fh:
            fh.write(text)
        vf.append("drawtext=fontfile='C\\:/Windows/Fonts/msyhbd.ttc':textfile='%s'"
                  ":fontcolor=white:fontsize=%d:borderw=3:bordercolor=black@0.8"
                  ":x=(w-text_w)/2:y=h-text_h-70"
                  % (tf.replace("\\", "/").replace(":", "\\:"), max(26, W // 26)))
    wm = (opt.get("wm") or "").strip()
    if wm:
        wf = os.path.join(tmp, "wm.txt")
        with open(wf, "w", encoding="utf-8") as fh:
            fh.write(wm)
        vf.append("drawtext=fontfile='C\\:/Windows/Fonts/msyh.ttc':textfile='%s'"
                  ":fontcolor=white@0.55:fontsize=%d:borderw=2:bordercolor=black@0.35"
                  ":x=w-text_w-36:y=36"
                  % (wf.replace("\\", "/").replace(":", "\\:"), max(20, W // 42)))
    try:
        fadeio = max(0.0, min(3.0, float(opt.get("fadeio") or 0)))
    except ValueError:
        fadeio = 0.0
    if fadeio > 0:                       # 片头淡入 / 片尾淡出（拼接后的整体）
        _w0, _h0, _d0 = probe_video(clips[0])
        total = sum(probe_video(c)[2] for c in clips) / max(0.25, speed)
        vf.append(f"fade=t=in:st=0:d={fadeio}")
        if total > fadeio * 2:
            vf.append(f"fade=t=out:st={max(0.0, total - fadeio):.2f}:d={fadeio}")
    outdir = os.path.join(OUTPUT_DIR, "video")
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, f"vpost_{time.strftime('%Y%m%d_%H%M%S')}.mp4")
    try:
        fade = max(0.0, min(2.0, float(opt.get("fade") or 0)))
    except ValueError:
        fade = 0.0
    if fade > 0 and len(clips) > 1:
        # 段间黑场过渡（本机 ffmpeg 无 xfade，用 fade 出/入 + concat 滤镜实现）
        parts = []
        for i in range(len(clips)):
            base = [f"scale={W}:{H}:force_original_aspect_ratio=decrease",
                    f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2", "setsar=1"]
            if speed != 1.0:
                base.append(f"setpts=PTS/{speed}")
            if fps:
                base.append(f"fps={fps}")
            d = probe_video(clips[i])[2] / max(0.25, speed)
            h = max(0.1, min(fade / 2.0, max(0.1, (d - 0.2) / 2.0)))
            base.append(f"fade=t=in:st=0:d={h:.2f}")
            if d > h * 2:
                base.append(f"fade=t=out:st={max(0.0, d - h):.2f}:d={h:.2f}")
            parts.append(f"[{i}:v]{','.join(base)}[v{i}]")
        parts.append("".join(f"[v{i}]" for i in range(len(clips))) +
                     f"concat=n={len(clips)}:v=1:a=0[vcat]")
        tail = [x for x in vf if x.startswith(("drawtext", "fade="))]
        parts.append(f"[vcat]{','.join(tail)}[vout]" if tail else "[vcat]null[vout]")
        cmd = [FFMPEG_BIN, "-y"] + sum([["-i", c] for c in clips], [])
        if music:
            cmd += ["-i", music]
        cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-crf", "20", "-preset", "medium"]
        if music:
            cmd += ["-map", f"{len(clips)}:a", "-shortest", "-c:a", "aac", "-b:a", "160k",
                    "-af", "atempo=%s" % max(0.5, min(2.0, speed))]
        else:
            cmd += ["-an"]
        cmd += ["-movflags", "+faststart", out]
        r = run_hidden(cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            raise RuntimeError("转场合成失败：" + (r.stderr or "")[-400:])
        _w, _h, dur = probe_video(out)
        return (os.path.relpath(out, OUTPUT_DIR).replace("\\", "/"),
                {"段数": len(clips), "尺寸": f"{_w}x{_h}", "时长": round(dur, 1),
                 "变速": speed, "段间过渡": f"黑场 {fade}s"})
    cmd = [FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0", "-i", lst]
    if music:
        cmd += ["-i", music]
    cmd += ["-vf", ",".join(vf), "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-crf", "20", "-preset", "medium"]
    if music:
        cmd += ["-map", "0:v", "-map", "1:a", "-shortest", "-c:a", "aac", "-b:a", "160k",
                "-af", "atempo=%s" % max(0.5, min(2.0, speed))]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", out]
    r = run_hidden(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg 失败：" + (r.stderr or "")[-400:])
    _w, _h, dur = probe_video(out)
    return (os.path.relpath(out, OUTPUT_DIR).replace("\\", "/"),
            {"段数": len(clips), "尺寸": f"{_w}x{_h}", "时长": round(dur, 1),
             "变速": speed, "帧率": fps or "原样"})


def h3_frames(n):
    """H3 只吃 17n+5 帧（24fps）。不合法的自动取最近合法值，别让任务白跑。"""
    try:
        n = int(float(n))
    except (TypeError, ValueError):
        n = 124
    n = max(22, min(345, n))
    k = int(round((n - 5) / 17.0))
    k = max(1, min(20, k))
    return 17 * k + 5


def _inside(root, path):
    """path 是否真的在 root 里面。

    ⚠ 第五轮复验指出：只做 normpath+commonpath 的**字符串**比较挡不住 Windows 的
    目录联接/符号链接（reparse point）——根里若有一条指向外面的 junction，穿过它就能出去。
    现在统一 realpath 之后再比（与 check_save_dir 的做法一致），联接/短名/大小写一次收口。
    """
    try:
        r = os.path.normpath(os.path.realpath(os.path.abspath(root)))
        p = os.path.normpath(os.path.realpath(os.path.abspath(path)))
        if os.name == "nt":
            r, p = r.lower(), p.lower()
        return os.path.commonpath([r, p]) == r
    except Exception:
        return False


def extract_last_frame(video_rel, out_name=None):
    """抽某段视频的最后一帧 → 放进 input 目录，作为下一段的首帧（连续接镜/尾帧续写用）。

    ⚠ 第三方第三轮复验指出：这个函数接受**绝对路径**，于是 chain_from 传
    `C:\\...\\私密.mp4` 就能让 ffmpeg 读任意视频、把末帧写进 input 再取回。
    这里（按调用方都会经过的公共函数）统一约束：只允许 OUTPUT_DIR 里的文件。
    """
    if os.path.isabs(video_rel):
        cand = os.path.normpath(video_rel)
        if not _inside(OUTPUT_DIR, cand):
            raise RuntimeError("只允许用出图目录里的视频来接镜")
        src = cand
    else:
        src = safe_path(OUTPUT_DIR, video_rel)
    if not src or not os.path.isfile(src):
        raise RuntimeError("找不到那段视频：" + str(video_rel))
    name = out_name or f"chain_{int(time.time())}.png"
    out = os.path.join(INPUT_DIR, name)
    r = run_hidden([FFMPEG_BIN, "-y", "-sseof", "-0.25", "-i", src,
                        "-frames:v", "1", "-q:v", "2", out],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not os.path.exists(out):
        r2 = run_hidden([FFMPEG_BIN, "-y", "-i", src, "-update", "1",
                             "-frames:v", "1", "-q:v", "2", out],
                            capture_output=True, text=True, timeout=600)
        if r2.returncode != 0 or not os.path.exists(out):
            raise RuntimeError("抽尾帧失败：" + (r.stderr or "")[-200:])
    return name


def resolve_chain_first_frame(params, default_image):
    """「接着上一段继续」：给了 chain_from（某段视频）就抽它最后一帧当首帧。
       返回 (首帧图名, 是否为续写抽出的帧)。没给就原样返回上传的那张。"""
    src = (params.get("chain_from") or "").strip()
    if not src:
        return default_image, None
    name = extract_last_frame(src)
    return name, name


# ── 实时采样预览 ───────────────────────────────────────────────────────
# ComfyUI 采样时会把「潜在图 → 小图预览」按 WebSocket 推给**提交那个任务的 client_id**
# （见 execution.py：extra_data["client_id"] 决定 server.client_id，预览只发给它）。
# 所以面板用一个固定 client_id 提交，并常驻一个 WS 线程把最新一帧+步数存内存，
# 界面在轮询任务时顺带取，就能看到"正在画出什么"。
PREVIEW_CLIENT = "panel-live-preview"
PREVIEW_LOCK = threading.Lock()
PREVIEW = {"png": b"", "kind": "png", "step": 0, "max": 0, "node": None,
           "pid": "", "t": 0.0, "running": False}


def prompt_body(graph, preview=True):
    """提交给 ComfyUI 的请求体：带上固定 client_id，预览帧才会推给我们。"""
    body = {"prompt": graph}
    if preview:
        body["client_id"] = PREVIEW_CLIENT
        body["extra_data"] = {"preview_method": "auto"}
    return body


def preview_ws_thread():
    """常驻监听 ComfyUI 的 WS：收二进制预览帧与 progress 事件。断了就重连。"""
    try:
        import websocket          # websocket-client
    except ImportError:
        print("[预览] 没装 websocket-client，实时采样预览关闭", flush=True)
        return
    url = COMFY.replace("http://", "ws://").replace("https://", "wss://") + "/ws?clientId=" + PREVIEW_CLIENT
    while True:
        try:
            ws = websocket.create_connection(url, timeout=10)
            ws.settimeout(None)          # 连上后一直等：空闲时不该被判超时反复重连
            print("[预览] 已接上 ComfyUI 的实时预览通道", flush=True)
            while True:
                msg = ws.recv()
                if isinstance(msg, (bytes, bytearray)):
                    if len(msg) < 8:
                        continue
                    etype = int.from_bytes(msg[:4], "big")
                    if etype not in (1, 2):          # 1=PREVIEW_IMAGE 2=UNENCODED_PREVIEW_IMAGE
                        continue
                    itype = int.from_bytes(msg[4:8], "big")
                    data = bytes(msg[8:])
                    if not data:
                        continue
                    with PREVIEW_LOCK:
                        PREVIEW.update(png=data, kind=("jpg" if itype == 1 else "png"), t=time.time())
                else:
                    try:
                        m = json.loads(msg)
                    except ValueError:
                        continue
                    t, d = m.get("type"), (m.get("data") or {})
                    with PREVIEW_LOCK:
                        if t == "progress":
                            mx = int(d.get("max") or 0)
                            # 采样器步数不会超过 200；ComfyUI 也会给别的节点发 progress，
                            # 不过滤的话界面上会闪出 265/265 这种怪数字。
                            if 0 < mx <= 200:
                                PREVIEW.update(step=int(d.get("value") or 0), max=mx,
                                               node=d.get("node"), running=True)
                        elif t == "executing":
                            if d.get("node") is None:
                                PREVIEW.update(running=False)
                            else:
                                fresh = d.get("prompt_id") != PREVIEW.get("pid")
                                PREVIEW.update(running=True, pid=d.get("prompt_id") or "",
                                               node=d.get("node"))
                                if fresh:        # 换任务了，上一张预览作废
                                    PREVIEW.update(png=b"", step=0, max=0)
        except Exception as e:
            with PREVIEW_LOCK:
                PREVIEW.update(running=False)
            print(f"[预览] 通道断开，3 秒后重连（{type(e).__name__}）", flush=True)
            time.sleep(3)


def comfy_error_text(entry):
    """把 ComfyUI 的 execution_error 翻译成人话（原来是甩一串转义 JSON，用户根本看不懂）"""
    try:
        msgs = (entry.get("status") or {}).get("messages") or []
        for kind, d in msgs:
            if kind == "execution_error" and isinstance(d, dict):
                m = (d.get("exception_message") or "").strip()
                node = d.get("node_type") or d.get("node_id") or ""
                if m:
                    low = m.lower()
                    if ("out of memory" in low) or ("cuda oom" in low) or ("insufficient memory" in low):
                        return (f"显存/内存不够：{m[:110]} —— 把模型档位降到 Q4 或 INT8，"
                                f"或先关掉别的占显卡的程序再试")
                    return f"{m}（出错节点：{node}）" if node else m
        if msgs:
            return json.dumps(msgs[-1:], ensure_ascii=False)[:300]
    except Exception:
        pass
    return "ComfyUI 报错（详情见 comfyui_panel.log）"


def submit_and_wait(graph, say, tag=""):
    """提交并等结果；基座模型这一档跑不起来（加载失败 / 显存不够 / FP8 对不上）时，
    **自动换下一档重试**，链尾一定落到最兼容的 Q4 GGUF。用户不用关心用的是哪个模型。"""
    chain = tier_chain()
    last = None
    for i, t in enumerate(chain):
        if i:
            if not retarget_base_model(graph, t):
                raise last
            say(f"自动换成「{(QWEN_TIERS.get(t) or {}).get('label', t)}」再来一次 …")
        try:
            out = _submit_once(graph, say)
            if TIER.get("qwen") != t:          # 记住这一档跑得动，同一批后续任务直接用它
                TIER["qwen"] = t
            if _TIER_LOG.get("v") != t:
                _TIER_LOG["v"] = t
                un, _g = tier_unet(t)
                say(f"基座模型：{(QWEN_TIERS.get(t) or {}).get('label', t)}（{un}）")
            return out
        except JobCancelled:
            raise
        except Exception as e:
            last = e
            if i >= len(chain) - 1 or not _model_error(e):
                raise
            say(f"这一档没跑起来：{str(e)[:140]}")
    raise last


_TIER_LOG = {"v": None}



# ---------------- 队列卡死 / 显卡被占的自我防护 ----------------
# 2026-10-02 用户报"只有第一张能成，后面全搞不出来"：ComfyUI 队列里卡着一个没跑完的旧任务，
# 中断都不理，后面每个任务都排在它后面 → 界面上永远 0/40。另外同类软件的后端（）会一起抢显卡。
def comfy_queue_ids():
    """ComfyUI 队列里现存的 prompt_id（正在跑 + 排队）"""
    try:
        q = comfy_get("/queue")
    except Exception:
        return []
    return ([it[1] for it in (q.get("queue_running") or [])]
            + [it[1] for it in (q.get("queue_pending") or [])])


def comfy_vram_free():
    try:
        d = comfy_get("/system_stats")
        return float(((d.get("devices") or [{}])[0]).get("vram_free") or 0)
    except Exception:
        return 0.0


def other_comfy_ports():
    """除我们自己以外，还有没有别的 ComfyUI/后端在监听（同类软件那个是 ）——它会把 10G 显存吃光。"""
    import socket as _sk
    hit = []
    for _p in (8189, 8187, 8186, 8000):
        if _p == COMFY_PORT:
            continue
        try:
            _c = _sk.create_connection(("127.0.0.1", _p), 0.25)
            _c.close()
            hit.append(_p)
        except OSError:
            pass
    return hit


def comfy_clear_queue(say=None, keep=None):
    """把 ComfyUI 里卡住的旧任务中断并从队列删除。返回被清掉的 id 列表。"""
    ids = [i for i in comfy_queue_ids() if i != keep]
    if not ids:
        return []
    try:
        comfy_post("/interrupt", {})
    except Exception:
        pass
    time.sleep(1.5)
    try:
        comfy_post("/queue", {"delete": ids})
    except Exception:
        pass
    time.sleep(1.5)
    left = [i for i in comfy_queue_ids() if i != keep]
    if say:
        if left:
            say(f"⚠ 清了 {len(ids)} 个卡住的旧任务，还剩 {len(left)} 个删不掉（旧任务卡死了）")
        else:
            say(f"已清掉卡住的旧任务 {len(ids)} 个（上次没跑完的，会把新任务全堵住）")
    return ids


def comfy_restart_ours(say=None):
    """旧任务卡死到清不掉 → 重启**我们自己**的 ComfyUI。按端口精确找 PID，绝不按名字杀。"""
    pid = None
    try:
        out = run_hidden(["netstat", "-ano", "-p", "TCP"], capture_output=True)
        for ln in (out.stdout or b"").decode("utf-8", "replace").splitlines():
            if (":%d " % COMFY_PORT) in ln and "LISTENING" in ln.upper():
                pid = int(ln.split()[-1])
                break
        if pid and pid > 0:
            run_hidden(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            if say:
                say(f"卡死的旧任务删不掉 → 重启了我们自己的 ComfyUI（PID {pid}）")
    except Exception as e:
        if say:
            say(f"重启 ComfyUI 没成功：{type(e).__name__}: {str(e)[:60]}")
    time.sleep(3)
    return ensure_comfy()


def _slow_note(vf, others):
    """卡住时的提示文案：显存**真的**被占光才说"被别的程序占着"，
    否则老实说"这一步本来就慢"—— 2026-10-03 实测数字人（LongCat 480p）加载几分钟是很正常的，
    当时显存空闲 8.4GB，面板却报"显卡八成被别的程序占着"，会让人白折腾。"""
    if vf and vf < 2.5 * 1073741824:
        return ("⚠ 已经 120 秒没有任何进度：显卡被别的程序占着"
                + ("（127.0.0.1:%s 上那个 ComfyUI，通常是同类软件）" % "/".join(map(str, others)) if others else "")
                + f"，当前显存空闲 {vf / 1073741824:.1f}GB。关掉它再重试；或点「停止本批」后重新提交")
    return ("⏳ 已经 120 秒没有新进度：这一步本来就慢（大模型加载/初始化，视频类最常见），"
            f"当前显存空闲 {(vf or 0) / 1073741824:.1f}GB —— 再等等；实在不动就点「停止本批」")


def _comfy_died(say=None, err=None):
    """ComfyUI 中途断开时的统一善后：说人话 + 顺手把它拉回来。

    2026-10-03 实测：跑「照片动起来」（MiniMaxH3 staged 19995MB，10G 卡 + 同类软件占着）时
    ComfyUI 被系统结束，用户看到的是 `URLError: <urlopen error [WinError 10061] 由于目标计算机
    积极拒绝，无法连接。>` —— 完全看不懂。这里把它翻译成人话，并自动拉起 ComfyUI，
    免得用户对着一个死掉的服务反复点。
    """
    if say:
        say(f"⚠ ComfyUI 中途断开了（{type(err).__name__ if err else '连接失败'}）："
            f"多半是显存不够被系统结束了（跑视频这类吃显存的任务时最常见）。正在把它拉回来…")
    try:
        ensure_comfy()
        if say:
            say("已重新拉起 ComfyUI。本次任务算失败，直接重试即可；要跑视频建议先把同类软件关掉腾显卡")
    except Exception:
        if say:
            say("自动拉起 ComfyUI 没成功，请在「设置」里手动启动，或重启面板")
    return ("ComfyUI 中途断开了（多半是显存不够，或被别的程序抢崩）→ 本次任务失败，请直接重试；"
            "跑视频这类吃显存的活，建议先把同类软件关掉")


def _submit_once(graph, say, tag=""):
    # ① 提交前先把卡住的旧任务清掉：否则新任务永远排在它后面，界面就永远停在 0/40
    comfy_clear_queue(say)
    # ② 显卡被别家占着（同类软件那个后端最常见）→ 先说明白，再清一次显存；别让用户以为面板坏了
    vf = comfy_vram_free()
    if vf:
        others = other_comfy_ports()
        if vf < 2.5 * 1073741824:
            extra = ("　另外 127.0.0.1:%s 上还有另一个 ComfyUI 在跑（多半是同类软件开着）" % "/".join(map(str, others))) if others else ""
            say(f"⚠ 显存只剩 {vf / 1073741824:.1f}GB{extra} —— 出图会明显变慢甚至卡住，"
                f"建议先把同类软件（或别的 AI 程序）关掉；我先清一次显存")
            try:
                comfy_post("/free", {"unload_models": True, "free_memory": True})
                time.sleep(2)
            except Exception:
                pass
    try:
        pid = comfy_post("/prompt", prompt_body(graph)).get("prompt_id")
    except Exception as e:
        raise RuntimeError(_comfy_died(say, e))
    if not pid:
        raise RuntimeError("提交失败")
    t0 = time.time()
    warned_slow = False
    while True:
        time.sleep(5)
        cj = CURRENT_JID.get("id")
        if cj and (JOBS.get(cj) or {}).get("cancel"):
            # 取消时要把它从 ComfyUI 队列里也删掉，否则会留个卡死的任务把后面全堵住（踩过）
            comfy_clear_queue(say=None, keep=None)
            raise JobCancelled("用户停止了本批")
        # ③ 看门狗：两分钟一点动静都没有 → 说明显卡被占死/任务卡住，说清楚原因
        if not warned_slow and (time.time() - t0) > 120:
            warned_slow = True
            say(_slow_note(comfy_vram_free(), other_comfy_ports()))
        try:
            h = comfy_get(f"/history/{pid}")
        except Exception as e:
            raise RuntimeError(_comfy_died(say, e))
        if pid in h:
            e = h[pid]
            st = e.get("status", {}).get("status_str")
            out = None
            for nid, o in (e.get("outputs") or {}).items():
                for kind in ("images", "videos", "gifs", "audio"):
                    for it in (o.get(kind) or []):
                        out = os.path.join(it.get("subfolder") or "", it.get("filename"))
            if st == "error" or not out:
                raise RuntimeError("生成失败：" + comfy_error_text(e))
            return out.replace("\\", "/"), round(time.time() - t0, 1), pid


CAPTIONS_DIR = os.path.join(BASE_DIR, "图片描述")


def save_caption_files(text, src_image, ask=""):
    """把读出来的描述存到工作区：单条 txt + 汇总 txt（方便随时拷走用）"""
    try:
        os.makedirs(CAPTIONS_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base = os.path.splitext(os.path.basename(src_image or "image"))[0][:40]
        one = os.path.join(CAPTIONS_DIR, f"{stamp}_{base}.txt")
        with open(one, "w", encoding="utf-8") as fh:
            fh.write(text.strip() + "\n")
        allf = os.path.join(CAPTIONS_DIR, "描述汇总.txt")
        with open(allf, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} | 原图：{src_image}"
                     + (f" | 要求：{ask}" if ask else "") + " =====\n")
            fh.write(text.strip() + "\n")
        return one, allf
    except OSError:
        return None, None


PROMPTTOOL_NAMES = {"rewrite": "扩写/改写提示词", "caption": "图片反推提示词",
                    "clothing_inventory": "衣服清单"}


def polish_graph(text):
    """整理口述文字：纯文本走 TextGenerate（不带图）。
    用节点自带模板（use_default_template=True）—— 反推就是这样出干净结果的；
    用 False 会按"对话记录"整段吐回来（含提示词与原文回显），很脏。"""
    ask = ("请把下面这段用户口述的要求整理成规范的一句话：去掉口语词和重复、"
           "纠正错别字、补上标点、理顺语序；保持原意，不要扩写、不要解释。\n\n" + text)
    return {
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": QWEN_CLIP, "type": "qwen_image", "device": "default"}},
        "12": {"class_type": "TextGenerate", "inputs": {
            "clip": ["2", 0], "prompt": ask, "max_length": 768,
            "sampling_mode": "off", "thinking": False, "use_default_template": True, "mtp": "auto"}},
        "20": {"class_type": "SaveText", "inputs": {"text": ["12", 0],
                                                    "filename_prefix": "QwenEdit/polish", "format": "txt"}},
    }


def polish_text(text):
    """返回 (整理后的文字, 用时秒)。输出脏的时候自动从回显里把结果抠出来。"""
    ensure_comfy()      # 整理也要用 ComfyUI 里的本地模型：没起来就先拉起来（否则直接 10061 连接被拒）
    got, _f, secs = submit_caption(polish_graph(text), lambda *_a, **_k: None, timeout_s=420)
    out = (got or "").strip()
    # 兜底：万一把提示词/原文回显了，取「整理后：」之后、回显之前那一段
    if "整理后" in out or "原文：" in out:
        cand = re.split(r"整理后[:：]", out)
        best = ""
        for seg in cand[1:]:
            seg = seg.split("请把下面这段")[0].split("原文：")[0].strip()
            if len(seg) > len(best):
                best = seg
        if best:
            out = best
    out = re.sub(r"^(整理后[的文字]*[:：]\s*)", "", out)
    out = re.sub(r"^(输出|结果)[:：]\s*", "", out)
    out = out.strip().strip('"').strip("“”").strip("「」").strip("'")
    return out.strip(), secs


def caption_graph(image_name, prompt, max_length=512):
    """读图出描述：用本机 Qwen3-VL（文本编码器带视觉塔）+ TextGenerate 生成文字，再用 SaveText 存成 txt。"""
    return {
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": QWEN_CLIP, "type": "qwen_image", "device": "default"}},
        "10": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "12": {"class_type": "TextGenerate", "inputs": {
            "clip": ["2", 0], "image": ["10", 0], "prompt": prompt, "max_length": int(max_length),
            "sampling_mode": "off", "thinking": True, "use_default_template": True, "mtp": "auto"}},
        "20": {"class_type": "SaveText", "inputs": {"text": ["12", 0], "filename_prefix": "QwenEdit/desc",
                                                    "format": "txt"}},
    }


def submit_caption(graph, say, timeout_s=900):
    """提交并等结果，返回 (文字, txt 相对路径, 用时秒)"""
    pid = comfy_post("/prompt", prompt_body(graph)).get("prompt_id")
    if not pid:
        raise RuntimeError("提交失败")
    t0 = time.time()
    while True:
        time.sleep(3)
        if time.time() - t0 > timeout_s:
            raise RuntimeError("等太久了")
        h = comfy_get(f"/history/{pid}")
        if pid not in h:
            continue
        e = h[pid]
        st = e.get("status", {}).get("status_str")
        text, txt = "", ""
        for nid, o in (e.get("outputs") or {}).items():
            for t in (o.get("text") or []):
                if t:
                    text = t
            for f in (o.get("files") or []):
                fn = f.get("filename") or ""
                if fn.endswith(".txt"):
                    txt = os.path.join(f.get("subfolder") or "", fn).replace("\\", "/")
        if st == "error" or not text:
            msg = json.dumps((e.get("status") or {}).get("messages", [])[-1:], ensure_ascii=False)[:400]
            raise RuntimeError(f"读图失败：{msg}")
        return text, txt, round(time.time() - t0, 1)


def resolve_user_path(p, roots, must_exist=True):
    """**用户可控路径的唯一入口**（第三方四轮复验的建议：别再每个接口各写一遍 isabs 分支）。

    规则：
      · 绝对路径：必须已经落在 roots 里的某一个根**里面**（`_inside` 分隔符边界判定），否则返回 ""；
      · 相对路径：对每个根做 `safe_path()`，取第一个存在的（`must_exist=False` 时取第一个非空）。
    这样"只修了绝对路径那一支、相对路径还能 ../ 出去"这类漏改不会再出现。
    """
    raw = str(p or "").strip()
    if not raw:
        return ""
    norm = raw.replace("/", os.sep).replace("\\", os.sep)
    if os.path.isabs(norm):
        cand = os.path.normpath(norm)
        if not any(_inside(r, cand) for r in roots if r):
            return ""
        if must_exist and not os.path.exists(cand):
            return ""
        return cand
    for r in roots:
        if not r:
            continue
        fp = safe_path(r, norm)
        # ⚠ 第五轮复验：safe_path 只做字符串边界，挡不住目录联接（reparse point）。
        #   再过一遍 _inside（它用 realpath），联接/短名在这里被挡住。
        if fp and _inside(r, fp) and (os.path.exists(fp) if must_exist else True):
            return fp
    return ""


def safe_pose_name(name):
    """姿势库里的名字净化：只允许纯文件名（不含路径分隔符 / 盘符 / ..），且必须以 .json 结尾。

    ⚠ 第五轮复验指出：姿势库这一族（/api/pose/save、/api/pose/get、run 的 pose_name/pose_list）
    原来直接把用户给的名字拼进 POSE_LIB —— 传 `..\\..\\Users\\x\\evil.json` 就能写/读任意 .json。
    ⚠ 第六轮复验补充：Windows 保留设备名（CON/PRN/AUX/NUL/COM1-9/LPT1-9）要挡掉，
    否则 `con` 会命中设备而不是文件。
    """
    n = str(name or "").strip().replace("\\", "/")
    if not n or "/" in n or ":" in n or n.startswith("."):
        return ""
    if ".." in n:
        return ""
    if not n.lower().endswith(".json"):
        n += ".json"
    stem = n[:-5].strip().lower()
    if stem in ("con", "prn", "aux", "nul") or re.fullmatch(r"(com|lpt)[1-9]", stem):
        return ""
    if re.search(r'[<>:"|?*\x00-\x1f]', n):
        return ""
    return n


def resolve_pose_path(name, must_exist=True):
    """把姿势名解析成 POSE_LIB 里的真实路径（唯一入口，绝对路径也一律拒绝）。

    must_exist=False 用于「保存」——新建一个姿势时文件当然还不存在。
    """
    n = safe_pose_name(name)
    if not n:
        return ""
    return resolve_user_path(n, [POSE_LIB], must_exist=must_exist)


def resolve_chain_input(params, files, key="person", src_key="chain_from"):
    """「接着改」：没有新上传图时，把「已完成」里选中的那张（chain_from，相对输出目录）
       复制到 ComfyUI 的 input 目录，直接当本次输入。
       注意：面板的「出图」根就是 QwenEdit 子目录，所以 rel 可能只有文件名 —— 这里按多个根依次找。"""
    if files.get(key):
        return files[key]
    src = (params.get(src_key) or "").strip().replace("\\", "/")
    if not src:
        return None
    base = os.path.basename(src)
    # 所有候选根都列出来，然后**统一**走 resolve_user_path（绝对/相对一视同仁地围在根里）
    roots = [OUTPUT_DIR, os.path.join(OUTPUT_DIR, "QwenEdit"), os.path.join(OUTPUT_DIR, "video"), INPUT_DIR]
    try:
        for sub in os.listdir(OUTPUT_DIR):           # FisherPose / references / … 各种子目录
            d = os.path.join(OUTPUT_DIR, sub)
            if os.path.isdir(d):
                roots.append(d)
    except OSError:
        pass
    full = None
    for r in roots:
        for cand_rel in (src, base):
            fp = resolve_user_path(cand_rel if not os.path.isabs(src) else src, [r])
            if fp and os.path.isfile(fp):
                full = fp
                break
        if full:
            break
    if not full:
        return None
    if not full:
        return None
    import shutil
    name = f"chain{int(time.time())}_{base}"
    try:
        shutil.copyfile(full, os.path.join(INPUT_DIR, name))
    except OSError:
        return None
    # 摘掉 chain_from：否则下游视频分支会把它当成"接上一段视频"去抽尾帧（图片没有尾帧）
    params.pop(src_key, None)
    params["_chain_image"] = name
    return name


def cam_prompt(horizontal_angle, vertical_angle, zoom):
    """多角度摄影术语（阈值与 MIT 项目 ComfyUI-qwenmultiangle 的 nodes.py 完全一致）。"""
    h = int(horizontal_angle) % 360
    v = max(-30, min(60, int(vertical_angle)))
    z = max(0.0, min(10.0, float(zoom)))
    if h < 22.5 or h >= 337.5:
        hd = "front view"
    elif h < 67.5:
        hd = "front-right quarter view"
    elif h < 112.5:
        hd = "right side view"
    elif h < 157.5:
        hd = "back-right quarter view"
    elif h < 202.5:
        hd = "back view"
    elif h < 247.5:
        hd = "back-left quarter view"
    elif h < 292.5:
        hd = "left side view"
    else:
        hd = "front-left quarter view"
    if v < -15:
        vd = "low-angle shot"
    elif v < 15:
        vd = "eye-level shot"
    elif v < 45:
        vd = "elevated shot"
    else:
        vd = "high-angle shot"
    dd = "wide shot" if z < 2 else ("medium shot" if z < 6 else "close-up")
    return f"<sks> {hd} {vd} {dd}"


# 多角度 8 个常用视角（水平角度，同类软件页面上的那一排）
MULTIANGLE_VIEWS = [("正面", 0), ("右前", 45), ("右侧", 90), ("右后", 135),
                    ("背面", 180), ("左后", 225), ("左侧", 270), ("左前", 315)]


POSE_LIB = os.path.join(BASE_DIR, "姿势库")
THUMB_DIR = os.path.join(BASE_DIR, "_缩略图")     # 缩略图缓存（"最近的作品"那排靠它，别再去拉几兆的原图）
# 动作姿态的免费编辑器要用 ComfyUI-Fisher-Pose 自带的那份工作流模板；
# 默认按数据目录拼，装到别处就在 config.json 里写 pose_workflow。
POSE_BASE_WF = str(_cfg_raw("pose_workflow", "") or
                   os.path.join(USER_DIR, "default", "workflows", "FisherPose_姿势自由编辑.json"))
POSE_FACE_KEYS = ("fcl_", "Face", "Eye", "Jaw", "Tongue")

# 三维摆姿编辑器：用 Fisher-Pose 自带的那套（web/editor/freepose.html）。
# 它内部写死了 `event.origin !== location.origin` 就丢弃消息，所以必须由面板同源托管，
# 不能直接 iframe 到 ComfyUI 的 8188（那样 origin 不同，父子完全说不上话）。
FISHER_WEB = os.path.join(COMFY_DIR, "custom_nodes", "ComfyUI-Fisher-Pose", "web")
POSE_EXTRA = os.path.join(BASE_DIR, "pose_editor")
POSE_EDITOR_ENTRY = "editor/freepose.html"
MIME_OVERRIDE = {".mjs": "text/javascript; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".html": "text/html; charset=utf-8",
                 ".json": "application/json; charset=utf-8",
                 ".gz": "application/gzip", ".bin": "application/octet-stream"}


def pose_base_json():
    """基准姿势（取自本机 FisherPose 工作流里 FisherQwenFreePose 节点的 pose_json 控件）。"""
    with open(POSE_BASE_WF, encoding="utf-8") as fh:
        w = json.load(fh)
    node = [x for x in w["nodes"] if x.get("type") == "FisherQwenFreePose"]
    if not node:
        return {}
    return json.loads(node[0]["widgets_values"][4])


def pose_bone_names():
    b = pose_base_json()
    bones = ((b.get("pose") or {}).get("bones") or {})
    return [k for k in sorted(bones) if not k.startswith(POSE_FACE_KEYS)]


def pose_render_graph(pose_json, person, width=1024, height=1024, ref_res=1024,
                      extra="", prefix="QwenEdit/panel_mannequin"):
    """人偶姿态图（几秒，不用采样）：FisherQwenFreePose(pose_json) → SaveImage(第 5 路＝人偶图)。"""
    return {
        "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": QWEN_CLIP, "type": "qwen_image", "device": "default"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": QWEN_VAE}},
        "5": {"class_type": "LoadImage", "inputs": {"image": person}},
        "6": {"class_type": "FisherQwenFreePose", "inputs": {
            "clip": ["3", 0], "vae": ["4", 0], "reference_image": ["5", 0],
            "width": int(width), "height": int(height), "reference_resolution": int(ref_res),
            "extra_prompt": extra or "", "pose_json": pose_json}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["6", 4], "filename_prefix": prefix}},
    }


POWER = {"arm": False, "dry": True, "fired": False, "t": 0.0, "note": "", "cmd": ""}
# 真关机命令。用绝对路径，免得面板被以最小 PATH 拉起时找不到 shutdown.exe。
# 留一个替身入口（PANEL_POWER_CMD）是为了能"真跑验证触发时机"而不真关机：
# 设成别的命令（例如写个标记文件）时，走的仍是同一条代码路径，只是最后一步换成替身。
SHUTDOWN_EXE = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "shutdown.exe")
POWER_CMD = [SHUTDOWN_EXE, "/s", "/t", "60",
             "/c", "AI 工作台：全部任务完成，60 秒后关机（要取消请点面板上的取消）"]
_ENV_POWER = (os.environ.get("PANEL_POWER_CMD") or "").strip()
if _ENV_POWER:
    POWER_CMD = shlex.split(_ENV_POWER)


def power_watcher():
    """关机预约：只有「队列里既没有排队也没有在跑」时才动作。
       dry=True 只记日志（演练），dry=False 才真的执行 POWER_CMD。"""
    while True:
        time.sleep(8)
        try:
            if not POWER["arm"] or POWER["fired"]:
                continue
            busy = [j for j in JOBS.values() if j.get("status") in ("queued", "running")]
            if busy or time.time() - POWER["t"] < 5:
                continue
            if POWER["dry"]:
                msg = ("干跑：队列已空，本应执行「" + " ".join(POWER_CMD[:4]) + " …」（没有真执行）")
            else:
                r = run_hidden(POWER_CMD, capture_output=True)
                if r.returncode == 0:
                    msg = "已发关机指令：60 秒后关机（取消：面板「取消」或 shutdown /a）"
                else:
                    err = (r.stderr or b"").decode("utf-8", "replace").strip()[:160]
                    msg = f"关机指令返回码 {r.returncode}（{err or '无错误输出'}）"
            POWER.update(arm=False, fired=True, note=msg, cmd=" ".join(POWER_CMD))
            print("[关机] " + msg, flush=True)
        except Exception as e:
            POWER.update(note=f"关机监视线程出错：{type(e).__name__}: {e}")


def run_job(job_id, task, files, params):
    J = JOBS[job_id]
    TIER["qwen"] = (params.get("tier") or "auto").strip().lower()   # 默认全自动：先 INT8，跑不动自动降级
    TIER["video"] = (params.get("tier") or "auto").strip().lower()  # 视频（H3）也吃这个档位：q4 就用 10.6GB 的 GGUF
    _H3_PICK.pop("note", None)
    slots = []
    # 同类软件那套：最多 10 个 LoRA 插槽，每槽权重 -5~5；空槽跳过。
    for i in range(1, 11):
        nm = (params.get(f"lora{i}") or "").strip()
        try:
            w = float(params.get(f"lora{i}_w") or 1.0)
        except ValueError:
            w = 1.0
        w = max(-5.0, min(5.0, w))
        if nm and nm not in ("", "none"):
            slots.append((nm, w))
    if len(slots) > 10:
        slots = slots[:10]
    LORA_SLOTS["v"] = slots
    if slots:
        print(f"[LoRA] 本次串了 {len(slots)} 个：" + "、".join(f"{n}@{w}" for n, w in slots), flush=True)
    run_id = None
    try:
        run_id = record_run_start(task, files, params)
        J["run_id"] = run_id
    except Exception:
        run_id = None

    def say(msg):
        J["log"].append(f"{time.strftime('%H:%M:%S')} {msg}")
        print(f"[{job_id[:6]}] {msg}", flush=True)

    try:
        say("检查 ComfyUI 服务 ...")
        if not ensure_comfy():
            J.update(status="error", error="ComfyUI 起不来")
            return

        if task in ("vton", "hair"):
            free_models("qwen")
            person = files.get("person")
            garment = files.get("garment")
            hair_txt = (params.get("hair_text") or "").strip()
            hair_img = files.get("hair_ref")
            if not person:
                J.update(status="error", error="请先选人物图")
                return
            if task == "vton" and not garment:
                J.update(status="error", error="换衣需要再选一张服装图")
                return
            extras, extra_line = [], []
            prep = (params.get("vton_prep") or "extract").strip()
            mode_pre = (params.get("vton_mode") or "raw").strip()
            unet_sel = (params.get("unet") or "").strip() or None
            turbo = (params.get("turbo") or "") in ("1", "true", "on")
            if turbo:
                say("⚡ 加速模式：Viggle Turbo 6 步（关 CFG，负向提示词不生效）")
            if unet_sel:
                say(f"本次使用模型：{unet_sel}")
            if mode_pre == "model_pose":
                # 姿势跟衣服走：直接用换脸，把目标人的脸贴到服装图的人身上
                say("模式：姿势跟衣服走 —— 把人物图的脸换到服装图的人身上（衣服/姿势/背景保持原样）")
                try:
                    rel, msg = faceswap_onto(garment, person)
                    say(msg)
                    J.update(status="done", result=f"/file?p={urllib.parse.quote(rel)}",
                             result_name=os.path.basename(rel), seconds=0, note="换脸模式")
                    return
                except Exception as e:
                    say(f"换脸失败({type(e).__name__}: {e})，改用 Qwen 重绘方案")
            if garment and prep == "extract":
                say("先把服装图里的衣服抠出来（去掉那个模特和背景，只留白底服装图）...")
                try:
                    new_name, esec, erel = extract_garment(garment, f"garment_only_{int(time.time())}.png",
                                                           turbo=turbo)
                    if new_name:
                        garment = new_name
                        say(f"服装已提取完成 → {new_name}（{esec} 秒）")
                    else:
                        say("服装提取失败，改用原服装图")
                except Exception as e:
                    say(f"服装提取出错({type(e).__name__})，改用原服装图")
            imgs = [person]
            if garment:
                extras.append("和发型" if (hair_txt or hair_img) else "")
                imgs.append(garment)
            hair_line = ""
            if hair_img:
                imgs.append(hair_img)
                hair_line = (f"图{len(imgs)}：发型参考图——**只取这张图的头发**"
                             f"（长度、厚薄、刘海、卷直、发色、分缝），这张图的衣服/姿势/背景一概不要参考。")
            elif hair_txt:
                hair_line = f"发型要求：把人物发型改成「{hair_txt}」，发际线与脸型衔接自然，其余容貌与服装不变。"
            if hair_img:
                extra_line.append(hair_line)
            elif hair_txt and task == "vton":
                extra_line.append(hair_line)
            note = (params.get("note") or "").strip()
            if mode_pre == "model_pose":
                pos = POSE_MATCH_POS
                neg = POSE_MATCH_NEG
                if note:
                    pos += f"\n补充说明（用户要求，优先级最高）：{note}"
                say(f"提交 Qwen-Image 2.1 换脸到服装图模特身上（{len(imgs)} 张参考图）...")
                J["prompt_pos"], J["prompt_neg"] = pos, neg
                builder = lambda: qwen_graph(imgs, pos, neg, unet=unet_sel, turbo=turbo)
            else:
                if task == "hair":
                    # 换发型走专用模板：只改头发，衣服/姿势/背景一律不许动
                    pos = HAIR_TMPL.format(ref_line=hair_line or "发型要求：把人物发型改成自然好看的发型。")
                    neg = HAIR_NEG
                else:
                    tmpl_extra = "".join(extras) if extras else ""
                    line = "\n".join(extra_line) if extra_line else "只换服装，发型不变。"
                    pos = VTON_TMPL.format(extra=tmpl_extra, extra_line=line)
                    neg = VTON_NEG
                if note:
                    pos += f"\n补充说明（用户要求，优先级最高）：{note}"
                say(f"提交 Qwen-Image 2.1 编辑（{len(imgs)} 张参考图）...")
                J["prompt_pos"], J["prompt_neg"] = pos, neg
                builder = lambda: qwen_graph(imgs, pos, neg, unet=unet_sel, turbo=turbo)

        elif task == "restore":
            free_models("qwen")
            src = files.get("person")
            if not src:
                J.update(status="error", error="请先选老照片")
                return
            say("提交 Qwen-Image 2.1 修复上色 ...")
            J["prompt_pos"], J["prompt_neg"] = RESTORE_POS, RESTORE_NEG
            _turbo = (params.get("turbo") or "") in ("1", "true", "on")
            builder = lambda: qwen_graph([src], RESTORE_POS, RESTORE_NEG, turbo=_turbo)

        elif task == "motion":
            free_models("h3")
            src = files.get("person") or resolve_chain_input(params, files)
            if not src:
                J.update(status="error", error="请先选照片")
                return
            preset = (params.get("preset") or "wind").strip()
            if preset in MOTION_PRESETS:
                action, detail = MOTION_PRESETS[preset]
            else:
                action = ((params.get("action") or params.get("note")) or "").strip() or "微风吹动树叶与衣角，人物轻轻呼吸、眨眼、微笑。"
                detail = (params.get("action_detail") or "").strip() or (
                    "1) 树叶在动：一阵一阵的风吹过，画面中的树叶与枝条轻轻摆动，地上的树影跟着晃动。\n"
                    "2) 人在动：所有人保持原姿势，像活人一样自然呼吸，胸口与肩膀轻微起伏，陆续自然眨眼，有人嘴角轻轻上扬微笑，衣角与头发被风吹得轻轻晃动。\n"
                    "3) 全片只允许以上这些轻微动作。")
            extra = ((params.get("action") or params.get("note")) or "").strip()
            if preset in MOTION_PRESETS and extra:
                action = extra
            w = int(params.get("width") or 1024)
            h = int(params.get("height") or 768)
            ln = h3_frames(params.get("length") or 124)
            src, chained_img = resolve_chain_first_frame(params, src)
            if chained_img:
                say("接着上一段的尾帧继续（已抽帧：" + chained_img + "）")
            mode = (params.get("mode") or "locked").strip()
            # 走/跳/摇头/转身这类"人真的动"的动作，在"锁背景"里做不出来：
            # 锁背景只会把原图的一小块整体挪动（看上去就是"半张照片在晃"），所以自动改用整幅重绘。
            if mode == "locked" and preset in PERSON_ACTIONS:
                say("「锁背景」只能把原图一小块挪来挪去，做不出真实的走动/摇头/转身 —— 已自动改用整幅重绘。")
                mode = "whole"
            face_ref = files.get("face_ref")
            if mode == "locked":
                say("分析画面里该动的东西（人脸 / 主体 / 树叶）...")
                # 有人物动作的模板必须动到人身上；"风吹微动"才带上树叶。
                # （老逻辑只认人脸，插画/背影/商品检测不到人脸时就会去动一片草地——人一点没动）
                focus = "person" if preset in PERSON_ACTIONS else "scene"
                spec = locked_motion(os.path.join(INPUT_DIR, src), action, "video/panel_locked",
                                     length=ln, focus=focus)
                say(f"识别到: {spec['note']}；只裁出 {spec['tw']}x{spec['th']} 交给 H3 生成动作")
                prompt = MOTION_TMPL.format(action=action, action_detail=detail)
                J["prompt_pos"] = prompt
                if face_ref:
                    say("带上锁脸参考图（H3 参考图版），人物长相更稳 ...")
                    graph = h3_ref_graph([spec["crop_name"], face_ref], prompt, spec["tw"], spec["th"], ln,
                                         save_prefix="video/panel_ref_locked")
                else:
                    graph = h3_motion_graph(spec["crop_name"], prompt, spec["tw"], spec["th"], ln)
                rel, secs, pid = submit_and_wait(graph, say)
                J["prompt_id"] = pid
                say("用光流把 H3 的位移套到原照片上贴回（贴回区域仍是原照片的像素，不会变成另一张图）...")
                out_dir = os.path.join(OUTPUT_DIR, "video")
                os.makedirs(out_dir, exist_ok=True)
                out_abs = os.path.join(out_dir, f"locked_motion_{int(time.time())}.mp4")
                frames = compose_locked(spec, os.path.join(OUTPUT_DIR, rel), out_abs)
                rel_out = os.path.relpath(out_abs, OUTPUT_DIR).replace("\\", "/")
                J.update(status="done", result=f"/file?p={urllib.parse.quote(rel_out)}",
                         result_name=os.path.basename(rel_out), seconds=secs,
                         note=f"{frames} 帧 · 背景像素未改动")
                say(f"完成 -> {os.path.basename(rel_out)}（{frames} 帧，照片本身没动）")
                return
            say(f"提交 H3（{'int8 融合' if h3_int8_ok() else 'Q4 GGUF'}）整幅重绘 {w}x{h} x{ln}帧，动作模板={preset} ...")
            if _H3_PICK.get("note"):
                say(_H3_PICK["note"])
            p_all = MOTION_TMPL.format(action=action, action_detail=detail)
            J["prompt_pos"] = p_all
            if face_ref:
                say("带上锁脸参考图（H3 参考图版），人物长相更稳、脸不漂 ...")
                builder = lambda: h3_ref_graph([src, face_ref], p_all, w, h, ln, save_prefix="video/panel_ref_motion")
            else:
                builder = lambda: h3_motion_graph(src, p_all, w, h, ln)

        elif task == "flf":
            free_models("h3")
            first = files.get("person") or resolve_chain_input(params, files)
            last = files.get("last")
            if not first:
                J.update(status="error", error="请先选首帧图")
                return
            # 注意：首尾帧页界面不发 preset，这里默认必须是"空"（走下面首帧→末帧的默认描述），
            # 不能默认成 dance —— 那会让整段提示词变成"站着原地跳"，和首尾帧约束打架。
            preset = (params.get("preset") or "").strip()
            if preset in MOTION_PRESETS:
                action, detail = MOTION_PRESETS[preset]
            else:
                action = ((params.get("action") or params.get("note")) or "").strip() or "人物从起始姿势自然连贯地运动到结束姿势。"
                detail = (params.get("action_detail") or "").strip() or (
                    "画面从首帧姿势开始，身体连贯地运动到末帧姿势：四肢路径连贯、速度自然、不跳帧、不突变；\n"
                    "中间过程是真实的人体运动，重心自然转移；镜头固定不动，背景与光线保持不变；\n"
                    "不出现多余肢体或手指，人体结构正常不变形。")
            extra = ((params.get("action") or params.get("note")) or "").strip()
            if preset in MOTION_PRESETS and extra:
                action = extra
            if not last:
                # 只给首帧：模型自由往下演
                detail += "\n（本次只提供首帧，没有末帧约束，动作自然延续即可。）"
            w = int(params.get("width") or 768)
            h = int(params.get("height") or 1344)
            ln = h3_frames(params.get("length") or 124)
            say(f"提交 H3（{'int8 融合' if h3_int8_ok() else 'Q4 GGUF'}）"
                f"{'首尾帧' if last else '仅首帧'}驱动 {w}x{h} x{ln}帧，模板={preset} ...")
            if _H3_PICK.get("note"):
                say(_H3_PICK["note"])
            p = MOTION_TMPL.format(action=action, action_detail=detail)
            builder = lambda: h3_motion_graph(first, p, w, h, ln, last_image=last)

        elif task == "talkaudio":
            free_models("h3")
            person = files.get("person")
            aud = files.get("audio") or (params.get("audio_chain") or "").strip()
            if not person:
                J.update(status="error", error="请先选照片")
                return
            if not aud:
                J.update(status="error", error="请先选一段音频（wav/mp3），照片会照着这段音频开口")
                return
            _aud_p = resolve_user_path(aud, [INPUT_DIR])
            if not _aud_p:
                J.update(status="error", error="那段音频不在面板的 input 目录里（为安全只允许用面板自己的音频）")
                return
            dur = audio_seconds(_aud_p)
            # length 传 auto/0/空 ＝ 按音频时长自动定帧数（口型同步必须这样，否则会被截断）
            want = (params.get("length") or "").strip()
            if want in ("", "0", "auto"):
                ln = fit_length(dur) if dur > 0 else h3_frames(124)
            else:
                ln = h3_frames(want)
            w = int(params.get("width") or 768)
            h = int(params.get("height") or 1024)
            if dur > 0:
                say(f"音频 {dur:.1f} 秒 → 生成 {ln} 帧（{ln / 24:.1f} 秒 @24fps）")
            else:
                say(f"读不出音频时长（非 wav？），按 {ln} 帧生成")
            extra = ((params.get("action") or params.get("note")) or "").strip()
            prompt = TALK_AUDIO_TMPL + (f"\n补充说明（用户要求，优先级最高）：{extra}" if extra else "")
            say("提交 H3 参考图版（音频驱动）... 口型会跟着音频动，但不保证每个字都对得上。")
            J["prompt_pos"] = prompt
            builder = lambda: h3_ref_graph([person], prompt, w, h, ln, audio=aud,
                                           save_prefix="video/panel_talkaudio")

        elif task == "remove":
            free_models("qwen")
            target = files.get("person")
            if not target:
                J.update(status="error", error="请先选一张要处理的图")
                return
            box = (params.get("box") or "").strip()
            if not box:
                J.update(status="error", error="请在图上拖一个框，圈住要移除的东西")
                return
            try:
                box_pct = [float(v) for v in box.split(",")]
                assert len(box_pct) == 4
            except Exception:
                J.update(status="error", error="框的坐标不对")
                return
            steps = int(params.get("steps") or 40)
            ask = (params.get("ask") or "").strip()
            stem = f"rm{int(time.time())}"
            say(f"在图上画红框（{box}）并生成遮罩 ...")
            b_name, m_name = make_redbox_and_mask(target, box_pct, stem)
            prompt = REMOVE_PROMPT + (f"\n要移除的东西是：{ask}。" if ask else "")
            say(f"Qwen-Image 2.1 + 移除对象 LoRA · {steps} 步 · cfg=1.0 ...")
            g = qwen_graph21x([b_name], prompt, "", resolution=768, steps=steps, cfg=1.0,
                              lora=QWEN_LORA_REMOVE, mask_image=m_name)

            t0 = time.time()
            rel, _secs, _pid = submit_and_wait(g, say)
            secs = time.time() - t0
            say(f"完成 -> {os.path.basename(rel)}（{secs:.0f} 秒）")
            try:
                if clean_box_lines(rel, target, box_pct):
                    say("已把红框线擦掉（框内保留生成结果）")
            except Exception as _e:
                say(f"红框清理跳过：{type(_e).__name__}: {_e}")
            J.update(status="done", result=f"/file?p={urllib.parse.quote(rel)}",
                     result_name=os.path.basename(rel), seconds=round(secs),
                     note="移除对象完成")
            return

        elif task == "inpaint":
            # 局部重绘（涂抹）：前端把笔迹导成"白＝要重绘"的遮罩 PNG，
            # 走 SetLatentNoiseMask 只重绘涂过的地方，最后把选区外的原图像素贴回去（零改动）。
            free_models("qwen")
            target = files.get("person") or resolve_chain_input(params, files, key="prefx")
            mask = files.get("rm_mask")
            ask = (params.get("ask") or "").strip()
            if not target:
                J.update(status="error", error="请先选一张要改的图")
                return
            if not mask:
                J.update(status="error", error="先在图上涂抹要改的区域（最多 500 笔）")
                return
            if not ask:
                J.update(status="error", error="请描述选区内要改成什么（例：改成一面砖墙）")
                return
            # 选区太小 → 先拦下来（同类软件原话："请放大选区或提高局部重绘精度"）
            try:
                _mp = os.path.join(INPUT_DIR, os.path.basename(mask))
                _mk = cv2.imdecode(np.fromfile(_mp, dtype=np.uint8), cv2.IMREAD_COLOR)
                if _mk is not None:
                    _frac = float((_mk[:, :, 2] > 127).mean())
                    if _frac < 0.002:
                        J.update(status="error",
                                 error=f"涂抹的选区太小了（只占画面 {_frac * 100:.2f}%）："
                                       f"请把要改的地方涂大一点，或用更小的笔刷提高局部重绘精度")
                        return
                    say(f"选区占画面 {_frac * 100:.1f}%")
            except Exception:
                pass
            try:
                steps = int(params.get("steps") or 40)
            except ValueError:
                steps = 25
            say(f"局部重绘：遮罩 {mask} · {steps} 步 · 只重绘涂过的区域 ...")
            g = qwen_graph21x([target], ask, "", resolution=768, steps=steps, cfg=1.0,
                              mask_image=mask)
            t0 = time.time()
            rel, _secs, _pid = submit_and_wait(g, say)
            secs = time.time() - t0
            say(f"采样完成 -> {os.path.basename(rel)}（{secs:.0f} 秒）")
            try:
                back = paste_mask_back(rel, target, mask)
                if back:
                    rel = back
                    say("已把选区外的原图像素贴回（没涂的地方零改动）")
                else:
                    say("贴回跳过（图读不出来），保留生成整图")
            except Exception as _e:
                say(f"贴回跳过：{type(_e).__name__}: {_e}")
            J.update(status="done", result=f"/file?p={urllib.parse.quote(rel)}",
                     result_name=os.path.basename(rel), seconds=round(secs),
                     note=f"局部重绘完成（{secs:.0f} 秒 · 选区外零改动）")
            return

        elif task == "outpaint":
            free_models("qwen")
            target = resolve_chain_input(params, files)
            if not target:
                J.update(status="error", error="请先选一张要扩的图（或从下面「已完成」里点一张「接着改」）")
                return
            def _opx(k, d):
                try:
                    return max(0, min(2048, int(float(params.get(k) or d))))
                except Exception:
                    return d

            def _round16(v):
                """对齐到 16 的倍数（Qwen：VAE 压 8 倍 × patch 2×2 = 16）"""
                return max(16, int(round(float(v) / 16.0)) * 16)
            oL, oT = _opx("op_left", 256), _opx("op_top", 256)
            oR, oB = _opx("op_right", 256), _opx("op_bottom", 256)
            if oL + oT + oR + oB <= 0:
                J.update(status="error", error="四边扩展像素不能全是 0（至少要有一边 > 0）")
                return
            # base = 缩放后的基准图边长（同类软件原话：像素值以缩放后的参考图为基准）。
            # 实测：base=512 时采样出来是灰板、base=1024 正常 —— 不能传原图尺寸。
            base = _opx("base", 1024) or 1024
            feather = _opx("feather", 48)
            steps = int(params.get("steps") or 40)
            prompt = (params.get("prompt") or "").strip() or (
                "把画面向外自然扩展：延续原图的场景、光线、材质与色调；"
                "人物、服装、物体的边缘要自然延续，不要沿边界被水平截断，接缝过渡自然，"
                "不要出现画框、重复的主体或明显的接缝线条。")
            tier = (params.get("tier") or "q4").strip().lower()
            src_path = target if os.path.isabs(target) else os.path.join(INPUT_DIR, target)
            src = cv2.imdecode(np.fromfile(src_path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if src is None:
                J.update(status="error", error=f"读不到这张图：{target}")
                return
            sh, sw = src.shape[:2]
            # 基准图按长边缩放到 base，另一边等比。
            # 关键：画布宽高必须是 **16 的倍数** —— Qwen 的 VAE 压 8 倍、patch 又是 2×2，
            # 合起来 16；画布不是 16 的倍数时模型侧会被取整（1128 → 1136），
            # 扩图合成节点就报"扩图结果尺寸与画布不一致"直接拒绝合成（踩过）。
            # 做法：只把「基准边」微调，用户填的扩展像素一个都不改。
            def _fit_base(ext_a, ext_b, b):
                total = _round16(b + ext_a + ext_b)
                return max(16, total - ext_a - ext_b)
            if sw >= sh:
                bw = _fit_base(oL, oR, base)
                bh = _fit_base(oT, oB, max(64, int(round(base * sh / sw / 8.0) * 8)))
            else:
                bh = _fit_base(oT, oB, base)
                bw = _fit_base(oL, oR, max(64, int(round(base * sw / sh / 8.0) * 8)))
            nw, nh = bw + oL + oR, bh + oT + oB
            say(f"扩图：原图 {sw}×{sh} → 基准 {bw}×{bh} → 画布 {nw}×{nh}"
                f"（左{oL} 上{oT} 右{oR} 下{oB}，羽化 {feather}）")
            _u, _gguf = tier_unet()
            # 参考图给文本编码器：**用原图本身，不要镜像补边**。
            # 走过的两条弯路：
            #   ① 灰底画布当参考 → Qwen 照抄灰色（同 seed 实测 std 0.8，全灰）
            #   ② 镜像补边当参考 → Qwen 照抄镜像（扩出来左右各多一个主体、上下倒影）
            # 现在给干净的原图：往哪扩、扩什么由提示词和遮罩决定，模型没有可照抄的东西。
            sc = cv2.resize(src, (bw, bh), interpolation=cv2.INTER_LANCZOS4)
            ref_img = sc
            ref_name = f"opref{int(time.time())}.png"
            cv2.imencode(".png", ref_img)[1].tofile(os.path.join(INPUT_DIR, ref_name))
            say("参考图＝原图本身（不镜像、不补灰），扩展内容交给提示词与遮罩")

            def _build(seed):
                gg = {}
                # 基座加载器＝按模型文件本身自动选（.gguf / 带 fp8 的 checkpoint / INT8 官方权重各自对应）。
                # 以前这里把 INT8 权重也塞给 FP8 加载器 → 必报
                # "Unsupported FP8 layer/scale: transformer_blocks.0.attn.to_k"，
                # 而界面默认档位正是 INT8 —— 也就是扩图必失败（用户 2026-10-02 撞上）。
                gg["1"] = unet_loader_node(_u, _gguf)
                gg["2"] = {"class_type": "CLIPLoader", "inputs": {"clip_name": QWEN_CLIP, "type": "qwen_image", "device": "default"}}
                gg["3"] = {"class_type": "VAELoader", "inputs": {"vae_name": QWEN_VAE}}
                gg["10"] = {"class_type": "LoadImage", "inputs": {"image": target}}
                gg["12"] = {"class_type": "LoadImage", "inputs": {"image": ref_name}}
                gg["11"] = {"class_type": (NODE_OUTPAINT_CANVAS or _need_node("node_outpaint_canvas", "扩图")), "inputs": {
                    "image": ["10", 0], "base_width": int(bw), "base_height": int(bh),
                    "left": oL, "top": oT, "right": oR, "bottom": oB, "feather": feather}}
                gg["20"] = {"class_type": "TextEncodeQwenImage21", "inputs": {
                    "clip": ["2", 0], "vae": ["3", 0], "prompt": prompt,
                    "negative_prompt": "边框，画中画，重复的主体，模糊，错位，水平接缝，截断线，断层，硬边",
                    "resolution": 1024, "images.image_1": ["12", 0]}}
                gg["21"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["11", 0], "vae": ["3", 0]}}
                gg["22"] = {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["21", 0], "mask": ["11", 1]}}
                gg["30"] = {"class_type": "KSampler", "inputs": {
                    "model": ["1", 0], "positive": ["20", 0], "negative": ["20", 1], "latent_image": ["22", 0],
                    "seed": seed, "steps": steps, "cfg": 1.0,
                    "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}}
                gg["40"] = {"class_type": "VAEDecode", "inputs": {"samples": ["30", 0], "vae": ["3", 0]}}
                gg["50"] = {"class_type": (NODE_OUTPAINT_COMPOSITE or _need_node("node_outpaint_composite", "扩图合成")), "inputs": {
                    "generated": ["40", 0], "canvas": ["11", 0], "mask": ["11", 1]}}
                gg["60"] = {"class_type": "SaveImage", "inputs": {"images": ["50", 0],
                                                                  "filename_prefix": "QwenEdit/panel_outpaint"}}
                return gg

            def _border_ok(rel_path):
                """扩出来的四条边是不是"空灰板"：取最外 32px 窄带看标准差。"""
                try:
                    p = rel_path if os.path.isabs(rel_path) else os.path.join(OUTPUT_DIR, rel_path)
                    im = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)
                    if im is None:
                        return True
                    band = 32
                    strips = [im[:band, :], im[-band:, :], im[:, :band], im[:, -band:]]
                    return max(float(s.reshape(-1, 3).std()) for s in strips) >= 6.0
                except Exception:
                    return True

            t0 = time.time()
            rel = None
            for attempt in (1, 2):
                seed = random.randint(1, 2 ** 31 - 1)
                say(f"扩图采样（第 {attempt} 次 · 种子 {seed} · {steps} 步）...")
                rel, _secs, _pid = submit_and_wait(_build(seed), say)
                if _border_ok(rel):
                    break
                say("这次扩出来的边是空的（灰板），换个种子重试 ...")
            # 硬边合成会留下"画中画方框"（用户明确指出来过）→ 最后一步自己接管接缝
            rel = outpaint_seam_fix(rel, os.path.join(INPUT_DIR, os.path.basename(target)),
                                    oL, oT, oR, oB, bw, bh, say)
            # 只重绘外圈时，新扩出来的地面花纹/地毯图案不会自动跟原图对齐 → 再接缝重绘一次
            rel = outpaint_pattern_fix(rel, oL, oT, oR, oB, bw, bh, prompt, steps, say)
            secs = time.time() - t0
            say(f"完成 -> {os.path.basename(rel)}（{secs:.0f} 秒）")
            J.update(status="done", result=f"/file?p={urllib.parse.quote(rel)}",
                     result_name=os.path.basename(rel), seconds=round(secs),
                     note=f"扩图完成：{sw}×{sh} → {nw}×{nh}")
            return

        elif task == "faceswap":
            target = files.get("person")
            face = files.get("garment")
            if not target:
                J.update(status="error", error="请先选要换脸的照片")
                return
            if not face:
                J.update(status="error", error="请再选一张清晰正脸照（把这张脸换上去）")
                return
            fs_mode = (params.get("fs_mode") or "fast").strip()
            try:
                ident = float(params.get("fs_strength") if params.get("fs_strength") is not None else 0.85)
            except (TypeError, ValueError):
                ident = 0.85
            ident = max(0.0, min(1.0, ident))
            if fs_mode == "bfs":
                free_models("qwen")
                steps = int(params.get("steps") or 40)
                say(f"BFS 高质量换脸（Qwen 原生 + 头部遮罩 + 身份增强 {ident:.2f}，约 1-3 分钟）...")
                if any("bfs" in (n or "").lower() for n, _ in LORA_SLOTS["v"]):
                    say("提示：人脸增强已自动加载 BFS LoRA —— 请把 LoRA 插槽里重复的那个关掉，否则会叠加两次")
                stem = f"bfs{int(time.time())}"
                try:
                    b_name, m_name, hb = make_head_mask(target, stem)
                except Exception as e:
                    J.update(status="error", error=f"定位头部失败：{type(e).__name__}: {e}")
                    return
                say(f"头部区域已锁定 {hb}，只重绘这一块，身体和背景不动")
                g = qwen_graph21x([b_name, face], BFS_PROMPT, "", resolution=768, steps=steps, cfg=1.0,
                                  lora=QWEN_LORA_BFS, lora_strength=ident, mask_image=m_name)
                t0 = time.time()
                rel, _secs, _pid = submit_and_wait(g, say)
                say(f"Qwen 精修头部完成 -> {os.path.basename(rel)}（{time.time() - t0:.0f} 秒）")
                # ⚠ 实测（2026-10-02，6 组对照）：BFS LoRA 能把头部重画细，但**锁不住身份** ——
                #   出来的脸是"另一个人"，跟参考脸相似度只有 0.10~0.37（快速档 inswapper 是 0.85+）。
                #   所以最后一步必须自己锁脸：跟姿态那条"护脸"同一个套路。
                sim_before = 0.0
                try:
                    sim_before = face_sim(os.path.join(OUTPUT_DIR, rel), os.path.join(INPUT_DIR, face))[0]
                    lock_name = f"bfs_lock_{int(time.time())}.png"
                    _lock_img = cv2.imdecode(np.fromfile(os.path.join(OUTPUT_DIR, rel), np.uint8), cv2.IMREAD_COLOR)
                    if _lock_img is None:
                        raise RuntimeError("读不到刚生成的那张图")
                    cv2.imencode(".png", _lock_img)[1].tofile(os.path.join(INPUT_DIR, lock_name))
                    rel2, _lmsg = faceswap_onto(lock_name, face, identity=1.0)
                    sim_after = face_sim(os.path.join(OUTPUT_DIR, rel2), os.path.join(INPUT_DIR, face))[0]
                    if sim_after >= sim_before:
                        say(f"锁脸：相似度 {sim_before:.2f} → {sim_after:.2f}（inswapper 把参考脸贴到精修过的头上）")
                        rel = rel2
                    else:
                        say(f"锁脸没变好（{sim_before:.2f} → {sim_after:.2f}），保留 Qwen 那张")
                except Exception as e:
                    say(f"锁脸跳过（{type(e).__name__}: {str(e)[:60]}）—— 这张脸可能不像参考脸")
                secs = time.time() - t0
                say(f"完成 -> {os.path.basename(rel)}（{secs:.0f} 秒）")
                J.update(status="done", result=f"/file?p={urllib.parse.quote(rel)}",
                         result_name=os.path.basename(rel), seconds=round(secs),
                         note=f"BFS 高质量换脸完成（Qwen 精修头部 + inswapper 锁脸 · Qwen 原相似度 {sim_before:.2f}）")
                return
            say(f"用换脸模型（inswapper）换脸，约 4 秒 ...")
            if ident < 0.999:
                say("说明：快速换脸不叠加「身份强度」——inswapper 自己会把脸融合好；"
                    "再按比例混回原脸只会把换好的脸擦掉（强度 0.3 时等于没换）。强度只对「高质」模式有意义。")
            try:
                rel, msg = faceswap_onto(target, face, identity=1.0)
            except Exception as e:
                J.update(status="error", error=f"换脸失败：{type(e).__name__}: {e}")
                return
            say(f"已把那张脸换到这张照片上（{msg}）")
            J.update(status="done", result=f"/file?p={urllib.parse.quote(rel)}",
                     result_name=os.path.basename(rel), seconds=4,
                     note="换脸完成（快速 · inswapper）")
            say(f"完成 -> {os.path.basename(rel)}")
            return

        elif task == "free":
            free_models("qwen")
            mode = (params.get("free_mode") or "t2i").strip().lower()
            # 前端历史上叫过 single / edit 两个名字，只认 edit 会把上传的图丢掉 —— 统一归一化。
            if mode in ("single", "img2img", "i2i", "image_edit"):
                mode = "edit"
            if mode not in ("t2i", "edit", "multi"):
                mode = "edit" if (files.get("person") or params.get("chain_from")) else "t2i"
            prompt = (params.get("prompt") or "").strip()
            neg = (params.get("negative") or "").strip()
            steps = int(params.get("steps") or 40)
            cfg = float(params.get("cfg") or 1.0)
            seed_raw = int(params.get("seed") or -1)
            batch = max(1, min(4, int(params.get("batch") or 1)))
            try:
                w, h = [int(v) for v in (params.get("size") or "1024x1024").split("x")]
            except ValueError:
                w, h = 1024, 1024
            if not prompt:
                J.update(status="error", error="请先写提示词")
                return
            if len(prompt) > 16000:
                J.update(status="error", error=f"提示词 {len(prompt)} 字，超过 16000 字符上限，请精简一下")
                return
            refs = [files[k] for k in sorted((k for k in files if k.startswith("ref_")),
                                             key=lambda x: int(x.split("_")[1]) if x.split("_")[1].isdigit() else 0)]
            if mode == "edit":
                src = resolve_chain_input(params, files)
                if not src:
                    J.update(status="error", error="图片编辑要先选一张图（或从作品里点「↪ 接着改」）")
                    return
                refs = [src] + [r for r in refs if r]        # 主图 + 附加参考图，合起来最多 10 张
            elif mode == "multi" and not refs:
                J.update(status="error", error="多图参考要选 2~10 张参考图")
                return
            if len(refs) > 10:
                say(f"选了 {len(refs)} 张参考图，按官方上限取前 10 张")
                refs = refs[:10]
            say(f"创作模式：{'文生图' if mode == 't2i' else ('图片编辑' if mode == 'edit' else '多图参考')}"
                + (f" · 参考图 {len(refs)} 张" if refs else ""))
            # 姿势：文字模板 + 姿势参考图（两条路可叠加）
            pose_key = (params.get("pose") or "").strip()
            pose_ref = files.get("pose_ref")
            if pose_ref:
                refs = refs + [pose_ref]
                prompt = prompt + POSE_REF_CLAUSE.format(n=len(refs))
                say(f"带姿势参考图（图{len(refs)}），按它的姿势来")
            if pose_key and pose_key in POSE_PRESETS:
                prompt = prompt + POSE_CLAUSE.format(p=POSE_PRESETS[pose_key])
                say(f"姿势模板：{POSE_PRESETS[pose_key][:24]}...")
            # 裁边已经在收图那一步全局做过了（见 do_POST），这里不再重复裁
            say(f"自由出图（{ {'t2i': '文生图', 'edit': '单图编辑', 'multi': '多图参考'}.get(mode, mode) }）"
                f"{w}x{h} · {steps} 步 · cfg={cfg} · 出 {batch} 张"
                + (f" · 参考图 {len(refs)} 张" if refs else ""))
            turbo = (params.get("turbo") or "") in ("1", "true", "on")
            if turbo:
                say("⚡ 加速模式：Viggle Turbo 6 步（关 CFG，负向提示词不生效）")
            # 「图片编辑」里明显是要换衣服、而且给了第二张参考图 → 直接借用「换衣」那套：
            # 参考图不是白底商品图就先把里面那套衣服抠成白底服装图，再套强约束模板 + 负向词
            # （cfg 提到 2.5 负向词才生效）。实测：原样把"把第二张的衣服穿到第一张身上"丢给模型，
            # 模型常常只换一半（只换裤子不换上衣）或干脆不换。
            if (mode == "edit" and len(refs) >= 2 and not turbo
                    and re.search(r"换(上衣|下装|衣|装|裤|裙|全身)|穿上|穿到|衣服.{0,8}(换|转移)", prompt)):
                extra_hint = ""
                need_extract = False
                try:
                    _p = refs[1] if os.path.isabs(refs[1]) else os.path.join(INPUT_DIR, os.path.basename(refs[1]))
                    _im = cv2.imdecode(np.fromfile(_p, dtype=np.uint8), cv2.IMREAD_COLOR)
                    if _im is not None and _face_box is not None:
                        # 参考图里有人 → 衣服长在人身上，得先剥成白底服装图
                        need_extract = bool(_face_box(_im))
                    else:
                        need_extract = not looks_like_garment_shot(refs[1])
                except Exception:
                    need_extract = not looks_like_garment_shot(refs[1])
                if need_extract:
                    try:
                        gnew, gsec, _grel = extract_garment(refs[1], f"garment_only_{int(time.time())}.png")
                        if gnew:
                            say(f"识别到换衣意图：参考图里有人，先把那套衣服抠成白底服装图 → {gnew}（{gsec:.0f} 秒）")
                            refs[1] = gnew
                            extra_hint = "（图2 已处理成白底服装图）"
                    except Exception as e:
                        say(f"服装提取跳过（{type(e).__name__}），直接用原参考图")
                else:
                    say("参考图本来就是服装图，不重复抠（省一次生成）")
                prompt = (VTON_TMPL.format(extra="和服装",
                                           extra_line="图2：服装参考图——这是唯一正确的服装来源。" + extra_hint)
                          + f"\n用户补充要求（优先级最高）：{prompt}")
                neg = VTON_NEG
                cfg = 2.5
                say("已套用换衣强约束：整套照图2 / 去掉图1原衣 / 姿势背景照图1 · 带负向词 · cfg=2.5")
            J["prompt_pos"], J["prompt_neg"] = prompt, neg

            # 「图片编辑」里说的是换背景/换场景 → 自动走"只重画背景、人物原样贴回"那套（脸不会变）。
            # 这样「编辑工具」里那个单独的"改背景/换场景"入口就多余了，已删掉（用户点名说重复）。
            scene_intent = bool(mode == "edit" and not turbo and refs and re.search(
                r"换背景|改背景|换个背景|换场景|改场景|换个环境|把背景|背景(换成|改成|换|改)", prompt))
            if scene_intent:
                scene_pos = SCENE_TMPL.format(scene=prompt, fimg=(
                    "图2：背景参考图——场景风格与陈设参考这张图。" if len(refs) > 1
                    else "（没有背景参考图，按上面的文字描述生成场景。）"))
                say("识别到换背景/换场景意图：只重画背景，人物（含脸）用原图像素贴回")
                J["prompt_pos"], J["prompt_neg"] = scene_pos, SCENE_NEG

                def _mk_scene(seed=None):
                    return qwen_scene_graph(refs[0], refs[1:3], scene_pos, SCENE_NEG, steps=steps)

                builder = lambda: _mk_scene(None if seed_raw < 0 else seed_raw)
            elif refs:
                # 有参考图：走 Qwen-Image-2.1 原生多参考节点
                # 参考图一多，编码精度必须降：视觉 token ≈(边长/16)²，1344×8 张＝5 万多 token，
                # 实测 7 分钟出不来还把机器压死。默认「自动」＝≥4 张按 768 编码（同类软件默认同样是 768）。
                _res = min(max(w, h), 768 if len(refs) >= 4 else 1024)
                # 界面上的「参考图编码精度」（照同类软件：512/768/1024/1536/2048）；"auto"/没传就走上面的规则
                _sel = (params.get("ref_res") or "auto").strip()
                if _sel.isdigit() and 256 <= int(_sel) <= 2048:
                    _ref_res = int(_sel)
                    say(f"参考图编码精度：按你选的 {_ref_res}（共 {len(refs)} 张参考图）")
                    # 按视觉 token ≈(边长/16)² 估一下：当年 8 张 ×1344 ≈5.6 万 token 把整机压死过。
                    _side1 = min(1024, max(512, _res))
                    _tok = (_side1 // 16) ** 2 + max(0, len(refs) - 1) * (_ref_res // 16) ** 2
                    if _tok > 30000:
                        say(f"⚠ 这样编码约 {_tok / 1000:.0f}k 视觉 token（>30k 就可能很慢/吃显存；"
                            f"出不来就把「参考图编码精度」降一档，或减少参考图张数）")
                else:
                    _ref_res = 768
                    if len(refs) >= 4:
                        say(f"参考图 {len(refs)} 张 → 自动把编码精度降到 768（张数多时不降会把机器压死）")

                def _mk21(seed=None):
                    return qwen_graph21(refs, prompt, neg, resolution=_res, ref_res=_ref_res, seed=seed,
                                        steps=steps, cfg=cfg, turbo=turbo)
                builder = lambda: _mk21(None if seed_raw < 0 else seed_raw)
            else:
                def _mk(seed=None):
                    return qwen_graph([], prompt, neg or " ", seed=seed, steps=steps, cfg=cfg,
                                      size=(w, h), batch=1, turbo=turbo)
                builder = lambda: _mk(None if seed_raw < 0 else seed_raw)

        elif task == "fuse":
            # 多图合成一个镜头：抠图分类 → 自己排版 → 模型只统一光线（详见 fuse_* 那一段的注释）
            free_models("qwen")
            names = [files[k] for k in sorted((k for k in files if k.startswith("f_")),
                                              key=lambda x: int(x.split("_")[1]) if x.split("_")[1].isdigit() else 0)]
            names = [n for n in names if n]
            if len(names) < 2:
                J.update(status="error", error="至少要选 2 张照片（3~10 张最合适）")
                return
            if len(names) > 10:
                say(f"选了 {len(names)} 张，按上限取前 10 张")
                names = names[:10]
            try:
                w, h = [int(v) for v in (params.get("size") or "1536x864").split("x")]
            except ValueError:
                w, h = 1536, 864
            steps = int(params.get("steps") or 40)
            cfg = float(params.get("cfg") or 1.0)
            extra = (params.get("prompt") or "").strip()
            say(f"多图合成一个镜头：收到 {len(names)} 张照片 · 目标 {w}x{h} · {steps} 步")
            _clean_fuse_temp()
            towers, scenes = fuse_matte_all(names, say)
            if not towers and not scenes:
                J.update(status="error", error="这些照片一张都没能处理，换几张再试")
                return
            say(f"分类：主体 {len(towers)} 个 · 大场景 {len(scenes)} 张")
            comp = fuse_compose(towers, scenes, w, h, say)
            pos = FUSE_POS + (f"\n补充要求（用户指定，优先级最高，但不得改变上面第 1 条）：{extra}" if extra else "")
            J["prompt_pos"], J["prompt_neg"] = pos, FUSE_NEG
            say("交给模型：只统一天空/光线/去拼接痕迹，每栋楼的位置和大小不许动")

            def _mk_fuse(seed=None):
                return qwen_graph21([comp], pos, FUSE_NEG, resolution=min(max(w, h), 1024),
                                    seed=seed, steps=steps, cfg=cfg, ref_res=768)

            builder = lambda: _mk_fuse(None)

        elif task == "caption":
            free_models("qwen")
            # 支持 1~10 张：cap_0..cap_9 / ref_* / person 都收
            def _k(n):
                try:
                    return int(n.split("_")[1])
                except (IndexError, ValueError):
                    return 0
            imgs = [files[k] for k in sorted((k for k in files if k.startswith("cap_")), key=_k)]
            if not imgs:
                imgs = [files[k] for k in sorted((k for k in files if k.startswith("ref_")), key=_k)]
            if not imgs and files.get("person"):
                imgs = [files["person"]]
            imgs = [i for i in imgs if i][:10]
            if not imgs:
                J.update(status="error", error="请先选至少一张要读的图")
                return
            purpose = (params.get("purpose") or "plain").strip()
            pname, pask, ptmpl = CAPTION_PURPOSES.get(purpose, CAPTION_PURPOSES["plain"])
            ask_extra = (params.get("prompt") or "").strip()
            if ask_extra:
                pask += f"（补充要求：{ask_extra}）"
            say(f"读图出描述：{len(imgs)} 张，用途=「{pname}」")
            ml = int(params.get("max_length") or 512)
            texts = []
            total = 0.0
            for i, name in enumerate(imgs, 1):
                say(f"  正在读第 {i}/{len(imgs)} 张 ...")
                t, _txt, secs = submit_caption(caption_graph(name, pask, ml), say)
                texts.append(t.strip())
                total += secs
                say(f"  第 {i} 张读完（{secs} 秒，{len(t)} 字）")
            if len(texts) > 1:
                label_from = max(1, int(params.get("label_from") or 1))
                combined = "\n\n".join(f"图{label_from + i}：{t}" for i, t in enumerate(texts))
            else:
                combined = texts[0]
            ref_label = (f"图{max(1, int(params.get('label_from') or 1))}"
                         if len(texts) > 1 else "参考图")
            instruction = ptmpl.format(d=combined, ref=ref_label)
            one, _all = save_caption_files(instruction, imgs[0] if len(imgs) == 1 else f"{len(imgs)}张图",
                                           f"用途={pname}")
            if one:
                say(f"已存到工作区：{one}")
            J.update(status="done", result=None, result_name=f"描述（{pname}）.txt",
                     text=instruction, seconds=round(total, 1),
                     caption_file=one or "",
                     note=f"{len(imgs)} 张 · {len(instruction)} 字 · 用途={pname}")
            say(f"完成（{total:.0f} 秒）：")
            for line in instruction.splitlines():
                if line.strip():
                    say("   " + line.strip()[:140])
            return

        elif task == "prompttool":
            # 提示词增强节点（第三方，可选）：用途=扩写改写 / 图片反推 / 衣服清单
            free_models("qwen")

            def _ptk(n):
                try:
                    return int(n.split("_")[1])
                except (IndexError, ValueError):
                    return 0
            imgs = [files[k] for k in sorted((k for k in files if k.startswith("pt_")), key=_ptk)]
            if not imgs and files.get("person"):
                imgs = [files["person"]]
            if not imgs:
                _ch = resolve_chain_input(params, files, key="_none_")
                if _ch:
                    imgs = [_ch]
            imgs = [i for i in imgs if i][:10]
            purpose = (params.get("purpose") or "rewrite").strip()
            if purpose not in PROMPTTOOL_NAMES:
                purpose = "rewrite"
            pname = PROMPTTOOL_NAMES[purpose]
            language = (params.get("language") or "中文").strip()
            if language not in ("中文", "English"):
                language = "中文"
            pmode = (params.get("pt_mode") or "文生图").strip()
            if pmode not in ("文生图", "图片编辑"):
                pmode = "文生图"
            transparent = (params.get("transparent") or "") in ("1", "true", "on")
            ask = (params.get("prompt") or "").strip()
            if len(ask) > 16000:
                J.update(status="error", error=f"要求文字 {len(ask)} 字，请控制在 16000 字符以内")
                return
            if purpose != "rewrite" and not imgs:
                J.update(status="error", error=f"「{pname}」需要先选图（1–10 张）")
                return
            if purpose == "rewrite" and not ask and not imgs:
                J.update(status="error", error="「扩写/改写」要先写一句要求，或选一张参考图")
                return
            say(f"提示词工具 · {pname} · {language} · 模式={pmode} · {len(imgs)} 张参考图")
            g = {"2": {"class_type": "CLIPLoader",
                       "inputs": {"clip_name": QWEN_CLIP, "type": "qwen_image", "device": "default"}}}
            ins = {"clip": ["2", 0], "prompt": ask, "mode": pmode,
                   "transparent": bool(transparent), "purpose": purpose, "language": language}
            for i, name in enumerate(imgs, 1):
                g[str(10 + i)] = {"class_type": "LoadImage", "inputs": {"image": name}}
                ins[f"image_{i}"] = [str(10 + i), 0]
            g["12"] = {"class_type": (NODE_PROMPT_ENHANCE or _need_node("node_prompt_enhance", "提示词增强")), "inputs": ins}
            g["20"] = {"class_type": "SaveText", "inputs": {"text": ["12", 0],
                                                            "filename_prefix": "QwenEdit/prompttool",
                                                            "format": "txt"}}
            text, _txt, secs = submit_caption(g, say, timeout_s=1200)
            one, _all = save_caption_files(text, imgs[0] if imgs else "prompttool", pname)
            J.update(status="done", result=None, result_name=f"{pname}.txt",
                     text=text, seconds=round(secs, 1), caption_file=one or "",
                     note=f"{pname} · {len(text)} 字 · {secs:.0f} 秒")
            say(f"完成（{secs:.0f} 秒，{len(text)} 字）")
            for line in text.splitlines():
                if line.strip():
                    say("   " + line.strip()[:160])
            return

        elif task == "multiangle":
            # 图像工具 › 多角度：按原图顺序逐个生成选中的视角（同类软件规格：最多 100 图 × 8 视角）
            free_models("qwen")

            def _mk(n):
                try:
                    return int(n.split("_")[1])
                except (IndexError, ValueError):
                    return 0
            imgs = [files[k] for k in sorted((k for k in files if k.startswith("ml_")), key=_mk)]
            if not imgs:
                _ch = resolve_chain_input(params, files)
                if _ch:
                    imgs = [_ch]
            imgs = [i for i in imgs if i][:100]
            if not imgs:
                J.update(status="error", error="请先上传 1–100 张图片（或用「继续创作」选一张）")
                return
            try:
                ha = max(0, min(360, int(float(params.get("ha") or 0))))
                va = max(-30, min(60, int(float(params.get("va") or 0))))
                zoom = max(0.0, min(10.0, float(params.get("zoom") or 5.0)))
            except ValueError:
                J.update(status="error", error="镜头参数无效：水平角度须为 0–360°，垂直角度 -30–60°，镜头距离 0–10。")
                return
            views = []
            raw = (params.get("views") or "").strip()
            if raw:
                for part in raw.split(";"):
                    pcs = part.split(",")
                    if len(pcs) >= 3:
                        try:
                            views.append((int(float(pcs[0])), int(float(pcs[1])), float(pcs[2])))
                        except ValueError:
                            continue
            if not views:
                views = [(ha, va, zoom)]
            extra = (params.get("prompt") or "").strip()
            steps = int(params.get("steps") or 40)
            total = len(imgs) * len(views)
            if total > 16:
                J.update(status="error", error=f"一次最多 16 张（现在是 {len(imgs)} 图 × {len(views)} 视角 = {total}）；分批来")
                return
            say(f"多角度：{len(imgs)} 张原图 × {len(views)} 个视角 = {total} 张，按原图顺序串行")
            outs, t0 = [], time.time()
            for ii, name in enumerate(imgs, 1):
                for vi, (h, v, z) in enumerate(views, 1):
                    cp = cam_prompt(h, v, z)
                    pos = cp + (("。" + extra) if extra else "")
                    say(f"  图 {ii}/{len(imgs)} · 视角 {vi}/{len(views)}：{cp}")
                    g = qwen_graph21x([name], pos, "", resolution=768, steps=steps, cfg=1.0,
                                      prefix="QwenEdit/panel_multiangle")
                    rel, _s, _p = submit_and_wait(g, say)
                    outs.append(rel)
                    say(f"    -> {os.path.basename(rel)}")
            secs = time.time() - t0
            last = outs[-1]
            J.update(status="done", result=f"/file?p={urllib.parse.quote(last)}",
                     result_name=os.path.basename(last), seconds=round(secs),
                     note=f"多角度完成：{len(outs)} 张（{len(imgs)} 图 × {len(views)} 视角）· {secs:.0f} 秒")
            say(f"全部完成：{len(outs)} 张，用时 {secs:.0f} 秒")
            return

        elif task == "poserender":
            # 动作姿态第一步：把姿势 JSON + 人物图渲染成"人偶姿态图"（几秒，不用采样）
            free_models("qwen")
            person = files.get("person") or resolve_chain_input(params, files, key="prefx")
            if not person:
                J.update(status="error", error="请先选一张人物参考图（人偶图要照着它的身形/构图出）")
                return
            name = (params.get("pose_name") or "").strip()
            pj = (params.get("pose_json") or "").strip()

            def _pose_usable(s):
                """空壳姿势（"{}" / 没有 pose.bones）不算"有姿势"。
                以前只判空字符串，于是 "{}" 能混过去，一路撞到第三方节点才报
                「请打开机位与姿态编辑器，点击「应用到节点」…」——用户看不出这是谁的问题。"""
                try:
                    d = json.loads(s or "{}")
                except ValueError:
                    return False
                return bool((d.get("pose") or {}).get("bones"))

            if not _pose_usable(pj) and name:
                fp = resolve_pose_path(name)      # 唯一入口：名字净化 + 只认姿势库目录
                if fp and os.path.isfile(fp):
                    with open(fp, encoding="utf-8") as fh:
                        pj = fh.read()
                elif not pj:
                    J.update(status="error", error=f"姿势库没有这个姿势：{name}")
                    return
            if not _pose_usable(pj):
                J.update(status="error",
                         error="还没拿到姿势：先打开「机位与姿态编辑器」，调好动作后点『应用到节点』，再点「渲染人偶图」")
                return
            try:
                w = int(params.get("width") or 1024)
                h = int(params.get("height") or 1024)
                rr = int(params.get("ref_res") or 1024)
            except ValueError:
                w = h = rr = 1024
            extra = (params.get("extra_prompt") or "").strip()
            say(f"渲染人偶姿态图（FisherQwenFreePose · {w}×{h} · 参考精度 {rr}）...")
            t0 = time.time()
            g = pose_render_graph(pj, person, width=w, height=h, ref_res=rr, extra=extra,
                                  prefix="QwenEdit/panel_mannequin")
            rel, _s, _pid = submit_and_wait(g, say)
            secs = time.time() - t0
            say(f"人偶图已出 -> {os.path.basename(rel)}（{secs:.1f} 秒，不用采样）")
            J.update(status="done", result=f"/file?p={urllib.parse.quote(rel)}",
                     result_name=os.path.basename(rel), seconds=round(secs, 1),
                     note=f"人偶姿态图完成（{secs:.1f} 秒）· 下一步可点「开始生成」用 PoseStudio LoRA 出成图")
            return

        elif task == "batch":
            # 批量处理：现在接的是「批量提取服装」（复用已验证的 extract_garment）
            free_models("qwen")
            mode = (params.get("mode") or "garment").strip()

            def _bk(n):
                try:
                    return int(n.split("_")[1])
                except (IndexError, ValueError):
                    return 0
            imgs = [files[k] for k in sorted((k for k in files if k.startswith("b_")), key=_bk)]
            imgs = [i for i in imgs if i][:100]
            if not imgs:
                J.update(status="error", error="请先选要批量处理的图（1–100 张）")
                return
            if mode not in ("garment", "vton", "cutout"):
                J.update(status="error", error=f"批量模式 {mode} 还没接（现在可用：批量抠图、批量提取服装、批量换装）")
                return
            _turbo = (params.get("turbo") or "") in ("1", "true", "on")
            if mode == "cutout":
                # 抠图：本地去背景模型，不采样、不联网；串行逐张，每张几秒
                model = (params.get("bg_model") or BG_MODEL).strip()
                if model not in BG_MODELS:
                    model = BG_MODEL
                white = (params.get("bg") or "transparent").strip() == "white"
                say(f"批量抠图：{len(imgs)} 张 → {'白底 PNG' if white else '透明 PNG'}（模型 {model}）")
                outs, t0 = [], time.time()
                for i, name in enumerate(imgs, 1):
                    say(f"  [{i}/{len(imgs)}] {name}")
                    try:
                        _n, esec, erel = cutout(name, f"cutout_{i}_{int(time.time())}.png",
                                                model=model, white=white)
                    except Exception as e:
                        say(f"    失败：{type(e).__name__}: {e}")
                        continue
                    outs.append(erel)
                    say(f"    -> {os.path.basename(erel)}（{esec:.1f} 秒）")
                secs = time.time() - t0
                if not outs:
                    J.update(status="error", error="这批一张都没成功")
                    return
                last = outs[-1]
                J.update(status="done", result=f"/file?p={urllib.parse.quote(last)}",
                         result_name=os.path.basename(last), seconds=round(secs),
                         note=f"批量抠图完成：{len(outs)}/{len(imgs)} 张 · {secs:.0f} 秒")
                say(f"完成：{len(outs)}/{len(imgs)} 张，用时 {secs:.0f} 秒")
                return
            if mode == "vton":
                # 服装可以选任意多套（以前只能 1 张，人物却能选 100 张 —— 不合理）
                glist = [files.get("garment_%d" % i) for i in range(100) if files.get("garment_%d" % i)]
                if not glist and files.get("garment"):
                    glist = [files["garment"]]
                if not glist:
                    J.update(status="error", error="批量换装要先选至少一张服装图")
                    return
                pair = (params.get("pair") or "cycle").strip()
                steps = int(params.get("steps") or (12 if _turbo else 40))   # 25 步人都变了，兜底一律 40
                if pair == "each":       # 每个人试所有衣服
                    tasks = [(nm, g) for nm in imgs for g in glist]
                elif pair == "first":    # 只用第 1 套，换给所有人
                    tasks = [(nm, glist[0]) for nm in imgs]
                else:                    # 一个人配一套（按顺序，衣服不够就循环用）
                    tasks = [(nm, glist[i % len(glist)]) for i, nm in enumerate(imgs)]
                say(f"批量换装：{len(imgs)} 张人物 × {len(glist)} 套服装 → 共 {len(tasks)} 张"
                    + ("（每个人试所有衣服）" if pair == "each"
                       else "（只用第 1 套换给所有人）" if pair == "first"
                       else "（按顺序一对一配，衣服不够就循环）")
                    + ("（turbo 6 步）" if _turbo else f"（{steps} 步）"))
                outs, t0 = [], time.time()
                for i, (name, garment) in enumerate(tasks, 1):
                    say(f"  [{i}/{len(tasks)}] {name}  ×  {garment}")
                    pos = VTON_TMPL.format(extra="和服装", extra_line="图2：服装参考图——这是唯一正确的服装来源。")
                    try:
                        g = qwen_graph21x([name, garment], pos, VTON_NEG, resolution=768,
                                          steps=steps, cfg=1.0, prefix=f"QwenEdit/batch_vton_{i}")
                        rel, _s, _p = submit_and_wait(g, say)
                        outs.append(rel)
                        say(f"    -> {os.path.basename(rel)}")
                    except Exception as e:
                        say(f"    失败：{type(e).__name__}: {e}")
                secs = time.time() - t0
                if not outs:
                    J.update(status="error", error="这批一张都没成功")
                    return
                last = outs[-1]
                J.update(status="done", result=f"/file?p={urllib.parse.quote(last)}",
                         result_name=os.path.basename(last), seconds=round(secs),
                         note=f"批量换装完成：{len(outs)}/{len(imgs)} 张 · {secs:.0f} 秒")
                say(f"完成：{len(outs)}/{len(imgs)} 张，用时 {secs:.0f} 秒")
                return
            say(f"批量提取服装：{len(imgs)} 张，逐张把衣服抠成白底服装图"
                + ("（turbo 6 步加速）" if _turbo else ""))
            outs, t0 = [], time.time()
            for i, name in enumerate(imgs, 1):
                say(f"  [{i}/{len(imgs)}] {name}")
                try:
                    new_name, esec, erel = extract_garment(name, f"batch_garment_{i}_{int(time.time())}.png",
                                                          turbo=_turbo)
                except Exception as e:
                    say(f"    失败：{type(e).__name__}: {e}")
                    continue
                if new_name:
                    outs.append(erel or new_name)
                    say(f"    -> {os.path.basename(erel or new_name)}（{esec:.0f} 秒）")
            secs = time.time() - t0
            if not outs:
                J.update(status="error", error="这批一张都没成功")
                return
            last = outs[-1]
            J.update(status="done", result=f"/file?p={urllib.parse.quote(last)}",
                     result_name=os.path.basename(last), seconds=round(secs),
                     note=f"批量提取服装完成：{len(outs)}/{len(imgs)} 张 · {secs:.0f} 秒")
            say(f"完成：{len(outs)}/{len(imgs)} 张，用时 {secs:.0f} 秒")
            return

        elif task == "tts":
            # 配音：Breeze-TTS 2（本机跑，不联网）。产物 wav 落到输入目录，可被数字人/按音频说话直接取用。
            free_models("tts")
            text = (params.get("text") or "").strip()
            if not text:
                J.update(status="error", error="先写要念的台词")
                return
            mode = (params.get("tts_mode") or "design").strip()
            ref = files.get("audio")
            if mode == "clone" and not ref:
                mode = "design"
                say("没给参考音 → 改用「文字描述音色」")
            if mode == "clone" and not (params.get("ref_text") or "").strip():
                # 界面以前写的是"留空＝自动转写"，但本机没装转写模型（Breeze 那个 Whisper 要联网下 1.6G），
                # 留空时节点只会甩一句英文 "Reference audio requires its exact transcript (ref_text)"。
                J.update(status="error",
                         error="克隆音色必须填「参考音里说的原话」—— 把那段参考音里说的话原样打进去。"
                               "（自动转写这台机器上还没有：本地缺 Whisper 转写模型，暂时只能手打）")
                return
            say(f"配音（Breeze-TTS 2）· {'克隆音色' if mode == 'clone' else '文字描述音色'} · {len(text)} 字 ...")
            graph = tts_graph(text, mode=mode, instruction=(params.get("voice") or "").strip(),
                              ref_audio=ref, ref_text=(params.get("ref_text") or "").strip(),
                              seed=int(params.get("seed") or 42))
            rel, secs, _pid = submit_and_wait(graph, say)
            src = os.path.join(OUTPUT_DIR, rel)
            if not os.path.isfile(src):
                J.update(status="error", error=f"配音产物找不到：{rel}")
                return
            name = f"tts_{int(time.time())}.wav"
            try:
                run_hidden([FFMPEG_BIN, "-y", "-v", "error", "-i", src, "-ar", "24000", "-ac", "1",
                                os.path.join(INPUT_DIR, name)], check=True, timeout=300)
            except Exception as e:
                J.update(status="error", error=f"转 wav 失败：{type(e).__name__}: {e}")
                return
            _name_p = resolve_user_path(name, [INPUT_DIR])
            if not _name_p:
                J.update(status="error", error="生成出来的音频找不到（只允许读面板自己的 input 目录）")
                return
            dur = audio_seconds(_name_p)
            say(f"配音完成 -> {name}（{dur:.1f} 秒）；可直接拿去「数字人」页说话")
            J.update(status="done", result=f"/raw?which=input&p={urllib.parse.quote(name)}",
                     result_name=name, seconds=round(secs),
                     note=f"{dur:.1f} 秒 · 已落到输入目录，数字人页可直接选它")
            return

        elif task == "longcat":
            # 数字人：LongCat Avatar（本机 int8）。照片 + 音频 → 对口型说话视频。
            free_models("longcat")
            person = files.get("person") or resolve_chain_input(params, files)
            aud = files.get("audio") or (params.get("audio_chain") or "").strip()
            if not person:
                J.update(status="error", error="先选一张正面清晰的照片（脸要看得清）")
                return
            if not aud:
                J.update(status="error", error="先选一段音频；或在「配音」页生成一段，点「拿它做数字人」")
                return
            res = (params.get("resolution") or "480p").strip()
            if res not in ("480p", "720p"):
                res = "480p"
            steps = max(4, min(16, int(params.get("steps") or 8)))
            say(f"数字人 LongCat（本机 int8 · {res} · {steps} 步）：音频 {aud}")
            # 第五轮复验建议：这一支原来把 aud 原样交给节点，也统一走唯一入口
            _aud2 = resolve_user_path(aud, [INPUT_DIR, OUTPUT_DIR])
            if not _aud2:
                J.update(status="error", error="那段音频不在面板目录里（为安全只允许用面板自己的音频）")
                return
            graph = longcat_graph(person, _aud2, prompt=(params.get("prompt") or "").strip(),
                                  resolution=res, steps=steps)
            rel, secs, _pid = submit_and_wait(graph, say)
            say(f"完成 -> {os.path.basename(rel)}（{secs} 秒）")
            J.update(status="done", result=f"/file?p={urllib.parse.quote(rel)}",
                     result_name=os.path.basename(rel), seconds=round(secs),
                     note="LongCat 数字人 · 带原音频")
            return

        elif task == "pose":
            # 图像工具 › 动作姿态：姿态图当 image 1，人物参考图按顺序当 image 2 / 3；PoseStudio LoRA 自动加载
            free_models("qwen")
            pose_img = files.get("person") or resolve_chain_input(params, files, key="pose")

            def _pk(n):
                try:
                    return int(n.split("_")[1])
                except (IndexError, ValueError):
                    return 0
            refs = [files[k] for k in sorted((k for k in files if k.startswith("pref_")), key=_pk)]
            refs = [r for r in refs if r][:2]
            if not refs:
                _rc = resolve_chain_input(params, {}, key="_none_", src_key="ref_chain")
                if _rc:
                    refs = [_rc]
            pose_list = [x.strip() for x in (params.get("pose_list") or "").split(";") if x.strip()]
            if not pose_img and not pose_list:
                J.update(status="error", error="缺少姿态图（白底姿态截图）；或用姿态卡片批次（pose_list）")
                return
            if not refs:
                J.update(status="error", error="缺少人物参考图（1–2 张；双人时按 A 蓝 / B 橙 顺序）")
                return
            strength = float(params.get("strength") or 1.0)
            steps = int(params.get("steps") or 40)
            cfg = float(params.get("cfg") or 1.0)
            extra = (params.get("prompt") or "").strip()
            pos = POSE_PROMPT + (("\n补充要求（用户要求，优先级最高）：" + extra) if extra else "")
            pose_list = [x.strip() for x in (params.get("pose_list") or "").split(";") if x.strip()]
            count = max(1, min(8, int(params.get("count") or 1)))
            if pose_list:      # 姿态卡片批次：每个卡片＝一个姿势，串行；每卡 count 张
                total = len(pose_list) * count
                if total > 16:
                    J.update(status="error", error=f"一次最多 16 张（现在 {len(pose_list)} 姿势 × {count} 张 = {total}）")
                    return
                say(f"姿态批次：{len(pose_list)} 个姿势 × {count} 张 = {total} 张（每个姿势先渲人偶图，再用 PoseStudio 出成图）")
                outs, t0 = [], time.time()
                for i, nm in enumerate(pose_list, 1):
                    fp = resolve_pose_path(nm)     # 唯一入口（姿势卡片批次同样不许穿越）
                    if not fp or not os.path.isfile(fp):
                        say(f"  跳过：姿势库没有 {nm}")
                        continue
                    with open(fp, encoding="utf-8") as fh:
                        pj = fh.read()
                    say(f"  [{i}/{len(pose_list)}] 渲人偶：{nm}")
                    mrel, _s1, _p1 = submit_and_wait(
                        pose_render_graph(pj, refs[0], width=768, height=1024, ref_res=1024,
                                          extra="白色背景，全身", prefix=f"QwenEdit/card_{i}"), say)
                    mfull = os.path.join(OUTPUT_DIR, mrel)
                    mname = f"card{i}_{int(time.time())}.png"
                    try:
                        import shutil
                        shutil.copyfile(mfull, os.path.join(INPUT_DIR, mname))
                    except OSError as e:
                        say(f"  人偶图复制失败：{e}")
                        continue
                    for c in range(count):
                        say(f"     出成图 {c + 1}/{count}（seed 随机）")
                        rel, _s2, _p2 = pose_gen_with_fallback(
                            mname, refs, pos, steps, cfg, strength, say, prefix=f"QwenEdit/panel_pose_{nm}")
                        rel, _lock = pose_face_lock(rel, refs, say)
                        outs.append(rel)
                        say(f"       -> {os.path.basename(rel)}")
                secs = time.time() - t0
                if not outs:
                    J.update(status="error", error="这批一张都没出（姿势名或人偶图有问题）")
                    return
                J.update(status="done", result=f"/file?p={urllib.parse.quote(outs[-1])}",
                         result_name=os.path.basename(outs[-1]), seconds=round(secs),
                         note=f"姿态批次完成：{len(outs)} 张 · {secs:.0f} 秒")
                say(f"批次完成：{len(outs)} 张，用时 {secs:.0f} 秒")
                return
            _bu, _bgguf = tier_unet()
            say(f"动作姿态：姿态图 image 1 ＋ 人物参考图 {len(refs)} 张 image 2/3；"
                f"PoseStudio LoRA 自动加载（{QWEN_LORA_POSE} · 强度 {strength} · {steps} 步 · CFG {cfg} · "
                f"基座模型 {_bu}{'（GGUF）' if _bgguf else ''}）")
            t0 = time.time()
            rel, _s, _p = pose_gen_with_fallback(pose_img, refs, pos, steps, cfg, strength, say)
            rel, lockmsg = pose_face_lock(rel, refs, say)
            say(f"护脸：{lockmsg}")
            secs = time.time() - t0
            say(f"完成 -> {os.path.basename(rel)}（{secs:.0f} 秒）")
            J.update(status="done", result=f"/file?p={urllib.parse.quote(rel)}",
                     result_name=os.path.basename(rel), seconds=round(secs),
                     note=f"动作姿态完成 · V1.1 强度 {strength} · {steps} 步 · {secs:.0f} 秒 · {lockmsg}")
            return

        elif task == "scene":
            free_models("qwen")
            person = files.get("person")
            if not person:
                J.update(status="error", error="请先选人物照片")
                return
            scene = (params.get("scene") or "").strip()
            if not scene:
                J.update(status="error", error="请写一句场景描述，例如：坐在深色木书桌前，背后是整面书柜")
                return
            bg = files.get("bg_ref")
            imgs = [person] + ([bg] if bg else [])
            fimg = "图2：背景参考图——场景风格与陈设参考这张图。" if bg else "（没有提供背景参考图，按下面的文字描述生成场景。）"
            note = (params.get("note") or "").strip()
            pos = SCENE_TMPL.format(scene=scene, fimg=fimg)
            if note:
                pos += f"\n补充说明（用户要求，优先级最高）：{note}"
            say(f"提交 Qwen-Image 2.1 改场景（{len(imgs)} 张参考图）· 只重画背景、人物原样贴回 ...")
            J["prompt_pos"], J["prompt_neg"] = pos, SCENE_NEG
            _steps = int(params.get("steps") or 40)
            _bgm = (params.get("bg_model") or "").strip() or None
            builder = lambda: qwen_scene_graph(person, [bg] if bg else [], pos, SCENE_NEG,
                                               bg_model=_bgm, steps=_steps)

        else:
            J.update(status="error", error=f"未知任务: {task}")
            return

        person_path = os.path.join(INPUT_DIR, files["person"]) if files.get("person") else None
        vton_mode = (params.get("vton_mode") or "raw").strip()
        # 多候选 + 自动筛选：50% 左右的单次成功率下，出 4 张基本能捞到及格的
        want = int(params.get("candidates") or 0)
        if task in ("vton", "hair") and want <= 0:
            want = 4 if vton_mode in ("safe", "whole_protect", "raw", "whole") else 1
        if task == "free":
            want = max(1, min(4, int(params.get("batch") or 1)))
        attempts = want if task in ("vton", "hair", "free") else 1
        upscale_to = (params.get("upscale_to") or "").strip()
        if attempts < 1:
            J.update(status="error", error="候选数算出来是 0，没法生成（内部错误）")
            return
        cands = []
        for k in range(1, attempts + 1):
            tag = "" if attempts == 1 else f"（候选 {k}/{attempts}）"
            say(f"已提交，正在生成{tag} ...")
            raw_rel, secs, pid = submit_and_wait(builder(), say)
            J["prompt_id"] = pid
            rel = raw_rel
            if task == "free" and upscale_to:
                up = upscale_image(raw_rel, upscale_to)
                if up:
                    say(f"已用 LANCZOS 放大到 {upscale_to}（省显存出图法）")
                    rel = up
            # 「多图合成一个镜头」的可选超分：RealESRGAN ×4 再收到 2 倍宽（真超分，不是插值）
            if task == "fuse" and (params.get("fuse_up") or "").strip().lower() in ("1", "true", "on"):
                say("正在超分（RealESRGAN ×4）…")
                _fup = esrgan_upscale(rel, say, target_w=min(3200, int(w) * 2))
                if _fup:
                    rel = _fup
            # 透明背景（照同类软件那个开关的用途）：同类软件写的是"原生 RGBA"，我们这条是出图后本机抠图，
            # 所以界面上不许写"原生"。抠失败就保留原图并在日志说清楚，绝不静默交一张透明空图。
            _rgba = None
            if task == "free" and (params.get("transparent") or "").strip().lower() in ("1", "true", "on"):
                try:
                    _rgba, _rsec, _rfg = rgba_from_output(rel, say)
                    say(f"透明背景：已抠成 RGBA PNG（前景 {_rfg * 100:.0f}% · 抠图 {_rsec:.0f} 秒）"
                        f"→ {os.path.basename(_rgba)}")
                    rel = _rgba
                except Exception as e:
                    say(f"透明背景这一步没成（{type(e).__name__}: {str(e)[:120]}）"
                        f"——这张仍按不透明存，不拿空图糊弄你")
            # 一个候选可以产出多个"变体"（原始生成 / 各种贴回方案），最后谁分高用谁
            variants = [("原始生成", raw_rel)]
            if rel != raw_rel:
                variants.append(("放大版", rel))
            # 姿势跟衣服走：只采用头部区域（换脸/换发型），服装/姿势/背景保留服装图原像素
            if task == "vton" and vton_mode == "model_pose" and files.get("garment"):
                try:
                    base_img = cv2.imdecode(np.fromfile(os.path.join(INPUT_DIR, files["garment"]), dtype=np.uint8), cv2.IMREAD_COLOR)
                    gen_img = cv2.imdecode(np.fromfile(os.path.join(OUTPUT_DIR, rel), dtype=np.uint8), cv2.IMREAD_COLOR)
                    if base_img is not None and gen_img is not None:
                        m, mnote = face_head_mask(base_img)
                        if m is not None and _composite is not None:
                            out_img = _composite(base_img, gen_img, m)
                            safe_rel = f"QwenEdit/panel_pose_{int(time.time())}.png"
                            cv2.imwrite(os.path.join(OUTPUT_DIR, safe_rel), out_img)
                            rel = safe_rel
                            variants.append(("脸贴回·服装图原像素", safe_rel))
                            say(f"只采用头部（换脸），衣服/姿势/背景用服装图原像素 → {mnote}")
                        else:
                            say(f"服装图头部遮罩不可用：{mnote}")
                except Exception as e:
                    say(f"贴回失败({type(e).__name__})，改用整幅结果")
            # 大幅换装：整幅重绘后，把脸/手/脚按原像素贴回（必要时再用换脸模型校一次脸）
            if task == "vton" and vton_mode == "whole_protect" and person_path and _protect_mask is not None:
                try:
                    src_img = cv2.imdecode(np.fromfile(person_path, dtype=np.uint8), cv2.IMREAD_COLOR)
                    gen_img = cv2.imdecode(np.fromfile(os.path.join(OUTPUT_DIR, rel), dtype=np.uint8), cv2.IMREAD_COLOR)
                    if src_img is not None and gen_img is not None:
                        m, mnote = _protect_mask(src_img)
                        g = gen_img if gen_img.shape[:2] == src_img.shape[:2] else cv2.resize(
                            gen_img, (src_img.shape[1], src_img.shape[0]), interpolation=cv2.INTER_LANCZOS4)
                        out_img = _composite(g, src_img, m)            # 遮罩内用原图（脸/手/脚）
                        prot_rel = f"QwenEdit/panel_prot_{int(time.time())}.png"
                        cv2.imwrite(os.path.join(OUTPUT_DIR, prot_rel), out_img)
                        variants.append(("贴回脸手脚", prot_rel))
                        rel = prot_rel
                        tmp_name = f"_prot_{int(time.time())}.png"
                        ok, buf = cv2.imencode(".png", out_img)
                        if ok:
                            buf.tofile(os.path.join(INPUT_DIR, tmp_name))
                        try:
                            rel2, msg2 = faceswap_onto(tmp_name, files["person"])
                            rel = rel2
                            variants.append(("贴回+换脸校正", rel2))
                            say(f"整幅重绘 + 脸/手/脚贴回（{mnote}）；再用换脸模型校正脸：{msg2}")
                        except Exception as e:
                            say(f"整幅重绘 + 脸/手/脚贴回（{mnote}）；换脸校正跳过({type(e).__name__})")
                        finally:
                            try:
                                os.remove(os.path.join(INPUT_DIR, tmp_name))
                            except OSError:
                                pass
                except Exception as e:
                    say(f"贴回失败({type(e).__name__}: {e})，改用整幅结果")
            # 保脸保手保脚：只保留遮罩内的改动，其余一律用原图像素
            if task == "vton" and vton_mode == "safe" and person_path and _clothing_mask is not None:
                try:
                    src_img = cv2.imdecode(np.fromfile(person_path, dtype=np.uint8), cv2.IMREAD_COLOR)
                    gen_img = cv2.imdecode(np.fromfile(os.path.join(OUTPUT_DIR, rel), dtype=np.uint8), cv2.IMREAD_COLOR)
                    if src_img is not None and gen_img is not None:
                        m, mnote = _clothing_mask(src_img, gen=gen_img)
                        if m is not None:
                            out_img = _composite(src_img, gen_img, m)
                            safe_rel = f"QwenEdit/panel_safe_{int(time.time())}.png"
                            cv2.imwrite(os.path.join(OUTPUT_DIR, safe_rel), out_img)
                            rel = safe_rel
                            variants.append(("只保留衣服改动", safe_rel))
                            say(f"已把脸/手/脚/背景贴回原图 → {mnote}")
                except Exception as e:
                    say(f"保手脚贴回失败({type(e).__name__})，改用整幅结果")
            score = 1.0
            sim = 1.0
            if task in ("vton", "hair") and person_path:
                # 逐个变体打分：脸像不像、要改的地方改没改、图是不是废图
                # 换衣看「衣服改动」，换发型看「头发改动」（换发型时衣服本来就该不变）
                is_hair = (task == "hair")
                metric = "头发改动" if is_hair else "衣服改动"
                best = None
                for vname, vrel in variants:
                    out_abs = os.path.join(OUTPUT_DIR, vrel)
                    if not os.path.exists(out_abs):
                        continue
                    vscore = (hair_change_score(out_abs, person_path) if is_hair
                              else change_score(out_abs, person_path))
                    vsim, vnface = face_sim(out_abs, person_path)
                    vgsim = garment_sim(out_abs, os.path.join(INPUT_DIR, garment)) if garment else 0.0
                    vsharp = sharpness(out_abs)
                    degen = (vnface != 1) or (vsharp < 12.0)
                    # 废图（纯色/无脸/糊）直接判死；清晰真图 + 脸没换人 + 该改的地方确实动过 = 合格
                    vok = (not degen) and (vsim >= 0.42) and (vscore >= 0.08)
                    say(f"  变体[{vname}] 脸 {vsim:.2f} ｜ {metric} {vscore:.2f} ｜ 服色 {vgsim:.2f} ｜ "
                        f"清晰度 {vsharp:.0f} → {'✅ 合格' if vok else ('❌ 废图' if degen else '❌ 不合格')}")
                    rank = ((1 if vok else 0),
                            vsim * 0.55 + vscore * 0.45 if is_hair else vsim * 0.40 + vgsim * 0.35 + vscore * 0.25)
                    if best is None or rank > best["rank"]:
                        best = {"rank": rank, "rel": vrel, "score": vscore, "sim": vsim,
                                "gsim": vgsim, "ok": vok, "name": vname}
                if best is None:
                    best = {"rank": (0, 0), "rel": raw_rel, "score": 0.0, "sim": 0.0,
                            "gsim": 0.0, "ok": False, "name": "原始生成"}
                rel, score, sim = best["rel"], best["score"], best["sim"]
                gsim, ok = best["gsim"], best["ok"]
                cands.append({"rel": rel, "secs": secs, "score": score, "sim": sim,
                              "gsim": gsim, "ok": ok, "name": best["name"], "metric": metric})
                say(f"候选 {k}: 用时 {secs}s ｜ 采用「{best['name']}」｜ 脸相似度 {sim:.2f} ｜ {metric} {score:.2f} ｜ "
                    f"与参考服色相似 {gsim:.2f} → {'✅ 合格' if ok else '❌ 不合格'}")
            else:
                say(f"完成，用时 {secs} 秒")
                _nm = "透明背景 RGBA" if _rgba else ("放大版" if rel != raw_rel else "原始生成")
                cands.append({"rel": rel, "secs": secs, "score": 1.0, "sim": 1.0, "gsim": 0.0, "ok": True,
                              "name": _nm})

        good = [c for c in cands if c["ok"]]
        J["cands"] = cands
        pool = good or cands
        if not pool:
            J.update(status="error", error="一个候选都没产出（输入图可能有问题），看上面日志")
            say("❌ 一个候选都没产出，停在这里，不再往下走")
            return
        pool.sort(key=lambda c: (c["sim"] * 0.40 + c["gsim"] * 0.35 + c["score"] * 0.25), reverse=True)
        pick = pool[0]
        rel, secs = pick["rel"], pick["secs"]
        # 有多个候选时，拼一张四宫格对比图，让你一眼挑
        if len(cands) > 1:
            paths = [os.path.join(OUTPUT_DIR, c["rel"]) for c in cands]
            if task == "free":
                labels = [f"#{i+1}" for i in range(len(cands))]
            else:
                labels = [f"#{i+1} " + ("OK " if c["ok"] else "NG ") + f"f{c['sim']:.2f} g{c['gsim']:.2f}"
                          for i, c in enumerate(cands)]
            grid_abs = os.path.join(OUTPUT_DIR, "QwenEdit", f"candidates_{int(time.time())}.jpg")
            g = make_grid(paths, labels, grid_abs, cols=min(4, len(paths)))
            if g:
                say(f"候选对比图 -> {os.path.relpath(g, OUTPUT_DIR)}")
        J.update(status="done", result=f"/file?p={urllib.parse.quote(rel)}",
                 result_name=os.path.basename(rel), seconds=secs,
                 note=(f"合格 {len(good)}/{len(cands)} 张；已挑最好的一张（脸 {pick['sim']:.2f} / "
                       f"{pick.get('metric', '衣服改动')} {pick['score']:.2f}）"
                       if len(cands) > 1 else ""))
        saved = copy_to_save_dir(rel)
        if saved:
            say(f"已另存一份 -> {saved}")
        say(f"完成 -> {os.path.basename(rel)}（合格 {len(good)}/{len(cands)}）")
    except Exception as exc:
        import traceback
        tb = traceback.format_exc().strip().splitlines()
        tail = " ← ".join(l.strip() for l in tb[-5:] if l.strip())
        J.update(status="error", error=f"{type(exc).__name__}: {exc}")
        say(f"出错：{exc}")
        say(f"出错位置：{tail}")
        print(tb, flush=True)
    finally:
        if run_id:
            J["cands"] = J.get("cands")
            try:
                record_run_end(run_id, J)
            except Exception:
                pass
        # 任务结束一律卸模型还内存，避免常驻把整机压死
        try:
            comfy_post("/free", {"unload_models": True, "free_memory": True})
            LAST_FAMILY["v"] = None
            say("已释放显存/内存")
        except Exception:
            pass


# ---------------- HTTP ----------------
# （原「旧面板」那一整套 HTML 界面已按用户要求整块删除：界面只保留 new_ui.html 一套，
#   /old 现在跳转到 /new。需要翻历史实现请看 git 记录。）


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None, nocache=True):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or []):
            self.send_header(k, v)
        # 局域网模式：带 token 打开过一次之后，给浏览器写一枚 cookie，
        # 免得页面上每个请求都要自己拼 token（第三方复验指出：原来只认 cookie 却没人写 cookie，
        # 结果外面的人打开页面后所有 /api/* 全 403，等于用不了）。
        if getattr(self, "_set_tok_cookie", False):
            try:
                self.send_header("Set-Cookie",
                                 f"panel_token={PANEL_TOKEN}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
            except Exception:
                pass
        self.send_header("Content-Length", str(len(body)))
        if nocache:
            # 不缓存：改了面板刷新就能看到新的（否则 Chrome 会拿旧页面）
            # 摆姿编辑器的静态资源（含 38MB 人偶模型）例外，否则每次开编辑器都重下 40MB。
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
        else:
            self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _proxy_try(self):
        """把 /proxy/<主机:端口>/<路径> 原样转给白名单里的本机服务（**方法 + 请求体 + Content-Type 都带上**）。

        ⚠ 2026-10-03 修：以前这段只在 do_GET 里、而且不带方法也不带请求体 —— 等于把所有经代理的请求
        都当 GET。后果：内嵌的原生 ComfyUI 点「运行」没反应（它要 POST /prompt，降级成 GET 就被拒），
        保存工作流、拖拽上传等 POST/PUT 接口全废。现在抽成一个方法，各方法先过这里。
        返回 True = 已经处理完（调用方直接 return）。
        """
        u = urllib.parse.urlparse(self.path)
        if not u.path.startswith("/proxy/"):
            return False
        rest = u.path[len("/proxy/"):]
        hostport, _, sub = rest.partition("/")
        _body = b""
        try:
            _n = int(self.headers.get("Content-Length") or 0)
            if _n > 0:
                _body = self.rfile.read(_n)
        except Exception:
            _body = b""
        st, hs, data = proxy_fetch(hostport,
                                   "/" + sub + (("?" + u.query) if u.query else ""),
                                   method=self.command,
                                   body=(_body or None),
                                   ctype=(self.headers.get("Content-Type") or ""))
        self.send_response(st)
        sent = set()
        for k, v in hs:
            if k.lower() in sent:
                continue
            sent.add(k.lower())
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        return True

    def _same_origin(self):
        """来源校验（防 CSRF）。

        ⚠ 第三方第三轮复验指出：以前"本机回环一律放行、且完全不看 Origin/Referer" ——
        你在浏览器里打开的**任何网站**都能用表单式 POST（简单请求，不触发预检）驱动这个面板，
        例如 fetch("http://127.0.0.1:8899/api/delete_file", {method:"POST", ...})，
        响应虽被 CORS 挡住，但**副作用已经发生**。GET 侧也有 /api/lastframe、/api/reveal 这类有副作用的。

        规则：
          · 浏览器发的跨站请求一定带 Origin（写操作必带；无 referrer 策略也删不掉 Origin）→ 不是同源就拒；
          · 写操作（POST/PUT/DELETE）**必须带同源 Origin 或有效 token**（防 `<img>`/no-referrer 这类无 Origin 的绕过）；
          · curl / 本机脚本这类不带来来源信息的客户端，只做 IP 判定（保持可用）。
        """
        def host_ok(v):
            try:
                h = (urllib.parse.urlparse(v).hostname or "").lower()
            except Exception:
                return False
            if h in ("127.0.0.1", "localhost", "::1"):
                return True
            try:
                if h and h == str(lan_ip()).lower():
                    return True
            except Exception:
                pass
            return False

        org = (self.headers.get("Origin") or "").strip()
        ref = (self.headers.get("Referer") or "").strip()
        if org:
            return host_ok(org)
        if ref:
            return host_ok(ref)
        # 没有来源信息：浏览器发起的请求一定带 Origin（写操作）或 Referer（同源 GET），
        # `referrerpolicy=no-referrer` 能删掉 Referer，所以**有副作用的 GET 也要挡**（第四轮复验指出）。
        _path = ""
        try:
            _path = urllib.parse.urlparse(self.path).path
        except Exception:
            _path = ""
        if self.command in ("POST", "PUT", "DELETE", "PATCH") or _path in _SIDE_EFFECT_GET:
            # 带有效 token（局域网路径）的放行；其余一律拒绝
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if PANEL_TOKEN and (q.get("token", [""])[0] == PANEL_TOKEN
                                    or self.headers.get("X-Panel-Token") == PANEL_TOKEN):
                    return True
            except Exception:
                pass
            return False
        return True

    def _client_ok(self):
        """请求门（第三方审查建议加的那一层）。

        规则：**来自本机回环的请求永远放行**（桌面外壳、本机浏览器）；
        其它来源只有在 lan 模式下、且带了正确 token 才放行 —— token 可以放在
        `?token=`、请求头 `X-Panel-Token`，或第一次用带 token 的地址打开后由服务端写下的 cookie。
        非 lan 模式本来就不该收到外部请求，一律拒绝。
        """
        try:
            ip = self.client_address[0] if self.client_address else ""
        except Exception:
            return False          # 拿不到来源就按外部处理（fail-closed，不 fail-open）
        if ip in ("127.0.0.1", "::1", "localhost"):
            return True
        if ACCESS_MODE != "lan" or not PANEL_TOKEN:
            return False
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("token", [""])[0] == PANEL_TOKEN:
                self._set_tok_cookie = True       # 让 _send 写一次 cookie，后面请求就不用再带 token
                return True
            if self.headers.get("X-Panel-Token") == PANEL_TOKEN:
                self._set_tok_cookie = True
                return True
            if ("panel_token=" + PANEL_TOKEN) in (self.headers.get("Cookie") or ""):
                return True
        except Exception:
            return False
        return False

    def _gate(self):
        """返回 True 表示已拒绝并回过 403 了，调用方直接 return。"""
        if not self._same_origin():
            self._send(403, json.dumps(
                {"error": "请求来源不对（不是本面板自己的页面）。为了防「网页偷偷操作你本机的面板」，"
                          "跨站请求会被拒绝；如果你是在用脚本调用，请带上 Origin 或 X-Panel-Token 头。"},
                ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return True
        if self._client_ok():
            return False
        self._send(403, json.dumps(
            {"error": "这个面板默认只允许本机访问。要用局域网访问，请在设置里开启 lan 模式，"
                      "并用带 token 的地址打开（启动日志里有）。"},
            ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        return True

    def do_GET(self):
        if self._gate():
            return
        if self._proxy_try():
            return
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            # 单一入口：界面只有一套（new_ui.html）。老面板那套 86KB 的旧界面已经整块删掉，
            # 不再保留"两套界面"——功能全在新界面里，重复的东西不留。
            return self._send(302, b"", "text/plain; charset=utf-8", extra=[("Location", "/new")])
        if u.path in ("/old", "/old/"):
            # 老路径留个跳转：以前收藏过 /old 的不会 404
            return self._send(302, b"", "text/plain; charset=utf-8", extra=[("Location", "/new")])
        if u.path == "/api/status":
            jid = urllib.parse.parse_qs(u.query).get("id", [""])[0]
            j = JOBS.get(jid)
            if not j:
                return self._send(404, b'{"error":"no job"}', "application/json")
            return self._send(200, json.dumps({k: j.get(k) for k in ("status", "log", "result", "result_name",
                                                                     "seconds", "error", "text", "caption_file")},
                                              ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        if u.path == "/api/config":
            cfg = load_config()
            cfg.update({"output_dir": OUTPUT_DIR, "input_dir": INPUT_DIR, "runs_dir": RUNS_DIR,
                        "qwen_dir": os.path.join(OUTPUT_DIR, "QwenEdit"),
                        "video_dir": os.path.join(OUTPUT_DIR, "video")})
            return self._send(200, json.dumps(cfg, ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if u.path == "/dsh" or u.path == "/dsh/":
            # 以前这里是个"AI 工作台"外壳，上面一排页签（我的面板 / 同类软件 / 原生 ComfyUI）——
            # 用户要的是"整个软件就是三脚架"：外壳去掉，直接进三脚架；
            # 同类软件和原生 ComfyUI 收进三脚架底栏（右下角）里开。
            return self._send(302, b"", "text/html; charset=utf-8",
                              extra=[("Location", "/new")])
        if u.path == "/api/power":
            # ⚠ 第三方审查指出：这个接口没鉴权，lan 模式下可被同网段的人远程预约关机。
            #   现在只允许本机（回环）来源调用 —— 关机这种事不该能远程触发。
            _cip = self.client_address[0] if self.client_address else ""
            if _cip not in ("127.0.0.1", "::1", "localhost"):
                return self._send(403, json.dumps(
                    {"ok": False, "error": "关机预约只允许在本机操作"}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8")
            act = (urllib.parse.parse_qs(u.query).get("action", [""])[0] or "").strip()
            if act == "dry":
                POWER.update(arm=True, dry=True, fired=False, t=time.time(),
                             note="干跑已布防：任务全部跑完后只记日志，不真关机")
            elif act == "arm":
                busy = [j for j in JOBS.values() if j.get("status") in ("queued", "running")]
                POWER.update(arm=True, dry=False, fired=False, t=time.time(), cmd=" ".join(POWER_CMD),
                             note="已预约：全部任务完成后关机（60 秒倒计时，可取消）"
                                  + (f"；当前还有 {len(busy)} 个任务" if busy else "；当前队列是空的"))
            elif act == "off":
                # 取消：先撤预约，再让 Windows 撤掉已排队的关机（没有排队时 shutdown /a 只报错 1116，无害）
                r = run_hidden([SHUTDOWN_EXE, "/a"], capture_output=True)
                err = (r.stderr or b"").decode("utf-8", "replace").strip()
                canceled = r.returncode == 0
                POWER.update(arm=False, dry=True, fired=False,
                             note=("已取消关机预约" + ("（已撤掉 Windows 侧的关机，取消：shutdown /a）" if canceled
                                                       else f"；Windows 侧无待撤关机（{err[:60] or '返回码 ' + str(r.returncode)}）")))
            return self._send(200, json.dumps({"ok": True, "action": act or "status", **POWER},
                                              ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if u.path == "/api/pose/save" or (u.path == "/api/pose/save"):
            qs = urllib.parse.parse_qs(u.query)
            nm = (qs.get("name", [""])[0] or "").strip()
            if not nm:
                return self._send(400, b'{"ok":false,"error":"\xe7\xbc\xba name"}', "application/json")
            # ⚠ 第五轮复验指出：这里原来直接拼 POSE_LIB → 传 `..\..\x\evil.json` 可写任意 .json。
            nm = safe_pose_name(nm)
            _pose_target = resolve_pose_path(nm, must_exist=False)
            if not nm or not _pose_target:
                return self._send(400, json.dumps(
                    {"ok": False, "error": "姿势名不合法（只能是文件名，会自动补 .json）"},
                    ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            try:
                os.makedirs(POSE_LIB, exist_ok=True)
                body = None
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    body = self.rfile.read(n).decode("utf-8", "replace")
                if body and body.strip():
                    obj = json.loads(body)          # 校验是合法 JSON 再落盘
                    if isinstance(obj, dict) and "pose" in obj:
                        data = obj                  # 已经是完整姿势文件
                    else:
                        # 传的是 {骨骼: [x,y,z]} 的角度改动 → 合并进基准姿势
                        base = pose_base_json()
                        bones = ((base.get("pose") or {}).get("bones") or {})
                        changed = 0
                        for k, v in (obj or {}).items():
                            if k in bones and isinstance(v, (list, tuple)) and len(v) >= 3:
                                bones[k] = [float(v[0]), float(v[1]), float(v[2])]
                                changed += 1
                        data = base
                        print(f"[姿势] {nm}：改了 {changed} 根骨骼", flush=True)
                    with open(_pose_target, "w", encoding="utf-8") as fh:
                        json.dump(data, fh, ensure_ascii=False)
                else:                          # 没带内容：以基准姿势为该名字建一份
                    b = pose_base_json()
                    with open(_pose_target, "w", encoding="utf-8") as fh:
                        json.dump(b, fh, ensure_ascii=False)
                return self._send(200, json.dumps({"ok": True, "name": nm}, ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                                                  ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
        if u.path == "/api/pose/list":
            try:
                os.makedirs(POSE_LIB, exist_ok=True)
                names = sorted(f[:-5] for f in os.listdir(POSE_LIB) if f.endswith(".json"))
            except OSError:
                names = []
            try:
                bones = pose_bone_names()
            except Exception:
                bones = []
            return self._send(200, json.dumps({"ok": True, "poses": names, "bones": bones},
                                              ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if u.path == "/api/pose/get":
            # 给三维编辑器回填用：把姿势库里某个姿势的原文取出来
            nm = urllib.parse.parse_qs(u.query).get("name", [""])[0].strip()
            fp = resolve_pose_path(nm)          # 唯一入口（不许穿越，只认姿势库目录）
            if not nm or not fp or not os.path.isfile(fp):
                return self._send(404, b'{"ok":false,"error":"no pose"}', "application/json")
            try:
                with open(fp, encoding="utf-8") as fh:
                    return self._send(200, fh.read().encode("utf-8"), "application/json; charset=utf-8")
            except OSError as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)},
                                                  ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
        if u.path in ("/logo.png", "/favicon.ico", "/logo256.png", "/brand.png"):
            fn = {"/logo256.png": "logo_256.png", "/brand.png": "brand_lockup.png"}.get(u.path, "logo_128.png")
            try:
                with open(os.path.join(BASE_DIR, fn), "rb") as fh:
                    return self._send(200, fh.read(), "image/png")
            except OSError:
                return self._send(404, b"no logo", "text/plain; charset=utf-8")
        if u.path in ("/new", "/new/"):
            # 新界面（同类软件式布局：一级/二级分组 + 下面「已完成」一栏 + 接着改）
            try:
                with open(os.path.join(BASE_DIR, "new_ui.html"), "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            except OSError as e:
                return self._send(500, f"读不到 new_ui.html：{e}".encode("utf-8"),
                                  "text/plain; charset=utf-8")
        if self._proxy_try():          # /proxy/<主机:端口>/<路径> 走通用转发（GET/POST/PUT/DELETE 都过这里）
            return
        if u.path == "/api/sysmem":
            # 运行内存（底栏照同类软件显示 "28.5 / 32 GB"）。用 ctypes 读，不加依赖。
            try:
                import ctypes

                class _MEM(ctypes.Structure):
                    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
                m = _MEM()
                m.dwLength = ctypes.sizeof(_MEM)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
                total = float(m.ullTotalPhys) / 1073741824.0
                used = (float(m.ullTotalPhys) - float(m.ullAvailPhys)) / 1073741824.0
                return self._send(200, json.dumps({"total": round(total, 1), "used": round(used, 1),
                                                   "percent": int(m.dwMemoryLoad)},
                                                  ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
        if u.path == "/api/comfy_state":
            return self._send(200, json.dumps({"up": bool(port_open()), "port": COMFY_PORT},
                                              ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if u.path == "/api/comfy_start":
            # 面板以前只在"跑任务时"才拉 ComfyUI，重启面板后它就一直是死的，模型管理直接 500。
            # 现在给一个一键启动（模型管理页用）。
            up = port_open()
            if not up:
                threading.Thread(target=ensure_comfy, daemon=True).start()
                for _ in range(120):
                    time.sleep(1)
                    if port_open():
                        up = True
                        break
            return self._send(200, json.dumps({"ok": bool(up), "up": bool(port_open()), "port": COMFY_PORT},
                                              ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if u.path == "/api/models":
            qs = urllib.parse.parse_qs(u.query)
            try:
                folders = comfy_get("/models") or []
            except Exception as e:
                return self._send(500, json.dumps({"error": f"ComfyUI 没在跑（{type(e).__name__}）"},
                                                  ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            folder = qs.get("folder", ["loras"])[0]
            if folder not in folders:
                folder = "loras" if "loras" in folders else (folders[0] if folders else "")
            data = model_list(folder, qs.get("q", [""])[0])
            data["folders"] = [{"key": f, "cn": MODEL_TYPE_CN.get(f, f)} for f in folders]
            return self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if u.path == "/api/models/hash":
            qs = urllib.parse.parse_qs(u.query)
            p = model_find(qs.get("folder", [""])[0], qs.get("name", [""])[0])
            if not p:
                return self._send(404, json.dumps({"error": "定位不到这个模型文件"},
                                                  ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            try:
                sha, secs = model_sha256(p)
            except Exception as e:
                return self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"},
                                                  ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            return self._send(200, json.dumps({"ok": True, "name": os.path.basename(p),
                                               "sha256": sha, "seconds": secs,
                                               "size": os.path.getsize(p), "path": p},
                                              ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if u.path == "/api/models/reveal":
            qs = urllib.parse.parse_qs(u.query)
            p = model_find(qs.get("folder", [""])[0], qs.get("name", [""])[0])
            if not p:
                return self._send(404, b'{"error":"\xe6\x89\xbe\xe4\xb8\x8d\xe5\x88\xb0"}',
                                  "application/json")
            try:
                popen_gui(["explorer", "/select,", os.path.normpath(p)], close_fds=True)
                return self._send(200, b'{"ok":true}', "application/json")
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
        if u.path == "/api/settings":
            cfg = load_config()
            disks = {}
            for d in _disk_list():
                try:
                    t, u2, f = shutil.disk_usage(d)
                    disks[d] = {"total": t, "free": f}
                except Exception:
                    pass
            comfy_up, gpu = False, ""
            try:
                sysd = comfy_get("/system_stats")
                dev = (sysd.get("devices") or [{}])[0]
                comfy_up = True
                gpu = f"{dev.get('name', '')} · 显存 {(dev.get('vram_total', 0) - dev.get('vram_free', 0)) / 1073741824:.1f}/{(dev.get('vram_total', 0) / 1073741824):.0f} GB"
            except Exception:
                pass
            return self._send(200, json.dumps({
                "panel_url": f"http://127.0.0.1:{PORT}/", "new_url": f"http://127.0.0.1:{PORT}/new",
                "comfy_url": COMFY + "/", "comfy_up": comfy_up, "gpu": gpu,
                "comfy_dir": COMFY_DIR, "comfy_py": COMFY_PY, "extra_paths": EXTRA_PATHS,
                "input_dir": INPUT_DIR, "output_dir": OUTPUT_DIR, "runs_dir": RUNS_DIR,
                "models_roots": MODEL_ROOTS, "disks": disks, "config": cfg,
                "access_mode": ACCESS_MODE, "host": HOST, "comfy_port": COMFY_PORT,
                "open_comfy": OPEN_COMFY, "lan_url": lan_url(), "lan_ip": lan_ip(),
            }, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        if u.path == "/api/files":
            qs = urllib.parse.parse_qs(u.query)
            which = qs.get("which", ["output"])[0]
            try:
                limit = max(1, min(200, int(qs.get("limit", ["48"])[0])))
                offset = max(0, int(qs.get("offset", ["0"])[0]))
            except ValueError:
                limit, offset = 48, 0
            q = qs.get("q", [""])[0]
            sort = qs.get("sort", ["time"])[0]
            hid = qs.get("hidden", ["0"])[0] in ("1", "true", "yes")   # 1＝只看"临时移掉"的那些
            return self._send(200, json.dumps(list_files(which, limit, offset, q, sort, hid),
                                              ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if u.path == "/api/ui_ver":
            # 界面文件（new_ui.html）的版本戳：页面每几秒问一次，变了就自己刷新 ——
            # 用户原话"你网页自己开"：我改完界面不该让他按 F5。
            try:
                st = os.stat(os.path.join(BASE_DIR, "new_ui.html"))
                ver = "%d-%d" % (int(st.st_mtime_ns), st.st_size)
            except OSError:
                ver = "0-0"
            return self._send(200, json.dumps({"v": ver}).encode("utf-8"),
                              "application/json; charset=utf-8", nocache=True)
        if u.path == "/api/models_ver":
            # 模型目录指纹：新装了模型/删了模型 → 前端据此**自动重载**内嵌的 ComfyUI。
            # 因为 ComfyUI 前端只在页面加载时读一次模型清单，不重载就会一直显示"缺失模型"——
            # 2026-10-03 用户踩过：模型装好了，界面还说不存在，得手动 Ctrl+F5。
            try:
                return self._send(200, json.dumps({"v": models_fingerprint()}).encode("utf-8"),
                                  "application/json; charset=utf-8", nocache=True)
            except Exception as e:
                return self._send(200, json.dumps({"v": "", "error": str(e)}).encode("utf-8"),
                                  "application/json; charset=utf-8", nocache=True)
        if u.path == "/api/model_info":
            # 「模型与 LoRA」弹窗用：把当前档位真实对应的主模型/文本编码器/VAE 报给前端，
            # 别让界面瞎写"跟随模型预设"这种空话。
            qs = urllib.parse.parse_qs(u.query)
            t = (qs.get("tier", [""])[0] or TIER.get("qwen") or "auto").strip().lower()
            cur = t if t in QWEN_TIERS else ("int8" if t in ("auto", "", "default") else t)
            info = {"tier": t, "resolved": cur,
                    "unet": QWEN_TIERS.get(cur, {}).get("unet", ""),
                    "clip": QWEN_CLIP, "vae": QWEN_VAE,
                    "loader": "自动识别（按模型文件选加载器）",
                    "tiers": [{"value": k, "label": v.get("label", k), "unet": v.get("unet", "")}
                              for k, v in QWEN_TIERS.items()]}
            return self._send(200, json.dumps(info, ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if u.path == "/thumb":
            # 缩略图：那排"最近的作品"以前直接拉原图（1~3MB × 40 张），界面会发涩。
            # 这里按需生成小图并落盘缓存（视频抓第一帧当封面）。
            qs = urllib.parse.parse_qs(u.query)
            which = qs.get("which", ["output"])[0]
            rel = urllib.parse.unquote(qs.get("p", [""])[0])
            try:
                size = max(48, min(640, int(qs.get("s", ["180"])[0])))
            except ValueError:
                size = 180
            # which=out＝直接按 output 目录算（history 里会有 QwenEdit/x、FisherPose/x、video/x 各种子目录）
            full = safe_path(OUTPUT_DIR, rel) if which == "out" else safe_path(file_root(which), rel)
            if not full or not os.path.isfile(full):
                return self._send(404, b"not found", "text/plain")
            try:
                stt = os.stat(full)
                key = hashlib.md5(("%s|%d|%d|%d" % (full, size, stt.st_mtime_ns, stt.st_size)).encode()).hexdigest()
            except OSError:
                return self._send(404, b"not found", "text/plain")
            cache_p = os.path.join(THUMB_DIR, key + ".jpg")
            if os.path.isfile(cache_p):
                try:
                    with open(cache_p, "rb") as fh:
                        return self._send(200, fh.read(), "image/jpeg", nocache=False)
                except OSError:
                    pass
            img = cv2.imdecode(np.fromfile(full, dtype=np.uint8), cv2.IMREAD_COLOR)
            if img is None and full.lower().endswith((".mp4", ".webm", ".mov", ".mkv", ".avi")):
                cap = None
                try:
                    cap = cv2.VideoCapture(full)
                    okf, frame = cap.read()
                    img = frame if okf else None
                except Exception:
                    img = None
                finally:
                    try:
                        if cap:
                            cap.release()
                    except Exception:
                        pass
            if img is None:
                return self._send(404, b"decode failed", "text/plain")
            h, w = img.shape[:2]
            sc = size / float(max(h, w))
            if sc < 1:
                img = cv2.resize(img, (max(1, int(w * sc)), max(1, int(h * sc))),
                                 interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
            if not ok:
                return self._send(500, b"encode failed", "text/plain")
            data = buf.tobytes()
            try:
                os.makedirs(THUMB_DIR, exist_ok=True)
                with open(cache_p, "wb") as fh:
                    fh.write(data)
            except OSError:
                pass
            return self._send(200, data, "image/jpeg", nocache=False)
        if u.path == "/raw":
            qs = urllib.parse.parse_qs(u.query)
            which = qs.get("which", ["output"])[0]
            rel = urllib.parse.unquote(qs.get("p", [""])[0])
            full = safe_path(file_root(which), rel)
            if not full or not os.path.isfile(full):
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as fh:
                return self._send(200, fh.read(), ctype)
        if u.path == "/api/reveal":
            qs = urllib.parse.parse_qs(u.query)
            which = qs.get("which", ["output"])[0]
            rel = urllib.parse.unquote(qs.get("p", [""])[0])
            full = safe_path(file_root(which), rel)
            if not full or not os.path.exists(full):
                return self._send(404, b'{"error":"no file"}', "application/json")
            try:
                popen_gui(["explorer", "/select,", os.path.normpath(full)], close_fds=True)
                return self._send(200, b'{"ok":true}', "application/json")
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
        if u.path == "/api/open":
            which = urllib.parse.parse_qs(u.query).get("which", ["output"])[0]
            cfg = load_config()
            if which == "comfyui":
                try:
                    os.startfile("http://127.0.0.1:8188")
                    return self._send(200, b'{"ok":true}', "application/json")
                except Exception as e:
                    return self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                                      "application/json; charset=utf-8")
            targets = {"output": os.path.join(OUTPUT_DIR, "QwenEdit"), "video": os.path.join(OUTPUT_DIR, "video"),
                       "input": INPUT_DIR, "runs": RUNS_DIR, "captions": CAPTIONS_DIR,
                       # models 不能写死 [2]：默认配置只有一个模型根，越界会 IndexError 把连接掐断
                       # （第七轮复验指出的功能性缺陷）
                       "models": (MODEL_ROOTS[2] if len(MODEL_ROOTS) > 2 else (MODEL_ROOTS[0] if MODEL_ROOTS else "")),
                       # save 走 file_root()：它会复检 check_save_dir，手改配置塞敏感目录也不会被打开
                       "save": file_root("save")}
            path = targets.get(which, "")
            if not path:
                return self._send(400, b'{"error":"no path"}', "application/json")
            try:
                how, hit = open_folder(path)
                return self._send(200, json.dumps({"ok": True, "path": os.path.normpath(path),
                                                   "how": how, "focused": hit},
                                                  ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            except Exception as e:
                return self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"},
                                                  ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
        if u.path == "/api/lastframe":
            qs = urllib.parse.parse_qs(u.query)
            rel = urllib.parse.unquote(qs.get("p", [""])[0])
            # ⚠ 第三方复验指出：这里原来直接把 rel 交给 ffmpeg（绝对路径也照用），
            #   等于给了"读任意视频 + 把末帧写进 input 再取回"的能力。现在只允许 OUTPUT_DIR 里的相对路径。
            _lv = safe_path(OUTPUT_DIR, rel)
            if not _lv or not os.path.isfile(_lv):
                return self._send(404, json.dumps({"error": "找不到这个视频文件"}, ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            rel = os.path.relpath(_lv, OUTPUT_DIR)
            try:
                name = extract_last_frame(rel)
                return self._send(200, json.dumps({"ok": True, "image": name, "video": rel},
                                                  ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
        if u.path == "/api/loras":
            return self._send(200, json.dumps({"loras": list_loras()}, ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if u.path == "/api/preview":
            # 采样过程中的最新一帧：有图就回图（步数放在响应头里），没图回 204 让界面自己收起来
            with PREVIEW_LOCK:
                snap = dict(PREVIEW)
            age = time.time() - (snap.get("t") or 0)
            if snap.get("png") and age < 90:
                ctype = "image/jpeg" if snap.get("kind") == "jpg" else "image/png"
                return self._send(200, snap["png"], ctype, extra=[
                    ("X-Step", str(snap.get("step") or 0)),
                    ("X-Max", str(snap.get("max") or 0)),
                    ("X-Running", "1" if snap.get("running") else "0"),
                    ("X-Age", f"{age:.1f}")], nocache=True)
            return self._send(204, b"", "image/png", extra=[
                ("X-Step", str(snap.get("step") or 0)),
                ("X-Max", str(snap.get("max") or 0)),
                ("X-Running", "1" if snap.get("running") else "0"),
                ("X-Age", f"{age:.1f}")])
        if u.path == "/api/queue":
            with SEQ_LOCK:
                items = sorted(JOBS.items(), key=lambda kv: kv[1].get("seq", 0), reverse=True)[:40]
            out = []
            for k, j in items:
                out.append({"id": k, "seq": j.get("seq"), "task": j.get("task"),
                            "task_name": j.get("task_name") or j.get("task"),
                            "status": j.get("status"), "result_name": j.get("result_name"),
                            "seconds": j.get("seconds"),
                            "waited": int(time.time() - (j.get("created") or time.time())),
                            "position": queue_position(k) if j.get("status") == "queued" else 0,
                            "log": (j.get("log") or [])[-3:]})
            running = [x for x in out if x["status"] == "running"]
            queued = [x for x in out if x["status"] == "queued"]
            # 「任务队列」抽屉底部要显示关机预约状态（照同类软件那条"完成后关机 · 未开启"）
            pw = {"arm": bool(POWER.get("arm")), "dry": bool(POWER.get("dry")),
                  "note": POWER.get("note") or ""}
            return self._send(200, json.dumps({"jobs": out, "running": len(running), "queued": len(queued),
                                               "power": pw},
                                              ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if u.path == "/api/gpu":
            info = {"vram_used_gb": None, "vram_total_gb": None, "device": None,
                    "queue_running": 0, "loaded": [], "ram_used_gb": None, "ram_total_gb": None}
            try:                                    # 内存用系统 API 读，不依赖第三方库
                import ctypes

                class _MS(ctypes.Structure):
                    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

                ms = _MS()
                ms.dwLength = ctypes.sizeof(_MS)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
                info["ram_total_gb"] = round(ms.ullTotalPhys / 1073741824, 1)
                info["ram_used_gb"] = round((ms.ullTotalPhys - ms.ullAvailPhys) / 1073741824, 1)
            except Exception:
                pass
            try:
                st = json.loads(urllib.request.urlopen(COMFY + "/system_stats", timeout=8).read().decode())
                dev = (st.get("devices") or [{}])[0]
                info["vram_total_gb"] = round((dev.get("vram_total") or 0) / 1073741824, 1)
                info["vram_used_gb"] = round(((dev.get("vram_total") or 0) - (dev.get("vram_free") or 0)) / 1073741824, 1)
                info["device"] = dev.get("name")
                q = json.loads(urllib.request.urlopen(COMFY + "/queue", timeout=8).read().decode())
                info["queue_running"] = len(q.get("queue_running") or [])
            except Exception as e:
                info["error"] = f"{type(e).__name__}"
            return self._send(200, json.dumps(info, ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if u.path == "/api/history":
            qs = urllib.parse.parse_qs(u.query)
            try:
                lim = max(1, min(2000, int(qs.get("limit", ["300"])[0])))
            except ValueError:
                lim = 300
            return self._send(200, json.dumps(list_runs(lim), ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if u.path == "/api/run":
            rid = urllib.parse.parse_qs(u.query).get("id", [""])[0]
            d = run_dir_of(rid)
            p = os.path.join(d, "meta.json") if d else ""
            if not p or not os.path.exists(p):
                return self._send(404, b'{"error":"no run"}', "application/json")
            with open(p, "rb") as fh:
                return self._send(200, fh.read(), "application/json; charset=utf-8")
        if u.path == "/infile":          # 历史记录里看输入图（input 目录）
            rel = urllib.parse.unquote(urllib.parse.parse_qs(u.query).get("p", [""])[0])
            full = safe_path(INPUT_DIR, rel)
            if not full or not os.path.exists(full):
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as fh:
                return self._send(200, fh.read(), ctype)
        if u.path == "/file":
            rel = urllib.parse.unquote(urllib.parse.parse_qs(u.query).get("p", [""])[0])
            full = safe_path(OUTPUT_DIR, rel)
            if not full or not os.path.exists(full):
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as fh:
                data = fh.read()
            return self._send(200, data, ctype)
        if u.path.startswith("/pose-edit/"):
            # 三维摆姿编辑器的静态文件（Fisher-Pose 原版，没改过它一行代码）
            # /pose-edit/_panel/… 是面板自己那一侧的小配件（自测页等），和节点目录分开，免得污染第三方节点。
            rel = urllib.parse.unquote(u.path[len("/pose-edit/"):]).replace("/", os.sep)
            if rel.startswith("_panel" + os.sep) or rel == "_panel":
                full = safe_path(POSE_EXTRA, rel[len("_panel" + os.sep):])
            else:
                full = safe_path(FISHER_WEB, rel)
            if not full or not os.path.isfile(full):
                return self._send(404, b"not found", "text/plain")
            ext = os.path.splitext(full)[1].lower()
            ctype = MIME_OVERRIDE.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream"
            try:
                with open(full, "rb") as fh:
                    data = fh.read()
            except OSError:
                return self._send(404, b"not found", "text/plain")
            return self._send(200, data, ctype, nocache=False)
        return self._send(404, b"404", "text/plain")

    def do_PUT(self):
        # 内嵌 ComfyUI 会用到 PUT（写 userdata 等）；不经过代理的 PUT 一律 405，不再返回 501
        if self._gate():
            return
        if self._proxy_try():
            return
        self._send(405, b'{"error":"method not allowed"}', "application/json")

    def do_DELETE(self):
        if self._gate():
            return
        if self._proxy_try():
            return
        self._send(405, b'{"error":"method not allowed"}', "application/json")

    def do_POST(self):
        if self._gate():
            return
        if self._proxy_try():          # 内嵌原生 ComfyUI 的 POST（点运行、保存工作流…）从这儿走
            return
        path = urllib.parse.urlparse(self.path).path
        if path in ("/api/pose/save"):
            return self.do_GET()           # 这几个接口 GET/POST 同一套逻辑
        if path == "/api/delete_run":
            n = int(self.headers.get("Content-Length", 0))
            rid = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace")).get("id", [""])[0]
            d = run_dir_of(rid)
            if d and os.path.isdir(d) and os.path.normpath(d).startswith(os.path.normpath(RUNS_DIR)):
                import shutil
                shutil.rmtree(d, ignore_errors=True)
                return self._send(200, b'{"ok":true}', "application/json")
            return self._send(404, b'{"error":"no run"}', "application/json")
        if path == "/api/rerun":
            n = int(self.headers.get("Content-Length", 0))
            rid = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace")).get("id", [""])[0]
            d = run_dir_of(rid)
            meta_path = os.path.join(d, "meta.json") if d else ""
            if not meta_path or not os.path.exists(meta_path):
                return self._send(404, json.dumps({"error": "找不到这条记录"}, ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
            files = meta.get("inputs") or {}
            task = meta.get("task") or "vton"
            params = dict(meta.get("params") or {})
            params["task"] = task
            gone = [k for k, v in files.items() if not os.path.exists(os.path.join(INPUT_DIR, v))]
            if gone or not files:
                return self._send(400, json.dumps(
                    {"error": "这条记录的输入图已经不在了，请重新选图"}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8")
            jid = uuid.uuid4().hex
            register_job(jid, "从历史记录重跑", task)
            launch_job(jid, task, files, params)
            return self._send(200, json.dumps({"id": jid, "task": task, "seq": JOBS[jid]["seq"]},
                                              ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if path == "/api/comfy_start":
            # 模型管理 / 前端按钮都可能用 POST 调它（原来只挂在 GET 上 → 404）
            up = port_open()
            if not up:
                threading.Thread(target=ensure_comfy, daemon=True).start()
                for _ in range(180):
                    time.sleep(1)
                    if port_open():
                        up = True
                        break
            return self._send(200, json.dumps({"ok": bool(up), "up": bool(port_open()), "port": COMFY_PORT},
                                              ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if path == "/api/polish":
            # 语音输入的文字先让本地大模型整理一遍再填回输入框（去口语、纠错别字、补标点、理顺语序）
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
            text = ""
            try:
                text = (json.loads(raw or "{}") or {}).get("text") or ""
            except Exception:
                text = urllib.parse.parse_qs(raw).get("text", [""])[0]
            text = (text or "").strip()
            if not text:
                return self._send(400, json.dumps({"error": "没有要整理的文字"}, ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            if len(text) > 16000:
                return self._send(400, json.dumps({"error": "文字太长（>16000 字），先精简"}, ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            try:
                out, secs = polish_text(text)
                if not out:
                    return self._send(500, json.dumps({"error": "模型没给出内容，再试一次"},
                                                      ensure_ascii=False).encode("utf-8"),
                                      "application/json; charset=utf-8")
                return self._send(200, json.dumps({"ok": True, "text": out, "seconds": round(secs, 1)},
                                                  ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            except Exception as e:
                return self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"},
                                                  ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
        if path == "/api/vpost":
            n = int(self.headers.get("Content-Length", 0))
            try:
                opt = json.loads(self.rfile.read(n).decode("utf-8", "replace") or "{}")
            except Exception:
                opt = {}
            try:
                out, info = video_post(opt)
                return self._send(200, json.dumps({"ok": True, "out": out, "info": info},
                                                  ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            except Exception as e:
                return self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"},
                                                  ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
        if path == "/api/cancel":
            n = int(self.headers.get("Content-Length", 0))
            body = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            jid = body.get("id", [""])[0]
            msg = cancel_job(jid)
            return self._send(200, json.dumps({"ok": True, "msg": msg}, ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if path == "/api/delete_file":
            n = int(self.headers.get("Content-Length", 0))
            body = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            which = body.get("which", ["output"])[0]
            rel = urllib.parse.unquote(body.get("p", [""])[0])
            root = file_root(which)
            full = safe_path(root, rel)
            if not full or not os.path.isfile(full):
                return self._send(404, b'{"error":"no file"}', "application/json")
            # 不直接抹掉：先挪进 _回收站（界面上立刻消失，误删还能一键放回）
            try:
                rid, msg = recycle_file(full)
            except OSError as e:
                return self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            set_hidden(which, full, False)      # 删掉的顺手从隐藏清单里摘掉，免得清单越攒越脏
            nn, bb = recycle_stat()
            return self._send(200, json.dumps({"ok": True, "rid": rid, "msg": msg,
                                               "recycle_count": nn, "recycle_bytes": bb},
                                              ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if path == "/api/undelete":
            n = int(self.headers.get("Content-Length", 0))
            body = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            rid = body.get("rid", [""])[0]
            back, msg = unrecycle_file(rid)
            return self._send(200 if back else 404, json.dumps({"ok": bool(back), "msg": msg},
                                                               ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if path == "/api/recycle_purge":
            n, b, err = recycle_purge()
            return self._send(200, json.dumps({"ok": not err, "removed": n, "bytes": b, "error": err},
                                              ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if path == "/api/hide_file":
            # 临时移掉：文件一个字节都不动，只是列表里不再出现
            n = int(self.headers.get("Content-Length", 0))
            body = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            which = body.get("which", ["output"])[0]
            rel = urllib.parse.unquote(body.get("p", [""])[0])
            full = safe_path(file_root(which), rel)
            if not full or not os.path.exists(full):
                return self._send(404, b'{"error":"no file"}', "application/json")
            set_hidden(which, full, True)
            return self._send(200, json.dumps({"ok": True, "name": os.path.basename(full),
                                               "hidden": len(hidden_map())},
                                              ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if path == "/api/unhide_file":
            n = int(self.headers.get("Content-Length", 0))
            body = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            which = body.get("which", ["output"])[0]
            rel = urllib.parse.unquote(body.get("p", [""])[0])
            _root = file_root(which)
            if not _root:
                # 第七轮复验：which 未知时原来会拿一个不存在的路径往隐藏清单里塞键（脏数据）
                return self._send(400, json.dumps({"ok": False, "error": "which 不对"},
                                                  ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            full = safe_path(_root, rel) or os.path.join(_root, os.path.basename(rel))
            set_hidden(which, full, False)
            return self._send(200, json.dumps({"ok": True, "name": os.path.basename(full),
                                               "hidden": len(hidden_map())},
                                              ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if path == "/api/caption_from":
            n = int(self.headers.get("Content-Length", 0))
            body = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            rel = urllib.parse.unquote(body.get("p", [""])[0]).replace("\\", "/")
            ask = (body.get("ask", [""])[0] or "").strip()
            src = safe_path(OUTPUT_DIR, rel)
            if not src or not os.path.exists(src):
                return self._send(404, json.dumps({"error": "找不到那张图"}, ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            try:
                name = f"cap_src_{int(time.time() * 1000) % 1000000}.png"
                import shutil
                shutil.copy2(src, os.path.join(INPUT_DIR, name))
            except OSError as e:
                return self._send(500, json.dumps({"error": f"复制失败: {e}"}, ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            jid = uuid.uuid4().hex
            register_job(jid, "读图出描述（来自刚生成的结果图）", "caption")
            launch_job(jid, "caption", {"person": name}, {"prompt": ask})
            return self._send(200, json.dumps({"id": jid, "task": "caption", "seq": JOBS[jid]["seq"]},
                                              ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if path == "/api/config":
            n = int(self.headers.get("Content-Length", 0))
            body = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            cfg = load_config()
            if "save_dir" in body:
                _sd = (body.get("save_dir", [""])[0] or "").strip()
                _ok, _why = check_save_dir(_sd)
                if not _ok:
                    return self._send(400, json.dumps({"ok": False, "error": _why},
                                                      ensure_ascii=False).encode("utf-8"),
                                      "application/json; charset=utf-8")
                cfg["save_dir"] = _sd
            # 访问模式 / 端口（同类软件设置页那几项）：保存后要重启面板才生效
            if "access_mode" in body:
                mode = (body.get("access_mode", ["local"])[0] or "local").strip().lower()
                if mode not in ("local", "lan"):
                    return self._send(400, json.dumps({"ok": False, "error": "访问模式必须是 local 或 lan"},
                                                      ensure_ascii=False).encode("utf-8"),
                                      "application/json; charset=utf-8")
                cfg["access_mode"] = mode
            if "comfy_port" in body:
                try:
                    p = int(body.get("comfy_port", ["8188"])[0])
                    if not (1024 <= p <= 65535):
                        raise ValueError
                except ValueError:
                    return self._send(400, json.dumps({"ok": False, "error": "ComfyUI 端口要在 1024–65535 之间"},
                                                      ensure_ascii=False).encode("utf-8"),
                                      "application/json; charset=utf-8")
                if p == PORT:
                    return self._send(400, json.dumps({"ok": False, "error": "ComfyUI 端口不能和面板端口相同"},
                                                      ensure_ascii=False).encode("utf-8"),
                                      "application/json; charset=utf-8")
                cfg["comfy_port"] = p
            if "open_comfy" in body:
                cfg["open_comfy"] = (body.get("open_comfy", ["0"])[0] or "0") in ("1", "true", "on")
            save_config(cfg)
            return self._send(200, json.dumps({"ok": True, "save_dir": cfg.get("save_dir", ""),
                                               "access_mode": cfg.get("access_mode", "local"),
                                               "comfy_port": cfg.get("comfy_port", 8188),
                                               "open_comfy": bool(cfg.get("open_comfy")),
                                               "lan_url": lan_url(),
                                               "note": "访问模式 / 端口改动要重启面板才生效"},
                                              ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
        if path == "/api/unload":
            try:
                comfy_post("/free", {"unload_models": True, "free_memory": True})
                LAST_FAMILY["v"] = None
                return self._send(200, json.dumps({"ok": True, "msg": "已卸载模型，显存已释放"},
                                                  ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            except Exception as e:
                return self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"},
                                                  ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
        if path != "/api/run":
            return self._send(404, b"404", "text/plain")
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r"boundary=(.+)$", ctype)
        if not m:
            return self._send(400, b'{"error":"bad content-type"}', "application/json")
        boundary = m.group(1).strip('"').encode()
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        fields, files = {}, {}
        for part in body.split(b"--" + boundary):
            if b"\r\n\r\n" not in part:
                continue
            head, data = part.split(b"\r\n\r\n", 1)
            data = data[:-2] if data.endswith(b"\r\n") else data
            hs = head.decode("utf-8", "replace")
            nm = re.search(r'name="([^"]+)"', hs)
            if not nm:
                continue
            name = safe_upload_name(nm.group(1))
            fn = re.search(r'filename="([^"]*)"', hs)
            if fn and fn.group(1):
                ext = os.path.splitext(fn.group(1))[1].lower() or ".png"
                if ext not in (".png", ".jpg", ".jpeg", ".webp", ".bmp",
                               ".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac"):
                    ext = ".png"
                # 同类软件的限制：单张 ≤40 MB、≤4000 万像素（超了给明确原因，别让它到模型那儿才炸）
                if len(data) > 40 * 1024 * 1024:
                    return self._send(400, json.dumps(
                        {"error": f"「{name}」这张图 {len(data) / 1048576:.1f} MB，超过 40 MB 上限，请先压缩再传"},
                        ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
                if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                    # ① 空文件：多半是 OneDrive/网盘「仅云端」的占位文件没下载到本机
                    #    （2026-10-02 用户就栽在这：OneDrive 没运行，整目录都是 OFFLINE 占位文件，
                    #     谁读都读不出来，浏览器传过来就是 0 字节）
                    if len(data) == 0:
                        return self._send(400, json.dumps(
                            {"error": f"「{name}」读到的是 0 字节，内容不在本机。"
                                      "如果这张图放在 OneDrive / 网盘里，先在资源管理器里右键「始终保留在此设备上」"
                                      "（或双击打开一次让它下载），确认能正常打开后再传。"},
                            ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
                    # ② 打不开 / 不是真图片：给明确原因，别让模型那边报怪错
                    try:
                        from PIL import Image as _PILImage
                        with _PILImage.open(_io.BytesIO(data)) as _pim:
                            _px = int(_pim.size[0]) * int(_pim.size[1])
                        if _px > 40000000:
                            return self._send(400, json.dumps(
                                {"error": f"「{name}」这张图 {_px / 1e6:.0f} 百万像素，超过 4000 万上限，请先缩一下"},
                                ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
                    except Exception as _e:
                        return self._send(400, json.dumps(
                            {"error": f"「{name}」打不开（{type(_e).__name__}）：可能没下载完整、或者不是真正的图片文件。"
                                      "如果是网盘里的文件，先把它下载到本机再传。"},
                            ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
                safe = f"panel_{int(time.time())}_{name}{ext}"
                with open(os.path.join(INPUT_DIR, safe), "wb") as fh:
                    fh.write(data)
                # 兜底：从作品栏拖小图进来时，Chrome 拖的是那张缩略图本身 —— 自动换回原图
                if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                    try:
                        _new, _msg = upgrade_small_upload(safe)
                        if _new != safe:
                            safe = _new
                            fields["_upload_note"] = _msg
                    except Exception:
                        pass
                files[name] = safe
            else:
                fields[name] = data.decode("utf-8", "replace")

        # 全局"去水印裁边"：填了 trim 就把本次所有上传的图四周各裁掉 trim%
        # （小红书那类角标/用户名/下载按钮都在四边，裁掉就干净了；
        #   图片和它的遮罩一起裁，尺寸仍然对齐）
        try:
            _tp = float(fields.get("trim") or 0)
        except (TypeError, ValueError):
            _tp = 0.0
        if _tp > 0 and files:
            _n = 0
            for _k, _v in list(files.items()):
                if _v and _v.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
                    try:
                        _nv = trim_edges(_v, _tp)
                        if _nv != _v:
                            files[_k] = _nv
                            _n += 1
                    except Exception:
                        pass
            if _n:
                print(f"[裁边] 本次有 {_n} 张输入图四周各裁掉 {_tp}%（去角标/水印）", flush=True)

        jid = uuid.uuid4().hex
        register_job(jid, "已收到图片", fields.get("task", "vton"))
        if fields.get("_upload_note"):
            try:
                JOBS[jid]["log"].append("⚠ " + fields["_upload_note"])
                print(f"[参考图] {fields['_upload_note']}", flush=True)
            except Exception:
                pass
        launch_job(jid, fields.get("task", "vton"), files, fields)
        return self._send(200, json.dumps({"id": jid, "seq": JOBS[jid]["seq"], "files": files},
                                          ensure_ascii=False).encode("utf-8"),
                          "application/json; charset=utf-8")


class PanelHTTP(ThreadingHTTPServer):
    """面板的 HTTP 服务。

    ⚠ 2026-10-03 踩过：`socketserver` 的 `request_queue_size` 默认只有 **5**。
    用户在启动失败时反复点快捷方式/启动脚本 → 大量连接重试堆上来 → 队列瞬间塞满 →
    **所有新连接立即被拒**（外面看就是"面板死了，端口连不上"），实测一共堆了 1.5 万条 TIME_WAIT。
    把队列放大 + 允许地址复用，并把每个请求交给独立线程（daemon，进程退出不被卡住）。
    """
    daemon_threads = True
    request_queue_size = 256
    allow_reuse_address = True


if __name__ == "__main__":
    os.makedirs(INPUT_DIR, exist_ok=True)
    # 已经有面板在跑就别再起一个：否则多个实例互抢 8899，最后谁都绑不上（用户看到的就是"启动不了"）
    try:
        import urllib.request as _u
        with _u.urlopen(f"http://127.0.0.1:{PORT}/api/ui_ver", timeout=2) as _r:
            if _r.status == 200:
                print(f"[panel] 已经有一个面板在 {PORT} 上跑着，本次不再重复启动", flush=True)
                # ⚠ 这里必须用 os._exit：实测 sys.exit(0) 在这条路上**没能终止进程**
                #   （打印完保护提示照样往下把面板又起了一遍，于是实例越堆越多）。
                #   os._exit 直接终止，不跑清理、也不可能被任何 except 吞掉。
                os._exit(0)
    except Exception:
        pass
    print(f"面板已启动: http://127.0.0.1:{PORT}", flush=True)
    print(f"单窗聚合页: http://127.0.0.1:{PORT}/dsh", flush=True)
    if ACCESS_MODE == "lan":
        print(f"局域网访问已开启: {lan_url()}（模式 lan · ComfyUI 直连 {'开' if OPEN_COMFY else '关'}）", flush=True)
    else:
        print("访问模式: local（只允许本机；要局域网访问就在「设置」里改成 lan 并重启）", flush=True)
    threading.Thread(target=power_watcher, daemon=True).start()
    threading.Thread(target=preview_ws_thread, daemon=True).start()
    # 面板一起来就把 ComfyUI 拉起来（以前只在跑任务时才拉，重启面板后它就一直是死的，
    # 「模型管理」直接 500、所有出图也全废 —— 踩过）
    def _boot_comfy():
        time.sleep(1.5)
        try:
            ensure_comfy()
        except Exception as e:
            print(f"[panel] 自动拉起 ComfyUI 失败：{type(e).__name__}: {e}", flush=True)
    threading.Thread(target=_boot_comfy, daemon=True).start()
    try:
        _srv = PanelHTTP((HOST, PORT), Handler)
    except OSError as e:
        print(f"[panel] 端口 {PORT} 被占用，起不来：{e}（多半已经有一个面板在跑）", flush=True)
        sys.exit(1)
    _srv.serve_forever()
