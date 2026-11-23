"""One image to a 3D mesh with TripoSR (MIT, Tripo AI & Stability AI). Run by media_backend/image3d.py.

    python -I triposr.py IMAGE OUT.glb --code DIR --weights DIR [--mc 256] [--no-rembg] [--fg 0.85]
                         [--max-faces N] [--device auto|cpu] [--models DIR] [--texture 1024]

--code holds TripoSR's `tsr` package, --weights its config.yaml + model.ckpt. Mesh extraction uses
scikit-image's marching cubes (no torchmcubes build needed). --texture N unwraps the mesh (xatlas) and
bakes TripoSR's colour field into an N x N texture (bake.py) instead of vertex colours. Prints one JSON line with stats; exits 3 with
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


def dense_volume(density, n: int, threshold: float):
    """Density on an n^3 grid, coarse to fine: a coarse grid finds the cells the surface passes through and
    only those (plus a one-cell margin) are evaluated at full resolution; elsewhere the coarse values are
    interpolated, which stay on one side of the threshold so they add no surface. About 10x fewer queries
    than the full grid at 256. Returns (volume as a numpy array, number of points queried)."""
    import torch
    import torch.nn.functional as F

    m = min(n, max(64, n // 4))
    if m >= n:
        lin = torch.linspace(0, 1, n)
        g = torch.stack(torch.meshgrid(lin, lin, lin, indexing="ij"), -1).reshape(-1, 3)
        return density(g).reshape(n, n, n).numpy(), n ** 3
    lc = torch.linspace(0, 1, m)
    coarse = density(torch.stack(torch.meshgrid(lc, lc, lc, indexing="ij"), -1).reshape(-1, 3)).reshape(m, m, m)
    inside = (coarse > threshold).float()[None, None]
    any_in = F.max_pool3d(inside, 2, 1)[0, 0] > 0             # per coarse cell: some corner inside...
    all_in = -F.max_pool3d(-inside, 2, 1)[0, 0] > 0           # ...and every corner inside
    cells = (any_in & ~all_in).float()[None, None]
    cells = F.max_pool3d(cells, 3, 1, 1)[0, 0] > 0             # one-cell margin for thin parts
    vol = F.interpolate(coarse[None, None], size=(n, n, n), mode="trilinear", align_corners=True)[0, 0]
    ci = torch.clamp((torch.arange(n) * (m - 1) / (n - 1)).floor().long(), max=m - 2)
    mask = cells[ci][:, ci][:, :, ci]
    idx = torch.nonzero(mask)
    if len(idx):
        vol[idx[:, 0], idx[:, 1], idx[:, 2]] = density(idx.float() / (n - 1))
    return vol.numpy(), m ** 3 + len(idx)


def pick_threads(torch) -> int:
    """Some machines (busy or virtual ones) make multi-threaded matrix maths collapse, e.g. 30 s instead of
    0.2 s for one of TripoSR's layers. Time that layer and use fewer threads when it is pathologically slow."""
    if os.environ.get("IMAGE3D_THREADS"):
        torch.set_num_threads(max(1, int(os.environ["IMAGE3D_THREADS"])))
        return torch.get_num_threads()
    x, w = torch.randn(512, 4096), torch.randn(1024, 4096)
    x[:8] @ w.t()   # warm up
    best, n = None, torch.get_num_threads()
    while n >= 1:
        torch.set_num_threads(n)
        t = time.time()
        x @ w.t()
        dt = time.time() - t
        if best is None or dt < best[0]:
            best = (dt, n)
        if dt < 0.4:   # about 10 GFLOPS; a healthy CPU does this in well under 0.1 s
            break
        n //= 2
    torch.set_num_threads(best[1])
    return best[1]


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
    ap.add_argument("--texture", type=int, default=0)
    a = ap.parse_args()

    missing = sorted({pip for mod, pip in NEEDS.items() if importlib.util.find_spec(mod) is None})
    if a.max_faces and importlib.util.find_spec("fast_simplification") is None:
        missing.append("fast-simplification")
    if a.texture and importlib.util.find_spec("xatlas") is None:
        missing.append("xatlas")
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
    if device == "cpu":
        pick_threads(torch)

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

    def density(pts01):   # points in [0, 1]^3 -> TripoSR density
        with torch.no_grad():
            d = model.renderer.query_triplane(model.decoder, scale_tensor(pts01, (0, 1), (-radius, radius)).to(device), code)["density_act"]
        return d.reshape(-1).float().cpu()

    vol, queried = dense_volume(density, n, a.threshold)
    times["grid_points"] = queried
    if vol.max() <= a.threshold:
        raise SystemExit("the model found no solid object in the picture (try remove_background or a plainer photo)")
    verts, faces, _, _ = marching_cubes(vol, a.threshold, gradient_direction="descent")
    verts = verts / (n - 1) * 2 * radius - radius
    mesh = trimesh.Trimesh(verts, faces, process=True)
    if mesh.volume < 0:   # normals must point outwards, or viewers cull the outside and show the inside
        mesh.invert()
    parts = mesh.split(only_watertight=False)
    if len(parts) > 1:   # drop floating specks
        mesh = trimesh.util.concatenate([p for p in parts if len(p.faces) >= 0.02 * len(mesh.faces)] or [max(parts, key=lambda p: len(p.faces))])
    times["mesh_s"] = time.time() - t

    t = time.time()
    if a.max_faces and len(mesh.faces) > a.max_faces:
        mesh = mesh.simplify_quadric_decimation(face_count=a.max_faces)
    extra: dict = {}

    def colour(pts):   # TripoSR's colour field at points in its own space, RGB 0..1
        outs = []
        with torch.no_grad():
            for i in range(0, len(pts), 200_000):
                q = torch.as_tensor(np.asarray(pts[i:i + 200_000]), dtype=torch.float32, device=device)
                outs.append(model.renderer.query_triplane(model.decoder, q, code)["color"].clamp(0, 1).cpu().numpy())
        return np.concatenate(outs) if outs else np.zeros((0, 3), np.float32)

    if a.texture:   # unwrap and bake the colour field into a texture (sharper than vertex colours, game-friendly)
        import importlib.util as iu

        spec = iu.spec_from_file_location("builder_bake", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bake.py"))
        bake = iu.module_from_spec(spec)
        spec.loader.exec_module(bake)
        v, f, uv, img, cov = bake.bake(mesh.vertices, mesh.faces, a.texture, colour_fn=colour)
        mesh = bake.textured_mesh(v, f, uv, img)
        extra.update({"texture": a.texture, "texture_coverage": round(cov, 3)})
    else:   # colours at the final vertices, so decimation keeps them
        rgb = (colour(mesh.vertices) * 255).astype(np.uint8)
        mesh.visual = trimesh.visual.ColorVisuals(mesh, vertex_colors=np.concatenate([rgb, np.full((len(rgb), 1), 255, np.uint8)], 1))
    # TripoSR is z-up and faces +x; glTF/OBJ viewers expect y-up facing +z.
    mesh.apply_transform(trimesh.transformations.rotation_matrix(-np.pi / 2, [1, 0, 0]))
    mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [0, 1, 0]))
    mesh.export(a.out)
    times["export_s"] = time.time() - t
    times = {k: round(v, 1) for k, v in times.items()}
    times["threads"] = torch.get_num_threads()
    print(json.dumps({"vertices": len(mesh.vertices), "faces": len(mesh.faces), "watertight": bool(mesh.is_watertight),
                      "extent": [round(float(x), 3) for x in mesh.extents], "device": device, "total_s": round(time.time() - t0, 1), **times, **extra}))


if __name__ == "__main__":
    main()
