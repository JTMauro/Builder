# Media Backend

![Builder web app](docs/screenshot.png)

An audio/video processing backend you can reuse from any project. It has three parts:

1. **Media API** (HTTP, FastAPI + ffmpeg): upload files, run operations, download the results.
2. **Built-in assistant** (Claude): describe what you want in plain words and it plans and runs the
   operations, then looks at its own results before reporting back. Its default name is **Rivet**;
   rename it or change its personality with `ASSISTANT_NAME` / `ASSISTANT_PERSONA`.
3. **MCP connector**: Claude Code (or any MCP client) can use every operation and the assistant directly.

All three share one operation registry (`media_backend/ops.py`), so a new operation shows up in the
API, the assistant and Claude Code at once.

## Operations

| Operation | What it does |
|---|---|
| `probe` | Duration, resolution, fps, codecs, whether there's audio/video |
| `convert` | mp4 / mov / mkv / webm, or mp3 / wav / flac / aac / m4a / ogg / opus |
| `trim` | Cut by start + end/duration (frame-accurate or fast copy) |
| `extract_audio` | Pull the soundtrack out of a video |
| `resize` | Scale (keeps aspect) and/or change frame rate |
| `thumbnail`, `frames` | One frame, or N evenly spaced frames, as PNG |
| `normalize_audio` | Loudness to a LUFS target (EBU R128) |
| `change_speed` | 0.25x-4x, pitch preserved |
| `concat` | Join clips (videos are scaled/padded to one size; silent clips get silence) |
| `replace_audio` | Add a soundtrack/voice-over: replace or mix, optional loop |
| `mute` | Drop the audio track |
| `make_gif` | Palette-optimised looping GIF |
| `waveform` | PNG picture of the audio |
| `fade` | Fade in/out (audio and video) |
| `add_text` | Burn in a caption/title; shrinks to fit |
| `images_to_video` | **Animation**: frames to MP4 at an fps, or a slideshow; optional soundtrack and loops |
| `animate_image` | **Animation**: bring one still image to life: zoom_in, zoom_out, pan_left, pan_right, bounce, wobble |

Inputs are never modified. Every operation writes new file(s) to the store and returns their ids.

## Install and run

Needs Python 3.10+ and ffmpeg/ffprobe on `PATH`.

```bash
pip install -e .            # add [dev] for pytest
export ANTHROPIC_API_KEY=... # only needed for the assistant
media-backend serve --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/` for the **Builder** web app. API docs are at `http://127.0.0.1:8000/docs`.

### The web app

- **Intro:** the mascot hammers a nail three times, the plank doors swing open, and it flies up next to
  the "Builder" title (`docs/intro.mp4`). It plays once per browser tab; "Skip intro" or a reduced-motion
  setting skips it.
- **Library:** drag-and-drop uploads, live thumbnails, hover-to-play videos. Shift-click files to pick
  several, in order (for joining clips or animating frames).
- **Workbench:** a preview with media details, plus a tile for every tool. Forms are generated from each
  operation's schema, so new operations appear automatically.
- **Jobs:** live status. Results open in the preview, and the mascot hammers away while anything is running.
- **Chat:** talk to the assistant, which can see the selected file. Click the mascot to jump to the chat.
- Light and dark themes, responsive down to phone width. If the server has a token, the page asks for it once.

## Connect Claude Code

**Same machine (stdio, recommended):** Claude Code starts the backend itself. Files are imported
from, and exported to, the project directory.

```bash
claude mcp add media -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY -- media-backend mcp
```

Or commit the included `.mcp.json` to a project, and everyone who opens it in Claude Code gets the connector.

**Remote server (HTTP):** run `media-backend serve --host 0.0.0.0` with `MEDIA_BACKEND_TOKEN` set, then

```bash
claude mcp add --transport http media https://YOUR-HOST:8000/mcp/ --header "Authorization: Bearer $MEDIA_BACKEND_TOKEN"
```

