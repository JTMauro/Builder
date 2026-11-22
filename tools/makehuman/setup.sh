#!/usr/bin/env bash
# Install Blender (as the Python module "bpy") and MakeHuman's Blender add-on (MPFB) with the
# CC0 MakeHuman system assets, for headless generation of realistic, rigged people.
#
# Usage: tools/makehuman/setup.sh [WORKDIR]      (default WORKDIR: ~/.builder/makehuman)
# Needs: uv (or python3.13 + venv), curl, unzip, about 1.5 GB of disk.
# Downloads:
#   - bpy 5.2.4 from PyPI (Blender, GPL)
#   - MPFB add-on from files2.makehumancommunity.org (GPL code, not committed here)
#   - makehuman_system_assets_cc0.zip (CC0 assets: skins, eyes, hair, clothes, rigs)
set -euo pipefail
S="${1:-$HOME/.builder/makehuman}"
mkdir -p "$S"/{dl,mhassets,mhtex,mhgen,bhome/extensions/user_default}
cd "$S"

# files2 is slow and resets connections: resume until complete
fetch() { local url="$1" out="$2"; for i in $(seq 1 40); do curl -sS -L -m 600 -C - -o "$out" "$url" && return 0; sleep 3; done; echo "download failed: $url" >&2; return 1; }

if [ ! -x mhenv/bin/python ]; then
  if command -v uv >/dev/null; then uv venv --python 3.13 mhenv && uv pip install --python mhenv/bin/python bpy==5.2.4 numpy pillow
  else python3.13 -m venv mhenv && mhenv/bin/pip install bpy==5.2.4 numpy pillow; fi
fi

[ -d bhome/extensions/user_default/mpfb ] || {
  fetch https://files2.makehumancommunity.org/plugins/mpfb2-20261122.zip dl/mpfb.zip
  unzip -q -o dl/mpfb.zip -d bhome/extensions/user_default/
}
[ -d mhassets/skins ] || {
  fetch https://files2.makehumancommunity.org/asset_packs/makehuman_system_assets/makehuman_system_assets_cc0.zip dl/assets.zip
  unzip -q -o dl/assets.zip -d mhassets/
  # some zips nest everything one folder down
  if [ ! -d mhassets/skins ]; then inner=$(dirname "$(find mhassets -maxdepth 3 -type d -name skins | head -1)"); mv "$inner"/* mhassets/; fi
}

# first enable creates MPFB's user data folder; link the assets into it
BLENDER_USER_RESOURCES="$S/bhome" mhenv/bin/python -I -c 'import bpy; bpy.ops.preferences.addon_enable(module="bl_ext.user_default.mpfb")'
D="$S/bhome/extensions/.user/user_default/mpfb/data"; mkdir -p "$D"
for f in mhassets/*/; do n=$(basename "$f"); [ -e "$D/$n" ] || ln -s "$S/mhassets/$n" "$D/$n"; done
echo "MakeHuman ready in $S"
