bl_info = {
    "name": "RF VFX Tools",
    "author": "RomekRF",
    "version": (0, 7, 8),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > RF VFX",
    "description": "Create and edit Red Faction 1 .vfx files in Blender. Aligned with RF Static Mesh Tools / RF Character Tools coordinate convention. Import RFG/WRL map geometry, morph animation, particles, dummies — no 3ds Max required.",
    "category": "Import-Export",
}

import bpy
import os, sys, io, shutil, struct, tempfile, traceback, subprocess, json, hashlib, csv, re
import datetime
from bpy.props import StringProperty, BoolProperty, FloatProperty, IntProperty, EnumProperty, PointerProperty

# Drop only sections known to crash the mesh converter (keep everything else so transforms aren't lost)
DROP_SECTIONS = {b"PART"}  # VParticle blocks (we'll support later)


def _op_has_prop(op, prop_name: str) -> bool:
    try:
        props = op.get_rna_type().properties
        return prop_name in props
    except Exception:
        return False

def _gltf_export(filepath: str, use_selection: bool, export_apply: bool, frame_start=None, frame_end=None, force_sampling: bool=False, full_mode: bool=False):
    """Export glTF for VFX pipeline.

    full_mode=False (patch): strips normals/UVs/materials so vertex counts match original VFX.
    full_mode=True  (true export): includes normals, UVs, materials for building new VFX.
    """
    op = bpy.ops.export_scene.gltf
    kwargs = {
        "filepath": filepath,
        "export_format": "GLTF_SEPARATE",
        "use_selection": bool(use_selection),
    }
    # export_apply was renamed to use_mesh_modifiers in Blender 4.x.
    # Try the old name first, fall back to the new name so transforms
    # are actually applied on all supported Blender versions.
    if _op_has_prop(op, "export_apply"):
        kwargs["export_apply"] = bool(export_apply)
    elif _op_has_prop(op, "use_mesh_modifiers"):
        kwargs["use_mesh_modifiers"] = bool(export_apply)
    # Explicitly set export_yup=True even though it's the documented default.
    # Blender's operator system uses last-used UI values as runtime defaults,
    # so if the user ever unchecked "+Y Up" in an export dialog this session,
    # subsequent script exports inherit that. Setting it explicitly removes
    # the dependency on UI state.
    if _op_has_prop(op, "export_yup"):
        kwargs["export_yup"] = True

    if full_mode:
        # Enable normals, UVs, materials for true export
        if _op_has_prop(op, "export_normals"):
            kwargs["export_normals"] = True
        if _op_has_prop(op, "export_texcoords"):
            kwargs["export_texcoords"] = True
        if _op_has_prop(op, "export_materials"):
            try:
                kwargs["export_materials"] = "EXPORT"
            except Exception:
                pass
        # Disable things we don't need
        for _k in ("export_tangents","export_colors","export_attributes","export_skins","export_cameras","export_lights"):
            if _op_has_prop(op, _k):
                kwargs[_k] = False
    else:
        # Patch mode: strip normals/UVs so glTF vertex counts match the original VFX stream
        if _op_has_prop(op, "export_materials"):
            try:
                kwargs["export_materials"] = "NONE"
            except Exception:
                pass
        for _k in ("export_normals","export_tangents","export_texcoords","export_colors","export_attributes","export_skins","export_cameras","export_lights"):
            if _op_has_prop(op, _k):
                kwargs[_k] = False
        if _op_has_prop(op, "export_vertex_color"):
            try:
                kwargs["export_vertex_color"] = "NONE"
            except Exception:
                kwargs["export_vertex_color"] = False
        if _op_has_prop(op, "export_image_format"):
            try:
                kwargs["export_image_format"] = "NONE"
            except Exception:
                pass

    # Common: preserve RF metadata through Blender custom properties
    if _op_has_prop(op, "export_extras"):
        kwargs["export_extras"] = True
    if _op_has_prop(op, "export_custom_properties"):
        kwargs["export_custom_properties"] = True
    if _op_has_prop(op, "export_animations"):
        kwargs["export_animations"] = True
    if _op_has_prop(op, "export_morph"):
        kwargs["export_morph"] = True
    if _op_has_prop(op, "export_shape_keys"):
        kwargs["export_shape_keys"] = True

    # Optional frame range (e.g., to match a template VFX)
    if (frame_start is not None) or (frame_end is not None):
        if _op_has_prop(op, "export_frame_range"):
            kwargs["export_frame_range"] = True
        if frame_start is not None and _op_has_prop(op, "export_frame_start"):
            kwargs["export_frame_start"] = int(frame_start)
        if frame_end is not None and _op_has_prop(op, "export_frame_end"):
            kwargs["export_frame_end"] = int(frame_end)
        if force_sampling and _op_has_prop(op, "export_force_sampling"):
            kwargs["export_force_sampling"] = True

    return op(**kwargs)

def _gltf_import(filepath: str):
    op = bpy.ops.import_scene.gltf
    kwargs = {"filepath": filepath}
    # Preserve extras into Blender custom properties (so they can be exported back out).
    if _op_has_prop(op, "import_extras"):
        kwargs["import_extras"] = True
    # NOTE: We deliberately do NOT pass import_yup=False here. vfx2obj writes
    # glTF data in standard Y-up RH form (Redux convention: RF→glTF is just
    # negate-X). Blender's importer rotates Y-up→Z-up automatically, which
    # combined with the negate-X gives the V3M-tool-aligned final orientation.
    # Older builds tried import_yup=False — that flag doesn't exist in
    # Blender 4.x's glTF importer (the rotation always happens), which made
    # imported meshes lie flat on their backs.
    return op(**kwargs)


def _addon_dir(): return os.path.dirname(__file__)
def _vendor_dir(): return os.path.join(_addon_dir(), "vendor")

def _get_or_create_text(name: str):
    """Get or create a Blender text block by name."""
    txt = bpy.data.texts.get(name)
    if txt is None:
        txt = bpy.data.texts.new(name)
    return txt

def _log_textblock():
    return _get_or_create_text("RFVFX_Log")

def _write_log(header: str, body: str):
    txt = _log_textblock()
    txt.clear()
    txt.write(header + "\n\n" + body)
    scr = getattr(bpy.context, "screen", None)
    if scr:
        for area in scr.areas:
            if area.type == "TEXT_EDITOR":
                area.spaces.active.text = txt
                break

def _popup(msg: str, title="RF VFX"):
    def draw(self, _context):
        for line in msg.splitlines():
            self.layout.label(text=line)
    bpy.context.window_manager.popup_menu(draw, title=title, icon="INFO")

def _read_vfx_header(path: str):
    with open(path, "rb") as f:
        b = f.read(8)
    if len(b) < 8 or b[:4] != b"VSFX":
        return None
    ver = int.from_bytes(b[4:8], "little", signed=False)
    return ver

def _read_template_end_frame(template_vfx: str):
    """Read end_frame from a template VFX using the vendored parser.

    Returns int or None.
    """
    try:
        sys.path.insert(0, _vendor_dir())
        import vfx2obj as _v
        hdr, _mats, _meshes, _dummies, _parts = _v.parse_vfx(template_vfx)
        return int(getattr(hdr, "end_frame", 0))
    except Exception:
        return None
    finally:
        # avoid polluting Blender's sys.path too much
        try:
            if sys.path and sys.path[0] == _vendor_dir():
                sys.path.pop(0)
        except Exception:
            pass



def _resolve_output_vfx_path(out_path: str, tmpl_vfx: str, vfx_name: str = ""):
    """Allow Output to be either a folder or a full .vfx filepath.

    If a folder is provided, we auto-name the output based on:
      1) VFX Name (if set on any mesh object)
      2) template base name (if set)
      3) current .blend filename (if saved)
      4) 'scene.vfx'
    """
    p = (out_path or "").strip()
    if not p:
        return "", ""

    raw = p

    # Treat as folder if it ends with a separator, exists as a folder,
    # or has no extension.
    is_dir = False
    if p.endswith(("\\", "/")):
        is_dir = True
        p = p.rstrip("\\/")  # normalize
    elif os.path.isdir(p):
        is_dir = True
    else:
        ext = os.path.splitext(p)[1]
        if ext == "":
            is_dir = True

    note = ""
    if is_dir:
        out_dir = p
        base = ""
        # Priority: 1) VFX Name from mesh objects, 2) template, 3) .blend filename, 4) 'scene'
        if vfx_name:
            base = vfx_name
        elif tmpl_vfx:
            base = os.path.splitext(os.path.basename(tmpl_vfx))[0] or ""
        if (not base) and getattr(bpy.data, "filepath", ""):
            base = os.path.splitext(os.path.basename(bpy.data.filepath))[0] or ""
        if not base:
            base = "scene"
        out_file = os.path.join(out_dir, base + ".vfx")
        note = f"Output was a folder; using '{out_file}'"
        return out_file, note

    # If it looks like a file but doesn't end with .vfx, append it.
    if os.path.splitext(p)[1].lower() != ".vfx":
        p2 = p + ".vfx"
        note = f"Output did not end with .vfx; using '{p2}'"
        return p2, note

    if os.path.isdir(p):
        # extremely edge case: user entered a folder named "*.vfx"
        out_file = os.path.join(p, "scene.vfx")
        note = f"Output points to a folder; using '{out_file}'"
        return out_file, note

    if raw != p:
        note = f"Normalized output to '{p}'"
    return p, note

def _is_printable_4cc(b4: bytes) -> bool:
    if len(b4) != 4: return False
    return all(0x20 <= c <= 0x7E for c in b4)

def _scan_section_start(data: bytes, max_scan=65536):
    n = min(len(data) - 8, max_scan)
    for off in range(8, n, 4):
        t = data[off:off+4]
        if not _is_printable_4cc(t): 
            continue
        size = struct.unpack_from("<I", data, off+4)[0]
        if size < 8: 
            continue
        end = off + 4 + size
        if end > len(data): 
            continue
        # quick chain validation
        ok = True
        cur = off
        for _ in range(8):
            if cur + 8 > len(data): break
            t2 = data[cur:cur+4]
            if not _is_printable_4cc(t2): ok = False; break
            s2 = struct.unpack_from("<I", data, cur+4)[0]
            if s2 < 8 or cur + 4 + s2 > len(data): ok = False; break
            cur = cur + 4 + s2
        if ok:
            return off
    return 128

def _normalize_pad_header_keep_version(src_vfx: str, dst_vfx: str):
    """
    Fix 'header issue' safely:
    - Keep original version value
    - Ensure header is 128 bytes (pad with zeros)
    - Copy sections (optionally drop PART)
    This avoids lying about version and preserves transforms across files.
    """
    data = open(src_vfx, "rb").read()
    if len(data) < 8 or data[:4] != b"VSFX":
        raise RuntimeError("Not a VSFX file.")

    ver = struct.unpack_from("<I", data, 4)[0]
    start = _scan_section_start(data)
    off = start
    kept = skipped = 0

    out = bytearray()
    out += b"VSFX"
    out += struct.pack("<I", ver)
    out += b"\x00" * (128 - 8)

    while off + 8 <= len(data):
        t = data[off:off+4]
        if not _is_printable_4cc(t): break
        size = struct.unpack_from("<I", data, off+4)[0]
        if size < 8 or off + 4 + size > len(data): break

        chunk = data[off:off + 4 + size]
        if t in DROP_SECTIONS:
            skipped += 1
        else:
            out += chunk
            kept += 1

        off = off + 4 + size

    open(dst_vfx, "wb").write(out)
    return ver, kept, skipped, start

def _run_vendor(script_name: str, argv: list[str], cwd: str):
    vdir = _vendor_dir()
    script = os.path.join(vdir, script_name)
    if not os.path.exists(script):
        raise RuntimeError(f"Missing vendor script: {script_name}")

    env = os.environ.copy()
    env["PYTHONPATH"] = vdir + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    cmd = [sys.executable, script] + argv
    p = subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout

def _flip_gltf_winding_in_place(gltf_path: str) -> str:
    """
    Swaps i0 <-> i2 for every triangle in every indexed primitive.
    This fixes inside-out meshes for CCW glTF viewers (Blender).
    Returns a short status string for logging.
    """
    with open(gltf_path, "r", encoding="utf-8") as f:
        g = json.load(f)

    # resolve .bin path
    buffers = g.get("buffers", [])
    if not buffers:
        return "flip_winding: no buffers (skipped)"
    uri = buffers[0].get("uri", "")
    if not uri:
        return "flip_winding: embedded/empty buffer uri (skipped)"
    bin_path = os.path.join(os.path.dirname(gltf_path), uri)
    if not os.path.exists(bin_path):
        return f"flip_winding: missing bin {bin_path} (skipped)"

    data = bytearray(open(bin_path, "rb").read())

    accessors = g.get("accessors", [])
    views = g.get("bufferViews", [])
    flipped = 0

    def comp_size(ct):
        return {5121:1, 5123:2, 5125:4}.get(ct, 0)

    for mesh in g.get("meshes", []) or []:
        for prim in mesh.get("primitives", []) or []:
            if "indices" not in prim:
                continue
            ai = prim["indices"]
            if ai is None or ai >= len(accessors):
                continue
            acc = accessors[ai]
            if acc.get("type") != "SCALAR":
                continue
            ct = acc.get("componentType")
            sz = comp_size(ct)
            if sz == 0:
                continue

            count = int(acc.get("count", 0))
            if count < 3 or (count % 3) != 0:
                continue

            vi = acc.get("bufferView")
            if vi is None or vi >= len(views):
                continue
            view = views[vi]

            bv_off = int(view.get("byteOffset", 0))
            a_off = int(acc.get("byteOffset", 0))
            off = bv_off + a_off

            # NOTE: indices are tightly packed in practice; if byteStride exists, we ignore (rare for indices)
            end = off + count * sz
            if end > len(data):
                continue

            # read indices
            fmt = {5121:"B", 5123:"H", 5125:"I"}[ct]
            # unpack -> list of ints
            vals = list(struct.unpack_from("<" + fmt*count, data, off))

            # flip triangles
            for t in range(0, count, 3):
                vals[t], vals[t+2] = vals[t+2], vals[t]
            struct.pack_into("<" + fmt*count, data, off, *vals)
            flipped += count // 3

    if flipped > 0:
        open(bin_path, "wb").write(data)
    return f"flip_winding: flipped_triangles={flipped}"


def _gltf_has_rf_keyframed_meta(gltf_path: str) -> bool:
    """Returns True if any node has extras.rf_vfx.keyframed_block_b64 (new robust round-trip)."""
    try:
        with open(gltf_path, "r", encoding="utf-8") as f:
            g = json.load(f)
        for n in (g.get("nodes") or []):
            ex = n.get("extras") if isinstance(n, dict) else None
            if not isinstance(ex, dict):
                continue
            rf = ex.get("rf_vfx")
            if isinstance(rf, dict) and isinstance(rf.get("keyframed_block_b64"), str) and rf.get("keyframed_block_b64"):
                return True
        return False
    except Exception:
        return False

class RFVFX_Settings(bpy.types.PropertyGroup):
    vfx_name: StringProperty(name="VFX Name", default="",
        description="Name for the exported VFX file. Leave blank to auto-name from template or .blend file")


    import_vfx: StringProperty(name="VFX File", subtype="FILE_PATH", default="")

    export_vfx_out: StringProperty(name="Output VFX", subtype="FILE_PATH", default="")
    export_template_vfx: StringProperty(name="Template VFX", subtype="FILE_PATH", default="")
    use_last_import_as_template: BoolProperty(name="Use last imported VFX as Template", default=False)

    export_apply_transforms: BoolProperty(name="Apply Transforms", default=True,
        description="Bake object Location/Rotation/Scale into mesh data before export. Recommended ON to ensure correct orientation")

    # When exporting using a template, we can preserve the template-authored pivot + key0 TRS
    # to prevent Blender from "recentering" assets.
    export_preserve_template_trs: BoolProperty(name="Preserve Template TRS", default=True)

    # If a template is provided, optionally force the Blender/glTF export frame range to match
    # the template's end_frame (helps avoid truncated exports like 0..29 instead of 0..45).
    export_use_template_frame_range: BoolProperty(name="Use Template Frame Range", default=True)

    # Export mode: determines whether to patch a template or build from scratch
    export_mode: EnumProperty(
        name="Export Mode",
        items=[
            ("AUTO", "Auto", "Patch if template available, otherwise True Export"),
            ("PATCH", "Template Patch", "Patch an existing VFX template (safer, preserves internal tables)"),
            ("TRUE_EXPORT", "True Export", "Build a new VFX from scratch (for new content, adding/removing meshes)"),
        ],
        default="AUTO",
    )

    # ✅ new: winding fix (default ON) applies both import and export (symmetrical)
    fix_winding: BoolProperty(name="Fix inside-out meshes (flip winding)", default=False)

    double_sided: BoolProperty(name="Double Sided Faces", default=False,
        description="Duplicate all faces with reversed normals so both sides render in-game. Doubles face count.")

    morph_fps: EnumProperty(
        name="Morph FPS",
        description=(
            "How many vertex positions are stored per second in the VFX file. "
            "The engine always plays back at 15fps and interpolates between stored frames, "
            "so lower rates save file size with no visible quality loss on smooth motion. "
            "Use 5fps for cloth and foliage, 15fps for explosions and sharp effects."
        ),
        items=[
            ("5",  "5 fps",  "Stores 3x fewer frames than 15fps. Best for cloth, foliage, ambient loops."),
            ("10", "10 fps", "Stores 1.5x fewer frames than 15fps. Good for moderate animations."),
            ("15", "15 fps", "Stores every frame. Use for explosions, impacts, anything frame-perfect."),
        ],
        default="15",
    )

    last_import_vfx: StringProperty(name="(internal) last import", default="")


    # Batch validation (foolproofing)
    validate_input_dir: StringProperty(name="Validate Folder", subtype="DIR_PATH", default="")
    validate_out_dir: StringProperty(name="Report Output (optional)", subtype="DIR_PATH", default="")
    validate_strict: BoolProperty(name="Strict (fail on errors)", default=True)

    show_import_advanced: BoolProperty(name="Advanced", default=False)
    show_export_advanced: BoolProperty(name="Advanced", default=False)
    show_prep_tools: BoolProperty(name="Prepare", default=False)
    keep_temp: BoolProperty(name="Keep Temp Files", default=False)

    export_anchor: StringProperty(
        name="Export Anchor",
        description=(
            "Object whose world position becomes (0,0,0) in the exported VFX. "
            "Only affects root-level objects. Leave blank to skip recentering. "
            "Default 'RFVFX_ROOT' matches the convention used by imports"
        ),
        default="RFVFX_ROOT",
    )

