---
name: unity-to-godot-assets
description: Port a Unity asset pack (FBX meshes, .mat materials, .prefab prefabs, PNG textures, .meta GUID files) into a Godot 4 project — converts URP/HDRP materials to StandardMaterial3D, fixes texture import settings, builds one .tscn prefab per .prefab, and generates a showcase scene that instantiates all of them. Use when migrating/porting Unity assets to Godot, when the user mentions Unity .prefab/.mat/.meta/.fbx files, asks to "перенести ассеты из Unity в Godot", or when Unity-imported models look wrong in Godot (white/blown-out emission, missing normal maps, mirrored or sideways meshes, wrong metal/roughness, "resource file not found fbx::").
---

# Unity → Godot 4 asset migration

Turns a Unity asset folder into a usable Godot asset pack:

```
<godot>/<Dest>/
  meshes/<name>.fbx              source meshes (copied as-is)
  meshes/extracted/<name>.res    ArrayMesh extracted from the imported FBX (used by prefabs)
  textures/<name>.png            source textures (copied as-is)
  materials/M_*.tres             StandardMaterial3D per Unity .mat
  prefabs/SM_*.tscn              one scene per Unity .prefab (Node3D + MeshInstance3D + material)
  <Dest>Showcase.tscn            floor + sun + camera + every prefab on a labelled grid
```

Everything is driven by `scripts/unity_to_godot.py` (Python 3, stdlib only, Unity not required —
it parses the `.mat` / `.prefab` YAML and the `.meta` GUIDs directly, and uses the Godot CLI only
for importing).

## Quick start

```powershell
python scripts/unity_to_godot.py `
  --unity-assets  "M:/Projects/TESTUnity/Assets/KitchenProps" `
  --godot-project "M:/Projects/test-godot" `
  --dest          "KitchenProps" `
  --godot-exe     "M:/Programs/Godot/Godot_v4.7.2-stable_win64_console.exe"
```

The script is **idempotent** — re-run it after changing the source assets. Useful flags:
`--skip-showcase`, `--columns 10 --spacing 3.0`, `--skip-unused-textures` (drops e.g. the
`*_ORM_MAS` duplicates and any texture no material references), `--godot-exe godot`.

Then verify (see *Verification*) and open `<Dest>Showcase.tscn`.

## Pipeline

1. **Index GUIDs.** Every Unity asset has `<file>.meta` with `guid: <32 hex>`. Materials and
   prefabs reference assets only by GUID, so build `guid → path` first (`build_guid_map`).
2. **Convert materials first** (`.mat` → `StandardMaterial3D` `.tres`) — this also tells you
   which textures are actually needed. Rules in the table below.
3. **Copy sources** (`*.fbx`, `*.png`, …) into the Godot project, then run
   `godot --headless --path <project> --import`.
4. **Patch texture import settings** (`.import` files) and re-import:
   `compress/mode=2` (VRAM compressed), `mipmaps/generate=true`,
   `compress/normal_map=1` for `*_N` maps, `detect_3d/compress_to=0` so the editor does not
   silently change them later.
5. **Extract meshes.** Load each imported FBX scene, take the `ArrayMesh` off its
   `MeshInstance3D`, clear the importer's placeholder material, `ResourceSaver.save()` it as
   `meshes/extracted/<name>.res`. Prefabs then reference a stable `res://...res` path.
6. **Build prefabs.** One `.tscn` per `.prefab`: `Node3D` root named after the GameObject, one
   `MeshInstance3D` child holding the extracted mesh + `surface_material_override/0`.
7. **Build the showcase scene**: plane floor, `WorldEnvironment` (procedural sky), sun, camera
   framed by binary search over the grid, one instance per prefab plus a floor `Label3D`.

## Conversion rules

### Materials — Unity URP Lit → `StandardMaterial3D`

