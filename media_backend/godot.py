"""Export characters and a set as a ready-to-open Godot 4 game project (free, MIT engine: godotengine.org).

export_godot packs: each character as a .glb with its moves (export_character), the set's sky (as the
light and background) and props, a floor, a third-person player you walk around with (WASD / arrows,
Shift to run, mouse to look, number keys for gestures), and the other characters standing in the scene
looking alive (idle). Open the folder (or unzip the .zip) in Godot 4.x and press Play.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path

from pydantic import BaseModel, Field

from . import storage
from .ops import OpError, OpResult, _guarded, operation, run_operation

PLAYER_GD = '''extends CharacterBody3D
# Third-person player made by Builder. WASD / arrows to move, Shift to run, mouse to look,
# number keys play gestures, Esc frees the mouse.
@export var walk_speed := 1.4
@export var run_speed := 3.6
@export var gestures: PackedStringArray = []
var anim: AnimationPlayer
var current := ""
var busy := false
@onready var model: Node3D = $Model
@onready var pivot: Node3D = $Pivot

func _ready() -> void:
	anim = model.find_child("AnimationPlayer", true, false) as AnimationPlayer
	if anim:
		anim.animation_finished.connect(func(_n): busy = false)
		for n in ["idle", "walk", "run"]:
			if anim.has_animation(n):
				anim.get_animation(n).loop_mode = Animation.LOOP_LINEAR
	Input.mouse_mode = Input.MOUSE_MODE_CAPTURED
	play("idle")

func play(n: String) -> void:
	if anim and anim.has_animation(n) and n != current:
		anim.play(n, 0.25)
		current = n

func _unhandled_input(e: InputEvent) -> void:
	if e is InputEventMouseMotion and Input.mouse_mode == Input.MOUSE_MODE_CAPTURED:
		pivot.rotation.y -= e.relative.x * 0.004
		pivot.rotation.x = clamp(pivot.rotation.x - e.relative.y * 0.003, -0.9, 0.4)
	elif e is InputEventKey and e.pressed:
		if e.keycode == KEY_ESCAPE:
			Input.mouse_mode = Input.MOUSE_MODE_VISIBLE
		var i: int = e.keycode - KEY_1
		if i >= 0 and i < gestures.size():
			busy = true
			current = ""
			play(gestures[i])
	elif e is InputEventMouseButton and e.pressed:
		Input.mouse_mode = Input.MOUSE_MODE_CAPTURED

func _physics_process(dt: float) -> void:
	var v := Vector2(
		float(Input.is_key_pressed(KEY_D) or Input.is_key_pressed(KEY_RIGHT)) - float(Input.is_key_pressed(KEY_A) or Input.is_key_pressed(KEY_LEFT)),
		float(Input.is_key_pressed(KEY_S) or Input.is_key_pressed(KEY_DOWN)) - float(Input.is_key_pressed(KEY_W) or Input.is_key_pressed(KEY_UP)))
	var running := Input.is_key_pressed(KEY_SHIFT)
	var dir := (pivot.global_transform.basis * Vector3(v.x, 0, v.y))
	dir.y = 0
	if not is_on_floor():
		velocity.y -= 9.8 * dt
	if dir.length() > 0.1:
		dir = dir.normalized()
		var speed := run_speed if running else walk_speed
		velocity.x = dir.x * speed
		velocity.z = dir.z * speed
		model.rotation.y = lerp_angle(model.rotation.y, atan2(dir.x, dir.z), dt * 10.0)
		busy = false
		play("run" if running else "walk")
	else:
		velocity.x = move_toward(velocity.x, 0, dt * 12.0)
		velocity.z = move_toward(velocity.z, 0, dt * 12.0)
		if not busy:
			play("idle")
	move_and_slide()
'''

NPC_GD = '''extends Node3D
# A character standing in the scene: plays idle (looped), and now and then a gesture.
@export var gestures: PackedStringArray = []
var anim: AnimationPlayer
var t := 0.0

func _ready() -> void:
	anim = find_child("AnimationPlayer", true, false) as AnimationPlayer
	if anim and anim.has_animation("idle"):
		anim.get_animation("idle").loop_mode = Animation.LOOP_LINEAR
		anim.play("idle")
		anim.animation_finished.connect(func(_n): anim.play("idle", 0.3))
	t = randf_range(3.0, 8.0)

func _process(dt: float) -> void:
	t -= dt
	if t <= 0 and anim and gestures.size() > 0:
		var g := gestures[randi() % gestures.size()]
		if anim.has_animation(g):
			anim.play(g, 0.3)
		t = randf_range(6.0, 12.0)
'''


def _tscn(player: str, npcs: list[tuple[str, list[float], float]], props: list[tuple[str, list[float], float, float]],
          sky: str | None, gestures: list[str]) -> str:
    """The main scene in Godot's text format."""
    res, n = [], 0

    def ext(kind: str, path: str) -> int:
        nonlocal n
        n += 1
        res.append(f'[ext_resource type="{kind}" path="res://{path}" id="{n}"]')
        return n

    pl_scene, pl_script, npc_script = ext("PackedScene", f"characters/{player}.glb"), ext("Script", "player.gd"), ext("Script", "npc.gd")
    npc_ids = [(ext("PackedScene", f"characters/{name}.glb"), at, face) for name, at, face in npcs]
    prop_ids = [(ext("PackedScene", path), at, rot, scale) for path, at, rot, scale in props]
    sky_id = ext("Texture2D", sky) if sky else None
    g = ", ".join(json.dumps(x) for x in gestures)
    out = [f"[gd_scene load_steps={n + 8} format=3]", "", *res, "",
           '[sub_resource type="BoxShape3D" id="floor_shape"]\nsize = Vector3(80, 1, 80)', "",
           '[sub_resource type="PlaneMesh" id="floor_mesh"]\nsize = Vector2(80, 80)', "",
           '[sub_resource type="StandardMaterial3D" id="floor_mat"]\nalbedo_color = Color(0.55, 0.57, 0.58, 1)\nroughness = 0.95', "",
           '[sub_resource type="CapsuleShape3D" id="body"]\nradius = 0.28\nheight = 1.75', ""]
    if sky_id:
        out += [f'[sub_resource type="PanoramaSkyMaterial" id="skymat"]\npanorama = ExtResource("{sky_id}")', "",
                '[sub_resource type="Sky" id="sky"]\nsky_material = SubResource("skymat")', "",
                '[sub_resource type="Environment" id="env"]\nbackground_mode = 2\nsky = SubResource("sky")\nambient_light_source = 3\n'
                'reflected_light_source = 2\ntonemap_mode = 3\nssao_enabled = true\nglow_enabled = true', ""]
    else:
        out += ['[sub_resource type="Environment" id="env"]\nbackground_mode = 1\nbackground_color = Color(0.87, 0.88, 0.89, 1)\n'
                'ambient_light_source = 2\nambient_light_color = Color(0.8, 0.82, 0.86, 1)\ntonemap_mode = 3\nssao_enabled = true', ""]
    out += ['[node name="Main" type="Node3D"]', "",
            '[node name="World" type="WorldEnvironment" parent="."]\nenvironment = SubResource("env")', "",
            '[node name="Sun" type="DirectionalLight3D" parent="."]\ntransform = Transform3D(0.866, -0.354, 0.354, 0, 0.707, 0.707, -0.5, -0.612, 0.612, 0, 6, 0)\nshadow_enabled = true', "",
            '[node name="Floor" type="StaticBody3D" parent="."]\ntransform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 0, -0.5, 0)', "",
            '[node name="Shape" type="CollisionShape3D" parent="Floor"]\nshape = SubResource("floor_shape")', "",
            f'[node name="Mesh" type="MeshInstance3D" parent="Floor"]\ntransform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0.5, 0)\nmesh = SubResource("floor_mesh")\nsurface_material_override/0 = SubResource("floor_mat")'
, "",
            f'[node name="Player" type="CharacterBody3D" parent="."]\ntransform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0.05, 2)\nscript = ExtResource("{pl_script}")\ngestures = PackedStringArray({g})', "",
            '[node name="Body" type="CollisionShape3D" parent="Player"]\ntransform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0.875, 0)\nshape = SubResource("body")', "",
            f'[node name="Model" parent="Player" instance=ExtResource("{pl_scene}")]', "",
            '[node name="Pivot" type="Node3D" parent="Player"]\ntransform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 1.45, 0)', "",
            '[node name="Arm" type="SpringArm3D" parent="Player/Pivot"]\nspring_length = 3.2\nmargin = 0.1', "",
            '[node name="Camera" type="Camera3D" parent="Player/Pivot/Arm"]\nfov = 50.0', ""]
    import math
    for i, (sid, at, face) in enumerate(npc_ids):
        c, s = math.cos(math.radians(face)), math.sin(math.radians(face))
        out += [f'[node name="NPC{i + 1}" parent="." instance=ExtResource("{sid}")]\n'
                f'transform = Transform3D({c:.4f}, 0, {-s:.4f}, 0, 1, 0, {s:.4f}, 0, {c:.4f}, {at[0]}, 0, {at[1]})\n'
                f'script = ExtResource("{npc_script}")\ngestures = PackedStringArray({g})', ""]
    for i, (pid, at, rot, scale) in enumerate(prop_ids):
        c, s = math.cos(math.radians(rot)) * scale, math.sin(math.radians(rot)) * scale
        out += [f'[node name="Prop{i + 1}" parent="." instance=ExtResource("{pid}")]\n'
                f'transform = Transform3D({c:.4f}, 0, {-s:.4f}, 0, {scale}, 0, {s:.4f}, 0, {c:.4f}, {at[0]}, 0, {at[1]})', ""]
    return "\n".join(out)


