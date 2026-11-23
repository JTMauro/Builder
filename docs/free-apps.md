# Free apps, models and libraries for the studio

A catalog of free tools that would make Builder better, and make Claude better at running it.
Researched November 2026. Builder already has ffmpeg, Kokoro (37 voices), faster-whisper, espeak
lip-sync cues, MediaPipe capture, Blender 5.x with MakeHuman/MPFB, Cycles, three.js stages with CMU
mocap, Poly Haven, ambientCG, Openverse and Freesound search, TripoSR, procedural music, ambience and
footsteps, the Unreal/MetaHuman bridge, Resolve timelines, optional Real-ESRGAN and RIFE, and Craft.
This list covers what to add on top of that.

**How to read it**

- **Licence** describes the model weights or the app, not only the code. When the code is MIT but
  the weights are non-commercial, the row says **NC**. Anything marked **NC**, a revenue cap or a
  territory limit is flagged. Those tools are left out of the top 15 and listed again at the end.
- **HW** is what the tool runs on. *CPU* means it is usable on a laptop CPU. *GPU n GB* is the VRAM
  for a usable speed, using quantised or offloaded builds where those exist. Sizes are approximate
  downloads.
- **Pri** is the priority for this studio: **H** add soon, **M** worth it, **L** niche or later.
- **Verified** means the licence was checked against the model card, repository or vendor page
  this month (the source is linked). *repo* means the licence is the one long published in the
  repository and was not re-checked. Before shipping anything commercial, read the LICENSE file of
  the exact checkpoint you download.
- **Account or key**: most tools need nothing. Gated Hugging Face models need a free HF account and
  token (`HF_TOKEN`). Exceptions are called out in the rows.

---

## Top 15 to add next

Chosen for the most new capability per install, clean licences, and a good fit with ops.py, the MCP
tools and Craft. GPU items are marked: they would be optional extras (`builder[...]`), like
TripoSR today.

