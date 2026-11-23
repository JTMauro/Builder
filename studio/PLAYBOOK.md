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
- Comics/stills: `scene.keep` (frame numbers) makes scene_render step every frame but draw only those, and
  `window.__step(dt, false)` skips the render: a 7 s scene simulates in ~5 s. `scene.probe` records head
  screen positions for speech-bubble tails.
- A beat's `view` used to replace an explicit shot ("over" looked wide): once a scene has shots, `view` is
  ignored in studio.html.
- Godot 4.3 headless prints `ERROR: Parameter "m" is null` (mesh_get_surface_count) for any mesh with the
  dummy renderer: harmless, not our export.
- Artifact pages can't fetch `data:`/`blob:` URLs and don't serve .glb/.hdr: ship models as one-file
  glTF JSON (glb2json.py) and convert to GLB bytes in the page; put HDRIs in JSON as base64.
- Off-screen pages throttle requestAnimationFrame → black screenshots. Render on-screen or step
  frames manually (scene mode).
- Element screenshots can time out while a canvas keeps changing: screenshot the page instead.
- Headless Chromium here needs `--use-gl=angle --use-angle=swiftshader --enable-unsafe-swiftshader`.
- In `__step`-style scene pages, never let module-level `const`s be used before their line runs
  (temporal dead zone errors stop the page).

### Characters
- Sitting: CMU sit clips are in place (hips xz removed), so glide the body from the front spot onto the seat
  while it sits; pause at the first moment the hips are lowest; the clip turns the body (~90° for "sit"), so
  measure the hips' heading change and pre-rotate. Seat spots live in add_set.py `USES` (measured from prop
  bounds: bench 1.17 m long, seat 0.45; stool 0.58).
- Footsteps while travelling: a planted foot still slides ~0.3–0.5 m/s (in-place clip vs glide), so a landing
  is "ground speed < 0.55 × walking speed" and low; 3 s walk = 6 even steps (was 2), run ≈ 3 a second.
- Grips: football out to the side of the palm (+x 0.07 in right-hand space) or it sinks into the thigh.
  Tune grips without editing the page: `scene.grips = {id: {pos, rot}}`.
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

## Big goal: books to a full-length movie

The owner is writing books in other projects. Once they're finished, turn them into a full-length
movie with this studio: chapters → scenes (direct_scene plans), a recurring cast in the character
library (make_character, fixed voices), sets per location, make_film per act, dub_scene for other
languages, Resolve timelines for the final edit. Feature length needs a GPU (CPU: ~4 s per 720p frame
→ days); photoreal needs MetaHuman (Unreal) or GPU video models. Ask which project/repo holds the
books before starting.

Backups: the owner has download packs of everything outside git (chat transcript, library, voice
lines, characters, stage, renders). Refresh them at the end of big sessions (container resets lose
~/.builder).

### Senses and self-test (watch_video, caption_audio, quality_score, builder bench)
- Qwen3-VL-2B on CPU (bf16, 3 threads, 16 frames at 448x256): ~5 s to load, ~35 s for a description, 13–27 s
  for a short question, ~20 s more for the per-moment notes. It gets people, clothes, setting, big gestures
  (the wave at 1 s) and close-ups right; it invents actions (someone "sits on the bench"), mixes up who
  says what, garbles subtitles and said "no on-screen text" while quoting it. Never trust it on fine
  contact (hands through legs): it just says "no". Use it to find moments, then look at the frames.
- Free-running "TIMELINE" prompts make small VLMs loop past the clip's end: ask for a fixed list of
  moments instead, and pair untimed lines with the moments in order.
- CLAP: a single "music" label wins against every specific sound. Ask "what kind of recording" (speech /
  music / ambience / effects) as its own category first, then the events. It called synthetic forest
  ambience "white noise, ocean waves" (no birds) and footsteps "clicks and pops, footsteps": fine for
  speech-vs-music-vs-noise checks, weak on exact events. Only quote voice pitch when there is speech.
- LPIPS tells blur (0.20) from film grain / the next frame (0.05) where SSIM gives ~0.81 for both:
  use `visual_diff perceptual=true` when grain or noise is in the look.
- github.com downloads get 403 through this proxy; raw.githubusercontent.com works.
- torchvision 0.29.2 (built for torch 2.14) works with torch 2.15 CPU; install with --no-deps so torch
  isn't replaced.
- The bench cleans its own files out of the store, including orphans like lip-sync cues whose parent voice
  line it deleted (scene_video doesn't link them to the output). review_video always flags the grey-studio
  bench scene as "almost no motion" / "low contrast" (small figure): the bench compares new issues only.
- Bench timings swing 2–10x when other jobs share the CPU: slow steps are notes, not regressions.

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
  ratings of outputs, a phone video of real movement. (Godot here: approved and done.)
- Next ideas they approved in principle: GPU models from docs/free-apps.md top 15 (via their PC or a
  rented GPU), Kenney / Quaternius imports, nightly test bench, footstep tuning, publishing the
  updated Talking Teen page.
- Added (studio more): add_set/find_sets (any Poly Haven HDRI, presets incl. arena = old_outdoor_theater,
  colosseum, desert, ruins, circus), find_moves/fetch_moves (all CMU moves on demand), prop use beats
  (sit/lean/open), make_comic, finish_picture, make_video_short, Studio templates in the library.
  Godot 4.3 is in /root/.builder/tools/godot: the teen/friend/coach park export imports and runs clean.
- The owner watches usage: stop and pause (commit everything) when they say, or near their limits.

## Open issues

- BUG: all characters share ~/.builder/makehuman/mhtex, so building a new one (e.g. friend) changes
  older .blend characters' hair/shirt/eye colours in Cycles stills. Fix: per-character texture
  folders in gen.py / run_job.py. (Stage .json models embed their textures and are not affected.)
- Cycles `--strand-hair` / `--cloth-detail` work (opt-in). To do: break up the glossy hair highlight,
  raise the fringe, pose-based folds, test long/ponytail hair, full-body cloth before/after renders.

- Studio preview line timing is estimated from text length.
- Lean is a whole-body tilt (+ arms crossed against a wall), not a hand-on-table pose; open plays "point"
  while the door swings. Sitting arms follow the mocap chair's armrests. No CMU "lean"/"open door" clip exists.
- Over-the-shoulder, templates, grips (football, jug) and footsteps were fixed (see Lessons).
- `fromLandmarks()` (motion from video) hasn't been tested on a real person's video yet.
- Teen close-ups: skin a little pale; strand hair for Cycles in progress.