class GodotParams(BaseModel):
    stage: str = "teen"
    player: str = Field("teen", description="Character the player controls (a model on the stage)")
    others: list[str] = Field(default_factory=list, max_length=6, description="Other characters standing in the scene")
    set: str | None = Field(None, description="A set on the stage (park, beach, cafe, ...); default a plain studio floor")
    moves: list[str] = Field(default_factory=lambda: ["idle", "walk", "run", "wave", "agree", "headShake", "shrug", "laugh", "dance", "jump"], max_length=40)
    name: str = Field("Builder Game", max_length=60)


@operation("export_godot", GodotParams, "Export characters and a set as a ready-to-open Godot 4 game project (zip): third-person player with walk/run/gestures, other characters idling, the set's sky and props, a floor. Open in Godot 4.x (free) and press Play.")
def export_godot(p: GodotParams) -> OpResult:
    from .scene import stages_dir

    stage = stages_dir() / p.stage
    if not (stage / "models").exists():
        raise OpError(f"no stage called {p.stage!r}")
    work = Path(tempfile.mkdtemp(prefix="godot-"))
    try:
        proj = work / storage.safe_name(p.name).replace(" ", "_")
        (proj / "characters").mkdir(parents=True)
        made = []
        for who in [p.player, *p.others]:
            if (proj / "characters" / f"{who}.glb").exists():
                continue
            res = run_operation("export_character", {"stage": p.stage, "character": who, "moves": p.moves, "morphs": True, "preview": None})
            made.append(res.outputs[0].id)
            shutil.copyfile(storage.path_of(res.outputs[0].id), proj / "characters" / f"{who}.glb")
        sky, props = None, []
        if p.set:
            sdir = stage / "sets" / p.set
            if not (sdir / "set.json").exists():
                raise OpError(f"no set called {p.set!r} on this stage")
            spec = json.loads((sdir / "set.json").read_text(encoding="utf-8"))
            shutil.copytree(sdir, proj / "set", ignore=shutil.ignore_patterns("set.json"))
            sky = f"set/{spec['sky']}"
            props = [(f"set/{x['file']}", x["at"], x.get("rot", 0), x.get("scale", 1)) for x in spec.get("props", [])]
        npcs = [(o, [1.2 * (i + 1) * (-1 if i % 2 else 1), 0.0], 180.0 + (-25 if i % 2 else 25)) for i, o in enumerate(p.others)]
        gest = [m for m in p.moves if m not in ("idle", "walk", "run")][:9]
        (proj / "main.tscn").write_text(_tscn(p.player, npcs, props, sky, gest), encoding="utf-8")
        (proj / "player.gd").write_text(PLAYER_GD, encoding="utf-8")
        (proj / "npc.gd").write_text(NPC_GD, encoding="utf-8")
        (proj / "project.godot").write_text(
            "config_version=5\n\n[application]\n\n"
            f'config/name="{p.name.replace(chr(34), "")}"\nrun/main_scene="res://main.tscn"\nconfig/features=PackedStringArray("4.3", "Forward Plus")\n\n'
            "[rendering]\n\nanti_aliasing/quality/msaa_3d=2\n", encoding="utf-8")
        credits = "Characters: MakeHuman (CC0). Motion: CMU Graphics Lab mocap (mocap.cs.cmu.edu, NSF EIA-0196217) and three.js sample clips (MIT)."
        if p.set:
            credits += " Set: Poly Haven (CC0)."
        (proj / "README.txt").write_text(
            f"{p.name} (made with Builder)\n\nOpen in Godot 4.3 or newer (free: https://godotengine.org): Import > this folder > Play (F5).\n"
            "Controls: WASD / arrows move, Shift runs, mouse looks, 1-9 play gestures: " + ", ".join(gest) + ". Esc frees the mouse.\n\n" + credits + "\n",
            encoding="utf-8")
        z = work / f"{proj.name}.zip"
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(proj.rglob("*")):
                if f.is_file():
                    zf.write(f, Path(proj.name) / f.relative_to(proj))
        return _guarded("export_godot", made, z.name, lambda out: shutil.copyfile(z, out),
                        data={"characters": [p.player, *p.others], "set": p.set, "gestures": gest, "engine": "Godot 4.3+"})
    finally:
        shutil.rmtree(work, ignore_errors=True)
