"""Merge expression variants into one GLB as named morph targets (shape keys three.js can drive).

    python3 -I morphs.py base.glb variants.json out.glb

variants.json maps a name (viseme_A, blink_L, smile...) to a GLB exported from the same person with
only that expression applied (gen.py MORPHS=talk). The meshes have the same vertices in the same order,
so each mesh's morph target is simply (variant - base) for positions and normals. Meshes an expression
doesn't move get no target for it. Names go in mesh.extras.targetNames (three.js reads them into
morphTargetDictionary).
"""

import json
import struct
import sys

import numpy as np

COMP = {5126: np.float32, 5125: np.uint32, 5123: np.uint16, 5121: np.uint8}
NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


def read_glb(path):
    b = open(path, "rb").read()
    assert b[:4] == b"glTF", path
    off, js, bin_ = 12, None, b""
    while off < len(b):
        ln, typ = struct.unpack_from("<II", b, off)
        chunk = b[off + 8: off + 8 + ln]
        off += 8 + ln
        if typ == 0x4E4F534A:
            js = json.loads(chunk)
        elif typ == 0x004E4942:
            bin_ = bytes(chunk)
    return js, bin_


def accessor(js, bin_, i):
    a = js["accessors"][i]
    bv = js["bufferViews"][a["bufferView"]]
    n = NCOMP[a["type"]]
    dt = np.dtype(COMP[a["componentType"]])
    start = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
    stride = bv.get("byteStride", 0) or dt.itemsize * n
    if stride == dt.itemsize * n:
        return np.frombuffer(bin_, dt, a["count"] * n, start).reshape(a["count"], n).copy()
    rows = [np.frombuffer(bin_, dt, n, start + k * stride) for k in range(a["count"])]
    return np.array(rows)


def write_glb(path, js, bin_):
    bin_ += b"\0" * ((4 - len(bin_) % 4) % 4)
    js["buffers"] = [{"byteLength": len(bin_)}]
    j = json.dumps(js, separators=(",", ":")).encode()
    j += b" " * ((4 - len(j) % 4) % 4)
    out = struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(j) + 8 + len(bin_))
    out += struct.pack("<II", len(j), 0x4E4F534A) + j + struct.pack("<II", len(bin_), 0x004E4942) + bin_
    open(path, "wb").write(out)


def main(base_path, variants_path, out_path, eps=5e-4):   # ignore movements under half a millimetre
    js, bin_ = read_glb(base_path)
    bin_ = bytearray(bin_)
    variants = json.load(open(variants_path))

    def view(data: bytes) -> int:
        nonlocal bin_
        while len(bin_) % 4:
            bin_.append(0)
        js["bufferViews"].append({"buffer": 0, "byteOffset": len(bin_), "byteLength": len(data)})
        bin_ += data
        return len(js["bufferViews"]) - 1

    def add(arr, idx):
        """A sparse accessor: zeros except the vertices in idx (most of the body doesn't move for a mouth shape)."""
        arr = arr.astype(np.float32)
        acc = {"componentType": 5126, "count": int(arr.shape[0]), "type": "VEC3",
               "min": [float(x) for x in arr.min(0)], "max": [float(x) for x in arr.max(0)]}
        if len(idx):
            acc["sparse"] = {"count": int(len(idx)),
                             "indices": {"bufferView": view(idx.astype(np.uint32).tobytes()), "componentType": 5125},
                             "values": {"bufferView": view(arr[idx].tobytes())}}
        js["accessors"].append(acc)
        return len(js["accessors"]) - 1

    summary = {}
    vdata = {name: read_glb(p) for name, p in variants.items()}
    for mi, mesh in enumerate(js["meshes"]):
        names = []
        for name, (vjs, vbin) in vdata.items():
            vmesh = vjs["meshes"][mi]
            deltas = []
            for pi, prim in enumerate(mesh["primitives"]):
                vprim = vmesh["primitives"][pi]
                p0 = accessor(js, bin_, prim["attributes"]["POSITION"])
                p1 = accessor(vjs, vbin, vprim["attributes"]["POSITION"])
                if p0.shape != p1.shape:
                    raise SystemExit(f"{mesh.get('name')}: {name} has a different vertex count ({p1.shape} vs {p0.shape})")
                dn = None
                if "NORMAL" in prim["attributes"]:
                    dn = accessor(vjs, vbin, vprim["attributes"]["NORMAL"]) - accessor(js, bin_, prim["attributes"]["NORMAL"])
                deltas.append((p1 - p0, dn))
            if max(float(np.abs(d).max()) for d, _ in deltas) < eps:
                continue
            names.append(name)
            for prim, (dp, dn) in zip(mesh["primitives"], deltas):
                idx = np.nonzero(np.abs(dp).max(1) >= eps * 0.2)[0]
                t = {"POSITION": add(dp, idx)}
                if dn is not None:
                    t["NORMAL"] = add(dn, idx)
                prim.setdefault("targets", []).append(t)
        if names:
            mesh["weights"] = [0.0] * len(names)
            mesh.setdefault("extras", {})["targetNames"] = names
            summary[mesh.get("name", str(mi))] = names
    write_glb(out_path, js, bytes(bin_))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:4])
