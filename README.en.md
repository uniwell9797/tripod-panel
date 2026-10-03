# Tripod Panel

> **Pick an image → type one line → hit generate.** Tripod Panel turns a local ComfyUI install into a
> foolproof, all-in-one studio: no node graphs, no workflows, no "where do I put the model".
> Text-to-image, image editing, virtual try-on, face swap, matting, outpaint, video, TTS and digital
> humans — all from one Chinese-language UI. **Everything runs on your own machine**: nothing is
> uploaded, no cloud API is called.

> ⚠️ **This repository contains code only — no model weights.** Download models yourself from their
> official sources (see [`models.en.md`](models.en.md)); each model keeps its own license.
> Please read [`DISCLAIMER.md`](DISCLAIMER.md) before use.

> 中文说明见 [`README.md`](README.md)。

---

## The starting point: why this exists

**Short version: I kept getting stuck on these problems, and the whole thing was simply too hard and too painful
— so I built a framework and named it "Tripod".**

**The problems I ran into:**

- ComfyUI is the strongest local engine for AI image/video generation, but its learning curve keeps most people
  out: **node graphs**, **workflows**, **which model goes in which folder**, **a wall of English tracebacks**,
  and **running out of VRAM**;
- People who know it produce an image in minutes; **everyone else spends a night failing to install it**;
- Commercial web services cost money and **want you to upload your photos**; other local frontends are either too
  limited or still require you to understand workflows;
- Worse, the jobs people actually want come **as a chain** (generate → try-on → matting → video → voice →
  subtitles), and each step used to mean another piece of software and another learning curve.

**So I built this framework and named it "Tripod":**

- **ComfyUI is the foundation and the materials, the models are the bricks — this frame pins down *how to build*.**
  The everyday jobs are already wired up: text-to-image, image editing, try-on / hairstyle / face swap,
  old-photo restore, matting, outpaint, multi-angle, pose editor, photo animation, talking from audio,
  first/last-frame video, video post, TTS and digital human — prompt templates and parameter tiers included,
  **ready to use without building anything from scratch**. Underneath sits a common layer: gallery, run history,
  model manager, task queue, live preview and the embedded native ComfyUI.
- **Once the frame stands, the point is to build higher.** Adding a page, swapping a model or adding a feature is a
  matter of copying an existing pattern — one `panel.py` backend, one `new_ui.html` frontend, no framework, no
  build step.

**What I hope for:**

> **Everyone who understands this and wants to use it should add to it themselves — to make this house bigger and
> better, so that everyone who needs it can use it.** How to contribute: see the
> **"Contributions welcome"** section at the end.

> In one line: **a foolproof platform wrapped around a professional engine — running entirely on your own machine.**

## Why is it called "Tripod"?

**On a building site the first thing you put up is the scaffolding.** With the frame standing, bricks can go
up course by course; when the house is finished the scaffolding comes down — but *being able to build it*
started with that frame. **This software is that frame.**

- **ComfyUI is the foundation and the materials; the models are the bricks.** They are all there, but nobody
  tells you *how to build*: how to wire nodes, what values to pass, which folder a model goes in, what to do
  when VRAM runs out. This panel pins all of that down: every feature's workflow, prompt template, parameter
  tiers, plain-language error messages and artefact cleanup **are already written**.
- **It is scaffolding for you — and for your AI agent.** You don't start from zero: adding a page, swapping a
  model or adding a feature is a matter of **copying an existing pattern** (one backend file `panel.py`, one
  frontend file `new_ui.html`, no framework, no build step — edit and refresh).
- **What you build yourself will be more complete.** Only you know what you actually need. What this framework
  gives you is "**no begging, no starting over**": every feature is a template you can adapt — how a workflow
  is wired, how parameters are passed, how errors are turned into plain language, how temp files get cleaned
  up. **The frame is already standing — just keep adding to it.**

