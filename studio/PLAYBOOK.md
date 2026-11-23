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

## Open issues

- Footstep detection found only 2 steps on a short walk: tune the contact threshold.
- `fromLandmarks()` (motion from video) hasn't been tested on a real person's video yet.
- Teen close-ups: skin a little pale; strand hair for Cycles in progress.
