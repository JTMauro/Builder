"""One photo or picture of an object to a 3D model (free, local): TripoSR.

image_to_3d cuts the object out (rembg), runs TripoSR (MIT, Tripo AI & Stability AI) to get a 3D field,
turns it into a mesh with marching cubes, optionally reduces the triangle count, and writes a .glb or .obj
with vertex colours. Works best on one object on a plain background, seen from slightly above; the back is
the model's guess. CPU works (about 1.5 minutes at resolution 256, 50 s at 128, on 4 busy cores); a CUDA GPU
is much faster. Only the cells the surface passes through are sampled at full resolution.

Needs `pip install "builder[image3d]"` (PyTorch; CPU wheels: --extra-index-url
https://download.pytorch.org/whl/cpu). On first use it downloads into the models folder: TripoSR's code at
a pinned commit (about 0.1 MB), its weights (about 1.7 GB) and rembg's u2net (about 170 MB).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .config import NO_WINDOW
from .ops import OpError, OpResult, _guarded, _src, _stem, operation

WORKER = Path(__file__).resolve().parents[1] / "tools" / "image3d" / "triposr.py"
TRIPOSR_COMMIT = "107cefdc244c39106fa830359024f6a2f1c78871"
CODE_URL = f"https://raw.githubusercontent.com/VAST-AI-Research/TripoSR/{TRIPOSR_COMMIT}"
CODE_FILES = {   # TripoSR's tsr package (MIT) without bake_texture.py, which needs an OpenGL context
    "tsr/models/isosurface.py": "94c9a2ec27f8fe3c2fb57558ef28be253eb77074d2c171d49a41cf73d95d6f5e",
    "tsr/models/nerf_renderer.py": "afe8c12c964078b2c6bb3922d2e30969aa59f7edbacb8cb73d2c3df839dc785f",
    "tsr/models/network_utils.py": "b3d48e86be9ca3f3abc1bfd774f640fd350e4365843d804e2a3145e7692e1419",
    "tsr/models/tokenizers/image.py": "f6da23124dfabe6a1f8d0d27693d0cece6574de68be15487a77781b6a9d73195",
    "tsr/models/tokenizers/triplane.py": "44811fbd53013d6f49e229ecfd0717f89dd8ebeb71608aeb1f8e5ed1d5e8e378",
    "tsr/models/transformer/attention.py": "347c1a707257cb4eb664ebe9ecd16db7b6006131e74925c049245d2c0101ba7e",
    "tsr/models/transformer/basic_transformer_block.py": "ced8ce4c5c5bbc13368130bc15dee1717cc6bc7f35699f64d03aeec185b1198e",
    "tsr/models/transformer/transformer_1d.py": "fd83a71d59d04ef0a45dbadcc63f9d56d354ebc011a40f5589eb1786a1dd15f4",
    "tsr/system.py": "7aec93e421394ad7c746b6d63a0189532b57b26af9a199fc75ac3bb7c993c94f",
    "tsr/utils.py": "d9d1a645fef7ea489615bff3471fd01a4941a2c0e138c5dff5daae619563279b",
    "LICENSE": "ade0a66629bdd7e01e46b3296b3851cff0fd27989bca53da470ad6e96ed620fb",
}
WEIGHTS_REPO, WEIGHTS_REVISION = "stabilityai/TripoSR", "5b521936b01fbe1890f6f9baed0254ab6351c04a"   # MIT, about 1.7 GB
INSTALL = ('pip install "builder[image3d]" --extra-index-url https://download.pytorch.org/whl/cpu  '
           '(or: pip install torch omegaconf einops transformers trimesh rembg scikit-image fast-simplification)')
PACKAGES = {"torch": "torch", "omegaconf": "omegaconf", "einops": "einops", "transformers": "transformers",
            "trimesh": "trimesh", "rembg": "rembg", "skimage": "scikit-image", "imageio": "imageio",
            "huggingface_hub": "huggingface_hub"}
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def missing_packages() -> list[str]:
    import importlib.util

    return sorted(pip for mod, pip in PACKAGES.items() if importlib.util.find_spec(mod) is None)


def ensure_model() -> tuple[Path, Path]:
    """TripoSR's code and weights in the models folder (downloaded once). Returns (code_dir, weights_dir)."""
    from .voice import _fetch, models_dir

    root = models_dir() / "triposr"
    code = root / f"code-{TRIPOSR_COMMIT[:10]}"
    if not all((code / f).exists() for f in CODE_FILES):
        tmp = root / "code.part"
        shutil.rmtree(tmp, ignore_errors=True)
        for rel, sha in CODE_FILES.items():   # pinned commit, each file checked against its hash
            _fetch(f"{CODE_URL}/{rel}", tmp / rel)
            if hashlib.sha256((tmp / rel).read_bytes()).hexdigest() != sha:
                shutil.rmtree(tmp, ignore_errors=True)
                raise OpError(f"TripoSR's {rel} didn't match the expected checksum")
        shutil.rmtree(code, ignore_errors=True)
        tmp.rename(code)
    weights = root / "weights"
    if not all((weights / f).exists() for f in ("config.yaml", "model.ckpt")):
        try:
            from huggingface_hub import hf_hub_download
        except ImportError as e:
            raise OpError(f"image_to_3d needs huggingface_hub: {INSTALL}") from e
        try:
            for f in ("config.yaml", "model.ckpt"):
                hf_hub_download(WEIGHTS_REPO, f, revision=WEIGHTS_REVISION, local_dir=weights)
        except Exception as e:  # network, disk
            raise OpError(f"Couldn't download TripoSR's weights from huggingface.co/{WEIGHTS_REPO}: {e}") from e
    return code, weights