| Unity property | Godot property | Notes |
|---|---|---|
| `_BaseMap` / `_MainTex` | `albedo_texture` | |
| `_BaseColor` / `_Color` | `albedo_color` | Unity serialises colours **linear**; copy the numbers, do not gamma-correct |
| `_BaseMap` `m_Scale`/`m_Offset` | `uv1_scale` / `uv1_offset` | only when ≠ (1,1) / (0,0) |
| `_BumpMap` | `normal_texture`, `normal_enabled=true` | `normal_scale = _BumpScale` |
| `_MetallicGlossMap` + `_OcclusionMap` → `*_ORM.png` | `metallic_texture` ch **2 (B)**, `roughness_texture` ch **1 (G)**, `ao_texture` ch **0 (R)**, `ao_enabled=true`, `metallic=1`, `roughness=1` | Godot's ORM layout is exactly R=AO, G=roughness, B=metallic |
| `*_ORM_MAS.png` (Unity mask: R=metal, G=AO, A=smooth) | prefer the sibling `*_ORM.png`; otherwise `metallic_texture` ch 0 + `ao_texture` ch 1, `roughness = 1 - _Smoothness` | Unity materials usually point at the **MAS** variant, Godot wants the ORM one |
| `_EmissionMap` | `emission_texture` + `emission_enabled=true` + **`emission_operator = 1`** | see pitfall #1 |
| `_EmissionColor` | `emission` colour, magnitude → `emission_energy_multiplier` | Unity multiplies colour × map |
| `_Cull` (0 off / 1 front / 2 back) | `cull_mode` (2 / 1 / 0) | |
| `_Surface=1` | `transparency=1` + `blend_mode` (0→1, 1→2, 2→3, 3→4) | plus `cull_mode=2` |
| `_AlphaClip=1`, `_Cutoff` | `transparency=1`, `alpha_scissor_threshold=_Cutoff` | |

Only `_Metallic` / `_Smoothness` scalars → `metallic` / `roughness = 1 - smoothness` when there
is no metal/roughness map at all.

### Prefabs

* Root: `Node3D` named like the Unity GameObject; child: `MeshInstance3D` named `Mesh`.
* `mesh` = the extracted `ArrayMesh`; `surface_material_override/0` = the converted material
  (first `m_Materials` GUID that resolves).
* `transform` = the prefab's local TRS (see the axis note), written as `Transform3D(...)`;
  written only when it is not identity.
* Prefabs keep Unity's component set: no colliders in Unity → no colliders in Godot. Add a
  `StaticBody3D` + `CollisionShape3D` (or *Mesh → Create Trimesh/Convex Static Body*) when the
  target project needs collision.

### Axis, scale, handedness

* Unity FBX files are Z-up. Godot's importer bakes the Z-up → Y-up correction into the
  `MeshInstance3D` node (typically `rotation = (-90, 0, 0)`), and Unity does the same through the
  axis-conversion rotation that ends up on the prefab root. **So copy the prefab's local rotation
  numerically** onto the raw mesh — don't drop it, don't add your own -90°.
* Unity quaternion `(x, y, z, w)` and Godot evaluate the quaternion with the same maths; a
  straight copy reproduces the same orientation. Only asymmetric models look mirrored in Z (an
  accepted Unity↔Godot artefact); fix by negating the mesh's X and flipping the winding if it
  ever matters.
* Scale: the FBX importer converts units to metres; a 1 m barrel comes out ~1 m tall. Verify with
  the AABB, not by eye.

## Pitfalls (all of these were hit for real)

1. **Emission blows objects pure white (Godot ≥ 4.5).** `emission_operator` defaults to
   `EMISSION_OP_ADD` (0), so the shader computes `emission_color + texture`. Unity multiplies.
   Always write `emission_operator = 1` when an emission map is present.
   `emission_intensity` also defaults to 1000 nits but is only applied when *Project Settings →
   Rendering → Lights and Shadows → Use Physical Light Units* is on; if the target project
   enables PLU, set it to a sensible value (≈1–16).
2. **`Transform3D` in `.tscn` text is stored transposed.** The nine numbers are the matrix rows,
   i.e. `(x.x, y.x, z.x, x.y, y.y, z.y, x.z, y.z, z.z)`, followed by the origin. Writing the axis
   vectors in column order gives the inverse rotation — objects stand on their heads and cameras
   point at the sky. Use `tf_string()` from the script instead of hand-writing it.
3. **`res://model.fbx::ArrayMesh_xxx` cannot be loaded** from a `.tscn` `ext_resource` or from
   `load()` ("Resource file not found"). Sub-resource paths only work for a few resource types.
   Extract the mesh into its own `.res` and reference that (step 5).
