# 模型清单（本仓库不附带权重）

> **重要**：本仓库**只发代码，不包含任何模型文件**。下面这些权重请自行从官方渠道下载，
> 每个模型**各自遵循它自己的许可证**（有的禁止商用，有的要求署名，请务必自己确认后再用）。
>
> 表格里的链接是"去哪儿找"的参考。若链接失效，请按文件名在 Hugging Face / GitHub 上搜同名资源。

## 放哪儿（目录名对应 ComfyUI 的模型分类）

| 目录 | 放什么 |
| --- | --- |
| `models/diffusion_models`（也常叫 `unet`）| 主模型 / 去噪主干 |
| `models/text_encoders`（或 `clip`）| 文本/视觉编码器 |
| `models/vae` | VAE |
| `models/loras` | LoRA（加速、风格、换脸等）|
| `models/background_removal` | 抠图模型 |
| `models/upscale_models` | 放大模型 |
| `models/insightface` | 换脸：`buffalo_l`（目录）+ `inswapper_128.onnx` |

`models/` 指的是 `panel_config.json` 里 `model_roots` 配的那些目录（可以多个）。

---

## 1. 图像主力：Qwen-Image 2.1（文生图 / 图片编辑 / 大部分图像功能）

| 文件 | 目录 | 必需 | 说明 |
| --- | --- | --- | --- |
| `qwen_image_2.1_int8_convrot.safetensors` | diffusion_models | ✅ 推荐 | 质量最好；12GB 显存以上 |
| `qwen_image_2.1_Q4_K_M.gguf` | diffusion_models | 备选 | 最省显存（约 4GB 级）|
| `qwen-image-2.1-Q8_0_fixed.gguf` | diffusion_models | 备选 | 中间档 |
| `qwen_image_2.1_rin247_fp8.safetensors` | diffusion_models | 备选 | FP8 |
| `qwen3vl_8b_w4a8.safetensors` | text_encoders | ✅ | 文本编码器 |
| `qwen_image_2.1_vae_bf16.safetensors` | vae | ✅ | VAE |

来源：Hugging Face 上的 **Comfy-Org** 官方组织（Qwen-Image 系列仓库）。

## 2. 视频 / 数字人：MiniMax H3

| 文件 | 目录 | 必需 | 说明 |
| --- | --- | --- | --- |
| `minimax_h3_fl2va_pruned_fp8_Q4_0.gguf` | diffusion_models | ✅（图生视频）| Q4，10GB 卡能跑 |
| `minimax-h3-ref2va-Q4_0.gguf` | diffusion_models | 备选 | 参考图生视频 |
| `minimax_h3_fused_refdelta_r1024_turbo8_mystic07_int8_convrot.safetensors` | diffusion_models | 可选 | INT8 融合版（质量更好、很吃显存 = 很慢）|
| `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | text_encoders | ✅ | H3 的文本编码器 |
| `minimax_h3_video_vae_fp16.safetensors` | vae | ✅ | 视频 VAE |
| `minimax_h3_audio_vae_fp32.safetensors` | vae | ✅ | 音频 VAE |
| `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors` | loras | 可选 | 提速 LoRA（官方模板用它）|
| `minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors` | loras | 可选 | 提速 LoRA（8 步）|

来源（已核实）：
- https://huggingface.co/Comfy-Org/MiniMax-H3
- https://huggingface.co/lightx2v/Minimax-h3-Turbo
- https://huggingface.co/drbaph/MiniMax-H3-Turbo-Lora-ComfyUI

## 3. 换脸 / 人像相关

| 文件 | 目录 | 必需 | 说明 |
| --- | --- | --- | --- |
| `buffalo_l`（整个目录）| insightface | 换脸需要 | 人脸检测/识别；`insightface` 首次运行会自动下载，也可手动放 |
| `inswapper_128.onnx` | insightface | 换脸需要 | 换脸模型。**该模型许可证为非商用/研究用途，请自行确认合规** |
| `bfs_head_v1.1_qwen_2.1.safetensors` | loras | 可选 | 换脸高质模式用的头部 LoRA |

## 4. 抠图 / 放大 / 其它

| 文件 | 目录 | 必需 | 说明 |
| --- | --- | --- | --- |
| `birefnet.safetensors` | background_removal | ✅（抠图/透明背景/多图合成要用）| BiRefNet |
| `lucida.safetensors` | background_removal | 可选 | 备用抠图模型 |
| `RealESRGAN_x4.pth` | upscale_models | 可选（超清放大）| https://github.com/xinntao/Real-ESRGAN |
| `Qwen-Image-2.1-viggle-turbo-v0.2.1-6step-lora-r128.safetensors` | loras | 可选 | 「加速模式」用的蒸馏 LoRA |
| `VNCCS_QI2_PoseStudioV1.1.safetensors` | loras | 可选 | 动作姿态相关 |
| `Qwen-Image-2.1-Object-Remover-Bbox-Preview-5000.safetensors` | loras | 可选 | 移除对象 |
| `umt5_xxl_fp8_e4m3fn_scaled.safetensors` | text_encoders | 可选 | 某些视频/动效工作流用 |

## 5. 配音 / 数字人（可选，界面里对应 TTS 与数字人页）

| 文件 | 目录 | 说明 |
| --- | --- | --- |
| Breeze-TTS 2 权重 | 由对应 ComfyUI 自定义节点管理 | 配音页使用 |
| `whisper-large-v3.safetensors` | 由对应节点管理 | 部分音频驱动流程需要 |
| `LongCat-Video-Avatar-vae.safetensors` / `longcat-avatar-dmd_lora.safetensors` | 由对应节点管理 | 数字人（可选引擎）|

---

## 还需要的 ComfyUI 自定义节点（不是模型，是代码）

面板通过 ComfyUI 的节点工作，以下功能依赖对应的自定义节点（用 ComfyUI-Manager 装即可）：

| 功能 | 依赖 |
| --- | --- |
| 姿势/骨骼编辑 | ComfyUI-Fisher-Pose |
| 多角度 | ComfyUI-qwenmultiangle |
| H3 视频 / 数字人 | ComfyUI-AIToolkit-MiniMaxH3（或等价的 H3 节点包）|
| 抠图 | 提供 `RemoveBackground` / `LoadBackgroundRemovalModel` 的节点包 |

装完自定义节点记得**重启 ComfyUI**；面板的"模型管理"页会列出它当前能看到的全部模型。

---

## 可选功能依赖的第三方节点（不在本仓库内）

下面这几个功能需要额外的 ComfyUI 自定义节点。节点的类名各人环境可能不同，
所以仓库里**没有写死**，请在 `panel_config.json` 里填：

| 功能 | 配置键 | 说明 |
| --- | --- | --- |
| 加载 FP8 权重 | `node_fp8_loader` | 某些 FP8 版主模型需要专用加载器节点 |
| 扩图（画面合成）| `node_outpaint_canvas` / `node_outpaint_composite` | 扩图功能用到的两个节点 |
| 提示词增强（扩写 / 图片反推）| `node_prompt_enhance` | 用本机 Qwen3-VL 做扩写或读图出描述 |

没配也能正常启动：用到这些功能时会给出中文提示，告诉你该填哪个键，而不是神秘失败。
（其余功能——文生图、图片编辑、多图合成、抠图、换脸、批量处理、视频、配音、数字人——
只依赖 ComfyUI 官方节点与上面 models.md 里列出的模型。）
