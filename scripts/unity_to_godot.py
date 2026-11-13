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

# Known metal/roughness/AO packings. The channel triple is (metallic, roughness, AO).
PACKINGS = {
    # R=AO, G=roughness, B=metallic -- Godot's native ORM layout (ORM / ARM / ORMH ...)
    "native": (2, 1, 0),
    # R=metallic, G=roughness, B=AO
    "mra": (0, 1, 2),
}

# suffixes that use the native R=AO / G=rough / B=metal layout
NATIVE_SUFFIXES = ("_ORM", "_ARM", "_ORMH", "_AOR", "_RMA", "_RMO")
MRA_SUFFIXES = ("_MRA", "_MRAO")
ROUGHNESS_SUFFIXES = ("_R", "_ROUGH", "_ROUGHNESS")
MASK_SUFFIXES = ("_MAS", "_MASK", "_MASKRBG", "_MASKRGB", "_MSK")


def _classify_surface_texture(path: Path) -> str:
    """Guess the channel layout of a metal/roughness/AO texture from its name."""
    s = path.stem.upper()
    if s.endswith(MASK_SUFFIXES):
        return "mask"
    if s.endswith(MRA_SUFFIXES):
        return "mra"
    if s.endswith(NATIVE_SUFFIXES):
        return "native"
    if s.endswith(ROUGHNESS_SUFFIXES):
        return "roughness"
    return "native"


def resolve_surface_texture(metallic_tex: Path | None, ao_tex: Path | None) -> dict:
    """Work out which texture drives metallic/roughness/AO and how its channels are packed.

    Asset packs ship a different convention per pack, and Unity materials usually reference
    the *Unity* variant of it:

      `T_X_ORM_MAS.png`  Unity mask (R=metal, G=AO, A=smoothness) -- sibling `T_X_ORM.png`
                         exists and is already in Godot's layout (R=AO, G=rough, B=metal)
      `T_X_ARM_MAS.png`  same story, `T_X_ARM.png` is R=AO, G=rough, B=metal
      `T_X_MAS.png`      Unity mask only, `T_X_MRA.png` is R=metal, G=rough, B=AO
      `T_X_R_MAS.png`    Unity mask plus a separate roughness map `T_X_R.png`

    Returns {"kind": ..., ...} with kind in {"native", "mra", "mask", "mask_rough", None}.
    """
    for cand in (metallic_tex, ao_tex):
        if cand is None:
            continue
        stem = cand.stem
        base = stem[:-4] if stem.upper().endswith("_MAS") else stem
        # 1) sibling without the Unity suffix, e.g. T_X_ORM_MAS -> T_X_ORM
        for extra in ("", "_ORM", "_ARM", "_ORMH", "_MRA", "_R", "_O"):
            name = base + extra + cand.suffix
            if name == cand.name:
                continue
            sib = cand.with_name(name)
            if sib.exists():
                kind = _classify_surface_texture(sib)
                if kind == "roughness":
                    # keep the mask for metallic/AO, take roughness from the grey map
                    return {"kind": "mask_rough", "mask": cand, "rough": sib}
                if kind in PACKINGS:
                    return {"kind": kind, "tex": sib}
        # 2) no sibling: use the file itself
        kind = _classify_surface_texture(cand)
        if kind in PACKINGS:
            return {"kind": kind, "tex": cand}
        return {"kind": "mask", "mask": cand}
    return {"kind": None}


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
        self.alpha_inverted: set[str] = set()   # textures whose alpha must be inverted at import
        self.packing_counts: dict[str, int] = {}

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
        # Unity materials point at a mask map (R=metallic, G=AO, A=smoothness); the
        # sibling textures carry the same data in a layout Godot can sample directly.
        metallic = floats.get("_Metallic", 0.0)
        smoothness = floats.get("_Smoothness", floats.get("_Glossiness", 0.5))
        plan = resolve_surface_texture(
            self.tex_by_guid((texenvs.get("_MetallicGlossMap") or {}).get("guid")),
            self.tex_by_guid((texenvs.get("_OcclusionMap") or {}).get("guid")),
        )
        kind = plan.get("kind")
        key = kind or "scalar"
        self.packing_counts[key] = self.packing_counts.get(key, 0) + 1

        def surface_channels(m_ch: int, r_ch: int, a_ch: int) -> None:
            rid = add_tex("orm", plan["tex"])
            props.append(f'metallic_texture = ExtResource("{rid}")')
            props.append(f"metallic_texture_channel = {m_ch}")
            props.append(f'roughness_texture = ExtResource("{rid}")')
            props.append(f"roughness_texture_channel = {r_ch}")
            props.append(f'ao_texture = ExtResource("{rid}")')
            props.append(f"ao_texture_channel = {a_ch}")
            props.append("ao_enabled = true")
            props.append("metallic = 1.0")
            props.append("roughness = 1.0")
            props.append("metallic_specular = 0.5")

        if kind in PACKINGS:
            surface_channels(*PACKINGS[kind])
        elif kind == "mask_rough":
            # Unity mask for metallic/AO + a separate grey roughness map
            rid = add_tex("mask", plan["mask"])
            props.append(f'metallic_texture = ExtResource("{rid}")')
            props.append("metallic_texture_channel = 0")   # R
            props.append(f'ao_texture = ExtResource("{rid}")')
            props.append("ao_texture_channel = 1")         # G
            props.append("ao_enabled = true")
            rrid = add_tex("rough", plan["rough"])
            props.append(f'roughness_texture = ExtResource("{rrid}")')
            props.append("roughness_texture_channel = 0")
            props.append("metallic = 1.0")
            props.append("roughness = 1.0")
            props.append("metallic_specular = 0.5")
        elif kind == "mask":
            # Unity URP mask map: R = metallic, G = AO, A = smoothness.
            # Godot samples roughness directly, so the alpha channel gets inverted at
            # import time (process/channel_remap/alpha = 7) and read back as roughness.
            rid = add_tex("mask", plan["mask"])
            props.append(f'metallic_texture = ExtResource("{rid}")')
            props.append("metallic_texture_channel = 0")   # R
            props.append(f'ao_texture = ExtResource("{rid}")')
            props.append("ao_texture_channel = 1")         # G
            props.append("ao_enabled = true")
            props.append(f'roughness_texture = ExtResource("{rid}")')
            props.append("roughness_texture_channel = 3")  # A (inverted smoothness)
            props.append("metallic = 1.0")
            props.append("roughness = 1.0")
            props.append("metallic_specular = 0.5")
            self.alpha_inverted.add(plan["mask"].name)
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


