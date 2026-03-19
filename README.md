# RF VFX Tools

A Blender add-on for creating and editing Red Faction 1 `.vfx` files — without requiring 3ds Max or any Volition tools.

Built for the [Alpine Faction](https://github.com/GooberRF/alpinefaction) community.

![Version](https://img.shields.io/badge/version-0.7.1-blue) ![Blender](https://img.shields.io/badge/Blender-3.6--5.0-orange)

---

## What is this?

Red Faction's `.vfx` format stores animated mesh effects — explosions, thrusters, flags, shield effects, and more. Previously the only way to create or edit these files was through Volition's 3ds Max exporter, which is no longer accessible. RF VFX Tools brings the full pipeline into Blender.

---

## Features

- **Import and export `.vfx` files** — edit existing effects or build new ones from scratch
- **Morph animation** — bake any Blender animation (shape keys, armatures, modifiers, cloth sim) into RF per-vertex morph frames
- **One-click Mixamo / armature → shape keys** — import a Mixamo FBX, click once, done
- **One-click pre-keyed shape keys → RF timing** — for Quake/MD2-style models with existing shape key geometry
- **Particle emitters** — create and configure PART sections with full settings
- **Dummies (attachment points)** — one-click presets for all engine-recognised dummy names
- **Materials panel** — per-slot texture assignment with `.tga`, `.vbm`, and `.dds` support
- **RFG map importer** — import Red Faction level geometry directly into Blender for reference
- **File > New > RF VFX** — installable scene template for a clean starting point every time
- **Blender 3.6 through 5.0 compatible**

---

## Installation

1. Download `rf_vfx_tools_vX.Y.Z.zip` from [Releases](../../releases)
2. Open Blender → Edit > Preferences > Add-ons
3. Click **Install...** and select the zip
4. Enable **RF VFX Tools**
5. An **RF VFX** tab appears in the 3D Viewport sidebar (press **N**)

> If upgrading from a previous version, remove the old version first, restart Blender, then install the new one.

---

## Quick Start

### Create a new VFX
1. Click **New RF VFX Scene**
2. Model your mesh and assign a texture in the Materials panel
3. Click **Add Selected To RF_VFX**
4. Set flags if needed (optional)
5. Set the output path → **Export VFX**

### Edit an existing VFX
1. Click **Import VFX...** → edit the mesh
2. Export — Patch mode auto-detects the template
3. Vertex count must stay the same

### Morph animation (custom)
1. Animate your mesh (shape keys, modifiers, armatures, etc.)
2. Click **Bake Animation to Shape Keys**
3. Set Morph FPS in Advanced Options
4. Click **Export VFX**

### Morph animation (Mixamo / rigged mesh)
1. Import your Mixamo FBX into Blender
2. Select the mesh or armature
3. Click **Armature → Shape Keys (Mixamo)** — handles everything automatically
4. Click **Export VFX**

### Morph animation (Quake / pre-keyed shapes)
1. Import a model that already has shape keys with geometry data
2. Click **Shape Keys → RF Timing (Quake)**
3. Click **Export VFX**

---

## File > New Template

Install a clean RF VFX scene template directly into Blender's File > New menu:

- In the **New VFX / Import** panel, click **Install RF VFX Template**
- Restart Blender
- Use **File > New > RF VFX** for a guaranteed-clean starting scene every time

---

## Panel Overview

| Panel | Purpose |
|---|---|
| Workflows | Step-by-step cheat sheets |
| New VFX / Import | Create scenes, import VFX, import RFG maps |
| Materials | Per-slot texture assignment and Additive toggle |
| Object Flags | Billboard, Rod, Fullbright per mesh |
| Dummies | One-click attachment point presets |
| Particle Emitters | Create and configure particle emitters |
| Export | Output settings, prepare tools, Export VFX button |

---

## Object Flags

| Flag | Description |
|---|---|
| Billboard | Always faces camera. Use for flat effects: sparks, flares. Mutually exclusive with Rod. |
| Rod | Cylindrical facing locked to longest axis. Use for beams and streaks. Auto-enables Fullbright. |
| Fullbright | Ignores scene lighting. Always fully lit. |

---

## Dummies (Attachment Points)

| Dummy | Description |
|---|---|
| `$prop_flag` | CTF flag attachment — must be parented to the flag mesh |
| `muzzle_1` | Weapon muzzle flash (arrow) |
| `thruster` | Thruster VFX (auto-increments) |
| `corona` | Glare/corona position (auto-increments) |
| `chaingun_1` | Vehicle mounted weapon |
| `primary_1` | Primary weapon position |
| `secondary_1` | Secondary weapon position |
| `interface_1` | Player interaction / entry point |

---

## Morph FPS

| FPS | Best for |
|---|---|
| 5 fps | Cloth, foliage, water, long ambient loops |
| 10 fps | Moderate animations |
| 15 fps | Explosions, impacts, sharp fast animation |

---

## Limits

- All meshes must have at least one UV map
- `end_frame` must be ≥ 5
- Vertex count must stay constant across morph frames
- Billboard and Rod are mutually exclusive
- Alpine Faction removes vanilla RF hard limits — be sensible with morph animation file sizes

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Invisible in-game | Check texture filename exists in RF's data files |
| Inside-out faces | Enable Flip Faces in Advanced Options |
| Wrong position/rotation | Apply transforms before export (Ctrl+A) |
| Particles going wrong direction | Rotate the arrow emitter — tip = emit direction |
| VFX skipped / crash on load | Run Readiness Check; ensure end_frame ≥ 5 |
| Mesh too dark | Enable Fullbright flag |
| No animation in-game | Bake Animation to Shape Keys before export |
| Animation cuts off halfway | Re-bake — scene frame_end updates automatically |
| `$prop_flag` not attaching | Dummy must be parented to the flag mesh, not Scene Root |

---

## Community & Support

- **Discord:** [discord.gg/factionfiles](https://discord.gg/factionfiles) — ask in the RF modding channels
- **FactionFiles:** [factionfiles.com](https://www.factionfiles.com)
- **Alpine Faction:** [github.com/GooberRF/alpinefaction](https://github.com/GooberRF/alpinefaction)
- **Contact:** romekaddams on Discord

---

## Documentation

Full documentation is included in each release:

- `RF_VFX_Guide.pdf` — comprehensive guide covering every panel and feature
- `RF_VFX_Quick_Reference.pdf` — cheat sheet for common workflows
- `RF_VFX_Reference.pdf` — engine integration reference (dummies, .tbl mappings, cockpit HUD naming, binary flags)

---

*Created by Romek and Claude*