The name carries a second meaning: a **camera tripod** — two legs wobble, three legs hold. That is what this
software aims at: stability. It **downgrades the model tier** instead of running out of VRAM, **restarts
ComfyUI** when it dies, **reloads the UI** when you add models, and **explains errors in plain language**
instead of dumping a wall of logs.

## What problems it solves

| Where normal people get stuck | What Tripod Panel does |
| --- | --- |
| Node graphs, links, workflows | Everything is pre-built as pages: text-to-image / image edit / try-on / face swap / matting / outpaint / video / TTS / digital human — click and go |
| Which model to download, and where to put it | The **Models** page lists what you have / what is missing / which folder it belongs to; `models.en.md` gives official links |
| Out of VRAM → crash | Automatic tier downgrade (INT8 → Q4/Q8 GGUF), one-click VRAM release, auto-restart of ComfyUI |
| Errors are a wall of English tracebacks | Runs in Chinese and rewrites errors into plain language (e.g. "this step is just slow, nothing is broken") |
| No idea what values to set | Automatic reference-encoding precision, automatic tier selection, presets for poses/styles/prompts |
| Rebuilding the workflow every time | Per-page settings memory, run history, one-click "continue editing" |
| Not wanting to upload personal photos | **100% local inference** — no cloud API, your images never leave your machine |

## Who it is for (five audiences, and what each one gets)

### ① Video & art creators

**A photo, an audio clip and a few assets are enough to produce a short video, a talking-head clip or a
moving scene — and the whole post-production chain lives in one interface.**

- **Photo animation (MiniMax H3 image-to-video)**: one photo → a video; motion/camera description, duration and resolution selectable;
- **First/last-frame and reference-to-video**: give a first and last frame and it fills the middle, or give a reference image to lock the person/style;
- **Talking from audio**: a photo speaks a given audio track (lip sync) — the audio can be your own recording or generated by the panel;
- **Digital human**: one photo + one voice track → a talking-head video (H3 engine; LongCat optional);
- **TTS**: describe a voice in words (no reference audio needed) or **clone a voice** from 5–20 s of audio; produces a wav you can feed straight into the avatar/lip-sync features;
- **Video post**: concat clips, **burned-in caption text** (a line or two you type, rendered into the picture —
  note: importing an `.srt` file is **not** supported), watermark, background music, speed change,
  **frame-rate normalisation** (frame duplication/drop, not interpolation), fade in/out and black transitions
  between clips; outputs MP4;
- **Multi-photo merge**: several assets → automatic matting → automatic layout → the model only unifies lighting and removes seams; the result can be animated afterwards;
- Around it: text-to-image for covers and storyboards, outpaint, multi-angle, pose editor (skeleton), inpaint, hi-res upscaling.

> Why it saves time: no bouncing between "image tool → editor → voice tool → subtitle tool" — one pipeline, one panel.

### ② Xiaohongshu / e-commerce clothing creators

**Import 10 outfits at once and let it dress one person automatically — you see how every outfit looks on
your own machine, immediately.**

- **Batch try-on**: one person + N garments → each garment applied in turn, N images in one run (pairing rule selectable);
- **Fine single try-on**: five modes (raw / whole-protect / safe / model-pose / whole) — **face, hands and background are pasted back from the original pixels**, so no extra fingers or feet;
- **Batch garment extraction**: turn an outfit photo into a **white-background product shot** for your own asset library;
- **Batch matting**: transparent PNGs (for covers, stickers, composites);
- **Hairstyle change / face swap**: build a consistent "model persona" (face swap has a high-quality BFS mode and adjustable strength);
- **Old photo restore & colorize**: refresh old material;
- **Image-to-prompt (captioning)**: read an image → get a reusable prompt, keeping a whole set visually consistent;
- **Text-to-image + 4× upscaling**: covers and hero images (real RealESRGAN upscaling);
- **Short video**: turn try-on results into video (photo animation → subtitles/music in video post → TTS voice).

> Why it saves time: no hiring models, no Photoshop marathon; all controls are plain-language tiers and the
> VRAM tier downgrades automatically instead of crashing your machine.

