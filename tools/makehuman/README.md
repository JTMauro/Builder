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

## One call from a settings file

```bash
python3 -I tools/makehuman/run_job.py params.json teen   # -> ~/.builder/makehuman/mhgen/teen.glb + teen.json
```

`params.json` uses Craft Studio's "Real human" fields: `age`, `gender`, `height`, `weight`, `muscle`,
`skinRGB`, `hairRGB`, `topRGB` (plain tee `male_casualsuit06` only), `shoesRGB`, `hair`, `eyes`,
`outfit`, `shoes`. A person takes about 10 seconds to build.

## Also fixed in Builder's container

- `bpy` has to be imported before `bmesh` and `mathutils`.
- Hair layers render black from the inside: the hair material is single-sided
  (`use_backface_culling`), and only `short02`'s hair normal map is used (the others have empty areas).
- Dark hair texels showed as black patches: `tex.py` lifts them.
- The casual tee looked like ribs and abs on the chest: its baked fold shading is softened in
  `tex.py`, the clothes normal maps are exported at 0.45 strength (`CLOTHES_NORMAL`), and the clothes
  meshes get a volume-preserving smooth before the rig (`CLOTHES_SMOOTH`, default 0.7, 0 = off).
- Warm studio light pushes brown hair toward ginger: for light brown use a desaturated colour
  such as `HAIR_RGB=.40,.315,.235`.
- In three.js, measure a rigged model with `SkinnedMesh.computeBoundingBox()`;
  `Box3.setFromObject` gives the wrong height for MPFB's rig.

## Smile, fringe, lighter jeans and poses

- `SMILE=0.85` (or `"smile"` in params) blends MakeHuman's expression units (mouth corner puller,
  slight open mouth, cheek squint) and adds teeth, for a natural smile.
- By default (`FRINGE=brow`) hair that falls below the eyebrows is lifted so both eyes show.
- `JEANS_LIGHT=1.3` (`"jeansLight"`) gives the casual suits a lighter, faded wash.
- Poses: `view/pose.html` + `view/pose.js` aim the arm bones at target directions in three.js and
  export a posed GLB (still rigged). Serve the work folder (`python3 -m http.server 8775` in
  `~/.builder/makehuman`, with `view/` copied there), then:

```bash
node tools/makehuman/view/pose.js /mhgen/teen.glb shots front,three '{}' teen_posed.glb   # '{}' = arms crossed
node tools/makehuman/view/shot.js /mhgen/teen.glb shots front,three,face,back             # check renders
```

## Movement (walk, run, gestures)

`view/anim.js` (three.js r160, ES module):

- `retarget(srcScene, clips, tgtScene)` copies Mixamo-skeleton clips onto an MPFB `mixamo` rig in
  world space. It first bends the target into the source's rest pose by matching bone directions
  (MPFB rests in an A-pose with different bone rolls), then per frame sets
  `W_target = W_source · W_source_rest⁻¹ · W_target_rest`. Hips height is scaled by the hip-height
  ratio; `inPlace` drops forward travel. Free source clips: three.js's `Xbot.glb` (MIT) has idle,
  walk, run, agree (nod), headShake, sneak_pose, sad_pose.
- `gestures(scene, basePose)` makes point, wave, thumbs up and arms crossed by aiming arm bones
  and curling fingers toward the palm (found from the middle finger's natural bend). The wave lifts
  the shoulder so the raised arm doesn't look stretched.

Both return ordinary `AnimationClip`s. `view/anim.html` + `view/animshot.js` render test frames.

