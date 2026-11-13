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
`--skip-showcase`, `--margin 0.9 --row-width 0` (showcase cell spacing / max row width),
`--skip-unused-textures` (drops e.g. the `*_ORM_MAS` duplicates and any texture no material
references), `--godot-exe godot`. Run it once per pack — `--dest "External/Crypt"` nests the
output and names the showcase after the last path segment.

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
   `compress/normal_map=1` for `*_N` maps, `process/normal_map_invert_y` taken from the Unity
   `bumpmap.flipGreenChannel` flag, and `detect_3d/compress_to=0` so the editor does not silently
   change them later.
5. **Extract meshes.** Load each imported FBX scene, take the `ArrayMesh` off its
   `MeshInstance3D`, clear the importer's placeholder material, `ResourceSaver.save()` it as
   `meshes/extracted/<name>.res`. Prefabs then reference a stable `res://...res` path.
6. **Build prefabs.** One `.tscn` per `.prefab`: `Node3D` root named after the GameObject, one
   `MeshInstance3D` child holding the extracted mesh and one `surface_material_override/i` per
   submesh (see *Prefabs* below).
7. **Build the showcase scene**: plane floor, `WorldEnvironment` (procedural sky), sun, camera
   framed by binary search, one instance per prefab plus a floor `Label3D`. The layout is a
   size-aware shelf pack (each cell is as big as the object's world AABB), so architectural
   modules do not overlap the way they would on a fixed grid.

## Conversion rules

### Materials — Unity URP Lit → `StandardMaterial3D`

| Unity property | Godot property | Notes |
|---|---|---|
| `_BaseMap` / `_MainTex` | `albedo_texture` | |
| `_BaseColor` / `_Color` | `albedo_color` | Unity serialises colours **linear**; copy the numbers, do not gamma-correct |
| `_BaseMap` `m_Scale`/`m_Offset` | `uv1_scale` / `uv1_offset` | only when ≠ (1,1) / (0,0) |
| `_BumpMap` | `normal_texture`, `normal_enabled=true` | `normal_scale = _BumpScale`; the green channel usually needs a flip, see *Normal maps* below |
| `bumpmap.flipGreenChannel` (in the texture `.meta`) | `process/normal_map_invert_y=true` in the texture `.import` | `1` means Unity flipped green at import, i.e. the PNG is DirectX-style (Y-) |
| `_MetallicGlossMap` + `_OcclusionMap` | `metallic_texture` / `roughness_texture` / `ao_texture` + per-channel selects | which channel is which depends on the pack, see *Metal / roughness / AO packings* |
| `_EmissionMap` | `emission_texture` + `emission_enabled=true` + **`emission_operator = 1`** | see pitfall #1 |
| `_EmissionColor` | `emission` colour, magnitude → `emission_energy_multiplier` | Unity multiplies colour × map |
| `_Cull` (0 off / 1 front / 2 back) | `cull_mode` (2 / 1 / 0) | |
| `_Surface=1` | `transparency=1` + `blend_mode` (0→1, 1→2, 2→3, 3→4) | plus `cull_mode=2` |
| `_AlphaClip=1`, `_Cutoff` | `transparency=1`, `alpha_scissor_threshold=_Cutoff` | |

Only `_Metallic` / `_Smoothness` scalars → `metallic` / `roughness = 1 - smoothness` when there
is no metal/roughness map at all.

### Metal / roughness / AO packings

Godot reads all three from one texture with per-channel selectors, so the only thing that matters
is *which channel holds what*. Packs ship several conventions, and the Unity material always
points at the **Unity mask** variant (`R=metallic, G=AO, A=smoothness`), which is not what Godot
wants. Resolve it like this:

| Files in the pack | Layout | Godot wiring |
|---|---|---|
| `T_X_ORM.png`, `T_X_ARM.png`, `T_X_ORMH.png` | R=AO, G=rough, B=metal (Godot native) | one texture, channels metal **2**, rough **1**, AO **0** |
| `T_X_MRA.png` | R=metal, G=rough, B=AO | one texture, channels metal **0**, rough **1**, AO **2** |
| `T_X_R.png` (grey) + `T_X_R_MAS.png` | mask for metal/AO, separate roughness map | mask channels metal **0** / AO **1**, roughness from the grey map ch **0** |
| only `T_X_MAS.png` / `T_X_Mask.png` | R=metal, G=AO, A=smoothness | mask channels metal **0** / AO **1**, roughness ch **3** *after* inverting alpha at import (`process/channel_remap/alpha=7`) |

Rules of thumb:

* Unity materials reference `*_MAS`; strip that suffix to find the sibling Godot can use
  (`T_X_ORM_MAS` → `T_X_ORM`, `T_X_ARM_MAS` → `T_X_ARM`, `T_X_MAS` → `T_X_MRA`).
* Verify the layout by looking at channel means instead of trusting the name — e.g. stone with
  `R≈0` means R is metallic, while `R≈200` with `B=0` means R is AO.
* Never fall back to `roughness = 1 - _Smoothness` when a mask map is present: `_Smoothness` is a
  multiplier (usually `1`), so you get `roughness = 0` and the whole pack turns into chrome. This
  is the single most common way to wreck a Unity → Godot material port.

### Prefabs

* Root: `Node3D` named like the Unity GameObject; child: `MeshInstance3D` named `Mesh`.
* `mesh` = the extracted `ArrayMesh`; `surface_material_override/i` = the material of submesh `i`
  (`m_Materials[i]`), one entry per surface — asset packs are full of multi-material meshes
  (bookshelves, chests, modular walls), so never assign surface 0 only.
* **Unity merges submeshes that share a material, Godot's importer does not.** A mesh can
  therefore have more surfaces than the prefab has material slots (e.g. a roof with
  `WorldGrid / Roof / Roof / Floor` surfaces and 3 materials). Group the surfaces by their FBX
  placeholder material name — a run of equal names is one Unity submesh — and hand each group one
  material. `map_materials_to_surfaces()` in the script does exactly that; the extraction step
  stores the per-surface FBX material names for it.
* `transform` = the prefab's local TRS (see the axis note), written as `Transform3D(...)`;
  written only when it is not identity.
* Prefabs keep Unity's component set: no colliders in Unity → no colliders in Godot. Add a
  `StaticBody3D` + `CollisionShape3D` (or *Mesh → Create Trimesh/Convex Static Body*) when the
  target project needs collision.

### Normal maps and handedness

Normal maps encode the surface tilt in the green channel, and engines disagree about which way
"up" is. From the Polycount table (linked from Godot's own importer docs):

| Engine | Red | Green | Blue |
|---|---|---|---|
| Unity | X+ | **Y+** (OpenGL) | Z+ |
| Godot | X+ | **Y+** (OpenGL) | Z+ |
| Unreal | X+ | **Y−** (DirectX) | Z+ |
| Blender / Maya / Modo | X+ | Y+ | Z+ |
| 3ds Max / Source / CryEngine | X+ | Y− | Z+ |

Godot's docs for `process/normal_map_invert_y` say it plainly: *"convert the normal map from
Y- (DirectX-style) to Y+ (OpenGL-style) by inverting its green color channel. This is the normal
map convention expected by Godot."*

So Unity and Godot agree — **unless the PNG on disk was authored for DirectX** (very common: packs
sold for Unreal, Substance presets, 3ds Max bakes). In that case Unity is configured to flip the
green channel at import time, and Godot must do the same:

* read `bumpmap.flipGreenChannel` from `<texture>.png.meta`;
* if it is `1`, the PNG is Y− and Godot needs `process/normal_map_invert_y=true` in its `.import`;
* if it is `0` (or absent), leave the default `false`.

`process/normal_map_invert_y` is deprecated in 4.7 in favour of `process/channel_remap/green=5`
("Inverted Green") but still implemented — and it is the only one of the two that exists all the
way back to 4.0, so prefer it. **Never set both**: the importer applies the channel remap first and
then inverts again, which cancels the flip out.

Symptom of getting it wrong: raised details (stone blocks, planks, rivets) look *pressed in* —
lit from above, their bottom edge is bright and their top edge is dark. It is subtle on flat
stylised props and obvious on masonry.

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
4. **Normal maps need `compress/normal_map=1` and the right green channel.** Default
   `compress/normal_map` is 0, so the map is imported as an sRGB colour texture and the lighting
   looks flat/plastic (symptom: correct albedo, wrong shading). And if the source PNG is
   DirectX-style (see *Normal maps and handedness*), `process/normal_map_invert_y=true` is required,
   otherwise every raised detail reads as a dent. Match the file name before the extension
   (`Path("T_Barrel_N.png").stem`), not the whole `.import` file name.
5. **CLI imports are not 3D-ready.** `--headless --import` leaves `compress/mode=0` (lossless,
   huge) and `mipmaps/generate=false`. Set mode 2 + mipmaps, and set
   `detect_3d/compress_to=0`, otherwise the editor's "detect 3D" pass may rewrite them on open.
6. **`*_ORM_MAS` vs `*_ORM`.** Unity materials reference the Unity mask (R=metal, G=AO,
   A=smoothness); Godot's native ORM layout is R=AO, G=rough, B=metal. Feeding the mask in
   directly, or falling back to `roughness = 1 - _Smoothness`, makes wood look like chrome
   (`roughness = 0`). See *Metal / roughness / AO packings* — packs also ship `ARM`, `MRA` and
   `Mask` variants, so classify the sibling texture per pack instead of hard-coding one layout.
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
11. **Surface indices drift when Unity merged submeshes.** A prefab with 3 materials can sit on a
    mesh with 4 surfaces (Unity merged the two that share a material), so a naive
    `surface_material_override[i] = materials[i]` puts the floor material on a roof and leaves the
    last surface untextured. Only a handful of prefabs per pack are affected — which is exactly why
    the verification step must count materials **per surface**, not just check surface 0.
12. **`verify_prefabs.gd` "not ground-aligned" notes are not errors.** Wall decorations, roof
    pieces and hanging props legitimately have their origin elsewhere; only `!!` lines (missing
    mesh / missing material / absurd size) are failures.

## Verification

```powershell
# every prefab: mesh present, EVERY surface has a material, sane size
godot --headless --path . --script res://verify_prefabs.gd -- res://KitchenProps/prefabs

# which normal-map handedness do the PNGs use? (corroborates bumpmap.flipGreenChannel)
godot --headless --path . --script res://check_normal_convention.gd -- res://KitchenProps/textures

# PNG preview of the showcase scene (needs a real GPU/display, do NOT pass --headless)
godot --path . --script res://render_preview.gd -- res://KitchenProps/KitchenPropsShowcase.tscn res://preview.png 1600 900
```

All three helpers live in `scripts/`; copy them into the project root (Godot resolves `res://`
only inside the project) and delete them afterwards. `verify_prefabs.gd` exits non-zero when a
prefab has no mesh or a surface without a material; pass `-v` as a second argument to list every
prefab instead of only the problems. Its summary line is the fastest sanity check of a migration:

```
TOTAL=1963 BAD=0 SURFACES=2938 MISSING_MATERIALS=0
```

`check_normal_convention.gd` correlates the divergence of the height field implied by the normal
map with `1 - AO` (concave areas are occluded, so the two must agree). It prints `corr_GL` and
`corr_DX` per texture and a vote at the end; the AO of stylised packs is hand-painted, so treat it
as a cross-check of the `.meta` value, not as the primary source of truth.

Expected: `BAD=0` and `MISSING_MATERIALS=0`, plausible sizes, no white blobs in the preview,
textures visibly detailed when you zoom in, and raised details (stone blocks, planks, rivets)
reading as **raised** — bright on the side facing the light, dark on the opposite side.

Also worth doing once: `godot --headless --path . --editor --quit` and confirm the log has zero
`ERROR` / `WARNING` lines before handing the project over.

## Adapting to another pack

* Material templates differ per pipeline — check `m_Shader` (one GUID per shader) and read the
  real property names from a sample `.mat`; add aliases to the `_BaseMap` / `_BumpMap` /
  `_MetallicGlossMap` lookups (HDRP: `_BaseColorMap`, `_NormalMap`, `_MaskMap`; Built-in:
  `_MainTex`, `_BumpMap`, `_MetallicGlossMap`, `_SpecGlossMap`).
* Transparent / foliage packs: also handle `_AlphaClip`, `_Surface`, `_Blend` and
  `alpha_antialiasing_mode` for alpha-to-coverage. Alpha-tested foliage imports correctly with
  just `transparency = 1` + `alpha_scissor_threshold`, but check a leaf card in the preview.
* Packs whose meshes are split into many submeshes (merged prop sets) rely on the per-surface
  mapping — always run the verification, it reports surfaces without materials.
* Nested prefabs (`PrefabInstance` documents, class id 1001) are not handled — such prefabs need
  a manual pass or an extension that resolves `m_Modifications`.
* No Godot CLI available? Do steps 4–6 by hand in the editor, or point `--godot-exe` at any
  Godot 4.3+ binary (FBX import requires 4.3+).

## Files

```
SKILL.md                        this document
scripts/unity_to_godot.py       the converter (main entry point)
scripts/verify_prefabs.gd       headless prefab checker (copy into the project to run)
scripts/check_normal_convention.gd  normal-map handedness cross-check (copy into the project)
scripts/render_preview.gd       screenshot helper (copy into the project to run)
```
