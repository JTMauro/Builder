# Builder

Builder = the Craft Anything engine (`engine/`, Rust, its own rules in `engine/AGENTS.md`) + this
Python server (FastAPI) with the web app, Rivet, accounts and the audio/video workshop.
Python 3.10+ / FastAPI / ffmpeg. Package in `media_backend/`, tests in `tests/` (`pytest -q`).

- `ops.py` is the single source of truth for media operations. The HTTP API (`api.py`), the
  built-in Claude assistant (`assistant.py`) and the MCP connector (`mcp_server.py`) all read
  `ops.REGISTRY`. Add operations there with `@operation`, and wrap the ffmpeg call in `_guarded`.
- Never modify input files; operations write new files via `storage.new_output_path`.
- Build ffmpeg commands as argument lists (never through a shell). User text goes to drawtext via a
  temp `textfile`, never inlined into a filter string.
- MCP tools raise `ToolError` (through `_friendly`) so error messages reach the client.
- Auth lives in `users.py` (SQLite: users, login sessions, personal `mbp_` tokens, saved chats) and the
  `auth` middleware in `api.py`. New routes are private by default; only `PUBLIC_PATHS` skip sign-in.
  Assistant chats are keyed by owner (username); never return another owner's chat.
- Craft builds: `craft.py` runs `craft-anything-cli rebuild --events-json` per build and keeps its
  events; `craft_tools.py` holds the tools Rivet and MCP share. Builds and projects are per owner;
  never let one person read another's run or project path (`owned_project`). Approvals are only
  answered by a person through `/craft/runs/{id}/answer`, never by Rivet or MCP.
- The engine loads Builder's MCP as plugin `builder_media` in media-only mode
  (`MEDIA_BACKEND_MCP_MEDIA_ONLY=1`), so a build can't start builds or call Rivet.
- Engine changes are listed in `engine/BUILDER.md`; keep that list current. Tests use the stand-in
  engine `tests/fixtures/fake_engine.py`.

- Studio work (characters, scenes, sound): read `studio/PLAYBOOK.md` and `studio/BIBLE.md` first, and add lessons to the playbook.
