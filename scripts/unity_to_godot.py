#!/usr/bin/env python3
"""
unity_to_godot.py
=================
Migrate a Unity asset folder (FBX meshes + .mat materials + .prefab prefabs + PNG textures)
into a Godot 4 project:

  * copies meshes / textures into the Godot project
  * converts Unity URP-Lit `.mat` files into Godot `StandardMaterial3D` `.tres`
    (albedo / normal / ORM packing, emission, cull, transparency)
  * fixes texture import settings (VRAM compression, mipmaps, normal-map flag)
  * converts `.prefab` files into Godot `.tscn` prefab scenes
    (root Node3D + MeshInstance3D pointing at the imported FBX mesh, material applied)
  * builds a showcase scene that instantiates every prefab on a grid

Usage
-----
  python unity_to_godot.py \
      --unity-assets  "M:/Projects/TESTUnity/Assets/KitchenProps" \
      --godot-project "M:/Projects/test-godot" \
      --dest          "KitchenProps" \
      --godot-exe     "M:/Programs/Godot/Godot_v4.7.2-stable_win64.exe"

Only the Python standard library is required. Godot 4.3+ is required for FBX import.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# --------------------------------------------------------------------------------------
# Unity YAML (.mat / .prefab) parsing
# --------------------------------------------------------------------------------------

DOC_RE = re.compile(r"^--- !u!(\d+) &(-?\d+)(?: stripped)?[ \t]*$", re.M)

CLS_GAMEOBJECT = 1
CLS_TRANSFORM = 4
CLS_MATERIAL = 21
CLS_MESHRENDERER = 23
CLS_MESHFILTER = 33
CLS_RECTTRANSFORM = 224
CLS_PREFABINSTANCE = 1001


class UnityDoc:
    """Very small reader for Unity's YAML flavour (one file = many `--- !u!` documents)."""

    def __init__(self, path: Path):
        self.path = path
        self.text = path.read_text(encoding="utf-8", errors="replace")
        self.objects: list[tuple[int, int, str]] = []
        parts = DOC_RE.split(self.text)
        for i in range(1, len(parts) - 2, 3):
            self.objects.append((int(parts[i]), int(parts[i + 1]), parts[i + 2]))

    def body(self, class_id: int) -> str | None:
        for cid, _fid, body in self.objects:
            if cid == class_id:
                return body
        return None

    def bodies(self, class_id: int) -> list[str]:
        return [b for cid, _f, b in self.objects if cid == class_id]

    def has(self, class_id: int) -> bool:
        return any(cid == class_id for cid, _f, _b in self.objects)


def scalar(body: str, name: str, default: str = "") -> str:
    m = re.search(rf"^  {re.escape(name)}: (.*)$", body, re.M)
    return m.group(1).strip() if m else default


def vec3(body: str, name: str) -> tuple[float, float, float]:
    m = re.search(
        rf"^  {re.escape(name)}: \{{x: ([-\d.eE+]+), y: ([-\d.eE+]+), z: ([-\d.eE+]+)\}}",
        body,
        re.M,
    )
    if not m:
        return (0.0, 0.0, 0.0)
    return tuple(float(g) for g in m.groups())  # type: ignore[return-value]


def quat(body: str, name: str) -> tuple[float, float, float, float]:
    m = re.search(
        rf"^  {re.escape(name)}: \{{x: ([-\d.eE+]+), y: ([-\d.eE+]+), z: ([-\d.eE+]+), w: ([-\d.eE+]+)\}}",
        body,
        re.M,
    )
    if not m:
        return (0.0, 0.0, 0.0, 1.0)
    return tuple(float(g) for g in m.groups())  # type: ignore[return-value]


def parse_texenvs(body: str) -> dict[str, dict]:
    """Return {property_name: {'guid': str|None, 'scale': (x, y), 'offset': (x, y)}}."""
    out: dict[str, dict] = {}
    section = re.search(r"^    m_TexEnvs:\n(.*?)^    m_Ints:", body, re.M | re.S)
    if not section:
        return out
    block = section.group(1)
    entry_re = re.compile(
        r"^    - (_\w+):\n"
        r"        m_Texture: \{fileID: (-?\d+)(?:, guid: ([0-9a-f]+), type: \d+)?\}\n"
        r"        m_Scale: \{x: ([-\d.eE+]+), y: ([-\d.eE+]+)\}\n"
        r"        m_Offset: \{x: ([-\d.eE+]+), y: ([-\d.eE+]+)\}",
        re.M,
    )
    for m in entry_re.finditer(block):
        name, file_id, guid, sx, sy, ox, oy = m.groups()
        out[name] = {
            "guid": guid if file_id not in ("0", None) else None,
            "scale": (float(sx), float(sy)),
            "offset": (float(ox), float(oy)),
        }
    return out