class ImageTo3DParams(BaseModel):
    file_id: str = Field(description="Image of one object (png/jpg/webp); a transparent PNG skips background removal")
    resolution: int = Field(256, ge=32, le=512, description="Marching cubes grid size: 128 is quick and rough, 256 detailed, 384+ slow")
    remove_background: bool = Field(True, description="Cut the object out first (rembg). Turn off only for an object already centred on a plain grey background")
    foreground_ratio: float = Field(0.85, ge=0.5, le=1.0, description="How much of the frame the object fills after cutting out")
    format: Literal["glb", "obj"] = "glb"
    max_faces: int | None = Field(None, ge=500, le=2_000_000, description="Reduce the mesh to about this many triangles (e.g. 20000 for web/games); colours are kept")
    device: Literal["auto", "cpu"] = Field("auto", description="auto uses a CUDA GPU when there is one")
    texture: bool = Field(False, description="Bake the colours into a UV texture (xatlas unwrap) instead of vertex colours: sharper colours, a normal textured GLB for games and engines. Best with max_faces (e.g. 20000)")
    texture_size: int = Field(1024, ge=256, le=4096, description="Texture width/height in pixels with texture=true")


@operation("image_to_3d", ImageTo3DParams, "Turn a picture of one object into a 3D model (.glb or .obj with vertex colours), free and local with TripoSR: background removal, single-image reconstruction, marching cubes, optional decimation, optional baked UV texture. Best with one object on a plain background; the hidden back is guessed. 1-2 minutes on CPU at resolution 256 (first use downloads ~1.9 GB); start with wait=false.")
def image_to_3d(p: ImageTo3DParams) -> OpResult:
    src = _src(p.file_id)
    if src.suffix.lower() not in IMAGE_TYPES:
        raise OpError(f"image_to_3d needs an image ({', '.join(sorted(IMAGE_TYPES))}), not {src.suffix or 'a file without extension'}")
    worker = Path(os.environ.get("IMAGE3D_WORKER") or WORKER)
    if p.texture and p.format != "glb":
        raise OpError("texture=true needs format glb (an OBJ would need a separate .mtl and image)")
    if not os.environ.get("IMAGE3D_WORKER"):
        missing = missing_packages() + (["xatlas"] if p.texture and importlib.util.find_spec("xatlas") is None else [])
        if missing:
            raise OpError(f"image_to_3d needs {', '.join(missing)}: {INSTALL}")
    from .voice import models_dir

    code, weights = ensure_model()
    args = [sys.executable, "-I", str(worker), str(src), "OUT", "--code", str(code), "--weights", str(weights),
            "--models", str(models_dir()), "--mc", str(p.resolution), "--fg", str(p.foreground_ratio), "--device", p.device]
    if not p.remove_background:
        args.append("--no-rembg")
    if p.max_faces:
        args += ["--max-faces", str(p.max_faces)]
    if p.texture:
        args += ["--texture", str(p.texture_size)]
    stats: dict = {}

    def build(out: Path) -> None:
        proc = subprocess.run([str(out) if x == "OUT" else x for x in args], capture_output=True, text=True, check=False, **NO_WINDOW)
        last = (proc.stdout.strip().splitlines() or ["{}"])[-1]
        try:
            info = json.loads(last)
        except ValueError:
            info = {}
        if proc.returncode == 3 and info.get("missing"):
            raise OpError(f"image_to_3d needs {', '.join(info['missing'])}: {INSTALL}")
        if proc.returncode != 0 or not out.exists():
            raise OpError("TripoSR failed:\n" + "\n".join(ln[:400] for ln in (proc.stderr or proc.stdout).strip().splitlines()[-8:]))
        stats.update(info)

    res = _guarded("image_to_3d", [p.file_id], f"{_stem(p.file_id)}_3d.{p.format}", build)
    res.data = {"engine": "TripoSR (MIT)", "resolution": p.resolution, **stats,
                "how_to_open": "Drag the .glb into Blender (File > Import > glTF), https://gltf-viewer.donmccurdy.com or a three.js scene; "
                               + ("colours are a baked UV texture." if p.texture else "colours are vertex colours (in Blender: material > Color Attribute).")}
    return res