### ③ E-commerce sellers, import/export, apparel makers, wedding studios, model & ad agencies

**What these have in common: many SKUs, many versions needed, and often client- or product-confidential
work — all three are handled locally on your own machine.**

**Chinese e-commerce sellers (Taobao / Tmall / JD / Pinduoduo / Douyin): no product shoot required**

> Whichever marketplace you sell on — **Taobao / Tmall / JD / Pinduoduo / Douyin / Amazon / eBay / your own store /
> TikTok Shop** — the hero images, detail-page visuals and hero videos can all be produced here instead of
> being photographed one by one.

- **Generate the product image directly**: text-to-image for white-background shots, scene shots and mood shots —
  no studio, no set building, no retoucher;
- **One simple photo is enough**: photograph the item flat → **change the background/scene** (wooden desk, kitchen,
  outdoors, festive) and you have a hero or detail-page image;
- **Product with a person in frame**: model shots / in-use scenes (multi-photo merge, batch try-on);
- **All the formats in one go**: outpaint to 1:1 (hero), 3:4 (detail), 16:9 (banner);
- **Print-grade output**: real RealESRGAN ×4 upscaling (long detail images, print files);
- **White-background listing images**: batch matting, ready to upload — especially handy where the platform
  **demands pure white (JD, Amazon)**;
- **Copy inspiration**: caption an image to get a description for titles and detail pages;
- **Hero and detail videos**: photo animation → video post (subtitles / watermark / music) → TTS;
- **Refresh old shots**: restore and colorize old product photos, or re-output them at higher resolution.

**Export / cross-border e-commerce (Amazon, eBay, standalone stores, TikTok Shop)**

- **Platform image specs**: batch matting for **pure white backgrounds**, then RealESRGAN upscaling to the required
  size (the 2000 px+ kind);
- **Lifestyle scenes without a shoot**: background/scene changes → kitchen, garden, outdoors, festive;
- **A+ and detail-page banners**: outpaint to any aspect ratio, several versions at once;
- **Multilingual copy inspiration**: image captioning can describe the product **in English**, ready for listing
  titles and bullet points;
- **Hero videos**: photo animation → video post (subtitles / watermark / music) → TTS;
- **Confidentiality**: all inference is local, so **new designs and client material never go to a cloud service** —
  which matters a lot in export businesses.

**Import / domestic wholesale / warehouses & supply chain**

- **One SKU, one asset set**: white background + scene + multiple colours and sizes, produced in batch;
- **Supplier images are unusable**: old-photo restore & colorize plus upscaling rescues blurry or watermarked shots;
- **Large SKU batches at once**: batch matting / batch garment extraction / batch try-on /
  **batch multi-angle (up to 100 images × 8 viewpoints)**;
- **Standardised output**: uniform white background, size and format across marketplaces;
- **Pre-launch previews**: generate images of new items before touching stock or unpacking cartons.

> ⚠️ **E-commerce compliance matters**: marketplaces have explicit rules against misleading imagery.
> AI-generated images **must match the real product** — use this for scenes, mood, composition and layout, but
> **never to fake an appearance, feature or effect that does not exist**. Review before publishing; the
> responsibility is the user's.

**Apparel makers / wholesalers / e-commerce suppliers**

- **Model shots for new drops**: one model + N new garments → **batch try-on**, no studio booking per item;
- **White-background product shots**: batch garment extraction / batch matting, ready for product listings;
- **Preview cut and colour**: text-to-image + try-on to check a design before sampling/production;
- **Same style, many colours and scenes**: batch background/scene changes (the person is pasted back from the original pixels);
- **Digitise old catalogues**: old-photo restore and colorize.

**Wedding photography studios**

- **No reshoots**: swap background/scene (beach, church, forest, seamless) with **the couple's face, hair and make-up taken from the original pixels**;
- **Dress try-on previews**: one dress × several scenes, so clients can decide before booking a date;
- **One shot, many versions**: multiple styles from the same original, for photo selection and upsells;
- **Albums and demo videos**: photo animation → video post (concat / subtitles / music) → TTS;
- **Value-added service**: restore and colorize a client's old family photos.

