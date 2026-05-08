# Changelog — RF VFX Tools

All notable changes to RF VFX Tools are documented here.

---

## v0.7.8 — 2026-05-07

### Fixed
- Vehicle and animation exports could be silently broken depending on
  Blender's session state. Blender's operator system uses the last-used
  UI value as the runtime default for `bpy.ops.export_scene.gltf`, so if
  the "+Y Up" checkbox had ever been unticked in an export dialog this
  session, scripted exports inherited that — vfx2obj got Z-up data and
  produced files with axes scrambled in-game. `export_yup=True` is now
  set explicitly at every export call site.

---

## v0.7.7 — 2026-05-07

### Fixed
- Imported VFX meshes appeared face-down on the floor in Blender instead
  of standing upright. Root cause: Blender 4.x's glTF importer **always**
  applies a Y-up → Z-up rotation, and the `import_yup` parameter older
  builds passed to suppress that rotation no longer exists in current
  Blender. The result was the up-axis swap happening twice — once in
  vfx2obj, once in Blender's importer — leaving meshes lying on their
  backs. The conversion pipeline now writes standard glTF Y-up data
  (Redux convention: just negate X) and lets Blender's importer apply
  its own up-axis rotation. The combined result places meshes in the
  same Blender Z-up frame the V3M and Character tools use, so all three
  add-ons now agree on orientation.

### Changed
- Quaternion conversion simplified to `(-qx, qy, qz, qw)` (Redux
  convention, self-inverse). The old matrix-conjugation approach was
  correct but unnecessary now that the pipeline uses Blender's importer
  for the up-axis swap.
- Removed the now-pointless `import_yup=False` and `export_yup=False`
  arguments from glTF wrapper calls.

### Internal
- Triangle winding flip remains required (the negate-X step is a
  reflection with det = -1) and is still wired in for both directions.
- RFG and WRL importers were not affected — they build bmesh directly
  without a glTF intermediary, so they kept their existing
  `(-rx, -rz, ry)` direct conversion.

---

## v0.7.6 — 2026-05-07

### Fixed
- Quaternion conversion direction. With the V3M-aligned `_M` matrix
  introduced in v0.7.5, the conjugation in `quat_rf_to_blender` was
  using `M·R·M^T` when it should have been `M^T·R·M`. Roundtrips were
  already correct because the inverse used the matching wrong direction,
  but the rotation itself was reflected — particles and dummies fired
  in mirrored directions in-game.

### Internal
- Verification suite added: position roundtrip preserves to 1e-16
  precision, normalized quaternion roundtrip is exact (allowing for
  q ≡ -q sign ambiguity), and rotation equivalence holds across
  cardinal-axis and arbitrary test quats.

---

## v0.7.5 — 2026-05-07

### Changed
- Coordinate convention realigned to match RF Static Mesh Tools and
  RF Character Tools. Previously the VFX add-on used `(-rz, -rx, ry)`
  for RF → Blender; it now uses `(-rx, -rz, ry)` like the other tools.
  Imported VFX content sits in the same orientation as imported V3M /
  V3C content, so they can be combined in one scene without manual
  rotation.
- Triangle winding is now flipped on both import and export
  (`_flip_gltf_winding_in_place` is invoked after vfx2obj writes glTF
  on import, and before vfx2obj reads glTF on export). Required because
  the new `_M` matrix has det = -1.

### Fixed
- RFG map import: brush coordinate formula corrected to `(-rz, -rx, ry)`,
  brush world position now applied correctly to vertices.
- RFG: added "Import at Origin" option for editing convenience (skips
  the world-position translation).

---

## v0.7.4 — 2026-04-18

### Added
- "Bake Rigged Mesh → Shape Keys" operator: convert an armature-driven
  animation into per-frame shape keys, suitable for VFX morph export.
- "Wire RF Timing on Existing Shape Keys" operator: take a shape-keyed
  mesh and apply RF-style timing curves (linear interpolation,
  per-frame keys) without re-baking the geometry.
- Quake MDL/MD2 importer for asset migration: reads classic Quake
  assets with RF unit scaling (1/12 factor) so they slot into RF maps
  at appropriate scale.
- WRL terrain importer.

### Internal
- `Alpine_VFX` event type sketch drafted (not yet posted to Goober for
  Alpine Faction integration).

