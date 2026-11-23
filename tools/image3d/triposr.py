"""One image to a 3D mesh with TripoSR (MIT, Tripo AI & Stability AI). Run by media_backend/image3d.py.

    python -I triposr.py IMAGE OUT.glb --code DIR --weights DIR [--mc 256] [--no-rembg] [--fg 0.85]
                         [--max-faces N] [--device auto|cpu] [--models DIR]

--code holds TripoSR's `tsr` package, --weights its config.yaml + model.ckpt. Mesh extraction uses
scikit-image's marching cubes (no torchmcubes build needed). Prints one JSON line with stats; exits 3 with
{"missing": [...]} when a Python package is missing.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import types

NEEDS = {"torch": "torch", "omegaconf": "omegaconf", "einops": "einops", "transformers": "transformers",
         "trimesh": "trimesh", "rembg": "rembg", "skimage": "scikit-image", "PIL": "pillow", "numpy": "numpy",
         "imageio": "imageio", "huggingface_hub": "huggingface_hub"}


def vit_keys(state: dict, want: set) -> dict:
    """transformers 5 renamed ViT's layers; map TripoSR's (transformers 4) checkpoint names when needed."""
    import re

    if not any(k.startswith("image_tokenizer.model.encoder.layer.") for k in state) or "image_tokenizer.model.layers.0.attention.q_proj.weight" not in want:
        return state
    ren = [(r"encoder\.layer\.(\d+)\.attention\.attention\.query\.", r"layers.\1.attention.q_proj."),
           (r"encoder\.layer\.(\d+)\.attention\.attention\.key\.", r"layers.\1.attention.k_proj."),
           (r"encoder\.layer\.(\d+)\.attention\.attention\.value\.", r"layers.\1.attention.v_proj."),
           (r"encoder\.layer\.(\d+)\.attention\.output\.dense\.", r"layers.\1.attention.o_proj."),
           (r"encoder\.layer\.(\d+)\.intermediate\.dense\.", r"layers.\1.mlp.fc1."),
           (r"encoder\.layer\.(\d+)\.output\.dense\.", r"layers.\1.mlp.fc2."),
           (r"encoder\.layer\.(\d+)\.", r"layers.\1.")]
    out = {}
    for k, v in state.items():
        for pat, rep in ren:
            k2 = re.sub(pat, rep, k)
            if k2 != k:
                k = k2
                break
        out[k] = v
    return out


def load_model(TSR, weights: str):
    import torch
    from omegaconf import OmegaConf

    cfg = OmegaConf.load(os.path.join(weights, "config.yaml"))
    OmegaConf.resolve(cfg)
    model = TSR(cfg)
    state = torch.load(os.path.join(weights, "model.ckpt"), map_location="cpu", weights_only=True)
    model.load_state_dict(vit_keys(state, set(model.state_dict())))
    return model


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("out")
    ap.add_argument("--code", required=True)
    ap.add_argument("--weights", required=True)
    ap.add_argument("--models", default=None)
    ap.add_argument("--mc", type=int, default=256)
    ap.add_argument("--threshold", type=float, default=25.0)
    ap.add_argument("--no-rembg", action="store_true")
    ap.add_argument("--fg", type=float, default=0.85)
    ap.add_argument("--max-faces", type=int, default=0)
    ap.add_argument("--device", default="auto")
    a = ap.parse_args()

    missing = sorted({pip for mod, pip in NEEDS.items() if importlib.util.find_spec(mod) is None})
    if a.max_faces and importlib.util.find_spec("fast_simplification") is None:
        missing.append("fast-simplification")
    if missing:
        print(json.dumps({"missing": missing}))
        sys.exit(3)
    if a.models:   # rembg's u2net and the DINO config land in Builder's models folder
        os.environ.setdefault("U2NET_HOME", os.path.join(a.models, "rembg"))
        os.environ.setdefault("HF_HOME", os.path.join(a.models, "huggingface"))

    import numpy as np
    import torch
    import trimesh
    from PIL import Image
    from skimage.measure import marching_cubes

    # tsr.models.isosurface imports torchmcubes (a C++ build); we extract the surface ourselves.
    stub = types.ModuleType("torchmcubes")
    stub.marching_cubes = None
    sys.modules.setdefault("torchmcubes", stub)
    sys.path.insert(0, a.code)
    from tsr.system import TSR
    from tsr.utils import remove_background, resize_foreground, scale_tensor

    t0 = time.time()
    times: dict[str, float] = {}
    device = "cuda" if a.device == "auto" and torch.cuda.is_available() else "cpu"
    torch.set_num_threads(max(1, os.cpu_count() or 1))

    model = load_model(TSR, a.weights)
    model.renderer.set_chunk_size(8192)
    model.to(device).eval()
    times["load_s"] = time.time() - t0

    t = time.time()
    img = Image.open(a.image)
    if a.no_rembg:
        img = img.convert("RGB")
    else:
        import rembg

        img = remove_background(img.convert("RGBA") if img.mode == "RGBA" else img.convert("RGB"), rembg.new_session("u2net"))
        if np.asarray(img)[..., 3].max() == 0:
            raise SystemExit("background removal found no object in the picture")
        img = resize_foreground(img, a.fg)
        arr = np.asarray(img).astype(np.float32) / 255.0
        arr = arr[..., :3] * arr[..., 3:4] + (1 - arr[..., 3:4]) * 0.5     # grey backdrop, as TripoSR expects
        img = Image.fromarray((arr * 255.0).astype(np.uint8))
    times["prep_s"] = time.time() - t

    t = time.time()
    with torch.no_grad():
        code = model([img], device=device)[0]
    times["model_s"] = time.time() - t

    t = time.time()
    n, radius = a.mc, model.renderer.cfg.radius
    lin = torch.linspace(0, 1, n)
    grid = torch.stack(torch.meshgrid(lin, lin, lin, indexing="ij"), -1).reshape(-1, 3)
    with torch.no_grad():
        dens = model.renderer.query_triplane(model.decoder, scale_tensor(grid, (0, 1), (-radius, radius)).to(device), code)["density_act"]
    vol = dens.reshape(n, n, n).float().cpu().numpy()
    if vol.max() <= a.threshold:
        raise SystemExit("the model found no solid object in the picture (try remove_background or a plainer photo)")
    verts, faces, _, _ = marching_cubes(vol, a.threshold, gradient_direction="descent")
    verts = verts / (n - 1) * 2 * radius - radius
    mesh = trimesh.Trimesh(verts, faces, process=True)
    parts = mesh.split(only_watertight=False)
    if len(parts) > 1:   # drop floating specks
        mesh = trimesh.util.concatenate([p for p in parts if len(p.faces) >= 0.02 * len(mesh.faces)] or [max(parts, key=lambda p: len(p.faces))])
    times["mesh_s"] = time.time() - t

    t = time.time()
    if a.max_faces and len(mesh.faces) > a.max_faces:
        mesh = mesh.simplify_quadric_decimation(face_count=a.max_faces)
    with torch.no_grad():   # colours at the final vertices, so decimation keeps them
        rgb = model.renderer.query_triplane(model.decoder, torch.as_tensor(mesh.vertices, dtype=torch.float32, device=device), code)["color"]
    rgb = (rgb.clamp(0, 1).cpu().numpy() * 255).astype(np.uint8)
    mesh.visual = trimesh.visual.ColorVisuals(mesh, vertex_colors=np.concatenate([rgb, np.full((len(rgb), 1), 255, np.uint8)], 1))
    # TripoSR is z-up and faces +x; glTF/OBJ viewers expect y-up facing +z.
    mesh.apply_transform(trimesh.transformations.rotation_matrix(-np.pi / 2, [1, 0, 0]))
    mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [0, 1, 0]))
    mesh.export(a.out)
    times["export_s"] = time.time() - t
    times = {k: round(v, 1) for k, v in times.items()}
    print(json.dumps({"vertices": len(mesh.vertices), "faces": len(mesh.faces), "watertight": bool(mesh.is_watertight),
                      "extent": [round(float(x), 3) for x in mesh.extents], "device": device, "total_s": round(time.time() - t0, 1), **times}))


if __name__ == "__main__":
    main()
