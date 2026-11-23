---
name: use-builder
description: Use the Builder media backend (audio/video tools, Rivet the Claude assistant, web app) from another project, or extend Builder itself. Use when a task needs video or audio processing (trim, convert, captions, soundtracks, joining clips, GIFs, thumbnails, vertical/square social versions, frame-by-frame or still-image animation), a DaVinci Resolve timeline or watch folder, an Unreal Engine / MetaHuman 4K render, when someone mentions Builder or Rivet, or when adding a feature to this repository.
---

# Using Builder

Builder runs on the Craft Anything engine (`engine/`): it studies a website, app or game and has a
coding agent build your own version, checked against the original. Use the `craft_build_*` tools
(or `POST /craft/runs`) for that; see README "Craft builds". It also has its own audio/video
workshop: a Python (FastAPI + ffmpeg) service offering 18 media operations through three doors that
share one registry (`media_backend/ops.py`):

1. **MCP connector** (best for Claude Code): tools such as `trim`, `add_text`, `images_to_video`,
   `animate_image`, `import_path`, `export_file`, `view_file`, `ask_assistant`.
2. **HTTP API:** `POST /files`, `POST /ops/{operation}`, `POST /jobs`, `GET /files/{id}/content`, and so on.
3. **Rivet, the built-in assistant:** `POST /assistant/chat`, or the MCP tool `ask_assistant`, for
   multi-step requests in plain words.

## Pick the setup

**A. Same machine as the other project (simplest).** Install Builder, then register the connector in
that project. Claude Code starts Builder itself, and files move in and out of the project directory.

```bash
pip install "git+https://github.com/JTMauro/builder.git"   # private repo: needs GitHub access
claude mcp add builder -s user -e BUILDER_USER=<your username> -- builder mcp
```

Add `-e ANTHROPIC_API_KEY=...` only if you want `ask_assistant` (Rivet). The media tools work without it.

**B. A shared Builder server.** Someone runs `builder serve --host 0.0.0.0` behind HTTPS. Each
person signs in to the web app, opens *their name → Connect Claude Code*, and runs the command it shows:

```bash
claude mcp add --transport http builder https://THEIR-HOST/mcp/ --header "Authorization: Bearer mbp_..."
```

**C. From code (any language).** Call the HTTP API with a personal token:

```bash
ID=$(curl -s -H "Authorization: Bearer $BUILDER_TOKEN" -F file=@clip.mp4 $BUILDER_URL/files | jq -r .id)
curl -s -H "Authorization: Bearer $BUILDER_TOKEN" -H 'content-type: application/json' \
  -X POST $BUILDER_URL/ops/trim -d "{\"file_id\":\"$ID\",\"start\":2,\"duration\":5}"
```

`GET /operations` returns every operation with its JSON schema. Read it rather than guessing parameters.

## Working with the MCP tools

- Files are referenced by id. `import_path` copies a file in; every operation writes **new** files and
  returns their ids, and inputs are never changed. `export_file` writes a result back into the project.
- Check visual results with `view_file` (it shows images or frames) before saying a task is done.
- For long encodes, pass `wait=false` and poll `get_job`.
- `ask_assistant` hands a whole job to Rivet. Pass the returned `session_id` back to continue.

## Extending Builder (when working in this repo)

Read `CLAUDE.md` first. A new operation is a params model plus an `@operation` function in
`media_backend/ops.py` that wraps its ffmpeg call in `_guarded(...)`. It appears in the API, Rivet and
the MCP connector automatically. Add a test in `tests/test_ops.py` and run `pytest -q`.
Only original characters and assets: don't recreate trademarked characters.

## Film work: Unreal Engine, MetaHuman, DaVinci Resolve

On the PC running Builder for Windows: `unreal_status` (is Unreal ready?), `unreal_render` (render a Level
Sequence with Movie Render Queue, 4K ProRes by default; long, so start with `wait=false` and poll
`get_job`), `unreal_script` (only if the person set `UNREAL_ALLOW_SCRIPTS=on`), `timeline_export` (FCPXML
for Resolve: *File > Import > Timeline*), `reframe`, and watch folders (`watch_folder_add` with the
`social` preset on Resolve's render folder). Setup guide: `docs/unreal-metahuman.md`.

## Voices, music and sound

Voice ops: `speak` (Kokoro TTS), `transcribe`, `burn_captions`, `lipsync`, `denoise_audio`, `voice_polish`,
`room_reverb`. Sound design (free, offline, seeded): `make_music` (original music by mood and length),
`make_ambience` (room tone, rain, forest, city…), `footsteps` (surface + pace or exact `times` from an
animation), and `audio_check` (LUFS, true peak, clipping, silences; with `expected_text`, a Whisper word match)
before delivering anything with sound. Mix music or ambience under a video with `replace_audio`.

## Characters, scenes and films

`scene_video` turns beats into a finished video on a stage (stages live in `~/.builder/stages`; build one
with `tools/makehuman/make_stage.py`, add places with `add_set.py`): `cast` for several characters with
their own voices, `set` (park, living_room, street), per-beat `actor`, `say`, `move`, `walk_to`, `look`
and `shot` (wide, two, medium, close, over; `move` seconds to glide). `direct_scene` does it from plain
words and checks its own result; `make_anything` routes any request. `render_still` makes photoreal
Cycles stills; `video_to_motion` captures body and face from a video; `style_check` / `visual_diff`
catch unwanted changes against approved pictures. Rendering is slow on CPU (720p about 2–4 s a frame);
start long jobs with `wait=false`.

## Free assets for a build

`asset_search` (HDRI skies, textures, models, CC images and sounds; licence and credit in every result),
then `asset_import` with the result's `source` and `id` (credit saved in `license.json`), and
`page_references` to gather look references from one page. Prefer CC0; keep the attribution for CC BY.

