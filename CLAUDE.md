# Media Backend

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
