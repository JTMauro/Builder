import numpy as np, cv2

def refine2(img, poly_mask, T=3.4):
    """Cut a creature out of open water: learn the water colour from a ring just outside the outline,
    drop matching pixels inside, then GrabCut, keep the main body, and clean the edge colour."""
    H, W = img.shape[:2]; m = (poly_mask > 0.5).astype(np.uint8)
    u8 = (np.clip(img, 0, 1) * 255).astype(np.uint8)
    lab = cv2.cvtColor(u8, cv2.COLOR_RGB2LAB).astype(np.float32)
    k = max(9, int(0.03 * max(H, W)))
    ring = (cv2.dilate(m, np.ones((k, k), np.uint8)) > 0) & (m == 0)
    # local water colour at every pixel: normalised blur of what lies outside the outline
    keep = (1 - m).astype(np.float32); sig = 0.03 * max(H, W)
    est = cv2.GaussianBlur(img * keep[..., None], (0, 0), sig) / np.maximum(cv2.GaussianBlur(keep, (0, 0), sig), 1e-4)[..., None]
    elab = cv2.cvtColor((np.clip(est, 0, 1) * 255).astype(np.uint8), cv2.COLOR_RGB2LAB).astype(np.float32)
    dE = np.linalg.norm(lab - elab, axis=2)
    md = dE / 3.0
    water = (dE < T * 3.0) & (m > 0)
    mu = elab[m > 0].mean(0)
    gc = np.full((H, W), cv2.GC_BGD, np.uint8)
    gc[ring] = cv2.GC_PR_BGD
    gc[(m > 0)] = cv2.GC_PR_FGD
    gc[water] = cv2.GC_PR_BGD
    gc[(dE < T * 2.0) & (m > 0)] = cv2.GC_BGD
    core = cv2.erode(((m > 0) & ~water).astype(np.uint8), np.ones((7, 7), np.uint8)) > 0
    core &= (md > T * 2.0)
    gc[core] = cv2.GC_FGD
    try:
        bg = np.zeros((1, 65), np.float64); fg = np.zeros((1, 65), np.float64)
        cv2.grabCut(u8[..., ::-1].copy(), gc, None, bg, fg, 5, cv2.GC_INIT_WITH_MASK)
        a = ((gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD)).astype(np.uint8)
    except Exception:
        a = ((m > 0) & ~water).astype(np.uint8)
    a = cv2.morphologyEx(a, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lb, st, _ = cv2.connectedComponentsWithStats(a)
    if n > 1:
        big = st[1:, cv2.CC_STAT_AREA].max()
        keep = [i for i in range(1, n) if st[i, cv2.CC_STAT_AREA] >= 0.03 * big]
        a = np.isin(lb, keep).astype(np.uint8)
    # fill pin holes only
    inv = 1 - a; n2, lb2, st2, _ = cv2.connectedComponentsWithStats(inv)
    for i in range(1, n2):
        if st2[i, cv2.CC_STAT_AREA] < 0.002 * a.sum(): a[lb2 == i] = 1
    af = cv2.GaussianBlur(a.astype(np.float32), (0, 0), 0.9)
    # pull water colour out of the soft edge
    wc = cv2.cvtColor(np.uint8([[mu]]).astype(np.uint8), cv2.COLOR_LAB2RGB)[0, 0].astype(np.float32) / 255
    edge = (af > 0.02) & (af < 0.98)
    out = img.copy()
    aa = np.clip(af, 0.15, 1)[..., None]
    dec = np.clip((img - (1 - aa) * wc) / aa, 0, 1)
    out[edge] = dec[edge]
    return af, out
