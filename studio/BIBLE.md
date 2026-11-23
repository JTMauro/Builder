# Studio bible

The one place any Claude chat (or person) looks before making or changing a character, a voice or a
scene. Keep it current: when the owner approves a look or gives a rule, write it here.

## How we work

- Free tools first (Blender, MakeHuman, Kokoro, Whisper, ffmpeg, three.js, CMU mocap, Poly Haven).
  Paid tools only when the owner asks.
- Show, then change: render a picture (or a page) after every change and look at it before saying
  it's done. The owner judges by eye; their notes are rules (below).
- Never paste keys in chat. Never recreate someone else's characters, music or brands.
- Real people: only with their consent (a parent's for minors) before anything made from their
  likeness is shared or sold.
- Credit what needs credit (CMU mocap, Kokoro, Poly Haven are listed in each page's credits).

## Characters

### Teen (from the owner's photo)

- Settings: `studio/characters/teen.json` (MakeHuman via `tools/makehuman/run_job.py`;
  add `"morphs": "talk"` / `MORPHS=talk` for the talking version).
- Look, as approved: age 16, slim, light-brown hair (not gold, not ginger), blue eyes, pale grey tee,
  light faded jeans, olive sneakers. Slim chest and waist, visible neck (the SHAPE targets in the
  settings file fix "big chest, no neck").
- Voice: Kokoro `am_puck` (young, bright), polished with `voice_polish` podcast + `room_reverb` studio.
- Posture rules (radians of forward lean at the top of the spine): idle 0.015 (upright, head up),
  walk 0.07, run 0.30. Never bend the spine back (swayback).
- Gestures: wave palm forward with an eased, layered motion (not robotic); raised arm lifts the
  shoulder so the arm doesn't look too long.
- Motion-capture moves: CMU clips get a small head/neck lift (`neck -0.08, head -0.14`).
- Pages: Talking Teen (https://claude.ai/artifact/VZoBkwaPDJ3ffodiep7aXo),
  Teen in Motion (https://claude.ai/artifact/8is8TWytN2XpjQMdmhgPs3),
  Crossed-Arms Teen (https://claude.ai/artifact/8j2D2hnRCTosgUnohcub2E).
- Known issue: his hair piece shows behind the painted fringe in close-ups.

## Voices

| Use | Kokoro voice |
| --- | --- |
| Teen boy | am_puck |
| Warm adult man | am_liam |
| Warm adult woman (best) | af_heart |
| UK narrator | bm_george / bf_emma |

## Looks

- Studio light: Poly Haven `studio_small_09` HDRI, key light warm, rim light cool.
- Film finish: GTAO, light bloom, depth of field on the face, cool shadows / warm highlights, grain.
  For video, Builder `film_look` with `film` preset (grain 0.35, vignette).

## Where things are

- Tools: `tools/makehuman/` (people, morphs, Cycles stills, CMU clips, viewers in `view/`).
- Builder operations: `media_backend/ops.py` registry (voice, finishing, Unreal, assets, sound).
- Set up a new machine: `tools/setup_studio.sh` (Linux/macOS) — then `builder doctor`.