class RFVFX_OT_ImportVFX(bpy.types.Operator):
    bl_idname = "rfvfx.import_vfx"
    bl_label = "Import VFX"
    bl_description = "Open a .vfx file and import it into Blender"
    bl_options = {"REGISTER"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.vfx", options={"HIDDEN"})

    def invoke(self, context, event):
        # Pre-populate with last imported path if available
        s = context.scene.rfvfx
        last = bpy.path.abspath(s.import_vfx).strip() if s.import_vfx.strip() else ""
        if last and os.path.isfile(last):
            self.filepath = last
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        s = context.scene.rfvfx
        vfx = bpy.path.abspath(self.filepath).strip()
        if not vfx or not os.path.exists(vfx):
            self.report({"ERROR"}, "Pick a valid .vfx file.")
            return {"CANCELLED"}

        # Store path so export panel can use it as template
        s.import_vfx = self.filepath

        ver = _read_vfx_header(vfx)
        if ver is None:
            _popup("Not a VSFX file (bad header).", title="RF VFX: Import")
            return {"CANCELLED"}

        tmpdir = tempfile.mkdtemp(prefix="rfvfx_import_")
        norm_vfx = os.path.join(tmpdir, "input_norm.vfx")

        header = (
            "RF VFX Import\n"
            f"Input: {vfx}\n"
            f"Header version: 0x{ver:08X}\n"
            f"Temp: {tmpdir}\n"
        )

        try:
            ver2, kept, skipped, start = _normalize_pad_header_keep_version(vfx, norm_vfx)
            header += f"Normalize: start_off={start} kept={kept} skipped={skipped} ver_preserved=0x{ver2:08X}\n\n"

            args = ["--gltf"]
            args.append(norm_vfx)

            header += "Run:\n  vfx2obj.py " + " ".join(args) + "\n"
            rc, out = _run_vendor("vfx2obj.py", args, cwd=_vendor_dir())
            if rc != 0:
                _write_log(header, out)
                _popup("Import failed. Open Text Editor → RFVFX_Log.", title="RF VFX: Import")
                if not s.keep_temp:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                return {"CANCELLED"}

            gltf = os.path.splitext(norm_vfx)[0] + ".gltf"
            if not os.path.exists(gltf):
                _write_log(header, out)
                _popup("Conversion ran but glTF was not produced. See RFVFX_Log.", title="RF VFX: Import")
                if not s.keep_temp:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                return {"CANCELLED"}

            # The new RF↔Blender conversion has det=-1 (reflection), so the
            # glTF written by vfx2obj has inverted triangle winding for Blender.
            # Flip it back so faces show correct normals/visibility in Blender.
            try:
                wfix = _flip_gltf_winding_in_place(gltf)
                header += wfix + "\n"
            except Exception as e:
                header += f"flip_winding (import) failed: {e}\n"

            _write_log(header, out)

            _gltf_import(filepath=gltf)

            # Set up imported particle emitters as proper Empties
            try:
                _authoring_register()  # ensure particle props are available
                nparts = _post_import_setup_particles()
            except Exception:
                nparts = 0

            s.last_import_vfx = vfx
            if s.use_last_import_as_template and not s.export_template_vfx.strip():
                s.export_template_vfx = vfx

            if not s.keep_temp:
                shutil.rmtree(tmpdir, ignore_errors=True)

            # Set viewport to RF orientation after import
            # _set_viewport_rf_orientation(context)  # disabled: don't change user's view

            self.report({"INFO"}, f"Imported VFX: {os.path.basename(vfx)}")
            return {"FINISHED"}

        except BaseException:
            tb = traceback.format_exc()
            _write_log(header, "ERROR:\n" + tb)
            _popup("Import failed hard. Open Text Editor → RFVFX_Log.", title="RF VFX: Import")
            if not s.keep_temp:
                shutil.rmtree(tmpdir, ignore_errors=True)
            return {"CANCELLED"}

class RFVFX_OT_ExportVFX(bpy.types.Operator):
    bl_idname = "rfvfx.export_vfx"
    bl_label = "Export VFX"
    bl_options = {"REGISTER"}

    def execute(self, context):
        s = context.scene.rfvfx

        # $prop_flag parent check — BLOCKING. Must be parented to a mesh, not Scene Root.
        # If parented to Scene Root, late-joining players see the flag stuck at base
        # while it is being carried because the engine can't resolve the attachment transform.
        for _o in _export_objs(context.scene):
            if _o.type == "EMPTY" and _o.name.lower() == "$prop_flag":
                _parent = _o.parent
                if _parent is None or _parent.name in ("RFVFX_ROOT", "Scene Root") or _parent.type != "MESH":
                    _popup(
                        "$prop_flag must be parented to a mesh — export cancelled.\n\n"
                        "If $prop_flag is parented to Scene Root, late-joining players\n"
                        "will see the flag stuck at the base while it is being carried.\n\n"
                        "Fix: select $prop_flag, Shift+click your flagpole mesh,\n"
                        "then Ctrl+P > Object.",
                        title="RF VFX: $prop_flag Error"
                    )
                    return {"CANCELLED"}
                break

        out_vfx_raw = bpy.path.abspath(s.export_vfx_out).strip()
        # Get VFX Name: scene-level property first, fall back to per-object custom_name
        _vfx_name = s.vfx_name.strip()
        if not _vfx_name:
            for _o in _export_objs(context.scene):
                if hasattr(_o, "rfvfx_props") and _o.rfvfx_props.custom_name.strip():
                    _vfx_name = _o.rfvfx_props.custom_name.strip()
                    break
        tmpl_vfx = bpy.path.abspath(s.export_template_vfx).strip()

        if not out_vfx_raw:
            self.report({"ERROR"}, "Set Output VFX path.")
            return {"CANCELLED"}

        if s.use_last_import_as_template and (not tmpl_vfx) and s.last_import_vfx.strip():
            tmpl_vfx = bpy.path.abspath(s.last_import_vfx).strip()

        out_vfx, out_note = _resolve_output_vfx_path(out_vfx_raw, tmpl_vfx, _vfx_name)

        if not out_vfx:
            self.report({"ERROR"}, "Could not resolve Output VFX path.")
            return {"CANCELLED"}

        out_dir = os.path.dirname(out_vfx)
        if out_dir and not os.path.exists(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        pivot_tool = os.path.join(_vendor_dir(), "pivot_patch_xkey0.py")
        has_template = bool(tmpl_vfx and os.path.exists(tmpl_vfx))
        tmpl_ok = False
        tmpl_ver = None
        tmpl_end = None
        if has_template:
            tmpl_ver = _read_vfx_header(tmpl_vfx)
            if bool(s.export_use_template_frame_range):
                tmpl_end = _read_template_end_frame(tmpl_vfx)
            if bool(s.export_preserve_template_trs) and os.path.exists(pivot_tool):
                tmpl_ok = (tmpl_ver == 0x00040006)

        # Resolve export mode
        mode = s.export_mode
        if mode == "AUTO":
            mode = "PATCH" if has_template else "TRUE_EXPORT"

        if mode == "PATCH" and not has_template:
            _popup("Patch mode requires a Template VFX. Import a VFX first, or switch to True Export mode.", title="RF VFX: Export")
            return {"CANCELLED"}

        if has_template and os.path.abspath(out_vfx) == os.path.abspath(tmpl_vfx):
            _popup("Refusing to overwrite the Template VFX. Choose a different output path.", title="RF VFX: Export")
            return {"CANCELLED"}

        tmpdir = tempfile.mkdtemp(prefix="rfvfx_export_")
        gltf_path = os.path.join(tmpdir, "scene.gltf")
        tmp_vfx = os.path.join(tmpdir, "trueexport_tmp.vfx")

        header = "RF VFX Export\n"
        header += f"Output: {out_vfx_raw}\n"
        if out_vfx != out_vfx_raw:
            header += f"Output (resolved): {out_vfx}\n"
        if out_note:
            header += f"Note: {out_note}\n"
        header += f"Mode: {mode}\n"
        header += f"Template (used): {tmpl_vfx if has_template else '(none)'}\n"
        header += f"Preserve Template TRS: {bool(s.export_preserve_template_trs)}\n"
        header += f"Template v4.6 TRS patch enabled: {tmpl_ok}\n"
        if tmpl_end is not None and tmpl_end > 0 and bool(s.export_use_template_frame_range):
            header += f"Template end_frame: {tmpl_end} (will match export range)\n"
        header += f"Morph FPS: {s.morph_fps}\n"
        header += f"Temp: {tmpdir}\n\n"

        try:
            # Optionally force the Blender/glTF export range to match the template.
            old_fs = context.scene.frame_start
            old_fe = context.scene.frame_end
            use_fs = None
            use_fe = None
            if has_template and tmpl_end is not None and tmpl_end > 0 and bool(s.export_use_template_frame_range):
                context.scene.frame_start = 0
                context.scene.frame_end = int(tmpl_end)
                use_fs = 0
                use_fe = int(tmpl_end)

            try:
                # Sync PropertyGroup values → custom properties for glTF export
                _scene_double_sided = bool(s.double_sided)
                # Declared here so the finally block can safely iterate even if
                # we bail out before the rename loop runs.
                _mat_renames = {}
                for _sobj in _export_objs(context.scene):
                    try:
                        if hasattr(_sobj, "rfvfx_props"):
                            if _scene_double_sided and _sobj.type == "MESH":
                                _sobj.rfvfx_props.double_sided = True
                            _sync_obj_to_idprops(_sobj)
                    except Exception:
                        pass
                    try:
                        if hasattr(_sobj, "rfvfx_particle") and _sobj.rfvfx_particle.is_emitter:
                            _sync_particle_to_idprops(_sobj)
                    except Exception:
                        pass
                    if _sobj.type == "MESH":
                        for _sl in (_sobj.material_slots or []):
                            _mat = _sl.material
                            if _mat and hasattr(_mat, "rfvfx_props"):
                                _sync_mat_to_idprops(_mat)

                # Temporarily rename materials to their explicit RF texture name.
                # vfx2obj reads the glTF material name as a reliable fallback for
                # tex0 resolution — more reliable than custom property export which
                # varies by Blender version. Restored in the finally block.
                for _sobj in _export_objs(context.scene):
                    if _sobj.type != "MESH":
                        continue
                    for _sl in (_sobj.material_slots or []):
                        _mat = _sl.material
                        if not _mat:
                            continue
                        _p = getattr(_mat, "rfvfx_props", None)
                        if _p and _p.texture_name.strip():
                            _tex = _p.texture_name.strip()
                            if _mat.name != _tex and _mat not in _mat_renames:
                                _mat_renames[_mat] = _mat.name
                                _mat.name = _tex

                if mode == "PATCH":
                    # Patch mode: strip normals/UVs to preserve vertex count
                    _gltf_export(
                        filepath=gltf_path,
                        use_selection=False,
                        export_apply=True,  # always apply transforms
                        frame_start=use_fs,
                        frame_end=use_fe,
                        force_sampling=bool(use_fs is not None or use_fe is not None),
                        full_mode=False,
                    )
                else:
                    # True Export: include normals, UVs, materials
                    _gltf_export(
                        filepath=gltf_path,
                        use_selection=False,
                        export_apply=True,  # always apply transforms
                        frame_start=use_fs,
                        frame_end=use_fe,
                        force_sampling=bool(use_fs is not None or use_fe is not None),
                        full_mode=True,
                    )
            finally:
                context.scene.frame_start = old_fs
                context.scene.frame_end = old_fe
                # Restore material names
                for _mat, _orig in _mat_renames.items():
                    try:
                        _mat.name = _orig
                    except Exception:
                        pass

            # Write baked per-frame vertex data sidecar for meshes with shape keys.
            # This bypasses glTF morph targets which can lose animation data.
            baked_sidecar_path = os.path.join(tmpdir, "baked_frames.json")
            _wrote_baked_sidecar = False
            try:
                col = None
                for c in bpy.data.collections:
                    if c.name == "RF_VFX":
                        col = c
                        break
                if col:
                    baked_data = {}
                    for obj in col.objects:
                        if obj.type != "MESH":
                            continue
                        me = obj.data
                        if not me.shape_keys or len(me.shape_keys.key_blocks) < 2:
                            continue
                        # Read per-frame positions from shape keys
                        keys = me.shape_keys.key_blocks
                        nv = len(me.vertices)
                        frames = []
                        # Frame 0 = Basis
                        basis_co = [0.0] * (nv * 3)
                        keys[0].data.foreach_get("co", basis_co)
                        frames.append([[basis_co[i*3], basis_co[i*3+1], basis_co[i*3+2]] for i in range(nv)])
                        # Frame 1+ = each subsequent shape key (absolute positions)
                        for ki in range(1, len(keys)):
                            sk_co = [0.0] * (nv * 3)
                            keys[ki].data.foreach_get("co", sk_co)
                            frames.append([[sk_co[i*3], sk_co[i*3+1], sk_co[i*3+2]] for i in range(nv)])
                        baked_data[obj.name] = {"frames": frames, "num_frames": len(frames)}
                    if baked_data:
                        # Store morph_fps in the sidecar header so vfx2obj can
                        # compute end_frame directly from frame count rather than
                        # inferring it from glTF animation times (which is unreliable
                        # when step > 1 shortens the frame range).
                        morph_fps_int = int(getattr(s, "morph_fps", 15))
                        max_nf = max(v["num_frames"] for v in baked_data.values())
                        # end_frame is in RF's 15fps base unit
                        baked_end_frame_15fps = int(round(float(max_nf - 1) / float(morph_fps_int) * 15.0))
                        sidecar_out = {
                            "_morph_fps": morph_fps_int,
                            "_num_frames": max_nf,
                            "_end_frame_15fps": baked_end_frame_15fps,
                        }
                        sidecar_out.update(baked_data)
                        with open(baked_sidecar_path, "w") as bf:
                            import json as _json
                            _json.dump(sidecar_out, bf)
                        _wrote_baked_sidecar = True
                        header += f"Baked sidecar: {len(baked_data)} mesh(es), {max_nf} frames, end_frame(15fps)={baked_end_frame_15fps}\n"
            except Exception as e:
                header += f"Baked sidecar write failed: {e}\n"

            # The new RF↔Blender conversion has det=-1, so before handing the
            # glTF to vfx2obj for RF export we need to flip winding so the RF
            # output has the correct face orientation in-game.
            try:
                wfix = _flip_gltf_winding_in_place(gltf_path)
                header += wfix + "\n"
            except Exception as e:
                header += f"flip_winding (export) failed: {e}\n"

            has_rf_meta = _gltf_has_rf_keyframed_meta(gltf_path)
            header += f"glTF has rf_vfx keyframed meta: {bool(has_rf_meta)}\n"

            rc1 = 0
            out1 = ""
            out2 = ""

            if mode == "PATCH":
                # ── PATCH MODE ──
                args = [
                    "--patch-vfx-only",
                    "--gltf-in", gltf_path,
                    "--vfx-out", out_vfx,
                    "--gltf-scale", "1.0",
                    tmpl_vfx,
                ]
                header += "Run template patch export:\n  vfx2obj.py " + " ".join(args) + "\n"
                rc1, out1 = _run_vendor("vfx2obj.py", args, cwd=_vendor_dir())

            else:
                # ── TRUE EXPORT MODE ──
                args = [
                    "--new-vfx-from-gltf", gltf_path,
                    "--vfx-out", tmp_vfx,
                    "--gltf-scale", "1.0",
                    "--morph-fps", str(s.morph_fps),
                ]
                if _wrote_baked_sidecar:
                    args += ["--baked-frames", baked_sidecar_path]
                if has_template:
                    args += ["--template-vfx", tmpl_vfx]
                anchor = s.export_anchor.strip()
                if anchor:
                    args += ["--anchor", anchor]
                header += "Run true export:\n  vfx2obj.py " + " ".join(args) + "\n"
                rc1, out1 = _run_vendor("vfx2obj.py", args, cwd=_vendor_dir())

            if rc1 != 0:
                _write_log(header, out1)
                _popup("Export failed. Open Text Editor → RFVFX_Log.", title="RF VFX: Export")
                if not s.keep_temp:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                return {"CANCELLED"}

            if mode == "PATCH":
                # Patch already wrote to out_vfx
                pass
            else:
                # True export wrote to tmp_vfx; optionally apply TRS patch, then copy to out_vfx
                if tmpl_ok and (not has_rf_meta):
                    args2 = ["--template", tmpl_vfx, "--in", tmp_vfx, "--out", out_vfx]
                    header += "\nRun template TRS patch:\n  pivot_patch_xkey0.py " + " ".join(args2) + "\n"
                    rc2, out2 = _run_vendor("pivot_patch_xkey0.py", args2, cwd=_vendor_dir())
                    if rc2 != 0:
                        _write_log(header, out1 + "\n\n" + out2)
                        _popup("Pivot fix failed. Open Text Editor → RFVFX_Log.", title="RF VFX: Export")
                        if not s.keep_temp:
                            shutil.rmtree(tmpdir, ignore_errors=True)
                        return {"CANCELLED"}
                else:
                    shutil.copyfile(tmp_vfx, out_vfx)

            _write_log(header, out1 + ("\n\n" + out2 if out2 else ""))

            if not s.keep_temp:
                shutil.rmtree(tmpdir, ignore_errors=True)

            sz = ""
            try:
                sz = f" [{os.path.getsize(out_vfx):,}B]"
            except Exception:
                pass
            self.report({"INFO"}, f"Exported VFX ({mode}){sz}: {os.path.basename(out_vfx)}")
            return {"FINISHED"}

        except BaseException:
            tb = traceback.format_exc()
            _write_log(header, "ERROR:\n" + tb)
            _popup("Export failed hard. Open Text Editor → RFVFX_Log.", title="RF VFX: Export")
            if not s.keep_temp:
                shutil.rmtree(tmpdir, ignore_errors=True)
            return {"CANCELLED"}

class RFVFX_OT_ValidateFolder(bpy.types.Operator):
    bl_idname = "rfvfx.validate_folder"
    bl_label = "Validate VFX Folder"
    bl_description = "Batch-validate .vfx files in a folder (parse, counts, face_vertex sanity, section tags). Writes JSON/CSV/MD report."

    def execute(self, context):
        s = context.scene.rfvfx

        in_dir = (s.validate_input_dir or "").strip()
        if not in_dir:
            src = (s.last_import_vfx or s.import_vfx or "").strip()
            if src:
                in_dir = os.path.dirname(bpy.path.abspath(src))

        if not in_dir:
            self.report({"ERROR"}, "No input folder set (pick a folder or import a VFX first).")
            return {"CANCELLED"}

        in_dir = bpy.path.abspath(in_dir)
        if not os.path.isdir(in_dir):
            self.report({"ERROR"}, f"Not a folder: {in_dir}")
            return {"CANCELLED"}

        out_dir = (s.validate_out_dir or "").strip()
        if out_dir:
            out_dir = bpy.path.abspath(out_dir)
        else:
            out_dir = os.path.join(in_dir, "vfx_validate_out")

        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            self.report({"ERROR"}, f"Cannot create output folder: {out_dir} ({e})")
            return {"CANCELLED"}

        try:
            from .vendor import vfx2obj as vfxmod
        except Exception as e:
            self.report({"ERROR"}, f"Failed to import vendor.vfx2obj: {e}")
            return {"CANCELLED"}

        def sha256_file(path: str) -> str:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            return h.hexdigest().upper()

        def scan_texture_names(path: str):
            with open(path, "rb") as fh:
                b = fh.read()
            hits = sorted(set(m.group(0).decode("ascii", "ignore")
                              for m in re.finditer(rb"[A-Za-z0-9_\-./]{1,120}\.(?:tga|png|jpg|jpeg)", b, re.I)))
            return hits

        def scan_section_tags(path: str):
            tags = []
            with open(path, "rb") as fh:
                data = fh.read()
            # VFX files have a 128-byte header (VSFX + version + padding) before sections start
            off = 128 if (len(data) >= 128 and data[:4] == b"VSFX") else 0
            n = len(data)
            while off + 8 <= n:
                tag = data[off:off+4]
                size = struct.unpack_from("<I", data, off+4)[0]
                if size < 4 or off + 4 + size > n:
                    break
                try:
                    t = tag.decode("ascii", "replace")
                except Exception:
                    t = repr(tag)
                tags.append(t)
                off += 4 + size
            return tags

        vfx_files = []
        for root, _, fnames in os.walk(in_dir):
            for fn in fnames:
                if fn.lower().endswith(".vfx"):
                    vfx_files.append(os.path.join(root, fn))
        vfx_files.sort()

        report = {
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "input_dir": in_dir,
            "out_dir": out_dir,
            "file_count": len(vfx_files),
            "files": [],
            "summary": {"files_with_errors": 0, "files_with_warnings": 0},
        }

        csv_rows = []
        for fp in vfx_files:
            item = {
                "path": fp,
                "sha256": "",
                "size_bytes": 0,
                "textures": [],
                "section_tags": [],
                "header": {},
                "meshes": [],
                "dummies": [],
                "materials": [],
                "errors": [],
                "warnings": [],
            }
            try:
                st = os.stat(fp)
                item["size_bytes"] = st.st_size
                item["sha256"] = sha256_file(fp)
                item["textures"] = scan_texture_names(fp)
                item["section_tags"] = scan_section_tags(fp)

                hdr, mats, meshes, dummies, parts = vfxmod.parse_vfx(fp)

                for k in ("version","end_frame","num_meshes","num_dummies","num_materials","num_faces","num_vertices",
                          "num_face_vertices","num_vertex_normals","num_adjacent_faces"):
                    if hasattr(hdr, k):
                        item["header"][k] = getattr(hdr, k)

                for mi, m in enumerate(mats or []):
                    item["materials"].append({"idx": mi, "tex0": getattr(m, "tex0", None)})

                for m in meshes or []:
                    me = {
                        "name": getattr(m, "name", None),
                        "parent_name": getattr(m, "parent_name", None),
                        "num_vertices": getattr(m, "num_vertices", None),
                        "num_faces": getattr(m, "num_faces", None),
                        "num_face_vertices": getattr(m, "num_face_vertices", None),
                        "num_adjacent_faces": getattr(m, "num_adjacent_faces", None),
                        "flags": getattr(m, "flags", None),
                        "save_parent": getattr(m, "save_parent", None),
                        "is_keyframed": bool(getattr(m, "is_keyframed", False)),
                        "morph": bool(getattr(m, "morph", False)),
                        "fps": getattr(m, "fps", None),
                        "num_frames": getattr(m, "num_frames", None),
                    }
                    item["meshes"].append(me)

                    name = me["name"] or "<unnamed>"
                    v = me.get("num_vertices")
                    f = me.get("num_faces")
                    fv = me.get("num_face_vertices")
                    if isinstance(v, int) and isinstance(f, int):
                        if v <= 0 or f <= 0:
                            item["errors"].append(f"{name}: invalid counts verts={v} faces={f}")
                        if isinstance(fv, int) and f > 0:
                            corners = 3 * f
                            if fv >= int(corners * 0.95):
                                item["warnings"].append(f"{name}: face_vertex count ~ per-corner (fv={fv}, 3*faces={corners}) -> likely exploded table")
                            if fv > corners:
                                item["warnings"].append(f"{name}: face_vertex count exceeds 3*faces (fv={fv}, 3*faces={corners}) -> suspicious")
                    else:
                        item["warnings"].append(f"{name}: missing mesh counts (parser)")

                    if me.get("save_parent") == 1:
                        item["warnings"].append(f"{name}: save_parent=1 (many stock assets use 0)")

                for d in dummies or []:
                    item["dummies"].append({
                        "name": getattr(d, "name", None),
                        "parent_name": getattr(d, "parent_name", None),
                        "num_frames": getattr(d, "num_frames", None),
                    })

                # Particle systems
                if not hasattr(item, "particles"):
                    item["particles"] = []
                for ps in parts or []:
                    item.setdefault("particles", []).append({
                        "name": getattr(ps, "name", None),
                        "parent_name": getattr(ps, "parent_name", None),
                        "num_particles": getattr(ps, "num_particles", None),
                        "num_frames": getattr(ps, "num_frames", None),
                    })

                for t in item["textures"]:
                    tl = t.lower()
                    if tl.endswith(".png") or tl.endswith(".jpg") or tl.endswith(".jpeg"):
                        item["warnings"].append(f"Texture name string '{t}' is non-TGA (VFX stores names only)")

                if item["section_tags"] and item["section_tags"][0] != "VERS":
                    item["warnings"].append(f"First section is '{item['section_tags'][0]}', expected 'VERS'")

            except Exception as e:
                item["errors"].append(f"Parse failed: {type(e).__name__}: {e}")
                item["errors"].append(traceback.format_exc(limit=6))

            if item["errors"]:
                report["summary"]["files_with_errors"] += 1
            if item["warnings"]:
                report["summary"]["files_with_warnings"] += 1

            report["files"].append(item)

            csv_rows.append({
                "path": fp,
                "sha256": item["sha256"],
                "size_bytes": item["size_bytes"],
                "mesh_counts": ";".join([f"{m.get('name')}:{m.get('num_vertices')}v/{m.get('num_faces')}f" for m in item["meshes"]]),
                "errors": " | ".join(item["errors"][:1]),
                "warnings": " | ".join(item["warnings"][:1]),
            })

        json_path = os.path.join(out_dir, "vfx_validate_report.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)

        csv_path = os.path.join(out_dir, "vfx_validate_report.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()) if csv_rows else ["path"])
            w.writeheader()
            for r in csv_rows:
                w.writerow(r)

        md_path = os.path.join(out_dir, "vfx_validate_summary.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("# VFX Batch Validation Summary\n\n")
            f.write(f"- Generated: {report['generated_at']}\n")
            f.write(f"- Input: `{in_dir}`\n")
            f.write(f"- Files: **{report['file_count']}**\n")
            f.write(f"- Files with errors: **{report['summary']['files_with_errors']}**\n")
            f.write(f"- Files with warnings: **{report['summary']['files_with_warnings']}**\n\n")

            f.write("## First 25 problem files\n\n")
            count = 0
            for it in report["files"]:
                if it["errors"] or it["warnings"]:
                    count += 1
                    f.write(f"### {it['path']}\n\n")
                    for e in it["errors"][:4]:
                        f.write(f"- ERROR: {e}\n")
                    for wmsg in it["warnings"][:4]:
                        f.write(f"- WARN: {wmsg}\n")
                    f.write("\n")
                if count >= 25:
                    break

        msg = f"Validated {len(vfx_files)} file(s). Errors in {report['summary']['files_with_errors']}, warnings in {report['summary']['files_with_warnings']}. Report: {out_dir}"
        if report["summary"]["files_with_errors"] > 0 and s.validate_strict:
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}
        self.report({"INFO"}, msg)
        return {"FINISHED"}

class RFVFX_PT_Panel(bpy.types.Panel):
    bl_label = "RF VFX"
    bl_idname = "RFVFX_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "RF VFX"

    def draw(self, context):
        pass  # sub-panels handle all content

_classes = (
    RFVFX_Settings,
    RFVFX_OT_ImportVFX,
    RFVFX_OT_ExportVFX,
    RFVFX_OT_ValidateFolder,
    RFVFX_PT_Panel,
)


# === RFVFX_AUTHORING_WIZARD_V053 ===
# Adds: New Scene Wizard + RF flags UI + readiness checks + authoring glTF export (extras-friendly)

# Reuse _log_textblock / _write_log from above for consistency
def _rfvfx_log(msg: str):
    txt = _log_textblock()
    txt.write(msg.rstrip() + "\n")

def _get_export_collection(scene):
    # prefer explicit scene custom prop, else default "RF_VFX"
    cname = None
    try:
        cname = scene.get("rfvfx_export_collection", None)
    except Exception:
        cname = None
    if not cname:
        cname = "RF_VFX"
    return bpy.data.collections.get(cname)

def _set_viewport_rf_orientation(context):
    """Set the 3D viewport to Front Orthographic view (RF editor orientation).
    
    RF axes:     X = left/right, Y = up,  Z = forward/back
    Blender:     X = left/right, Z = up,  Y = forward/back
    Front view shows Blender X + Z → matches RF X + Y.
    
    Directly sets the view quaternion for reliability (operators can fail
    silently depending on context).
    """
    from mathutils import Quaternion as _Quat
    # Front view = 90° rotation around X axis
    front_quat = _Quat((0.7071068, 0.7071068, 0.0, 0.0))
    try:
        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                for space in area.spaces:
                    if space.type == "VIEW_3D":
                        r3d = space.region_3d
                        if r3d:
                            r3d.view_rotation = front_quat
                            r3d.view_perspective = "ORTHO"
                            # Reasonable clip range for VFX-scale objects
                            space.clip_start = 0.01
                            space.clip_end = 1000.0
                        break
                break
    except Exception:
        pass

def _ensure_collection(name: str):
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(col)
    return col

def _ensure_root_dummy(col, name="RFVFX_ROOT"):
    obj = bpy.data.objects.get(name)
    if obj is None:
        obj = bpy.data.objects.new(name, None)
        obj.empty_display_type = "ARROWS"
        obj.empty_display_size = 0.4
    if obj.name not in col.objects:
        # link to collection (and scene if needed)
        col.objects.link(obj)
    try:
        obj["rfvfx_is_dummy"] = 1
    except Exception:
        pass
    return obj

def _on_facing_update(self, context):
    """When Billboard is toggled on, auto-set fullbright and disable Rod."""
    if self.facing:
        self.facing_rod = False
        self.fullbright = True

def _on_facing_rod_update(self, context):
    """When Rod Billboard is toggled on, auto-set save_parent+fullbright, disable Billboard."""
    if self.facing_rod:
        self.facing = False
        self.save_parent = True
        self.fullbright = True

class RFVFX_ObjectProps(bpy.types.PropertyGroup):
    export: BoolProperty(name="Export", default=True, description="Include this object in VFX export")
    morph: BoolProperty(name="morph", default=False, description="Shape key animation (auto-set from shape keys, rarely need to set manually)")
    facing: BoolProperty(name="facing", default=False, description="Billboard: mesh always faces the camera (for sparks, flares)",
        update=_on_facing_update)
    facing_rod: BoolProperty(name="facing-rod", default=False, description="Rod billboard: faces camera but locked along the mesh's longest axis (for beams, tracers, streaks)",
        update=_on_facing_rod_update)
    dump_uvs: BoolProperty(name="dump-UVs", default=False, description="Export UV data for every animation frame (rare, for scrolling textures)")
    rotate_placement: BoolProperty(name="rotate-placement", default=False, description="Allow random rotation when placed in editor")
    save_parent: BoolProperty(name="save-parent", default=False, description="Parent transform persists after VFX ends (for permanent props like pickups)")
    fullbright: BoolProperty(name="Fullbright", default=False, description="Ignore scene lighting, render at full brightness")
    seethrough: BoolProperty(name="Seethrough", default=False, description="Render with alpha transparency")
    no_interp: BoolProperty(name="no-interp", default=False, description="Disable frame interpolation (hard frame transitions)")
    double_sided: BoolProperty(name="Double Sided", default=False, description="Duplicate faces with flipped normals so both sides render in-game")
    width: FloatProperty(name="width", default=1.0, min=0.0, description="Billboard width (only for facing/facing-rod meshes)")
    glow: StringProperty(name="glow", default="", description="Glow effect name (engine-specific)")
    custom_name: StringProperty(name="VFX Name", default="",
        description="Override exported mesh name. Leave blank to use the Blender object name")

class RFVFX_MaterialProps(bpy.types.PropertyGroup):
    # RF stores *names*, not file bytes; we enforce name strings here.
    texture_name: StringProperty(name="Texture Name (.tga/.vbm/.dds)", default="",
        description="RF texture filename (e.g., 'spark.tga'). Must match a file in the game's texture paths")
    additive: BoolProperty(name="Additive Blending", default=False,
        description="Use additive blending (glow/fire effects). Bright pixels add to the scene instead of replacing")

class RFVFX_ParticleProps(bpy.types.PropertyGroup):
    """Properties for a VFX particle emitter (stored on an Empty object)."""
    is_emitter: BoolProperty(name="Is VFX Particle Emitter", default=False,
        description="Mark this Empty as a VFX particle emitter")
    # --- Appearance ---
    particle_type: EnumProperty(
        name="Type",
        items=[("FACING", "Facing", "Camera-facing textured billboards (explosions, smoke, fire)"),
               ("DROPS", "Drops", "Untextured triangular shapes (sparks, water, debris)")],
        default="FACING",
        description="Particle rendering type")
    texture_name: StringProperty(name="Particle Bitmap", default="",
        description="RF texture filename for Facing particles (e.g. Fire01.tga). Leave blank for Drops")
    tail_distance: FloatProperty(name="Tail Distance", default=0.3, min=0.0, max=5.0, step=1, precision=2,
        description="Length of each drop particle (Drops type only)")
    additive: BoolProperty(name="Glow", default=False,
        description="Glow blending mode (bright/glowing effects). Disable for solid particles like smoke")
    fade: BoolProperty(name="Fade", default=True,
        description="Particles fade out as they die")
    randomize_orient: BoolProperty(name="Randomize", default=False,
        description="Randomly orient each particle bitmap to prevent strobing (Facing only)")
    no_cull: BoolProperty(name="No Cull", default=False,
        description="Force particle rendering even when emitter is offscreen (expensive)")
    # --- Emission ---
    particle_size: FloatProperty(name="Radius", default=0.50, min=0.01, max=10.0, step=1, precision=3,
        description="Base particle radius in meters")
    size_variation: FloatProperty(name="+/-", default=0.1, min=0.0, max=5.0, step=1, precision=3,
        description="Random size variation (+/- meters)")
    spawn_delay: FloatProperty(name="Spawn Delay", default=0.05, min=0.001, max=5.0, step=1, precision=3,
        description="Seconds between particle spawns (lower = faster)")
    spawn_delay_variation: FloatProperty(name="+/-", default=0.01, min=0.0, max=2.5, step=1, precision=3,
        description="Random variation in spawn delay (seconds)")
    speed: FloatProperty(name="Velocity", default=1.0, min=0.0, max=50.0, step=1, precision=2,
        description="Initial particle velocity (meters/second)")
    speed_variation: FloatProperty(name="+/-", default=0.5, min=0.0, max=25.0, step=1, precision=2,
        description="Random velocity variation (+/- meters/second)")
    decay: FloatProperty(name="Decay", default=5.0, min=0.1, max=60.0, step=10, precision=2,
        description="How long each particle lives (seconds)")
    decay_variation: FloatProperty(name="+/-", default=1.0, min=0.0, max=30.0, step=10, precision=2,
        description="Random variation in decay time (seconds)")
    fps: IntProperty(name="FPS", default=15, min=1, max=60,
        description="(Deprecated — use Morph FPS in Export > Advanced Options instead.) "
                    "Kept for round-trip compatibility with older saved files.")
    # --- Physics ---
    apply_gravity: BoolProperty(name="Gravity", default=False,
        description="Apply gravity to emitted particles (on/off only — gravity strength is not stored in the PART binary)")
    gravity_strength: FloatProperty(name="Strength", default=1.0, min=0.0, max=10.0, step=1, precision=2,
        description="NOTE: gravity strength has no confirmed slot in the RF PART binary. The engine uses global gravity * the on/off toggle only. This value is stored but not exported.")
    # --- Emitter Shape ---
    emitter_radius: FloatProperty(name="Emitter Radius", default=0.0, min=0.0, max=10.0, step=1, precision=3,
        description="Sphere radius for spawn position randomization (0 = point emitter)")
    random_direction: FloatProperty(name="Random Direction", default=30.0, min=0.0, max=360.0, step=100, precision=1,
        description="Random spread angle in degrees (0 = all same direction, 360 = omnidirectional)")
    # --- Fade ---
    size_at_birth: FloatProperty(name="Size at Birth", default=0.0, min=0.0, max=1.0, step=1, precision=2,
        description="Fraction of lifetime during which particle grows from zero to full size. 0=instant, 0.2=grows during first 20%. Confirmed against Volition shipexp/TorpedoHit VFX files.")
    size_at_death: FloatProperty(name="Size at Death", default=-0.2, min=-1.0, max=1.0, step=1, precision=2,
        description="Particle size change at death. Negative = shrink to nothing. Written to header float_01 (verified against SonarAttack.vfx).")
    fade_at_birth: FloatProperty(name="Fade at Birth", default=0.0, min=0.0, max=1.0, step=1, precision=2,
        description="NOTE: fade_at_birth slot is unconfirmed in the RF PART binary (no reference file with non-zero value found yet). Stored but may not export correctly.")
    fade_at_death: FloatProperty(name="Fade at Death", default=0.6, min=0.0, max=1.0, step=1, precision=2,
        description="Fraction of lifetime to fade opacity. Written to header float_02 (verified against SonarAttack.vfx).")
    # --- Internal ---
    raw_body_b64: StringProperty(name="", default="",
        description="(Internal) base64 raw PART body for round-trip")

class RFVFX_OT_CreateParticleEmitter(bpy.types.Operator):
    bl_idname = "rfvfx.create_particle_emitter"
    bl_label = "Create Particle Emitter"
    bl_description = "Create a new VFX particle emitter Empty with default properties"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        empty = bpy.data.objects.new("VFX_Particles", None)
        empty.empty_display_type = "SINGLE_ARROW"
        empty.empty_display_size = 0.3
        empty.show_name = True

        # Add to active collection (or RF_VFX if it exists)
        coll = None
        for c in bpy.data.collections:
            if c.name == "RF_VFX":
                coll = c
                break
        if coll is None:
            coll = context.collection
        coll.objects.link(empty)

        # Place at 3D cursor so the emitter appears where the user is working
        # rather than at world origin. Matches the behaviour of _make_dummy().
        empty.location = context.scene.cursor.location.copy()

        # Set particle properties
        pp = empty.rfvfx_particle
        pp.is_emitter = True

        # Select and make active
        if context.mode == "OBJECT":
            bpy.ops.object.select_all(action="DESELECT")
        empty.select_set(True)
        context.view_layer.objects.active = empty

        self.report({"INFO"}, f"Created particle emitter '{empty.name}'")
        return {"FINISHED"}

class RFVFX_OT_CloneParticleFromImport(bpy.types.Operator):
    bl_idname = "rfvfx.clone_particle_from_import"
    bl_label = "Clone Particle from Imported"
    bl_description = "Clone a particle system from an imported VFX node (preserves baked trajectory data)"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj and obj.type == "EMPTY":
            # Check if this object has rf_vfx_particle data from import
            ex = obj.get("rf_vfx_particle")
            if isinstance(ex, dict) and ex.get("raw_body_b64"):
                return True
        return False

    def execute(self, context):
        src = context.active_object
        ex = dict(src.get("rf_vfx_particle", {}))

        # Create clone
        empty = bpy.data.objects.new(src.name + "_clone", None)
        empty.empty_display_type = "SINGLE_ARROW"
        empty.empty_display_size = 0.3
        empty.show_name = True
        empty.location = src.location.copy()
        empty.rotation_quaternion = src.rotation_quaternion.copy()

        # Link to same collection
        for c in src.users_collection:
            c.objects.link(empty)
            break
        else:
            context.collection.objects.link(empty)

        # Copy particle data
        pp = empty.rfvfx_particle
        pp.is_emitter = True
        pp.raw_body_b64 = ex.get("raw_body_b64", "")
        pp.particle_size = float(ex.get("particle_size", 0.45))
        pp.decay = float(ex.get("num_frames", 30)) / 15.0  # convert frames to seconds at 15fps
        pp.fps = int(ex.get("fps", 15))

        bpy.ops.object.select_all(action="DESELECT")
        empty.select_set(True)
        context.view_layer.objects.active = empty

        self.report({"INFO"}, f"Cloned particle emitter '{empty.name}' from '{src.name}'")
        return {"FINISHED"}


class RFVFX_OT_BrowseParticleTexture(bpy.types.Operator):
    bl_idname = "rfvfx.browse_particle_texture"
    bl_label = "Browse Texture"
    bl_description = "Pick a texture file (.tga/.vbm/.dds) from disk — only the filename is used"

    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.tga;*.vbm;*.dds", options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and hasattr(obj, "rfvfx_particle") and obj.rfvfx_particle.is_emitter

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        import os
        name = os.path.basename(self.filepath)
        obj = context.active_object
        obj.rfvfx_particle.texture_name = name
        self.report({"INFO"}, f"Texture set to '{name}'")
        return {"FINISHED"}


class RFVFX_OT_BrowseMaterialTexture(bpy.types.Operator):
    bl_idname = "rfvfx.browse_material_texture"
    bl_label = "Browse Texture"
    bl_description = "Pick a texture file (.tga/.vbm/.dds) from disk — only the filename is used"

    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.tga;*.vbm;*.dds", options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == "MESH" and obj.active_material and hasattr(obj.active_material, "rfvfx_props")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        import os
        name = os.path.basename(self.filepath)
        context.active_object.active_material.rfvfx_props.texture_name = name
        self.report({"INFO"}, f"Texture set to '{name}'")
        return {"FINISHED"}

def _sync_obj_to_idprops(obj):
    p = getattr(obj, "rfvfx_props", None)
    if not p:
        return
    # IDProps (exporter can put these in glTF extras)
    obj["rfvfx_export"] = 1 if p.export else 0
    obj["rfvfx_morph"] = 1 if p.morph else 0
    obj["rfvfx_facing"] = 1 if p.facing else 0
    obj["rfvfx_facing_rod"] = 1 if p.facing_rod else 0
    obj["rfvfx_dump_uvs"] = 1 if p.dump_uvs else 0
    obj["rfvfx_save_parent"] = 1 if p.save_parent else 0
    obj["rfvfx_fullbright"] = 1 if p.fullbright else 0
    obj["rfvfx_double_sided"] = 1 if p.double_sided else 0
    obj["rfvfx_width"] = float(p.width)
    obj["rfvfx_custom_name"] = str(p.custom_name or "")

def _sync_mat_to_idprops(mat):
    p = getattr(mat, "rfvfx_props", None)
    if not p:
        return
    mat["rfvfx_texture_name"] = str(p.texture_name or "")
    mat["rfvfx_additive"] = 1 if p.additive else 0

def _sync_particle_to_idprops(obj):
    """Sync particle emitter properties to custom properties for glTF export."""
    pp = getattr(obj, "rfvfx_particle", None)
    if not pp or not pp.is_emitter:
        return
    # For the glTF exporter to pick these up, we use plain custom properties
    # that become glTF node extras
    obj["rfvfx_particle_emitter"] = 1
    obj["rfvfx_pe_type"] = pp.particle_type
    # Flags field encodes particle BEHAVIOR, not FACING/DROPS type.
    # FACING vs DROPS is determined by whether a particle MATL texture section exists.
    # Verified against 38 Volition VFX files:
    #   0x04 = default/ambient mode (SonarAttack)
    #   0x10 = Fast Particles / PS2 optimization (most explosion effects)
    #   0x02 = Randomize Orientation (confirmed SpitAttack VP04)
    #   0x20 = No Cull (confirmed Cutscene08 large explosions)
    # We use 0x04 for FACING and 0x10 for DROPS to match Volition convention,
    # but the game engine uses MATL pairing (not this flag) to determine render type.
    _flags = 0x04 if pp.particle_type == "FACING" else 0x10
    if pp.randomize_orient: _flags |= 0x02   # confirmed: bit1 = Randomize Orientation
    if pp.no_cull: _flags |= 0x20            # confirmed: bit5 = No Cull
    obj["rfvfx_pe_flags"] = _flags
    obj["rfvfx_pe_size"] = float(pp.particle_size)
    obj["rfvfx_pe_spawn_delay"] = float(pp.spawn_delay)
    obj["rfvfx_pe_spawn_delay_var"] = float(pp.spawn_delay_variation)
    obj["rfvfx_pe_speed"] = float(pp.speed)
    obj["rfvfx_pe_decay"] = float(pp.decay)
    obj["rfvfx_pe_decay_var"] = float(pp.decay_variation)
    obj["rfvfx_pe_fps"] = int(pp.fps)
    obj["rfvfx_pe_gravity"] = 1 if pp.apply_gravity else 0
    obj["rfvfx_pe_size_birth"] = float(pp.size_at_birth)
    obj["rfvfx_pe_size_death"] = float(pp.size_at_death)
    obj["rfvfx_pe_fade"] = float(pp.fade_at_death)
    obj["rfvfx_pe_save_parent"] = 0
    obj["rfvfx_pe_texture"] = str(pp.texture_name or "")
    obj["rfvfx_pe_additive"] = 1 if pp.additive else 0
    obj["rfvfx_pe_fade_flag"] = 1 if pp.fade else 0
    obj["rfvfx_pe_rand_orient"] = 1 if pp.randomize_orient else 0
    obj["rfvfx_pe_no_cull"] = 1 if pp.no_cull else 0
    obj["rfvfx_pe_tail_dist"] = float(pp.tail_distance)
    obj["rfvfx_pe_fade_birth"] = float(pp.fade_at_birth)
    obj["rfvfx_pe_size_var"] = float(pp.size_variation)
    obj["rfvfx_pe_speed_var"] = float(pp.speed_variation)
    obj["rfvfx_pe_grav_str"] = float(pp.gravity_strength)
    obj["rfvfx_pe_emit_rad"] = float(pp.emitter_radius)
    obj["rfvfx_pe_rand_dir"] = float(pp.random_direction)
    if pp.raw_body_b64:
        obj["rfvfx_pe_raw_b64"] = pp.raw_body_b64

def _post_import_setup_particles():
    """After glTF import, find particle nodes and set up their display + properties."""
    count = 0
    for obj in bpy.context.scene.objects:
        if obj.type != "EMPTY":
            continue
        # Check for round-trip particle data (from VFX→glTF import)
        ex = obj.get("rf_vfx_particle")
        if isinstance(ex, (dict, bpy.types.bpy_prop_collection)):
            try:
                ps_dict = dict(ex)
            except Exception:
                continue
            if not ps_dict.get("rf_vfx_particle"):
                continue
            # Visual setup
            obj.empty_display_type = "SINGLE_ARROW"
            obj.empty_display_size = 0.25
            obj.show_name = True
            # Set rfvfx_particle props if authoring system is registered
            pp = getattr(obj, "rfvfx_particle", None)
            if pp:
                pp.is_emitter = True
                pp.raw_body_b64 = str(ps_dict.get("raw_body_b64", ""))
                pp.decay = float(ps_dict.get("num_frames", 30)) / 15.0
                pp.fps = int(ps_dict.get("fps", 15))
                pp.particle_size = 0.45  # default, real values are in the baked data
            count += 1
        # Also check for authored emitter data (glTF round-trip)
        elif obj.get("rfvfx_particle_emitter") == 1:
            obj.empty_display_type = "SINGLE_ARROW"
            obj.empty_display_size = 0.25
            obj.show_name = True
            pp = getattr(obj, "rfvfx_particle", None)
            if pp:
                pp.is_emitter = True
                pp.particle_type = str(obj.get("rfvfx_pe_type", "FACING"))
                pp.particle_size = float(obj.get("rfvfx_pe_size", 0.45))
                pp.spawn_delay = float(obj.get("rfvfx_pe_spawn_delay", 0.05))
                pp.spawn_delay_variation = float(obj.get("rfvfx_pe_spawn_delay_var", 0.01))
                pp.speed = float(obj.get("rfvfx_pe_speed", 0.42))
                pp.decay = float(obj.get("rfvfx_pe_decay", 5.0))
                pp.decay_variation = float(obj.get("rfvfx_pe_decay_var", 1.0))
                pp.fps = int(obj.get("rfvfx_pe_fps", 15))
                pp.apply_gravity = bool(obj.get("rfvfx_pe_gravity", 0))
                pp.size_at_birth = float(obj.get("rfvfx_pe_size_birth", 0.0))
                pp.size_at_death = float(obj.get("rfvfx_pe_size_death", -0.2))

                pp.randomize_orient = bool(obj.get("rfvfx_pe_rand_orient", 0))
                pp.no_cull = bool(obj.get("rfvfx_pe_no_cull", 0))
                pp.tail_distance = float(obj.get("rfvfx_pe_tail_dist", 0.3))
                pp.fade_at_birth = float(obj.get("rfvfx_pe_fade_birth", 0.0))
                pp.fade_at_death = float(obj.get("rfvfx_pe_fade", 0.6))
                pp.texture_name = str(obj.get("rfvfx_pe_texture", ""))
                pp.additive = bool(obj.get("rfvfx_pe_additive", 1))
                pp.size_variation = float(obj.get("rfvfx_pe_size_var", 0.1))
                pp.speed_variation = float(obj.get("rfvfx_pe_speed_var", 0.0))
                pp.gravity_strength = float(obj.get("rfvfx_pe_grav_str", 1.0))
                pp.emitter_radius = float(obj.get("rfvfx_pe_emit_rad", 0.0))
                pp.random_direction = float(obj.get("rfvfx_pe_rand_dir", 30.0))
                pp.raw_body_b64 = str(obj.get("rfvfx_pe_raw_b64", ""))
            count += 1
    return count

def _export_objs(scene):
    col = _get_export_collection(scene)
    if col:
        return [o for o in col.objects]
    # fallback: objects tagged for export
    out = []
    for o in scene.objects:
        try:
            if o.get("rfvfx_export", 0) == 1:
                out.append(o)
        except Exception:
            pass
    return out

def _has_animated_scale(obj):
    ad = getattr(obj, "animation_data", None)
    if not ad or not ad.action:
        return False
    try:
        # Blender 5.0+: fcurves may be under action.layers[].strips[].channels or action.fcurves
        fcurves = None
        if hasattr(ad.action, "fcurves"):
            fcurves = ad.action.fcurves
        elif hasattr(ad.action, "layers"):
            for layer in ad.action.layers:
                for strip in getattr(layer, "strips", []):
                    if hasattr(strip, "channels"):
                        fcurves = strip.channels
                        break
                if fcurves:
                    break
        if not fcurves:
            return False
        for fc in fcurves:
            dp = getattr(fc, "data_path", "") if hasattr(fc, "data_path") else ""
            if dp.startswith("scale"):
                kps = getattr(fc, "keyframe_points", None)
                if kps and len(kps) > 0:
                    return True
    except Exception:
        pass
    return False

def _mesh_has_uvs(obj):
    if obj.type != "MESH" or obj.data is None:
        return True
    me = obj.data
    return (me.uv_layers is not None) and (len(me.uv_layers) > 0)

def _collect_texture_names(mat):
    # priority: explicit RF texture name
    names = []
    try:
        p = getattr(mat, "rfvfx_props", None)
        if p and p.texture_name.strip():
            names.append(p.texture_name.strip())
    except Exception:
        pass

    # fallback: node image texture filenames
    try:
        if mat.node_tree:
            for n in mat.node_tree.nodes:
                if n.type == "TEX_IMAGE" and getattr(n, "image", None):
                    img = n.image
                    fp = (img.filepath or "").strip()
                    if fp:
                        names.append(os.path.basename(bpy.path.abspath(fp)))
                    else:
                        names.append(img.name)
    except Exception:
        pass

    # fallback: material ID prop (if somebody already set it)
    try:
        v = mat.get("rfvfx_texture_name", "")
        if isinstance(v, str) and v.strip():
            names.append(v.strip())
    except Exception:
        pass

    # unique stable
    out = []
    for n in names:
        if n and n not in out:
            out.append(n)
    return out

def _gltf_op_props():
    # Blender versions vary; filter kwargs by actual operator RNA props
    try:
        return set(bpy.ops.export_scene.gltf.get_rna_type().properties.keys())
    except Exception:
        return set()

class RFVFX_OT_NewAuthoringScene(bpy.types.Operator):
    bl_idname = "rfvfx.new_authoring_scene"
    bl_label = "New RF VFX Scene"
    bl_description = "Recommended first step for every new VFX. Sets up the RF_VFX collection, scene root, and viewport orientation. Run this before adding any objects."
    bl_options = {"REGISTER", "UNDO"}

    collection_name: StringProperty(name="Collection", default="RF_VFX")
    root_name: StringProperty(name="Root Dummy", default="RFVFX_ROOT")

    def execute(self, context):
        scene = context.scene
        col = _ensure_collection(self.collection_name)
        _ensure_root_dummy(col, self.root_name)

        # store defaults on scene (so readiness/export can find it)
        scene["rfvfx_export_collection"] = self.collection_name
        scene["rfvfx_root_dummy"] = self.root_name

        # ── Configure viewport to match RF editor orientation ──
        # RF: X=left/right, Y=up, Z=forward/back
        # Front view in Blender shows X horizontal + Z vertical = RF X + RF Y
        # _set_viewport_rf_orientation(context)  # disabled: don't change user's view

        # Set scene unit scale (RF uses roughly metric scale)
        try:
            scene.unit_settings.system = "METRIC"
            scene.unit_settings.scale_length = 1.0
        except Exception:
            pass

        self.report({"INFO"}, f"RF VFX scene ready. Viewport set to RF orientation (Front view: X=left/right, Z=up).")
        return {"FINISHED"}






def _make_dummy(context, name, directional=False):
    """Create a dummy empty in RF_VFX, parented to active mesh or RFVFX_ROOT."""
    empty = bpy.data.objects.new(name, None)
    empty.empty_display_type = "SINGLE_ARROW" if directional else "PLAIN_AXES"
    empty.empty_display_size = 0.2 if directional else 0.15
    empty.show_name = True
    coll = None
    for c in bpy.data.collections:
        if c.name == "RF_VFX":
            coll = c
            break
    if coll is None:
        coll = context.collection
    coll.objects.link(empty)
    # Auto-parent to active mesh if selected, otherwise RFVFX_ROOT
    parent_obj = None
    active = context.active_object
    if active and active.type == "MESH":
        parent_obj = active
    else:
        for o in coll.objects:
            if o.name == "RFVFX_ROOT":
                parent_obj = o
                break
    if parent_obj:
        empty.parent = parent_obj
    empty.location = context.scene.cursor.location.copy()
    bpy.ops.object.select_all(action="DESELECT")
    empty.select_set(True)
    context.view_layer.objects.active = empty
    return empty


class RFVFX_OT_Dummy_PropFlag(bpy.types.Operator):
    bl_idname = "rfvfx.dummy_propflag"
    bl_label = "$prop_flag"
    bl_description = "CTF flag attachment to player or base"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        e = _make_dummy(context, "$prop_flag")
        p = e.parent.name if e.parent else "Scene Root"
        self.report({"INFO"}, f"Created $prop_flag (parent: {p})")
        return {"FINISHED"}

class RFVFX_OT_Dummy_Muzzle(bpy.types.Operator):
    bl_idname = "rfvfx.dummy_muzzle"
    bl_label = "muzzle_1"
    bl_description = "Weapon muzzle flash spawn point"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        e = _make_dummy(context, "muzzle_1", directional=True)
        p = e.parent.name if e.parent else "Scene Root"
        self.report({"INFO"}, f"Created muzzle_1 (parent: {p})")
        return {"FINISHED"}

class RFVFX_OT_Dummy_Thruster(bpy.types.Operator):
    bl_idname = "rfvfx.dummy_thruster"
    bl_label = "thruster (auto)"
    bl_description = "Thruster VFX attachment — auto-increments number"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        existing = [o.name for o in bpy.data.objects if o.name.startswith("thruster_")]
        n = 1
        while f"thruster_{n}" in existing:
            n += 1
        _make_dummy(context, f"thruster_{n}", directional=True)
        return {"FINISHED"}

class RFVFX_OT_Dummy_Corona(bpy.types.Operator):
    bl_idname = "rfvfx.dummy_corona"
    bl_label = "corona (auto)"
    bl_description = "Glare/corona position — auto-increments number"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        existing = [o.name for o in bpy.data.objects if o.name.startswith("corona_")]
        n = 1
        while f"corona_{n}" in existing:
            n += 1
        _make_dummy(context, f"corona_{n}")
        return {"FINISHED"}

class RFVFX_OT_Dummy_Chaingun(bpy.types.Operator):
    bl_idname = "rfvfx.dummy_chaingun"
    bl_label = "chaingun_1"
    bl_description = "Mounted weapon position on vehicle"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        e = _make_dummy(context, "chaingun_1", directional=True)
        p = e.parent.name if e.parent else "Scene Root"
        self.report({"INFO"}, f"Created chaingun_1 (parent: {p})")
        return {"FINISHED"}

class RFVFX_OT_Dummy_Primary(bpy.types.Operator):
    bl_idname = "rfvfx.dummy_primary"
    bl_label = "primary_1"
    bl_description = "Primary weapon position"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        e = _make_dummy(context, "primary_1", directional=True)
        p = e.parent.name if e.parent else "Scene Root"
        self.report({"INFO"}, f"Created primary_1 (parent: {p})")
        return {"FINISHED"}

class RFVFX_OT_Dummy_Secondary(bpy.types.Operator):
    bl_idname = "rfvfx.dummy_secondary"
    bl_label = "secondary_1"
    bl_description = "Secondary weapon position"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        e = _make_dummy(context, "secondary_1", directional=True)
        p = e.parent.name if e.parent else "Scene Root"
        self.report({"INFO"}, f"Created secondary_1 (parent: {p})")
        return {"FINISHED"}

class RFVFX_OT_Dummy_Interface(bpy.types.Operator):
    bl_idname = "rfvfx.dummy_interface"
    bl_label = "interface_1"
    bl_description = "Player interaction/entry point"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        e = _make_dummy(context, "interface_1")
        p = e.parent.name if e.parent else "Scene Root"
        self.report({"INFO"}, f"Created interface_1 (parent: {p})")
        return {"FINISHED"}


class RFVFX_OT_AddSelectedToRF(bpy.types.Operator):
    bl_idname = "rfvfx.add_selected_to_rf"
    bl_label = "Add Selected To RF_VFX"
    bl_description = "Moves selected objects (and their children/parents) into the RF_VFX collection. Cleans up empty collections afterwards."
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        col = _get_export_collection(scene)
        if col is None:
            col = _ensure_collection("RF_VFX")
            scene["rfvfx_export_collection"] = "RF_VFX"

        sel = list(context.selected_objects or [])
        if not sel:
            self.report({"ERROR"}, "No objects selected.")
            return {"CANCELLED"}

        # Collect full set: selected + all their children recursively
        # + armature parents of any selected mesh (Mixamo imports etc.)
        def collect_with_children(obj, seen):
            if obj in seen:
                return
            seen.add(obj)
            for child in obj.children:
                collect_with_children(child, seen)

        to_move = set()
        for obj in sel:
            collect_with_children(obj, to_move)
            # If mesh has an armature parent not already selected, include it
            if obj.type == "MESH" and obj.parent and obj.parent.type == "ARMATURE":
                collect_with_children(obj.parent, to_move)
            # If armature, include mesh children
            if obj.type == "ARMATURE":
                for child in obj.children:
                    collect_with_children(child, to_move)

        moved = 0
        for obj in to_move:
            # Link into RF_VFX if not already there
            if obj.name not in col.objects:
                col.objects.link(obj)

            # Mark for export
            try:
                if hasattr(obj, "rfvfx_props") and obj.rfvfx_props is not None:
                    obj.rfvfx_props.export = True
                    _sync_obj_to_idprops(obj)
            except Exception:
                pass
            try:
                obj["rfvfx_export"] = 1
            except Exception:
                pass
            moved += 1

        # Remove objects from all other collections they were in
        # (except the master scene collection and RF_VFX itself)
        for obj in to_move:
            for other_col in list(obj.users_collection):
                if other_col == col:
                    continue
                if other_col == scene.collection:
                    continue
                try:
                    other_col.objects.unlink(obj)
                except Exception:
                    pass

        # Clean up collections that are now empty (except RF_VFX and master)
        cleaned = []
        for other_col in list(bpy.data.collections):
            if other_col == col:
                continue
            if other_col.name == "RF_VFX":
                continue
            # Only remove if it has no objects and no children
            if len(other_col.objects) == 0 and len(other_col.children) == 0:
                # Unlink from scene if linked
                try:
                    scene.collection.children.unlink(other_col)
                    cleaned.append(other_col.name)
                except Exception:
                    pass

        msg = f"Moved {moved} object(s) to '{col.name}'."
        if cleaned:
            msg += f" Removed empty collection(s): {', '.join(cleaned)}."
        self.report({"INFO"}, msg)
        return {"FINISHED"}

class RFVFX_OT_ArrangeScene(bpy.types.Operator):
    bl_idname = "rfvfx.arrange_scene"
    bl_label = "Select and Frame All"
    bl_description = "Selects all objects in the RF_VFX collection and frames them in the viewport. Use to get a quick overview of your full scene before export."
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        col = _get_export_collection(scene)
        if col is None:
            self.report({"WARNING"}, "No RF_VFX collection found. Create a scene first.")
            return {"CANCELLED"}

        # Gather all objects in RF_VFX
        all_objs = list(col.all_objects)
        if not all_objs:
            self.report({"WARNING"}, "No objects in RF_VFX collection.")
            return {"CANCELLED"}

        # Unhide everything in the collection
        unhidden = 0
        for obj in all_objs:
            if obj.hide_get():
                obj.hide_set(False)
                unhidden += 1
            if obj.hide_viewport:
                obj.hide_viewport = False
                unhidden += 1

        # Make sure collection is visible
        try:
            vl = context.view_layer
            lc = _find_layer_collection(vl.layer_collection, col.name)
            if lc and lc.hide_viewport:
                lc.hide_viewport = False
        except Exception:
            pass

        # Deselect all, then select RF_VFX objects
        bpy.ops.object.select_all(action='DESELECT')
        for obj in all_objs:
            obj.select_set(True)

        # Set active object to first mesh (or first object)
        meshes = [o for o in all_objs if o.type == "MESH"]
        if meshes:
            context.view_layer.objects.active = meshes[0]
        elif all_objs:
            context.view_layer.objects.active = all_objs[0]

        # Frame selected in viewport
        try:
            bpy.ops.view3d.view_selected(use_all_regions=False)
        except Exception:
            pass

        parts = []
        mesh_count = len([o for o in all_objs if o.type == "MESH"])
        emitter_count = len([o for o in all_objs if getattr(getattr(o, "rfvfx_particle", None), "is_emitter", False)])
        empty_count = len([o for o in all_objs if o.type == "EMPTY"]) - (1 if any(o.name == "RFVFX_ROOT" for o in all_objs) else 0)
        if mesh_count: parts.append(f"{mesh_count} mesh{'es' if mesh_count != 1 else ''}")
        if emitter_count: parts.append(f"{emitter_count} emitter{'s' if emitter_count != 1 else ''}")
        if empty_count > 0: parts.append(f"{empty_count} empty")
        msg = f"Selected {', '.join(parts)}."
        if unhidden: msg += f" Unhid {unhidden} objects."
        self.report({"INFO"}, msg)
        return {"FINISHED"}


def _find_layer_collection(lc, name):
    """Recursively find a LayerCollection by collection name."""
    if lc.collection.name == name:
        return lc
    for child in lc.children:
        result = _find_layer_collection(child, name)
        if result:
            return result
    return None


class RFVFX_OT_ExportAuthoringGLTF(bpy.types.Operator):
    bl_idname = "rfvfx.export_authoring_gltf"
    bl_label = "Export Authoring glTF (with extras)"
    bl_options = {"REGISTER"}

    filepath: StringProperty(name="glTF Path", subtype="FILE_PATH", default="")

    def invoke(self, context, event):
        # try to seed from existing addon setting if present
        try:
            s = getattr(context.scene, "rfvfx", None)
            if s and getattr(s, "export_gltf", ""):
                self.filepath = bpy.path.abspath(s.export_gltf)
        except Exception:
            pass
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        scene = context.scene
        path = bpy.path.abspath(self.filepath)
        if not path:
            self.report({"ERROR"}, "No filepath chosen.")
            return {"CANCELLED"}

        # sync props -> ID props so they can be exported as extras
        objs = _export_objs(scene)
        for o in objs:
            try:
                if hasattr(o, "rfvfx_props"):
                    _sync_obj_to_idprops(o)
            except Exception:
                pass
            try:
                if hasattr(o, "rfvfx_particle") and o.rfvfx_particle.is_emitter:
                    _sync_particle_to_idprops(o)
            except Exception:
                pass
            if o.type == "MESH":
                for slot in (o.material_slots or []):
                    mat = slot.material
                    if mat and hasattr(mat, "rfvfx_props"):
                        _sync_mat_to_idprops(mat)

        # Temporarily rename materials that have an explicit RF texture name so
        # vfx2obj can read it regardless of whether the glTF exporter supports
        # export_custom_properties. vfx2obj checks the material name as a fallback
        # and uses it when the extension is .tga/.vbm/.dds. Restored after export.
        _mat_name_restore = {}  # mat -> original_name
        try:
            for o in objs:
                if o.type != "MESH":
                    continue
                for slot in (o.material_slots or []):
                    mat = slot.material
                    if not mat:
                        continue
                    p = getattr(mat, "rfvfx_props", None)
                    if p and p.texture_name.strip():
                        tex = p.texture_name.strip()
                        if mat.name != tex and mat not in _mat_name_restore:
                            _mat_name_restore[mat] = mat.name
                            mat.name = tex
        except Exception as _e:
            header += f"Material rename pre-export warning: {_e}\n"

        props = _gltf_op_props()
        kwargs = {
            "filepath": path,
            "export_format": "GLTF_SEPARATE",
            "use_selection": False,
            "export_apply": True,
            "use_mesh_modifiers": True,
            "export_extras": True,
            "export_custom_properties": True,
            "export_normals": True,
            "export_texcoords": True,
            "export_animations": True,
            "export_morph": True,
            "export_shape_keys": True,
            "export_yup": True,  # explicit: don't inherit from UI state
        }
        # Filter to only props supported by this Blender version
        call = {k: v for k, v in kwargs.items() if k in props}

        try:
            bpy.ops.export_scene.gltf(**call)
        except Exception as e:
            _rfvfx_log("[AUTHORING_GLTF] export failed: " + repr(e))
            self.report({"ERROR"}, "glTF export failed. See RFVFX_Log in Blender Text Editor.")
            return {"CANCELLED"}

        # also store to existing addon setting if present
        try:
            s = getattr(scene, "rfvfx", None)
            if s and hasattr(s, "export_gltf"):
                s.export_gltf = self.filepath
        except Exception:
            pass

        self.report({"INFO"}, f"Wrote: {path}")
        return {"FINISHED"}


class RFVFX_OT_BakeAnimToShapeKeys(bpy.types.Operator):
    bl_idname = "rfvfx.bake_anim_to_shape_keys"
    bl_label = "Bake Animation to Shape Keys"
    bl_description = "Samples your animated mesh at every frame and bakes the result into shape keys. Required for morph animation export. Run this after animating, before exporting."
    bl_options = {"REGISTER", "UNDO"}

    frame_start: IntProperty(name="Start Frame", default=1, min=0, max=10000)
    frame_end: IntProperty(name="End Frame", default=20, min=1, max=20000)
    step: IntProperty(
        name="Speed Step",
        default=1, min=1, max=20,
        description=(
            "Sample every Nth frame. Step=1: every frame (slowest/smoothest in RF). "
            "Step=2: every 2nd frame (2x faster in RF). "
            "Step=3: every 3rd frame (3x faster), etc. "
            "Use this to compensate for RF playing morph animations slower than Blender."
        )
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == "MESH"

    def invoke(self, context, event):
        # Try to auto-detect range from keyframes, fall back to reasonable defaults
        obj = context.active_object
        first_kf = None
        last_kf = None

        # Scan all animation sources for the first and last keyframe
        actions_to_check = []
        me = obj.data
        if me.shape_keys and me.shape_keys.animation_data and me.shape_keys.animation_data.action:
            actions_to_check.append(me.shape_keys.animation_data.action)
        if obj.animation_data and obj.animation_data.action:
            actions_to_check.append(obj.animation_data.action)

        for act in actions_to_check:
            try:
                # Try legacy fcurves
                fcs = getattr(act, "fcurves", None)
                if fcs:
                    for fc in fcs:
                        for kp in fc.keyframe_points:
                            f = int(kp.co[0])
                            if first_kf is None or f < first_kf:
                                first_kf = f
                            if last_kf is None or f > last_kf:
                                last_kf = f
            except Exception:
                pass
            try:
                # Try Blender 5.0 layered actions
                for layer in getattr(act, "layers", []):
                    for strip in getattr(layer, "strips", []):
                        for ch in getattr(strip, "channels", []):
                            for kp in getattr(ch, "keyframe_points", []):
                                f = int(kp.co[0])
                                if first_kf is None or f < first_kf:
                                    first_kf = f
                                if last_kf is None or f > last_kf:
                                    last_kf = f
            except Exception:
                pass

        # Use actual keyframe bounds if found, otherwise fall back to scene range
        self.frame_start = first_kf if first_kf is not None else context.scene.frame_start
        if last_kf is not None and last_kf > self.frame_start:
            self.frame_end = last_kf
        else:
            # No keyframes found (e.g. Wave modifier) — use scene end frame
            self.frame_end = context.scene.frame_end

        return context.window_manager.invoke_props_dialog(self, width=340)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "frame_start")
        layout.prop(self, "frame_end")
        layout.separator()
        layout.prop(self, "step")

        # Live timing readout
        total_source = self.frame_end - self.frame_start + 1
        num_keys = max(1, len(range(self.frame_start, self.frame_end + 1, self.step)))
        s = getattr(context.scene, "rfvfx", None)
        rf_fps = int(getattr(s, "morph_fps", 15)) if s else 15
        rf_secs = num_keys / rf_fps
        bl_fps = context.scene.render.fps
        bl_secs = total_source / bl_fps

        col = layout.column(align=True)
        col.scale_y = 0.8
        col.separator()
        col.label(text=f"Shape keys to bake: {num_keys}", icon="SHAPEKEY_DATA")
        col.label(text=f"RF duration: {rf_secs:.2f}s  (at {rf_fps} morph fps)", icon="TIME")
        col.label(text=f"Blender duration: {bl_secs:.2f}s  (at {bl_fps} fps)", icon="RENDER_ANIMATION")
        speed_ratio = bl_secs / rf_secs if rf_secs > 0 else 1.0
        if abs(speed_ratio - 1.0) < 0.05:
            col.label(text="RF timing matches Blender", icon="CHECKBOX_HLT")
        elif speed_ratio < 1.0:
            col.label(text=f"RF plays {1/speed_ratio:.2f}x slower than Blender — increase Step", icon="ERROR")
        else:
            col.label(text=f"RF plays {speed_ratio:.2f}x faster than Blender", icon="INFO")

        # File size estimate
        # Per-frame binary cost: 24 bytes (center+mult vec3s) + 6 bytes per vertex (3x int16)
        # UVs are written on frame 0 only: 8 bytes per face-corner
        obj = context.active_object
        if obj and obj.type == "MESH":
            num_verts = len(obj.data.vertices)
            num_loops = len(obj.data.loops)
            bytes_per_frame = 24 + num_verts * 6
            uv_bytes = num_loops * 8  # frame 0 UVs
            est_bytes = num_keys * bytes_per_frame + uv_bytes
            est_mb = est_bytes / (1024 * 1024)

            col.separator()
            size_col = layout.column(align=True)
            size_col.scale_y = 0.8

            if est_mb < 1.0:
                size_icon = "CHECKBOX_HLT"
                size_text = f"Est. VFX size: {est_mb*1024:.0f} KB  ({num_verts} verts)"
            elif est_mb < 10.0:
                size_icon = "INFO"
                size_text = f"Est. VFX size: {est_mb:.1f} MB  ({num_verts} verts)"
            elif est_mb < 30.0:
                size_icon = "ERROR"
                size_text = f"Est. VFX size: {est_mb:.1f} MB — consider reducing grid density"
            else:
                size_icon = "ERROR"
                size_text = f"Est. VFX size: {est_mb:.1f} MB — grid too dense, reduce subdivisions"

            size_col.label(text=size_text, icon=size_icon)

            # Suggest a target grid size if too large
            if est_mb > 10.0:
                # Work backwards: what vert count gives ~5MB?
                target_verts = int((5 * 1024 * 1024 - uv_bytes) / (num_keys * 6))
                target_side = max(10, int(target_verts ** 0.5))
                size_col.label(text=f"  → Try a {target_side}×{target_side} grid (~{target_side*target_side} verts)", icon="MESH_GRID")

    def execute(self, context):
        obj = context.active_object
        scene = context.scene
        dg = context.evaluated_depsgraph_get()

        frame_start = self.frame_start
        frame_end = self.frame_end
        step = max(1, self.step)
        num_frames = frame_end - frame_start + 1

        if num_frames < 2:
            self.report({"ERROR"}, "Need at least 2 frames (check timeline Start/End)")
            return {"CANCELLED"}

        num_keys = len(range(frame_start, frame_end + 1, step))
        if num_keys > 2000:
            self.report({"ERROR"}, f"Too many shape keys ({num_keys}). Reduce range or increase Step. Max 2000.")
            return {"CANCELLED"}

        me = obj.data
        num_verts = len(me.vertices)

        # PHASE 1: Capture what the viewport sees at every frame via the depsgraph.
        # Always use evaluated_get(dg).to_mesh() — this captures the full modifier
        # stack (Wave, Subdivision, etc.) plus any animated shape keys, exactly as
        # Blender renders them. The old approach of manually blending shape key data
        # bypassed modifiers entirely, causing chunky/incorrect results when a Wave
        # or other modifier was present on top of (or instead of) shape keys.
        all_frame_cos = []  # list of flat float arrays, one per frame

        for frame in range(frame_start, frame_end + 1, step):
            # subframe=0.0 + fresh depsgraph ensures time-based modifiers
            # (Wave phase, etc.) are fully up to date for this exact frame.
            scene.frame_set(frame, subframe=0.0)
            dg = context.evaluated_depsgraph_get()
            dg.update()
            obj_eval = obj.evaluated_get(dg)
            mesh_eval = obj_eval.to_mesh()

            cos = [0.0] * (num_verts * 3)
            mesh_eval.vertices.foreach_get("co", cos)
            all_frame_cos.append(cos)
            obj_eval.to_mesh_clear()

        if not all_frame_cos:
            self.report({"ERROR"}, "No frames could be evaluated")
            return {"CANCELLED"}

        # Check if any frames actually differ from the first
        has_animation = False
        for i in range(1, len(all_frame_cos)):
            if all_frame_cos[i] != all_frame_cos[0]:
                has_animation = True
                break
        if not has_animation:
            self.report({"WARNING"}, "All frames are identical — no animation detected. Check shape key values are keyframed.")

        # PHASE 2: Remove old shape keys and object animation, rebuild from baked data
        if me.shape_keys:
            obj.shape_key_clear()

        # Basis = frame_start positions
        basis_sk = obj.shape_key_add(name="Basis", from_mix=False)
        basis_sk.data.foreach_set("co", all_frame_cos[0])

        # One shape key per subsequent frame
        created = 0
        for fi in range(1, len(all_frame_cos)):
            sk = obj.shape_key_add(name=f"Frame_{frame_start + fi:04d}", from_mix=False)
            sk.data.foreach_set("co", all_frame_cos[fi])
            sk.value = 0.0  # Default to 0 so they don't all stack
            created += 1

        # Set all shape key values to 0 (the data is stored regardless of value)
        if me.shape_keys:
            for kb in me.shape_keys.key_blocks:
                kb.value = 0.0

        # Set up shape key animation so each frame activates its shape key.
        # Shape keys are placed on SEQUENTIAL frames starting from frame_start,
        # regardless of the original step. This means:
        #   - step=1: shape keys at frame_start, frame_start+1, frame_start+2 ...
        #   - step=2: shape keys at frame_start, frame_start+1, frame_start+2 ...
        #             (but each represents 2 source frames, so plays 2x faster in RF)
        # The scene frame_end is updated to match so glTF/vfx2obj see the correct length.
        baked_end_frame = frame_start + created  # last shape key sits at this frame

        if me.shape_keys and created > 0:
            me.shape_keys.use_relative = True

            all_keys = me.shape_keys.key_blocks
            for ki in range(1, len(all_keys)):
                sk = all_keys[ki]
                # Always place on consecutive frames from frame_start — not the
                # original source frame. This ensures the VFX end_frame in the
                # binary matches the actual number of shape keys written.
                target_frame = frame_start + ki

                # Keyframe this shape key's value using direct keyframe insertion.
                # Insert value=0.0 at target_frame-1 to anchor the start so Blender
                # doesn't constant-extrapolate backwards from the first key.
                sk.value = 0.0
                sk.keyframe_insert(data_path="value", frame=target_frame - 1)
                sk.value = 1.0
                sk.keyframe_insert(data_path="value", frame=target_frame)
                sk.value = 0.0
                if target_frame < baked_end_frame:
                    sk.keyframe_insert(data_path="value", frame=target_frame + 1)
                sk.value = 0.0

                # Set interpolation to CONSTANT for clean frame transitions
                try:
                    if me.shape_keys.animation_data and me.shape_keys.animation_data.action:
                        act = me.shape_keys.animation_data.action
                        dp = f'key_blocks["{sk.name}"].value'
                        fcs = None
                        if hasattr(act, "fcurves"):
                            fcs = act.fcurves
                        elif hasattr(act, "layers"):
                            for layer in act.layers:
                                for strip in getattr(layer, "strips", []):
                                    fcs = getattr(strip, "channels", None)
                                    if fcs: break
                                if fcs: break
                        if fcs:
                            for fc in fcs:
                                if getattr(fc, "data_path", "") == dp:
                                    for kp in fc.keyframe_points:
                                        kp.interpolation = "CONSTANT"
                except Exception:
                    pass

        # Update scene frame range to match the baked key count so the glTF
        # exporter and vfx2obj both see the correct animation length.
        # Without this, vfx2obj reads the old (longer) frame range and writes a
        # VFX end_frame that extends past the last shape key — causing the object
        # to vanish mid-playback in RF.
        scene.frame_start = frame_start
        scene.frame_end = baked_end_frame

        # Remove object-level animation to avoid double-transform
        if obj.animation_data and obj.animation_data.action:
            old_action_name = obj.animation_data.action.name
            obj.animation_data_clear()
            self.report({"INFO"}, f"Removed object animation '{old_action_name}' (baked into shape keys)")

        # NOTE: transform_apply is intentionally NOT called here.
        # Shape keys store local-space vertex positions as captured by to_mesh().
        # Applying transforms with shape keys present would multiply every shape key
        # position by the object's scale/rotation, distorting wave heights and shape.
        # The object transform is correctly applied on top of shape key positions
        # by Blender at display/export time — no manual zeroing needed.

        # Reset frame to start
        scene.frame_set(frame_start)

        self.report({"INFO"}, f"Baked {created} shape keys (step={step}, scene range updated to {frame_start}-{baked_end_frame})")
        return {"FINISHED"}


class RFVFX_OT_ArmatureToShapeKeys(bpy.types.Operator):
    """One-click: bakes a bone-rigged mesh (Mixamo or any armature) into RF shape key animation.
    Select the MESH object (or its armature — we auto-find the mesh).
    Reads the frame range from the armature action automatically.
    Removes the armature modifier and armature object after baking so the
    scene is clean for VFX export."""
    bl_idname = "rfvfx.armature_to_shape_keys"
    bl_label = "Armature → Shape Keys (Mixamo / Rigged)"
    bl_description = (
        "One-click: bakes bone/armature animation into RF morph shape keys. "
        "Works with Mixamo and any rigged mesh. Select the mesh OR armature — "
        "auto-detects everything. Cleans up armature after baking."
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj is None:
            return False
        if obj.type == "MESH":
            # Must have an armature modifier
            return any(m.type == "ARMATURE" for m in obj.modifiers)
        if obj.type == "ARMATURE":
            # Must have at least one mesh child
            return any(c.type == "MESH" for c in obj.children)
        return False

    @staticmethod
    def _find_mesh_and_armature(obj):
        """Return (mesh_obj, armature_obj) regardless of which one is active."""
        if obj.type == "MESH":
            mesh_obj = obj
            arm_obj = None
            for m in obj.modifiers:
                if m.type == "ARMATURE" and m.object:
                    arm_obj = m.object
                    break
        else:  # ARMATURE
            arm_obj = obj
            mesh_obj = None
            for child in obj.children:
                if child.type == "MESH":
                    mesh_obj = child
                    break
        return mesh_obj, arm_obj

    @staticmethod
    def _detect_frame_range(arm_obj, mesh_obj, scene):
        """Detect frame range from armature action, fall back to scene range."""
        first_kf = None
        last_kf = None

        sources = []
        if arm_obj and arm_obj.animation_data and arm_obj.animation_data.action:
            sources.append(arm_obj.animation_data.action)
        if mesh_obj and mesh_obj.animation_data and mesh_obj.animation_data.action:
            sources.append(mesh_obj.animation_data.action)

        for act in sources:
            # Use action.frame_range first — Blender sets this automatically
            # and it's the most reliable way to get the actual animation extent.
            try:
                fr = act.frame_range
                f_start = int(fr[0])
                f_end   = int(fr[1])
                if f_end > f_start:
                    first_kf = f_start if first_kf is None else min(first_kf, f_start)
                    last_kf  = f_end   if last_kf  is None else max(last_kf,  f_end)
                    continue
            except Exception:
                pass
            # Legacy fcurves fallback (Blender < 5.0)
            try:
                for fc in getattr(act, "fcurves", []):
                    for kp in fc.keyframe_points:
                        f = int(kp.co[0])
                        first_kf = f if first_kf is None else min(first_kf, f)
                        last_kf  = f if last_kf  is None else max(last_kf,  f)
            except Exception:
                pass
            # Blender 5.0 layered actions
            try:
                for layer in getattr(act, "layers", []):
                    for strip in getattr(layer, "strips", []):
                        for ch in getattr(strip, "channels", []):
                            for kp in getattr(ch, "keyframe_points", []):
                                f = int(kp.co[0])
                                first_kf = f if first_kf is None else min(first_kf, f)
                                last_kf  = f if last_kf  is None else max(last_kf,  f)
            except Exception:
                pass

        frame_start = first_kf if first_kf is not None else scene.frame_start
        frame_end   = last_kf  if (last_kf is not None and last_kf > frame_start) else scene.frame_end
        return frame_start, frame_end

    def execute(self, context):
        obj = context.active_object
        scene = context.scene

        mesh_obj, arm_obj = self._find_mesh_and_armature(obj)

        if mesh_obj is None:
            self.report({"ERROR"}, "Could not find a mesh object with an armature modifier.")
            return {"CANCELLED"}

        frame_start, frame_end = self._detect_frame_range(arm_obj, mesh_obj, scene)
        num_frames = frame_end - frame_start + 1

        if num_frames < 2:
            self.report({"ERROR"}, f"Animation range too short ({frame_start}–{frame_end}). Check armature action has keyframes.")
            return {"CANCELLED"}

        if num_frames > 2000:
            self.report({"ERROR"}, f"Too many frames ({num_frames}). Reduce the action length or trim start/end. Max 2000.")
            return {"CANCELLED"}

        # --- Make mesh_obj the active selection ---
        bpy.ops.object.select_all(action="DESELECT")
        mesh_obj.select_set(True)
        context.view_layer.objects.active = mesh_obj

        # --- Apply rotation and scale before baking ---
        # Mixamo FBX imports typically arrive with unapplied rotation (-90° X)
        # and scale (100x) on the object. Shape key coords are stored in local
        # mesh space, so if these transforms aren't applied first, the baked
        # shape keys will have wrong orientation and scale.
        # We apply rotation+scale (not location) so the mesh data is correct
        # while preserving the object's world position.
        try:
            bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
        except Exception as e:
            self.report({"WARNING"}, f"Could not apply transforms: {e}. Results may have wrong scale/orientation.")

        me = mesh_obj.data
        num_verts = len(me.vertices)
        dg = context.evaluated_depsgraph_get()
        all_frame_cos = []

        for frame in range(frame_start, frame_end + 1):
            scene.frame_set(frame, subframe=0.0)
            dg = context.evaluated_depsgraph_get()
            dg.update()
            obj_eval = mesh_obj.evaluated_get(dg)
            mesh_eval = obj_eval.to_mesh()
            cos = [0.0] * (num_verts * 3)
            mesh_eval.vertices.foreach_get("co", cos)
            all_frame_cos.append(cos)
            obj_eval.to_mesh_clear()

        if not all_frame_cos:
            self.report({"ERROR"}, "No frames could be evaluated from armature animation.")
            return {"CANCELLED"}

        has_animation = any(all_frame_cos[i] != all_frame_cos[0] for i in range(1, len(all_frame_cos)))
        if not has_animation:
            self.report({"WARNING"}, "All frames are identical — armature may not have bone keyframes on the mesh deformation.")

        # --- Rebuild shape keys from baked data ---
        if me.shape_keys:
            mesh_obj.shape_key_clear()

        basis_sk = mesh_obj.shape_key_add(name="Basis", from_mix=False)
        basis_sk.data.foreach_set("co", all_frame_cos[0])

        created = 0
        for fi in range(1, len(all_frame_cos)):
            sk = mesh_obj.shape_key_add(name=f"Frame_{frame_start + fi:04d}", from_mix=False)
            sk.data.foreach_set("co", all_frame_cos[fi])
            sk.value = 0.0
            created += 1

        if me.shape_keys:
            for kb in me.shape_keys.key_blocks:
                kb.value = 0.0

        baked_end_frame = frame_start + created

        # --- Keyframe the shape key sequence ---
        if me.shape_keys and created > 0:
            me.shape_keys.use_relative = True
            all_keys = me.shape_keys.key_blocks
            for ki in range(1, len(all_keys)):
                sk = all_keys[ki]
                target_frame = frame_start + ki
                sk.value = 0.0
                sk.keyframe_insert(data_path="value", frame=target_frame - 1)
                sk.value = 1.0
                sk.keyframe_insert(data_path="value", frame=target_frame)
                sk.value = 0.0
                if target_frame < baked_end_frame:
                    sk.keyframe_insert(data_path="value", frame=target_frame + 1)
                sk.value = 0.0
                # Set CONSTANT interpolation
                try:
                    act = me.shape_keys.animation_data.action if me.shape_keys.animation_data else None
                    if act:
                        dp = f'key_blocks["{sk.name}"].value'
                        fcs = getattr(act, "fcurves", None)
                        if not fcs:
                            for layer in getattr(act, "layers", []):
                                for strip in getattr(layer, "strips", []):
                                    fcs = getattr(strip, "channels", None)
                                    if fcs: break
                                if fcs: break
                        if fcs:
                            for fc in fcs:
                                if getattr(fc, "data_path", "") == dp:
                                    for kp in fc.keyframe_points:
                                        kp.interpolation = "CONSTANT"
                except Exception:
                    pass

        scene.frame_start = frame_start
        scene.frame_end = baked_end_frame

        # --- Remove object-level animation (armature drove it, no longer needed) ---
        if mesh_obj.animation_data and mesh_obj.animation_data.action:
            mesh_obj.animation_data_clear()

        # --- Remove the armature modifier from the mesh ---
        arm_mod_names = [m.name for m in mesh_obj.modifiers if m.type == "ARMATURE"]
        for mod_name in arm_mod_names:
            try:
                mesh_obj.modifiers.remove(mesh_obj.modifiers[mod_name])
            except Exception:
                pass

        # --- Delete the armature object from the scene ---
        arm_deleted = False
        if arm_obj is not None:
            try:
                bpy.ops.object.select_all(action="DESELECT")
                arm_obj.select_set(True)
                context.view_layer.objects.active = arm_obj
                bpy.ops.object.delete(use_global=False)
                arm_deleted = True
            except Exception:
                pass

        # Restore mesh as active
        try:
            mesh_obj.select_set(True)
            context.view_layer.objects.active = mesh_obj
        except Exception:
            pass

        arm_msg = " Armature removed." if arm_deleted else " (Armature could not be deleted — remove manually.)"
        self.report(
            {"INFO"},
            f"Baked {created} shape keys from frames {frame_start}–{frame_end} ({num_verts} verts).{arm_msg}"
        )
        return {"FINISHED"}


class RFVFX_OT_KeyedShapesToRFTiming(bpy.types.Operator):
    """One-click: takes a mesh that already has shape keys with geometry data
    (e.g. imported Quake/MD2 model) and inserts the RF-compatible keyframe
    sequence so each shape key plays for exactly one frame in sequence.
    No baking — just wires up the timing on existing shapes."""
    bl_idname = "rfvfx.keyed_shapes_to_rf_timing"
    bl_label = "Shape Keys → RF Timing (Quake / Pre-keyed)"
    bl_description = (
        "One-click: wires RF morph timing onto a mesh that already has shape keys "
        "with geometry baked in (e.g. Quake/MD2 models). Each shape key gets "
        "exactly one frame. Select the mesh and click — done."
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj is None or obj.type != "MESH":
            return False
        sk = obj.data.shape_keys
        # Need Basis + at least 1 more key
        return sk is not None and len(sk.key_blocks) >= 2

    def execute(self, context):
        obj = context.active_object
        scene = context.scene
        me = obj.data
        sk = me.shape_keys

        if sk is None or len(sk.key_blocks) < 2:
            self.report({"ERROR"}, "Mesh needs at least a Basis key and one animation key.")
            return {"CANCELLED"}

        # All keys except Basis (index 0)
        anim_keys = list(sk.key_blocks[1:])
        num_keys = len(anim_keys)

        if num_keys > 2000:
            self.report({"ERROR"}, f"Too many shape keys ({num_keys}). Max 2000 for RF. Reduce key count.")
            return {"CANCELLED"}

        frame_start = scene.frame_start
        baked_end_frame = frame_start + num_keys

        # Reset all key values to 0 first
        for kb in sk.key_blocks:
            kb.value = 0.0

        # Clear any existing shape key animation so we start fresh
        if sk.animation_data and sk.animation_data.action:
            sk.animation_data.action = None

        sk.use_relative = True

        for ki, key in enumerate(anim_keys):
            target_frame = frame_start + ki + 1

            key.value = 0.0
            key.keyframe_insert(data_path="value", frame=target_frame - 1)
            key.value = 1.0
            key.keyframe_insert(data_path="value", frame=target_frame)
            key.value = 0.0
            if target_frame < baked_end_frame:
                key.keyframe_insert(data_path="value", frame=target_frame + 1)
            key.value = 0.0

            # Set CONSTANT interpolation on this key's fcurve
            try:
                act = sk.animation_data.action if sk.animation_data else None
                if act:
                    dp = f'key_blocks["{key.name}"].value'
                    fcs = getattr(act, "fcurves", None)
                    if not fcs:
                        for layer in getattr(act, "layers", []):
                            for strip in getattr(layer, "strips", []):
                                fcs = getattr(strip, "channels", None)
                                if fcs: break
                            if fcs: break
                    if fcs:
                        for fc in fcs:
                            if getattr(fc, "data_path", "") == dp:
                                for kp in fc.keyframe_points:
                                    kp.interpolation = "CONSTANT"
            except Exception:
                pass

        scene.frame_end = baked_end_frame

        self.report(
            {"INFO"},
            f"RF timing applied: {num_keys} shape keys → frames {frame_start}–{baked_end_frame}. Ready to export."
        )
        return {"FINISHED"}


def _quake_mdl_to_rf_cos(verts_raw, scale, scale_origin):
    """Convert Quake MDL compressed vertex bytes to RF-ready Blender coords.
    MDL stores verts as 3 unsigned bytes scaled by per-file scale + origin.
    Quake axes: X=forward, Y=left, Z=up.
    RF/Blender pipeline expects Blender axes with RF unit scale.
    RF uses feet; Quake uses inches. 1 inch = 1/12 foot.
    Scale factor 0.08333 (1/12) converts Quake inches to RF feet (Blender units).
    Axis mapping: Quake X->Bl -Y, Quake Y->Bl -X, Quake Z->Bl Z
    then blender_to_rf inverse applies on export.
    """
    INCH_TO_RF = 1.0 / 12.0  # 1 Quake inch = 1/12 RF foot
    sx, sy, sz = scale
    ox, oy, oz = scale_origin
    out = []
    for (bx, by, bz) in verts_raw:
        qx = bx * sx + ox
        qy = by * sy + oy
        qz = bz * sz + oz
        blx = -qy * INCH_TO_RF
        bly = -qx * INCH_TO_RF
        blz =  qz * INCH_TO_RF
        out.extend([blx, bly, blz])
    return out


def _quake_md2_frame_cos(verts_raw, scale, translate):
    """Convert MD2 compressed vertex bytes to RF-ready Blender coords.
    MD2 per-frame scale/translate unpacks 0-255 bytes to world coords.
    Same axis mapping and scale as MDL. 1 Quake inch = 1/12 RF foot.
    """
    INCH_TO_RF = 1.0 / 12.0
    sx, sy, sz = scale
    tx, ty, tz = translate
    out = []
    for v in verts_raw:
        bx_raw, by_raw, bz_raw = v[0], v[1], v[2]
        qx = bx_raw * sx + tx
        qy = by_raw * sy + ty
        qz = bz_raw * sz + tz
        blx = -qy * INCH_TO_RF
        bly = -qx * INCH_TO_RF
        blz =  qz * INCH_TO_RF
        out.extend([blx, bly, blz])
    return out


def _read_mdl(filepath):
    """Parse a Quake 1 .mdl file. Returns dict with mesh and frame data."""
    import struct as _st
    with open(filepath, "rb") as f:
        raw = f.read()
    pos = 0
    def ru(fmt):
        nonlocal pos
        size = _st.calcsize(fmt)
        val = _st.unpack_from(fmt, raw, pos)
        pos += size
        return val

    ident = raw[0:4]
    pos = 4
    (version,) = ru("<i")
    if ident not in (b"IDPO", b"MD16") or version not in (3, 6):
        raise ValueError(f"Not a valid Quake MDL file (ident={ident} version={version})")

    scale        = ru("<3f")
    scale_origin = ru("<3f")
    pos += 4  # bounding radius
    pos += 12 # eye position
    (num_skins,)   = ru("<i")
    skinw, skinh   = ru("<2i")
    numverts, numtris, numframes = ru("<3i")
    pos += 4  # synctype
    if version == 6:
        pos += 8  # flags + size

    # Skip skin data
    for _ in range(num_skins):
        (skin_type,) = ru("<i")
        if skin_type:
            (n,) = ru("<i")
            pos += n * 4          # times
            pos += n * skinw * skinh
        else:
            pos += skinw * skinh

    # Skip ST verts
    pos += numverts * 12

    # Read tris (facesfront + 3 vert indices)
    tris = []
    for _ in range(numtris):
        ff, v0, v1, v2 = ru("<4i")
        tris.append((v2, v1, v0))  # reverse winding for Blender

    # Read frames
    frames = []
    frame_names = []
    for fi in range(numframes):
        (ftype,) = ru("<i")
        if ftype:
            # frame group
            (nsubframes,) = ru("<i")
            pos += 8   # mins/maxs bounds
            pos += nsubframes * 4  # times
            for sfi in range(nsubframes):
                pos += 8  # sub mins/maxs
                name_bytes = raw[pos:pos+16]; pos += 16
                name = name_bytes.split(b"\x00")[0].decode("latin1", errors="replace")
                verts_raw = []
                for _ in range(numverts):
                    bx, by, bz, ni = ru("<4B")
                    verts_raw.append((bx, by, bz))
                frames.append(verts_raw)
                frame_names.append(name)
        else:
            pos += 8  # mins/maxs
            name_bytes = raw[pos:pos+16]; pos += 16
            name = name_bytes.split(b"\x00")[0].decode("latin1", errors="replace")
            verts_raw = []
            for _ in range(numverts):
                bx, by, bz, ni = ru("<4B")
                verts_raw.append((bx, by, bz))
            frames.append(verts_raw)
            frame_names.append(name)

    return {
        "scale": scale, "scale_origin": scale_origin,
        "numverts": numverts, "tris": tris,
        "frames": frames, "frame_names": frame_names,
        "num_skins": num_skins, "skinwidth": skinw, "skinheight": skinh,
    }


def _read_md2(filepath):
    """Parse a Quake 2 .md2 file. Returns dict with mesh and frame data."""
    import struct as _st
    with open(filepath, "rb") as f:
        raw = f.read()

    (ident, version, skinw, skinh, framesize,
     num_skins, num_xyz, num_st, num_tris, num_glcmds, num_frames,
     ofs_skins, ofs_st, ofs_tris, ofs_frames, ofs_glcmds, ofs_end
    ) = _st.unpack_from("<17i", raw, 0)

    if ident != 0x32504449:  # 'IDP2'
        raise ValueError("Not a valid Quake 2 MD2 file")

    # Read tris
    tris = []
    for i in range(num_tris):
        off = ofs_tris + i * 12
        v0, v1, v2 = _st.unpack_from("<3H", raw, off)
        tris.append((v2, v1, v0))  # reverse winding

    # Read frames
    frames = []
    frame_names = []
    frame_size = 40 + num_xyz * 4
    for fi in range(num_frames):
        off = ofs_frames + fi * frame_size
        sx, sy, sz = _st.unpack_from("<3f", raw, off)
        tx, ty, tz = _st.unpack_from("<3f", raw, off + 12)
        name = raw[off+24:off+40].split(b"\x00")[0].decode("latin1", errors="replace")
        verts = []
        for vi in range(num_xyz):
            voff = off + 40 + vi * 4
            bx, by, bz, ni = _st.unpack_from("<4B", raw, voff)
            verts.append((bx, by, bz))
        frames.append({"scale": (sx,sy,sz), "translate": (tx,ty,tz), "verts": verts})
        frame_names.append(name)

    return {
        "numverts": num_xyz, "tris": tris,
        "frames": frames, "frame_names": frame_names,
        "skinwidth": skinw, "skinheight": skinh,
    }


class RFVFX_OT_ImportQuakeModel(bpy.types.Operator):
    """Import a Quake MDL or MD2 file directly into RF_VFX as shape key
    morph animation. Handles axis conversion (Quake->RF) and scale (inches->m)
    automatically. For large MD2 files, use Start/End Frame to import a subset."""
    bl_idname = "rfvfx.import_quake_model"
    bl_label = "Import Quake Model (MDL/MD2)..."
    bl_description = (
        "Import a Quake 1 .mdl or Quake 2 .md2 file as RF morph animation. "
        "Handles coordinate conversion and scale automatically. "
        "Large MD2 files: use Start/End Frame to import a frame range."
    )
    bl_options = {"REGISTER", "UNDO"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.mdl;*.md2", options={"HIDDEN"})

    frame_start: bpy.props.IntProperty(
        name="Start Frame", default=0, min=0,
        description="First animation frame to import (0 = beginning)"
    )
    frame_end: bpy.props.IntProperty(
        name="End Frame", default=0, min=0,
        description="Last animation frame to import (0 = all frames)"
    )
    step: bpy.props.IntProperty(
        name="Step", default=1, min=1, max=10,
        description="Import every Nth frame. Step=2 halves frame count and file size."
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.label(text="Frame Range (0 = auto):")
        layout.prop(self, "frame_start")
        layout.prop(self, "frame_end")
        layout.prop(self, "step")
        layout.separator()
        layout.label(text="Tip: MD2 files often have 1000+ frames.", icon="INFO")
        layout.label(text="Use Start/End to import one animation.")

    def execute(self, context):
        import os
        filepath = self.filepath
        ext = os.path.splitext(filepath)[1].lower()

        try:
            if ext == ".mdl":
                data = _read_mdl(filepath)
                is_md2 = False
            elif ext == ".md2":
                data = _read_md2(filepath)
                is_md2 = True
            else:
                self.report({"ERROR"}, f"Unsupported file type: {ext}. Use .mdl or .md2")
                return {"CANCELLED"}
        except Exception as e:
            self.report({"ERROR"}, f"Failed to read file: {e}")
            return {"CANCELLED"}

        total_frames = len(data["frames"])
        fs = self.frame_start
        fe = self.frame_end if self.frame_end > 0 else total_frames - 1
        fe = min(fe, total_frames - 1)
        step = max(1, self.step)

        frame_indices = list(range(fs, fe + 1, step))
        if len(frame_indices) < 2:
            self.report({"ERROR"}, f"Need at least 2 frames. File has {total_frames} frames total.")
            return {"CANCELLED"}
        if len(frame_indices) > 2000:
            self.report({"ERROR"}, f"Too many frames ({len(frame_indices)}). Increase Step or reduce range. Max 2000.")
            return {"CANCELLED"}

        numverts = data["numverts"]
        tris = data["tris"]

        # Build basis vertex positions (first selected frame)
        if is_md2:
            f0 = data["frames"][frame_indices[0]]
            basis_cos = _quake_md2_frame_cos(
                f0["verts"], f0["scale"], f0["translate"]
            )
        else:
            basis_cos = _quake_mdl_to_rf_cos(
                data["frames"][frame_indices[0]],
                data["scale"], data["scale_origin"]
            )

        # Build mesh from basis frame
        mesh = bpy.data.meshes.new(os.path.splitext(os.path.basename(filepath))[0])
        verts_3d = [(basis_cos[i*3], basis_cos[i*3+1], basis_cos[i*3+2])
                    for i in range(numverts)]
        mesh.from_pydata(verts_3d, [], tris)
        mesh.update()

        obj = bpy.data.objects.new(mesh.name, mesh)

        # Add to RF_VFX collection if it exists, else scene collection
        scene = context.scene
        col = None
        cname = scene.get("rfvfx_export_collection", "RF_VFX")
        col = bpy.data.collections.get(cname)
        if col is None:
            col = context.scene.collection
        col.objects.link(obj)
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        context.view_layer.objects.active = obj

        # Add UV layer (placeholder — Quake UV data needs skin texture)
        mesh.uv_layers.new(name="UVMap")

        # Build shape keys — Basis + one per selected frame
        basis_sk = obj.shape_key_add(name="Basis", from_mix=False)
        basis_sk.data.foreach_set("co", basis_cos)

        scene_frame_start = context.scene.frame_start
        created = 0
        for fi, frame_idx in enumerate(frame_indices[1:], start=1):
            if is_md2:
                fr = data["frames"][frame_idx]
                cos = _quake_md2_frame_cos(fr["verts"], fr["scale"], fr["translate"])
            else:
                cos = _quake_mdl_to_rf_cos(
                    data["frames"][frame_idx],
                    data["scale"], data["scale_origin"]
                )

            fname = data["frame_names"][frame_idx] if data["frame_names"][frame_idx] else f"Frame_{frame_idx:04d}"
            sk = obj.shape_key_add(name=fname, from_mix=False)
            sk.data.foreach_set("co", cos)
            sk.value = 0.0
            created += 1

        # Wire up RF constant-interpolation keyframes
        me = obj.data
        if me.shape_keys and created > 0:
            me.shape_keys.use_relative = True
            all_keys = me.shape_keys.key_blocks
            baked_end = scene_frame_start + created
            for ki in range(1, len(all_keys)):
                sk = all_keys[ki]
                target_frame = scene_frame_start + ki
                sk.value = 0.0
                sk.keyframe_insert(data_path="value", frame=target_frame - 1)
                sk.value = 1.0
                sk.keyframe_insert(data_path="value", frame=target_frame)
                sk.value = 0.0
                if target_frame < baked_end:
                    sk.keyframe_insert(data_path="value", frame=target_frame + 1)
                sk.value = 0.0
                try:
                    act = me.shape_keys.animation_data.action if me.shape_keys.animation_data else None
                    if act:
                        dp = f'key_blocks["{sk.name}"].value'
                        fcs = getattr(act, "fcurves", None)
                        if not fcs:
                            for layer in getattr(act, "layers", []):
                                for strip in getattr(layer, "strips", []):
                                    fcs = getattr(strip, "channels", None)
                                    if fcs: break
                                if fcs: break
                        if fcs:
                            for fc in fcs:
                                if getattr(fc, "data_path", "") == dp:
                                    for kp in fc.keyframe_points:
                                        kp.interpolation = "CONSTANT"
                except Exception:
                    pass

            context.scene.frame_end = baked_end

        # Mark for RF export
        try:
            obj["rfvfx_export"] = 1
        except Exception:
            pass

        self.report(
            {"INFO"},
            f"Imported {os.path.basename(filepath)}: {numverts} verts, "
            f"{created} shape keys (frames {fs}–{fe}, step={step}). "
            f"Assign a texture in the Materials panel, then Export VFX."
        )
        return {"FINISHED"}


class RFVFX_OT_ReadinessCheck(bpy.types.Operator):
    bl_idname = "rfvfx.readiness_check"
    bl_label = "Readiness Check"
    bl_description = "Scans the scene for common export problems: missing textures, duplicate names, invalid frame counts. Run before exporting to catch issues early."
    bl_options = {"REGISTER"}

    def execute(self, context):
        scene = context.scene
        col = _get_export_collection(scene)
        if col is None:
            self.report({"ERROR"}, "No RF_VFX collection found. Click 'New RF VFX Scene' first.")
            return {"CANCELLED"}
        objs = _export_objs(scene)

        errors = []
        warns  = []

        if not objs:
            errors.append("No export objects found. Use 'New RF VFX Scene' then 'Add Selected To RF_VFX'.")
        elif not any(o.type == "MESH" for o in objs):
            errors.append("RF_VFX has no mesh objects — nothing to export.")

        # end_frame note: the exporter auto-clamps end_frame to >= 5 for all exports.
        # No need to check here — it's enforced at export time.

        # duplicate names
        names = [o.name for o in objs]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            errors.append("Duplicate object names (RF uses name-based parenting): " + ", ".join(dup))

        # Check mesh count
        mesh_count = sum(1 for o in objs if o.type == "MESH")
        if mesh_count == 0:
            warns.append("No meshes found in export set. VFX will have no visible geometry.")

        # global camera constraint (future-proof)
        cams = [o for o in objs if o.type == "CAMERA"]
        if len(cams) > 1:
            errors.append(f"Too many cameras in export set ({len(cams)}). RF exporter expects max 1.")

        export_set = {o.name for o in objs}

        for o in objs:
            # flag conflicts
            try:
                p = getattr(o, "rfvfx_props", None)
                facing = bool(getattr(p, "facing", False)) if p else bool(o.get("rfvfx_facing", 0))
                facing_rod = bool(getattr(p, "facing_rod", False)) if p else bool(o.get("rfvfx_facing_rod", 0))
                if facing and facing_rod:
                    errors.append(f"{o.name}: both 'facing' and 'facing-rod' set (illegal).")
            except Exception:
                pass

            # UV requirement for meshes
            if o.type == "MESH":
                if not _mesh_has_uvs(o):
                    errors.append(f"{o.name}: mesh has no UV map (RF expects mapping on faces).")

                # Vertex/face count warnings (limits depend on Alpine Faction)
                is_af = True  # Alpine Faction always enabled
                nv = len(o.data.vertices)
                nf = len(o.data.polygons)
                if is_af:
                    if nv > 2000:
                        warns.append(f"{o.name}: {nv} vertices (high even for Alpine Faction, may affect performance).")
                    if nf > 1500:
                        warns.append(f"{o.name}: {nf} faces (high even for Alpine Faction, may affect performance).")
                else:
                    if nv > 500:
                        warns.append(f"{o.name}: {nv} vertices (vanilla RF limit ~500, consider reducing).")
                    if nf > 300:
                        warns.append(f"{o.name}: {nf} faces (vanilla RF limit ~300, consider reducing).")

                # animated scale (usually not supported / dangerous)
                if _has_animated_scale(o):
                    errors.append(f"{o.name}: animated SCALE detected (RF VFX scale anim is unsafe).")

                # materials / textures
                for slot in (o.material_slots or []):
                    mat = slot.material
                    if not mat:
                        warns.append(f"{o.name}: material slot has no material.")
                        continue
                    tex_names = _collect_texture_names(mat)
                    if not tex_names:
                        warns.append(f"{o.name}/{mat.name}: no texture name found (set Material → RFVFX → Texture Name).")
                    for tn in tex_names:
                        low = tn.lower()
                        if is_af:
                            if not (low.endswith(".tga") or low.endswith(".vbm") or low.endswith(".dds")):
                                warns.append(f"{o.name}/{mat.name}: suspicious texture '{tn}' (expects .tga, .vbm, or .dds).")
                        else:
                            if not (low.endswith(".tga") or low.endswith(".vbm")):
                                warns.append(f"{o.name}/{mat.name}: suspicious texture '{tn}' (vanilla RF expects .tga or .vbm).")

            # parenting sanity
            if o.parent:
                if o.parent.name not in export_set:
                    # Don't warn about RFVFX_ROOT parent — it's the standard root
                    if o.parent.name not in ("RFVFX_ROOT",):
                        warns.append(f"{o.name}: parent '{o.parent.name}' is NOT in export set.")

            # $prop_flag dummy parent check
            # The engine uses $prop_flag to attach the flag to a player's hand during carry.
            # It MUST be parented to the flagpole mesh (or equivalent root mesh), NOT to Scene Root.
            # If parented to Scene Root, late-joining players see the flag stuck at the base
            # position while it is being carried, because the attachment transform can't resolve.
            if o.type == "EMPTY" and o.name.lower() == "$prop_flag":
                parent = o.parent
                if parent is None or parent.name in ("RFVFX_ROOT", "Scene Root") or parent.type != "MESH":
                    errors.append(
                        f"$prop_flag is not parented to a mesh object. "
                        f"This causes late-joining players to see the flag stuck at the base "
                        f"while it is being carried. "
                        f"Fix: select $prop_flag, Shift+click your flagpole mesh, Ctrl+P > Object."
                    )

            # Particle emitter checks
            if o.type == "EMPTY":
                pp = getattr(o, "rfvfx_particle", None)
                if pp and pp.is_emitter:
                    if pp.decay < 0.1:
                        errors.append(f"{o.name}: particle lifetime must be >= 1")
                    if pp.spawn_delay <= 0:
                        errors.append(f"{o.name}: particle birth rate must be >= 1")
                    if pp.particle_type == "FACING" and not pp.texture_name.strip():
                        warns.append(f"{o.name}: Facing particle has no texture name (set in Particle Emitters panel)")
                    tn = (pp.texture_name or "").strip().lower()
                    if tn and not (tn.endswith(".tga") or tn.endswith(".vbm")):
                        warns.append(f"{o.name}: particle texture '{pp.texture_name}' should be .tga, .vbm, or .dds")

        # write report
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = []
        lines.append(f"# RFVFX Export Readiness Report")
        lines.append(f"- Time: {now}")
        lines.append(f"- Export objects: {len(objs)}")
        n_meshes = sum(1 for o in objs if o.type == "MESH")
        n_empties = sum(1 for o in objs if o.type == "EMPTY" and o.name.startswith("$"))
        n_particles = sum(1 for o in objs if o.type == "EMPTY" and getattr(getattr(o, "rfvfx_particle", None), "is_emitter", False))
        lines.append(f"- Meshes: {n_meshes}, Dummies: {n_empties}, Particle Emitters: {n_particles}")
        lines.append("")
        if errors:
            lines.append("## ERRORS (will likely break export/game)")
            for e in errors:
                lines.append(f"- {e}")
            lines.append("")
        if warns:
            lines.append("## WARNINGS (probably fine, but suspicious)")
            for w in warns:
                lines.append(f"- {w}")
            lines.append("")

        t = _get_or_create_text("RFVFX_Readiness_Report")
        t.clear()
        t.write("\n".join(lines) + "\n")

        # optional json copy into same folder as chosen export glTF if we can infer it
        out_dir = None
        try:
            s = getattr(scene, "rfvfx", None)
            if s and getattr(s, "export_gltf", ""):
                out_dir = os.path.dirname(bpy.path.abspath(s.export_gltf))
        except Exception:
            pass

        if out_dir and os.path.isdir(out_dir):
            try:
                payload = {"time": now, "objects": [o.name for o in objs], "errors": errors, "warnings": warns}
                p = os.path.join(out_dir, "rfvfx_readiness_report.json")
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
            except Exception as e:
                _rfvfx_log("[READINESS] could not write json: " + repr(e))

        if errors:
            self.report({"ERROR"}, f"Readiness FAILED: {len(errors)} error(s). Open RFVFX_Readiness_Report.")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Readiness OK: {len(warns)} warning(s). Open RFVFX_Readiness_Report.")
        return {"FINISHED"}

class RFVFX_OT_WriteAuthoringGuide(bpy.types.Operator):
    bl_idname = "rfvfx.write_authoring_guide"
    bl_label = "Write Authoring Guide"
    bl_description = "Writes a comprehensive RF VFX authoring guide to a text block in Blender (open in Text Editor)"
    bl_options = {"REGISTER"}

    def execute(self, context):
        md = []
        md.append("=" * 72)
        md.append("  RF VFX TOOLS — AUTHORING GUIDE  (v0.7.0)")
        md.append("=" * 72)
        md.append("")
        md.append("Complete reference for creating Red Faction VFX files in Blender.")
        md.append("Open this in the Text Editor (Scripting workspace or Text Editor area).")
        md.append("")
        md.append("")

        # ── SCENE HIERARCHY ──────────────────────────────────────────
        md.append("SCENE HIERARCHY")
        md.append("=" * 40)
        md.append("Every VFX file has a strict hierarchy the game engine expects:")
        md.append("")
        md.append("  Scene Root")
        md.append("    ├── Mesh objects  (SFXO sections — visible geometry)")
        md.append("    │     └── Can parent to each other or Scene Root")
        md.append("    ├── Particle emitters  (PART sections)")
        md.append("    │     └── Usually parented to a mesh")
        md.append("    ├── Dummies  (DMMY sections — invisible attachment points)")
        md.append("    └── Materials  (MATL sections — one per texture used)")
        md.append("")
        md.append("In Blender:")
        md.append("  - RF_VFX collection holds all export objects")
        md.append("  - RFVFX_ROOT empty = the Scene Root")
        md.append("  - Parenting in Blender = parenting in the VFX file")
        md.append("  - The engine uses parent relationships to attach particles")
        md.append("    to meshes and inherit transforms as meshes animate.")
        md.append("")
        md.append("")

        # ── QUICK START: EDIT ────────────────────────────────────────
        md.append("QUICK START: EDIT AN EXISTING VFX")
        md.append("=" * 40)
        md.append("1. New VFX / Import panel > Import VFX... > select .vfx file")
        md.append("2. Edit mesh geometry in Blender")
        md.append("3. Export panel > Export VFX")
        md.append("   (Patch mode auto-detects from your last import)")
        md.append("")
        md.append("Patch mode replaces mesh vertex data in the original file")
        md.append("while keeping header, materials, and animation data intact.")
        md.append("Vertex count must stay the same when patching.")
        md.append("")
        md.append("")

        # ── QUICK START: NEW VFX ─────────────────────────────────────
        md.append("QUICK START: CREATE A NEW VFX")
        md.append("=" * 40)
        md.append("1. Click 'New RF VFX Scene' to set up the collection and root")
        md.append("2. Model your mesh (low-poly recommended)")
        md.append("3. Select your mesh, click 'Add Selected To RF_VFX'")
        md.append("4. Assign material: RF VFX sidebar > Materials panel")
        md.append("   Enter texture filename e.g. 'fire01.tga' or 'fire_anim.vbm'")
        md.append("   Tip: use a .tga frame as a viewport placeholder, then type the .vbm name")
        md.append("5. Set object flags if needed (all optional)")
        md.append("6. If animated: Export panel > Bake Animation to Shape Keys")
        md.append("7. Set Output VFX path > Export VFX")
        md.append("")
        md.append("")

        # ── IMPORT RFG MAP ───────────────────────────────────────────
        md.append("IMPORT RFG MAP")
        md.append("=" * 40)
        md.append("Import Red Faction map geometry directly into Blender.")
        md.append("Useful for referencing level geometry when building VFX.")
        md.append("")
        md.append("  New VFX / Import panel > Import RFG Map...")
        md.append("  Options:")
        md.append("    Merge Brushes by Texture — combine brushes sharing")
        md.append("      the same texture into one object (recommended, on by default)")
        md.append("    Skip Invisible Faces — skip sky, hole, and portal faces")
        md.append("    Import into RF_VFX Collection — add to the active VFX collection")
        md.append("")
        md.append("Imported materials are automatically named after the RF texture filename")
        md.append("and have the texture name pre-set in the Materials panel.")
        md.append("")
        md.append("")

        # ── MATERIALS PANEL ──────────────────────────────────────────
        md.append("MATERIALS PANEL")
        md.append("=" * 40)
        md.append("Assigns RF textures to each material slot independently.")
        md.append("Select a mesh, open the Materials panel in the RF VFX sidebar.")
        md.append("")
        md.append("  Texture name field — type the RF filename (.tga, .vbm, or .dds)")
        md.append("  Browse button      — opens a file picker for that slot")
        md.append("  Extension label    — shows detected file type")
        md.append("  Additive Blending  — additive blend for glow/fire/energy effects")
        md.append("")
        md.append("VBM workflow:")
        md.append("  Plug a .tga single frame into the Blender material node for")
        md.append("  viewport preview and UV scaling reference.")
        md.append("  In the Materials panel, type the .vbm filename.")
        md.append("  The explicit name always overrides the node image on export.")
        md.append("")
        md.append("If the texture name field is blank, the exporter uses the Blender")
        md.append("material name as a fallback. Explicit entries are recommended.")
        md.append("")
        md.append("")

        # ── OBJECT FLAGS ─────────────────────────────────────────────
        md.append("OBJECT FLAGS")
        md.append("=" * 40)
        md.append("All flags are optional. Defaults render as a normal 3D object.")
        md.append("Select a mesh to see its flags — not shown for empties.")
        md.append("")
        md.append("  Billboard  — Mesh always faces the camera. Use for flat")
        md.append("               effects: sparks, flares, lens effects.")
        md.append("               Mesh should be a flat plane.")
        md.append("")
        md.append("  Rod        — Billboard locked to one axis (the mesh's longest).")
        md.append("               Use for beams, streaks, lightning.")
        md.append("               Auto-enables Fullbright. Mutually exclusive with Billboard.")
        md.append("")
        md.append("  Fullbright — Ignores scene lighting. Always fully lit.")
        md.append("               Use for glowing effects.")
        md.append("")
        md.append("  Morph      — Auto-detected from shape keys. Shown as info only.")
        md.append("               You do not set this manually.")
        md.append("")
        md.append("")

        # ── MORPH ANIMATION ──────────────────────────────────────────
        md.append("MORPH ANIMATION (Per-Vertex Animation)")
        md.append("=" * 40)
        md.append("Morph animation stores the position of every vertex at every")
        md.append("stored frame. This is how animated VFX meshes work in RF.")
        md.append("")
        md.append("HOW IT WORKS IN THE BINARY:")
        md.append("  - The VFX file stores vertex positions at a set 'Morph FPS'")
        md.append("  - The engine interpolates between stored frames")
        md.append("  - Lower Morph FPS = more interpolation between frames")
        md.append("    Good for smooth organic motion: cloth, foliage, water")
        md.append("    Saves file size with no visible quality loss on slow motion")
        md.append("  - Higher Morph FPS = less interpolation, sharper frame timing")
        md.append("    Better for impacts, explosions, anything that needs precise frames")
        md.append("")
        md.append("  Verified from Volition originals:")
        md.append("    CTFbanner (25s cloth loop, 263 verts) = stored at 5fps = 194KB")
        md.append("    CTFbanner at full 15fps would be                         578KB")
        md.append("    Explosion debris = stored at 15fps for frame-perfect timing")
        md.append("")
        md.append("MORPH FPS SETTING:")
        md.append("  Export panel > Advanced Options > Morph FPS")
        md.append("  5fps  = long looping cloth/ambient effects (biggest saving)")
        md.append("  10fps = moderate animations")
        md.append("  15fps = short snappy effects, explosions, impacts")
        md.append("  The readout shows actual in-game duration at the chosen fps.")
        md.append("")
        md.append("SPEED STEP (in the bake dialog):")
        md.append("  Samples every Nth source frame, producing fewer shape keys.")
        md.append("  Reduces file size and engine overhead.")
        md.append("  The engine interpolates between the remaining frames,")
        md.append("  so playback speed is unchanged — only resolution is reduced.")
        md.append("  Dial Step up until the motion starts to look too choppy.")
        md.append("")
        md.append("BAKE DIALOG — LIVE READOUT:")
        md.append("  Shape keys to bake — number of morph frames that will be stored")
        md.append("  RF duration        — actual playback length in-game at chosen Morph FPS")
        md.append("  Blender duration   — source animation length in Blender")
        md.append("  Speed ratio warning — shown if RF will play noticeably differently")
        md.append("  Est. VFX file size — approximate output file size")
        md.append("")
        md.append("HOW TO CREATE MORPH ANIMATION:")
        md.append("  1. Animate your mesh using any Blender method:")
        md.append("     Shape keys, armatures, modifiers, cloth sim, etc.")
        md.append("  2. Set your timeline Start/End frame range")
        md.append("  3. Export panel > Bake Animation to Shape Keys")
        md.append("     Scans keyframes automatically, sets Start/End in the dialog.")
        md.append("     Adjust Speed Step if needed, then click OK.")
        md.append("  4. Export normally.")
        md.append("")
        md.append("TIPS:")
        md.append("  - Vertex count must stay constant across all frames")
        md.append("  - Lower vertex counts = smaller files + better performance")
        md.append("  - The bake captures world-space positions so rotation,")
        md.append("    constraints, and modifiers are all baked in correctly")
        md.append("")
        md.append("")

        # ── PARTICLES ────────────────────────────────────────────────
        md.append("PARTICLES")
        md.append("=" * 40)
        md.append("Particle emitters spawn sprite-based effects: sparks, smoke,")
        md.append("fire, drops. Each emitter becomes a PART section in the file.")
        md.append("")
        md.append("CREATING AN EMITTER:")
        md.append("  1. RF VFX sidebar > Particles panel > New Emitter")
        md.append("  2. An arrow Empty is created — tip = emit direction")
        md.append("  3. Position and rotate it where particles should spawn")
        md.append("  4. Set texture (Facing type) or leave blank (Drops type)")
        md.append("")
        md.append("PARTICLE TYPE:")
        md.append("  Facing — textured camera-facing billboards (fire, smoke, glow)")
        md.append("           requires a texture (.tga/.vbm/.dds)")
        md.append("  Drops  — untextured colored triangles (sparks, water, blood)")
        md.append("           uses material Diffuse color, no texture needed")
        md.append("")
        md.append("KEY SETTINGS:")
        md.append("  Spawn Delay     — time between spawns (lower = more particles)")
        md.append("  Velocity / +/-  — speed along emit direction + variation")
        md.append("  Decay / +/-     — particle lifetime in seconds + variation")
        md.append("  Emitter Radius  — spawn area radius (0 = point source)")
        md.append("  Random Dir      — cone spread in degrees (0 = focused beam)")
        md.append("  Gravity         — on/off downward pull")
        md.append("  Size at Birth   — grow-in fraction at start of life")
        md.append("  Size at Death   — shrink fraction at end of life")
        md.append("  Fade at Death   — opacity fade at end of life")
        md.append("")
        md.append("PARENTING:")
        md.append("  Parent the emitter to a mesh so particles follow it.")
        md.append("  Select emitter > Shift+click mesh > Ctrl+P > Object.")
        md.append("")
        md.append("")

        # ── DUMMIES ──────────────────────────────────────────────────
        md.append("DUMMIES (Attachment Points)")
        md.append("=" * 40)
        md.append("Dummies are invisible DMMY sections. The engine uses their")
        md.append("names for specific attachment behaviors.")
        md.append("")
        md.append("Use the Dummies panel to create them with one click.")
        md.append("The panel shows which mesh they will parent to — select")
        md.append("your mesh first before clicking a preset button.")
        md.append("")
        md.append("KNOWN DUMMY NAMES:")
        md.append("  $prop_flag    — CTF flag attach point (must parent to flag mesh)")
        md.append("  muzzle_1      — Weapon muzzle flash (directional)")
        md.append("  thruster      — Thruster VFX (auto-increments)")
        md.append("  corona        — Glare/corona position (auto-increments)")
        md.append("  chaingun_1    — Vehicle mounted weapon")
        md.append("  primary_1     — Primary weapon position")
        md.append("  secondary_1   — Secondary weapon position")
        md.append("  interface_1   — Player interaction/entry point")
        md.append("")
        md.append("VEHICLE COCKPIT HUD MESHES (engine-recognized names):")
        md.append("  A_D_01, A_D_02, A_D_03  — Armor digit quads (3 digits)")
        md.append("  P_D_01, P_D_02, P_D_03  — Primary ammo digits")
        md.append("  S_D_02, S_D_03, S_D_04, S_D_05  — Secondary ammo digits")
        md.append("  The engine updates these meshes with current ammo/armor values.")
        md.append("")
        md.append("")

        # ── EXPORT MODES ─────────────────────────────────────────────
        md.append("EXPORT MODES")
        md.append("=" * 40)
        md.append("  Auto (default) — Patch if a template is available,")
        md.append("                   True Export otherwise.")
        md.append("")
        md.append("  Patch          — Replaces mesh vertex data in an existing VFX.")
        md.append("                   Keeps header, materials, and internal tables.")
        md.append("                   Vertex count must be unchanged.")
        md.append("                   Auto-used when you imported a VFX first.")
        md.append("")
        md.append("  True Export    — Builds a complete new VFX from scratch.")
        md.append("                   Use when adding/removing meshes or starting fresh.")
        md.append("")
        md.append("")

        # ── ADVANCED OPTIONS ─────────────────────────────────────────
        md.append("ADVANCED EXPORT OPTIONS")
        md.append("=" * 40)
        md.append("Found under Export panel > Advanced Options toggle.")
        md.append("")
        md.append("  Export Mode    — Auto / Patch / True Export (see above)")
        md.append("")
        md.append("  Double Sided   — Duplicate all faces reversed so both sides")
        md.append("                   render in-game. Doubles face count.")
        md.append("                   (RF renders both sides natively — use only if")
        md.append("                   you specifically need explicit back-face geometry.)")
        md.append("")
        md.append("  Flip Faces     — Fix inside-out geometry if mesh renders")
        md.append("                   inverted in-game.")
        md.append("")
        md.append("  Selected Only  — Export only selected objects.")
        md.append("")
        md.append("  Morph FPS      — Storage sample rate for vertex animation.")
        md.append("                   5fps  = smooth looping cloth/foliage/water")
        md.append("                   10fps = moderate animations")
        md.append("                   15fps = sharp impacts, explosions")
        md.append("                   Engine interpolates between stored frames.")
        md.append("")
        md.append("")

        # ── LIMITS ───────────────────────────────────────────────────
        md.append("LIMITS AND REQUIREMENTS")
        md.append("=" * 40)
        md.append("  - All meshes must have UV maps")
        md.append("  - Textures: .tga, .vbm, or .dds in RF's data files")
        md.append("  - end_frame must be >= 5 (game engine minimum)")
        md.append("  - Vertex count must stay constant across all morph frames")
        md.append("  - Billboard and Rod cannot both be on the same mesh")
        md.append("  - Object names must be unique within the file")
        md.append("  - No hard vertex/face limits with Alpine Faction")
        md.append("    (be sensible — morph animation stores every vertex every frame)")
        md.append("")
        md.append("")

        # ── COORDINATE SYSTEM ────────────────────────────────────────
        md.append("COORDINATE SYSTEM")
        md.append("=" * 40)
        md.append("The add-on handles conversion automatically.")
        md.append("  Blender: X=right, Y=forward, Z=up")
        md.append("  RF:      X=side,  Y=up,      Z=forward")
        md.append("")
        md.append("Model in Blender as normal. The front of your object should")
        md.append("face Blender's -Y direction (toward camera in Front view).")
        md.append("")
        md.append("")

        # ── TROUBLESHOOTING ──────────────────────────────────────────
        md.append("TROUBLESHOOTING")
        md.append("=" * 40)
        md.append("  Invisible in-game")
        md.append("    Check texture exists in RF's data files (.tga/.vbm/.dds).")
        md.append("")
        md.append("  Inside-out faces")
        md.append("    Advanced Options > Flip Faces.")
        md.append("")
        md.append("  Wrong position / rotated incorrectly")
        md.append("    Apply all transforms before export: Ctrl+A > All Transforms.")
        md.append("    (The exporter forces this, but doing it manually is cleaner.)")
        md.append("")
        md.append("  Particles going the wrong direction")
        md.append("    Arrow tip = emit direction. Rotate the emitter empty.")
        md.append("")
        md.append("  Crash on load / VFX skipped")
        md.append("    Run Export panel > Readiness Check first.")
        md.append("    Ensure end_frame >= 5.")
        md.append("")
        md.append("  Mesh too dark")
        md.append("    Enable Fullbright flag for glowing/emissive effects.")
        md.append("")
        md.append("  Animation too slow or cuts off")
        md.append("    Re-bake after changing Speed Step or Morph FPS.")
        md.append("    scene frame_end is updated automatically after each bake.")
        md.append("")
        md.append("  $prop_flag not attaching to player")
        md.append("    The dummy must be parented to the flag mesh, not Scene Root.")
        md.append("")
        md.append("  File too large")
        md.append("    Lower Morph FPS to 5 or 10 in Advanced Options.")
        md.append("    Increase Speed Step in the bake dialog.")
        md.append("    The engine interpolates between stored frames smoothly.")
        md.append("")
        md.append("")
        md.append("=" * 72)
        md.append("  RF VFX TOOLS v0.7.0 — Binary format verified against 61 Volition")
        md.append("  original VFX files. Community: discord.gg/factionfiles")
        md.append("  Alpine Faction: github.com/GooberRF/alpinefaction")
        md.append("=" * 72)

        t = _get_or_create_text("RF_VFX_AUTHORING_GUIDE")
        t.clear()
        t.write("\n".join(md) + "\n")

        scr = getattr(bpy.context, "screen", None)
        if scr:
            for area in scr.areas:
                if area.type == "TEXT_EDITOR":
                    area.spaces.active.text = t
                    break

        self.report({"INFO"}, "Guide written to 'RF_VFX_AUTHORING_GUIDE'. Open Text Editor to read.")
        return {"FINISHED"}
# ── Sub-panel system ──────────────────────────────────────────────
# Main header panel + collapsible sub-panels for each workflow area.
# Keeps the sidebar clean: users expand only what they need.

class RFVFX_PT_Workflows(bpy.types.Panel):
    bl_label = "Workflows"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "RF VFX"
    bl_parent_id = "RFVFX_PT_panel"
    bl_options = {"DEFAULT_CLOSED"}
    bl_description = "Quick reference for common workflows — Create, Edit, Animate, Particles, and Export options."

    def draw(self, context):
        layout = self.layout

        layout.operator("rfvfx.write_authoring_guide", text="Open Full Guide", icon="HELP")
        layout.separator()

        # ── Create ──
        box = layout.box()
        b = box.column(align=True)
        b.scale_y = 0.82
        b.label(text="CREATE A NEW VFX", icon="FILE_NEW")
        b.label(text="  1. Click 'New RF VFX Scene'  (recommended)")
        b.label(text="  2. Model mesh + assign texture")
        b.label(text="  3. Click 'Add Selected To RF_VFX'")
        b.label(text="  4. Set flags if needed  (optional)")
        b.label(text="  5. Set output path \u2192 Export VFX")

        # ── Edit ──
        box2 = layout.box()
        b2 = box2.column(align=True)
        b2.scale_y = 0.82
        b2.label(text="EDIT AN EXISTING VFX", icon="FILE_TICK")
        b2.label(text="  1. Click 'Import VFX' \u2192 edit mesh")
        b2.label(text="  2. Export \u2192 Patch mode auto-detects")
        b2.label(text="  Vertex count must stay the same")

        # ── Morph ──
        box3 = layout.box()
        b3 = box3.column(align=True)
        b3.scale_y = 0.82
        b3.label(text="MORPH ANIMATION", icon="SHAPEKEY_DATA")
        b3.label(text="  1. Animate mesh (shape keys, modifiers, etc.)")
        b3.label(text="  2. Click 'Bake Animation to Shape Keys'")
        b3.label(text="  3. Set Morph FPS in Advanced Options")
        b3.label(text="     5fps \u2192 cloth, foliage  (smooth, less RAM)")
        b3.label(text="     15fps \u2192 explosions, impacts")
        b3.label(text="  4. Click 'Export VFX'")

        # ── Import map reference ──
        box4 = layout.box()
        b4 = box4.column(align=True)
        b4.scale_y = 0.82
        b4.label(text="IMPORT MAP REFERENCE", icon="MESH_DATA")
        b4.label(text="  Import RFG or WRL to use as a reference")
        b4.label(text="  when building VFX for a specific map location.")
        b4.label(text="  New VFX / Import panel \u2192 Import RFG or WRL")
        b4.label(text="  Tick 'Import at Origin' for VFX building.")

        # ── Particles ──
        box5 = layout.box()
        b5 = box5.column(align=True)
        b5.scale_y = 0.82
        b5.label(text="ADD PARTICLES", icon="PARTICLES")
        b5.label(text="  1. Particles panel \u2192 Click 'New Emitter'")
        b5.label(text="  2. Position + rotate the arrow")
        b5.label(text="     Arrow tip = emit direction")
        b5.label(text="  3. Set texture + adjust settings")
        b5.label(text="  4. Export  (included automatically)")


class RFVFX_PT_ImportScene(bpy.types.Panel):
    bl_label = "New VFX / Import"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "RF VFX"
    bl_parent_id = "RFVFX_PT_panel"
    bl_options = set()
    bl_description = "Start a new VFX file, add objects to the scene, or import an existing .vfx to edit."

    def draw(self, context):
        layout = self.layout
        try:
            s = context.scene.rfvfx

            col = layout.column(align=True)
            col.scale_y = 1.1
            col.operator("rfvfx.new_authoring_scene", text="New RF VFX Scene", icon="FILE_NEW")
            col.operator("rfvfx.add_selected_to_rf", text="Add Selected To RF_VFX", icon="ADD")
            col.separator()
            col.operator("rfvfx.import_vfx", text="Import VFX...", icon="IMPORT")
            col.operator("rfvfx.import_rfg", text="Import RFG Map...", icon="MESH_DATA")
            col.operator("rfvfx.import_wrl", text="Import RED VRML Map...", icon="WORLD")
            col.operator("rfvfx.import_quake_model", text="Import Quake Model (MDL/MD2)...", icon="OBJECT_DATA")

            if s.import_vfx.strip():
                import os as _os
                fn = _os.path.basename(bpy.path.abspath(s.import_vfx))
                row = layout.row()
                row.scale_y = 0.7
                row.label(text=f"Last: {fn}", icon="FILE_TICK")

        except Exception as e:
            layout.label(text=f"Settings error: {e}", icon="ERROR")





# ── RFG IMPORTER ─────────────────────────────────────────────────────────────

def _rfg_read_vstring(f):
    """Read a VString: uint16 length prefix + ASCII bytes."""
    raw = f.read(2)
    if len(raw) < 2:
        return ""
    length = struct.unpack_from("<H", raw)[0]
    if length == 0 or length == 0xFFFF:
        return ""
    return f.read(length).decode("ascii", errors="replace")

def _rfg_rf_to_blender(rx, ry, rz):
    """RF Y-up → Blender Z-up direct (no glTF intermediary).
    RFG import builds bmesh directly, so we apply the full
    RF→Blender conversion here: (-rx, -rz, ry). This is equivalent
    to (negate-X to glTF Y-up RH) composed with (Y-up→Z-up rotation).
    Aligned with RF Static Mesh Tools and RF Character Tools."""
    return (-rx, -rz, ry)

def _rfg_parse(filepath):
    """Parse an RFG file and return a list of brush dicts.
    Each brush dict has: uid, position, rotation_matrix, textures, faces
    where faces = list of {texture_idx, verts: [(x,y,z)], uvs: [(u,v)]}
    """
    import struct as _struct

    brushes = []
    RF1_VERSION_MIN = 0x000000C8  # 200 - oldest known RF1 RFG version

    with open(filepath, "rb") as f:

        # ── Header ──
        magic = _struct.unpack("<I", f.read(4))[0]
        if magic != 0xD43DD00D:
            raise ValueError(f"Invalid RFG magic: 0x{magic:08X} (expected 0xD43DD00D)")

        version = _struct.unpack("<i", f.read(4))[0]
        num_groups = _struct.unpack("<i", f.read(4))[0]

        # RF1 RFG versions range from 0xC8 (200) to 0x12C (300)
        # All known RFG files from RF1 use RF1 format
        is_rf1 = True
        is_rf2 = False

        for _g in range(num_groups):

            # Group name + isMoving flag
            _rfg_read_vstring(f)
            is_moving = _struct.unpack("B", f.read(1))[0]
            if is_moving:
                # Moving groups have complex keyframe data — skip by reading
                # brush count as 0 guard; in practice RFG files don't have these
                pass

            num_brushes = _struct.unpack("<i", f.read(4))[0]

            for _b in range(num_brushes):
                brush = {"uid": 0, "position": (0,0,0), "rotation": None,
                         "textures": [], "faces": []}

                # ── Brush UID ──
                brush["uid"] = _struct.unpack("<i", f.read(4))[0]

                # ── Unknown 4 bytes after UID (flags/life field) ──
                f.read(4)

                # ── Position ──
                px, py, pz = _struct.unpack("<3f", f.read(12))
                brush["position"] = _rfg_rf_to_blender(px, py, pz)

                # ── 1 byte before rotation (modifiability flag) ──
                f.read(1)

                # ── Rotation matrix (fwd, right, up — each a 3-float row) ──
                fwd   = _struct.unpack("<3f", f.read(12))
                right = _struct.unpack("<3f", f.read(12))
                up    = _struct.unpack("<3f", f.read(12))
                brush["rotation"] = (right, up, fwd)

                # ── Geometry body ──
                # Skip version-specific unknown uint + modifiability (RF1 >= 0xC8)
                if is_rf1 and version >= 0xC8:
                    f.read(8)

                _rfg_read_vstring(f)  # geo name (usually blank)

                # Skip unknown (RF2) or old modifiability (RF1 < 0xC8)
                if is_rf2 or (is_rf1 and version < 0xC8):
                    f.read(4)

                # ── Textures ──
                num_textures = _struct.unpack("<i", f.read(4))[0]
                textures = []
                for _t in range(num_textures):
                    textures.append(_rfg_read_vstring(f))
                brush["textures"] = textures

                # ── Face scroll data (RF1 only) ──
                if is_rf1 and version >= 0xB4:
                    num_scroll = _struct.unpack("<i", f.read(4))[0]
                    f.read(num_scroll * 12)  # faceId(4) + uVel(4) + vVel(4)
                elif is_rf1 and version < 0xB4:
                    num_unk = _struct.unpack("<i", f.read(4))[0]
                    f.read(num_unk * 0x29)

                # ── Room data ──
                num_rooms = _struct.unpack("<i", f.read(4))[0]
                for _r in range(num_rooms):
                    if is_rf2:
                        f.read(4 + 24 + 4 + 4 + 16 + 4 + 4)  # id, aabb, 4 bytes, life, eax_name...
                        _rfg_read_vstring(f)
                        f.read(4 * 7)
                    else:
                        f.read(4 + 24 + 8)  # id, aabb, 8 flag bytes
                        is_liquid_room = False
                        has_ambient = False
                        # Re-read carefully: id(4) + aabbMin(12) + aabbMax(12) + 8 flag bytes + life(4)
                        # We already read 4+24+8=36 bytes, need to read life and conditional data
                        # Back up and re-read correctly
                        f.seek(-36, 1)
                        _room_id = _struct.unpack("<i", f.read(4))[0]
                        f.read(24)  # aabb
                        _sky = _struct.unpack("B", f.read(1))[0]
                        _cold = _struct.unpack("B", f.read(1))[0]
                        _outside = _struct.unpack("B", f.read(1))[0]
                        _airlock = _struct.unpack("B", f.read(1))[0]
                        is_liquid_room = _struct.unpack("B", f.read(1))[0]
                        has_ambient = _struct.unpack("B", f.read(1))[0]
                        _subroom = _struct.unpack("B", f.read(1))[0]
                        _alpha = _struct.unpack("B", f.read(1))[0]
                        f.read(4)  # life
                        if version >= 0xB4:
                            _rfg_read_vstring(f)  # eax effect
                        if is_liquid_room:
                            # depth(4) + color(4) + surfaceTex(vstr) + visibility(4) +
                            # liquidType(4) + liquidAlpha(4) + plankton(1) +
                            # ppmU(4) + ppmV(4) + angle(4) + waveform(4) + scrollU(4) + scrollV(4)
                            f.read(4 + 4)
                            _rfg_read_vstring(f)
                            f.read(4 + 4 + 4 + 1 + 4 + 4 + 4 + 4 + 4 + 4)
                        if has_ambient:
                            f.read(4)  # RGBA

                # ── Subroom links ──
                num_subroom_links = _struct.unpack("<i", f.read(4))[0]
                for _sl in range(num_subroom_links):
                    f.read(4)  # roomID
                    sub_count = _struct.unpack("<i", f.read(4))[0]
                    f.read(sub_count * 4)

                # ── RF2 uroom links ──
                if is_rf2:
                    num_uroom = _struct.unpack("<i", f.read(4))[0]
                    f.read(num_uroom * 8)

                # ── Portals ──
                num_portals = _struct.unpack("<i", f.read(4))[0]
                f.read(num_portals * 32)

                # ── Raw vertices ──
                num_raw_verts = _struct.unpack("<i", f.read(4))[0]
                raw_verts = []
                for _v in range(num_raw_verts):
                    raw_verts.append(_struct.unpack("<3f", f.read(12)))

                # ── Faces ──
                num_faces = _struct.unpack("<i", f.read(4))[0]
                faces = []

                for _fi in range(num_faces):
                    f.read(16)  # plane normal + dist
                    tex_idx = _struct.unpack("<i", f.read(4))[0]
                    _surface_idx = _struct.unpack("<i", f.read(4))[0]
                    _face_id = _struct.unpack("<i", f.read(4))[0]
                    _unk12 = _struct.unpack("<i", f.read(4))[0]

                    if is_rf1:
                        f.read(4)   # reserved1
                        f.read(4)   # portal_index
                        face_flags = _struct.unpack("<H", f.read(2))[0]
                        f.read(2)   # reserved2
                        f.read(4)   # smoothingGroups
                        f.read(4)   # roomIndex
                        vert_count = _struct.unpack("<i", f.read(4))[0]

                        FLAG_INVISIBLE  = 0x2000
                        FLAG_FULLBRIGHT = 0x0020
                        FLAG_HOLE       = 0x0080
                        is_invisible = bool(face_flags & FLAG_INVISIBLE)
                        is_fullbright = bool(face_flags & FLAG_FULLBRIGHT)
                        is_hole = bool(face_flags & FLAG_HOLE)

                        face_verts = []
                        face_uvs = []
                        for _vi in range(vert_count):
                            raw_idx = _struct.unpack("<i", f.read(4))[0]
                            u = _struct.unpack("<f", f.read(4))[0]
                            v = _struct.unpack("<f", f.read(4))[0]
                            # RFG static geometry has lmap coords when not fullbright/invisible
                            # (static_geo=False for brushes, so no lmap coords here)
                            if raw_idx < 0 or raw_idx >= len(raw_verts):
                                continue
                            face_verts.append(raw_verts[raw_idx])
                            # RF V is flipped relative to Blender
                            face_uvs.append((u, 1.0 - v))

                        # Skip invisible and hole faces (usually collision/portal geometry)
                        if is_invisible or is_hole:
                            continue
                        if len(face_verts) >= 3:
                            faces.append({
                                "texture_idx": max(0, min(tex_idx, len(textures) - 1)),
                                "verts": face_verts,
                                "uvs": face_uvs,
                            })
                    else:
                        # RF2
                        face_flags_rf2 = _struct.unpack("<I", f.read(4))[0]
                        f.read(4)  # smoothingGroups
                        INLINE_SCROLL = 0x00008000
                        if face_flags_rf2 & INLINE_SCROLL:
                            f.read(8)  # scroll period U + V
                        if version >= 0x127:
                            f.read(3 + 4)  # 3 unk bytes + flagDecider float
                        f.read(4)  # roomIndex
                        vert_count = _struct.unpack("<i", f.read(4))[0]

                        INVISIBLE_RF2 = 0x2000
                        HOLE_RF2      = 0x0080
                        is_invisible = bool(face_flags_rf2 & INVISIBLE_RF2)
                        is_hole = bool(face_flags_rf2 & HOLE_RF2)

                        face_verts = []
                        face_uvs = []
                        for _vi in range(vert_count):
                            raw_idx = _struct.unpack("<I", f.read(4))[0]
                            u = _struct.unpack("<f", f.read(4))[0]
                            v = _struct.unpack("<f", f.read(4))[0]
                            f.read(4)  # vertex colour RGBA
                            if raw_idx >= len(raw_verts):
                                continue
                            face_verts.append(raw_verts[raw_idx])
                            face_uvs.append((u, 1.0 - v))

                        if is_invisible or is_hole:
                            continue
                        if len(face_verts) >= 3:
                            faces.append({
                                "texture_idx": max(0, min(tex_idx, len(textures) - 1)),
                                "verts": face_verts,
                                "uvs": face_uvs,
                            })

                # ── Skip surfaces (RF1 only) ──
                if is_rf1:
                    num_surfaces = _struct.unpack("<i", f.read(4))[0]
                    f.read(num_surfaces * 96)

                # ── Skip old face scroll data (RF1 <= 0xB4) ──
                if is_rf1 and version <= 0xB4:
                    num_old_scroll = _struct.unpack("<i", f.read(4))[0]
                    f.read(num_old_scroll * 12)

                # ── Brush flags / life / state ──
                if f.tell() + 12 <= os.path.getsize(filepath):
                    f.read(12)  # flags(4) + life(4) + state(4)

                    # unk_c extra data (bits 2+3 both set = liquid brush)
                    # We already read flags — re-check would need storing them
                    # For safety just try to parse the remaining section fields
                    # (redux reads them if UNK_C_MASK == 0x000C is set)
                    # We skip this since it's liquid-specific and rare

                brush["faces"] = faces
                brushes.append(brush)

            # Skip the rest of the group sections
            def _safe_skip_count_entries(f, entry_size):
                raw = f.read(4)
                if len(raw) < 4: return
                count = _struct.unpack("<i", raw)[0]
                if count > 0:
                    f.read(count * entry_size)

            def _skip_vstring_list(f):
                raw = f.read(4)
                if len(raw) < 4: return
                count = _struct.unpack("<i", raw)[0]
                for _ in range(count):
                    _rfg_read_vstring(f)

            # SkipGeoRegions: count(4) + count*4 + 4
            raw = f.read(4)
            if len(raw) == 4:
                gr_count = _struct.unpack("<i", raw)[0]
                f.read(4 + gr_count * 4)

            _safe_skip_count_entries(f, 100)  # lights
            _safe_skip_count_entries(f, 84)   # cutscene cameras
            _safe_skip_count_entries(f, 84)   # cutscene path nodes
            _safe_skip_count_entries(f, 48)   # ambient sounds
            _safe_skip_count_entries(f, 112)  # events
            _safe_skip_count_entries(f, 80)   # mp respawn points
            _safe_skip_count_entries(f, 100)  # nav points
            _safe_skip_count_entries(f, 220)  # entities
            _safe_skip_count_entries(f, 100)  # items
            _safe_skip_count_entries(f, 100)  # clutters
            _safe_skip_count_entries(f, 120)  # triggers
            _safe_skip_count_entries(f, 180)  # particle emitters
            _safe_skip_count_entries(f, 84)   # gas regions
            _safe_skip_count_entries(f, 80)   # decals
            _safe_skip_count_entries(f, 64)   # climbing regions
            _safe_skip_count_entries(f, 64)   # room effects
            _safe_skip_count_entries(f, 80)   # eax effects
            _safe_skip_count_entries(f, 100)  # bolt emitters
            _safe_skip_count_entries(f, 64)   # targets
            _safe_skip_count_entries(f, 64)   # push regions

    return brushes


def _rfg_build_mesh(brush, collection, import_at_origin=False):
    """Build a Blender mesh object from a parsed RFG brush dict.
    Returns the created object."""

    textures = brush["textures"]
    faces = brush["faces"]
    if not faces:
        return None

    pos = brush["position"]  # brush world origin already in Blender space
    use_pos = not import_at_origin

    def transform_vert(rv):
        """Convert RF local-space vert to Blender world space."""
        bv = _rfg_rf_to_blender(rv[0], rv[1], rv[2])
        if use_pos:
            return (bv[0] + pos[0], bv[1] + pos[1], bv[2] + pos[2])
        return bv

    # Build flat vert/face lists for bmesh
    verts = []
    vert_key_map = {}
    poly_vert_lists = []
    poly_uv_lists = []
    poly_mat_indices = []

    # Material index per texture
    mat_index_map = {}

    for face in faces:
        tidx = face["texture_idx"]
        tex_name = textures[tidx] if tidx < len(textures) else ""

        if tex_name not in mat_index_map:
            mat_index_map[tex_name] = len(mat_index_map)

        poly_vis = []
        poly_uvs_f = []
        for rv, uv in zip(face["verts"], face["uvs"]):
            wv = transform_vert(rv)
            key = (round(wv[0]*1000), round(wv[1]*1000), round(wv[2]*1000))
            if key not in vert_key_map:
                vert_key_map[key] = len(verts)
                verts.append(wv)
            poly_vis.append(vert_key_map[key])
            poly_uvs_f.append(uv)

        poly_vert_lists.append(poly_vis)
        poly_uv_lists.append(poly_uvs_f)
        poly_mat_indices.append(mat_index_map[tex_name])

    if not verts:
        return None

    # Create mesh
    name = f"Brush_{brush['uid']:04d}"
    mesh_data = bpy.data.meshes.new(name)
    mesh_data.from_pydata(verts, [], poly_vert_lists)
    mesh_data.update()

    # UV layer
    uv_layer = mesh_data.uv_layers.new(name="UVMap")
    loop_idx = 0
    for poly in mesh_data.polygons:
        for li in range(poly.loop_start, poly.loop_start + poly.loop_total):
            fi = poly.index
            vi_in_poly = li - poly.loop_start
            if fi < len(poly_uv_lists) and vi_in_poly < len(poly_uv_lists[fi]):
                uv_layer.data[li].uv = poly_uv_lists[fi][vi_in_poly]

    # Materials — one per texture name
    for tex_name, _mi in sorted(mat_index_map.items(), key=lambda x: x[1]):
        mat = bpy.data.materials.get(tex_name)
        if mat is None:
            mat = bpy.data.materials.new(name=tex_name)
            mat.use_nodes = True
            # Set up a basic Principled BSDF with the texture name label
            bsdf = mat.node_tree.nodes.get("Principled BSDF")
            if bsdf:
                mat.node_tree.nodes["Principled BSDF"].label = tex_name
            # Store texture name in rfvfx_props so it exports correctly
            if hasattr(mat, "rfvfx_props"):
                mat.rfvfx_props.texture_name = tex_name
        mesh_data.materials.append(mat)

    # Assign material indices per polygon
    for poly in mesh_data.polygons:
        fi = poly.index
        if fi < len(poly_mat_indices):
            poly.material_index = poly_mat_indices[fi]

    # Create object
    obj = bpy.data.objects.new(name, mesh_data)
    collection.objects.link(obj)

    return obj


# ── WRL IMPORTER ──────────────────────────────────────────────────────────────

def _wrl_parse(filepath):
    """Parse a VRML 2.0 file exported from RED Editor.
    Returns (shapes, export_type) where shapes is a list of dicts:
      {name, vertices [(x,y,z)...], faces [(i,j,k...)...], color (r,g,b)}
    export_type is 'web_browser' or '3ds_max'.
    """
    import re as _re

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    if not content.lstrip().startswith("#VRML V2.0"):
        raise ValueError("Not a VRML 2.0 file")

    export_type = "3ds_max" if "scale 39.21569" in content else "web_browser"

    shape_re = _re.compile(
        r"Shape\s*\{.*?"
        r"diffuseColor\s+([\d.]+)\s+([\d.]+)\s+([\d.]+).*?"
        r"geometry\s+DEF\s+(\S+)-FACES\s+IndexedFaceSet\s*\{.*?"
        r"point\s*\[(.*?)\].*?"
        r"coordIndex\s*\[(.*?)\]",
        _re.DOTALL
    )

    shapes = []
    for m in shape_re.finditer(content):
        r, g, b = float(m.group(1)), float(m.group(2)), float(m.group(3))
        name = m.group(4)
        pts_raw = m.group(5).strip()
        idx_raw = m.group(6).strip()

        if not pts_raw or not idx_raw:
            continue

        verts = []
        for line in pts_raw.split("\n"):
            line = line.strip().rstrip(",")
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 3:
                try:
                    verts.append((float(parts[0]), float(parts[1]), float(parts[2])))
                except ValueError:
                    pass

        faces = []
        for face_str in idx_raw.split("-1"):
            indices = []
            for tok in face_str.replace(",", " ").split():
                tok = tok.strip()
                if tok:
                    try:
                        indices.append(int(tok))
                    except ValueError:
                        pass
            if len(indices) >= 3:
                faces.append(tuple(indices))

        if verts and faces:
            shapes.append({"name": name, "vertices": verts,
                           "faces": faces, "color": (r, g, b)})

    return shapes, export_type


class RFVFX_OT_ImportWRL(bpy.types.Operator):
    bl_idname = "rfvfx.import_wrl"
    bl_label = "Import RED VRML Map..."
    bl_description = (
        "Import level geometry exported from RED Editor as VRML (.wrl). "
        "Handles RF coordinate conversion automatically. "
        "Use RED: File > Export > VRML to generate the file."
    )
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.wrl", options={"HIDDEN"})

    merge_by_texture: BoolProperty(
        name="Merge Shapes by Color",
        default=False,
        description=(
            "Merge all shapes that share the same diffuseColor into one mesh. "
            "WRL files from RED use color per brush, so this reduces object count."
        )
    )
    import_into_rf_vfx: BoolProperty(
        name="Import into RF_VFX Collection",
        default=False,
        description=(
            "Place imported geometry into the RF_VFX export collection. "
            "Leave off to import into a separate WRL collection for reference only."
        )
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "merge_by_texture")
        layout.separator()
        layout.prop(self, "import_into_rf_vfx")

    def execute(self, context):
        filepath = bpy.path.abspath(self.filepath).strip()
        if not filepath or not os.path.isfile(filepath):
            self.report({"ERROR"}, "Pick a valid .wrl file.")
            return {"CANCELLED"}

        try:
            shapes, export_type = _wrl_parse(filepath)
        except Exception as e:
            self.report({"ERROR"}, f"WRL parse failed: {e}")
            return {"CANCELLED"}

        if not shapes:
            self.report({"WARNING"}, "No geometry found in WRL file.")
            return {"CANCELLED"}

        # Target collection
        if self.import_into_rf_vfx:
            col = bpy.data.collections.get("RF_VFX")
            if col is None:
                col = bpy.data.collections.new("RF_VFX")
                context.scene.collection.children.link(col)
        else:
            base = os.path.splitext(os.path.basename(filepath))[0]
            col_name = f"WRL_{base}"
            col = bpy.data.collections.get(col_name)
            if col is None:
                col = bpy.data.collections.new(col_name)
                context.scene.collection.children.link(col)

        import bmesh as _bmesh

        def _wrl_vert_to_blender(x, y, z):
            # WRL from RED stores verts as (RF_X, RF_Z, -RF_Y)
            # so: RF_X=x, RF_Y=-z, RF_Z=y
            # Apply new rf_to_blender(-rx,-rz,ry):
            # = (-RF_X, -RF_Z, RF_Y) = (-x, -y, -z)
            return (-x, -y, -z)

        def _make_mesh(name, verts_raw, faces, color):
            verts = [_wrl_vert_to_blender(*v) for v in verts_raw]
            mesh = bpy.data.meshes.new(name + "_mesh")
            obj = bpy.data.objects.new(name, mesh)
            col.objects.link(obj)

            bm = _bmesh.new()
            bm_verts = [bm.verts.new(v) for v in verts]
            bm.verts.ensure_lookup_table()
            skipped = 0
            for face_indices in faces:
                try:
                    # WRL stores CW; the new RF↔Blender conversion has det=-1,
                    # so the coord transform already flips winding to CCW for Blender.
                    bm.faces.new([bm_verts[i] for i in face_indices])
                except (IndexError, ValueError):
                    skipped += 1
            bm.to_mesh(mesh)
            bm.free()
            mesh.update()

            mat = bpy.data.materials.new(name + "_mat")
            mat.use_nodes = True
            mat.use_backface_culling = False
            bsdf = mat.node_tree.nodes.get("Principled BSDF")
            if bsdf:
                bsdf.inputs["Base Color"].default_value = (*color, 1.0)
                bsdf.inputs["Roughness"].default_value = 0.8
            mesh.materials.append(mat)
            return obj, skipped

        total_verts = 0
        total_faces = 0
        total_skipped = 0
        created = 0

        if self.merge_by_texture:
            # Group by color
            color_map = {}
            for shape in shapes:
                key = (round(shape["color"][0]*1000),
                       round(shape["color"][1]*1000),
                       round(shape["color"][2]*1000))
                if key not in color_map:
                    color_map[key] = {"verts": [], "faces": [], "color": shape["color"]}
                offset = len(color_map[key]["verts"])
                color_map[key]["verts"].extend(shape["vertices"])
                for face in shape["faces"]:
                    color_map[key]["faces"].append(tuple(i + offset for i in face))

            for ki, (key, data) in enumerate(color_map.items()):
                name = f"WRL_color_{ki:03d}"
                obj, skipped = _make_mesh(name, data["verts"], data["faces"], data["color"])
                total_verts += len(data["verts"])
                total_faces += len(data["faces"])
                total_skipped += skipped
                created += 1
        else:
            for shape in shapes:
                obj, skipped = _make_mesh(shape["name"], shape["vertices"],
                                          shape["faces"], shape["color"])
                total_verts += len(shape["vertices"])
                total_faces += len(shape["faces"])
                total_skipped += skipped
                created += 1

        msg = (
            f"Imported {created} object(s) from {os.path.basename(filepath)} "
            f"[{export_type}]: {total_verts} verts, {total_faces} faces."
        )
        if total_skipped:
            msg += f" {total_skipped} degenerate faces skipped."
        self.report({"INFO"}, msg)
        return {"FINISHED"}


class RFVFX_OT_ImportRFG(bpy.types.Operator):
    bl_idname = "rfvfx.import_rfg"
    bl_label = "Import RFG Map..."
    bl_description = (
        "Import an RF map geometry file (.rfg) into Blender as mesh objects. "
        "Each brush becomes a separate mesh. Textures are referenced by name — "
        "assign images manually or use a texture folder scan after import."
    )
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.rfg", options={"HIDDEN"})

    merge_by_texture: BoolProperty(
        name="Merge Brushes by Texture",
        default=True,
        description=(
            "Merge all brushes that share the same texture into a single mesh object. "
            "Greatly reduces object count for large maps. Disable to keep each brush separate."
        )
    )
    skip_invisible: BoolProperty(
        name="Skip Invisible Faces",
        default=True,
        description="Skip faces flagged as invisible or hole (collision geometry). Usually safe to leave on."
    )
    import_into_rf_vfx: BoolProperty(
        name="Import into RF_VFX Collection",
        default=False,
        description=(
            "Place imported geometry into the RF_VFX export collection. "
            "Use this if you want to export it as a VFX. "
            "Leave off to import into a separate RFG collection for reference only."
        )
    )
    import_at_origin: BoolProperty(
        name="Import at Origin",
        default=False,
        description=(
            "Ignore brush world positions and import all geometry centred at the Blender origin. "
            "Useful when importing a single brush as a VFX reference — avoids it appearing "
            "thousands of units away from the origin. Leave off for full map imports."
        )
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "merge_by_texture")
        layout.prop(self, "skip_invisible")
        layout.separator()
        layout.prop(self, "import_into_rf_vfx")
        layout.prop(self, "import_at_origin")

    def execute(self, context):
        filepath = bpy.path.abspath(self.filepath).strip()
        if not filepath or not os.path.isfile(filepath):
            self.report({"ERROR"}, "Pick a valid .rfg file.")
            return {"CANCELLED"}

        try:
            brushes = _rfg_parse(filepath)
        except Exception as e:
            self.report({"ERROR"}, f"RFG parse failed: {e}")
            import traceback; traceback.print_exc()
            return {"CANCELLED"}

        if not brushes:
            self.report({"WARNING"}, "No brushes found in RFG file.")
            return {"CANCELLED"}

        # Target collection
        if self.import_into_rf_vfx:
            col = bpy.data.collections.get("RF_VFX")
            if col is None:
                col = bpy.data.collections.new("RF_VFX")
                context.scene.collection.children.link(col)
        else:
            base = os.path.splitext(os.path.basename(filepath))[0]
            col_name = f"RFG_{base}"
            col = bpy.data.collections.get(col_name)
            if col is None:
                col = bpy.data.collections.new(col_name)
                context.scene.collection.children.link(col)

        if self.merge_by_texture:
            # Collect all faces grouped by texture name across all brushes
            tex_faces = {}   # tex_name -> list of (world_verts, uvs)
            all_textures = set()

            use_pos = not self.import_at_origin
            for brush in brushes:
                textures = brush["textures"]
                pos = brush["position"]  # brush world origin in Blender space

                for face in brush["faces"]:
                    tidx = face["texture_idx"]
                    tex_name = textures[tidx] if tidx < len(textures) else "no_texture"
                    all_textures.add(tex_name)

                    world_verts = []
                    for rv in face["verts"]:
                        bv = _rfg_rf_to_blender(rv[0], rv[1], rv[2])
                        if use_pos:
                            world_verts.append((bv[0] + pos[0], bv[1] + pos[1], bv[2] + pos[2]))
                        else:
                            world_verts.append(bv)

                    if tex_name not in tex_faces:
                        tex_faces[tex_name] = []
                    tex_faces[tex_name].append((world_verts, face["uvs"]))

            # Build one mesh per texture
            created = 0
            for tex_name, face_list in tex_faces.items():
                verts = []
                vert_key_map = {}
                polys = []
                poly_uvs = []

                for world_verts, uvs in face_list:
                    poly_vis = []
                    for wv, uv in zip(world_verts, uvs):
                        key = (round(wv[0]*1000), round(wv[1]*1000), round(wv[2]*1000))
                        if key not in vert_key_map:
                            vert_key_map[key] = len(verts)
                            verts.append(wv)
                        poly_vis.append(vert_key_map[key])
                    polys.append(poly_vis)
                    poly_uvs.append(uvs)

                if not verts:
                    continue

                safe_name = tex_name.replace("/", "_").replace("\\", "_")
                mesh_data = bpy.data.meshes.new(safe_name)
                mesh_data.from_pydata(verts, [], polys)
                mesh_data.update()

                uv_layer = mesh_data.uv_layers.new(name="UVMap")
                for poly in mesh_data.polygons:
                    fi = poly.index
                    for li in range(poly.loop_start, poly.loop_start + poly.loop_total):
                        vi_in_poly = li - poly.loop_start
                        if fi < len(poly_uvs) and vi_in_poly < len(poly_uvs[fi]):
                            uv_layer.data[li].uv = poly_uvs[fi][vi_in_poly]

                # Material
                mat = bpy.data.materials.get(tex_name)
                if mat is None:
                    mat = bpy.data.materials.new(name=tex_name)
                    mat.use_nodes = True
                    if hasattr(mat, "rfvfx_props"):
                        mat.rfvfx_props.texture_name = tex_name
                mesh_data.materials.append(mat)
                for poly in mesh_data.polygons:
                    poly.material_index = 0

                obj = bpy.data.objects.new(safe_name, mesh_data)
                col.objects.link(obj)
                created += 1

            self.report({"INFO"}, f"Imported {len(brushes)} brushes → {created} objects (merged by texture) from {os.path.basename(filepath)}")

        else:
            # One object per brush
            created = 0
            for brush in brushes:
                obj = _rfg_build_mesh(brush, col, import_at_origin=self.import_at_origin)
                if obj:
                    created += 1

            self.report({"INFO"}, f"Imported {created} brush objects from {os.path.basename(filepath)}")

        # Frame all imported objects
        try:
            bpy.ops.object.select_all(action="DESELECT")
            for obj in col.objects:
                obj.select_set(True)
            context.view_layer.objects.active = next(iter(col.objects), None)
            bpy.ops.view3d.view_selected(use_all_regions=False)
        except Exception:
            pass

        return {"FINISHED"}

class RFVFX_OT_BrowseMaterialTextureSlot(bpy.types.Operator):
    """Browse for a texture file and assign it to a specific material slot."""
    bl_idname = "rfvfx.browse_material_texture_slot"
    bl_label = "Browse"
    bl_description = "Pick a texture file (.tga/.vbm/.dds) from disk — only the filename is stored"

    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.tga;*.vbm;*.dds", options={"HIDDEN"})
    slot_index: IntProperty(default=0, options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == "MESH"

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        import os
        obj = context.active_object
        if not obj or self.slot_index >= len(obj.material_slots):
            return {"CANCELLED"}
        mat = obj.material_slots[self.slot_index].material
        if not mat:
            return {"CANCELLED"}
        name = os.path.basename(self.filepath)
        mat.rfvfx_props.texture_name = name
        self.report({"INFO"}, f"Slot {self.slot_index}: texture set to '{name}'")
        return {"FINISHED"}


class RFVFX_PT_Materials(bpy.types.Panel):
    bl_label = "Materials"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "RF VFX"
    bl_parent_id = "RFVFX_PT_panel"
    bl_options = set()
    bl_description = (
        "Set the RF texture filename for each material slot. "
        "Use a .tga frame as a viewport placeholder — the name typed here overrides it on export. "
        "Supports .tga, .vbm (animated), and .dds (Alpine Faction)."
    )

    def draw(self, context):
        layout = self.layout
        obj = context.active_object

        if not obj or obj.type != "MESH":
            layout.label(text="Select a mesh.", icon="INFO")
            return

        slots = obj.material_slots
        if not slots:
            layout.label(text="No material slots.", icon="INFO")
            return

        for i, slot in enumerate(slots):
            mat = slot.material
            box = layout.box()
            # Slot header
            row = box.row(align=True)
            row.label(text=mat.name if mat else f"Slot {i} (empty)", icon="MATERIAL")

            if not mat:
                box.label(text="Assign a material to this slot.", icon="ERROR")
                continue

            p = mat.rfvfx_props

            # Texture name row: text field + browse button
            row_t = box.row(align=True)
            row_t.prop(p, "texture_name", text="")
            op = row_t.operator("rfvfx.browse_material_texture_slot", text="", icon="FILEBROWSER")
            op.slot_index = i

            # Hint under the field
            hint_col = box.column(align=True)
            hint_col.scale_y = 0.75
            tex = p.texture_name.strip().lower()
            if not tex:
                # Show what the exporter would fall back to
                fallback = None
                try:
                    if mat.node_tree:
                        for n in mat.node_tree.nodes:
                            if n.type == "TEX_IMAGE" and getattr(n, "image", None):
                                img = n.image
                                fp = (img.filepath or "").strip()
                                fallback = os.path.basename(bpy.path.abspath(fp)) if fp else img.name
                                break
                except Exception:
                    pass
                if fallback:
                    hint_col.label(text=f"Fallback: {fallback}", icon="IMAGE_DATA")
                else:
                    hint_col.label(text="No texture name or node image set.", icon="ERROR")
            elif tex.endswith(".vbm"):
                hint_col.label(text="Animated texture (VBM)", icon="COLOR")
            elif tex.endswith(".tga"):
                hint_col.label(text="Static texture (TGA)", icon="IMAGE_DATA")
            elif tex.endswith(".dds"):
                hint_col.label(text="DDS texture (Alpine Faction)", icon="IMAGE_DATA")
            else:
                hint_col.label(text="Unknown extension — expect .tga / .vbm / .dds", icon="ERROR")

            # Additive toggle
            box.prop(p, "additive")


class RFVFX_PT_ObjectFlags(bpy.types.Panel):
    bl_label = "Object Flags"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "RF VFX"
    bl_parent_id = "RFVFX_PT_panel"
    bl_options = set()
    bl_description = "Controls how the engine renders the selected mesh. All optional — defaults to a standard solid 3D object. Billboard makes it always face the camera. Rod is for beams and streaks. Fullbright ignores scene lighting."

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        if not obj:
            layout.label(text="Select an object.", icon="INFO")
            return
        if not hasattr(obj, "rfvfx_props"):
            layout.label(text="Not an RF VFX object.", icon="INFO")
            return

        # Flags only apply to meshes
        if obj.type != "MESH":
            layout.label(text=obj.name, icon="EMPTY_DATA")
            layout.label(text="Flags apply to meshes only.")
            return


        p = obj.rfvfx_props
        box = layout.box()
        box.label(text=obj.name, icon="MESH_DATA")
        col = box.column(align=True)

        row = col.row(align=True)
        row.prop(p, "facing", text="Billboard", toggle=True)
        row.prop(p, "facing_rod", text="Rod", toggle=True)
        row2 = col.row(align=True)
        row2.prop(p, "fullbright", toggle=True)

        if p.facing or p.facing_rod:
            col.prop(p, "width")

        if obj.type == "MESH" and obj.data.shape_keys and len(obj.data.shape_keys.key_blocks) > 1:
            col.separator()
            col.label(text="Morph: detected (shape keys present)", icon="CHECKBOX_HLT")


class RFVFX_PT_Dummies(bpy.types.Panel):
    bl_label = "Dummies (Attachment Points)"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "RF VFX"
    bl_parent_id = "RFVFX_PT_panel"
    bl_options = set()
    bl_description = "Invisible named attachment points embedded in the VFX. The engine reads their names to attach weapons, flags, thrusters, and coronas to specific positions on the mesh. Parent a mesh first — dummies auto-parent to it."

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        box = layout.box()
        col = box.column(align=True)
        col.scale_y = 1.0
        col.operator("rfvfx.dummy_propflag")
        col.operator("rfvfx.dummy_muzzle")
        col.operator("rfvfx.dummy_thruster")
        col.operator("rfvfx.dummy_corona")
        col.operator("rfvfx.dummy_chaingun")
        col.operator("rfvfx.dummy_primary")
        col.operator("rfvfx.dummy_secondary")
        col.operator("rfvfx.dummy_interface")
        col.separator()
        if obj and obj.type == "MESH":
            col.label(text=f"Will parent to: {obj.name}", icon="CHECKBOX_HLT")
        else:
            col.label(text="Select a mesh first to auto-parent.", icon="INFO")


class RFVFX_PT_Particles(bpy.types.Panel):
    bl_label = "Particle Emitters"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "RF VFX"
    bl_parent_id = "RFVFX_PT_panel"
    bl_options = set()
    bl_description = "Adds sprite-based particle effects to the VFX: sparks, fire, smoke, water drops. The emitter is baked into the VFX file — it is not a live simulation like the RF level editor. Parent the emitter empty to a mesh so particles follow it during animation."

    def draw(self, context):
        layout = self.layout
        obj = context.active_object


        row = layout.row(align=True)
        row.operator("rfvfx.create_particle_emitter", text="New Emitter", icon="ADD")
        if obj and obj.type == "EMPTY" and obj.get("rf_vfx_particle"):
            row.operator("rfvfx.clone_particle_from_import", text="Clone", icon="COPYDOWN")

        pp = getattr(obj, "rfvfx_particle", None) if obj else None
        if not pp or not pp.is_emitter:
            layout.label(text="Select an emitter, or create one.", icon="INFO")
            return

        box = layout.box()
        box.label(text=obj.name, icon="EMPTY_ARROWS")

        if pp.raw_body_b64:
            box.label(text="Imported baked data (byte-exact)", icon="KEYFRAME")
            return

        # --- Particle Bitmap ---
        box2 = layout.box()
        box2.label(text="Particle Bitmap", icon="TEXTURE")
        box2.prop(pp, "particle_type")
        if pp.particle_type == "FACING":
            row_t = box2.row(align=True)
            row_t.prop(pp, "texture_name", text="")
            row_t.operator("rfvfx.browse_particle_texture", text="", icon="FILEBROWSER")
            if not pp.texture_name.strip():
                box2.label(text="Set a .tga/.vbm filename", icon="ERROR")
        else:
            box2.prop(pp, "tail_distance")

        # --- Particle Flags ---
        box3 = layout.box()
        box3.label(text="Particle Flags", icon="PROPERTIES")
        row_f = box3.row(align=True)
        row_f.prop(pp, "fade", toggle=True)
        row_f.prop(pp, "additive", text="Glow", toggle=True)
        row_f2 = box3.row(align=True)
        row_f2.prop(pp, "randomize_orient", text="Randomize", toggle=True)
        row_f2.prop(pp, "no_cull", text="No Cull", toggle=True)

        # --- Emission ---
        box4 = layout.box()
        box4.label(text="Emission", icon="FORCE_WIND")

        row_sd = box4.row(align=True)
        row_sd.prop(pp, "spawn_delay", text="Spawn Delay")
        row_sd.prop(pp, "spawn_delay_variation", text="+/-")

        row_v = box4.row(align=True)
        row_v.prop(pp, "speed", text="Velocity")
        row_v.prop(pp, "speed_variation", text="+/-")

        row_dc = box4.row(align=True)
        row_dc.prop(pp, "decay", text="Decay")
        row_dc.prop(pp, "decay_variation", text="+/-")

        row_s = box4.row(align=True)
        row_s.prop(pp, "particle_size", text="Particle Radius")
        row_s.prop(pp, "size_variation", text="+/-")


        # --- Emitter ---
        box5 = layout.box()
        box5.label(text="Emitter", icon="MESH_UVSPHERE")
        box5.prop(pp, "emitter_radius", text="Radius")
        box5.prop(pp, "random_direction")

        # --- Physics ---
        box6 = layout.box()
        box6.label(text="Physics", icon="PHYSICS")
        box6.prop(pp, "apply_gravity", text="Gravity  (on/off)")

        # --- Size / Fade ---
        box7 = layout.box()
        box7.label(text="Size / Fade", icon="MOD_OPACITY")
        box7.prop(pp, "size_at_birth", text="Size at Birth")
        box7.prop(pp, "size_at_death", text="Size at Death")
        box7.prop(pp, "fade_at_birth", text="Fade at Birth")
        box7.prop(pp, "fade_at_death", text="Fade at Death")



class RFVFX_PT_ExportPanel(bpy.types.Panel):
    bl_label = "Export"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "RF VFX"
    bl_parent_id = "RFVFX_PT_panel"
    bl_options = set()

    def draw(self, context):
        layout = self.layout
        s = context.scene.rfvfx

        # ── 1. Identity ──
        layout.prop(s, "vfx_name", text="Name")
        layout.prop(s, "export_vfx_out", text="Output")

        has_template = bool(s.last_import_vfx.strip() or s.export_template_vfx.strip())
        if has_template:
            tmpl_name = ""
            import os as _os2
            if s.export_template_vfx.strip():
                tmpl_name = _os2.path.basename(bpy.path.abspath(s.export_template_vfx))
            elif s.last_import_vfx.strip():
                tmpl_name = _os2.path.basename(bpy.path.abspath(s.last_import_vfx))
            layout.label(text=f"Patch mode: {tmpl_name}" if tmpl_name else "Patch mode — template set", icon="FILE_TICK")

        layout.separator()

        # ── 2. Prepare — collapsible ──
        row_prep = layout.row()
        prep_icon = "TRIA_DOWN" if s.show_prep_tools else "TRIA_RIGHT"
        row_prep.prop(s, "show_prep_tools", text="Prepare", emboss=False, icon=prep_icon)
        if s.show_prep_tools:
            box_p = layout.box()
            col_p = box_p.column(align=True)
            col_p.scale_y = 1.0
            col_p.operator("rfvfx.bake_anim_to_shape_keys", text="Bake Animation to Shape Keys", icon="SHAPEKEY_DATA")
            col_p.separator()
            col_p.label(text="Rigged / Pre-keyed Mesh Converters:", icon="FORWARD")
            col_p.operator("rfvfx.armature_to_shape_keys", text="Bake Rigged Mesh → Shape Keys", icon="ARMATURE_DATA")
            col_p.operator("rfvfx.keyed_shapes_to_rf_timing", text="Wire RF Timing on Existing Shape Keys", icon="KEYFRAME_HLT")
            col_p.separator()
            col_p.operator("rfvfx.readiness_check", text="Readiness Check", icon="VIEWZOOM")
            col_p.operator("rfvfx.arrange_scene", text="Select and Frame All", icon="RESTRICT_SELECT_OFF")

        # ── 3. Advanced — collapsible ──
        row_adv = layout.row()
        adv_icon = "TRIA_DOWN" if s.show_export_advanced else "TRIA_RIGHT"
        row_adv.prop(s, "show_export_advanced", text="Advanced", emboss=False, icon=adv_icon)
        if s.show_export_advanced:
            box = layout.box()
            box.prop(s, "export_mode", text="Mode")
            col = box.column(align=True)
            col.prop(s, "double_sided")
            box.separator()
            row_anchor = box.row(align=True)
            row_anchor.label(text="Anchor:", icon="EMPTY_AXIS")
            row_anchor.prop_search(s, "export_anchor", context.scene, "objects", text="")
            box.separator()
            row_fps = box.row(align=True)
            row_fps.label(text="Morph FPS:", icon="MOD_WARP")
            row_fps.prop(s, "morph_fps", text="")

            scene = context.scene
            bl_fps = scene.render.fps
            total_frames = scene.frame_end - scene.frame_start + 1
            bl_secs = total_frames / bl_fps
            rf_secs = total_frames / 15.0

            info = box.column(align=True)
            info.scale_y = 0.75
            info.label(text=f"Blender {bl_secs:.1f}s  \u2192  RF {rf_secs:.1f}s", icon="TIME")
            if s.morph_fps == "5":
                info.label(text="Cloth, foliage, ambient loops")
            elif s.morph_fps == "10":
                info.label(text="Moderate animations")
            else:
                info.label(text="Explosions, impacts, sharp effects")

        # ── 4. Export ──
        layout.separator()
        row = layout.row()
        row.scale_y = 1.6
        row.operator("rfvfx.export_vfx", text="Export VFX", icon="EXPORT")


def _get_template_dir():
    """Return the path where the RF VFX app template should be installed."""
    import os
    templates_dir = bpy.utils.user_resource("SCRIPTS", path="startup/bl_app_templates_user")
    return os.path.join(templates_dir, "RF_VFX")

def _install_app_template():
    """Write the RF VFX app template files to the Blender user scripts folder."""
    import os
    template_dir = _get_template_dir()
    os.makedirs(template_dir, exist_ok=True)

    # startup.py — runs when the template is loaded via File > New
    startup_py = '''\
import bpy

def setup_rf_vfx_scene():
    scene = bpy.context.scene

    # Remove everything in the default scene
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=True)

    # Remove all default collections except the master scene collection
    for col in list(bpy.data.collections):
        try:
            scene.collection.children.unlink(col)
        except Exception:
            pass
        bpy.data.collections.remove(col)

    # Remove leftover meshes/objects from default scene
    for block in list(bpy.data.meshes):
        bpy.data.meshes.remove(block)
    for block in list(bpy.data.lights):
        bpy.data.lights.remove(block)
    for block in list(bpy.data.cameras):
        bpy.data.cameras.remove(block)

    # Create RF_VFX collection
    rf_col = bpy.data.collections.new("RF_VFX")
    scene.collection.children.link(rf_col)

    # Create RFVFX_ROOT dummy
    root = bpy.data.objects.new("RFVFX_ROOT", None)
    root.empty_display_type = "ARROWS"
    root.empty_display_size = 0.4
    root["rfvfx_is_dummy"] = 1
    rf_col.objects.link(root)

    # Scene settings
    scene["rfvfx_export_collection"] = "RF_VFX"
    scene["rfvfx_root_dummy"] = "RFVFX_ROOT"
    try:
        scene.unit_settings.system = "METRIC"
        scene.unit_settings.scale_length = 1.0
    except Exception:
        pass

    # Name the scene
    scene.name = "RF_VFX_Scene"

    # Select root
    bpy.ops.object.select_all(action="DESELECT")
    root.select_set(True)
    bpy.context.view_layer.objects.active = root

setup_rf_vfx_scene()
'''

    startup_path = os.path.join(template_dir, "startup.py")
    with open(startup_path, "w", encoding="utf-8") as f:
        f.write(startup_py)

    # __init__.py required by Blender to recognise the template folder
    init_path = os.path.join(template_dir, "__init__.py")
    if not os.path.exists(init_path):
        with open(init_path, "w", encoding="utf-8") as f:
            f.write("# RF VFX App Template\n")

    return template_dir

def _uninstall_app_template():
    """Remove the RF VFX app template folder."""
    import os, shutil
    template_dir = _get_template_dir()
    if os.path.isdir(template_dir):
        shutil.rmtree(template_dir)


class RFVFX_OT_InstallTemplate(bpy.types.Operator):
    bl_idname = "rfvfx.install_template"
    bl_label = "Install RF VFX Scene Template"
    bl_description = (
        "Installs an 'RF VFX' entry in File > New. "
        "After installing, use File > New > RF VFX to start a clean scene "
        "with RF_VFX collection and RFVFX_ROOT already set up."
    )
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            path = _install_app_template()
            self.report({"INFO"}, f"RF VFX template installed. Restart Blender, then use File > New > RF VFX.")
        except Exception as e:
            self.report({"ERROR"}, f"Failed to install template: {e}")
            return {"CANCELLED"}
        return {"FINISHED"}


class RFVFX_OT_UninstallTemplate(bpy.types.Operator):
    bl_idname = "rfvfx.uninstall_template"
    bl_label = "Uninstall RF VFX Scene Template"
    bl_description = "Removes the RF VFX entry from File > New."
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            _uninstall_app_template()
            self.report({"INFO"}, "RF VFX template removed. Restart Blender to apply.")
        except Exception as e:
            self.report({"ERROR"}, f"Failed to remove template: {e}")
            return {"CANCELLED"}
        return {"FINISHED"}


_authoring_classes = [
    RFVFX_ObjectProps,
    RFVFX_MaterialProps,
    RFVFX_ParticleProps,
    RFVFX_OT_InstallTemplate,
    RFVFX_OT_UninstallTemplate,
    RFVFX_OT_NewAuthoringScene,
    RFVFX_OT_AddSelectedToRF,
    RFVFX_OT_ArrangeScene,
    RFVFX_OT_BakeAnimToShapeKeys,
    RFVFX_OT_ArmatureToShapeKeys,
    RFVFX_OT_KeyedShapesToRFTiming,
    RFVFX_OT_ImportQuakeModel,
    RFVFX_OT_ReadinessCheck,
    RFVFX_OT_ExportAuthoringGLTF,
    RFVFX_OT_WriteAuthoringGuide,
    RFVFX_OT_CreateParticleEmitter,
    RFVFX_OT_CloneParticleFromImport,
    RFVFX_OT_Dummy_PropFlag,
    RFVFX_OT_Dummy_Muzzle,
    RFVFX_OT_Dummy_Thruster,
    RFVFX_OT_Dummy_Corona,
    RFVFX_OT_Dummy_Chaingun,
    RFVFX_OT_Dummy_Primary,
    RFVFX_OT_Dummy_Secondary,
    RFVFX_OT_Dummy_Interface,
    RFVFX_OT_BrowseParticleTexture,
    RFVFX_OT_BrowseMaterialTexture,
    RFVFX_OT_BrowseMaterialTextureSlot,
    RFVFX_OT_ImportRFG,
    RFVFX_OT_ImportWRL,
    RFVFX_PT_Workflows,
    RFVFX_PT_ImportScene,
    RFVFX_PT_Materials,
    RFVFX_PT_ObjectFlags,
    RFVFX_PT_Dummies,
    RFVFX_PT_Particles,
    RFVFX_PT_ExportPanel,
]

_authoring_registered = False

def _authoring_register():
    global _authoring_registered
    if _authoring_registered:
        return
    for c in _authoring_classes:
        try:
            bpy.utils.register_class(c)
        except Exception as e:
            print(f"[RFVFX] FAILED to register {c.__name__}: {e}")
    bpy.types.Object.rfvfx_props = PointerProperty(type=RFVFX_ObjectProps)
    bpy.types.Material.rfvfx_props = PointerProperty(type=RFVFX_MaterialProps)
    bpy.types.Object.rfvfx_particle = PointerProperty(type=RFVFX_ParticleProps)
    _authoring_registered = True

def _authoring_unregister():
    global _authoring_registered
    if not _authoring_registered:
        return
    try:
        del bpy.types.Object.rfvfx_props
    except Exception:
        pass
    try:
        del bpy.types.Material.rfvfx_props
    except Exception:
        pass
    try:
        del bpy.types.Object.rfvfx_particle
    except Exception:
        pass

    for c in reversed(_authoring_classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass
    _authoring_registered = False

# Re-define register/unregister to include both base and authoring classes
def register():
    for c in _classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.rfvfx = PointerProperty(type=RFVFX_Settings)
    _authoring_register()

def unregister():
    _authoring_unregister()
    try:
        del bpy.types.Scene.rfvfx
    except Exception:
        pass
    for c in reversed(_classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass
