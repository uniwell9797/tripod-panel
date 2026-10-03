# Model list (no weights are bundled)

> **Important**: this repository ships **code only — no model files**. Download the weights below from
> their official sources. **Each model keeps its own license** (several are non-commercial or
> research-only — check before you use them).
>
> Links below are pointers to where to find things. If a link dies, search for the file name on
> Hugging Face / GitHub.

> 中文版见 [`models.md`](models.md)。

## Where files go (folder names = ComfyUI model categories)

| Folder | What goes there |
| --- | --- |
| `models/diffusion_models` (also called `unet`) | main / denoising model |
| `models/text_encoders` (or `clip`) | text & vision encoders |
| `models/vae` | VAE |
| `models/loras` | LoRAs (speed-up, style, face swap, …) |
| `models/background_removal` | matting models |
| `models/upscale_models` | upscalers |
| `models/insightface` | face swap: `buffalo_l` (folder) + `inswapper_128.onnx` |

`models/` means the folders listed in `model_roots` in `panel_config.json` (you may have several).

---

## 1. Image workhorse: Qwen-Image 2.1 (text-to-image / image edit / most image features)

| File | Folder | Required | Notes |
| --- | --- | --- | --- |
| `qwen_image_2.1_int8_convrot.safetensors` | diffusion_models | ✅ recommended | best quality; 12 GB VRAM and up |
| `qwen_image_2.1_Q4_K_M.gguf` | diffusion_models | alternative | lowest VRAM (~4 GB class) |
| `qwen-image-2.1-Q8_0_fixed.gguf` | diffusion_models | alternative | middle tier |
| `qwen_image_2.1_rin247_fp8.safetensors` | diffusion_models | alternative | FP8 |
| `qwen3vl_8b_w4a8.safetensors` | text_encoders | ✅ | text encoder |
| `qwen_image_2.1_vae_bf16.safetensors` | vae | ✅ | VAE |

Source: the **Comfy-Org** organisation on Hugging Face (Qwen-Image repositories).

## 2. Video / digital human: MiniMax H3

| File | Folder | Required | Notes |
| --- | --- | --- | --- |
| `minimax_h3_fl2va_pruned_fp8_Q4_0.gguf` | diffusion_models | ✅ (image-to-video) | Q4, runs on a 10 GB card |
| `minimax-h3-ref2va-Q4_0.gguf` | diffusion_models | alternative | reference-to-video |
| `minimax_h3_fused_refdelta_r1024_turbo8_mystic07_int8_convrot.safetensors` | diffusion_models | optional | INT8 fused (better quality, very VRAM-hungry = slow) |
| `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | text_encoders | ✅ | H3 text encoder |
| `minimax_h3_video_vae_fp16.safetensors` | vae | ✅ | video VAE |
| `minimax_h3_audio_vae_fp32.safetensors` | vae | ✅ | audio VAE |
| `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors` | loras | optional | speed-up LoRA (used by the official template) |
| `minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors` | loras | optional | speed-up LoRA (8-step) |

Sources (verified):
- https://huggingface.co/Comfy-Org/MiniMax-H3
- https://huggingface.co/lightx2v/Minimax-h3-Turbo
- https://huggingface.co/drbaph/MiniMax-H3-Turbo-Lora-ComfyUI

## 3. Face swap / portrait helpers

| File | Folder | Required | Notes |
| --- | --- | --- | --- |
| `buffalo_l` (whole folder) | insightface | for face swap | detection/recognition; `insightface` can auto-download it, or place it manually |
| `inswapper_128.onnx` | insightface | for face swap | the swap model. **Its license is non-commercial / research use — verify yourself** |
| `bfs_head_v1.1_qwen_2.1.safetensors` | loras | optional | head LoRA used by high-quality face swap |

## 4. Matting / upscaling / others

| File | Folder | Required | Notes |
| --- | --- | --- | --- |
| `birefnet.safetensors` | background_removal | ✅ (matting / transparent background / multi-photo merge) | BiRefNet |
| `lucida.safetensors` | background_removal | optional | fallback matting model |
| `RealESRGAN_x4.pth` | upscale_models | optional (hi-res upscale) | https://github.com/xinntao/Real-ESRGAN |
| `Qwen-Image-2.1-viggle-turbo-v0.2.1-6step-lora-r128.safetensors` | loras | optional | distilled LoRA for "turbo mode" |
| `VNCCS_QI2_PoseStudioV1.1.safetensors` | loras | optional | pose related |
| `Qwen-Image-2.1-Object-Remover-Bbox-Preview-5000.safetensors` | loras | optional | object removal |
| `umt5_xxl_fp8_e4m3fn_scaled.safetensors` | text_encoders | optional | used by some video/motion workflows |

## 5. TTS / digital human (optional — the TTS and avatar pages)

| File | Folder | Notes |
| --- | --- | --- |
| Breeze-TTS 2 weights | managed by the corresponding ComfyUI custom node | used by the TTS page |
| `whisper-large-v3.safetensors` | managed by the corresponding node | needed by some audio-driven pipelines |
| `LongCat-Video-Avatar-vae.safetensors` / `longcat-avatar-dmd_lora.safetensors` | managed by the corresponding node | digital human (optional engine) |

---

## ComfyUI custom nodes also required (code, not models)

Some features rely on custom ComfyUI nodes — install them with ComfyUI-Manager:

| Feature | Dependency |
| --- | --- |
| Pose / skeleton editor | ComfyUI-Fisher-Pose |
| Multi-angle | ComfyUI-qwenmultiangle |
| H3 video / digital human | ComfyUI-AIToolkit-MiniMaxH3 (or an equivalent H3 node pack) |
| Matting | a node pack providing `RemoveBackground` / `LoadBackgroundRemovalModel` |

Restart ComfyUI after installing custom nodes. The panel's "Models" page lists everything ComfyUI can
currently see.

---

## Optional features that need third-party nodes (not in this repo)

The following features need extra ComfyUI custom nodes. Node class names differ between environments, so
the code does **not hardcode them** — set them in `panel_config.json`:

| Feature | Config key | Notes |
| --- | --- | --- |
| Loading FP8 weights | `node_fp8_loader` | some FP8 main models need a dedicated loader node |
| Outpaint | `node_outpaint_canvas` / `node_outpaint_composite` | the two nodes used by outpaint |
| Prompt enhancement (expand / caption) | `node_prompt_enhance` | local Qwen3-VL for prompt expansion or image captioning |

The panel still starts fine without them: when you use such a feature it tells you (in Chinese) which key
to fill in, instead of failing mysteriously. Everything else — text-to-image, image edit, multi-photo
merge, matting, face swap, batch tools, video, TTS, digital human — only needs the official ComfyUI nodes
plus the models listed above.