| # | Add | What it gives Builder | Licence | HW / size | Why now |
|---|---|---|---|---|---|
| 1 | **Z-Image-Turbo** (alt: **FLUX.2 [klein] 4B**) | `make_image`: concept art, backdrops, textures, posters, and input pictures for `image_to_3d` and `animate_image`. Today Builder cannot make an image from words. | Apache-2.0 (both) [verified](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo) / [BFL](https://bfl.ai/blog/flux2-klein-towards-interactive-visual-intelligence) | GPU 12–16 GB; ~20 GB download (bf16), ~10 GB quantised | The biggest missing piece. Both are permissive, fast (8 or fewer steps) and fit consumer GPUs. Use only klein **4B**: the 9B is NC. |
| 2 | **Qwen-Image-Edit-2511** | `edit_image`: "make the shirt green", fix hands, add or remove objects, change the style while keeping the character consistent. Useful for fixing character sheets and thumbnails. | Apache-2.0 [verified](https://huggingface.co/Qwen/Qwen-Image-Edit-2511) | GPU 16–24 GB (fp8/NF4); ~20–40 GB | The best permissive editor. It pairs with #1 and with `visual_diff`. |
| 3 | **Qwen3-TTS** (1.7B VoiceDesign, Base and CustomVoice; 0.6B) | Voices described in words ("gravelly old sailor") and cloning from a few seconds of audio, in 10 languages. Fills Kokoro's gaps: no cloning and no voice design. | Apache-2.0 [verified](https://arxiv.org/html/2601.15621) | GPU 6–8 GB (CPU works but is slow); ~2–5 GB | Gives characters their own voices. Cloning must follow the bible's consent rule. |
| 4 | **ACE-Step 1.5** | `make_song`: full songs with vocals and lyrics, instrumentals, covers, repainting and extending a track. `make_music` stays the offline fallback. | MIT [verified](https://arxiv.org/abs/2602.00744) | GPU ~8 GB+; ~10 GB | Trained on licensed and royalty-free music, says outputs can be used commercially, and is fast. |
| 5 | **audio-separator** (BS-/Mel-RoFormer, MDX models) | `separate`: vocals, drums, bass and other stems. Uses: remove music under dialogue, karaoke, re-mix a client clip, clean the voice before `transcribe`. | MIT code; weights vary per model [verified](https://pypi.org/project/audio-separator/) | CPU ok (GPU faster); 100–600 MB per model | One pip install with many models. Use the MIT-tagged RoFormer checkpoints. |
| 6 | **BiRefNet** (directly or through `rembg`) | `remove_background`: clean cut-outs for thumbnails, compositing, sprites and `image_to_3d` (better than TripoSR's built-in step). | MIT [paper](https://arxiv.org/pdf/2401.03407), `rembg` MIT (repo) | CPU ok (ONNX); 170–900 MB | Avoid RMBG-2.0, which is NC. |
| 7 | **DeepFilterNet 3** | Real-time neural noise suppression for `denoise_audio` and `voice_polish`, much better than ffmpeg `afftdn` on speech. | MIT or Apache-2.0 [verified](https://raw.githubusercontent.com/Rikorose/DeepFilterNet/main/README.md) | CPU real time; ~10 MB | Tiny, CPU-only, a direct quality gain for an existing operation. |
| 8 | **TRELLIS.2** (4B) | `image_to_3d` high mode: textured PBR meshes (not vertex colours), much more detail than TripoSR. Feeds Cycles stills, three.js stages and Craft games. | MIT (nvdiffrast/nvdiffrec have their own licences) [verified](https://huggingface.co/microsoft/TRELLIS.2-4B) | GPU 24 GB; ~16 GB | The best permissive image-to-3D model. Keep TripoSR as the CPU fallback. |
| 9 | **UniRig** | `auto_rig`: skeleton and skin weights for any mesh (TripoSR/TRELLIS output, creatures, props), so generated models can use CMU mocap. | MIT (repo, HF card) [source](https://huggingface.co/VAST-AI/UniRig/tree/main) | GPU ~8–12 GB; ~1–2 GB | Today only MakeHuman people get rigs. Watch its successor SkinTokens (licence not yet confirmed). |
| 10 | **Wan 2.2** (TI2V-5B; Animate-14B, S2V-14B) | `generate_video`: text or image to a 5 s 720p clip; Animate drives a character picture with a reference performance. Use for b-roll, establishing shots and animated stills beyond pan and zoom. | Apache-2.0 [verified](https://huggingface.co/Wan-AI/Wan2.2-TI2V-5B) | GPU 24 GB official (8–12 GB with GGUF and offload); 20–60 GB | The only strong video-model family that is fully Apache-licensed. |
| 11 | **WhisperX + pyannote community-1** | `diarize` and better `transcribe`: who spoke when, tighter word timings (forced alignment), per-speaker captions. Starts the dubbing chain: diarise, translate with Claude, voice with Qwen3-TTS, then `lipsync`. | WhisperX BSD-2 (repo); pyannote CC-BY-4.0 [verified](https://huggingface.co/pyannote/speaker-diarization-community-1) | CPU ok (GPU faster); ~100 MB on top of Whisper | **Gated**: a free HF account and token are needed to download community-1. |
| 12 | **glTF-Transform + gltfpack (meshoptimizer)** | `optimize_glb`: Draco/Meshopt compression, KTX2 textures, decimation, deduplication. Makes MakeHuman, TRELLIS and Poly Haven models load fast in three.js stages and Craft pages (and silences the "Draco not available" warnings). | MIT (repos) | CPU; ~50 MB (Node) | Small, deterministic and scriptable. 3 MB characters could become well under 1 MB. |
| 13 | **Quality metrics pack: VMAF, LPIPS/DISTS, audiobox-aesthetics, ffmpeg detectors** | Extends `visual_diff`, `style_check` and `audio_check` with numbers Claude can act on: VMAF for encode quality, LPIPS for "looks the same" judgements, audiobox scores for production quality and enjoyment of music or voice, `blackdetect`/`freezedetect`/`silencedetect`/`signalstats` for broken renders. | VMAF BSD-2-Clause-Patent (repo); LPIPS BSD-2 (repo); audiobox CC-BY-4.0 [verified](https://huggingface.co/facebook/audiobox-aesthetics) | CPU ok; VMAF ships in ffmpeg builds with libvmaf; audiobox ~1 GB | The bible's "render, then look" rule becomes measurable, with pass/fail thresholds. Avoid `pyiqa`, which is now NC. |
| 14 | **Qwen3-VL 8B** (4B on smaller GPUs) + **Qwen3-Omni-Captioner** | A local second opinion for watching and listening: timestamped video understanding ("at 0:07 the arm goes through the table"), and detailed captions of sound and music. Cheaper than sending every frame to Claude, and it sees motion, not just stills. | Apache-2.0 [verified](https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct) / [captioner](https://huggingface.co/Qwen/Qwen3-Omni-30B-A3B-Captioner) | VL-8B: GPU 8–16 GB or CPU via llama.cpp GGUF (~5 GB). Captioner: GPU 24 GB+ (30B MoE) | Lets `direct_scene` check whole clips. Claude stays the judge, and these models supply evidence. |
| 15 | **Godot 4.7** | A third Craft target beside web pages: real 2D/3D games exported headless (`--headless --export-release`) to desktop, web and Android. Also a free realtime renderer for stage previews. | MIT (repo) [releases](https://godotengine.org/releases/4.6/) | CPU; ~150 MB editor plus ~1 GB export templates | Free with no royalties or account, scriptable, and imports GLB natively. |

**Next five** (nearly made the list): **SAM 2.1** (Apache-2.0, rotoscoping, masks and object
tracking), **LatentSync 1.6** (Apache-2.0, photoreal lip-sync on real video, 18 GB), **NVIDIA
Audio2Face-3D** (SDK MIT, models under the NVIDIA Open Model License, audio to ARKit blendshapes for
MetaHuman and MakeHuman faces), **Brush + Spark** (Apache-2.0 / MIT, capture a real place as
Gaussian splats and show it in three.js stages), **Resemble Enhance** (MIT, speech restoration and
bandwidth extension, a partner for DeepFilterNet).

**Host for the GPU models:** run them through **ComfyUI** (GPL-3.0, API mode with JSON workflows)
or `diffusers` (Apache-2.0), behind one Builder operation per task. ComfyUI already wraps Z-Image,
FLUX.2, Qwen-Image, Wan, SeedVR2 and LatentSync, so adding a model is mostly a workflow file.

---

## Full catalog

### 3D: modelling, retopology, UV, texturing, optimisation

| Name | What it adds here | Licence | Size | HW | Maturity | Pri |
|---|---|---|---|---|---|---|
| glTF-Transform / gltfpack | Compress and optimise GLB for stages and Craft (see Top 15) | MIT (repo) | ~50 MB | CPU | Stable, active | H |
| xatlas (`xatlas-python`) | Proper UV unwraps for TripoSR vertex-colour meshes, so colours can be baked to textures. **In use**: `image_to_3d texture=true`, `bake_texture` | MIT (repo) | <5 MB | CPU | Stable | M |
| PyMeshLab | Mesh repair, hole filling, decimation and remeshing in Python | GPL-3.0 (repo) | ~100 MB | CPU | Stable | M |
| Instant Meshes | Quad retopology from the command line for generated meshes | BSD-3 (repo) | ~5 MB | CPU | Old but stable | M |
| QRemeshify (Blender add-on, QuadWild) | Cleaner quads inside headless Blender | GPL-3.0 (repo) | ~20 MB | CPU | Active | L |
| Blender built-ins (QuadriFlow, Smart UV, bake, geometry nodes) | Already installed: script remesh, UV and bake through `bpy` before adding anything else | GPL | – | CPU/GPU | Stable ([5.1 notes](https://www.cgchannel.com/2026/03/discover-5-key-features-in-blender-5-1)) | H (use) |
| Material Maker | Node-based procedural PBR materials (Godot-based app) | MIT (repo) | ~100 MB | GPU (any) | Active | L |
| Ucupaint | Layered texture painting inside Blender | GPL-3.0 (repo) | small | CPU/GPU | Active | L |
| ArmorPaint | Substance-style painter | Source zlib; **prebuilt binaries are paid** | – | GPU | Active | L |
| Manifold (`manifold3d`) | Fast, robust mesh booleans for procedural props and level kits | Apache-2.0 (repo) | small | CPU | Stable | L |

### 3D: photogrammetry and Gaussian splats

| Name | What it adds here | Licence | Size | HW | Maturity | Pri |
|---|---|---|---|---|---|---|
| Brush | Train splats from photos or video, CLI and web, any GPU (WebGPU) | Apache-2.0 [verified](https://radiancefields.com/platforms/brush) | ~30 MB | GPU (any vendor) | v0.3, active | M |
| Spark (sparkjs) | Show splats inside three.js stages, mixed with meshes; streaming LoD in 2.0 | MIT [verified](https://github.com/sparkjsdev/spark) | npm package | Browser | Active (World Labs) | M |
| COLMAP (with GLOMAP global mapper) | Camera poses from photos, the input to Brush, gsplat and meshing | BSD-3 (repo) | ~200 MB | CPU (GPU helps) | Standard | M |
| gsplat / Nerfstudio | Python splat training and research tools | Apache-2.0 (repo) | ~2 GB with torch | GPU 8 GB+ | Active | L |
| Meshroom / AliceVision | Photos to textured mesh, node GUI plus CLI | MPL-2.0 (repo) | ~1.5 GB | NVIDIA GPU preferred | Active | L |
| MapAnything (**apache checkpoint only**) | Metric 3D from a few photos, feed-forward (fast scene blockouts) | Apache-2.0 for `map-anything-apache`; default checkpoint is **NC** [verified](https://huggingface.co/facebook/map-anything-apache/blob/main/README.md) | ~4 GB | GPU 8 GB+ | Research | L |
| MoGe (2 and 3) | Single-image metric depth and point maps: depth for parallax `animate_image`, set blockout from a photo | Code MIT [verified](https://github.com/microsoft/MoGe); check weights on HF | ~1.3 GB | GPU (CPU slow) | Active (MoGe-3, Aug 2026) | M |
| Depth Anything V2 **Small** | Fast depth maps (2.5D parallax, fog, DoF) | Small Apache-2.0; Base/Large/Giant **NC** [verified](https://raw.githubusercontent.com/DepthAnything/Depth-Anything-V2/main/README.md) | ~100 MB | CPU ok | Stable | M |

### 3D: image or text to 3D

| Name | What it adds here | Licence | Size | HW | Maturity | Pri |
|---|---|---|---|---|---|---|
| TRELLIS.2 | Textured PBR assets up to 1536³ (see Top 15) | MIT [verified](https://huggingface.co/microsoft/TRELLIS.2-4B) | ~16 GB | GPU 24 GB | Research, popular | H |
| TripoSG | Higher-quality shape than TripoSR (no texture); pair with xatlas plus a texture pass | MIT [card](https://huggingface.co/VAST-AI/TripoSG) | ~6 GB | GPU 8–12 GB | Research | M |
| Step1X-3D | Geometry plus texture pipeline | Apache-2.0 (mirror listing; check repo) [src](https://sourceforge.net/mirror/step1x-3d/) | ~15 GB | GPU 24 GB | Research | L |
| SAM 3D Objects | Single-photo object reconstruction in real scenes | SAM License (custom, commercial allowed, use restrictions) [src](https://roboflow.com/model-licenses/segment-anything-3) | ~5 GB | GPU 16 GB+ | New | L |
| Hunyuan3D 2.1 / Omni | Strong textured output | **Excludes EU, UK and South Korea**; MAU cap. See skip list | – | – | – | Skip |

### Rigging, cloth and hair

| Name | What it adds here | Licence | Size | HW | Maturity | Pri |
|---|---|---|---|---|---|---|
| UniRig | Auto-rig any mesh (see Top 15). CPU checked Nov 2026: not practical (needs CUDA builds of spconv, torch_scatter/cluster and flash_attn; README states an 8 GB CUDA GPU; joint names would still need mapping to Mixamo). Builder's `rig_model` fits a Mixamo skeleton from the mesh's shape in Blender instead (humanoids only) | MIT (repo, HF) | 1–2 GB | GPU 8–12 GB | SIGGRAPH '25 | H |
| SkinTokens | UniRig's successor (rig plus skinning in one model) | **Not yet confirmed**: check before use [paper](https://www.arxiv.org/pdf/2602.04805) | – | GPU | New (Feb 2026) | Watch |
| Rigify (built into Blender) | Animator-friendly control rigs on MPFB bodies | GPL | – | CPU | Stable | M |
| Mixamo | Free auto-rig plus a large animation library | Free with an **Adobe ID (account)**; maintenance mode; humanoids only [FAQ](https://helpx.adobe.com/creative-cloud/faq/mixamo-faq.html) | web | – | Ageing, not end-of-life | L |
| Blender cloth, hair curves, Essentials hair assets | Cloth sim for MakeHuman clothes, real hair curves instead of cards (would fix the "hair piece shows behind the fringe" issue in the bible) | GPL | – | CPU | Stable | M |
| NVIDIA Warp | GPU simulation kernels in Python (cloth, particles) for custom effects | Apache-2.0 (repo) | ~100 MB | NVIDIA GPU | Active | L |

### Animation: mocap from video, motion generation, retargeting

Most "video to 3D body" models output **SMPL** bodies, and SMPL is non-commercial unless licensed
from Meshcapade ([SMPL licence](https://smpl.is.tue.mpg.de/license.html),
[explainer](https://dev.to/arkadiuss/a-3d-body-scan-for-nine-cents-without-smpl-4me1)). That rules
out GVHMR, WHAM, 4DHumans and TokenHMR for free commercial use, even when their code is MIT.

| Name | What it adds here | Licence | Size | HW | Maturity | Pri |
|---|---|---|---|---|---|---|
| SAM 3D Body + MHR body model | Full-body mesh and pose from video frames without SMPL. Upgrades `video_to_motion` from MediaPipe landmarks to a real 3D body you can retarget | SAM 3D Body: SAM License (commercial ok, use restrictions); MHR: Apache-2.0 (reported) [src](https://sourceforge.net/mirror/sam-3d-body/) | ~3 GB | GPU 12 GB+ | New (CVPR 2026) | M |
| rtmlib / RTMPose (MMPose) | Fast, accurate 2D whole-body keypoints, CPU via ONNX; a sturdier alternative to MediaPipe | Apache-2.0 (repo) | ~100 MB | CPU ok | Stable | M |
| Pose2Sim | Multi-camera markerless capture with OpenSim kinematics, BVH/TRC out | BSD-3 (repo) [PyPI](https://pypi.org/project/pose2sim/) | ~500 MB | CPU | Active | L |
| FreeMoCap | Multi-webcam capture app, Blender export | **AGPL** (commercial licence on request) [PyPI](https://pypi.org/project/freemocap/) | ~1 GB | CPU | Active | L |
| HY-Motion 1.0 | Text to motion (1B DiT), humanoid skeleton clips: "a tired walk then a sit" | Tencent Hunyuan Community licence (**check territory**; Tencent's other Hunyuan licences exclude the EU, UK and South Korea) [card](https://huggingface.co/tencent/HY-Motion-1.0) | ~4 GB | GPU 24–26 GB | New | L |
| MoMask / MDM / T2M-GPT | Text to motion research baselines | Code MIT, but trained on HumanML3D/AMASS (**NC data**) | – | GPU | Research | Skip |
| 100STYLE dataset | 100 locomotion styles in BVH (sad walk, proud walk…) to add variety beyond CMU | CC BY 4.0 (dataset page; check) | ~4 GB | – | Stable | M |
| three.js `SkeletonUtils.retargetClip`, Blender Expy Kit / KeeMap | Retarget BVH/FBX clips to MPFB or Mixamo-named rigs | MIT / GPL (repos) | small | CPU | Stable | M |

### Faces, audio-to-face and lip-sync

| Name | What it adds here | Licence | Size | HW | Maturity | Pri |
|---|---|---|---|---|---|---|
| NVIDIA Audio2Face-3D (SDK v2.3 / v3.0 models) | Audio to full facial animation (ARKit-style blendshapes, tongue, emotion). A big step up from viseme cues for MetaHuman/Unreal and the talking MakeHuman | SDK MIT; training framework Apache; models NVIDIA Open Model License; Audio2Emotion custom [verified](https://developer.nvidia.com/blog/nvidia-open-sources-audio2face-animation-model) | ~2 GB | NVIDIA GPU (TensorRT) | Production | M |
| Rhubarb Lip Sync | Mature phoneme-based mouth cues (the format `lipsync` already copies); better English accuracy than espeak timing | MIT (repo) | ~50 MB | CPU | Stable, slow updates | M |
| LatentSync 1.6 | Re-sync lips on real or generated video to a new voice (dubbing, talking stills) | Apache-2.0 [src](https://raw.githubusercontent.com/bytedance/LatentSync/main/README.md) | ~7 GB | GPU 18 GB (1.5: 8 GB) | Active | M |
| MuseTalk 1.5 | Real-time talking-face lip-sync, lighter than LatentSync | MIT code, models "any purpose" (LICENSE file mixed) [repo](https://github.com/TMElyralab/MuseTalk) | ~3 GB | GPU 8 GB | Active | L |
| Wav2Lip, LivePortrait (InsightFace parts), SadTalker variants | – | **NC** pieces (Wav2Lip models, InsightFace weights) | – | – | – | Skip |

Use lip-sync on real people only with consent (bible rule).

### Rendering and realtime engines

| Name | What it adds here | Licence | Size | HW | Maturity | Pri |
|---|---|---|---|---|---|---|
| Godot 4.7 | Games and realtime previews (see Top 15) | MIT | ~150 MB | CPU/any GPU | Very active ([4.6 notes](https://godotengine.org/releases/4.6/)) | H |
| Bevy | Rust ECS engine, fits the Rust Craft engine; WASM builds | MIT/Apache-2.0 (repo) | crates | Any GPU | Pre-1.0, active | L |
| PlayCanvas engine / Babylon.js | Alternative web 3D engines (Babylon has a Gaussian-splat loader and physics built in) | MIT / Apache-2.0 (repos) | npm | Browser | Stable | L |
| Defold, LÖVE, Phaser | Light 2D game targets for Craft (Phaser is ideal for web pages) | Defold licence (free, no royalty) / zlib / MIT | small | CPU | Stable | M (Phaser) |
| Unreal Engine 5.x | Already bridged. Free under $1M lifetime gross per product, then 5% royalty; seat subscription for non-game companies over $1M; **Epic account**; EULA updated 30 Oct 2026 [change log](https://www.unrealengine.com/en-US/eula-change-log/unreal) | Proprietary (source-available) | ~60 GB+ | GPU | Production | (have) |
| O3DE | Apache-licensed AAA-style engine | Apache-2.0 (repo) | ~40 GB | GPU | Slow adoption | L |
| LuxCoreRender | Unbiased path tracer (caustics) with a Blender add-on | Apache-2.0 (repo) | ~300 MB | CPU/GPU | Slow but alive | L |
| Mitsuba 3 | Differentiable renderer: fit materials and lights to a reference photo | BSD-3 (repo) | ~200 MB | CPU/GPU | Active research | L |

### Image: generation, editing, cleanup

| Name | What it adds here | Licence | Size | HW | Maturity | Pri |
|---|---|---|---|---|---|---|
| Z-Image-Turbo (6B) | Text to image, very fast, good text rendering | Apache-2.0 [verified](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo) | ~20 GB (~10 GB quantised) | GPU 12–16 GB | Popular since Dec 2025 | H |
| FLUX.2 [klein] 4B (and 4B Base for LoRA training) | Text to image plus multi-reference editing in one small model | Apache-2.0; **9B is NC** [verified](https://bfl.ai/blog/flux2-klein-towards-interactive-visual-intelligence) | ~16 GB with encoder | GPU ~13 GB | Stable | H |
| Qwen-Image (20B) | Best open text-in-image (posters, signs, title cards) | Apache-2.0 (card) | ~40 GB | GPU 24 GB (fp8) | Stable | M |
| Qwen-Image-Edit-2511 | Instruction editing (see Top 15) | Apache-2.0 [verified](https://huggingface.co/Qwen/Qwen-Image-Edit-2511) | 20–40 GB | GPU 16–24 GB | Stable | H |
| FLUX.1 [schnell] | Older fast model, wide LoRA ecosystem | Apache-2.0 (card) | ~24 GB | GPU 12 GB+ | Mature | L |
| ComfyUI | Node runner plus HTTP API to host all of the above | GPL-3.0 (repo) | ~1 GB + models | GPU | Very active | H (as host) |
| stable-diffusion.cpp | CPU/Vulkan/Metal inference in C++ for machines without CUDA | MIT (repo) | small | CPU/any GPU | Active | M |
| BiRefNet / rembg | Background removal (see Top 15) | MIT | 170–900 MB | CPU | Stable | H |
| SAM 2.1 | Click-to-mask, video object tracking: rotoscope a person, recolour one object, matte for compositing | Apache-2.0 (repo) | 150–900 MB | CPU slow, GPU 4 GB+ | Stable | M |
| SAM 3 | Text-prompted masks ("all the chairs") | SAM License (custom; commercial ok with use restrictions) [src](https://roboflow.com/model-licenses/segment-anything-3) | ~3.5 GB | GPU 8 GB+ | New | M |
| LaMa / IOPaint | Object and watermark removal (inpainting), CPU friendly | Apache-2.0 (repos) | ~200 MB | CPU ok | Mature | M |
| GFPGAN | Face restoration for upscaled or generated faces | Apache-2.0 code (check facexlib weights) (repo) | ~350 MB | CPU slow / GPU 2 GB | Mature, quiet | L |
| Real-ESRGAN (have), **SeedVR2** | SeedVR2: diffusion video and image restoration and upscale, far beyond ESRGAN on soft footage | Apache-2.0 [src](https://docs.comfy.org/tutorials/utility/seedvr2) | 3B ~7 GB, 7B ~16 GB | GPU 12–24 GB | Active | M |
| vtracer | Raster to SVG (logos, flat art to vector for pages) | MIT (repo) | small | CPU | Stable | M |

### Video: generation, interpolation, cleanup, editing

| Name | What it adds here | Licence | Size | HW | Maturity | Pri |
|---|---|---|---|---|---|---|
| Wan 2.2 TI2V-5B / Animate-14B / S2V-14B | Text/image to video, character animation, sound-driven video (see Top 15) | Apache-2.0 [verified](https://huggingface.co/Wan-AI/Wan2.2-TI2V-5B) | 20–60 GB | GPU 24 GB (less with GGUF) | Mature ecosystem | H |
| LTX-2 / LTX-2.5 | Fast video **with synced audio**, multi-shot | LTX Community licence: **free only under $10M annual revenue** [src](https://ltx.io/model/license) | ~40 GB | GPU 24 GB+ | Active (2.5, Oct 2026) | M (flag) |
| Open-Sora 2.0 (11B) | Apache text/image to video, 256/768 px | Apache-2.0 [src](https://bdtechtalks.com/2025/03/24/open-sora-2/) | ~45 GB | GPU 40 GB+ | Stale since 2025 | L |
| Practical-RIFE (have via `smooth_motion`) | Keep it; current v4.2x models | MIT [src](https://github.com/hzwer/Practical-RIFE) | ~50 MB | CPU slow / GPU | Mature | (have) |
| ffmpeg `vidstabdetect`/`vidstabtransform`, `deshake` | `stabilize` operation for shaky phone clips (no new install with a full ffmpeg build) | GPL (vid.stab) | – | CPU | Mature | H (cheap) |
| ffmpeg `nlmeans`, `hqdn3d`, `bwdif`, `deband` | `denoise_video` / deinterlace / deband operations | LGPL/GPL | – | CPU | Mature | M |
| OpenColorIO + ACES configs, `colour-science` | Correct colour pipelines (Blender 5 ships ACES 1.3/2.0), LUT generation, grade matching | BSD-3 (repos) | ~50 MB | CPU | Standard | M |
| PySceneDetect | Shot and cut detection: auto-split footage, build Resolve timelines from cuts, check pacing | BSD-3 (repo) | small | CPU | Stable | H (cheap) |
| auto-editor | Remove silences and dead air automatically, export to Resolve, Premiere or Kdenlive | Unlicense (repo) | small | CPU | Active | M |
| MLT framework (`melt`) | Render Kdenlive/Shotcut project XML headless, so Builder can write editable projects for free editors (like `timeline_export` for Resolve) | LGPL-2.1 (repo) | ~50 MB | CPU | Mature | M |
| Kdenlive 25.12 | Free NLE; opens MLT projects Builder could write | GPL [src](https://kdenlive.org/news/releases/25.12.3) | ~300 MB | CPU/GPU | Very active | M |
| Shotcut 25.12 | Simpler NLE on MLT | GPL [src](https://www.linux.org/threads/phoronix-shotcut-25-12-released-with-10-bit-video-cpu-pipeline-linear-color-processing.60695/latest) | ~250 MB | CPU/GPU | Active | L |
| Olive | – | GPL | – | – | **Stalled; skip for now** | Skip |
| Blender VSE + compositor | Already installed; 5.0 overhauled VSE and added compositor modifiers | GPL | – | – | Stable | (have) |
| OBS Studio + obs-websocket | Screen and webcam capture and streaming that Claude can drive over websocket (record a Craft game demo) | GPL-2 (repo) | ~400 MB | CPU/GPU | Very active | M |
| Natron | Node compositor | GPL-2 | ~300 MB | CPU | Slow maintenance | L |
| Motion Canvas / Manim | Code-driven explainer animation (diagrams, typography, maths) that Claude can write and render | MIT (repos) | npm / pip | CPU | Active | M |
| Remotion | React to video | **Source-available; paid company licence over 3 people** | – | – | – | Skip (flag) |

### Audio: voices (TTS, cloning, conversion)

| Name | What it adds here | Licence | Size | HW | Maturity | Pri |
|---|---|---|---|---|---|---|
| Qwen3-TTS | Voice design and cloning, 10 languages (see Top 15) | Apache-2.0 [verified](https://arxiv.org/html/2601.15621) | 2–5 GB | GPU 6–8 GB | Active | H |
| Kyutai Pocket TTS (100M) | CPU-only real-time voice with cloning, good for `/talk` characters on machines without a GPU | CC-BY-4.0 weights (cloning weights are gated, consent terms) [src](https://huggingface.co/kyutai/pocket-tts) | ~400 MB | CPU real time | New (2026) | M |
| Kyutai TTS 1.6B (en/fr) | Streaming TTS with ~10 s cloning | CC-BY-4.0 [verified](https://huggingface.co/kyutai/tts-1.6b-en_fr) | ~4 GB | GPU 8 GB | Stable | L |
| Chatterbox (Turbo, Nano 110M CPU, Multilingual V3) | Expressive cloning with paralinguistic tags; every output carries the Perth watermark | MIT, but **retired, unmaintained since Aug 2026** [verified](https://github.com/resemble-ai/chatterbox) | 0.5–2 GB | CPU (Nano) / GPU | Archived | L |
| Dia (1.6B) | Two-speaker dialogue with laughs and sighs in one pass (podcasts, skits) | Apache-2.0 (repo) | ~6 GB | GPU 10 GB | Quiet | L |
| Piper | Tiny offline voices (Raspberry Pi class) | **GPL-3.0** now (moved to OHF-Voice) [src](https://www.assemblyai.com/blog/free-open-source-text-to-speech) | ~60 MB per voice | CPU | Active | L |
| RVC / OpenVoice v2 | Voice conversion (keep a performance, change the timbre) | MIT (repos) | ~1 GB | GPU 4 GB | Mature / quiet | L (consent) |

### Audio: music, sound effects, stems, restoration

| Name | What it adds here | Licence | Size | HW | Maturity | Pri |
|---|---|---|---|---|---|---|
| ACE-Step 1.5 | Songs with vocals, edits and extensions (see Top 15) | MIT [verified](https://huggingface.co/ACE-Step/acestep-v15-base/blob/main/README.md) | ~10 GB | GPU ~8 GB | Active | H |
| Magenta RealTime 2 (2.4B; 230M small) | Live, steerable instrumental music that can follow a game state in Craft builds | Code Apache-2.0, weights CC-BY-4.0 plus extra terms [src](https://huggingface.co/google/magenta-realtime-2) | 1–10 GB | GPU (small runs on Mac) | Active | M |
| DiffRhythm | Fast full-song generation | Apache-2.0 (reported; check repo) [src](https://www.creativeainews.com/articles/best-open-source-ai-music-models-2026/) | ~4 GB | GPU 8 GB | Research | L |
| FluidSynth + GeneralUser GS / MuseScore General SoundFonts | Real instrument sounds for `make_music` (render its MIDI through a SoundFont instead of synth tones) | LGPL-2.1; SoundFonts free/MIT-style | ~30–200 MB | CPU | Mature | M |
| Stable Audio Open (1.0 / Small) | Text to SFX, foley, loops | Stability Community licence (**free under $1M revenue**) [src](https://huggingface.co/stabilityai/stable-audio-open-small) | 1–5 GB | GPU / Arm CPU (Small) | Stable | L (flag) |
| jsfxr / sfxr | Retro game SFX (jump, coin, laser) generated from parameters; perfect for Craft games | MIT/Unlicense (repos) | tiny | CPU | Mature | M |
| Kenney audio packs, Sonniss GDC bundles | Ready SFX libraries: Kenney CC0; Sonniss royalty-free (licence file per bundle) | CC0 / royalty-free | 0.1–30 GB | – | – | M |
| audio-separator (RoFormer, MDX) | Stems (see Top 15) | MIT code; weights vary | 100–600 MB | CPU ok | Active | H |
| Demucs v4 | Classic 4-stem separation | MIT (repo); archived upstream | ~300 MB | CPU ok | Archived | L |
| DeepFilterNet 3 | Speech denoise (see Top 15) | MIT/Apache-2.0 | ~10 MB | CPU | Stable | H |
| Resemble Enhance | Restore muffled or band-limited speech (old recordings, phone audio) | MIT (repo) | ~1 GB | GPU (CPU slow) | Quiet | M |
| Pedalboard (Spotify) | Studio effects and VST3/AU hosting from Python: compressor, EQ, limiter, reverb for `voice_polish` | GPL-3.0 (repo) | ~30 MB | CPU | Active | M |
| Ardour / LMMS / Audacity / Surge XT / Carla | Free DAW, beat-maker, editor, synth, plugin host for hand finishing | GPL (Ardour's ready-made binaries are pay-what-you-want; distro builds free) | 50–500 MB | CPU | Mature | L |

### Speech: ASR, diarisation, translation for dubbing

| Name | What it adds here | Licence | Size | HW | Maturity | Pri |
|---|---|---|---|---|---|---|
| WhisperX | Word-accurate alignment, batched faster-whisper, diarisation glue | BSD-2 (repo) | ~100 MB | CPU/GPU | Active | H |
| pyannote speaker-diarization-community-1 | Who spoke when | CC-BY-4.0, **gated (HF token)** [verified](https://huggingface.co/pyannote/speaker-diarization-community-1) | ~30 MB | CPU ok | Active | H |
| Whisper large-v3-turbo (via faster-whisper) | Default model upgrade: near large-v3 accuracy, much faster | MIT (repo) | ~1.6 GB | CPU ok / GPU | Stable | M |
| NVIDIA Parakeet-TDT 0.6B v3 | Very fast, accurate ASR in 25 European languages with timestamps | CC-BY-4.0 on HF/NGC (NVIDIA build page says Open Model License) [src](https://huggingface.co/OpenASR/parakeet-tdt-0.6b-v3) | ~2.5 GB (ONNX int8 smaller) | CPU ok / GPU | Stable | M |
| Silero VAD | Accurate speech/no-speech detection (trim, chunk, `audio_check`) | MIT (repo) | 2 MB | CPU | Stable | M |
| Translation: Claude itself | Script translation for dubbing with tone and timing notes; no new install | – | – | – | – | H (use) |
| MADLAD-400 (3B/7B) | Offline MT in 400+ languages | Apache-2.0 (card) | 6–15 GB | GPU / CPU (GGUF) | Stable | L |
| Argos Translate | Lightweight offline MT (OPUS-MT models) | MIT; models mostly CC-BY (repo) | ~100 MB per pair | CPU | Active | L |
| NLLB-200, SeamlessM4T | – | **NC** | – | – | – | Skip |

### 2D, vector and design

| Name | What it adds here | Licence | Size | HW | Maturity | Pri |
|---|---|---|---|---|---|---|
| Inkscape 1.4 (CLI) | Headless SVG to PNG/PDF export, text-to-path, batch actions for title cards and logos | GPL (repo) | ~150 MB | CPU | Mature | M |
| GIMP 3.2 | Non-destructive layers, link layers that update from Inkscape; Script-Fu/Python batch | GPL [verified](https://gimp.org/news/2026/03/14/gimp-3-2-released) | ~300 MB | CPU | Active | L |
| Krita 5.3 / 6 (Qt6, early access) | Painting and frame-by-frame animation for hand-drawn work | GPL [notes](https://krita.org/cs/release-notes/krita-5-3-release-notes) | ~200 MB | CPU | Active | L |
| Penpot | Open design and prototyping (Figma-like), self-hostable, has an API | MPL-2.0 (repo) | Docker | CPU | Active | L |
| Synfig / OpenToonz / Pencil2D / Friction | 2D vector and cut-out animation, motion graphics | GPL / BSD-3 / GPL / GPL | 100–500 MB | CPU | Mixed | L |
| Pixelorama, Tiled, LDtk | Pixel art, tile maps and level editors for Craft 2D games (export JSON) | MIT / GPL app (exports free) / MIT | small | CPU | Active | M |
| Lottie (lottie-web) | Light vector animations for pages | MIT (repo) | npm | Browser | Mature | L |

### Game engines, assets and tooling

| Name | What it adds here | Licence | Size | HW | Maturity | Pri |
|---|---|---|---|---|---|---|
| Godot 4.7 | See Top 15 | MIT | ~150 MB | CPU | Very active | H |
| Phaser 3/4 | Web 2D games, the natural Craft page target | MIT (repo) | npm | Browser | Active | M |
| Kenney game assets | Thousands of CC0 2D/3D/UI/audio assets. **Added** as `asset_search`/`asset_import` source `kenney` (3D packs; the site's own download link, GLB inside) | CC0 | varies | – | Active | M |
| Quaternius models | CC0 low-poly characters, already rigged and animated. **Added** as source `quaternius` (his own OpenGameArt uploads: Google Drive refuses server downloads, itch.io has no plain link) | CC0 | varies | – | Active | M |
| Poly Pizza | Thousands of single low-poly models (many Kenney/Quaternius), CC0 or CC BY. **Added** as source `polypizza`: API needs a free key (`POLY_PIZZA_API_KEY`); its file CDN blocks many cloud IPs | CC0 / CC BY 3.0 per model | – | – | Active | M |
| Godot MCP servers (community) | Let Claude run, inspect and debug a Godot project | MIT (various, check each) | small | – | Young | L |
| Blender MCP (`mcp-for-blender`) | Interactive Blender control for Claude Code; Builder already runs Blender headless, so only needed for live sessions | MIT, third-party (not by Blender) [src](https://github.com/ahujasid/blender-mcp) | small | – | Popular | L |

### Automation and QA

| Name | What it adds here | Licence | Size | HW | Maturity | Pri |
|---|---|---|---|---|---|---|
| VMAF (libvmaf in ffmpeg) | Encode quality score per frame: catch bad compression in exports | BSD-2-Clause-Patent (repo) | built in | CPU | Industry standard | H |
| LPIPS / DISTS | Perceptual "same look?" distance next to SSIM in `visual_diff` / `style_check` | BSD-2 / MIT (repos) | ~100 MB | CPU ok | Stable | H |
| ffmpeg `blackdetect`, `freezedetect`, `silencedetect`, `signalstats`, `ebur128`, `astats` | Catch black frames, frozen renders, dropouts, crushed or clipped levels | LGPL/GPL | built in | CPU | Mature | H (cheap) |
| Playwright | Drive Craft pages: screenshots, input playback, console errors, video capture | Apache-2.0 (repo) | ~300 MB with browsers | CPU | Very active | H (if not already) |
| pixelmatch / odiff | Fast pixel-diff for page screenshots | ISC / MIT (repos) | tiny | CPU | Stable | M |
| VBench | Score generated video (motion smoothness, subject consistency, flicker) | Apache-2.0 (repo) | ~5 GB of models | GPU | Research | L |
| ImageReward / LAION aesthetic predictor | Rank N generated candidates and keep the best | Apache-2.0 / MIT (repos) | 1–2 GB | GPU or CPU | Stable | M |
| SigLIP 2 / CLIP score | "Does this image match the prompt?" check before showing the owner | Apache-2.0 (card) | ~400 MB–1.5 GB | CPU ok | Stable | M |
| UTMOS(v2) / DNSMOS | Predicted MOS for TTS naturalness and noise; gate voices in `audio_check` | MIT (repos) | ~500 MB | CPU ok | Stable | M |
| audiobox-aesthetics | Production quality, complexity and enjoyment for speech, music and SFX | CC-BY-4.0 [verified](https://huggingface.co/facebook/audiobox-aesthetics) | ~1 GB | CPU ok | Stable | H |
| LAION-CLAP | Text-to-audio match ("is this rain?"), SFX search by meaning | Code CC0-1.0 [verified](https://github.com/LAION-AI/CLAP); check checkpoint card | ~600 MB | CPU ok | Stable | M |
| ViSQOL | Reference-based audio quality (before/after denoise) | Apache-2.0 (repo) | small | CPU | Stable | L |
| pyiqa (IQA-PyTorch) | – | **PolyForm Noncommercial** [verified](https://github.com/chaofengc/IQA-PyTorch) | – | – | – | Skip |

### Helping Claude see, hear and judge its own output

| Name | What it adds here | Licence | Size | HW | Maturity | Pri |
|---|---|---|---|---|---|---|
| Qwen3-VL 4B/8B (Instruct/Thinking) | Local video QA with timestamps, OCR on title cards, describe the motion in a clip | Apache-2.0 [verified](https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct) | 5–17 GB | GPU 8–16 GB / CPU GGUF | Stable | H |
| Qwen3-Omni-30B-A3B-Captioner | Detailed captions of any audio (music mood, SFX, room, voice quality), up to 30 s per clip | Apache-2.0 [verified](https://huggingface.co/Qwen/Qwen3-Omni-30B-A3B-Captioner) | ~60 GB (bf16) | GPU 24 GB+ (quantised) | Stable | M |
| Florence-2 / moondream | Small CPU captioner and detector (object boxes for "is the character in frame?") | MIT / Apache-2.0 (cards) | 0.5–4 GB | CPU ok | Stable | M |
| PANNs / BEATs audio tagging | Cheap labels for audio events ("dog bark", "music", "speech") on CPU, to verify SFX placement | MIT (PANNs repo) | ~300 MB | CPU | Stable | M |
| TransNetV2 | Neural shot-boundary detection (more robust than thresholding) | MIT (repo) | ~30 MB | CPU ok | Stable | L |

---

## Skip, or licence problems

| Item | Problem |
|---|---|
| FLUX.2 [klein] **9B**, FLUX.1 [dev], FLUX.2 [dev] | FLUX Non-Commercial License ([BFL](https://bfl.ai/blog/flux2-klein-towards-interactive-visual-intelligence)). Use klein 4B instead. |
| Hunyuan3D 2.x, HunyuanVideo 1.5, other Tencent Hunyuan models | Territory **excludes EU, UK and South Korea**; MAU cap ([licence](https://huggingface.co/tencent/Hunyuan3D-2/blob/main/LICENSE), [HunyuanVideo](https://chatforest.com/reviews/hunyuanvideo-tencent-open-source-video-generation/)). HY-Motion uses a Tencent community licence too: check before use. |
| LTX-2 / 2.3 / 2.5 | Free only under $10M annual revenue ([ltx.io](https://ltx.io/model/license)). Usable for the owner today, but not "free forever". |
| Stability models (Stable Audio Open, SD3.5, Stable Fast 3D, SPAR3D) | Community licence, free under $1M revenue ([stability.ai/license](https://stability.ai/license)). |
| MMAudio, TangoFlux, AudioLDM2, MusicGen/AudioCraft weights | Non-commercial weights ([MMAudio](https://github.com/hkchengrex/MMAudio)). |
| F5-TTS weights, XTTS-v2 (CPML), Voxtral TTS (CC-BY-NC, and deprecated), IndexTTS2 (commercial use by agreement with bilibili) | NC or by request ([Voxtral](https://docs.mistral.ai/models/voxtral-tts-26-03.md), [IndexTTS](https://github.com/index-tts/index-tts)). |
| Microsoft VibeVoice TTS | Pulled by Microsoft for misuse (Sept 2025), still disabled; community copies exist, but avoid ([repo](https://github.com/microsoft/VibeVoice)). VibeVoice-ASR is fine. |
| Fish Audio S2 Pro | Open weights, but commercial use needs a paid licence. |
| RMBG-2.0 (BRIA) | CC-BY-NC-4.0 ([card](https://huggingface.co/briaai/rmbg-2.0)). Use BiRefNet. |
| CodeFormer | S-Lab licence, non-commercial. Use GFPGAN. |
| Depth Anything V2 Base/Large/Giant, MapAnything default checkpoint, DUSt3R/MASt3R, VGGT (default) | NC (use DA-V2 Small, `map-anything-apache`, MoGe). |
| Original Inria 3D Gaussian Splatting code | Non-commercial research licence. Use Brush or gsplat. |
| SMPL / SMPL-X and everything that needs them (GVHMR, WHAM, 4DHumans, TokenHMR, MoMask/MDM trained on AMASS) | Non-commercial; commercial use is licensed through Meshcapade ([SMPL](https://smpl.is.tue.mpg.de/license.html), [GVHMR](https://huggingface.co/ryanrudes/gvhmr)). |
| Wav2Lip, InsightFace weights (used by LivePortrait, many face-swap tools) | Non-commercial. |
| NLLB-200, SeamlessM4T | CC-BY-NC. |
| pyiqa / IQA-PyTorch | PolyForm Noncommercial ([repo](https://github.com/chaofengc/IQA-PyTorch)). Use LPIPS/DISTS directly. |
| Remotion | Source-available; companies over 3 people need a paid licence. |
| Pixar RenderMan Non-Commercial, Autodesk/Adobe free tiers (except Mixamo) | Non-commercial or account-locked. |
| Chatterbox | Licence is fine (MIT), but the project is retired as of Aug 2026: use only if pinned. |
| Olive video editor | GPL, but development stalled. Use Kdenlive. |
| SAM 3 / SAM 3D (all) | Not a problem, but not OSI: Meta's SAM License allows commercial use with use restrictions and terms Meta can change. Prefer SAM 2.1 (Apache) where it is enough. |

Sources not linked inline: licences marked *(repo)* are those published in each project's
repository. Check the LICENSE file at install time, and record it in `license.json` the same way
`asset_import` does.
