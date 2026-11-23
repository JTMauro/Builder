# Playbook: read this first

What any Claude chat working on this studio should know before touching anything. It's the memory
that survives between sessions: the owner's preferences, what worked, what broke and how it was
fixed. Add to it at the end of every session (newest lessons at the top of each list).

## The owner

- Wants an all-in-one, free creative studio ("build great things"). Free and open tools first.
- Judges by eye and ear and gives short notes ("10% less lean", "looks like a robot"). Treat each note
  as a rule and write it into BIBLE.md.
- Likes to be shown results, not told about them: send the picture, video or page.
- Wants plain language. Explain an acronym the first time (CMU = Carnegie Mellon's free motion library).
- Keys and secrets never go in chat; they go in builder.env / environment settings.
- Their own to-do list (PC connection, keys, ratings) is kept aside until they're ready.

## How to work

- Render a picture after every visual change and look at it before calling it done.
- Check sound with `audio_check` (and `describe_audio` when available) before delivering anything.
- Commit and push after each finished piece (author "Builder <builder@localhost>"; no model names).
- Long renders go in the background; say how long they'll take (CPU here: 720p ≈ 2–4 s a frame).
- Parallel helpers: give each its own files; only append to media_backend/ops.py; the main session
  commits. Run the full test suite before committing.
- Don't run Python scripts from the scratchpad root: helpers leave files there (an `inspect.py`
  shadowed the standard library once). Use a subfolder.

## Lessons (what broke and the fix)

### Rendering and pages
- Artifact pages can't fetch `data:`/`blob:` URLs and don't serve .glb/.hdr: ship models as one-file
  glTF JSON (glb2json.py) and convert to GLB bytes in the page; put HDRIs in JSON as base64.
- Off-screen pages throttle requestAnimationFrame → black screenshots. Render on-screen or step
  frames manually (scene mode).
- Element screenshots can time out while a canvas keeps changing: screenshot the page instead.
- Headless Chromium here needs `--use-gl=angle --use-angle=swiftshader --enable-unsafe-swiftshader`.
- In `__step`-style scene pages, never let module-level `const`s be used before their line runs
  (temporal dead zone errors stop the page).

### Characters
- MakeHuman: import bpy before bmesh. Hair cards need single-sided, alpha-clipped materials.
- Posture: bend only the limbs to the source rest pose; bending the spine causes a swayback.
- Lean per move: idle 0.015, walk 0.07, run 0.30 rad. CMU clips: lift neck −0.08 / head −0.14.
- Each clip's facing comes from its first frame; apply the yaw fix to all bones, not just the hips.
- Talking morphs: refit only brows, lashes and teeth per expression (refitting hair undid fixes).
- `HAIR_SNUG=0.95` removes the "helmet" hair look in close-ups.
- Morph targets are stored sparse (only moved vertices): 4.7 MB instead of 7.4 MB.

### Sound
- Kokoro voices: language from the name's first letter (a/b English, e Spanish, f French, i Italian,
  p Portuguese, h Hindi). Whisper checks intelligibility after generating.
- Mix with loudnorm to −16 LUFS and a −2 dBTP ceiling (a limiter at 0.95 still hit 0 dBTP after AAC).
- faster-whisper + PyAV fails on some files: decode with ffmpeg to 16 kHz float PCM first.
- MediaPipe on Linux needs `libegl1 libgles2`.

### Python package
- The voice list lives in media_backend/voices.py (no imports) to avoid circular imports; op modules
  that need other op modules import them inside functions.

## Where we stopped (resume here)

- Done this session: scene_video with casts, 9 sets, props, camera shots; director, make_anything,
  style_check; make_film, dub_scene, dub_video (keep_music via Demucs), make_short; review_video,
  describe_audio; make_character + library + export_character (coach added); export_godot;
  image_to_3d (TripoSR); Live Talk tab; extras (separate_stems, remove_background, clean_speech,
  optimize_model); NASA / Smithsonian / Internet Archive sources; docs/free-apps.md; Studio Toolbox
  links page (https://claude.ai/artifact/Xw2HtK5DPwnaWhjKMVEoFN).
- Finishing at the end of the session: Studio tab in the web app (scene builder, prop holding) and
  strand hair / cloth detail for Cycles stills — check their final reports/commits first.
- The owner's to-do list (set aside until they're ready): Remote Control on their PC, ANTHROPIC_API_KEY
  in Builder settings (director, make_anything, Live Talk, make_character from words), GPU tools
  (Real-ESRGAN, RIFE), Freesound key, api.data.gov key (Smithsonian), saved cloud environment,
  ratings of outputs, a phone video of real movement, approval to run Godot here for export tests.
- Next ideas they approved in principle: GPU models from docs/free-apps.md top 15 (via their PC or a
  rented GPU), Kenney / Quaternius imports, nightly test bench, footstep tuning, publishing the
  updated Talking Teen page.
- The owner watches usage: stop and pause (commit everything) when they say, or near their limits.

## Open issues

- BUG: all characters share ~/.builder/makehuman/mhtex, so building a new one (e.g. friend) changes
  older .blend characters' hair/shirt/eye colours in Cycles stills. Fix: per-character texture
  folders in gen.py / run_job.py. (Stage .json models embed their textures and are not affected.)
- Cycles `--strand-hair` / `--cloth-detail` work (opt-in). To do: break up the glossy hair highlight,
  raise the fringe, pose-based folds, test long/ponytail hair, full-body cloth before/after renders.

- Studio tab: templates are saved in browser storage; move them to the library ops (characters.py
  library_save_scene / library_get now exist). An "over" shot in a test render seemed to stay wide:
  check shots reach studio.html in scene mode. Prop grips are first passes (football into the thigh,
  jug handle). Preview line timing is estimated from text length.

- Footstep detection found only 2 steps on a short walk: tune the contact threshold.
- `fromLandmarks()` (motion from video) hasn't been tested on a real person's video yet.
- Teen close-ups: skin a little pale; strand hair for Cycles in progress.