def parse_floats(body: str) -> dict[str, float]:
    out: dict[str, float] = {}
    section = re.search(r"^    m_Floats:\n(.*?)^    m_Colors:", body, re.M | re.S)
    if not section:
        return out
    for m in re.finditer(r"^    - (_\w+): ([-\d.eE+]+)\s*$", section.group(1), re.M):
        out[m.group(1)] = float(m.group(2))
    return out


def parse_colors(body: str) -> dict[str, tuple[float, float, float, float]]:
    out: dict[str, tuple[float, float, float, float]] = {}
    section = re.search(r"^    m_Colors:\n(.*?)(?:^  \w|\Z)", body, re.M | re.S)
    if not section:
        return out
    for m in re.finditer(
        r"^    - (_\w+): \{r: ([-\d.eE+]+), g: ([-\d.eE+]+), b: ([-\d.eE+]+), a: ([-\d.eE+]+)\}",
        section.group(1),
        re.M,
    ):
        out[m.group(1)] = tuple(float(g) for g in m.groups()[1:])  # type: ignore[assignment]
    return out


# --------------------------------------------------------------------------------------
# Unity asset index (guid -> path)
# --------------------------------------------------------------------------------------


def build_guid_map(unity_dir: Path) -> dict[str, Path]:
    guid_map: dict[str, Path] = {}
    for meta in unity_dir.rglob("*.meta"):
        try:
            text = meta.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r"^guid: ([0-9a-f]+)\s*$", text, re.M)
        if m:
            guid_map[m.group(1)] = meta.with_suffix("")
    return guid_map


# --------------------------------------------------------------------------------------
# Material conversion
# --------------------------------------------------------------------------------------

# Unity URP Lit shader keyword -> our handling is property driven, keywords only inform us.