4. **Normal maps need `compress/normal_map=1`.** Default is 0, so the map is imported as an sRGB
   colour texture and the lighting looks flat/plastic. Symptom: correct albedo, wrong shading.
   Match the file name before the extension (`Path("T_Barrel_N.png").stem`), not the whole
   `.import` file name.
5. **CLI imports are not 3D-ready.** `--headless --import` leaves `compress/mode=0` (lossless,
   huge) and `mipmaps/generate=false`. Set mode 2 + mipmaps, and set
   `detect_3d/compress_to=0`, otherwise the editor's "detect 3D" pass may rewrite them on open.
6. **`*_ORM_MAS` vs `*_ORM`.** Unity materials reference the MAS mask (R=metal, G=AO,
   A=smoothness); Godot's ORM layout is R=AO, G=rough, B=metal. Feeding the MAS map straight in
   makes wood look like chrome. Use the ORM sibling when the pack ships both.
7. **Stale `.godot/imported` files.** Switching compression modes leaves old `.ctex` files
   behind (hundreds of MB). Delete `.godot/imported` and re-import for a clean size.
8. **`--skip-import` still needs mesh extraction.** Prefabs written without the extraction step
   come out empty (only labels show up in the showcase). The script now always extracts.
9. **Case-mismatch warnings from the FBX importer.** Godot resolves the texture paths stored in
   the FBX (`Props/Textures/T_X.png`) against a lower-cased folder name, so a `Textures/` folder
   produces ~150 `Case mismatch opening requested file 'res://.../textures/...'` warnings on every
   project open, and the lookup breaks on case-sensitive platforms. Name the destination folder
   `textures` (lower case) — warnings drop to zero. Keep `meshes`/`materials`/`prefabs` lower case
   too for consistency.
10. **`emission_energy_multiplier` is clamped to 0–16** in the inspector; higher values are legal
    in the file but the UI clamps them.

## Verification

```powershell
# every prefab: has a mesh, a material, sits on y≈0, prints its size
godot --headless --path . --script res://verify_prefabs.gd -- res://KitchenProps/prefabs

# PNG preview of the showcase scene (needs a real GPU/display, do NOT pass --headless)
godot --path . --script res://render_preview.gd -- res://KitchenProps/KitchenPropsShowcase.tscn res://preview.png 1600 900
```

Both helpers live in `scripts/`; copy them into the project root (Godot resolves `res://` only
inside the project) and delete them afterwards. `verify_prefabs.gd` exits non-zero when a prefab
has no mesh or no material.

Expected: `TOTAL=<n> BAD=0`, sizes in the 0.1–2 m range, `y` starting at ≈0, no white blobs in the
preview, textures visibly detailed when you zoom in.

Also worth doing once: `godot --headless --path . --editor --quit` and confirm the log has zero
`ERROR` / `WARNING` lines before handing the project over.

## Adapting to another pack

* Material templates differ per pipeline — check `m_Shader` (one GUID per shader) and read the
  real property names from a sample `.mat`; add aliases to the `_BaseMap` / `_BumpMap` /
  `_MetallicGlossMap` lookups (HDRP: `_BaseColorMap`, `_NormalMap`, `_MaskMap`; Built-in:
  `_MainTex`, `_BumpMap`, `_MetallicGlossMap`, `_SpecGlossMap`).
* Transparent / foliage packs: also handle `_AlphaClip`, `_Surface`, `_Blend` and
  `alpha_antialiasing_mode` for alpha-to-coverage.
* Multi-material meshes: map `m_Materials[i]` to `surface_material_override/i` (the script
  currently fills surface 0 only).
* Nested prefabs (`PrefabInstance` documents, class id 1001) are not handled — such prefabs need
  a manual pass or an extension that resolves `m_Modifications`.
* No Godot CLI available? Do steps 4–6 by hand in the editor, or point `--godot-exe` at any
  Godot 4.3+ binary (FBX import requires 4.3+).

## Files

```
SKILL.md                        this document
scripts/unity_to_godot.py       the converter (main entry point)
scripts/verify_prefabs.gd       headless prefab checker (copy into the project to run)
scripts/render_preview.gd       screenshot helper (copy into the project to run)
```