def quat_matrix(q: tuple[float, float, float, float]) -> list[list[float]]:
    """Rotation matrix (row-major, columns = rotated axes) for a Unity/Godot quaternion."""
    x, y, z, w = q
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]


def prefab_footprint(
    rotation: tuple[float, float, float, float],
    position: tuple[float, float, float],
    scale: tuple[float, float, float],
    aabb: list[float] | None,
) -> tuple[float, float, float]:
    """World-space (width, depth, height) of a prefab, from its mesh AABB."""
    if not aabb or len(aabb) != 6:
        return (1.0, 1.0, 1.0)
    mn = aabb[:3]
    size = aabb[3:]
    m = quat_matrix(rotation)
    sx, sy, sz = scale
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    for i in range(8):
        lx = mn[0] + (size[0] if i & 1 else 0.0)
        ly = mn[1] + (size[1] if i & 2 else 0.0)
        lz = mn[2] + (size[2] if i & 4 else 0.0)
        for axis in range(3):
            v = (
                m[axis][0] * lx * sx
                + m[axis][1] * ly * sy
                + m[axis][2] * lz * sz
                + position[axis]
            )
            lo[axis] = min(lo[axis], v)
            hi[axis] = max(hi[axis], v)
    return (hi[0] - lo[0], hi[2] - lo[2], hi[1] - lo[1])


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
    m = quat_matrix(q)
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
		# drop the placeholder materials created by the FBX importer, but remember their
		# names first: Unity merges submeshes that share a material, Godot keeps them apart
		var surfaces := mesh.get_surface_count()
		var surface_materials := []
		for i in surfaces:
			var m = mesh.surface_get_material(i)
			surface_materials.append(m.resource_name if m != null else "")
			mesh.surface_set_material(i, null)
		var out_path := out_dir.path_join(f.get_basename() + ".res")
		var err := ResourceSaver.save(mesh, out_path)
		var aabb := mesh.get_aabb()
		if err == OK:
			result[f] = {
				"path": out_path,
				"surfaces": surfaces,
				"surface_materials": surface_materials,
				"aabb": [aabb.position.x, aabb.position.y, aabb.position.z, aabb.size.x, aabb.size.y, aabb.size.z],
			}
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