def fmt(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


ORM_MAS_RE = re.compile(r"^(.*)_(?:ORM_)?MAS$", re.I)


def resolve_orm_texture(
    metallic_tex: Path | None, ao_tex: Path | None
) -> tuple[str | None, Path | None]:
    """Work out which texture drives metallic/roughness/AO and how it is packed.

    Returns (packing, texture) where packing is:
      "orm"  -> R=AO, G=roughness, B=metallic  (Godot's native ORM layout)
      "mask" -> R=metallic, G=AO, A=smoothness (Unity URP mask map)
      None   -> no map, use scalar values

    Asset packs exported for several engines usually ship both `T_X_ORM.png`
    (Unreal/Godot layout) and `T_X_ORM_MAS.png` (Unity mask layout). Unity
    materials reference the MAS variant, so prefer the ORM sibling when present.
    """
    for cand in (metallic_tex, ao_tex):
        if cand is None:
            continue
        m = ORM_MAS_RE.match(cand.stem)
        if m:
            sibling = cand.with_name(m.group(1) + cand.suffix)
            if sibling.exists():
                return "orm", sibling
            return "mask", cand
        if "orm" in cand.stem.lower():
            return "orm", cand
    if metallic_tex is not None and metallic_tex == ao_tex:
        return "mask", metallic_tex
    return None, None


def color_literal(c: tuple[float, float, float, float]) -> str:
    return "Color({}, {}, {}, {})".format(*(fmt(x) for x in c))


class MaterialBuilder:
    def __init__(self, unity_dir: Path, godot_dir: Path, guid_map: dict[str, Path], dest: str):
        self.unity_dir = unity_dir
        self.godot_dir = godot_dir
        self.guid_map = guid_map
        self.dest = dest
        self.textures_subdir = "textures"
        self.used_textures: set[Path] = set()

    # -- helpers -----------------------------------------------------------------
    def res_path(self, unity_asset: Path) -> str:
        """Godot res:// path of a copied asset (textures/meshes keep their file name)."""
        parent = unity_asset.parent.name.lower()
        sub = "meshes" if parent == "meshes" else self.textures_subdir
        return f"res://{self.dest}/{sub}/{unity_asset.name}"

    def tex_by_guid(self, guid: str | None) -> Path | None:
        if not guid:
            return None
        p = self.guid_map.get(guid)
        return p if p and p.suffix.lower() in (".png", ".tga", ".jpg", ".jpeg", ".exr", ".hdr") else None

    # -- main --------------------------------------------------------------------
    def build(self, mat_file: Path) -> tuple[str, str]:
        """Return (godot_file_name, tres_text)."""
        doc = UnityDoc(mat_file)
        body = doc.body(CLS_MATERIAL)
        if body is None:
            raise ValueError(f"{mat_file}: no Material document")

        name = scalar(body, "m_Name") or mat_file.stem
        texenvs = parse_texenvs(body)
        floats = parse_floats(body)
        colors = parse_colors(body)

        ext: list[tuple[str, str, str]] = []  # (type, res_path, id)

        def add_tex(kind: str, asset: Path) -> str:
            rid = f"{len(ext) + 1}_{kind}"
            ext.append(("Texture2D", self.res_path(asset), rid))
            self.used_textures.add(asset.resolve())
            return rid

        props: list[str] = [f'resource_name = "{name}"']

        # ---- albedo -------------------------------------------------------------
        base = texenvs.get("_BaseMap") or texenvs.get("_MainTex") or {}
        albedo_tex = self.tex_by_guid(base.get("guid"))
        base_color = colors.get("_BaseColor") or colors.get("_Color") or (1, 1, 1, 1)
        props.append(f"albedo_color = {color_literal(base_color)}")
        if albedo_tex:
            props.append(f'albedo_texture = ExtResource("{add_tex("albedo", albedo_tex)}")')
        if base and (base["scale"] != (1.0, 1.0) or base["offset"] != (0.0, 0.0)):
            sx, sy = base["scale"]
            ox, oy = base["offset"]
            props.append(f"uv1_scale = Vector3({fmt(sx)}, {fmt(sy)}, 1)")
            props.append(f"uv1_offset = Vector3({fmt(ox)}, {fmt(oy)}, 0)")

        # ---- normal -------------------------------------------------------------
        bump = texenvs.get("_BumpMap") or {}
        normal_tex = self.tex_by_guid(bump.get("guid"))
        if normal_tex:
            props.append("normal_enabled = true")
            props.append(f'normal_texture = ExtResource("{add_tex("normal", normal_tex)}")')
            props.append(f"normal_scale = {fmt(floats.get('_BumpScale', 1.0))}")

        # ---- metallic / roughness / AO ------------------------------------------
        # Unity URP stores either an ORM map (R=AO, G=roughness, B=metallic â€” the
        # Unreal-style packing this asset pack uses) or a Metallic/Smoothness map
        # (R=metallic, A=smoothness). Godot's ORM layout is R=AO, G=rough, B=metal.
        mg_tex = self.tex_by_guid((texenvs.get("_MetallicGlossMap") or {}).get("guid"))
        ao_tex = self.tex_by_guid((texenvs.get("_OcclusionMap") or {}).get("guid"))
        packing, orm_tex = resolve_orm_texture(mg_tex, ao_tex)

        metallic = floats.get("_Metallic", 0.0)
        smoothness = floats.get("_Smoothness", floats.get("_Glossiness", 0.5))

        if packing == "orm" and orm_tex is not None:
            # already in Godot's layout: R = AO, G = roughness, B = metallic
            rid = add_tex("orm", orm_tex)
            props.append(f'metallic_texture = ExtResource("{rid}")')
            props.append("metallic_texture_channel = 2")   # B
            props.append(f'roughness_texture = ExtResource("{rid}")')
            props.append("roughness_texture_channel = 1")  # G
            props.append(f'ao_texture = ExtResource("{rid}")')
            props.append("ao_texture_channel = 0")         # R
            props.append("ao_enabled = true")
            # scalars act as multipliers on the sampled channels
            props.append("metallic = 1.0")
            props.append("roughness = 1.0")
            props.append("metallic_specular = 0.5")
        elif packing == "mask" and orm_tex is not None:
            # Unity URP mask map: R = metallic, G = AO, A = smoothness
            rid = add_tex("mask", orm_tex)
            props.append(f'metallic_texture = ExtResource("{rid}")')
            props.append("metallic_texture_channel = 0")   # R
            props.append(f'ao_texture = ExtResource("{rid}")')
            props.append("ao_texture_channel = 1")         # G
            props.append("ao_enabled = true")
            props.append("metallic = 1.0")
            props.append(f"roughness = {fmt(1.0 - smoothness)}")
            props.append("metallic_specular = 0.5")
        else:
            props.append(f"metallic = {fmt(metallic)}")
            props.append(f"roughness = {fmt(1.0 - smoothness)}")

        # ---- emission -----------------------------------------------------------
        em_tex = self.tex_by_guid((texenvs.get("_EmissionMap") or {}).get("guid"))
        em_color = colors.get("_EmissionColor", (0, 0, 0, 1))
        em_strength = max(em_color[0], em_color[1], em_color[2])
        if em_tex or em_strength > 0.0:
            props.append("emission_enabled = true")
            props.append(f"emission = {color_literal((1, 1, 1, 1) if em_strength == 0 else (em_color[0], em_color[1], em_color[2], 1))}")
            props.append(f"emission_energy_multiplier = {fmt(em_strength if em_strength > 0 else 1.0)}")
            if em_tex:
                # Godot 4.7 defaults emission_operator to ADD; Unity multiplies the
                # emission colour by the emission map, which is operator 1 (Multiply).
                props.append("emission_operator = 1")
                props.append(f'emission_texture = ExtResource("{add_tex("emission", em_tex)}")')

        # ---- cull / transparency ------------------------------------------------
        cull = int(floats.get("_Cull", 2))          # Unity: 0 off, 1 front, 2 back
        props.append(f"cull_mode = { {0: 2, 1: 1, 2: 0}[cull] }")

        surface = int(floats.get("_Surface", 0))    # 0 opaque, 1 transparent
        alpha_clip = floats.get("_AlphaClip", 0.0) > 0.5
        if alpha_clip:
            props.append("transparency = 1")        # alpha scissor
            props.append(f"alpha_scissor_threshold = {fmt(floats.get('_Cutoff', 0.5))}")
        elif surface == 1:
            blend = int(floats.get("_Blend", 0))
            props.append("transparency = 1")
            props.append(f"blend_mode = { {0: 1, 1: 2, 2: 3, 3: 4}.get(blend, 1) }")
            props.append("cull_mode = 2")           # transparent surfaces are usually double sided

        if floats.get("_AlphaToMask", 0.0) > 0.5:
            props.append("alpha_antialiasing_mode = 1")

        lines = ["[gd_resource type=\"StandardMaterial3D\" format=3]", ""]
        for rtype, rpath, rid in ext:
            lines.append(f'[ext_resource type="{rtype}" path="{rpath}" id="{rid}"]')
        if ext:
            lines.append("")
        lines.append("[resource]")
        lines.extend(props)
        lines.append("")
        return f"{name}.tres", "\n".join(lines)


# --------------------------------------------------------------------------------------
# Prefab conversion
# --------------------------------------------------------------------------------------


def tf_string(
    x_axis: tuple[float, float, float],
    y_axis: tuple[float, float, float],
    z_axis: tuple[float, float, float],
    origin: tuple[float, float, float],
) -> str:
    """Serialise a Godot Transform3D for a .tscn file.

    In the text format the nine basis numbers are stored row-major, i.e. the
    transpose of the (x_axis, y_axis, z_axis) vectors, followed by the origin.
    """
    rows = [
        (x_axis[0], y_axis[0], z_axis[0]),
        (x_axis[1], y_axis[1], z_axis[1]),
        (x_axis[2], y_axis[2], z_axis[2]),
    ]
    vals = [*(v for row in rows for v in row), *origin]
    return "Transform3D({})".format(", ".join(fmt(v) for v in vals))


def quat_to_transform(
    q: tuple[float, float, float, float],
    pos: tuple[float, float, float],
    scale: tuple[float, float, float],
) -> str:
    """Unity local TRS -> Godot `Transform3D(...)` text.

    Unity and Godot both evaluate the quaternion with the same maths; Godot's FBX
    importer already performs the axis conversion on the geometry, so the prefab's
    local transform is copied numerically. Scale is baked into the basis vectors.
    """
    x, y, z, w = q
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    m = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    sx, sy, sz = scale
    x_axis = (m[0][0] * sx, m[1][0] * sx, m[2][0] * sx)
    y_axis = (m[0][1] * sy, m[1][1] * sy, m[2][1] * sy)
    z_axis = (m[0][2] * sz, m[1][2] * sz, m[2][2] * sz)
    return tf_string(x_axis, y_axis, z_axis, pos)


def parse_prefab(prefab: Path) -> dict:
    doc = UnityDoc(prefab)
    go = doc.body(CLS_GAMEOBJECT) or ""
    tr = doc.body(CLS_TRANSFORM) or doc.body(CLS_RECTTRANSFORM) or ""
    mf = doc.body(CLS_MESHFILTER) or ""
    mr = doc.body(CLS_MESHRENDERER) or ""

    mesh_guid = None
    m = re.search(r"^  m_Mesh: \{fileID: -?\d+, guid: ([0-9a-f]+), type: \d+\}", mf, re.M)
    if m:
        mesh_guid = m.group(1)

    materials: list[str | None] = []
    mats_block = re.search(r"^  m_Materials:\n(.*?)^  m_StaticBatchInfo:", mr, re.M | re.S)
    if mats_block:
        for line in mats_block.group(1).splitlines():
            mm = re.search(r"guid: ([0-9a-f]+)", line)
            materials.append(mm.group(1) if mm else None)

    return {
        "name": scalar(go, "m_Name") or prefab.stem,
        "active": scalar(go, "m_IsActive", "1") == "1",
        "rotation": quat(tr, "m_LocalRotation"),
        "position": vec3(tr, "m_LocalPosition"),
        "scale": vec3(tr, "m_LocalScale"),
        "mesh_guid": mesh_guid,
        "materials": materials,
    }


# --------------------------------------------------------------------------------------
# Godot helpers
# --------------------------------------------------------------------------------------


def run_godot(exe: str, project: Path, args: list[str], quiet: bool = True) -> subprocess.CompletedProcess:
    cmd = [exe, "--headless", "--path", str(project), *args]
    res = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if quiet and res.returncode != 0:
        sys.stderr.write(res.stdout[-4000:] + "\n" + res.stderr[-4000:] + "\n")
    return res


DUMP_GDSCRIPT = '''extends SceneTree

# For every imported mesh file, save the mesh as a standalone .res resource so that
# scenes can reference it with a stable path (Godot cannot load "file.fbx::SubResource"
# from scripts or .tscn ext_resources), and report the mapping as JSON.
func _init() -> void:
	var args := OS.get_cmdline_user_args()
	var src := args[0]
	var out_dir := args[1]
	var json_path := args[2]
	DirAccess.make_dir_recursive_absolute(out_dir)
	var result := {}
	for f in DirAccess.get_files_at(src):
		var lower := f.to_lower()
		if not (lower.ends_with(".fbx") or lower.ends_with(".glb") or lower.ends_with(".gltf") or lower.ends_with(".dae") or lower.ends_with(".obj")):
			continue
		var ps: PackedScene = load(src.path_join(f))
		if ps == null:
			push_warning("could not load " + src.path_join(f))
			continue
		var inst := ps.instantiate()
		var mesh: Mesh = null
		for n in _mesh_nodes(inst):
			mesh = n.mesh
			break
		if mesh == null:
			push_warning("no mesh inside " + f)
			inst.free()
			continue
		# drop the placeholder materials created by the FBX importer
		for i in mesh.get_surface_count():
			mesh.surface_set_material(i, null)
		var out_path := out_dir.path_join(f.get_basename() + ".res")
		var err := ResourceSaver.save(mesh, out_path)
		if err == OK:
			result[f] = out_path
		else:
			push_error("failed saving " + out_path)
		inst.free()
	var fa := FileAccess.open(json_path, FileAccess.WRITE)
	fa.store_string(JSON.stringify(result, "\\t"))
	fa.close()
	quit()

func _mesh_nodes(n: Node) -> Array:
	var out := []
	if n is MeshInstance3D and n.mesh != null:
		out.append(n)
	for c in n.get_children():
		out.append_array(_mesh_nodes(c))
	return out
'''


def extract_meshes(exe: str, project: Path, meshes_rel: str, out_rel: str) -> dict[str, str]:
    """Save every imported mesh as a standalone resource and return {file name: res:// path}."""
    tool_dir = project / "_migration_tools"
    tool_dir.mkdir(exist_ok=True)
    script = tool_dir / "extract_meshes.gd"
    script.write_text(DUMP_GDSCRIPT, encoding="utf-8")
    out_json = tool_dir / "mesh_ids.json"
    out_json.unlink(missing_ok=True)
    run_godot(
        exe,
        project,
        [
            "--script",
            "res://_migration_tools/extract_meshes.gd",
            "--",
            f"res://{meshes_rel}",
            f"res://{out_rel}",
            "res://_migration_tools/mesh_ids.json",
        ],
    )
    if not out_json.exists():
        return {}
    return json.loads(out_json.read_text(encoding="utf-8"))


TEXTURE_IMPORT_PARAMS = {
    "compress/mode": "2",           # VRAM compressed
    "mipmaps/generate": "true",
    "detect_3d/compress_to": "0",   # keep our settings, don't let the editor "detect" them
}


def patch_texture_imports(textures_dir: Path) -> int:
    """Set 3D-friendly import flags; enable the normal-map flag for *_N maps."""
    changed = 0
    for imp in textures_dir.glob("*.import"):
        text = imp.read_text(encoding="utf-8")
        params = dict(TEXTURE_IMPORT_PARAMS)
        # "T_Barrel_N.png.import" -> "T_Barrel_N"
        base_stem = Path(imp.name[: -len(".import")]).stem
        is_normal = bool(re.search(r"(_n|_nrm|_normal|_nor)$", base_stem, re.I))
        params["compress/normal_map"] = "1" if is_normal else "0"
        new = text
        for key, value in params.items():
            pat = re.compile(rf"^{re.escape(key)}=.*$", re.M)
            if pat.search(new):
                new = pat.sub(f"{key}={value}", new)
            else:
                new = new.replace("[params]\n", f"[params]\n{key}={value}\n", 1)
        if new != text:
            imp.write_text(new, encoding="utf-8")
            changed += 1
    return changed


def write_showcase(
    out_path: Path,
    prefabs: list[tuple[str, str]],
    *,
    columns: int = 10,
    spacing: float = 2.6,
) -> None:
    """Build a scene that instances every prefab on a grid with a floor, sun and camera."""
    n = len(prefabs)
    rows = max(1, math.ceil(n / columns))
    width = (columns - 1) * spacing
    depth = (rows - 1) * spacing

    ext_lines = [
        f'[ext_resource type="PackedScene" path="res://{res}" id="p{i}"]' for i, (_, res) in enumerate(prefabs)
    ]

    floor_w = max(24.0, width + spacing * 6)
    floor_d = max(24.0, depth + spacing * 6)

    sub_lines = [
        '[sub_resource type="PlaneMesh" id="PlaneMesh_floor"]',
        f"size = Vector2({fmt(floor_w)}, {fmt(floor_d)})",
        "",
        '[sub_resource type="StandardMaterial3D" id="StandardMaterial3D_floor"]',
        "albedo_color = Color(0.24, 0.235, 0.23, 1)",
        "roughness = 0.85",
        "metallic = 0",
        "",
        '[sub_resource type="ProceduralSkyMaterial" id="ProceduralSkyMaterial_sky"]',
        "sky_top_color = Color(0.34, 0.44, 0.6, 1)",
        "sky_horizon_color = Color(0.62, 0.63, 0.62, 1)",
        "ground_bottom_color = Color(0.3, 0.3, 0.31, 1)",
        "ground_horizon_color = Color(0.58, 0.58, 0.57, 1)",
        "",
        '[sub_resource type="Sky" id="Sky_main"]',
        'sky_material = SubResource("ProceduralSkyMaterial_sky")',
        "",
        '[sub_resource type="Environment" id="Environment_main"]',
        'background_mode = 2',
        'sky = SubResource("Sky_main")',
        'tonemap_mode = 3',
        "ambient_light_source = 3",
        "ambient_light_sky_contribution = 0.85",
        "ambient_light_energy = 1.3",
        "ssao_enabled = true",
        "glow_enabled = true",
        "glow_intensity = 0.35",
    ]

    # camera: frame the whole grid (binary-searched distance + centred target)
    cam_pos, cam_target, cam_x, cam_y, cam_z = fit_camera(width + spacing * 1.2, depth + spacing * 1.2, max_height=2.6, margin=0.9)
    cam_tf = tf_string(cam_x, cam_y, cam_z, cam_pos)

    body = []
    body.append('[node name="KitchenPropsShowcase" type="Node3D"]')
    body.append("")
    body.append('[node name="WorldEnvironment" type="WorldEnvironment" parent="."]')
    body.append('environment = SubResource("Environment_main")')
    body.append("")
    sun_pos = (0.0, 8.0, 0.0)
    sun_dir = (0.45, -1.0, -0.55)
    sun_x, sun_y, sun_z = look_at_basis(sun_pos, (sun_pos[0] + sun_dir[0], sun_pos[1] + sun_dir[1], sun_pos[2] + sun_dir[2]))
    body.append('[node name="Sun" type="DirectionalLight3D" parent="."]')
    body.append(f"transform = {tf_string(sun_x, sun_y, sun_z, sun_pos)}")
    body.append("light_energy = 2.0")
    body.append("shadow_enabled = true")
    body.append("directional_shadow_max_distance = 120.0")
    body.append("")
    body.append('[node name="Floor" type="MeshInstance3D" parent="."]')
    body.append('mesh = SubResource("PlaneMesh_floor")')
    body.append('surface_material_override/0 = SubResource("StandardMaterial3D_floor")')
    body.append("")
    body.append('[node name="Camera3D" type="Camera3D" parent="."]')
    body.append(f"transform = {cam_tf}")
    body.append("fov = 50.0")
    body.append("current = true")
    body.append("")
    body.append('[node name="Prefabs" type="Node3D" parent="."]')
    body.append("")
    body.append('[node name="Labels" type="Node3D" parent="."]')
    body.append("")

    for i, (name, res) in enumerate(prefabs):
        col = i % columns
        row = i // columns
        x = col * spacing - width / 2.0
        z = row * spacing - depth / 2.0
        body.append(f'[node name="{name}" parent="Prefabs" instance=ExtResource("p{i}")]')
        body.append(f"transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, {fmt(x)}, 0, {fmt(z)})")
        body.append("")
        body.append(f'[node name="Label_{name}" type="Label3D" parent="Labels"]')
        label_tf = tf_string((1, 0, 0), (0, 0, -1), (0, 1, 0), (x, 0.02, z + spacing * 0.42))
        body.append(f"transform = {label_tf}")
        body.append(f'text = "{name}"')
        body.append("font_size = 48")
        body.append("pixel_size = 0.004")
        body.append("modulate = Color(0.92, 0.92, 0.88, 1)")
        body.append("outline_size = 10")
        body.append("outline_modulate = Color(0.05, 0.05, 0.05, 1)")
        body.append("no_depth_test = false")
        body.append("")

    load_steps = len(prefabs) + 6  # ext resources + 5 sub-resources + 1
    text = "\n".join(
        [f"[gd_scene load_steps={load_steps} format=3]", ""]
        + ext_lines
        + [""]
        + sub_lines
        + [""]
        + body
    )
    out_path.write_text(text, encoding="utf-8")


def fit_camera(
    width: float,
    depth: float,
    max_height: float = 2.5,
    pitch_deg: float = 40.0,
    fov_deg: float = 50.0,
    aspect: float = 16.0 / 9.0,
    margin: float = 0.86,
):
    """Find a camera that frames a width x depth grid at the origin.

    Returns (eye, target, x_axis, y_axis, z_axis). The distance is binary-searched so
    that every grid corner stays inside the frustum, then the look-at target is nudged
    so the grid sits centred in the frame (perspective makes the near edge project
    much lower than the far edge).
    """
    pitch = math.radians(pitch_deg)
    tan_v = math.tan(math.radians(fov_deg / 2.0)) * margin
    tan_h = tan_v * aspect
    corners = []
    for sx in (-1, 1):
        for sz in (-1, 1):
            for y in (0.0, max_height):
                corners.append((sx * width / 2.0, y, sz * depth / 2.0))

    def view_of(eye, target, p):
        x_axis, y_axis, z_axis = look_at_basis(eye, target)
        rel = (p[0] - eye[0], p[1] - eye[1], p[2] - eye[2])
        vx = rel[0] * x_axis[0] + rel[1] * x_axis[1] + rel[2] * x_axis[2]
        vy = rel[0] * y_axis[0] + rel[1] * y_axis[1] + rel[2] * y_axis[2]
        vz = rel[0] * z_axis[0] + rel[1] * z_axis[1] + rel[2] * z_axis[2]
        return vx, vy, -vz

    target = [0.0, 0.0, 0.0]
    eye = (0.0, 0.0, 0.0)
    for _ in range(6):
        def fits(dist: float) -> bool:
            e = (
                target[0],
                target[1] + math.sin(pitch) * dist,
                target[2] + math.cos(pitch) * dist,
            )
            for p in corners:
                vx, vy, depth_view = view_of(e, target, p)
                if depth_view <= 0.1:
                    return False
                if abs(vx / depth_view) > tan_h or abs(vy / depth_view) > tan_v:
                    return False
            return True

        lo, hi = 0.5, 1000.0
        for _i in range(50):
            mid = (lo + hi) / 2.0
            if fits(mid):
                hi = mid
            else:
                lo = mid
        dist = hi
        eye = (target[0], target[1] + math.sin(pitch) * dist, target[2] + math.cos(pitch) * dist)

        # centre the projected grid in the frame
        ndc = []
        for p in corners:
            vx, vy, depth_view = view_of(eye, target, p)
            ndc.append((vx / depth_view / tan_h, vy / depth_view / tan_v))
        cx = (max(n[0] for n in ndc) + min(n[0] for n in ndc)) / 2.0
        cy = (max(n[1] for n in ndc) + min(n[1] for n in ndc)) / 2.0
        if abs(cx) < 0.01 and abs(cy) < 0.01:
            break
        x_axis, y_axis, _z = look_at_basis(eye, target)
        shift_x = cx * dist * tan_h
        shift_y = cy * dist * tan_v
        target[0] += x_axis[0] * shift_x + y_axis[0] * shift_y
        target[1] += x_axis[1] * shift_x + y_axis[1] * shift_y
        target[2] += x_axis[2] * shift_x + y_axis[2] * shift_y

    x_axis, y_axis, z_axis = look_at_basis(eye, tuple(target))
    return eye, tuple(target), x_axis, y_axis, z_axis


def look_at_basis(eye, target, up=(0.0, 1.0, 0.0)):
    """Return (x_axis, y_axis, z_axis) of a Godot camera looking from eye to target."""
    def sub(a, b):
        return (a[0] - b[0], a[1] - b[1], a[2] - b[2])

    def norm(v):
        l = math.sqrt(sum(c * c for c in v)) or 1.0
        return (v[0] / l, v[1] / l, v[2] / l)

    def cross(a, b):
        return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])

    z = norm(sub(eye, target))          # camera looks down -Z
    x = norm(cross(up, z))
    y = cross(z, x)
    return x, y, z


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="Unity assets -> Godot 4 project")
    ap.add_argument("--unity-assets", required=True, help="Unity asset folder (contains .mat/.prefab/Meshes/Textures)")
    ap.add_argument("--godot-project", required=True, help="Godot project root (contains project.godot)")
    ap.add_argument("--dest", default="", help="sub-folder inside the Godot project (default: asset folder name)")
    ap.add_argument("--godot-exe", default="godot", help="path to the Godot 4 editor executable")
    ap.add_argument("--showcase", default="", help="showcase scene file name (default: <dest>Showcase.tscn)")
    ap.add_argument("--columns", type=int, default=10, help="showcase grid columns")
    ap.add_argument("--spacing", type=float, default=3.0, help="showcase grid spacing in meters")
    ap.add_argument("--skip-import", action="store_true", help="do not run the engine importer")
    ap.add_argument("--skip-showcase", action="store_true", help="do not build the showcase scene")
    ap.add_argument(
        "--skip-unused-textures",
        action="store_true",
        help="only copy textures referenced by a converted material (drops e.g. *_ORM_MAS duplicates)",
    )
    args = ap.parse_args()

    unity_dir = Path(args.unity_assets).resolve()
    project = Path(args.godot_project).resolve()
    dest = args.dest or unity_dir.name
    godot_exe = args.godot_exe

    if not (project / "project.godot").exists():
        sys.exit(f"{project} does not look like a Godot project")

    dest_root = project / dest
    meshes_dir = dest_root / "meshes"
    textures_dir = dest_root / "textures"
    materials_dir = dest_root / "materials"
    prefabs_dir = dest_root / "prefabs"
    for d in (meshes_dir, textures_dir, materials_dir, prefabs_dir):
        d.mkdir(parents=True, exist_ok=True)

    print(f"Unity   : {unity_dir}")
    print(f"Godot   : {project}  ->  res://{dest}")

    guid_map = build_guid_map(unity_dir)
    print(f"indexed {len(guid_map)} Unity .meta guid(s)")

    # ---- 1. materials (built first so we know which textures are needed) -------
    mb = MaterialBuilder(unity_dir, project, guid_map, dest)
    mat_guid_to_res: dict[str, str] = {}
    made_materials = 0
    for mat_file in sorted(unity_dir.rglob("*.mat")):
        try:
            file_name, text = mb.build(mat_file)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! skipped {mat_file.name}: {exc}")
            continue
        (materials_dir / file_name).write_text(text, encoding="utf-8")
        made_materials += 1
        meta = mat_file.with_suffix(mat_file.suffix + ".meta")
        if meta.exists():
            m = re.search(r"^guid: ([0-9a-f]+)\s*$", meta.read_text(encoding="utf-8", errors="replace"), re.M)
            if m:
                mat_guid_to_res[m.group(1)] = f"{dest}/materials/{file_name}"
    print(f"wrote {made_materials} material(s)")

    # ---- 2. copy source assets -------------------------------------------------
    copied = {"meshes": 0, "textures": 0}
    skipped_tex: list[str] = []
    for folder in unity_dir.iterdir():
        if not folder.is_dir():
            continue
        for f in folder.iterdir():
            if f.suffix.lower() in (".fbx", ".obj", ".dae", ".gltf", ".glb"):
                shutil.copy2(f, meshes_dir / f.name)
                copied["meshes"] += 1
            elif f.suffix.lower() in (".png", ".tga", ".jpg", ".jpeg", ".exr", ".hdr"):
                if args.skip_unused_textures and f.resolve() not in mb.used_textures:
                    skipped_tex.append(f.name)
                    continue
                shutil.copy2(f, textures_dir / f.name)
                copied["textures"] += 1
    print(f"copied {copied['meshes']} mesh file(s), {copied['textures']} texture(s)")
    if skipped_tex:
        print(f"  skipped {len(skipped_tex)} texture(s) not referenced by any material")

    # ---- 3. import + fix texture settings --------------------------------------
    if not args.skip_import:
        print("running Godot importer ...")
        run_godot(godot_exe, project, ["--import"])
        changed = patch_texture_imports(textures_dir)
        print(f"patched {changed} texture import setting file(s); re-importing ...")
        run_godot(godot_exe, project, ["--import"])

    # ---- 4. prefabs ------------------------------------------------------------
    extracted_rel = f"{dest}/meshes/extracted"
    # always extract: the FBX files are already imported at this point even with --skip-import
    mesh_ids = extract_meshes(godot_exe, project, f"{dest}/meshes", extracted_rel)
    if not mesh_ids:
        print("  ! could not extract meshes - prefab scenes will have no mesh")

    def mesh_res_for(guid: str | None) -> str | None:
        asset = guid_map.get(guid) if guid else None
        if not asset:
            return None
        return mesh_ids.get(asset.name) or None

    made_prefabs: list[tuple[str, str]] = []
    for prefab_file in sorted(unity_dir.rglob("*.prefab")):
        info = parse_prefab(prefab_file)
        mesh_res = mesh_res_for(info["mesh_guid"])
        mat_res = None
        for g in info["materials"]:
            if g and g in mat_guid_to_res:
                mat_res = mat_guid_to_res[g]
                break
        name = info["name"]

        ext = []
        if mesh_res:
            ext.append(("ArrayMesh", mesh_res, "1_mesh"))
        if mat_res:
            ext.append(("Material", "res://" + mat_res, "2_material"))

        lines = [f"[gd_scene load_steps={len(ext) + 1} format=3]", ""]
        for rtype, rpath, rid in ext:
            lines.append(f'[ext_resource type="{rtype}" path="{rpath}" id="{rid}"]')
        if ext:
            lines.append("")
        lines.append(f'[node name="{name}" type="Node3D"]')
        lines.append("")
        if mesh_res:
            lines.append('[node name="Mesh" type="MeshInstance3D" parent="."]')
            tf = quat_to_transform(info["rotation"], info["position"], info["scale"])
            if tf != "Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0)":
                lines.append(f"transform = {tf}")
            lines.append('mesh = ExtResource("1_mesh")')
            if mat_res:
                lines.append('surface_material_override/0 = ExtResource("2_material")')
            lines.append("")
        (prefabs_dir / f"{name}.tscn").write_text("\n".join(lines), encoding="utf-8")
        made_prefabs.append((name, f"{dest}/prefabs/{name}.tscn"))

    print(f"wrote {len(made_prefabs)} prefab scene(s)")

    # ---- 5. showcase -----------------------------------------------------------
    if not args.skip_showcase and made_prefabs:
        showcase_name = args.showcase or f"{dest}Showcase.tscn"
        showcase = dest_root / showcase_name
        write_showcase(showcase, made_prefabs, columns=args.columns, spacing=args.spacing)
        print(f"wrote showcase scene {showcase.relative_to(project)}")

    # tidy up the temporary engine tooling
    tools = project / "_migration_tools"
    if tools.exists():
        shutil.rmtree(tools, ignore_errors=True)

    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())






