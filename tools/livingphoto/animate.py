"""Underwater living photos + swim-through loops. Every motion is periodic in the loop length (seamless)."""
import sys, math, subprocess, os, numpy as np, cv2
from PIL import Image, ImageDraw
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenes_underwater as scenes
from refine2 import refine2

def polymask(W, H, poly, s, dx=0, dy=0):
    m = Image.new('L', (W, H), 0); ImageDraw.Draw(m).polygon([(x*s + dx, y*s + dy) for x, y in poly], fill=255)
    return np.asarray(m).astype(np.float32) / 255
def soft(m, sig): return cv2.GaussianBlur(m, (0, 0), max(sig, 0.5))
def load(name, W):
    img = Image.open(f'src/{name}.jpg').convert('RGB'); w0, h0 = img.size
    H = int(round(h0 * W / w0 / 2) * 2)
    return np.asarray(img.resize((W, H), Image.LANCZOS)).astype(np.float32) / 255

def refine(img, poly, s):
    """GrabCut from a rough outline -> clean alpha"""
    H, W = img.shape[:2]; m = polymask(W, H, poly, s) > 0.5
    k = max(3, int(6 * s))
    gc = np.full((H, W), cv2.GC_BGD, np.uint8)
    ring = cv2.dilate(m.astype(np.uint8), np.ones((k*3, k*3), np.uint8)) > 0
    gc[ring] = cv2.GC_PR_BGD; gc[m] = cv2.GC_PR_FGD
    gc[cv2.erode(m.astype(np.uint8), np.ones((k*2, k*2), np.uint8)) > 0] = cv2.GC_FGD
    try:
        bg = np.zeros((1, 65), np.float64); fg = np.zeros((1, 65), np.float64)
        cv2.grabCut((img * 255).astype(np.uint8)[..., ::-1].copy(), gc, None, bg, fg, 4, cv2.GC_INIT_WITH_MASK)
        a = ((gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD)).astype(np.float32)
        if a.sum() < 0.4 * m.sum(): a = m.astype(np.float32)
    except Exception:
        a = m.astype(np.float32)
    a = cv2.morphologyEx(a, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    return soft(a, 1.0)

def fill_hole(img, hole):
    """smooth water fill: normalised blur of everything outside the hole, so no creature colour leaks in"""
    H, W = img.shape[:2]
    keep = (1 - (hole > 0.02).astype(np.float32))
    out = np.zeros_like(img)
    for sig in (60, 30):
        num = cv2.GaussianBlur(img * keep[..., None], (0, 0), sig * W / 960)
        den = cv2.GaussianBlur(keep, (0, 0), sig * W / 960)[..., None]
        est = num / np.maximum(den, 1e-4)
        out = est if sig == 60 else np.where(den > 0.25, est, out)
    # borrow fine detail (fish, specks, texture) from a part of the picture outside the hole
    hp = img - cv2.GaussianBlur(img, (0, 0), 6 * W / 960)
    hb = (hole > 0.02).astype(np.float32); best = None
    lo = cv2.GaussianBlur(img, (0, 0), 12 * W / 960)
    hm_ = hb > 0; nh = hm_.sum() + 1
    cands = [(fx, fy) for fx in (-.6, -.45, -.3, -.2, -.12, .12, .2, .3, .45, .6) for fy in (-.12, -.06, 0, .06, .12)]
    for fx, fy in cands:
        dx, dy = int(fx * W), int(fy * H)
        over = (np.roll(hb, (dy, dx), (0, 1)) * hb).sum() / nh
        mis = np.abs(np.roll(lo, (dy, dx), (0, 1))[hm_] - out[hm_]).mean()
        sc = mis + 0.25 * over
        if best is None or sc < best[0]: best = (sc, dx, dy)
    hpm = hp * (1 - cv2.dilate(hb, np.ones((15, 15), np.uint8)))[..., None]
    donor = np.roll(hpm, (best[2], best[1]), (0, 1))
    out = np.clip(out + donor * 0.7, 0, 1)
    hm = soft(hole, 6 * W / 960)[..., None]
    return img * (1 - hm) + out * hm

def creature_fields(c, W, H, s, X, Y, mask=None):
    m = polymask(W, H, c['poly'], s) if mask is None else mask
    hx, hy = c['head'][0]*s, c['head'][1]*s; tx, ty = c['tail'][0]*s, c['tail'][1]*s
    ax, ay = hx - tx, hy - ty; L = math.hypot(ax, ay) + 1e-6; ux, uy = ax / L, ay / L; px, py = -uy, ux
    along = np.clip(((hx - X) * ux + (hy - Y) * uy) / L, 0, 1)
    perp = (X - hx) * px + (Y - hy) * py
    pn = perp / (np.abs(perp * (m > 0.5)).max() + 1e-6)
    return dict(m=m, ux=ux, uy=uy, px=px, py=py, along=along, pn=pn)

def creature_disp(c, k, th, ph, s, glide_on=True):
    glide = c.get('glide', 6) * s * math.sin(th + ph) if glide_on else 0
    bob = c.get('bob', 3) * s * math.sin(2 * th + ph * 0.5)
    ddx = glide * k['ux'] + bob * k['px']; ddy = glide * k['uy'] + bob * k['py']
    und = c.get('wave', 5) * s
    if und:
        ws = c.get('wave_speed', 2); ws = max(1, round(ws))   # integer -> loops cleanly
        wv = und * np.sin(ws * th - 2.4 * math.pi * k['along'] + ph) * k['along'] ** 1.6
        ddx = ddx + wv * k['px']; ddy = ddy + wv * k['py']
    fl = c.get('flap', 0) * s
    if fl:
        fs = max(1, round(c.get('flap_speed', 1)))
        fv = fl * np.sin(fs * th + ph) * np.abs(k['pn']) ** 1.5
        fdx, fdy = c.get('flap_dir', (0, 1))
        ddx = ddx + fv * fdx; ddy = ddy + fv * fdy
    return ddx, ddy

def render(name, cfg, out_mp4, W=960, fps=15, secs=6, swim=None, seed=1):
    img = load(name, W); H = img.shape[0]; s = W / 960.0; N = fps * secs
    Y, X = np.mgrid[0:H, 0:W].astype(np.float32)
    rng = np.random.default_rng(seed)
    travelers = []
    removed = set()
    if swim:
        hole = np.zeros((H, W), np.float32)
        for key, py_, scale, off, speed in swim:
            c = scenes.C[key]; srcimg = img if c['img'] == name else load(c['img'], W)
            pm = polymask(srcimg.shape[1], srcimg.shape[0], c['poly'], s)
            a, srccol = refine2(srcimg, pm)
            if c['img'] == name:
                removed.add(key); hole = np.maximum(hole, cv2.dilate(np.maximum(a, pm), np.ones((int(17*s), int(17*s)), np.uint8)))
            ys, xs = np.nonzero(a > 0.05); pad = int(20 * s)
            x0, x1 = max(xs.min() - pad, 0), min(xs.max() + pad, W); y0, y1 = max(ys.min() - pad, 0), min(ys.max() + pad, srcimg.shape[0])
            patch = np.dstack([srccol[y0:y1, x0:x1], a[y0:y1, x0:x1]])
            sc = scale
            if sc != 1: patch = cv2.resize(patch, (max(2, int(patch.shape[1]*sc)), max(2, int(patch.shape[0]*sc))), interpolation=cv2.INTER_AREA)
            ph_, pw_ = patch.shape[:2]
            PY, PX = np.mgrid[0:ph_, 0:pw_].astype(np.float32)
            cc = dict(c); cc['poly'] = [((x*s - x0) * sc / s, (y*s - y0) * sc / s) for x, y in c['poly']]
            cc['head'] = ((c['head'][0]*s - x0) * sc / s, (c['head'][1]*s - y0) * sc / s); cc['tail'] = ((c['tail'][0]*s - x0) * sc / s, (c['tail'][1]*s - y0) * sc / s)
            fld = creature_fields(cc, pw_, ph_, s, PX, PY, mask=patch[..., 3])
            dirx = 1 if c['head'][0] > c['tail'][0] else -1
            cy = (py_ * s) if py_ is not None else (y0 + y1) / 2
            ocx = (x0 + x1) / 2
            D = W + pw_
            u0 = ((ocx + pw_ / 2) / D) if dirx > 0 else ((W + pw_ / 2 - ocx) / D)
            if c['img'] != name: u0 = 0
            travelers.append(dict(c=cc, patch=patch, fld=fld, PX=PX, PY=PY, dirx=dirx, cy=cy, D=D, u0=u0 + off, speed=speed, ph=rng.uniform(0, 6.28), sc=sc))
        if hole.max() > 0: img = fill_hole(img, hole)
    cre = []; keep_still = np.zeros((H, W), np.float32)
    for c in cfg['creatures']:
        if any(scenes.C[k] is c for k in removed): continue
        k = creature_fields(c, W, H, s, X, Y); keep_still = np.maximum(keep_still, soft(k['m'], 6))
        k['sm'] = soft(k['m'], c.get('feather', 10) * s); cre.append((c, k, rng.uniform(0, 6.28)))
    sway = []
    for p in cfg['sway']:
        m = soft(polymask(W, H, p['poly'], s), 8 * s)
        ys_ = [y for _, y in p['poly']]; root = p.get('root', max(ys_)) * s; top = min(ys_) * s
        hgt = np.clip((root - Y) / max(root - top, 1), 0, 1) ** 1.3
        sway.append((m * hgt * (1 - keep_still), p['amp'] * s, rng.uniform(0, 6.28)))
    schools = []
    for p in cfg['schools']:
        m = soft(polymask(W, H, p['poly'], s), 14 * s)
        schools.append((m * (1 - keep_still * 0.7), p['dir'], p['amp'] * s, rng.uniform(0, 6.28)))
    smiles = []
    for sm in cfg.get('smiles', []):
        rad = sm['rad'] * s
        for (x, y), side in ((sm['l'], -1), (sm['r'], 1)):
            g = np.exp(-(((X - x*s) ** 2 + (Y - y*s) ** 2) / (2 * rad ** 2)))
            smiles.append((g, side, sm['amp'] * s))
    waves = []
    for wv in cfg.get('waves', []):
        m = soft(polymask(W, H, wv['poly'], s), 16 * s)
        waves.append((m, wv['pivot'][0]*s, wv['pivot'][1]*s, wv['deg'], wv.get('cycles', 1)))
    lum = img.mean(2); topw = np.clip(1.15 - Y / H, 0, 1)
    cw = cfg.get('caustics', 0.055) * np.clip(lum * 1.4, 0, 1) * (0.35 + 0.65 * topw)
    nb = cfg.get('bubbles', 24); ns = cfg.get('snow', 110); srcs = cfg['bubble_from']
    bub = []
    for i in range(nb):
        if srcs and i < nb * 0.6:
            sx, sy = srcs[i % len(srcs)]; x0 = sx * s + rng.normal(0, 6 * s); y0 = min(sy * s, H - 4)
        else: x0 = rng.uniform(0, W); y0 = rng.uniform(H * 0.3, H)
        bub.append((x0, y0, rng.uniform(1.6, 5.5) * s, int(rng.integers(1, 3)), rng.uniform(0, 1), rng.uniform(0, 6.28), rng.uniform(4, 10) * s))
    snow = [(rng.uniform(0, W), rng.uniform(0, H), rng.uniform(0.6, 1.6) * s, rng.uniform(0, 6.28), rng.uniform(0.15, 0.5)) for _ in range(ns)]
    enc = subprocess.Popen(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{W}x{H}', '-r', str(fps), '-i', '-',
                            '-c:v', 'libx264', '-preset', 'slow', '-crf', '18', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', out_mp4], stdin=subprocess.PIPE)
    for f in range(N):
        th = 2 * math.pi * f / N; env = 0.5 - 0.5 * math.cos(th)
        dx = 0.9 * s * np.sin(2 * th + Y / (23 * s) + 2 * np.sin(X / (61 * s)))
        dy = 0.7 * s * np.sin(3 * th + X / (31 * s) + 1.5 * np.sin(Y / (47 * s)))
        for m, A, ph in sway:
            dx += A * (np.sin(th + ph + X / (90 * s) + Y / (140 * s)) + 0.35 * np.sin(2 * th + X / (37 * s))) * m
        for m, d, A, ph in schools:
            g = A * np.sin(th + ph) + 0.35 * A * np.sin(3 * th + ph + X / (40 * s))
            dx += d[0] * g * m; dy += d[1] * g * m + 0.25 * A * np.sin(2 * th + ph + X / (25 * s)) * m
        for c, k, ph in cre:
            ddx, ddy = creature_disp(c, k, th, ph, s); dx += ddx * k['sm']; dy += ddy * k['sm']
        for g, side, A in smiles:
            e = env ** 1.5
            dx += side * 0.45 * A * e * g; dy += -A * e * g
        for m, px, py, deg, cyc in waves:
            phi = math.radians(deg) * (math.sin(cyc * th) if cyc > 1 else 1) * env
            cs, sn = math.cos(-phi), math.sin(-phi)
            sx = px + cs * (X - px) - sn * (Y - py); sy = py + sn * (X - px) + cs * (Y - py)
            dx += (X - sx) * m; dy += (Y - sy) * m
        fr = cv2.remap(img, X - dx, Y - dy, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
        # travellers
        for t in travelers:
            u = (f / N * t['speed'] + t['u0']) % 1.0
            pw_ = t['patch'].shape[1]; ph_ = t['patch'].shape[0]
            cx = (-pw_ / 2 + u * t['D']) if t['dirx'] > 0 else (W + pw_ / 2 - u * t['D'])
            c = t['c']; ddx, ddy = creature_disp(c, t['fld'], th * t['speed'] if False else th, t['ph'], s * t['sc'], glide_on=False)
            if np.ndim(ddx) == 0: ddx = np.full_like(t['PX'], ddx); ddy = np.full_like(t['PX'], ddy)
            wp = cv2.remap(t['patch'], t['PX'] - ddx, t['PY'] - ddy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
            cy = t['cy'] + 4 * s * math.sin(2 * th + t['ph'])
            ox, oy = int(round(cx - pw_ / 2)), int(round(cy - ph_ / 2))
            xa, ya = max(ox, 0), max(oy, 0); xb, yb = min(ox + pw_, W), min(oy + ph_, H)
            if xb <= xa or yb <= ya: continue
            sub = wp[ya - oy:yb - oy, xa - ox:xb - ox]; a = sub[..., 3:4]
            fr[ya:yb, xa:xb] = fr[ya:yb, xa:xb] * (1 - a) + sub[..., :3] * a
        u = X / (70 * s); v = Y / (55 * s)
        ca = np.sin(u + th) + np.sin(v * 1.3 - th) + np.sin((u + v) * 0.7 + 2 * th) + np.sin((u - v) * 0.9 - th)
        ca = np.clip(1 - np.abs(ca) / 1.1, 0, 1) ** 3
        fr = fr + (ca * cw)[..., None] * np.array([0.85, 1.0, 1.0], np.float32)
        fr = fr * (1 + cfg.get('rays', 0.05) * topw ** 2 * np.sin(X / (45 * s) + Y / (90 * s) + th))[..., None]
        im = Image.fromarray((np.clip(fr, 0, 1) * 255).astype(np.uint8)); dr = ImageDraw.Draw(im, 'RGBA')
        for x0, y0, r, phx, sp in snow:
            yy = y0 - sp * 20 * s * math.sin(th + phx); xx = (x0 + 25 * s * math.sin(th + phx)) % W
            a = int(60 + 50 * math.sin(th * 2 + phx)); dr.ellipse([xx - r, yy - r, xx + r, yy + r], fill=(220, 240, 255, a))
        for x0, y0, r, loops, off, ph, wob in bub:
            t_ = (f / N * loops + off) % 1.0; yy = y0 - t_ * (y0 + 20 * s); xx = x0 + wob * math.sin(2 * math.pi * (t_ * 3) + ph)
            rr = r * (1 + 0.3 * t_); fade = min(1, t_ * 6, (1 - t_) * 6)
            dr.ellipse([xx - rr, yy - rr, xx + rr, yy + rr], outline=(235, 250, 255, int(200 * fade)), width=max(1, int(rr / 3)), fill=(200, 235, 255, int(30 * fade)))
            dr.ellipse([xx - rr * 0.45, yy - rr * 0.55, xx - rr * 0.05, yy - rr * 0.15], fill=(255, 255, 255, int(220 * fade)))
        enc.stdin.write(np.asarray(im).tobytes())
    enc.stdin.close(); enc.wait()

def to_gif(mp4, gif, area, fps=12, limit=20e6):
    import json as _j
    pr = subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=width,height','-of','json',mp4],capture_output=True,text=True)
    st = _j.loads(pr.stdout)['streams'][0]; asp = st['width'] / st['height']
    width = int(math.sqrt(area * asp)) // 2 * 2
    for w in (width, int(width * 0.88) // 2 * 2, int(width * 0.78) // 2 * 2):
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', mp4, '-vf',
                        f'fps={fps},scale={w}:-2:flags=lanczos,split[a][b];[a]palettegen=max_colors=256:stats_mode=full[p];[b][p]paletteuse=dither=sierra2_4a',
                        '-loop', '0', gif], check=True)
        if os.path.getsize(gif) <= limit: return w
    return w

if __name__ == '__main__':
    mode = sys.argv[1]; names = sys.argv[2:] or list(scenes.L)
    os.makedirs(f'out/{mode}', exist_ok=True)
    for n in names:
        mp4 = f'out/{mode}/{n}.mp4'; gif = f'out/{mode}/{n}.gif'
        if os.path.exists(gif): continue
        if mode == 'living': render(n, scenes.L[n], mp4, secs=6)
        else: render(n, scenes.L[n], mp4, secs=8, swim=scenes.SW[n])
        w = to_gif(mp4, gif, 720*405 if mode == 'living' else 640*360, fps=12 if mode == 'living' else 10)
        print(n, mode, w, os.path.getsize(gif) // 1000, 'KB', flush=True)