# Unity's TextureImporterType.NormalMap
UNITY_TEXTURE_TYPE_NORMAL_MAP = 1


def read_unity_texture_settings(meta: Path) -> dict:
    """Read the few TextureImporter settings that matter for a Godot re-import.

    Unity normal maps are the interesting part: `bumpmap.flipGreenChannel` records that
    Unity flipped the green channel *at import time*, which means the PNG on disk uses the
    DirectX convention (Y-) while both Unity and Godot shade with the OpenGL convention
    (Y+). Godot must therefore perform the same flip.
    """
    text = meta.read_text(encoding="utf-8", errors="replace")

    def num(pattern: str, default: int) -> int:
        m = re.search(pattern, text, re.M)
        return int(m.group(1)) if m else default

    return {
        "type": num(r"^  textureType: (\d+)", 0),
        "flip_green": bool(num(r"^    flipGreenChannel: (\d+)", 0)),
        "srgb": bool(num(r"^    sRGBTexture: (\d+)", 1)),
        "mipmaps": bool(num(r"^    enableMipMap: (\d+)", 1)),
    }


def index_unity_texture_settings(unity_dir: Path) -> dict[str, dict]:
    """Map image file name -> TextureImporter settings for every texture in the pack."""
    out: dict[str, dict] = {}
    for meta in unity_dir.rglob("*.png.meta"):
        out[meta.name[: -len(".meta")]] = read_unity_texture_settings(meta)
    for meta in unity_dir.rglob("*.tga.meta"):
        out[meta.name[: -len(".meta")]] = read_unity_texture_settings(meta)
    return out


def patch_texture_imports(
    textures_dir: Path,
    unity_settings: dict[str, dict] | None = None,
    alpha_inverted: set[str] | None = None,
) -> int:
    """Set 3D-friendly import flags, mark normal maps and fix their green channel."""
    unity_settings = unity_settings or {}
    alpha_inverted = alpha_inverted or set()
    changed = 0
    for imp in textures_dir.glob("*.import"):
        text = imp.read_text(encoding="utf-8")
        params = dict(TEXTURE_IMPORT_PARAMS)
        image_name = imp.name[: -len(".import")]          # "T_Barrel_N.png"
        base_stem = Path(image_name).stem                 # "T_Barrel_N"
        settings = unity_settings.get(image_name, {})
        is_normal = settings.get("type") == UNITY_TEXTURE_TYPE_NORMAL_MAP or bool(
            re.search(r"(_n|_nrm|_normal|_nor)$", base_stem, re.I)
        )
        params["compress/normal_map"] = "1" if is_normal else "0"
        # DirectX-style (Y-) source map -> invert green so it becomes OpenGL-style (Y+).
        invert_y = is_normal and settings.get("flip_green", False)
        params["process/normal_map_invert_y"] = "true" if invert_y else "false"
        # Unity mask maps store *smoothness* in the alpha channel; Godot wants roughness.
        params["process/channel_remap/alpha"] = "7" if image_name in alpha_inverted else "3"
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


def map_materials_to_surfaces(
    mat_paths: list[str | None], surface_materials: list[str]
) -> tuple[dict[int, str], bool]:
    """Distribute Unity's per-submesh material list over the Godot mesh surfaces.

    Unity merges submeshes that share a material at import time, Godot's FBX importer does
    not. When the mesh therefore has more surfaces than the prefab has material slots, the
    surfaces are grouped by their FBX placeholder material name (a run of equal names is one
    Unity submesh) and each group gets one material.

    Returns ({surface index: material res path}, merged_flag).
    """
    resolved = [(i, p) for i, p in enumerate(mat_paths) if p]
    n_surf = len(surface_materials)
    if not surface_materials or len(mat_paths) > n_surf:
        return dict(resolved), False

    groups: list[list[int]] = []
    for i, name in enumerate(surface_materials):
        if groups and surface_materials[groups[-1][0]] == name:
            groups[-1].append(i)
        else:
            groups.append([i])
    # Unity merged the submeshes that share a material: the runs line up with the material
    # list while the raw surface count is larger.
    if len(groups) == len(mat_paths) and len(groups) < n_surf:
        return {i: mat_paths[gi] for gi, idxs in enumerate(groups) for i in idxs if mat_paths[gi]}, True  # type: ignore[misc]
    return dict(resolved), False


