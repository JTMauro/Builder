# Film-quality people: Unreal Engine + MetaHuman + DaVinci Resolve, with Builder and Claude

This guide is for the PC that does the heavy work (Windows 11, NVIDIA RTX graphics card). At the end
you can ask Claude, in any chat, things like *"render the TeenWave sequence in 4K and make social
versions"*, and Builder does it on this PC.

**What does what**

| Part | Job |
|---|---|
| Unreal Engine 5 + MetaHuman | Film-quality people, sets, lighting, cameras; Movie Render Queue renders 4K frames |
| Builder (this app) | The hub Claude talks to: starts Unreal renders, encodes video, makes timelines, social versions, GIFs, thumbnails |
| DaVinci Resolve (free) | Editing, colour grading and sound by hand |
| Claude desktop app | Lets a Claude chat in the cloud run commands on this PC (Remote Control) |

---

## 1. Install (once)

1. **Unreal Engine 5** with the Epic Games Launcher: *Unreal Engine > Library > +*, latest 5.x.
   During install, keep *Starter Content* and *Templates*.
2. **DaVinci Resolve** (free) from blackmagicdesign.com.
3. **Builder for Windows**: run its installer, sign in to the web app, add your Claude API key in
   *Settings* (only needed for Rivet and Craft builds; Unreal renders don't need it).
4. **Claude desktop app** from claude.ai/download. Sign in with the same account you use here.

## 2. Make a project Builder can render (once per project)

1. Epic Games Launcher > Launch Unreal. *Film / Video & Live Events > Blank*, name it (e.g. `Film`), Create.
2. In the editor: **Edit > Plugins**, search and tick:
   - **Movie Render Queue** (Builder renders with it)
   - **Python Editor Script Plugin** (Builder drives Unreal through it)
   - **MetaHuman** (the MetaHuman Creator/Character plugins, whichever your version lists)
   Restart the editor when it asks.
3. Check from Builder: ask Claude *"check Unreal with Builder"* (tool `unreal_status`) or open
   `http://localhost:8000/unreal`. It lists the editor, your projects and anything still missing.

If Builder can't find them, add to `builder.env` (Builder's data folder, `%APPDATA%\Builder`):

```
UNREAL_EDITOR=C:\Program Files\Epic Games\UE_5.6
UNREAL_PROJECT=C:\Users\YOU\Documents\Unreal Projects\Film\Film.uproject
```

## 3. Make a MetaHuman scene (in Unreal, the creative part)

1. **Create a MetaHuman**: *Window > MetaHuman* (or *Add > MetaHuman Character* in the Content
   Browser, depending on version). Pick a preset close to who you want, then sculpt face, skin,
   hair, eyes, body and clothes. Assemble it (build the character for the engine).
2. **Put them in a level**: drag the MetaHuman Blueprint into the scene.
3. **Light it**: *Add > Lights*: an *HDRI Backdrop* (free HDRIs from polyhaven.com) plus a key
   light, or use a Film template level. Add a *Post Process Volume* for exposure.
4. **Camera**: *Add > Cinematic > Cine Camera Actor*. Set focal length (35–85 mm for people) and
   focus on the eyes.
5. **Sequence**: *Add > Cinematic > Level Sequence*, name it (e.g. `TeenWave`). Add the camera and
   the MetaHuman; on the body track add an animation (MetaHumans retarget Mixamo / UE Mannequin
   animations; face animation comes from MetaHuman Animator with an iPhone, a webcam video, or audio).
6. **Copy its path**: right-click the sequence in the Content Browser > *Copy Reference*. It looks
   like `/Game/Cinematics/TeenWave`. Save all.

## 4. Render with Builder (Claude can do this part)

Ask Claude: *"Render /Game/Cinematics/TeenWave in 4K with Builder, ProRes for Resolve."*
That calls `unreal_render`:

| Setting | Default | Notes |
|---|---|---|
| `width` × `height` | 3840 × 2160 | 4K UHD |
| `fps` | 24 | the film look |
| `path_tracing` | off | on = film-quality light (much slower, RTX card); use `samples` 64+ |
| `samples` | 8 | smoother light and motion blur, slower |
| `encode` | `prores` | `prores` for Resolve, `h265`/`h264` smaller files, `frames` PNGs only |

Unreal opens, renders, and closes by itself; don't touch it while it works. The video lands in
Builder's library (and Claude can `export_file` it anywhere). Renders take minutes to hours, so
Claude starts them in the background and checks on them.

## 5. Edit in DaVinci Resolve

- **Rough cut from Builder**: ask Claude to make a timeline (`timeline_export`) from your clips,
  with in/out points and music. In Resolve: *File > Import > Timeline*, pick the `.fcpxml`.
- **Grade and finish** in Resolve as usual; *Deliver* to a folder, e.g. `Videos\Resolve Renders`.
- **Automatic social versions**: ask Claude *"watch my Resolve Renders folder with the social preset"*
  (`watch_folder_add`). Every new export gets a vertical 9:16, a square, a 1080p web copy, a
  thumbnail and a GIF in `Builder output` beside it.

## 6. Let this Claude chat work on the PC (optional, most powerful)

1. Claude desktop app > **Settings > Claude Code > Remote Control**: add a folder, for example
   `Documents\Unreal Projects` or a `Builder` folder.
2. Keep the app open. In the cloud chat, Claude can then ask to use that folder; you approve it on
   the PC. From then on Claude can run commands there: start renders, read Unreal logs, fix
   settings, write Unreal Python scripts.
3. For Claude to set up scenes headless through Builder, allow Unreal scripts in `builder.env`:
   `UNREAL_ALLOW_SCRIPTS=on` (off by default; it lets connected Claude chats run Python inside
   Unreal on this PC).

## Troubleshooting

- **"needs these plugins enabled"**: step 2.2, then restart Unreal.
- **Render failed / no frames**: the error includes the log path; common causes are a wrong
  sequence path (use *Copy Reference*), a level that doesn't contain the actors, or the editor being
  open with unsaved changes. Save all and close Unreal before a render.
- **Out of video memory**: lower `samples`, turn off path tracing, or render 1440p first.
- **Slow**: path tracing at 4K is minutes per frame even on fast cards; test at 1280×720 first.