---

## v0.7.3 — internal build, not released

(Coordinate-convention experimentation that didn't ship.)

---

## v0.7.2 — internal build, not released

(Refactoring pass, not released.)

## v0.7.1

**Armature → Shape Keys (Mixamo / Rigged)**
One-click operator that auto-detects mesh and armature, reads frame range directly from the armature action, applies rotation and scale before baking so Mixamo imports come in at the correct size and orientation, bakes through the depsgraph capturing all modifiers and constraints, and removes the armature after baking so the scene is clean for export.

**Shape Keys → RF Timing (Quake / Pre-keyed)**
One-click operator for meshes that already have geometry stored in shape keys but no keyframes wired up. Clears any existing shape key animation, sequences every key one-per-frame from scene start with constant interpolation, and updates scene frame end to match.

**Transform apply fix (Blender 4.x / 5.x)**
The glTF exporter parameter `export_apply` was renamed to `use_mesh_modifiers` in Blender 4.x. The old name was being passed unconditionally and silently ignored, meaning transforms were never actually applied on Blender 4.x and 5.x. Both export paths are now fixed.

**RFG importer — coordinate conversion fix**
The RF to Blender coordinate conversion formula was wrong. `(-rz, -rx, ry)` was rotating geometry into an incorrect orientation. Corrected to `(rx, -rz, ry)` — RF X stays Blender X, RF Y (up) becomes Blender Z, RF Z (forward) becomes Blender -Y.

**RFG importer — parse offset fixes**
Two byte offsets were wrong in the brush parser. An unknown 4-byte field between the brush UID and position was not being skipped, and a single modifiability byte between the position and rotation matrix was also not being skipped. Combined, these caused the rotation matrix to be read from the wrong file offset, returning all zeros and corrupting every vertex transform.

**File > New > RF VFX template**
Install button in the New VFX / Import panel writes an app template into Blender's user scripts folder. After a restart, File > New > RF VFX produces a completely clean scene — no default cube, no stray collections, RF_VFX collection and RFVFX_ROOT already present.

**Improved Add Selected To RF_VFX**
Now recursively collects all children of selected objects, auto-includes armature parents of selected meshes, removes objects from their old collections after moving, and cleans up any collections that end up empty — including the default Blender collection.

---

## v0.7.0

**RFG Map Importer (New)**
Import `.rfg` map geometry directly into Blender as mesh objects with UVs and materials. Supports RF1 and RF2 format variants. Options: Merge Brushes by Texture, Skip Invisible Faces, Import into RF_VFX Collection. Materials are named after the RF texture filename with texture names pre-set on import.

**Materials Panel (New)**
New panel in the RF VFX sidebar for per-slot texture assignment. Each material slot has its own texture name field, browse button, extension type label, and Additive toggle. Slot-indexed browse buttons allow each material slot to be targeted independently.

**Morph FPS Setting (New)**
Scene-level dropdown to select 5, 10, or 15 FPS for morph playback. Lower values mean more interpolation (good for smooth organic motion — cloth, foliage, water). Higher values mean less interpolation (better for sharp animation — impacts, explosions). Used to compute the correct VFX binary end_frame value.

**Animation Speed Step (New)**
New Speed Step field in the bake dialog. Samples every Nth source frame to produce fewer shape keys, reducing file size and engine overhead. RF interpolates between the remaining frames.

**Animation Bake Dialog (New)**
Live readout showing shape key count, RF duration, Blender duration, speed ratio, and estimated VFX file size before committing. Start and End Frame auto-detected from actual keyframes.

**Dummies Panel (New)**
8 one-click preset buttons: `$prop_flag`, `muzzle_1`, `thruster`, `corona`, `chaingun_1`, `primary_1`, `secondary_1`, `interface_1`. Auto-parents to selected mesh. Thruster and corona auto-increment numbering.

**Dummy Export (New)**
All empties in RF_VFX now export as DMMY sections. RFVFX_ROOT and Scene Root are automatically excluded.

**Particle Preview (New)**
Preview Particles bakes 3 seconds of simulation to keyframed planes in the viewport. Clear Preview removes them. Respects all emitter settings.

**Double-Sided Faces (New)**
Export option duplicates all faces with reversed normals for correct in-game rendering of foliage and flags.

**Morph Animation (Fixed)**
- Fixed vertex count mismatch between glTF and baked shape keys crashing RED on larger animations
- Fixed frame start auto-detection — now scans actual keyframes instead of always using scene.frame_start
- Fixed frame end fallback for modifier-driven animations — now uses scene.frame_end
- Fixed bake to always evaluate via the depsgraph — old path bypassed modifiers entirely
- Fixed depsgraph staleness — forces a full update per frame
- Fixed world transform applied twice during bake
- Fixed shape key keyframe placement after step bake
- Scene frame_end updated after bake to match actual baked key count
- Frame cap raised from 500 to 2000

**Sidecar / vfx2obj Pipeline (Fixed)**
Sidecar JSON now stores `_morph_fps`, `_num_frames`, and `_end_frame_15fps`. vfx2obj reads `_end_frame_15fps` as the authoritative VFX binary end_frame value.

**Texture Export (Fixed)**
Materials with an explicit texture_name are temporarily renamed before glTF export so vfx2obj reads the correct texture, then restored immediately after.

**Icon Crashes (Fixed)**
Fixed UI crash caused by 6 deprecated or removed Blender icons in newer Blender versions.

**Coordinate System (Fixed)**
Corrected axis mapping: Blender Y→RF X, Blender Z→RF Y, Blender X→RF Z (det=+1). Removed incorrect 180° flip from particle export.

**UV Mapping (Fixed)**
Added V-coordinate flip on import/export (v_rf = 1 − v_gltf) — fixes vertical texture flip.

**Lighting & Fullbright (Fixed)**
Vertex colors corrected from 1.0 to 0.5 for neutral scene lighting. Fullbright flag now correctly writes binary flag 0x0010.

**DDS Texture Support**
Texture fields, file browsers, and readiness check now accept `.dds` alongside `.tga` and `.vbm`.

**Readiness Check (Updated)**
Warnings updated for Alpine Faction limits: 2,000 verts / 1,500 faces per mesh.

**Blender 5.0 Compatibility**
Fixed EMPTY_AXES icon crash, bpy.props annotation style, and animation API calls.

**Removed**
- New from Template feature removed
- Seethrough, No-interp, Save Parent flags removed from Object Flags panel
- DummyMenu class removed (was causing silent registration failure)

---

## v0.6.3

**Lighting (Fixed)**
Vertex colors default 0.5 (neutral scene lighting). Fullbright flag now correctly sets vertex colors 1.0, material self-illum 1.0, and binary flag 0x0010 — matching stock VFX files exactly.

**Coordinate System (Fixed)**
Corrected axis mapping: Blender Y→RF X, Blender Z→RF Y, Blender X→RF Z (det=+1). Removed incorrect 180° rotation from particle export.

**UV Mapping (Fixed)**
Added V-coordinate flip on import/export (v_rf = 1 - v_gltf) to fix vertical texture flip.

**Dummies Panel (New)**
Dedicated panel with one-click preset buttons for engine-recognised dummy names: `$prop_flag`, `muzzle_1`, `thruster`, `corona`, `chaingun_1`, `primary_1`, `secondary_1`, `interface_1`. Auto-parents to selected mesh.

**Dummy Export (New)**
All empties in RF_VFX export as DMMY sections. RFVFX_ROOT and Scene Root automatically excluded.

**Double Sided Faces (New)**
Export option duplicates all faces with reversed normals for foliage, flags, and similar meshes.

**Particle Preview (New)**
Preview Particles bakes 3 seconds of simulation to keyframed planes. Respects all emitter settings.

**Object Flags (Cleaned Up)**
Billboard, Rod, Fullbright only. Morph auto-detected from shape keys as info display. Removed: Seethrough, No-interp, Save Parent.

**DDS Texture Support**
Texture fields, file browsers, and readiness check accept `.dds` alongside `.tga` and `.vbm`.

**Blender 5.0 Compatibility**
Fixed EMPTY_AXES icon crash. Fixed bpy.props annotation style.

---

## v0.6.2

- Improved export pipeline stability
- Fixed edge cases in the glTF → VFX conversion path
- Minor UI improvements

---

## v0.6.1

- Initial public release
- Basic import/export of `.vfx` files
- Shape key morph animation support
- Particle emitter creation
- Billboard, Rod, Fullbright object flags