def write_showcase(
    out_path: Path,
    prefabs: list[tuple[str, str, float, float, float]],
    *,
    margin: float = 0.9,
    max_row_width: float = 0.0,
) -> None:
    """Shelf-pack every prefab into size-aware cells, with a floor, sun, sky and camera.

    `prefabs` is a list of (name, res path, width, depth, height). Cells are as large as the
    object needs, so architectural pieces (walls, towers) do not overlap their neighbours the
    way they would on a fixed grid.
    """
    cells = [
        {
            "name": name,
            "res": res,
            "w": max(w, 0.6) + margin,
            "d": max(d, 0.6) + margin,
            "h": max(h, 0.2),
        }
        for name, res, w, d, h in prefabs
    ]
    if not cells:
        return

    total_area = sum(c["w"] * c["d"] for c in cells)
    row_target = max_row_width or max(10.0, math.sqrt(total_area) * 1.9)

    rows: list[list[dict]] = []
    cur: list[dict] = []
    cur_w = 0.0
    for c in cells:
        if cur and cur_w + c["w"] > row_target:
            rows.append(cur)
            cur, cur_w = [], 0.0
        cur.append(c)
        cur_w += c["w"]
    if cur:
        rows.append(cur)

    placed: list[tuple[dict, float, float, float]] = []   # cell, x, z, row depth
    z = 0.0
    for row in rows:
        row_d = max(c["d"] for c in row)
        row_w = sum(c["w"] for c in row)
        x = -row_w / 2.0
        for c in row:
            placed.append((c, x + c["w"] / 2.0, z + row_d / 2.0, row_d))
            x += c["w"]
        z += row_d
    depth = z
    z_shift = -depth / 2.0

    xs = [p[1] for p in placed]
    width = (max(xs) - min(xs)) if placed else 10.0
    max_height = max(c["h"] for c in cells)

    ext_lines = [
        f'[ext_resource type="PackedScene" path="res://{p[0]["res"]}" id="p{i}"]'
        for i, p in enumerate(placed)
    ]

    floor_w = max(24.0, width + margin * 8)
    floor_d = max(24.0, depth + margin * 8)

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

    # camera: frame the whole layout (binary-searched distance + centred target)
    cam_pos, cam_target, cam_x, cam_y, cam_z = fit_camera(
        width + margin * 2, depth + margin * 2, max_height=max_height, margin=0.9
    )
    cam_tf = tf_string(cam_x, cam_y, cam_z, cam_pos)

    body = []
    body.append(f'[node name="{Path(out_path).stem}" type="Node3D"]')
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
    body.append(f"directional_shadow_max_distance = {fmt(max(120.0, depth * 3.0))}")
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

    for i, (cell, x, z, row_d) in enumerate(placed):
        name = cell["name"]
        zz = z + z_shift
        body.append(f'[node name="{name}" parent="Prefabs" instance=ExtResource("p{i}")]')
        body.append(f"transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, {fmt(x)}, 0, {fmt(zz)})")
        body.append("")
        body.append(f'[node name="Label_{name}" type="Label3D" parent="Labels"]')
        label_z = zz + row_d / 2.0 - 0.25
        label_tf = tf_string((1, 0, 0), (0, 0, -1), (0, 1, 0), (x, 0.02, label_z))
        body.append(f"transform = {label_tf}")
        body.append(f'text = "{name}"')
        body.append("font_size = 48")
        # keep the caption roughly as wide as its cell
        pixel = min(0.004, max(0.0012, (cell["w"] - margin) / (max(len(name), 8) * 26.0)))
        body.append(f"pixel_size = {fmt(pixel)}")
        body.append("modulate = Color(0.92, 0.92, 0.88, 1)")
        body.append("outline_size = 10")
        body.append("outline_modulate = Color(0.05, 0.05, 0.05, 1)")
        body.append("no_depth_test = false")
        body.append("")

    load_steps = len(placed) + 6  # ext resources + 5 sub-resources + 1
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
    ap.add_argument("--margin", type=float, default=0.9, help="free space around each showcase cell (meters)")
    ap.add_argument("--row-width", type=float, default=0.0, help="max showcase row width (0 = derive from the pack)")
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
    if mb.packing_counts:
        print("  metal/roughness/AO packings: " + ", ".join(
            f"{k}={v}" for k, v in sorted(mb.packing_counts.items())))
    if mb.alpha_inverted:
        print(f"  {len(mb.alpha_inverted)} mask map(s) get their alpha inverted at import")

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
        changed = patch_texture_imports(
            textures_dir, index_unity_texture_settings(unity_dir), mb.alpha_inverted
        )
        print(f"patched {changed} texture import setting file(s); re-importing ...")
        run_godot(godot_exe, project, ["--import"])

    # ---- 4. prefabs ------------------------------------------------------------
    extracted_rel = f"{dest}/meshes/extracted"
    # always extract: the FBX files are already imported at this point even with --skip-import
    mesh_ids = extract_meshes(godot_exe, project, f"{dest}/meshes", extracted_rel)
    if not mesh_ids:
        print("  ! could not extract meshes - prefab scenes will have no mesh")

    def mesh_res_for(guid: str | None) -> dict | None:
        asset = guid_map.get(guid) if guid else None
        if not asset:
            return None
        return mesh_ids.get(asset.name) or None

    made_prefabs: list[tuple[str, str]] = []
    multi_surface = 0
    merged_surfaces = 0
    unassigned_surfaces = 0
    for prefab_file in sorted(unity_dir.rglob("*.prefab")):
        info = parse_prefab(prefab_file)
        mesh_info = mesh_res_for(info["mesh_guid"]) or {}
        mesh_res = mesh_info.get("path") if isinstance(mesh_info, dict) else mesh_info
        surface_count = mesh_info.get("surfaces", 0) if isinstance(mesh_info, dict) else 0
        name = info["name"]

        # Unity: m_Materials[i] is the material of submesh i -> surface_material_override/i
        mat_paths: list[str | None] = []
        for g in info["materials"]:
            mat_paths.append(mat_guid_to_res.get(g) if g else None)
        while mat_paths and mat_paths[-1] is None:
            mat_paths.pop()          # drop unresolved tail so we don't emit dead overrides
        if len(mat_paths) > 1:
            multi_surface += 1

        surface_materials = mesh_info.get("surface_materials", []) if isinstance(mesh_info, dict) else []
        surface_map, merged = map_materials_to_surfaces(mat_paths, surface_materials)
        if merged:
            merged_surfaces += 1
        if surface_count and len(surface_map) < surface_count:
            unassigned_surfaces += 1

        ext: list[tuple[str, str, str]] = []
        if mesh_res:
            ext.append(("ArrayMesh", mesh_res, "1_mesh"))
        mat_ids: dict[str, str] = {}
        for i in sorted(surface_map):
            mp = surface_map[i]
            if mp not in mat_ids:
                rid = f"mat{len(mat_ids)}"
                mat_ids[mp] = rid
                ext.append(("Material", "res://" + mp, rid))

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
            for i in sorted(surface_map):
                lines.append(f'surface_material_override/{i} = ExtResource("{mat_ids[surface_map[i]]}")')
            lines.append("")
        (prefabs_dir / f"{name}.tscn").write_text("\n".join(lines), encoding="utf-8")
        fp = prefab_footprint(
            info["rotation"], info["position"], info["scale"], mesh_info.get("aabb") if isinstance(mesh_info, dict) else None
        )
        made_prefabs.append((name, f"{dest}/prefabs/{name}.tscn", fp[0], fp[1], fp[2]))

    print(f"wrote {len(made_prefabs)} prefab scene(s) ({multi_surface} with several material slots)")
    if merged_surfaces:
        print(f"  {merged_surfaces} prefab(s) needed Unity's merged-submesh mapping")
    if unassigned_surfaces:
        print(f"  {unassigned_surfaces} prefab(s) have a submesh without material (also empty in Unity)")

    # ---- 5. showcase -----------------------------------------------------------
    if not args.skip_showcase and made_prefabs:
        showcase_name = args.showcase or f"{Path(dest).name}Showcase.tscn"
        showcase = dest_root / showcase_name
        write_showcase(showcase, made_prefabs, margin=args.margin, max_row_width=args.row_width)
        print(f"wrote showcase scene {showcase.relative_to(project)}")

    # tidy up the temporary engine tooling
    tools = project / "_migration_tools"
    if tools.exists():
        shutil.rmtree(tools, ignore_errors=True)

    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())






