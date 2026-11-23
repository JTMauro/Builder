"""Pictures: finishing stills and making comics from scenes.

- finish_picture: a still's finishing pass: optional background removal (remove_background), a colour grade
  (the film looks of finish.LOOKS, plus a few for photos), sharpening, vignette, film grain, then crop or pad
  to a print size (A4, A3, 4x6, 5x7, 8x10, square) at 300 dpi, upscaled with Lanczos when needed; PNG or JPG.
- make_comic: a scene (scene_video params) as a comic page / storyboard: the chosen beats rendered as stills at
  their panel moments (no voices needed), laid out in a grid with speech bubbles whose tails point at the
  speaker's head (studio.html reports each head's screen position), a title, PNG and optional PDF.
Text never goes into an ffmpeg filter string: grades are fixed filter presets, and lettering is drawn with PIL.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import storage
from .ops import FONT, OpError, OpResult, _ffmpeg, _guarded, _src, _stem, operation, run_operation

FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
# colour grades for stills: the film looks plus a few photo ones (ffmpeg filter presets, no user text)
IMAGE_LOOKS = {
    "natural": "eq=contrast=1.03:saturation=1.04",
    "vivid": "eq=contrast=1.08:saturation=1.3,curves=master='0/0 0.25/0.22 0.75/0.8 1/1'",
    "portrait": "colortemperature=temperature=5600:mix=0.4,eq=contrast=0.98:saturation=0.95:gamma=1.03",
    "matte": "curves=master='0/0.08 0.5/0.5 1/0.94',eq=saturation=0.9",
    "golden": "colortemperature=temperature=4600:mix=0.55,eq=saturation=1.08:contrast=1.04",
    "teal_orange": "colorbalance=rs=-0.08:bs=0.1:rh=0.1:bh=-0.1,eq=saturation=1.1",
}
# print sizes in inches (portrait); landscape pictures turn them round
PRINT = {"A4": (8.27, 11.69), "A3": (11.69, 16.54), "4x6": (4, 6), "5x7": (5, 7), "8x10": (8, 10), "square": (8, 8)}


def _looks() -> dict[str, str]:
    from .finish import LOOKS

    return {**{k: v for k, v in LOOKS.items() if k != "none"}, **IMAGE_LOOKS, "none": "null"}


def _pil():
    try:
        from PIL import Image  # noqa: F401
    except ImportError as e:
        raise OpError("This needs Pillow: pip install pillow") from e
    import PIL

    return PIL


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    if len(c) != 6 or any(x not in "0123456789abcdefABCDEF" for x in c):
        raise OpError("colours are written like #ffffff")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


class FinishPictureParams(BaseModel):
    file_id: str = Field(description="A picture (PNG/JPG/WebP) or a frame you exported")
    remove_background: bool = Field(False, description="Cut the subject out first (rembg); transparent in PNG, the background colour in JPG")
    look: Literal["none", "film", "warm", "cool", "noir", "vintage", "bleach", "natural", "vivid", "portrait", "matte", "golden", "teal_orange"] = "natural"
    sharpen: float = Field(0.4, ge=0, le=2, description="0 = none, 0.4 gentle, 1+ strong")
    vignette: float = Field(0.0, ge=0, le=1)
    grain: float = Field(0.0, ge=0, le=1, description="Film grain amount")
    size: Literal["original", "A4", "A3", "4x6", "5x7", "8x10", "square"] = Field("original", description="Print size at the dpi (300 = photo quality)")
    orientation: Literal["auto", "portrait", "landscape"] = "auto"
    fit: Literal["crop", "pad"] = Field("crop", description="crop fills the page (trims the edges); pad keeps the whole picture with a border")
    focus: float = Field(0.5, ge=0, le=1, description="Where to keep when cropping: 0 = left/top, 1 = right/bottom")
    dpi: int = Field(300, ge=72, le=600)
    background: str = Field("#ffffff", description="Border / background colour (#rrggbb)")
    format: Literal["png", "jpg"] = "png"


def _target(size: str, orient: str, dpi: int, w: int, h: int) -> tuple[int, int] | None:
    if size == "original":
        return None
    a, b = PRINT[size]
    landscape = orient == "landscape" or (orient == "auto" and w > h)
    a, b = (b, a) if landscape else (a, b)
    return round(a * dpi), round(b * dpi)


@operation("finish_picture", FinishPictureParams, "Finish a still for sharing or print: optional background removal, a colour grade (film, warm, cool, noir, vintage, vivid, portrait, matte, golden, teal_orange...), sharpening, vignette, film grain, and crop or pad to a print size (A4, A3, 4x6, 5x7, 8x10, square) at 300 dpi with a high-quality upscale. PNG or JPG.")
def finish_picture(p: FinishPictureParams) -> OpResult:
    PIL = _pil()
    from PIL import Image

    bg = _hex(p.background)
    made, src = [], _src(p.file_id)
    if p.remove_background:
        cut = run_operation("remove_background", {"file_id": p.file_id}).outputs[0].id
        made.append(cut)
        src = storage.path_of(cut)
    work = Path(tempfile.mkdtemp(prefix="finpic-"))
    try:
        im = Image.open(src)
        im.load()
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        im = im.convert("RGBA" if has_alpha else "RGB")
        alpha = im.getchannel("A") if has_alpha else None
        im.convert("RGB").save(work / "in.png")
        # the colour grade, sharpening, vignette and grain: fixed ffmpeg presets on the RGB picture
        chain = [_looks()[p.look]]
        if p.sharpen > 0:
            chain.append(f"unsharp=5:5:{p.sharpen:.2f}:5:5:0")
        if p.vignette > 0:
            chain.append(f"vignette=angle={0.25 + 0.55 * p.vignette:.3f}")
        if p.grain > 0:
            chain.append(f"noise=alls={int(3 + p.grain * 16)}:allf=u")
        _ffmpeg("-i", str(work / "in.png"), "-vf", ",".join(chain), "-frames:v", "1", str(work / "graded.png"))
        out = Image.open(work / "graded.png").convert("RGB")
        if alpha is not None:
            out.putalpha(alpha)
        w, h = out.size
        tgt = _target(p.size, p.orientation, p.dpi, w, h)
        upscaled = False
        if tgt:
            tw, th = tgt
            if p.fit == "crop":
                k = max(tw / w, th / h)
                nw, nh = max(tw, round(w * k)), max(th, round(h * k))
                big = out.resize((nw, nh), Image.LANCZOS)
                x, y = round((nw - tw) * p.focus), round((nh - th) * p.focus)
                out = big.crop((x, y, x + tw, y + th))
            else:
                k = min(tw / w, th / h)
                nw, nh = round(w * k), round(h * k)
                page = Image.new("RGBA" if alpha is not None else "RGB", (tw, th), (*bg, 0) if alpha is not None and p.format == "png" else bg)
                page.paste(out.resize((nw, nh), Image.LANCZOS), ((tw - nw) // 2, (th - nh) // 2))
                out = page
            upscaled = k > 1
        if p.format == "jpg" and out.mode == "RGBA":
            flat = Image.new("RGB", out.size, bg)
            flat.paste(out, mask=out.getchannel("A"))
            out = flat
        final = out
        name = f"{_stem(p.file_id)}_{p.size if p.size != 'original' else 'finished'}.{p.format}"

        def build(path: Path) -> None:
            if p.format == "jpg":
                final.save(path, "JPEG", quality=94, subsampling=0, dpi=(p.dpi, p.dpi))
            else:
                final.save(path, "PNG", dpi=(p.dpi, p.dpi))

        res = _guarded("finish_picture", [p.file_id, *made], name, build,
                       data={"width": final.size[0], "height": final.size[1], "dpi": p.dpi, "size": p.size, "look": p.look,
                             "upscaled": upscaled, "transparent": final.mode == "RGBA", "pillow": PIL.__version__})
        if tgt:
            res.data["inches"] = [round(final.size[0] / p.dpi, 2), round(final.size[1] / p.dpi, 2)]
        return res
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- comics

class ComicParams(BaseModel):
    scene: dict[str, Any] = Field(description="scene_video parameters (stage, cast, set, beats with say / move / shot...)")
    panels: list[int | dict] = Field(default_factory=list, max_length=24,
                                     description='Which beats become panels (0-based beat numbers, or {"beat": n, "caption": "Meanwhile..."}); default: every beat with a line (or every beat)')
    title: str | None = Field(None, max_length=120)
    columns: int = Field(2, ge=1, le=4)
    panel_size: Literal["640x360", "960x540", "1280x720", "720x720", "540x720"] = "960x540"
    look: Literal["none", "film", "warm", "cool", "noir", "vintage", "bleach", "natural", "vivid", "matte", "golden"] = "none"
    pdf: bool = Field(True, description="Also write a PDF of the page")
    background: str = Field("#f4f1ea", description="Page colour (#rrggbb)")


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    try:
        return ImageFont.truetype(FONT if bold else FONT_REGULAR, size)
    except OSError:
        return ImageFont.load_default()


def _wrap(draw, text: str, font, width: int) -> list[str]:
    """Wrap text to a pixel width (by words; very long words are split)."""
    lines: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split():
            while draw.textlength(word, font=font) > width and len(word) > 1:
                cut = max(1, int(len(word) * width / draw.textlength(word, font=font)) - 1)
                if cur:
                    lines.append(cur)
                    cur = ""
                lines.append(word[:cut] + "-")
                word = word[cut:]
            trial = f"{cur} {word}".strip()
            if draw.textlength(trial, font=font) <= width:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return [x for x in lines if x] or [""]


def _bubble(img, text: str, head: tuple[float, float] | None, side_hint: float = 0.5) -> None:
    """A speech bubble with wrapped text near the top of the panel, its tail pointing at the speaker's head."""
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    W, H = img.size
    size = max(14, round(H * 0.052))
    font = _font(size, bold=True)
    maxw = int(W * 0.46)
    lines = _wrap(d, text, font, maxw)
    if len(lines) > 5:   # long line: smaller lettering
        size = max(12, round(size * 0.8))
        font = _font(size, bold=True)
        lines = _wrap(d, text, font, int(W * 0.56))
    lh = round(size * 1.22)
    tw = max(d.textlength(x, font=font) for x in lines)
    pad = round(size * 0.7)
    bw, bh = int(tw + 2 * pad), int(lh * len(lines) + 2 * pad - (lh - size))
    hx, hy = (head if head else (side_hint * W, H * 0.4))
    hx, hy = min(max(hx, 0), W), min(max(hy, 0), H)
    # above the head when there's room, otherwise beside it; keep it inside the panel
    bx = min(max(hx - bw / 2 + (bw * 0.25 if hx < W / 2 else -bw * 0.25), 8), W - bw - 8)
    by = max(8, min(hy - bh - size * 2.2, H * 0.45 - bh))
    by = max(8, by)
    box = [bx, by, bx + bw, by + bh]
    # tail: from the bubble's lower edge towards the head, stopping short of it
    base_x = min(max(hx, bx + bw * 0.2), bx + bw * 0.8)
    tip = (base_x + (hx - base_x) * 0.75, by + bh + max(size * 0.8, (hy - (by + bh)) * 0.7))
    tail = [(base_x - size * 0.45, by + bh - 2), (base_x + size * 0.45, by + bh - 2), tip]
    r = min(bh // 2, round(size * 0.9))
    d.polygon(tail, fill="white", outline="black", width=3)
    d.rounded_rectangle(box, radius=r, fill="white", outline="black", width=3)
    d.polygon([(tail[0][0] + 3, tail[0][1] - 1), (tail[1][0] - 3, tail[1][1] - 1), (tip[0], tip[1] - 5)], fill="white")
    y = by + pad - round(size * 0.1)
    for ln in lines:
        d.text((bx + (bw - d.textlength(ln, font=font)) / 2, y), ln, font=font, fill="black")
        y += lh


def _caption_box(img, text: str) -> None:
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    W, H = img.size
    size = max(12, round(H * 0.04))
    font = _font(size)
    lines = _wrap(d, text, font, int(W * 0.6))
    lh = round(size * 1.25)
    tw = max(d.textlength(x, font=font) for x in lines)
    box = [6, 6, 6 + tw + size, 6 + lh * len(lines) + size * 0.6]
    d.rectangle(box, fill=(255, 236, 150), outline="black", width=2)
    for i, ln in enumerate(lines):
        d.text((6 + size / 2, 6 + size * 0.3 + i * lh), ln, font=font, fill="black")


@operation("make_comic", ComicParams, "Turn a scene into a comic page or storyboard: chosen beats rendered as stills (no voices needed), laid out in a grid with speech bubbles pointing at each speaker's head, optional caption boxes and a title; PNG plus PDF. Takes scene_video params.")
def make_comic(p: ComicParams) -> OpResult:
    _pil()
    from PIL import Image, ImageDraw

    from . import scene as sc
    from .characters import resolve_cast

    page_bg = _hex(p.background)
    params = sc.SceneParams.model_validate({**p.scene, "fps": 12})
    params.cast = resolve_cast(params.cast)
    stage = sc.stages_dir() / params.stage
    if not (stage / "index.html").exists():
        raise OpError(f"no stage called {params.stage!r} (have: {', '.join(sc.list_stages()) or 'none'})")
    w, h = (int(x) for x in p.panel_size.split("x"))
    beats, lines, _, _, duration = sc.build_timeline(params, voiced=False)
    by_key = {ln["key"]: ln for ln in lines}
    # which beats, and the moment in each: halfway through its line, or a little into its move
    chosen = []
    for x in p.panels or [i for i, b in enumerate(params.beats) if b.say] or list(range(len(params.beats))):
        i, cap = (x, None) if isinstance(x, int) else (int(x.get("beat", -1)), x.get("caption"))
        if not 0 <= i < len(beats):
            raise OpError(f"panel beat {i} doesn't exist (the scene has {len(beats)} beats, counted from 0)")
        b = beats[i]
        ln = by_key.get(b.get("say", ""))
        t = b["t"] + (ln["duration"] * 0.45 if ln else min(params.beats[i].hold, 1.6) * 0.6)
        chosen.append({"beat": i, "t": round(min(t, duration - 0.1), 3), "caption": cap, "text": ln["text"] if ln else None,
                       "actor": b.get("actor") or (params.cast[0].id if params.cast else "a")})
    if not chosen:
        raise OpError("no panels to draw")
    frames_at = sorted({round(c["t"] * params.fps) + 1 for c in chosen})
    work = Path(tempfile.mkdtemp(prefix="comic-"))
    try:
        scene, _ = sc.studio_scene(params, stage, beats, lines, w, h, duration)
        scene.update({"keep": frames_at, "probe": [c["t"] for c in chosen]})
        frames, events = sc.render_stage(stage, scene, work)
        probes = [e for e in events if "heads" in e]
        have = sorted(frames.glob("*.png"))
        if not have:
            raise OpError("the renderer made no pictures")
        panels = []
        for c in chosen:
            n = round(c["t"] * params.fps) + 1
            f = frames / f"{n:06d}.png"
            if not f.exists():
                f = min(have, key=lambda q: abs(int(q.stem) - n))
            img = Image.open(f).convert("RGB")
            if img.size != (w, h):
                img = img.resize((w, h), Image.LANCZOS)
            if p.look != "none":
                img.save(work / "g_in.png")
                _ffmpeg("-i", str(work / "g_in.png"), "-vf", _looks()[p.look], "-frames:v", "1", str(work / "g_out.png"))
                img = Image.open(work / "g_out.png").convert("RGB")
            pr = min(probes, key=lambda e: abs(e["t"] - c["t"])) if probes else None
            hp = (pr or {}).get("heads", {}).get(c["actor"])
            head = (hp[0] * w, hp[1] * h) if hp and (len(hp) < 3 or hp[2]) else None
            if c["text"]:
                _bubble(img, c["text"], head)
            if c["caption"]:
                _caption_box(img, str(c["caption"]))
            panels.append(img)
            c["head"] = [round(head[0]), round(head[1])] if head else None
        # the page: title, then the panels in a grid with gutters and black borders
        cols = min(p.columns, len(panels))
        rows = -(-len(panels) // cols)
        gut, margin, border = max(12, w // 40), max(24, w // 18), 4
        title_h = round(w * 0.16) if p.title else 0
        PW = margin * 2 + cols * w + (cols - 1) * gut
        PH = margin * 2 + title_h + rows * h + (rows - 1) * gut
        page = Image.new("RGB", (PW, PH), page_bg)
        d = ImageDraw.Draw(page)
        if p.title:
            ts = round(title_h * 0.55)
            font = _font(ts, bold=True)
            while d.textlength(p.title, font=font) > PW - 2 * margin and ts > 16:
                ts -= 4
                font = _font(ts, bold=True)
            d.text(((PW - d.textlength(p.title, font=font)) / 2, margin + (title_h - ts) / 2 - ts * 0.15), p.title, font=font, fill=(20, 20, 20))
        for k, img in enumerate(panels):
            x = margin + (k % cols) * (w + gut)
            y = margin + title_h + (k // cols) * (h + gut)
            page.paste(img, (x, y))
            d.rectangle([x - border // 2, y - border // 2, x + w + border // 2 - 1, y + h + border // 2 - 1], outline="black", width=border)
        name = (p.title or "comic").replace(" ", "_")[:40]
        res = _guarded("make_comic", [], f"{storage.safe_name(name)}.png", lambda out: page.save(out, "PNG", dpi=(150, 150)),
                       data={"panels": chosen, "width": PW, "height": PH, "stage": params.stage})
        if p.pdf:
            res.outputs += _guarded("make_comic", [res.outputs[0].id], f"{storage.safe_name(name)}.pdf",
                                    lambda out: page.save(out, "PDF", resolution=150)).outputs
        return res
    finally:
        shutil.rmtree(work, ignore_errors=True)

