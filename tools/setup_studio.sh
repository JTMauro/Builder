#!/usr/bin/env bash
# One command to install everything the studio uses (all free), then check it with `builder doctor`.
#
#   tools/setup_studio.sh [WORKDIR]        (default ~/.builder)
#
# Installs / downloads (about 2.5 GB):
#   - Builder's voice extras (Kokoro speech, Whisper captions, espeak phonemes)       pip
#   - Kokoro-82M voice model and Whisper "base" model                               Hugging Face
#   - Blender (bpy) + MakeHuman MPFB + CC0 assets                                   tools/makehuman/setup.sh
#   - CMU motion-capture clips listed in tools/makehuman/cmu_clips.json            Hugging Face
#   - Poly Haven studio HDRI (CC0)
#   - Playwright's Chromium for scene videos (if Node is installed)
# Safe to run again: finished steps are skipped.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(dirname "$HERE")"
W="${1:-$HOME/.builder}"; mkdir -p "$W"/{models,dl/hdri}
export BUILDER_MODELS_DIR="${BUILDER_MODELS_DIR:-$W/models}"

echo "== voice extras"
python3 -m pip install -q -e "$REPO[voice,motion]" phonemizer espeakng_loader
# MediaPipe needs EGL on Linux (Debian/Ubuntu: sudo apt install libegl1 libgles2)
echo "== voice + caption models (first download only)"
python3 - <<'PY'
from media_backend import voice
voice._kokoro_engine()
voice._whisper_model("base")
print("voices ready in", voice.models_dir())
PY
echo "== Blender + MakeHuman"
bash "$HERE/makehuman/setup.sh" "$W/makehuman"
echo "== CMU motion capture"
python3 -I "$HERE/makehuman/get_cmu.py" "$W/makehuman"
echo "== studio light"
[ -s "$W/dl/hdri/studio.hdr" ] || curl -sSfL -o "$W/dl/hdri/studio.hdr" https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/1k/studio_small_09_1k.hdr
if command -v node >/dev/null; then
  echo "== scene videos (headless browser)"
  (cd "$HERE/makehuman/view" && { [ -d node_modules/playwright ] || npm i -s playwright; } && npx -y playwright install chromium) || echo "  (skipped: Playwright install failed)"
fi
echo "== check"
python3 -m media_backend.cli doctor || true