**Model agencies / talent management**

- **Comp cards**: one frontal portrait → multi-angle character sheet in a consistent style;
- **Portfolio in different looks**: wardrobe, hairstyle and background changes to build a portfolio fast;
- **Audition videos**: photo animation, or a talking-head self-introduction from audio;
- **New-face onboarding**: batch matting for a consistent backdrop set.

**Advertising agencies / design studios / in-house media teams**

- **Pitches and comps**: text-to-image + multi-angle + outpaint for several visual directions quickly;
- **Storyboards and motion previews**: photo animation and first/last-frame video for animatics;
- **Finishing**: video post (concat / subtitles / watermark / music);
- **Multi-format deliverables**: outpaint to 1:1 / 9:16 / 16:9 and upscale for print-grade assets;
- **Voice auditions**: several voices (described or cloned) for radio-style demos.

> **Why this audience fits especially well**: ① **lower cost** — no model, studio and retoucher booked every time;
> ② **batch scale** — dozens at once instead of Photoshop one by one; ③ **confidentiality** — all inference is
> local, so **client material never leaves your machine** (the reason many studios cannot use cloud APIs).
>
> ⚠️ **For commercial use, check two things yourself**: each model's license (some are non-commercial) and the
> **consent** of any person whose face or voice you use. See [`DISCLAIMER.md`](DISCLAIMER.md).

### ④ AI enthusiasts & learners

**This is a ready-made textbook: complete pipelines, parameters and prompt templates for current local
models are all out in the open — install it, play with it, learn from it.**

