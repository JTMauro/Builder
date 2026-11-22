# Realistic people with MakeHuman (free)

Craft Studio and Builder's own procedural models build people from code (shaped tubes and spheres),
which always look like mannequins. For human-looking people use MakeHuman: its Blender add-on
(MPFB) builds a realistic body from age, gender, height, weight and muscle sliders, dresses it from
the CC0 MakeHuman asset pack, and adds a Mixamo-compatible rig. Everything here is free and runs
headless (no display) in a cloud container or on a server.

## Licences

- Blender (`bpy` from PyPI) and MPFB are GPL programs. They are downloaded by `setup.sh` and are not
  part of this repository. Models you make with them are yours.
- The MakeHuman system assets (skins, eyes, hair, clothes, rigs) are CC0, so the exported people are
  free to use for anything, commercial included.

## Set up (once, about 1.5 GB)

```bash
tools/makehuman/setup.sh            # work folder: ~/.builder/makehuman
```

`files2.makehumancommunity.org` is slow and drops connections; the script resumes downloads.

## Make a person

```bash
S=~/.builder/makehuman
# 1. textures (colours as sRGB 0..1)
SKIN_RGB=.89,.74,.64 HAIR_RGB=.62,.32,.14 TOP_RGB=.80,.84,.82 EYE=blue_eye \
  python3 -I tools/makehuman/tex.py $S/mhassets $S/mhtex
# 2. the person (body, clothes, rig) -> $S/mhgen/teen.glb
OUT=teen AGE=16 GENDER=1 HEIGHT=.55 WEIGHT=.4 MUSCLE=.45 HAIR=short02 \
  BLENDER_USER_RESOURCES=$S/bhome $S/mhenv/bin/python -I tools/makehuman/gen.py $S
# 3. optional: one-file glTF JSON for pages that need it
python3 -I tools/makehuman/glb2json.py $S/mhgen/teen.glb teen.json
```

Hair, clothes and shoes are asset folder names in `$S/mhassets/hair`, `.../clothes`. Use the same
`HAIR` and `CLOTHES` for `tex.py` and `gen.py`, and the same `HAIR_RGB` for `gen.py` (it repaints the scalp).

## What you get

- About 19k vertices and a 3 MB GLB. Y up, faces +Z, in the A-pose. Real MakeHuman scale (scale it
  in your engine if you need an exact height).
- `RIG=mixamo` gives `mixamorig:*` bones. Its bone rolls and rest pose differ from real Mixamo
  downloads, so copy animation in world space (match bone directions), not by local rotations.

## Fixes already applied (from a working build)

- Black eyes: the high-poly eye's cornea shell maps to a transparent area, so `gen.py` deletes it.
- Invisible eyebrows and lashes: alpha ×1.6 in `tex.py`. In three.js use `alphaTest` about 0.2.
- Bald patches between hair cards: `paint.py` paints hair colour into the skin under the hair.
- Shirt logo: removed with a median filter, then the shirt is recoloured by luminance.
- MPFB materials don't export to glTF, so every material is replaced with a plain Principled BSDF.
- Harmless export warnings: "Draco not available" and "MeshOptimizer not available".

Not yet run in Builder's own container: the first run may need small path fixes (for example
asset folder names in a newer asset pack).
