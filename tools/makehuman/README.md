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
- A teen read as "big chest, no neck": `SHAPE` (or `"shape"` in params) loads MakeHuman's own shape
  targets before the rig and clothes are fitted. Used for the photo teen: `neck/measure-neck-height-incr:0.7,
  neck/neck-scale-vert-incr:0.35, neck/measure-neck-circ-decr:0.35, torso/torso-scale-depth-decr:0.4,
  torso/measure-bust-circ-decr:0.45, torso/torso-muscle-pectoral-decr:0.6, torso/measure-shoulder-dist-decr:0.25,
  torso/torso-vshape-decr:0.35`.
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
- `gestures(scene, basePose, idleClip)` makes point, wave, thumbs up and arms crossed. Each is sampled
  at 30 fps with eased timing and layered over the idle: only the gesturing arm(s) follow the gesture,
  the rest of the body keeps its idle motion. The wave is palm-forward with an open hand and a wrist
  that trails the forearm. Earlier version: by aiming arm bones
  and curling fingers toward the palm (found from the middle finger's natural bend). The wave lifts
  the shoulder so the raised arm doesn't look stretched.

Posture: only arms and legs are matched to the source rest pose (bending the spine gave a swayback).
  `retarget(..., { lean: { idle: 0.015, walk: 0.07, run: 0.30 } })` sets a per-move forward lean through
  the spine; the neck and head keep only part of it so he looks ahead.

Both return ordinary `AnimationClip`s. `view/anim.html` + `view/animshot.js` render test frames.


## Talking: mouth shapes, blinks and a voice

- `MORPHS=talk` makes `gen.py` also export one GLB per expression (`OUT__viseme_A.glb` … `viseme_H`,
  `blink_L`, `blink_R`, `smile`, `brows_up`, `brows_frown`) plus `OUT__variants.json`. Teeth are added
  so open mouths aren't empty.
- `python3 -I morphs.py OUT.glb OUT__variants.json OUT_morph.glb` merges them into one GLB as named
  morph targets (`mesh.extras.targetNames`; three.js puts them in `morphTargetDictionary`). Targets are
  sparse (only the vertices that move), so 13 expressions add about 1.5 MB.
- The mouth shapes are Rhubarb's A–H (X = closed/rest), the same letters Builder's `lipsync` returns.
  Make a line with Builder: `speak` → `lipsync` (mouth cues + word times) → `voice_polish` → `room_reverb`.
- `view/talk.html` is the full viewer: lip-sync with smoothing, random blinks, breathing, head look-at,
  positional voice and procedural footsteps, HDRI light, GTAO, bloom, depth of field and a grade/grain
  pass, plus CMU motion-capture moves (credit: mocap.cs.cmu.edu, NSF EIA-0196217). It expects
  `models/teen.json`, `models/light.json` (base64 .hdr), `clips/mixamo-clips.json`,
  `clips/cmu-clips.json` (base64 GLBs by label) and `sound/lines.json` next to it.
- CMU clips: `retarget(src, clips, tgt, { rename: CMU })` maps the CMU ASF bone names; each clip's
  facing is taken from its first frame, so all moves face forward.

## Cycles stills (photoreal renders)

`cycles.py` renders a still of a `gen.py` character with Blender Cycles on the CPU (no GPU or display):
subsurface skin, wet eyes, alpha-clipped hair, brows and lashes, the clothes' own textures, light from a
CC0 HDRI (default `~/.builder/dl/hdri/studio.hdr`) plus a soft key and rim light, on a curved grey studio
backdrop. The camera is framed from the mesh bounds and the head bone. AgX view, OpenImageDenoise.

```bash
S=~/.builder/makehuman
BLENDER_USER_RESOURCES=$S/bhome $S/mhenv/bin/python -I tools/makehuman/cycles.py \
  $S/mhgen/teen.blend $S/shots/teen_closeup.png --shot closeup --pose relaxed --dof --percent 50
```

- Input: the `.blend` (best: shape keys work) or the `.glb`.
- `--shot full|medium|closeup|portrait3q` (50/70/85/85 mm), `--lens`, `--angle` (degrees around the
  person), `--dof` (focus on the eyes, `--fstop`).
- `--pose relaxed` lowers the arms from the A-pose. `--smile 0..1` sets gen.py's smile (or loads it on a
  `.blend` body); `--shape-key NAME=VALUE` sets a shape key or loads a MakeHuman expression unit such as
  `mouth-open=0.4`, `eyebrows-left-up=0.8` (teeth, brows and lashes are refitted).
- `--hdri`, `--hdri-strength`, `--hdri-rotate`, `--key W` (80, 0 = off), `--rim W` (60),
  `--exposure` (-0.5), `--backdrop grey|white|none|shadow` (`shadow` = transparent PNG with a shadow catcher).
- `--res 1920x1080` (or `3840x2160`, `1080x1350`), `--percent` for previews, `--samples` (64), `--no-denoise`,
  `--view AgX|Filmic|Standard`, `--look "Medium High Contrast"`, `--skin-even` (0.35: hides the pale
  patches in MakeHuman's skin texture), `--save-blend` to keep the lit scene.

Times on 4 CPU cores, 64 samples: 960x540 (`--percent 50`) full shot 45 s, closeup 95 s (subsurface skin
filling the frame is the slow part); a full shot at 1920x1080 took 6 minutes, and 3840x2160 is about 4x
that again. Previews at `--percent 25 --samples 16` take 10-20 s.

Known limits: the hair is a smooth card mesh (reads as a neat helmet up close), the tee is boxy, and the
skin texture is low on pore detail (a fine noise bump stands in).