Over HTTP, `import_path`/`export_file` refer to the *server's* disk. Use `import_url`, or upload with
`POST /files`, to move files in.

### MCP tools

`list_files`, `import_path`, `import_url`, `export_file`, `delete_file`, `view_file` (Claude sees images
or video frames), `list_operations`, `get_job`, one tool per operation (`trim`, `concat`,
`images_to_video`, ...; pass `wait=false` for long encodes), and `ask_assistant` (hand a whole task to
the built-in assistant; pass `session_id` back to continue).

A typical prompt in Claude Code: *"Import assets/frames/*.png, animate them at 12 fps with
assets/theme.mp3 as the soundtrack, add the title 'Episode 1', and export it to build/ep1.mp4."*

## HTTP API

| Method & path | Purpose |
|---|---|
| `POST /files` (multipart `file`) | Upload. Returns `{id, name, ...}` |
| `GET /files`, `GET /files/{id}`, `GET /files/{id}/probe` | List / info / media details |
| `GET /files/{id}/content[?inline=true]` | Download |
| `DELETE /files/{id}` | Delete |
| `GET /operations` | All operations with JSON schemas |
| `POST /jobs[?wait=true]` `{operation, params}` | Start an operation (background, or wait for it) |
| `POST /ops/{operation}` `{...params}` | Run an operation and wait |
| `GET /jobs/{id}[?wait=30]` | Job status, outputs, error |
| `POST /assistant/chat` `{message, session_id?, file_ids?}` | Talk to the assistant |
| `DELETE /assistant/sessions/{id}` | Forget a conversation |

```bash
ID=$(curl -s -F file=@intro.mp4 localhost:8000/files | jq -r .id)
curl -s -X POST localhost:8000/ops/trim -H 'content-type: application/json' -d "{\"file_id\":\"$ID\",\"start\":2,\"duration\":5}"
curl -s -X POST localhost:8000/assistant/chat -H 'content-type: application/json' \
  -d "{\"message\":\"Make a 3 second GIF of the best moment\",\"file_ids\":[\"$ID\"]}"
```

You can also talk to the assistant in a terminal with `media-backend chat`.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `MEDIA_BACKEND_DATA` | `./media-data` | Where files are stored |
| `MEDIA_BACKEND_TOKEN` | *(none)* | If set, every route except `/health` (including `/mcp`) needs `Authorization: Bearer <token>` |
| `MEDIA_BACKEND_ALLOWED_DIRS` | current directory | Directories (`:`-separated) that `import_path`/`export_file` may touch |
| `MEDIA_BACKEND_MAX_UPLOAD_MB` | `2048` | Upload size limit |
| `MEDIA_BACKEND_WORKERS` | `2` | Parallel ffmpeg jobs |
| `ASSISTANT_NAME` / `ASSISTANT_PERSONA` | `Rivet` / a friendly builder | The assistant's name and personality |
| `ASSISTANT_MODEL` | `claude-opus-5-5` | Claude model |
| `ASSISTANT_EFFORT` | `medium` | `low` / `medium` / `high` / `xhigh` / `max` |
| `ASSISTANT_MAX_TOOL_TURNS` | `25` | Step limit per message |

The assistant uses server-side refusal fallbacks (`fallbacks: "default"`): if the model declines a
request on safety grounds, the API retries it on a suitable fallback model within the same call.

## Adding an operation

Add a params model and a function decorated with `@operation(...)` in `media_backend/ops.py`. Use
`_guarded(...)` so failed runs leave nothing behind. It appears in the API, the assistant's tools
and the MCP connector automatically. Add a test in `tests/test_ops.py`.

## Tests

```bash
pip install -e '.[dev]' && pytest -q
```

## Notes

- Jobs and assistant conversations are kept in memory, so they reset when the server restarts.
  Files persist on disk.
- No speech-to-text or AI image/voice generation yet. Those would be new operations.