**What you can learn here (the pipelines are already written, so you don't have to build them):**

- **Learn ComfyUI**: every feature has its **workflow already wired up**, and you can **open the native
  ComfyUI UI (port 8188) right inside the panel** to see how nodes are connected, how parameters are passed
  and how models are loaded — far faster than reading docs;
- **Learn Qwen-Image**: the complete pipelines and parameters for text-to-image, image editing and
  multi-reference generation (encoding precision, resolution tiers, CFG, steps, negative prompts, LoRA
  strength) are all in the panel, so you can try them one by one and see how parameters change results;
- **Learn MiniMax H3 (video)**: image-to-video, first/last frame, reference-to-video, talking from audio,
  digital human — one H3 family, including the real difference between Q4 and INT8 on a 10 GB card;
- **Learn TTS**: Breeze-TTS voice design and voice cloning (including real constraints such as "the
  reference audio must come with its exact transcript");
- **Learn prompt engineering**: image captioning, prompt enhancement, prompt tools, plus every template and
  negative prompt used by the features — all readable and editable;
- **Read the post-processing source**: garment masking for try-on, Poisson seam blending for outpaint,
  matting + layout for multi-photo merge — none of it is a black box; the backend is a single `panel.py`.

**Hands-on side:**

- **Everything is exposed**: model tier (INT8 / Q4 / Q8 / FP8 GGUF), **up to 10 LoRA slots with weights -5~5**,
  sampling steps, CFG, **reference encoding precision 512–2048**, seed, number of references;
- **Model manager**: listing, **file-header verification** (spots half-downloaded or corrupt files), one-click
  VRAM release, model-folder fingerprint (the UI reloads itself when you add models);
- **Run history**: prompt, parameters, duration and output of every run are kept — copy the prompt, or continue editing;
- **Hackable**: one backend file + one frontend file, no build step; 100% local inference, offline-capable, edit freely.

### ⑤ Complete beginners (the whole point of this project)

**If you can type, you can use it — and an AI agent can do the installation for you.**

- **One-line image generation**: text-to-image → type one sentence in Chinese → click generate; size, steps and
  tier all have sensible defaults, and it downgrades instead of running out of VRAM;
- **Three one-click presets**: character sheet (front/back/head multi-angle), old-photo restore & colorize,
  old-photo merge;
- **Fully automatic short video**: photo animation → video post (concat / subtitles / watermark / music) → TTS — a few clicks;
- **Multi-photo merge**: drop in a few photos; it mats, lays out and harmonises them into one coherent image;
- **Plain-language Chinese UI and errors**: not a wall of English; it tells you what went wrong and what to do;
- **Double-click to start** on Windows (no command line), or just use it in a browser;
- **Stuck on installation?** The section below gives you one paragraph to paste into any AI agent, so it can
  find paths, install dependencies, download models and debug for you.

> Why it is safe for beginners: everything is automatic by default, and mistakes are cheap — every run is kept
> in the history, ready to review or re-run.


## Where the "foolproof" part shows

- **One interface for everything** — no nodes, no workflows, no LoRA wiring.
- **Plain-language errors** — it tells you what went wrong and what to do, instead of dumping logs.
- **Fully automatic** — tier selection, VRAM downgrade, ComfyUI auto-restart, and the UI auto-reloads when you add models.
- **One-click start** — on Windows, double-click `启动三脚架.vbs` and you get a native window; no console window.
- **Stuck on installation?** The section below gives you a paragraph to paste into any AI agent, so it can do the boring parts (find paths, install deps, download models, fill config, debug) for you.

---

## Screenshots

> Taken from a **fresh, empty instance** (no gallery items) — they contain no user images.

| Text-to-image | Image edit |
| --- | --- |
| ![text-to-image](docs/screens/01-文生图.png) | ![image edit](docs/screens/02-图片编辑.png) |

| Multi-photo merge | Editing tools |
| --- | --- |
| ![multi-photo merge](docs/screens/03-多图合成一个镜头.png) | ![editing tools](docs/screens/04-编辑工具.png) |

| Photo-to-video | |
| --- | --- |
| ![photo to video](docs/screens/05-照片动起来.png) | |

---

## What it is

ComfyUI is powerful but demanding. This panel folds it into a single interface: **pick an image → write
one sentence → click generate**, and it handles the rest. All inference is local, so it works offline.

Everything is inside the same interface — here is the **complete feature list**:

**Create**

| Feature | What it does |
| --- | --- |
| Text-to-image | prompt in Chinese; 1–10 reference images (multi-reference); canvas 1:1 / 3:4 / 4:3 / 9:16 / 16:9; base edge 512/768/1024/1536/2048; steps, batch count, seed; transparent background (RGBA); live preview; turbo mode |
| Image edit | up to 10 main images + references; adjustable reference encoding precision (keeps the machine alive); saying "change the background/scene" switches to "repaint background only, paste the person back from the original pixels" |
| Virtual try-on | five modes (raw / whole-protect / safe / model-pose / whole); face, hands and background pasted back from the original pixels |
| Hairstyle change | changes the hair only, keeps face and background |
| Face swap | high-quality BFS mode (prompt-driven + a BFS head LoRA) with adjustable strength; plus a fast insightface + inswapper path |
| Old photo restore & colorize | one click, with dedicated positive/negative prompts |
| Object removal | paint over what you don't want |
| Inpaint | repaint a painted region from your prompt |
| Image-to-prompt | local Qwen3-VL reads an image and writes a reusable prompt; rewritable, multi-language |
| Presets | 🎴 character sheet (front/back/head multi-angle), 🖼 old-photo restore, 🏞 old-photo merge |

**Multi-photo merge**

| Feature | What it does |
| --- | --- |
| Merge several photos into one shot | 2–10 photos → per-image matting and classification → **our own layout** (sky / hazy distance / subjects on one horizon / water reflection) → the model only unifies lighting and removes seams → optional RealESRGAN upscaling |

**Batch processing**

| Feature | What it does |
| --- | --- |
| Batch matting | many images at once → transparent PNGs |
| Batch garment extraction | outfit photos → white-background product shots |
| Batch try-on | one person × N garments, one image per garment |
| Batch multi-angle | up to 100 images × 8 viewpoints |

**Image tools**

| Feature | What it does |
| --- | --- |
| Outpaint | expand all four sides; Poisson seam blending + seam-band repaint; original pixels untouched |
| Multi-angle | 8 horizontal/pitch/distance viewpoints of the same subject |
| Pose editor | 3D skeleton editor → pose images / mannequin images; poses can be saved to a library |

**Video**

| Feature | What it does |
| --- | --- |
| Photo animation (H3 I2V) | one photo → video; motion/camera description; duration and resolution selectable; Q4 (VRAM-saving) or INT8 (better, slower) tier |
| Talking from audio | the photo speaks the audio (lip sync) |
| First/last frame & reference-to-video | fill between first and last frame; or lock person/style with a reference image |
| Video post | concat clips, burned-in caption text (a line or two you type — **no `.srt` import**), watermark, music, speed change, frame-rate normalisation (duplication/drop, **not** interpolation), fade/black transitions, MP4 output |

**Audio & avatars**

| Feature | What it does |
| --- | --- |
| TTS (Breeze-TTS) | design a voice from a description (no reference audio) or clone one (5–20 s reference + its transcript); outputs wav, ready for the avatar page |
| Digital human | photo + voice track → talking-head video (H3 engine by default; LongCat optional) |

**System / general**

| Feature | What it does |
| --- | --- |
| Gallery | every image in the output folder, with category filters (all / garments / people / other / removed / mannequins) |
| Run history | prompt, parameters, duration and outputs of each run; copy the prompt or continue editing |
| File browser | browse the local output/input folders |
| Model manager | model listing, file-header verification (spots half/corrupt downloads), one-click VRAM release; the UI reloads automatically when you add models |
| Task queue | queue / cancel / retry / stop the batch; live progress and timings |
| Live preview | see the image being sampled |
| Embedded native ComfyUI | open the native ComfyUI UI (8188) inside the panel; "back to Tripod" and "reload" buttons |
| Settings | paths, access mode (local only / LAN), output folder, direct-ComfyUI switch |
| Desktop window | Electron native window (no browser, no console window); browser-only use is also fine |
| Automation & self-healing | automatic tier downgrade, VRAM handling, ComfyUI auto-restart, automatic UI reload |

The UI is a **single HTML file** (`new_ui.html`) and the backend is a **single Python file**
(`panel.py`), wrapped by a tiny Electron shell (`shell/`) — no frontend framework, no build step.

---

## Requirements

- **Windows 10/11** (the panel itself is cross-platform, but the launcher scripts are Windows-only for now)
- **An NVIDIA GPU**: 8 GB VRAM can run small images; 12 GB+ is much more comfortable (video and avatars are VRAM-hungry)
- **Python 3.10+** (using ComfyUI's own venv is easiest)
- **[ComfyUI](https://github.com/comfyanonymous/ComfyUI)** itself, already able to generate images
- **ffmpeg** (only for video post-processing / transcoding; on PATH is enough)
- Optional: Node.js 18+ (only if you want the native Electron window)

---

## Quick start

```text
1) Make sure ComfyUI itself works (note its folder — the one containing main.py)

2) Download models: follow models.en.md and place the missing weights in the right folders
   (this is the most time-consuming step)

3) Configure paths:
   copy panel_config.example.json -> panel_config.json
   fill in comfy_dir / comfy_python / model_roots for YOUR machine

4) Install dependencies (into ComfyUI's **own** venv, i.e. the `.venv` inside the ComfyUI folder):
   <ComfyUI>/.venv/Scripts/pip install -r requirements.txt
   (if that venv does not exist yet, run `python -m venv .venv` inside the ComfyUI folder first)

5) Start:
   · native window: cd shell && npm install, then double-click 启动三脚架.vbs
   · browser only:  double-click _launch_panel.ps1, or run it with **the python from step 4's venv**:
        <ComfyUI>/.venv/Scripts/python.exe panel.py
     ⚠ Do not run it with your system python: the dependencies live in the venv, and system python
       will fail with `ModuleNotFoundError: No module named 'cv2'`.
     then open http://127.0.0.1:8899/new
```

### Configuration (`panel_config.json`)

| Key | Meaning | Default when empty |
| --- | --- | --- |
| `comfy_dir` | ComfyUI source folder (contains `main.py`) | common locations are auto-detected; otherwise you get a clear error |
| `comfy_python` | interpreter used to run ComfyUI | it tries `<comfy_dir>/.venv`, `<comfy_dir>/../.venv`, then `pythonw.exe` (on Windows `pythonw.exe` is preferred so no console window pops up) |
| `user_dir` / `input_dir` / `output_dir` | ComfyUI data folders | `<comfy_dir>/user|input|output` |
| `model_roots` | model root folders (can be several) | `[<comfy_dir>/models]` |
| `lora_dir` | LoRA folder | first model root containing `loras` |
| `insightface_dir` | face-swap model folder | `<model root>/insightface` |
| `ffmpeg` / `ffprobe` | transcoding binaries | found on `PATH` |
| `save_dir` | "also save a copy here" folder | empty = off; if set it must be an **existing** normal folder (for safety it may not be a system folder or a user-config area) |
| `node_fp8_loader`, `node_outpaint_canvas`, `node_outpaint_composite`, `node_prompt_enhance` | optional third-party node class names | features tell you what to fill in when needed |
| `access_mode` | `local` (this machine only) / `lan` | `local` |
| `lan_token` | access token for LAN mode (auto-generated) | generated when you enable `lan`; open the panel from another device with the URL containing `?token=` (printed in the startup log). **The desktop shell and local browsers are unaffected** |
| `comfy_port` | ComfyUI port | `8188` |

You can also skip the file and use environment variables: `PANEL_COMFY_DIR`, `PANEL_COMFY_PYTHON`,
`PANEL_INPUT_DIR`, … (upper-cased key names with a `PANEL_` prefix).

---

## 🤖 Let an AI agent install it for you (recommended)

The annoying part of this stack is "paths don't match, models are in the wrong folder, dependencies are
missing". **Paste the block below into any AI agent that can run shell commands** (Claude Code, Codex,
Cursor, …) and it will do the work and debug it for you:

```text
I'm on Windows and want to install "Tripod Panel", a local GUI wrapper around ComfyUI.
The repo is already downloaded/cloned locally. Please set it up for me, telling me what you're
doing at each step:

1) Find my ComfyUI install folder (the one containing main.py) and its venv interpreter
   (prefer pythonw.exe on Windows — python.exe pops up a console window);
2) Install the dependencies into ComfyUI's venv: numpy, opencv-python (requirements.txt);
   if I want face swap, also insightface + onnxruntime;
3) Read models.en.md, check which models are missing, and FIRST give me a list
   (name / size / download link / target folder) for confirmation before downloading;
   do not download hundreds of GB at once;
4) Copy panel_config.example.json to panel_config.json and fill comfy_dir, comfy_python, user_dir,
   input_dir, output_dir, model_roots, lora_dir, insightface_dir and ffmpeg with the real paths on my
   machine (remember: backslashes must be doubled in JSON);
5) Start it and verify: run `python panel.py`, then open http://127.0.0.1:8899/new — it must return 200.
   If it fails, read the console output / comfyui_panel.log and fix it (usual causes: wrong path,
   port 8899 already in use, ComfyUI not running, model folders not matching);
6) Finally tell me which features work now and which are still missing models.

Important:
- Do NOT change my system proxy / network settings;
- Do NOT kill my browser (chrome/msedge) or any process by name — only processes you started;
- Ask me before downloading large files; never write my private images, configs or paths
  into anything that gets committed.
```

After that, day-to-day use is just double-clicking the launcher. `new_ui.html` / `panel.py` are edited in
place — refresh the page and the change is live.

---

## FAQ

**Q: It says "ComfyUI not found" on startup.**
`comfy_dir` in `panel_config.json` is wrong. That folder must contain `main.py`.

**Q: Tasks stay queued / it says ComfyUI isn't running.**
The panel tries to start ComfyUI automatically. Check manually: open `http://127.0.0.1:8188` — if you see
ComfyUI's own UI, it is running.

**Q: A black console window flashes sometimes.**
ComfyUI was started with `python.exe`. Point `comfy_python` at `pythonw.exe` in the same folder.

**Q: "Port already in use" (8899).**
Close the panel process holding it, or start with a different port: set `PANEL_PORT=8900`.

**Q: The Models page shows lots of "missing".**
That is expected — add them per `models.en.md`. The panel reports what is missing; it never downloads
anything behind your back.

**Q: Out of VRAM.**
Switch the "model tier" from INT8 to Q4/Q8 GGUF in the UI: more than half the VRAM, at some cost in speed
and quality.

---

## Repository layout

```
panel.py                  Backend: local HTTP server + ComfyUI client + prompts/post-processing
new_ui.html               UI: single file (HTML+CSS+JS), no build step
clothing_mask.py          Garment mask module for virtual try-on (keeps face/hands/background intact)
panel_config.example.json Example config (copy to panel_config.json)
shell/main.js             Electron shell (wraps the panel in a native window)
shell/package.json        Shell dependency (electron)
启动三脚架.vbs / _launch_panel.ps1   Windows launchers
tools/upscale_esrgan.py   Upscale CLI (the UI has the same feature as a button)
logo_*.png / brand_lockup.png  Icons
```

---

## Contributions welcome — that is what "scaffolding" means

This project has been **scaffolding** from day one: it pins down *how to build*, but it does not dictate
*what* you build. Sooner or later you will hit a "I wish it could do X" moment — bring X back here.

**Things worth contributing (examples):**

- **A pipeline you built**: e.g. a complete "10 outfits → batch try-on → images → copy → short video" flow,
  or a new way of laying out a composite;
- **Better prompt templates / default parameters**: if a feature works better on your machine and your subject
  matter, send those values up;
- **New models and tiers**: newly released models, leaner quantised tiers, new loaders;
- **Turning "mysterious failure" into one plain sentence**: the pit you fell into + the real cause + one line of
  plain-language guidance. This kind of contribution is the most valuable of all;
- **New pages / features**: copy an existing page — every feature doubles as a template;
- **Docs, translations, screenshots, better defaults** — all count.

**How to submit (short version):**

```text
1. Fork the repo and edit panel.py and/or new_ui.html (just those two files)
2. Self-test on a separate instance: PANEL_PORT=8911 python panel.py → open http://127.0.0.1:8911/new
   (the "let an AI agent install it" prompt earlier in this README works for verifying too)
3. Open a PR describing three things: what changed / why / how you verified it
```

**A few ground rules (they make merging easy):**

1. **No frameworks, no build step** — keep one `panel.py` backend and one `new_ui.html` frontend; edit and refresh;
2. **Errors must speak plain language**, ideally in Chinese; say what is wrong and what to do instead of dumping a traceback;
3. **VRAM/GPU-related changes: state the VRAM you tested on** (e.g. "verified on a 10 GB card with INT8");
4. **Never commit model weights** (this repo ships no weights, only download links);
5. **Never commit private images, private paths or any credentials** (`.gitignore` catches a layer of it — check once more before the PR);
6. Windows first, but cross-platform is better;
7. One PR, one change — easier to roll back.

**You don't have to write code to help:** open an Issue describing the flow you want, attach a result image,
share a prompt/parameter combination you use, or write down a pit someone else will fall into — all of that gets
folded into the docs and the defaults.

> In one line: **the defaults here are not the last word.** If you built something better, replace them and send
> them back, so the next person has an easier time.

---

## License and disclaimer

- **Code**: MIT, see [`LICENSE`](LICENSE).
- **Models**: this repository ships **no weights**; you obtain them yourself and each one keeps its own
  license (some are non-commercial — check before you use them).
- **Disclaimer**: see [`DISCLAIMER.md`](DISCLAIMER.md). **You are responsible for anything you generate
  with this software and for how you use it.**
