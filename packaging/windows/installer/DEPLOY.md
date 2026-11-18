# Deploying Builder {VERSION}

Builder runs on each PC as a local server with a tray icon: web app at http://127.0.0.1:8000,
HTTP API for other apps, and an MCP connector at /mcp/ for Claude Code. User data (accounts, files,
chats, projects, settings) lives in `%APPDATA%\Builder` and survives upgrades and uninstall.

| Installer | For | Silent install |
|---|---|---|
| `Builder-Setup-{VERSION}-PerUser.exe` | Anyone, no admin. Installs to `%LOCALAPPDATA%\Programs\Builder` | `Builder-Setup-{VERSION}-PerUser.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /TASKS="autostart"` |
| `Builder-Setup-{VERSION}.exe` | IT, Intune (System), SCCM. Installs to `C:\Program Files\Builder` | `Builder-Setup-{VERSION}.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-` |
| `Builder-{VERSION}-Portable.zip` | No install. Unblock the zip, extract, run `Builder.exe` | n/a |

Intune detection: uninstall key `{6B0F3C2E-5D1A-4E7B-9C3F-2A8E4B1D7C55}_is1`, `DisplayVersion` = `{VERSION}`.

Uninstall: Settings > Apps > Builder, or `unins000.exe /VERYSILENT` in the install folder.

## After installing
1. Builder opens in the browser. Make the first account (it becomes the admin).
2. Tray icon > **Settings (builder.env)**: paste `ANTHROPIC_API_KEY=...` for Rivet and Craft builds, save, then tray > **Restart**.
3. Tray icon > **Connect Claude Code…** > Make token, and run the command it shows in a terminal on the same PC:
   `claude mcp add --transport http builder -s user http://127.0.0.1:8000/mcp/ --header "Authorization: Bearer mbp_..."`

## Other apps
Send files with HTTP and a personal token (`Authorization: Bearer mbp_...`):
- `POST /files` (multipart field `file`) returns a file id
- `POST /ops/<operation>` runs trim, convert, captions, etc.; `GET /operations` lists them
- `GET /files/<id>/content` downloads the result. Full reference: http://127.0.0.1:8000/docs

## Trust
Signed by **Cydcor LLC** (Cydcor internal CA). Cydcor-managed PCs show a verified publisher; other
PCs show "Unknown publisher" unless they trust the Cydcor root CA. `Builder-publisher.cer` is the
public certificate for WDAC/AppLocker publisher rules (`O=Cydcor LLC`).

## Bundled third-party programs
- ffmpeg/ffprobe (gyan.dev release essentials build, GPL v3; license in `ffmpeg\`)
- Craft Anything engine (`engine\craft-anything-cli.exe`)
