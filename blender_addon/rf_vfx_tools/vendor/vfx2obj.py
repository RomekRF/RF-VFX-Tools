#!/usr/bin/env python3
"""
vfx2obj_optionA.py — Red Faction 1 (.vfx) -> glTF 2.0 (single-scene) converter

Option A feature set (from our chat logs):
- Morphed meshes export as glTF morph targets + a real weights animation track
- DMMY nodes export as empty nodes with proper parenting
- Constant scale on NON-morph meshes can be baked into geometry (to avoid scaling children in Blender)

This script is intentionally "format-first": it follows the vfx.ksy (Rafalh) field order and avoids
guessy post-passes.

NOTE: This is a *reader/exporter* only (no VFX writer yet).
"""

from __future__ import annotations

import os
import sys
import json
import math
import struct
import hashlib
import base64
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any

# ----------------------------
# Bin reader
# ----------------------------

class Bin:
    __slots__ = ("data", "ofs")
    def __init__(self, data: bytes):
        self.data = data
        self.ofs = 0

    def tell(self) -> int:
        return self.ofs

    def seek(self, ofs: int) -> None:
        self.ofs = ofs

    def read(self, n: int) -> bytes:
        b = self.data[self.ofs:self.ofs+n]
        if len(b) != n:
            raise EOFError(f"Unexpected EOF at {self.ofs}, need {n}")
        self.ofs += n
        return b

    def u8(self) -> int:
        return self.read(1)[0]

    def s8(self) -> int:
        return struct.unpack("<b", self.read(1))[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.read(2))[0]

    def s16(self) -> int:
        return struct.unpack("<h", self.read(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def s32(self) -> int:
        return struct.unpack("<i", self.read(4))[0]

    def f32(self) -> float:
        return struct.unpack("<f", self.read(4))[0]

    def strz(self) -> str:
        start = self.ofs
        while self.ofs < len(self.data) and self.data[self.ofs] != 0:
            self.ofs += 1
        if self.ofs >= len(self.data):
            raise EOFError("Unterminated string")
        s = self.data[start:self.ofs].decode("ascii", errors="replace")
        self.ofs += 1
        return s

# ----------------------------
# RF stored basis -> Blender basis
# vfx.ksy vec3:
#   stored.x = -max.x
#   stored.y =  max.z
#   stored.z = -max.y
# so max/blender:
#   x = -stored.x
#   y = -stored.z
#   z =  stored.y
# ----------------------------

def rf_to_blender(v: Tuple[float, float, float]) -> Tuple[float, float, float]:
    sx, sy, sz = v
    return (-sx, -sz, sy)


def blender_to_rf(v: Tuple[float, float, float]) -> Tuple[float, float, float]:
    bx, by, bz = v
    # inverse of rf_to_blender: (bx,by,bz)=(-sx,-sz,sy)
    sx = -bx
    sy = bz
    sz = -by
    return (sx, sy, sz)

def read_vec3_rf(b: Bin) -> Tuple[float, float, float]:
    return (b.f32(), b.f32(), b.f32())

def read_vec3(b: Bin) -> Tuple[float, float, float]:
    return rf_to_blender(read_vec3_rf(b))

def read_uv(b: Bin) -> Tuple[float, float]:
    u = b.f32()
    v_stored = b.f32()
    return (u, -v_stored)

def read_quat_raw(b: Bin) -> Tuple[float, float, float, float]:
    return (b.f32(), b.f32(), b.f32(), b.f32())

# ----------------------------
# Quaternion/matrix helpers for basis conversion
# We treat stored quats as operating in stored basis, and convert:
#   R_blender = M * R_stored * M^T
# where M maps stored vectors to blender vectors: v_b = M v_s
# M is orthonormal (with reflection), so inverse is transpose.
# ----------------------------

# M = [[-1,0,0],[0,0,-1],[0,1,0]]
_M = (
    (-1.0, 0.0, 0.0),
    ( 0.0, 0.0,-1.0),
    ( 0.0, 1.0, 0.0),
)

def mat3_mul(a, b):
    # a,b: 3x3 tuples
    return tuple(
        tuple(sum(a[i][k]*b[k][j] for k in range(3)) for j in range(3))
        for i in range(3)
    )

def mat3_T(a):
    return tuple(tuple(a[j][i] for j in range(3)) for i in range(3))

def quat_to_mat3(q: Tuple[float, float, float, float]) -> Tuple[Tuple[float,float,float],Tuple[float,float,float],Tuple[float,float,float]]:
    x,y,z,w = q
    # standard right-handed quaternion to matrix
    xx, yy, zz = x*x, y*y, z*z
    xy, xz, yz = x*y, x*z, y*z
    wx, wy, wz = w*x, w*y, w*z
    return (
        (1.0-2.0*(yy+zz), 2.0*(xy - wz),     2.0*(xz + wy)),
        (2.0*(xy + wz),     1.0-2.0*(xx+zz), 2.0*(yz - wx)),
        (2.0*(xz - wy),     2.0*(yz + wx),     1.0-2.0*(xx+yy)),
    )

def mat3_to_quat(m: Tuple[Tuple[float,float,float],Tuple[float,float,float],Tuple[float,float,float]]) -> Tuple[float,float,float,float]:
    # robust-ish matrix->quat
    m00,m01,m02 = m[0]
    m10,m11,m12 = m[1]
    m20,m21,m22 = m[2]
    tr = m00+m11+m22
    if tr > 0.0:
        S = math.sqrt(tr+1.0)*2.0
        w = 0.25*S
        x = (m21 - m12)/S
        y = (m02 - m20)/S
        z = (m10 - m01)/S
    elif (m00 > m11) and (m00 > m22):
        S = math.sqrt(1.0 + m00 - m11 - m22)*2.0
        w = (m21 - m12)/S
        x = 0.25*S
        y = (m01 + m10)/S
        z = (m02 + m20)/S
    elif m11 > m22:
        S = math.sqrt(1.0 + m11 - m00 - m22)*2.0
        w = (m02 - m20)/S
        x = (m01 + m10)/S
        y = 0.25*S
        z = (m12 + m21)/S
    else:
        S = math.sqrt(1.0 + m22 - m00 - m11)*2.0
        w = (m10 - m01)/S
        x = (m02 + m20)/S
        y = (m12 + m21)/S
        z = 0.25*S
    return quat_norm((x,y,z,w))

def quat_norm(q: Tuple[float,float,float,float]) -> Tuple[float,float,float,float]:
    x,y,z,w = q
    n = math.sqrt(x*x+y*y+z*z+w*w)
    if n <= 0.0:
        return (0.0,0.0,0.0,1.0)
    inv = 1.0/n
    return (x*inv,y*inv,z*inv,w*inv)

def quat_rf_to_blender(q: Tuple[float,float,float,float]) -> Tuple[float,float,float,float]:
    R = quat_to_mat3(q)
    Rt = mat3_mul(mat3_mul(_M, R), mat3_T(_M))
    return mat3_to_quat(Rt)


def quat_blender_to_rf(q: Tuple[float,float,float,float]) -> Tuple[float,float,float,float]:
    # Inverse of quat_rf_to_blender.
    # If qb = M * qrf * M^T (as rotation matrices), then qrf = M^T * qb * M.
    Rb = quat_to_mat3(q)
    Rt = mat3_mul(mat3_mul(mat3_T(_M), Rb), _M)
    return mat3_to_quat(Rt)

def quat_dot(a: Tuple[float,float,float,float], b: Tuple[float,float,float,float]) -> float:
    return a[0]*b[0] + a[1]*b[1] + a[2]*b[2] + a[3]*b[3]

def quat_neg(q: Tuple[float,float,float,float]) -> Tuple[float,float,float,float]:
    return (-q[0], -q[1], -q[2], -q[3])

def quat_fix_sign_seq(qs: List[Tuple[float,float,float,float]]) -> List[Tuple[float,float,float,float]]:
    # Quaternions q and -q represent the same rotation. Fix sign flips so we can measure variation sanely.
    if not qs:
        return []
    out = [qs[0]]
    for q in qs[1:]:
        if quat_dot(out[-1], q) < 0.0:
            q = quat_neg(q)
        out.append(q)
    return out

def quat_angle_diff_rad(a: Tuple[float,float,float,float], b: Tuple[float,float,float,float]) -> float:
    # Small, stable rotation difference metric in radians (0..pi)
    d = abs(quat_dot(a, b))
    if d > 1.0: d = 1.0
    # angle = 2*acos(d); clamp numeric noise
    return 2.0 * math.acos(d)


# ----------------------------
# VFX structures (subset we need)
# ----------------------------

SEC_SFXO = 0x4F584653  # mesh
SEC_MATL = 0x4C54414D  # material
SEC_DMMY = 0x594D4D44  # dummy
SEC_PART = 0x54524150  # particle system

@dataclass
class VfxHeader:
    version: int
    flags: Optional[int]
    end_frame: int

@dataclass
class VfxMaterial:
    # minimal: diffuse texture name (tex_0)
    index: int
    mat_type: int
    additive: bool
    tex0: str
    self_illum: Optional[List[float]] = None       # self-illumination per frame
    opacity_frames: Optional[List[float]] = None   # opacity per frame (fade in/out)

@dataclass
class MeshFace:
    material_index: int     # as stored in VFX face (mesh-local index or -1)
    face_vertex_indices: Tuple[int,int,int]

@dataclass
class FaceVertex:
    vertex_index: int

@dataclass
class MeshFrame:
    # positions only present for morph or index==0
    center_rf: Optional[Tuple[float,float,float]] = None
    mult_rf: Optional[Tuple[float,float,float]] = None
    pos_s16: Optional[List[Tuple[int,int,int]]] = None

    # absolute offsets into the original VFX file (for patch-in-place writing)
    center_ofs: Optional[int] = None
    mult_ofs: Optional[int] = None
    pos_ofs: Optional[int] = None  # start of packed int16 triplets (x,y,z) * num_vertices
    pos_count: int = 0

    # UVs present for index==0 (or dump_uvs)
    uvs: Optional[List[Tuple[float,float]]] = None  # length 3*num_faces

    # Facing mesh data (billboards)
    facing_pair: Optional[Tuple[float,float]] = None  # 2 floats per frame for facing/facing_rod
    facing_rod_up: Optional[Tuple[float,float,float]] = None  # up_vector, frame 0 only for facing_rod

    # TRS per frame when not morph and not keyframed
    translation_rf: Optional[Tuple[float,float,float]] = None
    rotation_raw: Optional[Tuple[float,float,float,float]] = None
    scale_rf: Optional[Tuple[float,float,float]] = None

@dataclass
class MeshKeyframes:
    t_times: List[float]
    t_values: List[Tuple[float,float,float]]
    r_times: List[float]
    r_values: List[Tuple[float,float,float,float]]
    s_times: List[float]
    s_values: List[Tuple[float,float,float]]

@dataclass
class VfxMesh:
    name: str
    parent_name: str
    flags_raw: int
    morph: bool
    facing: bool
    facing_rod: bool
    dump_uvs: bool
    is_keyframed: bool
    num_vertices: int
    num_faces: int
    fps: int
    start_time: float
    end_time: float
    num_frames: int
    materials_indices: List[int]  # maps mesh-local material indices -> global material indices
    faces: List[MeshFace]
    face_vertices: List[FaceVertex]
    frames: List[MeshFrame]
    keyframes: Optional[MeshKeyframes]
    pivot_translation_rf: Optional[Tuple[float,float,float]]
    pivot_rotation_raw: Optional[Tuple[float,float,float,float]]
    pivot_scale_rf: Optional[Tuple[float,float,float]]
    keyframed_block: Optional[bytes]
    save_parent: int = 0  # s8 field (0 or 1)

@dataclass
class DummyFrame:
    pos_rf: Tuple[float,float,float]
    orient_raw: Tuple[float,float,float,float]

@dataclass
class VfxDummy:
    name: str
    parent_name: str
    pos_rf: Tuple[float,float,float]
    orient_raw: Tuple[float,float,float,float]
    frames: List[DummyFrame]

@dataclass
class VfxParticleSystem:
    """VFX particle system (PART section).
    
    Stores the full binary body for faithful round-trip, plus parsed header fields
    for display and authoring. The body contains per-particle baked trajectory data
    generated by VParticle in 3ds Max.
    """
    name: str
    parent_name: str
    save_parent: int
    num_particles: int    # number of baked particle entries
    num_frames: int       # animation frame count
    fps: int
    raw_body: bytes       # complete section body (after tag+size header) for round-trip

# ----------------------------
# Parsing (subset)
# ----------------------------

def parse_header(b: Bin) -> VfxHeader:
    if b.read(4) != b"VSFX":
        raise ValueError("Not a VSFX/VFX file")
    version = b.s32()
    flags = b.s32() if version >= 0x30008 else None
    end_frame = b.s32()
    # skip the rest of header counts (we don't need them, but must advance correctly)
    # See vfx.ksy file_header
    b.s32(); b.s32(); b.s32(); b.s32(); b.s32(); b.s32()  # num_meshes..num_cameras
    if version >= 0x3000F:
        b.s32()  # num_selsets
    if version >= 0x40000:
        b.s32()  # num_materials
    if version >= 0x40002:
        b.s32()  # num_mix_frames
    if version >= 0x40003:
        b.s32()  # num_self_illumination_frames
    if version >= 0x40005:
        b.s32()  # num_opacity_frames
    if version < 0x3000A:
        b.s32()  # unk_1
    # remaining fixed counts
    b.s32(); b.s32(); b.s32(); b.s32(); b.s32()  # num_faces..num_mesh_frames
    if version >= 0x3000D:
        b.s32()  # num_uv_frames
    if version >= 0x30009:
        b.s32(); b.s32(); b.s32(); b.s32(); b.s32()  # transform counts
    b.s32(); b.s32(); b.s32(); b.s32(); b.s32()  # light..camera frames
    if version >= 0x3000F:
        b.s32()  # num_selset_objects
    return VfxHeader(version=version, flags=flags, end_frame=end_frame)

def parse_material(b: Bin, version: int, index: int) -> VfxMaterial:
    mat_type = b.s32()
    fps = b.s32() if version >= 0x40003 else 15
    additive = False
    tex0 = ""
    if (mat_type in (0,1)) or (version >= 0x40006):
        additive = (b.s8() != 0)
    if mat_type in (0,1):
        tex0 = b.strz()
        if version >= 0x30012:
            _ = b.s32()  # start_frame
            _ = b.f32()  # playback_rate
            _ = b.s32()  # anim_type
        if mat_type == 1:
            _t1 = b.strz()
            if version >= 0x30012:
                _ = b.s32(); _ = b.f32(); _ = b.s32()
            num_mix = b.s32()
            if mat_type == 1 and version < 0x40003:
                _ = b.s32()
            for _ in range(num_mix):
                _ = b.f32()
        # spec/gloss/refl
        _ = b.f32(); _ = b.f32(); _ = b.f32()
        _ = b.strz()
    elif mat_type == 2:
        # solid_color rgb_s4
        _ = b.s32(); _ = b.s32(); _ = b.s32()
    self_illum_vals = None
    opacity_vals = None
    if version >= 0x40003:
        n_si = b.s32()
        self_illum_vals = [b.f32() for _ in range(n_si)]
    else:
        self_illum_vals = [b.f32()]
    if version >= 0x40005:
        n_op = b.s32()
        opacity_vals = [b.f32() for _ in range(n_op)]
    return VfxMaterial(index=index, mat_type=mat_type, additive=additive, tex0=tex0,
                       self_illum=self_illum_vals, opacity_frames=opacity_vals)

def parse_mesh(b: Bin, hdr: VfxHeader, section_end: int) -> VfxMesh:
    ver = hdr.version
    name = b.strz()
    parent_name = b.strz()
    save_parent_val = b.s8()
    num_vertices = b.s32()
    if ver < 0x3000A:
        for _ in range(num_vertices):
            _ = read_vec3(b)
    num_faces = b.s32()

    # faces: we only keep material_index + face_vertex_indices
    faces: List[MeshFace] = []
    for _ in range(num_faces):
        # indices (ignored; topo is via face_vertex_indices + face_vertices table)
        _ = b.s32(); _ = b.s32(); _ = b.s32()
        if ver < 0x3000D:
            for __ in range(3):
                _ = read_uv(b)
        # 3 colors (rgb_f4) each has 3 floats
        for __ in range(3*3):
            _ = b.f32()
        _ = read_vec3_rf(b)  # face normal stored basis
        _ = read_vec3_rf(b)  # center
        _ = b.f32()          # radius
        material_index = b.s32()
        _ = b.s32()          # smoothing_group
        fvi0 = b.s32(); fvi1 = b.s32(); fvi2 = b.s32()
        faces.append(MeshFace(material_index=material_index, face_vertex_indices=(fvi0,fvi1,fvi2)))

    fps = b.s32() if ver >= 0x30009 else 15

    start_time = 0.0
    end_time = 0.0
    num_frames = 1
    if ver >= 0x40004:
        start_time = b.f32()
        end_time = b.f32()
        num_frames = b.s32()
    else:
        start_frame = b.s32()
        end_frame = b.s32()
        num_frames = max(1, (end_frame - start_frame + 1) if ver >= 0x3000C else (end_frame - start_frame))
        start_time = float(start_frame) / float(fps)
        end_time = float(end_frame) / float(fps)

    num_materials = b.s32()
    materials_indices: List[int] = []
    if ver >= 0x40000:
        for _ in range(num_materials):
            materials_indices.append(b.s32())
    else:
        # older: inline materials; skip
        for _ in range(num_materials):
            # mesh_material_old: too much; skip by seeking? can't easily without spec.
            pass

    _bounding_center = read_vec3_rf(b)
    _bounding_radius = b.f32()
    if ver < 0x30002:
        _ = b.s32()
    flags_raw = b.u32()
    facing = (flags_raw & 0x00000001) != 0
    dump_uvs = (flags_raw & 0x00000100) != 0
    facing_rod = (flags_raw & 0x00000800) != 0
    morph = (flags_raw & 0x00000004) != 0

    if facing and ver == 0x3000A:
        _ = b.f32(); _ = b.f32()

    num_face_vertices = b.s32()
    face_vertices: List[FaceVertex] = []
    for _ in range(num_face_vertices):
        _ = b.s32()  # smoothing_group
        vi = b.s32()
        _ = b.f32(); _ = b.f32()  # u,v garbage
        n_adj = b.s32()
        for __ in range(n_adj):
            _ = b.s32()
        face_vertices.append(FaceVertex(vertex_index=vi))

    is_keyframed = False
    if ver >= 0x30009:
        is_keyframed = (b.u8() != 0)

    frames: List[MeshFrame] = []
    for fi in range(num_frames):
        fr = MeshFrame()
        # positions
        if morph or fi == 0:
            fr.center_ofs = b.tell()
            fr.center_rf = read_vec3_rf(b)
            fr.mult_ofs = b.tell()
            fr.mult_rf = read_vec3_rf(b)
            fr.pos_ofs = b.tell()
            fr.pos_count = num_vertices
            fr.pos_s16 = [(b.s16(), b.s16(), b.s16()) for _ in range(num_vertices)]
            if (facing or facing_rod) and ver >= 0x3000B:
                fw = b.f32(); fh = b.f32()
                fr.facing_pair = (fw, fh)
            if facing_rod and fi == 0 and ver >= 0x40001:
                up = read_vec3_rf(b)
                fr.facing_rod_up = up
            # uvs
            if (dump_uvs or fi == 0) and ver >= 0x3000D:
                fr.uvs = [read_uv(b) for _ in range(3*num_faces)]
        # TRS for non-morph per-frame (when not keyframed)
        if (not morph) and (not is_keyframed):
            fr.translation_rf = read_vec3_rf(b)
            fr.rotation_raw = read_quat_raw(b)
            fr.scale_rf = read_vec3_rf(b)
        if ver < 0x30009:
            _ = b.read(1)
        if ver < 0x40005:
            _ = b.f32()  # opacity
        frames.append(fr)

    keyframes: Optional[MeshKeyframes] = None
    pivot_translation_rf: Optional[Tuple[float,float,float]] = None
    pivot_rotation_raw: Optional[Tuple[float,float,float,float]] = None
    pivot_scale_rf: Optional[Tuple[float,float,float]] = None
    keyframed_block: Optional[bytes] = None
    pivot_block_start: Optional[int] = None
    if is_keyframed:
        if ver >= 0x3000A:
            pivot_block_start = b.tell()
            pivot_translation_rf = read_vec3_rf(b)
            pivot_rotation_raw = read_quat_raw(b)
            pivot_scale_rf = read_vec3_rf(b)  # IMPORTANT: used for correct mesh scale
        # keyframe list
        num_t = b.s32()
        t_times: List[float] = []
        t_vals: List[Tuple[float,float,float]] = []
        for _ in range(num_t):
            t_raw = b.s32()
            v = read_vec3_rf(b)
            _ = read_vec3_rf(b); _ = read_vec3_rf(b)
            t_times.append((t_raw / 320.0) / float(fps))
            t_vals.append(rf_to_blender(v))
        num_r = b.s32()
        r_times: List[float] = []
        r_vals: List[Tuple[float,float,float,float]] = []
        for _ in range(num_r):
            t_raw = b.s32()
            q = read_quat_raw(b)
            _ = b.f32(); _ = b.f32(); _ = b.f32(); _ = b.f32(); _ = b.f32()
            r_times.append((t_raw / 320.0) / float(fps))
            r_vals.append(quat_rf_to_blender(q))
        num_s = b.s32()
        s_times: List[float] = []
        s_vals: List[Tuple[float,float,float]] = []
        for _ in range(num_s):
            t_raw = b.s32()
            v = read_vec3_rf(b)
            _ = read_vec3_rf(b); _ = read_vec3_rf(b)
            s_times.append((t_raw / 320.0) / float(fps))
            # scale: ignore sign flips
            sx,sy,sz = v
            s_vals.append((abs(sx), abs(sz), abs(sy)))
        keyframes = MeshKeyframes(
            t_times=t_times, t_values=t_vals,
            r_times=r_times, r_values=r_vals,
            s_times=s_times, s_values=s_vals
        )
        if pivot_block_start is not None:
            keyframed_block = b.data[pivot_block_start:b.tell()]

    # be safe: jump to section_end
    if b.tell() < section_end:
        b.seek(section_end)

    return VfxMesh(
        name=name,
        parent_name=parent_name,
        flags_raw=flags_raw,
        morph=morph,
        facing=facing,
        facing_rod=facing_rod,
        dump_uvs=dump_uvs,
        is_keyframed=is_keyframed,
        num_vertices=num_vertices,
        num_faces=num_faces,
        fps=fps,
        start_time=start_time,
        end_time=end_time,
        num_frames=num_frames,
        materials_indices=materials_indices,
        faces=faces,
        face_vertices=face_vertices,
        frames=frames,
        keyframes=keyframes,
        pivot_translation_rf=pivot_translation_rf,
        pivot_rotation_raw=pivot_rotation_raw,
        pivot_scale_rf=pivot_scale_rf,
        keyframed_block=keyframed_block,
        save_parent=int(save_parent_val),
    )

def parse_dummy(b: Bin, hdr: VfxHeader, section_end: int) -> VfxDummy:
    ver = hdr.version
    name = b.strz()
    parent_name = b.strz()
    _save_parent = b.u8()
    pos_rf = read_vec3_rf(b)
    orient_raw = read_quat_raw(b)
    num_frames = b.s32()
    frames: List[DummyFrame] = []
    for _ in range(num_frames):
        p = read_vec3_rf(b)
        q = read_quat_raw(b)
        frames.append(DummyFrame(pos_rf=p, orient_raw=q))
    if b.tell() < section_end:
        b.seek(section_end)
    return VfxDummy(name=name, parent_name=parent_name, pos_rf=pos_rf, orient_raw=orient_raw, frames=frames)

def parse_particle(b: Bin, hdr: VfxHeader, section_start: int, section_end: int) -> VfxParticleSystem:
    """Parse a PART section. We extract the human-readable header fields and store
    the complete section body for faithful binary round-trip."""
    # Preserve full body from section start (after tag+size) to section end
    raw_body = b.data[section_start:section_end]
    # Parse header fields for display
    b.seek(section_start)
    name = b.strz()
    parent_name = b.strz()
    save_parent_val = b.s8()
    # Fixed header fields (14 × s32/f32)
    _flags = b.s32()
    _unk1 = b.s32()
    _unk2 = b.s32()
    num_particles = b.s32()
    num_frames = b.s32()
    fps = b.s32()
    # Skip remaining fixed fields; full data is in raw_body
    b.seek(section_end)
    return VfxParticleSystem(
        name=name, parent_name=parent_name, save_parent=save_parent_val,
        num_particles=num_particles, num_frames=num_frames, fps=fps,
        raw_body=raw_body,
    )

def parse_vfx(path: str) -> Tuple[VfxHeader, List[VfxMaterial], List[VfxMesh], List[VfxDummy], List[VfxParticleSystem]]:
    data = open(path, "rb").read()
    b = Bin(data)
    hdr = parse_header(b)

    materials: List[VfxMaterial] = []
    meshes: List[VfxMesh] = []
    dummies: List[VfxDummy] = []
    particles: List[VfxParticleSystem] = []

    mat_index = 0
    while b.tell() < len(data):
        # section header: type(s4) + len(s4)
        sec_type = b.u32()
        sec_len = b.u32()
        # sec_len includes the len field itself, not the type field
        body_len = sec_len - 4
        section_end = b.tell() + body_len
        section_start = b.tell()
        try:
            if sec_type == SEC_MATL:
                materials.append(parse_material(b, hdr.version, mat_index))
                mat_index += 1
            elif sec_type == SEC_SFXO:
                meshes.append(parse_mesh(b, hdr, section_end))
            elif sec_type == SEC_DMMY:
                dummies.append(parse_dummy(b, hdr, section_end))
            elif sec_type == SEC_PART:
                particles.append(parse_particle(b, hdr, section_start, section_end))
            else:
                # skip unsupported sections
                b.seek(section_end)
        except Exception as e:
            # recover to next section
            raise RuntimeError(f"Failed parsing section type=0x{sec_type:08X} at ofs={b.tell()} in {os.path.basename(path)}: {e}") from e
        if b.tell() != section_end:
            b.seek(section_end)

    return hdr, materials, meshes, dummies, particles

# ----------------------------
# glTF builder
# ----------------------------

def _align4(n: int) -> int:
    return (n + 3) & ~3

class GltfBuilder:
    def __init__(self):
        self.g: Dict[str, Any] = {
            "asset": {"version": "2.0", "generator": "vfx2obj_optionA.py"},
            "scene": 0,
            "scenes": [{"nodes": []}],
            "nodes": [],
            "meshes": [],
            "accessors": [],
            "bufferViews": [],
            "buffers": [{"byteLength": 0, "uri": ""}],
            "materials": [],
            "images": [],
            "textures": [],
            "samplers": [{"magFilter": 9729, "minFilter": 9729, "wrapS": 10497, "wrapT": 10497}],
            "animations": [],
        }
        self._bin = bytearray()
        self._mat_map: Dict[int,int] = {}  # global material idx -> gltf material idx
        self._img_map: Dict[str,int] = {}  # tex name -> image idx
        self._tex_map: Dict[str,int] = {}  # tex name -> texture idx

    def _add_bufferview(self, data: bytes, target: Optional[int]) -> Tuple[int, int]:
        # returns (bufferViewIndex, byteOffset)
        off = _align4(len(self._bin))
        if off > len(self._bin):
            self._bin += b"\x00" * (off - len(self._bin))
        self._bin += data
        bv = {
            "buffer": 0,
            "byteOffset": off,
            "byteLength": len(data),
        }
        if target is not None:
            bv["target"] = target
        self.g["bufferViews"].append(bv)
        return len(self.g["bufferViews"]) - 1, off

    def _add_accessor(self, bv_i: int, byte_offset: int, component_type: int, count: int, acc_type: str,
                      minv: Optional[List[float]] = None, maxv: Optional[List[float]] = None) -> int:
        acc = {
            "bufferView": bv_i,
            "byteOffset": byte_offset,
            "componentType": component_type,
            "count": count,
            "type": acc_type,
        }
        if minv is not None: acc["min"] = minv
        if maxv is not None: acc["max"] = maxv
        self.g["accessors"].append(acc)
        return len(self.g["accessors"]) - 1

    def _ensure_material(self, mat: Optional[VfxMaterial]) -> Optional[int]:
        if mat is None:
            return None
        if mat.index in self._mat_map:
            return self._mat_map[mat.index]
        # image+texture if tex0 exists
        pbr: Dict[str, Any] = {}
        if mat.tex0:
            img_i = self._img_map.get(mat.tex0)
            if img_i is None:
                self.g["images"].append({"uri": mat.tex0})
                img_i = len(self.g["images"]) - 1
                self._img_map[mat.tex0] = img_i
            tex_i = self._tex_map.get(mat.tex0)
            if tex_i is None:
                self.g["textures"].append({"sampler": 0, "source": img_i})
                tex_i = len(self.g["textures"]) - 1
                self._tex_map[mat.tex0] = tex_i
            pbr["baseColorTexture"] = {"index": tex_i}
        m = {"name": mat.tex0 if mat.tex0 else f"MATL_{mat.index}"}
        if pbr:
            m["pbrMetallicRoughness"] = pbr
        if mat.additive:
            m["alphaMode"] = "BLEND"
        # Preserve RF material properties for round-trip
        mat_ex = {
            "rf_vfx_tex0": mat.tex0 or "",
            "rf_vfx_additive": 1 if mat.additive else 0,
        }
        if mat.self_illum is not None:
            mat_ex["rf_vfx_self_illum"] = [float(v) for v in mat.self_illum]
        if mat.opacity_frames is not None:
            mat_ex["rf_vfx_opacity"] = [float(v) for v in mat.opacity_frames]
        m["extras"] = mat_ex
        self.g["materials"].append(m)
        gi = len(self.g["materials"]) - 1
        self._mat_map[mat.index] = gi
        return gi

    def add_node(self, name: str, mesh_index: Optional[int]=None) -> int:
        n: Dict[str, Any] = {"name": name}
        if mesh_index is not None:
            n["mesh"] = mesh_index
        self.g["nodes"].append(n)
        return len(self.g["nodes"]) - 1

    def set_node_trs(self, node_index: int, t: Optional[Tuple[float,float,float]]=None,
                     r: Optional[Tuple[float,float,float,float]]=None,
                     s: Optional[Tuple[float,float,float]]=None):
        n = self.g["nodes"][node_index]
        if t is not None: n["translation"] = [float(t[0]), float(t[1]), float(t[2])]
        if r is not None: n["rotation"] = [float(r[0]), float(r[1]), float(r[2]), float(r[3])]
        if s is not None: n["scale"] = [float(s[0]), float(s[1]), float(s[2])]

    def add_child(self, parent: int, child: int) -> None:
        n = self.g["nodes"][parent]
        ch = n.get("children")
        if ch is None:
            n["children"] = [child]
        else:
            ch.append(child)

    def add_mesh_from_faces(
        self,
        mesh_name: str,
        base_positions_bl: List[Tuple[float,float,float]],
        faces: List[MeshFace],
        face_vertices: List[FaceVertex],
        frame0_uvs: List[Tuple[float,float]],
        scale_geom: float,
        trs_scale: float,
        materials: List[VfxMaterial],
        mesh_local_mat_to_global: List[int],
        morph_frames_bl: Optional[List[List[Tuple[float,float,float]]]],  # includes frame0 positions in blender basis
        bake_constant_scale: bool = True,
        trs_frames_rf: Optional[List[Tuple[Tuple[float,float,float],Tuple[float,float,float,float],Tuple[float,float,float]]]] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """
        Returns (gltf_mesh_index, info dict used by animation builder)
        """
        # group faces by global material (or None)
        groups: Dict[Optional[int], List[int]] = {}
        for fi, f in enumerate(faces):
            mat_idx = f.material_index
            glb_mat: Optional[int] = None
            if mat_idx >= 0 and mat_idx < len(mesh_local_mat_to_global):
                glb = mesh_local_mat_to_global[mat_idx]
                if 0 <= glb < len(materials):
                    glb_mat = glb
            groups.setdefault(glb_mat, []).append(fi)

        primitives: List[Dict[str, Any]] = []
        prim_infos: List[Dict[str, Any]] = []  # for morph + anim mapping
        for glb_mat, face_ids in groups.items():
            # build vertex stream with uv splits
            out_pos: List[Tuple[float,float,float]] = []
            out_uv: List[Tuple[float,float]] = []
            out_src_pi: List[int] = []  # original position index for each out vert
            out_idx: List[int] = []
            vmap: Dict[Tuple[int,int,int], int] = {}  # (pos_index, u_q, v_q) -> out index

            def uv_key(uv: Tuple[float,float]) -> Tuple[int,int]:
                # quantize to stabilize keys
                return (int(round(uv[0]*100000.0)), int(round(uv[1]*100000.0)))

            for fi in face_ids:
                f = faces[fi]
                tri = []
                for corner in range(3):
                    fvi = f.face_vertex_indices[corner]
                    pi = face_vertices[fvi].vertex_index
                    uv = frame0_uvs[fi*3 + corner]
                    uq, vq = uv_key(uv)
                    key = (pi, uq, vq)
                    oi = vmap.get(key)
                    if oi is None:
                        oi = len(out_pos)
                        vmap[key] = oi
                        px,py,pz = base_positions_bl[pi]
                        out_pos.append((px*scale_geom, py*scale_geom, pz*scale_geom))
                        out_uv.append(uv)
                        out_src_pi.append(pi)
                    tri.append(oi)
                out_idx.extend(tri)

            # indices component type
            max_index = max(out_idx) if out_idx else 0
            if max_index <= 65535:
                idx_ct = 5123  # UNSIGNED_SHORT
                idx_pack = lambda arr: struct.pack("<" + "H"*len(arr), *arr)
                idx_blen = 2
            else:
                idx_ct = 5125  # UNSIGNED_INT
                idx_pack = lambda arr: struct.pack("<" + "I"*len(arr), *arr)
                idx_blen = 4

            # POSITION accessor
            pos_bytes = b"".join(struct.pack("<fff", *p) for p in out_pos)
            pos_bv, _ = self._add_bufferview(pos_bytes, target=34962)
            # compute min/max
            if out_pos:
                mn = [min(p[i] for p in out_pos) for i in range(3)]
                mx = [max(p[i] for p in out_pos) for i in range(3)]
            else:
                mn = [0.0,0.0,0.0]; mx = [0.0,0.0,0.0]
            pos_acc = self._add_accessor(pos_bv, 0, 5126, len(out_pos), "VEC3",
                                         minv=[float(mn[0]),float(mn[1]),float(mn[2])],
                                         maxv=[float(mx[0]),float(mx[1]),float(mx[2])])

            # TEXCOORD_0 accessor
            uv_bytes = b"".join(struct.pack("<ff", float(uv[0]), float(uv[1])) for uv in out_uv)
            uv_bv, _ = self._add_bufferview(uv_bytes, target=34962)
            uv_acc = self._add_accessor(uv_bv, 0, 5126, len(out_uv), "VEC2")

            # indices accessor
            idx_bytes = idx_pack(out_idx)
            idx_bv, _ = self._add_bufferview(idx_bytes, target=34963)
            idx_acc = self._add_accessor(idx_bv, 0, idx_ct, len(out_idx), "SCALAR")

            prim: Dict[str, Any] = {
                "attributes": {"POSITION": pos_acc, "TEXCOORD_0": uv_acc},
                "indices": idx_acc,
            }

            # material
            gltf_mat_i = None
            if glb_mat is not None and 0 <= glb_mat < len(materials):
                gltf_mat_i = self._ensure_material(materials[glb_mat])
            if gltf_mat_i is not None:
                prim["material"] = gltf_mat_i

            # morph targets
            morph_info = {}
            if morph_frames_bl is not None and len(morph_frames_bl) > 1:
                targets = []
                # frame0 in morph_frames_bl[0], deltas for each subsequent frame
                base0 = morph_frames_bl[0]
                for fi_m in range(1, len(morph_frames_bl)):
                    frp = morph_frames_bl[fi_m]
                    deltas: List[Tuple[float,float,float]] = []
                    for pi in out_src_pi:
                        bx,by,bz = base0[pi]
                        fx,fy,fz = frp[pi]
                        dx,dy,dz = (fx-bx)*scale_geom, (fy-by)*scale_geom, (fz-bz)*scale_geom
                        deltas.append((dx,dy,dz))
                    tgt_bytes = b"".join(struct.pack("<fff", *d) for d in deltas)
                    tgt_bv, _ = self._add_bufferview(tgt_bytes, target=34962)
                    if deltas:
                        mn = [min(d[i] for d in deltas) for i in range(3)]
                        mx = [max(d[i] for d in deltas) for i in range(3)]
                    else:
                        mn = [0.0,0.0,0.0]; mx=[0.0,0.0,0.0]
                    tgt_acc = self._add_accessor(tgt_bv, 0, 5126, len(deltas), "VEC3",
                                                 minv=[float(mn[0]),float(mn[1]),float(mn[2])],
                                                 maxv=[float(mx[0]),float(mx[1]),float(mx[2])])
                    targets.append({"POSITION": tgt_acc})
                prim["targets"] = targets
                # default weights = all 0
                morph_info = {"targets_count": len(targets)}
            primitives.append(prim)
            prim_infos.append({"morph": morph_info, "out_vert_count": len(out_pos)})

        gltf_mesh = {"name": mesh_name, "primitives": primitives}

        # Provide default morph weights on the mesh for Blender/glTF importer compatibility.

        # Blender may not create Shape Keys unless mesh.weights exists and matches morph target count.

        _mw = 0

        for _p in primitives:

            _tc = len(_p.get("targets", []) or [])

            if _tc > _mw:

                _mw = _tc

        if _mw > 0:

            gltf_mesh["weights"] = [0.0] * _mw
        self.g["meshes"].append(gltf_mesh)
        mesh_i = len(self.g["meshes"]) - 1
        return mesh_i, {"prim_infos": prim_infos}

    def add_animation(self, anim: Dict[str, Any]) -> int:
        self.g["animations"].append(anim)
        return len(self.g["animations"]) - 1

    def add_anim_sampler(self, anim: Dict[str, Any], input_acc: int, output_acc: int, interp: str="LINEAR") -> int:
        sam = {"input": input_acc, "output": output_acc, "interpolation": interp}
        anim.setdefault("samplers", []).append(sam)
        return len(anim["samplers"]) - 1

    def add_anim_channel(self, anim: Dict[str, Any], sampler_i: int, node_i: int, path: str) -> None:
        anim.setdefault("channels", []).append({"sampler": sampler_i, "target": {"node": node_i, "path": path}})

    def add_accessor_f32(self, values: List[float], acc_type: str, elem_n: int) -> int:
        # values length = count*elem_n
        data = struct.pack("<" + "f"*len(values), *values) if values else b""
        bv, _ = self._add_bufferview(data, target=None)
        count = (len(values) // elem_n) if elem_n else 0
        return self._add_accessor(bv, 0, 5126, count, acc_type)

    def finalize(self) -> Tuple[Dict[str, Any], bytes]:
        self.g["buffers"][0]["byteLength"] = len(self._bin)
        return self.g, bytes(self._bin)

# ----------------------------
# Export logic
# ----------------------------

def decompress_positions_bl(fr: MeshFrame, num_vertices: int) -> List[Tuple[float,float,float]]:
    assert fr.center_rf is not None and fr.mult_rf is not None and fr.pos_s16 is not None
    cx,cy,cz = fr.center_rf
    mx,my,mz = fr.mult_rf
    out: List[Tuple[float,float,float]] = []
    for (ix,iy,iz) in fr.pos_s16:
        # correct decompression: center + mult * raw_s16 (no /32767)
        vx = cx + (mx * float(ix))
        vy = cy + (my * float(iy))
        vz = cz + (mz * float(iz))
        out.append(rf_to_blender((vx, vy, vz)))
    return out


# ----------------------------
# VFX patching / roundtrip helpers
# ----------------------------

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()

def roundtrip_copy(in_path: str, out_path: str, verify: bool) -> None:
    data = open(in_path, "rb").read()
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(data)
    print("Wrote:", out_path)
    if verify:
        h1 = _sha256_file(in_path)
        h2 = _sha256_file(out_path)
        if h1 == h2:
            print(f"[ROUNDTRIP_OK] byte-identical (SHA256={h2})")
        else:
            print(f"[ROUNDTRIP_FAIL] SHA256 mismatch: in={h1} out={h2}")
            raise SystemExit(1)

def _write_vec3_f32(buf: bytearray, ofs: int, v: Tuple[float,float,float]) -> None:
    buf[ofs:ofs+12] = struct.pack("<fff", float(v[0]), float(v[1]), float(v[2]))

def _write_pos_s16(buf: bytearray, ofs: int, pos_s16: List[Tuple[int,int,int]]) -> None:
    # pack as <hhh repeated
    out = bytearray()
    out_extend = out.extend
    for (x,y,z) in pos_s16:
        out_extend(struct.pack("<hhh", int(x), int(y), int(z)))
    buf[ofs:ofs+len(out)] = out

def encode_positions_to_s16(positions_bl: List[Tuple[float,float,float]]) -> Tuple[Tuple[float,float,float], Tuple[float,float,float], List[Tuple[int,int,int]]]:
    """Encode Blender-basis positions into RF stored-basis center/mult + signed-16 triplets.
    Decodes as: stored_pos = center + mult * s16.
    """
    if not positions_bl:
        return (0.0,0.0,0.0), (1.0,1.0,1.0), []
    # convert to stored basis
    ps = [blender_to_rf(p) for p in positions_bl]
    xs = [p[0] for p in ps]; ys = [p[1] for p in ps]; zs = [p[2] for p in ps]
    mins = (min(xs), min(ys), min(zs))
    maxs = (max(xs), max(ys), max(zs))
    mult = [0.0,0.0,0.0]
    center = [0.0,0.0,0.0]
    for ax in range(3):
        mn = mins[ax]; mx = maxs[ax]
        if abs(mx - mn) < 1e-20:
            mult[ax] = 1.0
            center[ax] = mn
        else:
            m = (mx - mn) / 65535.0
            mult[ax] = m
            center[ax] = mn + m * 32768.0
    cx,cy,cz = center
    mx,my,mz = mult
    pos_s16: List[Tuple[int,int,int]] = []
    def clamp16(v: int) -> int:
        return -32768 if v < -32768 else (32767 if v > 32767 else v)
    for (sx,sy,sz) in ps:
        ix = 0 if mx == 0.0 else int(round((sx - cx) / mx))
        iy = 0 if my == 0.0 else int(round((sy - cy) / my))
        iz = 0 if mz == 0.0 else int(round((sz - cz) / mz))
        pos_s16.append((clamp16(ix), clamp16(iy), clamp16(iz)))
    return (float(cx),float(cy),float(cz)), (float(mx),float(my),float(mz)), pos_s16

# ----------------------------
# Minimal glTF reader (POSITION + morph POSITION deltas)
# ----------------------------

def _gltf_load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _gltf_load_bin(g: Dict[str, Any], gltf_path: str) -> bytes:
    bufs = g.get("buffers") or []
    if not bufs:
        return b""
    uri = bufs[0].get("uri") or ""
    if uri.startswith("data:"):
        # data:application/octet-stream;base64,...
        b64 = uri.split(",", 1)[1]
        return base64.b64decode(b64)
    base = os.path.dirname(os.path.abspath(gltf_path))
    bin_path = os.path.join(base, uri)
    return open(bin_path, "rb").read()

def _gltf_accessor_read_vec3_f32(g: Dict[str, Any], bin_data: bytes, acc_i: int) -> List[Tuple[float,float,float]]:
    acc = g["accessors"][acc_i]
    if acc.get("componentType") != 5126 or acc.get("type") != "VEC3":
        raise ValueError(f"Accessor {acc_i} not float32 VEC3")
    count = int(acc["count"])
    # Some accessors have no bufferView (e.g. zero-displacement morph targets).
    # In that case, all values are zero.
    if "bufferView" not in acc:
        return [(0.0, 0.0, 0.0)] * count
    bv = g["bufferViews"][acc["bufferView"]]
    bv_ofs = int(bv.get("byteOffset", 0))
    acc_ofs = int(acc.get("byteOffset", 0))
    start = bv_ofs + acc_ofs
    stride = int(bv.get("byteStride", 12))
    out: List[Tuple[float,float,float]] = []
    for i in range(count):
        o = start + i*stride
        x,y,z = struct.unpack_from("<fff", bin_data, o)
        out.append((float(x), float(y), float(z)))
    return out


def _gltf_accessor_read_vec2_f32(g: Dict[str, Any], bin_data: bytes, acc_i: int) -> List[Tuple[float,float]]:
    acc = g["accessors"][acc_i]
    if acc.get("componentType") != 5126 or acc.get("type") != "VEC2":
        raise ValueError(f"Accessor {acc_i} not float32 VEC2")
    count = int(acc["count"])
    bv = g["bufferViews"][acc["bufferView"]]
    bv_ofs = int(bv.get("byteOffset", 0))
    acc_ofs = int(acc.get("byteOffset", 0))
    start = bv_ofs + acc_ofs
    stride = int(bv.get("byteStride", 8))
    out: List[Tuple[float,float]] = []
    for i in range(count):
        o = start + i*stride
        x,y = struct.unpack_from("<ff", bin_data, o)
        out.append((float(x), float(y)))
    return out

def _gltf_accessor_read_scalar_f32(g: Dict[str, Any], bin_data: bytes, acc_i: int) -> List[float]:
    acc = g["accessors"][acc_i]
    if acc.get("componentType") != 5126 or acc.get("type") != "SCALAR":
        raise ValueError(f"Accessor {acc_i} not float32 SCALAR")
    count = int(acc["count"])
    bv = g["bufferViews"][acc["bufferView"]]
    bv_ofs = int(bv.get("byteOffset", 0))
    acc_ofs = int(acc.get("byteOffset", 0))
    start = bv_ofs + acc_ofs
    stride = int(bv.get("byteStride", 4))
    out: List[float] = []
    for i in range(count):
        o = start + i*stride
        (x,) = struct.unpack_from("<f", bin_data, o)
        out.append(float(x))
    return out

def _gltf_accessor_read_indices(g: Dict[str, Any], bin_data: bytes, acc_i: int) -> List[int]:
    acc = g["accessors"][acc_i]
    if acc.get("type") != "SCALAR":
        raise ValueError(f"Accessor {acc_i} indices not SCALAR")
    ct = int(acc.get("componentType"))
    fmt = None
    if ct == 5121:   # u8
        fmt = "B"; size = 1
    elif ct == 5123: # u16
        fmt = "H"; size = 2
    elif ct == 5125: # u32
        fmt = "I"; size = 4
    else:
        raise ValueError(f"Unsupported index componentType {ct}")
    count = int(acc["count"])
    bv = g["bufferViews"][acc["bufferView"]]
    bv_ofs = int(bv.get("byteOffset", 0))
    acc_ofs = int(acc.get("byteOffset", 0))
    start = bv_ofs + acc_ofs
    stride = int(bv.get("byteStride", size))
    out: List[int] = []
    for i in range(count):
        o = start + i*stride
        (x,) = struct.unpack_from("<"+fmt, bin_data, o)
        out.append(int(x))
    return out

def gltf_collect_mesh_data(gltf_path: str) -> Dict[str, Dict[str, Any]]:
    g = _gltf_load_json(gltf_path)
    bin_data = _gltf_load_bin(g, gltf_path)
    out: Dict[str, Dict[str, Any]] = {}
    nodes = g.get("nodes") or []
    meshes = g.get("meshes") or []
    for ni, node in enumerate(nodes):
        if "mesh" not in node:
            continue
        name = (node.get("name") or f"node{ni}").strip()
        # RF round-trip metadata lives on the mesh node extras.
        # We use this to reverse any geometry-space baking we applied when exporting to glTF.
        rf_meta = (node.get("extras") or {}).get("rf_vfx") or {}
        baked_geom_scale = rf_meta.get("baked_geom_scale")
        mesh = meshes[int(node["mesh"])]
        prim = (mesh.get("primitives") or [None])[0]
        if not prim:
            continue
        attrs = prim.get("attributes") or {}
        if "POSITION" not in attrs:
            continue
        pos = _gltf_accessor_read_vec3_f32(g, bin_data, int(attrs["POSITION"]))
        targets = []
        for tgt in prim.get("targets") or []:
            if "POSITION" in tgt:
                targets.append(_gltf_accessor_read_vec3_f32(g, bin_data, int(tgt["POSITION"])))
        rec: Dict[str, Any] = {"name": name, "positions": pos, "targets": targets}
        # Collect indices for face-topology matching (needed for patch vertex dedup)
        if prim.get("indices") is not None:
            try:
                rec["indices"] = _gltf_accessor_read_indices(g, bin_data, int(prim["indices"]))
            except Exception:
                pass
        if isinstance(baked_geom_scale, (list, tuple)) and len(baked_geom_scale) == 3:
            try:
                rec["baked_geom_scale"] = (float(baked_geom_scale[0]), float(baked_geom_scale[1]), float(baked_geom_scale[2]))
            except Exception:
                pass
        out[name.lower()] = rec
    return out

def _dedup_positions(pos: List[Tuple[float,float,float]],
                     targets: List[List[Tuple[float,float,float]]],
                     tol: float = 1e-7
                     ) -> Tuple[List[Tuple[float,float,float]], List[List[Tuple[float,float,float]]]]:
    """Merge duplicate vertices that Blender's glTF exporter creates at normal/UV seams.

    Blender 5.0+ may ignore export_normals=False, splitting vertices.  Patch mode
    only needs positions + morph deltas, so we can safely merge them back by position.
    Returns (deduped_positions, deduped_targets).

    NOTE: This is a fallback; face-topology remapping (_remap_split_verts) is preferred.
    """
    if not pos:
        return pos, targets
    inv = 1.0 / tol
    seen: Dict[Tuple[int,int,int], int] = {}
    remap: List[int] = []
    deduped_pos: List[Tuple[float,float,float]] = []
    for p in pos:
        key = (int(round(p[0]*inv)), int(round(p[1]*inv)), int(round(p[2]*inv)))
        if key in seen:
            remap.append(seen[key])
        else:
            idx = len(deduped_pos)
            seen[key] = idx
            remap.append(idx)
            deduped_pos.append(p)
    if len(deduped_pos) == len(pos):
        return pos, targets
    deduped_targets: List[List[Tuple[float,float,float]]] = []
    for tgt in targets:
        dt: List[Tuple[float,float,float]] = [(0.0,0.0,0.0)] * len(deduped_pos)
        used = [False] * len(deduped_pos)
        for old_i, new_i in enumerate(remap):
            if not used[new_i]:
                dt[new_i] = tgt[old_i]
                used[new_i] = True
        deduped_targets.append(dt)
    return deduped_pos, deduped_targets


def _remap_split_verts(gltf_positions: List[Tuple[float,float,float]],
                       gltf_targets: List[List[Tuple[float,float,float]]],
                       gltf_indices: List[int],
                       vfx_mesh: "VfxMesh",
                       ) -> Tuple[List[Tuple[float,float,float]], List[List[Tuple[float,float,float]]]]:
    """Remap Blender's split glTF vertices back to VFX vertex order using face topology.

    Blender 5.0+ duplicates vertices at normal/UV seams.  The face ORDER is preserved,
    so we match each face corner: glTF face i corner j <-> VFX face i corner j.
    This correctly handles vertices that share positions but are distinct in the VFX
    (e.g. junction vertices with different normals).

    Returns (positions, targets) with len == vfx_mesh.num_vertices.
    """
    num_vfx_verts = vfx_mesh.num_vertices
    num_faces = len(vfx_mesh.faces)

    # Build VFX vertex index for each face corner: face i, corner j -> vertex_index
    remapped_pos: List[Optional[Tuple[float,float,float]]] = [None] * num_vfx_verts
    remapped_targets: List[List[Optional[Tuple[float,float,float]]]] = [
        [None] * num_vfx_verts for _ in gltf_targets
    ]

    for fi in range(num_faces):
        for corner in range(3):
            # VFX vertex index for this face corner
            fvi = vfx_mesh.faces[fi].face_vertex_indices[corner]
            vi = vfx_mesh.face_vertices[fvi].vertex_index

            # Corresponding glTF vertex index
            gltf_vi = gltf_indices[fi * 3 + corner]

            if remapped_pos[vi] is None:
                remapped_pos[vi] = gltf_positions[gltf_vi]
                for ti, tgt in enumerate(gltf_targets):
                    remapped_targets[ti][vi] = tgt[gltf_vi]

    # Fill any unmapped vertices (shouldn't happen with valid data, but be safe)
    zero = (0.0, 0.0, 0.0)
    for i in range(num_vfx_verts):
        if remapped_pos[i] is None:
            remapped_pos[i] = zero
            for ti in range(len(gltf_targets)):
                remapped_targets[ti][i] = zero

    return remapped_pos, [t for t in remapped_targets]  # type: ignore

def patch_vfx_meshes_from_gltf(template_vfx: str, gltf_path: str, out_vfx: str,
                              gltf_scale: float = 1.0,
                              only_meshes: Optional[List[str]] = None,
                              debug: bool = False) -> None:
    raw = bytearray(open(template_vfx, "rb").read())
    hdr, mats, meshes, dummies, _particles = parse_vfx(template_vfx)
    gmap = gltf_collect_mesh_data(gltf_path)
    only_set = set([m.lower() for m in only_meshes]) if only_meshes else None

    patched_any = False
    for m in meshes:
        key = m.name.lower()
        if only_set is not None and key not in only_set:
            continue
        if key not in gmap:
            raise RuntimeError(f"glTF missing mesh/node named '{m.name}' (case-insensitive)")
        gd = gmap[key]
        pos = gd["positions"]
        if abs(gltf_scale) < 1e-20:
            raise ValueError("gltf_scale cannot be 0")
        pos_bl = [(p[0]/gltf_scale, p[1]/gltf_scale, p[2]/gltf_scale) for p in pos]

        # Blender 5.0+ may split vertices at normal/UV seams even with export_normals=False.
        # For patch mode we only need positions, so remap split vertices back to VFX order.
        targets_raw = gd.get("targets") or []
        if len(pos_bl) != m.num_vertices:
            gltf_indices = gd.get("indices")
            if gltf_indices is not None and len(gltf_indices) == len(m.faces) * 3:
                # Preferred: face-topology remapping (handles shared-position vertices correctly)
                # Scale targets by 1/gltf_scale before remapping
                targets_scaled = [[(d[0]/gltf_scale, d[1]/gltf_scale, d[2]/gltf_scale) for d in tgt] for tgt in targets_raw]
                pos_bl, targets_scaled = _remap_split_verts(pos_bl, targets_scaled, gltf_indices, m)
                # Mark targets as already scaled so we skip gltf_scale division later
                targets_raw = targets_scaled
                targets_prescaled = True
                if debug:
                    print(f"[PATCH_REMAP] mesh='{m.name}' remapped {len(pos)} -> {len(pos_bl)} vertices via face topology")
            else:
                # Fallback: position-based dedup (may fail if original has shared-position verts)
                pos_bl, targets_raw = _dedup_positions(pos_bl, targets_raw)
                targets_prescaled = False
                if debug:
                    print(f"[PATCH_DEDUP] mesh='{m.name}' deduped {len(pos)} -> {len(pos_bl)} vertices by position")
        else:
            targets_prescaled = False

        # If the glTF node indicates we baked a constant geometry scale (e.g. pivot_scale*scale
        # on keyframed meshes like the CTF flagpole), reverse that bake here so we write
        # template-space vertices back into the VFX. Otherwise the engine will apply the
        # pivot/keyframe scale again and the mesh can become tiny/invisible in-game.
        bgs = gd.get("baked_geom_scale")
        if bgs is not None:
            sx, sy, sz = bgs
            if abs(sx) < 1e-20 or abs(sy) < 1e-20 or abs(sz) < 1e-20:
                raise RuntimeError(f"Invalid baked_geom_scale for '{m.name}': {bgs}")
            pos_bl = [(p[0]/sx, p[1]/sy, p[2]/sz) for p in pos_bl]
            if debug:
                print(f"[PATCH_UNBAKE] mesh='{m.name}' baked_geom_scale=({sx:.6f},{sy:.6f},{sz:.6f})")
        if len(pos_bl) != m.num_vertices:
            raise RuntimeError(f"Vertex count mismatch for '{m.name}': glTF={len(pos_bl)} vfx={m.num_vertices}. Topology changes are not supported.")

        if m.morph:
            targets = targets_raw  # already deduped if needed
            expected = max(0, m.num_frames - 1)
            if len(targets) != expected:
                raise RuntimeError(f"Morph target count mismatch for '{m.name}': glTF targets={len(targets)} expected={expected} (vfx frames={m.num_frames}).")
            # frame0 = base, frame i = base + target(i-1)
            for fi in range(m.num_frames):
                fr = m.frames[fi]
                if fr.center_ofs is None or fr.mult_ofs is None or fr.pos_ofs is None:
                    raise RuntimeError(f"Missing patch offsets for mesh '{m.name}' frame {fi}")
                if fi == 0:
                    frame_bl = pos_bl
                else:
                    deltas = targets[fi-1]
                    if len(deltas) != m.num_vertices:
                        raise RuntimeError(f"Target vertex mismatch for '{m.name}' target {fi-1}")
                    # Apply the same unbake to morph deltas if present.
                    if targets_prescaled:
                        # Targets already in Blender units from face-topology remap
                        if bgs is not None:
                            sx, sy, sz = bgs
                            frame_bl = [(pos_bl[i][0] + deltas[i][0]/sx,
                                         pos_bl[i][1] + deltas[i][1]/sy,
                                         pos_bl[i][2] + deltas[i][2]/sz) for i in range(m.num_vertices)]
                        else:
                            frame_bl = [(pos_bl[i][0] + deltas[i][0],
                                         pos_bl[i][1] + deltas[i][1],
                                         pos_bl[i][2] + deltas[i][2]) for i in range(m.num_vertices)]
                    elif bgs is not None:
                        sx, sy, sz = bgs
                        frame_bl = [(pos_bl[i][0] + (deltas[i][0]/gltf_scale)/sx,
                                     pos_bl[i][1] + (deltas[i][1]/gltf_scale)/sy,
                                     pos_bl[i][2] + (deltas[i][2]/gltf_scale)/sz) for i in range(m.num_vertices)]
                    else:
                        frame_bl = [(pos_bl[i][0] + deltas[i][0]/gltf_scale,
                                     pos_bl[i][1] + deltas[i][1]/gltf_scale,
                                     pos_bl[i][2] + deltas[i][2]/gltf_scale) for i in range(m.num_vertices)]
                center_rf, mult_rf, pos_s16 = encode_positions_to_s16(frame_bl)
                _write_vec3_f32(raw, fr.center_ofs, center_rf)
                _write_vec3_f32(raw, fr.mult_ofs, mult_rf)
                _write_pos_s16(raw, fr.pos_ofs, pos_s16)
            patched_any = True
            if debug:
                print(f"[PATCH_GLTF] morphed mesh='{m.name}' frames={m.num_frames} patched")
        else:
            # patch only frame0 positions
            fr = m.frames[0]
            if fr.center_ofs is None or fr.mult_ofs is None or fr.pos_ofs is None:
                raise RuntimeError(f"Missing patch offsets for mesh '{m.name}' frame0")
            center_rf, mult_rf, pos_s16 = encode_positions_to_s16(pos_bl)
            _write_vec3_f32(raw, fr.center_ofs, center_rf)
            _write_vec3_f32(raw, fr.mult_ofs, mult_rf)
            _write_pos_s16(raw, fr.pos_ofs, pos_s16)
            patched_any = True
            if debug:
                print(f"[PATCH_GLTF] mesh='{m.name}' patched")

    if not patched_any:
        raise RuntimeError("No meshes were patched (check names/--only-mesh)")

    os.makedirs(os.path.dirname(os.path.abspath(out_vfx)) or ".", exist_ok=True)
    with open(out_vfx, "wb") as f:
        f.write(raw)
    print("Wrote:", out_vfx)

def patch_vfx_apply_mesh_offsets(template_vfx: str, out_vfx: str, mesh_offsets: Dict[str, Tuple[float,float,float]], scale: float, debug: bool=False) -> None:
    raw = bytearray(open(template_vfx, "rb").read())
    hdr, mats, meshes, dummies, _particles = parse_vfx(template_vfx)
    # convert to pre-scale and then to stored basis
    for m in meshes:
        key = m.name.lower()
        if key not in mesh_offsets:
            continue
        ox,oy,oz = mesh_offsets[key]
        if abs(scale) > 1e-12:
            ox /= scale; oy /= scale; oz /= scale
        off_rf = blender_to_rf((ox,oy,oz))
        for fi, fr in enumerate(m.frames):
            if fr.center_ofs is None:
                continue
            if fr.center_rf is None:
                continue
            cx,cy,cz = fr.center_rf
            nx,ny,nz = (cx + off_rf[0], cy + off_rf[1], cz + off_rf[2])
            _write_vec3_f32(raw, fr.center_ofs, (nx,ny,nz))
        if debug:
            print(f"[DEBUG_OFFSET_VFX] mesh='{m.name}' applied offset(bl)=({mesh_offsets[key][0]:.6g},{mesh_offsets[key][1]:.6g},{mesh_offsets[key][2]:.6g})")
    os.makedirs(os.path.dirname(os.path.abspath(out_vfx)) or ".", exist_ok=True)
    with open(out_vfx, "wb") as f:
        f.write(raw)
    print("Wrote:", out_vfx)


def should_bake_constant_scale(scales_rf: List[Tuple[float,float,float]]) -> Optional[Tuple[float,float,float]]:
    if not scales_rf:
        return None
    # compare all scales against first
    s0 = scales_rf[0]
    def close(a,b): return abs(a-b) <= 1e-6
    for s in scales_rf[1:]:
        if not (close(s[0],s0[0]) and close(s[1],s0[1]) and close(s[2],s0[2])):
            return None
    # bake only if meaningfully different from 1
    if abs(s0[0]-1.0) < 1e-6 and abs(s0[1]-1.0) < 1e-6 and abs(s0[2]-1.0) < 1e-6:
        return None
    return s0

def export_gltf(
    in_path: str,
    out_gltf: str,
    debug_frames: bool,
    scale: float,
    trs_scale: Optional[float],
    mesh_offsets: Optional[Dict[str, Tuple[float,float,float]]] = None,
) -> None:
    hdr, mats, meshes, dummies, particles = parse_vfx(in_path)

    if trs_scale is None:
        trs_scale = scale

    mesh_offsets_l: Dict[str, Tuple[float,float,float]] = {}
    if mesh_offsets:
        for k,v in mesh_offsets.items():
            mesh_offsets_l[k.lower()] = v

    # --- Debug print ---
    if debug_frames:
        for m in meshes:
            print(f"[DEBUG_FRAMES] mesh='{m.name}' fps={m.fps} frames={m.num_frames} morph={m.morph} flags=0x{m.flags_raw:08X}")

    gb = GltfBuilder()

    # root node
    root_idx = gb.add_node("__VFX_ROOT__")
    gb.g["scenes"][0]["nodes"] = [root_idx]
    gb.g["scenes"][0]["extras"] = {"rf_vfx": {"end_frame": int(hdr.end_frame)}}

    # Create glTF nodes for meshes and dummies, then parent them
    node_by_name: Dict[str, int] = {"__VFX_ROOT__": root_idx}
    pending_parent: List[Tuple[int,str]] = []

    # --- Mesh nodes ---
    mesh_node_infos: List[Dict[str, Any]] = []
    for m in meshes:
        # base geometry
        base_bl = decompress_positions_bl(m.frames[0], m.num_vertices)
        frame0_uvs = m.frames[0].uvs or [(0.0,0.0)]*(3*m.num_faces)

        morph_frames_bl: Optional[List[List[Tuple[float,float,float]]]] = None
        if m.morph:
            morph_frames_bl = []
            for fr in m.frames:
                morph_frames_bl.append(decompress_positions_bl(fr, m.num_vertices))

        # per-frame TRS (non-keyframed only)
        trs_frames = None
        if (not m.morph) and (not m.is_keyframed) and m.num_frames > 0:
            trs_frames = []
            for fr in m.frames:
                t_rf = fr.translation_rf or (0.0,0.0,0.0)
                r_raw = fr.rotation_raw or (0.0,0.0,0.0,1.0)
                s_rf = fr.scale_rf or (1.0,1.0,1.0)
                trs_frames.append((rf_to_blender(t_rf), quat_rf_to_blender(r_raw), (abs(s_rf[0]), abs(s_rf[2]), abs(s_rf[1]))))

        # Pivot scale (pre-keyframe) — this is the missing ingredient for correct scale on keyframed meshes (e.g. flagpole)
        # In CTFflag-blue, pivot_scale ~0.13495 and scale keyframe is 1.2, so effective scale is 0.161943 (what we debugged in Chat 04).
        baked_scale_from_pivot: Optional[Tuple[float,float,float]] = None
        if (not m.morph) and m.pivot_scale_rf is not None:
            psx, psy, psz = m.pivot_scale_rf
            baked_scale_from_pivot = (abs(psx), abs(psz), abs(psy))

        skip_scale_anim = False
        baked_geom_scale: Optional[Tuple[float,float,float]] = None
        if (not m.morph) and m.is_keyframed and m.keyframes and baked_scale_from_pivot is not None:
            # if scale keys are constant, bake pivot_scale * scale into geometry and remove scale channel
            if m.keyframes.s_values:
                s0 = m.keyframes.s_values[0]
                if all(abs(s[0]-s0[0])<1e-6 and abs(s[1]-s0[1])<1e-6 and abs(s[2]-s0[2])<1e-6 for s in m.keyframes.s_values):
                    eff = (baked_scale_from_pivot[0]*s0[0], baked_scale_from_pivot[1]*s0[1], baked_scale_from_pivot[2]*s0[2])
                    baked_geom_scale = eff
                    if debug_frames:
                        print(f"[DEBUG_SCALE] mesh='{m.name}' baked geom_scale=({eff[0]:.6f},{eff[1]:.6f},{eff[2]:.6f})")
                    base_bl = [(p[0]*eff[0], p[1]*eff[1], p[2]*eff[2]) for p in base_bl]
                    # keyframed non-morph meshes won't have morph frames; but keep for completeness
                    if morph_frames_bl is not None:
                        morph_frames_bl = [[(p[0]*eff[0], p[1]*eff[1], p[2]*eff[2]) for p in fr] for fr in morph_frames_bl]
                    skip_scale_anim = True
        
        # bake constant scale for non-morph meshes (avoid scaling children)
        geom_scale_vec = None
        if trs_frames is not None:
            scales_rf = [fr.scale_rf for fr in m.frames if fr.scale_rf is not None]
            geom_scale_vec = should_bake_constant_scale(scales_rf)
            if geom_scale_vec is not None:
                # apply to vertices; remove scale from TRS frames
                sx,sy,sz = geom_scale_vec
                # convert scale axes to blender axis order (abs, swap)
                bake_s = (abs(sx), abs(sz), abs(sy))
                baked_geom_scale = bake_s
                if debug_frames:
                    print(f"[DEBUG_SCALE] mesh='{m.name}' baked geom_scale=({bake_s[0]:.6f},{bake_s[1]:.6f},{bake_s[2]:.6f})")
                # fold into base + morph positions in blender basis, before global 'scale'
                base_bl = [(p[0]*bake_s[0], p[1]*bake_s[1], p[2]*bake_s[2]) for p in base_bl]
                if morph_frames_bl is not None:
                    morph_frames_bl = [[(p[0]*bake_s[0], p[1]*bake_s[1], p[2]*bake_s[2]) for p in fr] for fr in morph_frames_bl]
                # overwrite trs_frames scales to 1
                trs_frames2 = []
                for (t,r,_s) in trs_frames:
                    trs_frames2.append((t,r,(1.0,1.0,1.0)))
                trs_frames = trs_frames2


        # --- Optional per-mesh vertex offset (applied to base + ALL morph frames) ---
        # Offsets are specified in OUTPUT units (after --scale). We convert to pre-scale units here.
        off = mesh_offsets_l.get(m.name.lower())
        if off is not None:
            ox, oy, oz = off
            if abs(scale) > 1e-12:
                ox /= scale; oy /= scale; oz /= scale
            base_bl = [(p[0] + ox, p[1] + oy, p[2] + oz) for p in base_bl]
            if morph_frames_bl is not None:
                morph_frames_bl = [[(p[0] + ox, p[1] + oy, p[2] + oz) for p in fr] for fr in morph_frames_bl]
            if debug_frames:
                print(f"[DEBUG_OFFSET] mesh='{m.name}' applied mesh_offset=({off[0]:.6g},{off[1]:.6g},{off[2]:.6g})")

        mesh_i, info = gb.add_mesh_from_faces(
            mesh_name=m.name,
            base_positions_bl=base_bl,
            faces=m.faces,
            face_vertices=m.face_vertices,
            frame0_uvs=frame0_uvs,
            scale_geom=scale,
            trs_scale=trs_scale,
            materials=mats,
            mesh_local_mat_to_global=m.materials_indices,
            morph_frames_bl=morph_frames_bl,
        )

        # Mesh node (holds the actual mesh + metadata)
        mesh_node_i = gb.add_node(m.name, mesh_index=mesh_i)

        # Keyframed meshes have a "pivot_*" pre-transform that must be applied BEFORE keyframes.
        # glTF composes parent*child, so we model:
        #   world = KEY * PIVOT * local
        # by making a KEY parent (animated) and a PIVOT child (static) that holds the mesh.
        anim_node_i = mesh_node_i
        key_node_i = None

        def _pivot_is_nontrivial(mm: VfxMesh) -> bool:
            if mm.pivot_translation_rf is None and mm.pivot_rotation_raw is None and mm.pivot_scale_rf is None:
                return False
            t = mm.pivot_translation_rf or (0.0,0.0,0.0)
            r = mm.pivot_rotation_raw or (0.0,0.0,0.0,1.0)
            s = mm.pivot_scale_rf or (1.0,1.0,1.0)
            if (abs(t[0]) + abs(t[1]) + abs(t[2])) > 1e-9:
                return True
            if (abs(r[0]) + abs(r[1]) + abs(r[2]) + abs(r[3]-1.0)) > 1e-9:
                return True
            if (abs(s[0]-1.0) + abs(s[1]-1.0) + abs(s[2]-1.0)) > 1e-9:
                return True
            return False

        if (not m.morph) and m.is_keyframed and m.keyframes and _pivot_is_nontrivial(m):
            key_node_i = gb.add_node(m.name + "__KEY__", mesh_index=None)
            anim_node_i = key_node_i
            # KEY is parent of mesh node (which will carry PIVOT TRS)
            gb.add_child(key_node_i, mesh_node_i)
            # Parent the KEY node according to RF hierarchy
            pending_parent.append((key_node_i, m.parent_name))
        else:
            pending_parent.append((mesh_node_i, m.parent_name))

        # RF round-trip metadata (preserve keyframed pivot+keys and any geometry-scale baking we applied)
        rf_meta: Dict[str, Any] = {"vfx_version": int(hdr.version)}
        if m.is_keyframed and m.keyframed_block:
            rf_meta["keyframed_block_b64"] = base64.b64encode(m.keyframed_block).decode("ascii")
        if baked_geom_scale is not None:
            rf_meta["baked_geom_scale"] = [float(baked_geom_scale[0]), float(baked_geom_scale[1]), float(baked_geom_scale[2])]
        # Preserve mesh flags so they round-trip through Blender
        rf_meta["flags_raw"] = int(m.flags_raw)
        rf_meta["save_parent"] = int(m.save_parent)
        rf_meta["facing"] = int(m.facing)
        rf_meta["facing_rod"] = int(m.facing_rod)
        rf_meta["morph"] = int(m.morph)
        rf_meta["dump_uvs"] = int(m.dump_uvs)
        # Facing billboard data (for round-trip fidelity)
        if m.facing or m.facing_rod:
            fp_list = []
            for fr in m.frames:
                if fr.facing_pair is not None:
                    fp_list.append([float(fr.facing_pair[0]), float(fr.facing_pair[1])])
            if fp_list:
                rf_meta["facing_pairs"] = fp_list
        if m.facing_rod and m.frames and m.frames[0].facing_rod_up is not None:
            up = m.frames[0].facing_rod_up
            rf_meta["facing_rod_up"] = [float(up[0]), float(up[1]), float(up[2])]
        gb.g["nodes"][mesh_node_i].setdefault("extras", {})["rf_vfx"] = rf_meta

        # Blender compatibility: also set default morph weights on the NODE when the mesh has morph targets.
        _mw_list = gb.g.get("meshes", [{}])[mesh_i].get("weights")
        if _mw_list:
            gb.g["nodes"][mesh_node_i]["weights"] = list(_mw_list)

        # set initial TRS (time 0)
        if m.morph:
            gb.set_node_trs(mesh_node_i, t=(0.0,0.0,0.0), r=(0.0,0.0,0.0,1.0), s=(1.0,1.0,1.0))

        elif m.is_keyframed and m.keyframes:
            # Animate KEY (or the mesh node if no pivot wrapper)
            t0 = m.keyframes.t_values[0] if m.keyframes.t_values else (0.0,0.0,0.0)
            r0 = m.keyframes.r_values[0] if m.keyframes.r_values else (0.0,0.0,0.0,1.0)
            s0 = m.keyframes.s_values[0] if m.keyframes.s_values else (1.0,1.0,1.0)
            if skip_scale_anim:
                s0 = (1.0,1.0,1.0)
            gb.set_node_trs(anim_node_i, t=(t0[0]*trs_scale, t0[1]*trs_scale, t0[2]*trs_scale), r=r0, s=s0)

            # Apply PIVOT TRS on mesh node when wrapped
            if key_node_i is not None:
                pt_rf = m.pivot_translation_rf or (0.0,0.0,0.0)
                pr_raw = m.pivot_rotation_raw or (0.0,0.0,0.0,1.0)
                ps_rf = m.pivot_scale_rf or (1.0,1.0,1.0)

                pt_bl = rf_to_blender(pt_rf)
                pr_bl = quat_rf_to_blender(pr_raw)

                # If we baked pivot_scale (and possibly constant scale keys) into geometry, keep pivot scale at identity.
                if baked_geom_scale is not None and skip_scale_anim:
                    ps_bl = (1.0,1.0,1.0)
                else:
                    ps_bl = (abs(ps_rf[0]), abs(ps_rf[2]), abs(ps_rf[1]))

                gb.set_node_trs(mesh_node_i, t=(pt_bl[0]*trs_scale, pt_bl[1]*trs_scale, pt_bl[2]*trs_scale), r=pr_bl, s=ps_bl)

        elif trs_frames:
            t0,r0,s0 = trs_frames[0]
            gb.set_node_trs(mesh_node_i, t=(t0[0]*trs_scale, t0[1]*trs_scale, t0[2]*trs_scale), r=r0, s=s0)

        else:
            gb.set_node_trs(mesh_node_i, t=(0.0,0.0,0.0), r=(0.0,0.0,0.0,1.0), s=(1.0,1.0,1.0))

        # Parent-resolution map: RF child objects refer to the *mesh name* (e.g. FlagMesh parent='flagpole').
        # For keyframed meshes with a pivot wrapper, children should attach to the KEY node (animation node),
        # not to the pivot mesh node (which would incorrectly offset children).
        node_by_name[m.name] = key_node_i if key_node_i is not None else mesh_node_i
        mesh_node_infos.append({
            "mesh": m,
            "node_i": anim_node_i,
            "morph_targets_count": (len(morph_frames_bl)-1) if (morph_frames_bl is not None) else 0,
            "trs_frames": trs_frames,
            "skip_scale_anim": skip_scale_anim,
        })

    # --- Dummy nodes ---
    if debug_frames:
        print(f"[INFO] dummies found: {len(dummies)}")
    dummy_infos: List[Dict[str, Any]] = []
    for d in dummies:
        di = gb.add_node(d.name, mesh_index=None)
        node_by_name[d.name] = di
        pending_parent.append((di, d.parent_name))
        # set base TRS
        gb.set_node_trs(di, t=tuple(x*trs_scale for x in rf_to_blender(d.pos_rf)), r=quat_rf_to_blender(d.orient_raw), s=(1.0,1.0,1.0))
        if debug_frames:
            print(f"        {d.name} parent={d.parent_name} pos={rf_to_blender(d.pos_rf)} frames={len(d.frames)}")
        dummy_infos.append({"dummy": d, "node_i": di})

    # --- Particle System nodes ---
    for ps in particles:
        pi = gb.add_node(ps.name, mesh_index=None)
        node_by_name[ps.name] = pi
        pending_parent.append((pi, ps.parent_name))
        # Store particle data as base64 in extras for round-trip
        ps_meta = {
            "rf_vfx_particle": True,
            "num_particles": ps.num_particles,
            "num_frames": ps.num_frames,
            "fps": ps.fps,
            "save_parent": ps.save_parent,
            "raw_body_b64": base64.b64encode(ps.raw_body).decode("ascii"),
        }
        gb.g["nodes"][pi].setdefault("extras", {})["rf_vfx_particle"] = ps_meta

    # --- Parent hierarchy resolution ---
    for child_i, parent_name in pending_parent:
        p = parent_name.strip()
        if not p or p.lower() in ("scene root", "* scene root", "*scene root"):
            gb.add_child(root_idx, child_i)
            continue
        # prefer exact match; fallback case-insensitive
        parent_i = node_by_name.get(p)
        if parent_i is None:
            for k,v in node_by_name.items():
                if k.lower() == p.lower():
                    parent_i = v
                    break
        if parent_i is None:
            gb.add_child(root_idx, child_i)
        else:
            gb.add_child(parent_i, child_i)

    # --- Animations ---
    anim: Dict[str, Any] = {"name": "VFX"}
    # morph weights
    for mi in mesh_node_infos:
        m: VfxMesh = mi["mesh"]
        node_i = mi["node_i"]
        targets_count = mi["morph_targets_count"]
        if m.morph and targets_count > 0:
            fps = float(m.fps) if m.fps else 15.0
            times = [float(i)/fps for i in range(m.num_frames)]
            # weights: STEP, one-hot through targets
            weights: List[float] = []
            for fi in range(m.num_frames):
                w = [0.0]*targets_count
                if fi > 0:
                    w[fi-1] = 1.0
                weights.extend(w)
            inp_acc = gb.add_accessor_f32(times, "SCALAR", 1)
            out_acc = gb.add_accessor_f32(weights, "SCALAR", targets_count)  # type is SCALAR but elem_n = targets_count; accessor count = num_frames
            # BUT glTF expects output accessor type SCALAR with count=num_frames*targets_count? No: for weights, accessor type should be SCALAR with count=num_frames*targets_count? Actually spec wants VECn? There is no VECn>4.
            # Many tools use SCALAR with count=num_frames*targets_count and treat as array. We'll use SCALAR with count=num_frames*targets_count and set output stride? Not possible.
            # Safer approach: use accessor type "SCALAR" count = num_frames*targets_count, and sampler output interpreted with stride=targets_count (Blender handles).
            # We'll override accessor count to len(weights) and keep SCALAR; but sampler uses output accessor directly.
            # To keep builder simple, we re-add properly:
            # Rebuild output accessor as SCALAR with count=len(weights)
            # (Blender/glTF importer handles weights with count = keyframes * targets as packed array)
            gb.g["accessors"][out_acc]["count"] = len(weights)
            sam_i = gb.add_anim_sampler(anim, inp_acc, out_acc, interp="STEP")
            gb.add_anim_channel(anim, sam_i, node_i, "weights")

    # TRS for non-morph meshes (per-frame)
    for mi in mesh_node_infos:
        m: VfxMesh = mi["mesh"]
        node_i = mi["node_i"]
        if m.morph:
            continue
        if m.is_keyframed and m.keyframes:
            k = m.keyframes
            if k.t_times and k.t_values:
                inp_acc = gb.add_accessor_f32(k.t_times, "SCALAR", 1)
                out_vals = []
                for (x,y,z) in k.t_values:
                    out_vals.extend([x*trs_scale, y*trs_scale, z*trs_scale])
                out_acc = gb.add_accessor_f32(out_vals, "VEC3", 3)
                gb.g["accessors"][out_acc]["type"] = "VEC3"
                sam = gb.add_anim_sampler(anim, inp_acc, out_acc, interp="LINEAR")
                gb.add_anim_channel(anim, sam, node_i, "translation")
            if k.r_times and k.r_values:
                inp_acc = gb.add_accessor_f32(k.r_times, "SCALAR", 1)
                out_vals = []
                for (x,y,z,w) in k.r_values:
                    out_vals.extend([x,y,z,w])
                out_acc = gb.add_accessor_f32(out_vals, "VEC4", 4)
                gb.g["accessors"][out_acc]["type"] = "VEC4"
                sam = gb.add_anim_sampler(anim, inp_acc, out_acc, interp="LINEAR")
                gb.add_anim_channel(anim, sam, node_i, "rotation")
            if (not mi.get('skip_scale_anim')) and k.s_times and k.s_values:
                inp_acc = gb.add_accessor_f32(k.s_times, "SCALAR", 1)
                out_vals = []
                for (x,y,z) in k.s_values:
                    out_vals.extend([x,y,z])
                out_acc = gb.add_accessor_f32(out_vals, "VEC3", 3)
                gb.g["accessors"][out_acc]["type"] = "VEC3"
                sam = gb.add_anim_sampler(anim, inp_acc, out_acc, interp="LINEAR")
                gb.add_anim_channel(anim, sam, node_i, "scale")
        else:
            trs_frames = mi["trs_frames"]
            if trs_frames and len(trs_frames) > 1:
                fps = float(m.fps) if m.fps else 15.0
                times = [float(i)/fps for i in range(len(trs_frames))]
                inp_acc = gb.add_accessor_f32(times, "SCALAR", 1)
                # translation
                out_t = []
                out_r = []
                out_s = []
                for (t,r,s) in trs_frames:
                    out_t.extend([t[0]*trs_scale, t[1]*trs_scale, t[2]*trs_scale])
                    out_r.extend([r[0], r[1], r[2], r[3]])
                    out_s.extend([s[0], s[1], s[2]])
                out_t_acc = gb.add_accessor_f32(out_t, "VEC3", 3); gb.g["accessors"][out_t_acc]["type"]="VEC3"
                out_r_acc = gb.add_accessor_f32(out_r, "VEC4", 4); gb.g["accessors"][out_r_acc]["type"]="VEC4"
                out_s_acc = gb.add_accessor_f32(out_s, "VEC3", 3); gb.g["accessors"][out_s_acc]["type"]="VEC3"
                sam_t = gb.add_anim_sampler(anim, inp_acc, out_t_acc, interp="LINEAR")
                sam_r = gb.add_anim_sampler(anim, inp_acc, out_r_acc, interp="LINEAR")
                sam_s = gb.add_anim_sampler(anim, inp_acc, out_s_acc, interp="LINEAR")
                gb.add_anim_channel(anim, sam_t, node_i, "translation")
                gb.add_anim_channel(anim, sam_r, node_i, "rotation")
                gb.add_anim_channel(anim, sam_s, node_i, "scale")

    # dummies animation (only emit channels when something actually changes)
    # (For example: $prop_flag in CTFflag-blue has 46 frames but all values are identical,
    #  so exporting keys just adds noise and makes Blender show an unnecessary Action.)
    for di in dummy_infos:
        d: VfxDummy = di["dummy"]
        node_i = di["node_i"]
        if not d.frames or len(d.frames) <= 1:
            continue
        fps = 15.0
        times = [float(i)/fps for i in range(len(d.frames))]

        # Convert sequences to Blender space
        t_seq = []
        r_seq = []
        for fr in d.frames:
            t = rf_to_blender(fr.pos_rf)
            r = quat_rf_to_blender(fr.orient_raw)
            t_seq.append((t[0]*trs_scale, t[1]*trs_scale, t[2]*trs_scale))
            r_seq.append(r)
        r_seq = quat_fix_sign_seq(r_seq)

        # Decide whether translation / rotation actually vary
        eps_t = 1e-6
        eps_ang = 1e-6  # radians
        t0 = t_seq[0]
        r0 = r_seq[0]
        max_dt = 0.0
        for t in t_seq[1:]:
            dx = abs(t[0]-t0[0]); dy = abs(t[1]-t0[1]); dz = abs(t[2]-t0[2])
            if dx > max_dt: max_dt = dx
            if dy > max_dt: max_dt = dy
            if dz > max_dt: max_dt = dz
        max_ang = 0.0
        for r in r_seq[1:]:
            ang = quat_angle_diff_rad(r0, r)
            if ang > max_ang: max_ang = ang
        need_t = (max_dt > eps_t)
        need_r = (max_ang > eps_ang)
        if (not need_t) and (not need_r):
            continue

        inp_acc = gb.add_accessor_f32(times, "SCALAR", 1)
        if need_t:
            out_t = []
            for t in t_seq:
                out_t.extend([t[0], t[1], t[2]])
            out_t_acc = gb.add_accessor_f32(out_t, "VEC3", 3)
            gb.g["accessors"][out_t_acc]["type"] = "VEC3"
            sam_t = gb.add_anim_sampler(anim, inp_acc, out_t_acc, interp="LINEAR")
            gb.add_anim_channel(anim, sam_t, node_i, "translation")

        if need_r:
            out_r = []
            for r in r_seq:
                out_r.extend([r[0], r[1], r[2], r[3]])
            out_r_acc = gb.add_accessor_f32(out_r, "VEC4", 4)
            gb.g["accessors"][out_r_acc]["type"] = "VEC4"
            sam_r = gb.add_anim_sampler(anim, inp_acc, out_r_acc, interp="LINEAR")
            gb.add_anim_channel(anim, sam_r, node_i, "rotation")

    if anim.get("channels"):
        gb.add_animation(anim)

    g, blob = gb.finalize()
    # write files
    out_dir = os.path.dirname(os.path.abspath(out_gltf)) or "."
    base = os.path.splitext(os.path.basename(out_gltf))[0]
    out_bin = os.path.join(out_dir, base + ".bin")
    g["buffers"][0]["uri"] = os.path.basename(out_bin)

    with open(out_bin, "wb") as f:
        f.write(blob)
    with open(out_gltf, "w", encoding="utf-8", newline="") as f:
        json.dump(g, f, indent=2)

    print("Wrote:", out_gltf)
    print("Wrote:", out_bin)



# ----------------------------
# True export: glTF -> NEW VFX (v4.6)
# ----------------------------

def _pack_strz(s: str) -> bytes:
    return (s or "").encode("ascii", errors="replace") + b"\x00"

def _pack_vec3_rf(v: Tuple[float,float,float]) -> bytes:
    return struct.pack("<fff", float(v[0]), float(v[1]), float(v[2]))

def _pack_quat_raw(q: Tuple[float,float,float,float]) -> bytes:
    return struct.pack("<ffff", float(q[0]), float(q[1]), float(q[2]), float(q[3]))

def _pack_uv(uv: Tuple[float,float]) -> bytes:
    u,v = uv
    # stored v is negated (see read_uv)
    return struct.pack("<ff", float(u), float(-v))

def _clamp_i16(x: int) -> int:
    if x < -32767: return -32767
    if x >  32767: return  32767
    return int(x)

def _quantize_positions_rf(pos_rf: List[Tuple[float,float,float]]) -> Tuple[Tuple[float,float,float], Tuple[float,float,float], List[Tuple[int,int,int]]]:
    # VFX decompression is: pos = center + mult * raw_s16 (no /32767).
    # Choose center as mid of min/max, mult as half-span / 32767.
    xs = [p[0] for p in pos_rf]; ys = [p[1] for p in pos_rf]; zs = [p[2] for p in pos_rf]
    minx,maxx = (min(xs), max(xs)) if xs else (0.0,0.0)
    miny,maxy = (min(ys), max(ys)) if ys else (0.0,0.0)
    minz,maxz = (min(zs), max(zs)) if zs else (0.0,0.0)
    cx = (minx+maxx)*0.5
    cy = (miny+maxy)*0.5
    cz = (minz+maxz)*0.5
    sx = (maxx-minx)*0.5
    sy = (maxy-miny)*0.5
    sz = (maxz-minz)*0.5
    mx = (sx/32767.0) if sx != 0.0 else 0.0
    my = (sy/32767.0) if sy != 0.0 else 0.0
    mz = (sz/32767.0) if sz != 0.0 else 0.0

    out: List[Tuple[int,int,int]] = []
    for (x,y,z) in pos_rf:
        if mx != 0.0:
            ix = _clamp_i16(int(round((x - cx) / mx)))
        else:
            ix = 0
        if my != 0.0:
            iy = _clamp_i16(int(round((y - cy) / my)))
        else:
            iy = 0
        if mz != 0.0:
            iz = _clamp_i16(int(round((z - cz) / mz)))
        else:
            iz = 0
        out.append((ix,iy,iz))
    return ( (cx,cy,cz), (mx,my,mz), out )

def _tri_normal(a, b, c) -> Tuple[float,float,float]:
    ax,ay,az = a; bx,by,bz = b; cx,cy,cz = c
    ux,uy,uz = (bx-ax, by-ay, bz-az)
    vx,vy,vz = (cx-ax, cy-ay, cz-az)
    nx = uy*vz - uz*vy
    ny = uz*vx - ux*vz
    nz = ux*vy - uy*vx
    l = math.sqrt(nx*nx + ny*ny + nz*nz)
    if l < 1e-20:
        return (0.0,0.0,0.0)
    return (nx/l, ny/l, nz/l)

def _dot3(a: Tuple[float,float,float], b: Tuple[float,float,float]) -> float:
    return a[0]*b[0] + a[1]*b[1] + a[2]*b[2]

def _bounds_center_radius(pts: List[Tuple[float,float,float]]) -> Tuple[Tuple[float,float,float], float]:
    if not pts:
        return (0.0,0.0,0.0), 0.0
    xs=[p[0] for p in pts]; ys=[p[1] for p in pts]; zs=[p[2] for p in pts]
    cx=(min(xs)+max(xs))*0.5
    cy=(min(ys)+max(ys))*0.5
    cz=(min(zs)+max(zs))*0.5
    r=0.0
    for (x,y,z) in pts:
        dx=x-cx; dy=y-cy; dz=z-cz
        d=math.sqrt(dx*dx+dy*dy+dz*dz)
        if d>r: r=d
    return (cx,cy,cz), float(r)

def _gltf_parent_map(nodes: List[Dict[str,Any]]) -> Dict[int,int]:
    parent: Dict[int,int] = {}
    for pi, n in enumerate(nodes):
        for ci in (n.get("children") or []):
            parent[int(ci)] = pi
    return parent

def _gltf_find_weights_anim(g: Dict[str,Any], bin_data: bytes, node_index: int) -> Optional[Tuple[List[float], List[float], int]]:
    # returns (times, weights_flat, num_targets) if found
    for anim in (g.get("animations") or []):
        chans = anim.get("channels") or []
        samps = anim.get("samplers") or []
        for ch in chans:
            tgt = ch.get("target") or {}
            if int(tgt.get("node", -1)) != int(node_index):
                continue
            if tgt.get("path") != "weights":
                continue
            si = int(ch.get("sampler"))
            samp = samps[si]
            t_acc = int(samp["input"])
            w_acc = int(samp["output"])
            times = _gltf_accessor_read_scalar_f32(g, bin_data, t_acc)
            weights = _gltf_accessor_read_scalar_f32(g, bin_data, w_acc)
            # We infer num_targets from weights length / times
            if len(times) > 0:
                num_targets = int(len(weights) / len(times))
            else:
                num_targets = 0
            return (times, weights, num_targets)
    return None

def _infer_fps_from_times(times: List[float], default_fps: int = 15) -> int:
    if not times or len(times) < 2:
        return default_fps
    diffs = [times[i+1]-times[i] for i in range(len(times)-1)]
    diffs = [d for d in diffs if d > 1e-6]
    if not diffs:
        return default_fps
    dt = sorted(diffs)[len(diffs)//2]
    fps = int(round(1.0 / dt)) if dt > 1e-6 else default_fps
    if fps <= 0: fps = default_fps
    return fps

def _write_vfx_header(version: int,
                      flags: int,
                      end_frame: int,
                      counts: Dict[str,int]) -> bytes:
    # Must match parse_header() read order.
    out = bytearray()
    out += b"VSFX"
    out += struct.pack("<i", int(version))
    out += struct.pack("<i", int(flags))
    out += struct.pack("<i", int(end_frame))

    def w(k: str):
        out.extend(struct.pack("<i", int(counts.get(k, 0))))

    for k in ["num_meshes","num_lights","num_dummies","num_particle_systems","num_spacewarps","num_cameras"]:
        w(k)
    w("num_selsets")
    w("num_materials")
    w("num_mix_frames")
    w("num_self_illumination_frames")
    w("num_opacity_frames")

    # version >= 0x3000A so no unk_1

    for k in ["num_faces","num_mesh_material_indices","num_vertex_normals","num_adjacent_faces","num_mesh_frames"]:
        w(k)
    w("num_uv_frames")

    for k in ["num_mesh_transform_frames","num_mesh_transform_keyframe_lists","num_mesh_translation_keys","num_mesh_rotation_keys","num_mesh_scale_keys"]:
        w(k)

    for k in ["num_light_frames","num_dummy_frames","num_part_sys_frames","num_spacewarp_frames","num_camera_frames"]:
        w(k)
    w("num_selset_objects")
    return bytes(out)

def _get_facing_pair(rf_ex: dict, frame_idx: int, default_width: float = 1.0) -> Tuple[float, float]:
    """Get facing billboard pair for a given frame from rf_vfx extras or default."""
    fp_list = rf_ex.get("facing_pairs")
    if isinstance(fp_list, list) and frame_idx < len(fp_list):
        fp = fp_list[frame_idx]
        if isinstance(fp, (list, tuple)) and len(fp) >= 2:
            return (float(fp[0]), float(fp[1]))
    return (float(default_width), float(default_width))

def _get_facing_rod_up(rf_ex: dict) -> Tuple[float, float, float]:
    """Get facing-rod up vector from rf_vfx extras or default (RF Y-up)."""
    up = rf_ex.get("facing_rod_up")
    if isinstance(up, (list, tuple)) and len(up) >= 3:
        return (float(up[0]), float(up[1]), float(up[2]))
    return (0.0, 1.0, 0.0)  # RF Y-up default

def _build_part_section(
    name: str,
    parent_name: str,
    save_parent: int,
    positions_rf: List[Tuple[float,float,float]],
    orientations_rf: List[Tuple[float,float,float,float]],
    flags: int = 0x10,           # 0x10 = facing
    lifetime: int = 30,
    fps: int = 15,
    particle_size: float = 0.5,
    speed: float = 0.4,
    shrink_ratio: float = 0.2,
    birth_rate: int = 10,
    apply_gravity: int = 0,
    fade_at_death: float = 0.6,
    size_variation: float = 0.0,
    speed_variation: float = 0.0,
    gravity_strength: float = 1.0,
    emitter_radius: float = 0.0,
    random_direction: float = 30.0,
) -> bytes:
    """Build a complete PART section body from emitter parameters and per-frame TRS.
    
    positions_rf / orientations_rf: one entry per VFX frame (end_frame+1 entries).
    The game reads these as the emitter's worldspace position/orientation each frame
    and spawns particles from it.
    """
    import random as _rng
    _rng.seed(hash(name) & 0xFFFFFFFF)  # deterministic per emitter name

    num_entries = len(positions_rf)
    total_spawn = birth_rate * fps * lifetime // 15  # observed formula from real files
    grav_val = int(apply_gravity)

    out = bytearray()
    out += _pack_strz(name)
    out += _pack_strz(parent_name)
    out += struct.pack("<b", int(save_parent))

    # 14-field header (11 ints + 3 floats)
    out += struct.pack("<i", int(flags))
    out += struct.pack("<i", 0)                    # unk1
    out += struct.pack("<i", 0)                    # unk2
    out += struct.pack("<i", int(num_entries))      # num_entries (one per frame)
    out += struct.pack("<i", int(lifetime))         # particle lifetime (frames at fps)
    out += struct.pack("<i", int(fps))              # fps
    out += struct.pack("<i", 0)                    # unk3
    out += struct.pack("<i", int(total_spawn))      # total_spawn
    out += struct.pack("<i", 0)                    # unk4
    out += struct.pack("<i", grav_val)              # apply_gravity
    out += struct.pack("<i", 0)                    # unk5
    out += struct.pack("<f", float(gravity_strength))  # float_00 (gravity multiplier)
    out += struct.pack("<f", 0.0)                  # float_01
    out += struct.pack("<f", float(fade_at_death))  # float_02 (fade_at_death ratio)

    # Per-frame entries: 13 floats each
    death_start = max(0, num_entries - lifetime)
    rad_spread = math.radians(float(random_direction) * 0.5) if random_direction > 0.0 else 0.0

    for fi in range(num_entries):
        px, py, pz = positions_rf[fi]
        qx, qy, qz, qw = orientations_rf[fi]

        # Apply emitter radius offset (randomized per frame for spawn spread)
        if emitter_radius > 0.0:
            dx = _rng.uniform(-emitter_radius, emitter_radius)
            dy = _rng.uniform(-emitter_radius, emitter_radius)
            dz = _rng.uniform(-emitter_radius, emitter_radius)
            px += dx; py += dy; pz += dz

        # Apply random direction spread to orientation
        if rad_spread > 0.0:
            ax = _rng.uniform(-rad_spread, rad_spread)
            ay = _rng.uniform(-rad_spread, rad_spread)
            # Small rotation perturbation via quaternion
            ha = ax * 0.5; hb = ay * 0.5
            sa, ca = math.sin(ha), math.cos(ha)
            sb, cb = math.sin(hb), math.cos(hb)
            dqw = ca * cb; dqx = sa * cb; dqy = ca * sb; dqz = sa * sb
            # Multiply: q_new = dq * q_orig
            nw = dqw*qw - dqx*qx - dqy*qy - dqz*qz
            nx = dqw*qx + dqx*qw + dqy*qz - dqz*qy
            ny = dqw*qy - dqx*qz + dqy*qw + dqz*qx
            nz = dqw*qz + dqx*qy - dqy*qx + dqz*qw
            mag = math.sqrt(nw*nw + nx*nx + ny*ny + nz*nz) or 1.0
            qx, qy, qz, qw = nx/mag, ny/mag, nz/mag, nw/mag

        # Per-frame size with variation
        f_size = float(particle_size) + _rng.uniform(-size_variation, size_variation)
        f_size = max(0.01, f_size)

        # Per-frame speed with variation
        f_speed = float(speed) + _rng.uniform(-speed_variation, speed_variation)
        f_speed = max(0.0, f_speed)

        # f7: particle size, f8: speed, f9: shrink ratio
        f7 = f_size
        f8 = f_speed
        f9 = float(shrink_ratio)

        # f10: active birth rate multiplier (ramps down near end)
        if fi >= death_start:
            progress = (fi - death_start) / max(1, lifetime)
            f10 = f_size * (1.0 - progress * 0.6)
        else:
            f10 = f_size

        # f11: tail/min size, f12: negligible
        f11 = f_size * 0.1
        f12 = 0.0

        out += struct.pack("<13f", px, py, pz, qx, qy, qz, qw, f7, f8, f9, f10, f11, f12)

    return bytes(out)

def _make_matl_section(tex0: str, fps: int = 15, additive: int = 0,
                       self_illum: float = 1.0,
                       self_illum_list: Optional[List[float]] = None,
                       opacity_frames: Optional[List[float]] = None) -> bytes:
    # Mirror the layout observed in CTFflag-blue (v4.6 image material)
    out = bytearray()
    out += struct.pack("<i", 0)            # mat_type = image
    out += struct.pack("<i", int(fps))     # fps
    out += struct.pack("<b", int(additive))
    out += _pack_strz(tex0)
    out += struct.pack("<i", 0)            # start_frame
    out += struct.pack("<f", 1.0)          # playback_rate
    out += struct.pack("<i", 2)            # anim_type (once)
    out += struct.pack("<f", 0.0)          # spec
    out += struct.pack("<f", 0.0)          # gloss
    out += struct.pack("<f", 0.0)          # refl
    out += _pack_strz("")                  # unknown string
    # self-illumination frames
    if self_illum_list and len(self_illum_list) > 0:
        out += struct.pack("<i", len(self_illum_list))
        for sv in self_illum_list:
            out += struct.pack("<f", float(sv))
    else:
        out += struct.pack("<i", 1)
        out += struct.pack("<f", float(self_illum))
    # opacity: per-frame fade values (default: 1 frame at full opacity)
    if opacity_frames and len(opacity_frames) > 0:
        out += struct.pack("<i", len(opacity_frames))
        for ov in opacity_frames:
            out += struct.pack("<f", float(ov))
    else:
        out += struct.pack("<i", 1)
        out += struct.pack("<f", 1.0)
    return bytes(out)

def _section(type_u32: int, body: bytes) -> bytes:
    # section header: type + len (len includes itself, not type)
    return struct.pack("<II", int(type_u32), int(len(body) + 4)) + body


# ----------------------------
# RF v4.6 keyframed pivot+key block helpers (for faithful round-trip)
# ----------------------------

def _rf46_keyframed_block_counts(block: bytes) -> Tuple[int,int,int]:
    """Return (num_t, num_r, num_s) from a v4.6 keyframed block.

    Layout we rely on (v4.6 keyframed mesh):
      pivot_translation (3f) + pivot_rotation (4f) + pivot_scale (3f) = 40 bytes
      num_t (i32) then num_t * 40 bytes
      num_r (i32) then num_r * 40 bytes
      num_s (i32) then num_s * 40 bytes
    """
    if not block or len(block) < 40 + 4:
        return (0,0,0)
    o = 40
    try:
        (nt,) = struct.unpack_from("<i", block, o); o += 4
        if nt < 0: nt = 0
        o += nt * 40
        (nr,) = struct.unpack_from("<i", block, o); o += 4
        if nr < 0: nr = 0
        o += nr * 40
        (ns,) = struct.unpack_from("<i", block, o); o += 4
        if ns < 0: ns = 0
        return (int(nt), int(nr), int(ns))
    except Exception:
        return (0,0,0)

def _rf46_keyframed_block_is_sane(block: bytes) -> bool:
    nt,nr,ns = _rf46_keyframed_block_counts(block)
    if nt == 0 and nr == 0 and ns == 0:
        return False
    # quick length check (approx)
    need = 40 + 4 + nt*40 + 4 + nr*40 + 4 + ns*40
    return len(block) >= need

def _rf46_keyframed_block_has_safe_trs(block: bytes) -> bool:
    """Check if an rf_block has identity-ish scale and rotation (safe to reuse in true export)."""
    try:
        nt,nr,ns = _rf46_keyframed_block_counts(block)
        o = 40  # skip pivot
        # Skip translation keys
        o += 4  # count
        for _ in range(nt):
            o += 4 + 12 + 24  # time + val + tangents
        # Read rotation key values
        o += 4  # count
        for _ in range(nr):
            o += 4  # time
            rv = struct.unpack_from("<4f", block, o); o += 16
            o += 20  # TCB
            # Check identity: (0,0,0,1) or close — allow -0.0
            if abs(rv[0]) > 0.01 or abs(rv[1]) > 0.01 or abs(rv[2]) > 0.01 or abs(abs(rv[3]) - 1.0) > 0.01:
                return False
        # Read scale key values
        o += 4  # count
        for _ in range(ns):
            o += 4  # time
            sv = struct.unpack_from("<3f", block, o); o += 12
            o += 24  # tangents
            if abs(sv[0] - 1.0) > 0.01 or abs(sv[1] - 1.0) > 0.01 or abs(sv[2] - 1.0) > 0.01:
                return False
        return True
    except Exception:
        return False

def export_new_vfx_from_gltf(gltf_path: str,
                             out_vfx: str,
                             gltf_scale: float = 1.0,
                             anchor: Optional[str] = None,
                             template_vfx: Optional[str] = None,
                             debug: bool = False,
                             baked_frames_path: Optional[str] = None) -> None:
    g = _gltf_load_json(gltf_path)
    bin_data = _gltf_load_bin(g, gltf_path)
    nodes = g.get("nodes") or []

    # Load baked per-frame vertex data sidecar if available
    baked_frames_data: Dict[str, Any] = {}
    if baked_frames_path and os.path.isfile(baked_frames_path):
        try:
            with open(baked_frames_path, "r") as bf:
                baked_frames_data = json.load(bf)
            print(f"[NEW_VFX] Loaded baked frames sidecar: {len(baked_frames_data)} mesh(es)")
        except Exception as e:
            print(f"[NEW_VFX] Failed to load baked frames: {e}")

    # Optional template VFX: used to preserve RF-native material names (usually .tga/.vbm)
    # and timing (end_frame) for exact/round-trip exports. glTF images are commonly PNG/JPG,
    # which RF may not support for VFX materials.
    template_hdr = None
    template_materials: Optional[List[VfxMaterial]] = None
    if template_vfx:
        try:
            template_hdr, template_materials, _t_meshes, _t_dummies, _t_particles = parse_vfx(template_vfx)
        except Exception as e:
            raise RuntimeError(f"Failed to parse template VFX '{template_vfx}': {e}")

    parent_map = _gltf_parent_map(nodes)
    meshes_g = g.get("meshes") or []

    # --- optional anchor recenter ---
    # Moves the entire effect so that the chosen node's WORLD position becomes (0,0,0),
    # by subtracting that world translation from all ROOT nodes (parent == Scene Root).
    # This preserves local child relationships.
    def _mat4_mul(a, b):
        out = [[0.0]*4 for _ in range(4)]
        for i in range(4):
            for j in range(4):
                out[i][j] = a[i][0]*b[0][j] + a[i][1]*b[1][j] + a[i][2]*b[2][j] + a[i][3]*b[3][j]
        return out

    def _mat4_from_trs(t, q, s):
        m3 = quat_to_mat3(q)
        # apply scale on columns
        rs = (
            (m3[0][0]*s[0], m3[0][1]*s[1], m3[0][2]*s[2]),
            (m3[1][0]*s[0], m3[1][1]*s[1], m3[1][2]*s[2]),
            (m3[2][0]*s[0], m3[2][1]*s[1], m3[2][2]*s[2]),
        )
        return [
            [rs[0][0], rs[0][1], rs[0][2], t[0]],
            [rs[1][0], rs[1][1], rs[1][2], t[1]],
            [rs[2][0], rs[2][1], rs[2][2], t[2]],
            [0.0, 0.0, 0.0, 1.0],
        ]

    world_cache = {}
    def _node_world_mat(i):
        if i in world_cache:
            return world_cache[i]
        node = nodes[i] or {}
        t = node.get("translation") or [0.0,0.0,0.0]
        r = node.get("rotation") or [0.0,0.0,0.0,1.0]
        s = node.get("scale") or [1.0,1.0,1.0]
        local = _mat4_from_trs((float(t[0]),float(t[1]),float(t[2])),
                               (float(r[0]),float(r[1]),float(r[2]),float(r[3])),
                               (float(s[0]),float(s[1]),float(s[2])))
        p = parent_map.get(i, None)
        if p is None:
            world = local
        else:
            world = _mat4_mul(_node_world_mat(p), local)
        world_cache[i] = world
        return world

    anchor_shift = (0.0,0.0,0.0)
    if anchor:
        ai = None
        for ni, node in enumerate(nodes):
            if (node.get("name") or "").strip() == anchor:
                ai = ni
                break
        if ai is not None:
            wm = _node_world_mat(ai)
            anchor_shift = (wm[0][3], wm[1][3], wm[2][3])
            if debug:
                print(f"[ANCHOR] node='{anchor}' world_shift={anchor_shift}")
        else:
            if debug:
                print(f"[ANCHOR] node='{anchor}' not found; no shift applied")


    # --- materials (global) ---
    # Build a glTF material->texture0 (image uri) map.
    # If a template VFX is provided, we will prefer its tex0 names when writing MATL sections.
    # Strategy for tex0 name resolution (in priority order):
    #   1. Template VFX material name (if template provided)
    #   1. Template VFX material name (if template provided) - applied later
    #   2. glTF extras rfvfx_texture_name (from authoring panel)
    #   3. glTF material name (if it looks like a filename with .tga/.vbm/.v3d extension)
    #   4. glTF image URI basename (strip path, swap extension to .tga)
    #   5. Empty string (RF will render untextured)
    gltf_tex0_by_mat: Dict[int, str] = {}
    images = g.get("images") or []
    textures = g.get("textures") or []
    _rf_tex_exts = {".tga", ".vbm", ".v3d", ".tif", ".pcx"}
    for mi, mat in enumerate(g.get("materials") or []):
        tex0 = ""
        # Check extras for authoring texture_name (set via Blender Authoring panel)
        mat_extras = (mat.get("extras") or {})
        rfvfx_tex = ""
        if isinstance(mat_extras, dict):
            rfvfx_tex = str(mat_extras.get("rfvfx_texture_name", "") or "").strip()
        if rfvfx_tex:
            tex0 = rfvfx_tex
        # Check if the material NAME itself is a texture filename (user convention)
        elif (mat.get("name") or "").strip() and os.path.splitext((mat.get("name") or "").strip())[1].lower() in _rf_tex_exts:
            tex0 = (mat.get("name") or "").strip()
        else:
            # Fall back to image URI
            try:
                pbr = mat.get("pbrMetallicRoughness") or {}
                bct = (pbr.get("baseColorTexture") or {})
                ti = bct.get("index")
                if ti is not None:
                    tex = textures[int(ti)]
                    si = tex.get("source")
                    if si is not None and int(si) < len(images):
                        uri = images[int(si)].get("uri") or ""
                        if uri:
                            # Extract basename and swap extension to .tga if not already RF-native
                            base = os.path.basename(uri)
                            name_part, ext = os.path.splitext(base)
                            if ext.lower() in _rf_tex_exts:
                                tex0 = base
                            elif name_part:
                                tex0 = name_part + ".tga"
            except Exception:
                pass
        gltf_tex0_by_mat[int(mi)] = tex0


    # --- gather objects ---
    mesh_objs: List[Dict[str,Any]] = []
    dummy_objs: List[Dict[str,Any]] = []
    particle_objs: List[Dict[str,Any]] = []

    for ni, node in enumerate(nodes):
        name = (node.get("name") or f"node{ni}").strip()
        parent_i = parent_map.get(ni, None)
        parent_name = "Scene Root"
        if parent_i is not None:
            pn = (nodes[parent_i].get("name") or "").strip()
            # Strip internal __KEY__ wrapper nodes (created during VFX import for pivot separation).
            # Children of KEY nodes should reference the original mesh name instead.
            if pn.endswith("__KEY__"):
                base_pn = pn[:-len("__KEY__")]
                # If stripping __KEY__ gives our own name (mesh is child of its own KEY wrapper),
                # look through to the KEY node's parent for the real hierarchy parent.
                if base_pn == name:
                    grandparent_i = parent_map.get(parent_i, None)
                    if grandparent_i is not None:
                        gpn = (nodes[grandparent_i].get("name") or "").strip()
                        if gpn.endswith("__KEY__"):
                            gpn = gpn[:-len("__KEY__")]
                        pn = gpn
                    else:
                        pn = ""
                else:
                    pn = base_pn
            parent_name = pn if pn and pn not in ("__VFX_ROOT__", "RFVFX_ROOT") else "Scene Root"

        parent_is_mesh = False
        if parent_i is not None:
            try:
                parent_is_mesh = ("mesh" in (nodes[parent_i] or {}))
            except Exception:
                parent_is_mesh = False

        t = node.get("translation") or [0.0,0.0,0.0]
        r = node.get("rotation") or [0.0,0.0,0.0,1.0]
        s = node.get("scale") or [1.0,1.0,1.0]

        # RF round-trip metadata (if present in glTF extras)
        rf_ex = {}
        try:
            ex = node.get("extras")
            if isinstance(ex, dict):
                rf_ex = ex.get("rf_vfx") or {}
                if not isinstance(rf_ex, dict):
                    rf_ex = {}
        except Exception:
            rf_ex = {}

        if "mesh" in node:
            mesh_i = int(node["mesh"])
            mesh = meshes_g[mesh_i]
            prim = (mesh.get("primitives") or [None])[0]
            if not prim:
                continue
            attrs = prim.get("attributes") or {}
            if "POSITION" not in attrs:
                continue
            pos = _gltf_accessor_read_vec3_f32(g, bin_data, int(attrs["POSITION"]))
            uv = None
            if "TEXCOORD_0" in attrs:
                uv = _gltf_accessor_read_vec2_f32(g, bin_data, int(attrs["TEXCOORD_0"]))
            indices = None
            if "indices" in prim:
                indices = _gltf_accessor_read_indices(g, bin_data, int(prim["indices"]))
            else:
                indices = list(range(len(pos)))
            if len(indices) % 3 != 0:
                raise RuntimeError(f"Mesh '{name}' indices not divisible by 3 (only triangles supported).")
            targets = []
            for tgt in prim.get("targets") or []:
                if "POSITION" in tgt:
                    targets.append(_gltf_accessor_read_vec3_f32(g, bin_data, int(tgt["POSITION"])))


            # Apply anchor shift to ROOT nodes only (global recenter)
            t_b = (float(t[0]), float(t[1]), float(t[2]))
            r_b = (float(r[0]), float(r[1]), float(r[2]), float(r[3]))
            s_b = (float(s[0]), float(s[1]), float(s[2]))
            if parent_name == "Scene Root" and anchor_shift != (0.0,0.0,0.0):
                t_b = (t_b[0] - anchor_shift[0], t_b[1] - anchor_shift[1], t_b[2] - anchor_shift[2])

            # If this mesh is MORPHED, VFX has no per-mesh TRS for it.
            # So we must bake the node's local TRS into the vertex positions (base + all morph targets).
            def _apply_trs(pts, t, q, s):
                m3 = quat_to_mat3(q)
                out = []
                for (x,y,z) in pts:
                    x2,y2,z2 = x*s[0], y*s[1], z*s[2]
                    rx = m3[0][0]*x2 + m3[0][1]*y2 + m3[0][2]*z2
                    ry = m3[1][0]*x2 + m3[1][1]*y2 + m3[1][2]*z2
                    rz = m3[2][0]*x2 + m3[2][1]*y2 + m3[2][2]*z2
                    out.append((rx + t[0], ry + t[1], rz + t[2]))
                return out

            if len(targets) > 0:
                # bake local TRS into morph vertices
                if t_b != (0.0,0.0,0.0) or r_b != (0.0,0.0,0.0,1.0) or s_b != (1.0,1.0,1.0):
                    pos = _apply_trs(pos, t_b, r_b, s_b)
                    targets = [_apply_trs(tgt, t_b, r_b, s_b) for tgt in targets]
                    # after baking, clear TRS so we don't also store it
                    t_b = (0.0,0.0,0.0)
                    r_b = (0.0,0.0,0.0,1.0)
                    s_b = (1.0,1.0,1.0)
            mat_i = int(prim.get("material", 0)) if (prim.get("material") is not None) else 0
            mesh_objs.append({
                "node_index": ni,
                "name": name,
                "parent_name": parent_name,
                "parent_is_mesh": bool(parent_is_mesh),
                "t": (float(t_b[0]), float(t_b[1]), float(t_b[2])),
                "r": (float(r_b[0]), float(r_b[1]), float(r_b[2]), float(r_b[3])),
                "s": (float(s_b[0]), float(s_b[1]), float(s_b[2])),
                "positions": pos,
                "uv": uv,
                "indices": indices,
                "targets": targets,
                "material_index": mat_i,
                "rf_vfx": rf_ex,
                "extras": (node.get("extras") or {}),
            })
        else:
            # Check for particle system nodes (stored from VFX→glTF import)
            node_extras = node.get("extras") or {}
            ps_data = node_extras.get("rf_vfx_particle")
            is_authored_emitter = node_extras.get("rfvfx_particle_emitter")
            if isinstance(ps_data, dict) and ps_data.get("rf_vfx_particle"):
                # Round-trip from VFX→glTF import (has raw binary)
                particle_objs.append({
                    "node_index": ni,
                    "name": name,
                    "parent_name": parent_name,
                    "ps_data": ps_data,
                })
            elif is_authored_emitter:
                # Authored particle emitter from Blender panel
                t_b = (float(t[0]), float(t[1]), float(t[2]))
                r_b = (float(r[0]), float(r[1]), float(r[2]), float(r[3]))
                if parent_name == "Scene Root" and anchor_shift != (0.0,0.0,0.0):
                    t_b = (t_b[0] - anchor_shift[0], t_b[1] - anchor_shift[1], t_b[2] - anchor_shift[2])
                # Read flat IDprops from extras
                ps_author = {
                    "flags": int(node_extras.get("rfvfx_pe_flags", 0x10)),
                    "particle_size": float(node_extras.get("rfvfx_pe_size", 0.45)),
                    "birth_rate": int(node_extras.get("rfvfx_pe_birth_rate", 10)),
                    "speed": float(node_extras.get("rfvfx_pe_speed", 0.42)),
                    "lifetime": int(node_extras.get("rfvfx_pe_lifetime", 30)),
                    "fps": int(node_extras.get("rfvfx_pe_fps", 15)),
                    "apply_gravity": int(node_extras.get("rfvfx_pe_gravity", 0)),
                    "shrink_ratio": float(node_extras.get("rfvfx_pe_shrink", 0.2)),
                    "fade_at_death": float(node_extras.get("rfvfx_pe_fade", 0.6)),
                    "save_parent": int(node_extras.get("rfvfx_pe_save_parent", 0)),
                    "texture_name": str(node_extras.get("rfvfx_pe_texture", "")),
                    "additive": int(node_extras.get("rfvfx_pe_additive", 1)),
                    "size_variation": float(node_extras.get("rfvfx_pe_size_var", 0.1)),
                    "speed_variation": float(node_extras.get("rfvfx_pe_speed_var", 0.0)),
                    "gravity_strength": float(node_extras.get("rfvfx_pe_grav_str", 1.0)),
                    "emitter_radius": float(node_extras.get("rfvfx_pe_emit_rad", 0.0)),
                    "random_direction": float(node_extras.get("rfvfx_pe_rand_dir", 30.0)),
                }
                raw_b64 = node_extras.get("rfvfx_pe_raw_b64", "")
                if raw_b64:
                    ps_author["raw_body_b64"] = str(raw_b64)
                particle_objs.append({
                    "node_index": ni,
                    "name": name,
                    "parent_name": parent_name,
                    "t": t_b,
                    "r": r_b,
                    "ps_data": ps_author,
                })
            # Treat '$*' nodes as DMMY (markers/props)
            elif name.startswith("$"):
                # DMMY nodes must use THEIR OWN local TRS. Do NOT reuse the last mesh's t_b/r_b.
                t_b = (float(t[0]), float(t[1]), float(t[2]))
                r_b = (float(r[0]), float(r[1]), float(r[2]), float(r[3]))
                # Anchor shift applies only to ROOT nodes (parent == Scene Root)
                if parent_name == "Scene Root" and anchor_shift != (0.0,0.0,0.0):
                    t_b = (t_b[0] - anchor_shift[0], t_b[1] - anchor_shift[1], t_b[2] - anchor_shift[2])
                dummy_objs.append({
                    "node_index": ni,
                    "name": name,
                    "parent_name": parent_name,
                    "t": t_b,
                    "r": r_b,
                })

    # --- normalize & preserve materials ---
    # Use only materials actually referenced by meshes, and remap them densely (0..N-1).
    # If template VFX is provided, prefer its tex0 strings (RF-native names) over glTF image uris.
    used_mat_ids = sorted({int(mo.get("material_index", 0)) for mo in mesh_objs}) if mesh_objs else [0]
    mat_id_remap = {old: new for new, old in enumerate(used_mat_ids)}
    materials_g = g.get("materials") or []
    mats: List[Dict[str,Any]] = []
    for old in used_mat_ids:
        tex0 = gltf_tex0_by_mat.get(int(old), "")
        mat_additive = 0
        mat_self_illum = 1.0  # VFX default: fully self-lit (effects glow)
        mat_opacity: Optional[List[float]] = None
        mat_self_illum_list: Optional[List[float]] = None
        # Template material data takes priority (preserves original values)
        if template_materials is not None and 0 <= int(old) < len(template_materials):
            tm = template_materials[int(old)]
            if tm.tex0:
                tex0 = tm.tex0
            mat_additive = 1 if tm.additive else 0
            if tm.opacity_frames is not None:
                mat_opacity = list(tm.opacity_frames)
            if tm.self_illum is not None:
                mat_self_illum_list = list(tm.self_illum)
        # glTF material extras: rf_vfx round-trip data then authoring overrides
        mat_g = (materials_g[int(old)] if 0 <= int(old) < len(materials_g) else {})
        mat_extras = mat_g.get("extras") or {} if isinstance(mat_g, dict) else {}
        # Round-trip extras (from VFX→glTF import)
        rt_add = mat_extras.get("rf_vfx_additive")
        if rt_add is not None and template_materials is None:
            mat_additive = int(rt_add)
        rt_opacity = mat_extras.get("rf_vfx_opacity")
        if isinstance(rt_opacity, list) and template_materials is None:
            mat_opacity = [float(v) for v in rt_opacity]
        rt_si = mat_extras.get("rf_vfx_self_illum")
        if isinstance(rt_si, list) and template_materials is None:
            mat_self_illum_list = [float(v) for v in rt_si]
        rt_tex = str(mat_extras.get("rf_vfx_tex0", "") or "").strip()
        if rt_tex and not tex0:
            tex0 = rt_tex
        # Authoring panel overrides (highest priority)
        auth_add = mat_extras.get("rfvfx_additive")
        if auth_add is not None:
            mat_additive = int(auth_add)
        auth_tex = str(mat_extras.get("rfvfx_texture_name", "") or "").strip()
        if auth_tex:
            tex0 = auth_tex
        # Build self_illum as float for _make_matl_section
        if mat_self_illum_list:
            mat_self_illum = mat_self_illum_list[0] if mat_self_illum_list else 1.0
        mats.append({"tex0": tex0, "additive": mat_additive,
                      "self_illum": mat_self_illum, "opacity": mat_opacity,
                      "self_illum_list": mat_self_illum_list})
    # apply remap so meshes reference the dense material table we will emit
    for mo in mesh_objs:
        mo["material_index"] = int(mat_id_remap.get(int(mo.get("material_index", 0)), 0))


    # --- infer durations (global end_frame is in 15fps units) ---
    max_end_time = 0.0
    morph_meta: Dict[str, Dict[str,Any]] = {}
    for mo in mesh_objs:
        if not mo["targets"]:
            continue
        wa = _gltf_find_weights_anim(g, bin_data, int(mo["node_index"]))
        if wa:
            times, weights_flat, num_targets = wa
            inferred_fps = _infer_fps_from_times(times, default_fps=15)
            # Force 15fps for RF compatibility. Remap times to 15fps spacing.
            fps = 15
            # Recalculate end time based on number of animation samples at 15fps
            num_weight_samples = len(times) if times else 0
            if num_weight_samples > 0:
                end_t = float(num_weight_samples - 1) / float(fps)
            else:
                end_t = float(len(mo["targets"])) / float(fps)
            morph_meta[mo["name"].lower()] = {"times": times, "weights": weights_flat, "num_targets": num_targets, "fps": fps}
            if end_t > max_end_time: max_end_time = end_t
        else:
            # no anim; still has targets
            fps = 15
            end_t = float(len(mo["targets"])) / float(fps) if mo["targets"] else 0.0
            morph_meta[mo["name"].lower()] = {"times": None, "weights": None, "num_targets": len(mo["targets"]), "fps": fps}
            if end_t > max_end_time: max_end_time = end_t

    end_frame = int(round(max_end_time * 15.0))
    if end_frame < 0: end_frame = 0

    # Check glTF scene extras for original end_frame (round-trip from VFX import)
    scene_extras = (g.get("scenes") or [{}])[0].get("extras", {})
    scene_rf = scene_extras.get("rf_vfx", {}) if isinstance(scene_extras, dict) else {}
    gltf_end_frame = scene_rf.get("end_frame") if isinstance(scene_rf, dict) else None
    if isinstance(gltf_end_frame, (int, float)) and int(gltf_end_frame) > end_frame:
        end_frame = int(gltf_end_frame)
        max_end_time = float(end_frame) / 15.0
    # RF ignores VFX files with end_frame=0. Static meshes need at least 1 frame.
    if end_frame == 0 and (mesh_objs or dummy_objs or particle_objs):
        end_frame = 1

    # If a template VFX is provided, preserve its timing (exact round-trip behavior).
    if template_hdr is not None:
        end_frame = int(template_hdr.end_frame)
        if end_frame < 0: end_frame = 0
        max_end_time = float(end_frame) / 15.0
    # RF does not render VFX files with end_frame=0. Ensure at least 5.
    # All known working VFX files have end_frame >= 5.
    if end_frame < 5 and (mesh_objs or dummy_objs or particle_objs):
        end_frame = 5
        max_end_time = 5.0 / 15.0

    if debug:
        print(f"[NEW_VFX] meshes={len(mesh_objs)} dummies={len(dummy_objs)} materials={len(mats)} end_time={max_end_time:.6f}s end_frame(15fps)={end_frame}")

    # --- build sections ---
    sec_bytes = bytearray()

    total_faces = 0
    total_mesh_material_indices = 0
    total_face_vertices = 0
    total_adjacent = 0
    total_mesh_frames = 0
    total_uv_frames = 0
    total_dummy_frames = (end_frame + 1) * len(dummy_objs)

    total_keyframe_lists = 0
    total_translation_keys = 0
    total_rotation_keys = 0
    total_scale_keys = 0

    # Mesh sections
    for mo in mesh_objs:
        name = mo["name"]
        parent_name = mo["parent_name"]
        # Custom name override from authoring panel
        node_ex_raw = mo.get("extras") or {}
        custom_name = str(node_ex_raw.get("rfvfx_custom_name", "") or "").strip()
        if custom_name:
            name = custom_name
        bake_s = None
        try:
            rf_ex = mo.get("rf_vfx") or {}
            bake_s = rf_ex.get("baked_geom_scale") if isinstance(rf_ex, dict) else None
        except Exception:
            bake_s = None
        if isinstance(bake_s, (list, tuple)) and len(bake_s) == 3:
            bx,by,bz = (float(bake_s[0]), float(bake_s[1]), float(bake_s[2]))
            if abs(bx) < 1e-12: bx = 1.0
            if abs(by) < 1e-12: by = 1.0
            if abs(bz) < 1e-12: bz = 1.0
            pos_bl = [(p[0]/gltf_scale/bx, p[1]/gltf_scale/by, p[2]/gltf_scale/bz) for p in mo["positions"]]
            targets_bl = [[(tp[0]/gltf_scale/bx, tp[1]/gltf_scale/by, tp[2]/gltf_scale/bz) for tp in tgt] for tgt in (mo.get("targets") or [])]
        else:
            pos_bl = [(p[0]/gltf_scale, p[1]/gltf_scale, p[2]/gltf_scale) for p in mo["positions"]]
            targets_bl = (mo.get("targets") or [])
        uv_bl = None
        if mo["uv"] is not None:
            uv_bl = [(u[0], u[1]) for u in mo["uv"]]
        indices = mo["indices"]
        num_vertices = len(pos_bl)
        num_faces = int(len(indices) // 3)
        total_faces += num_faces

        # materials per mesh (simple: one)
        total_mesh_material_indices += 1

        # face_vertices table (v4.6): list of "vertex normals" entries.
        # Faces reference this table via fvi0/fvi1/fvi2.
        # Uses normal-based grouping for shared face-vertex entries (matching 3ds Max exporter).
        fv_entries: List[Dict[str, Any]] = []

        # flags — resolve from multiple sources:
        #   1. Authoring panel extras (rfvfx_*) — user explicitly set in Blender
        #   2. rf_vfx round-trip extras (flags from original VFX) — preserved from import
        #   3. Auto-detection (morph from shape keys)
        #   4. Default (0)
        has_shape_keys = True if (mo["targets"] and len(mo["targets"]) > 0) else False
        # Force morph path for ALL meshes in true export.
        # RF clutter system requires morph meshes (per-frame vertex data).
        # Non-morph keyframed meshes only work in specific engine contexts
        # (weapons, cutscenes) but fail as clutter items.
        # Static meshes simply repeat frame 0 vertices for all frames.
        is_morph = True
        node_ex = mo.get("extras") or {}
        # rf_ex already set above (bake_s section)

        def _flag_bool(authoring_key: str, rf_key: str, default: bool) -> bool:
            """Resolve a boolean flag: authoring extras > rf_vfx round-trip > default."""
            # Authoring panel (rfvfx_* IDprops exported as glTF extras)
            v = node_ex.get(authoring_key)
            if v is not None:
                return bool(int(v))
            # rf_vfx round-trip
            v = rf_ex.get(rf_key)
            if v is not None:
                return bool(int(v))
            return default

        flag_facing = _flag_bool("rfvfx_facing", "facing", False)
        flag_facing_rod = _flag_bool("rfvfx_facing_rod", "facing_rod", False)
        flag_morph = is_morph  # morph flag MUST match data layout (shape keys present or not)
        flag_dump_uvs = _flag_bool("rfvfx_dump_uvs", "dump_uvs", False)

        # Build flags_raw from resolved booleans
        # If rf_vfx has the original flags_raw AND no authoring overrides, prefer it
        # (preserves unknown flag bits we don't decode)
        has_authoring_flags = any(node_ex.get(k) is not None for k in
            ("rfvfx_facing", "rfvfx_facing_rod", "rfvfx_morph", "rfvfx_dump_uvs"))
        if not has_authoring_flags and isinstance(rf_ex.get("flags_raw"), (int, float)):
            # Preserve original flags_raw (including any unknown bits)
            flags_raw = int(rf_ex["flags_raw"])
            # But ensure morph bit matches actual shape key presence
            if is_morph:
                flags_raw |= 0x00000004
            elif not flag_morph:
                flags_raw &= ~0x00000004
        else:
            flags_raw = 0
            if flag_facing:     flags_raw |= 0x00000001
            if flag_morph:      flags_raw |= 0x00000004
            if flag_dump_uvs:   flags_raw |= 0x00000100
            if flag_facing_rod: flags_raw |= 0x00000800

        # save_parent: authoring > rf_vfx > default(0)
        save_parent_val = 0
        v = node_ex.get("rfvfx_save_parent")
        if v is not None:
            save_parent_val = int(v)
        elif rf_ex.get("save_parent") is not None:
            save_parent_val = int(rf_ex["save_parent"])

        # facing billboard width (authoring > default 1.0)
        facing_width = 1.0
        v = node_ex.get("rfvfx_width")
        if v is not None:
            facing_width = float(v)
        elif isinstance(rf_ex.get("facing_pairs"), list) and len(rf_ex["facing_pairs"]) > 0:
            facing_width = float(rf_ex["facing_pairs"][0][0])  # use first frame's width

        # fps and num_frames
        fps = 15
        start_time = 0.0
        end_time = float(end_frame) / 15.0 if end_frame > 0 else 0.0
        num_frames = end_frame + 1
        weights_info = morph_meta.get(name.lower())

        # Check for baked sidecar data first
        baked_mesh_pre = baked_frames_data.get(name) if baked_frames_data else None
        if baked_mesh_pre and "frames" in baked_mesh_pre:
            num_frames = len(baked_mesh_pre["frames"])
            end_time = float(num_frames - 1) / float(fps) if fps > 0 else 0.0
            if weights_info:
                weights_info["times"] = None
                weights_info["weights"] = None
        elif is_morph and weights_info:
            fps = int(weights_info["fps"])
            # For true export with baked per-frame shape keys, ignore weight animation
            # and use the sequential target path (frame0=base, frame1=target0, etc.).
            # Weight animation from Blender's shape key keyframes is unreliable for
            # per-frame sampling. The sequential path directly maps targets to frames.
            weights_info["times"] = None
            weights_info["weights"] = None
            num_frames = len(mo["targets"]) + 1
            end_time = float(num_frames - 1) / float(fps) if fps > 0 else 0.0
        total_mesh_frames += int(num_frames)
        if uv_bl is not None:
            total_uv_frames += 1

        # Build faces + uv-per-face-vertex order
        face_uvs: List[Tuple[float,float]] = []
        faces_blob = bytearray()
        smoothing_group = 1

        # --- Pass 1: compute face normals and build faces_blob (fvi placeholder) ---
        face_normals: List[Tuple[float,float,float]] = []
        for fi in range(num_faces):
            i0 = int(indices[fi*3+0]); i1 = int(indices[fi*3+1]); i2 = int(indices[fi*3+2])
            faces_blob += struct.pack("<iii", i0, i1, i2)
            for _ in range(9):
                faces_blob += struct.pack("<f", 1.0)
            a = pos_bl[i0]; b = pos_bl[i1]; c = pos_bl[i2]
            n_bl = _tri_normal(a,b,c)
            face_normals.append(n_bl)
            center_bl = ((a[0]+b[0]+c[0])/3.0, (a[1]+b[1]+c[1])/3.0, (a[2]+b[2]+c[2])/3.0)
            r = 0.0
            for v in (a,b,c):
                dx=v[0]-center_bl[0]; dy=v[1]-center_bl[1]; dz=v[2]-center_bl[2]
                d=math.sqrt(dx*dx+dy*dy+dz*dz)
                if d>r: r=d

            faces_blob += _pack_vec3_rf(blender_to_rf(n_bl))
            faces_blob += _pack_vec3_rf(blender_to_rf(center_bl))
            faces_blob += struct.pack("<f", float(r))
            faces_blob += struct.pack("<i", 0)    # material_index (mesh-local)
            faces_blob += struct.pack("<i", smoothing_group)
            # fvi placeholder (3 ints) — will be patched below
            fvi_offset_in_blob = len(faces_blob)
            faces_blob += struct.pack("<iii", 0, 0, 0)

            if uv_bl is not None:
                face_uvs.extend([uv_bl[i0], uv_bl[i1], uv_bl[i2]])

        # --- Pass 2: Build shared face_vertex table using normal-based grouping ---
        # Group face corners by vertex index, splitting groups where normals diverge.
        # This matches the original 3ds Max VFX exporter behavior and avoids bloated tables.
        vi_groups: Dict[int, List[List[int]]] = {}  # vi -> list of face groups
        for fi in range(num_faces):
            for c in range(3):
                vi = int(indices[fi*3 + c])
                fn = face_normals[fi]
                if vi not in vi_groups:
                    vi_groups[vi] = [[fi]]
                else:
                    placed = False
                    for grp in vi_groups[vi]:
                        ref_fn = face_normals[grp[0]]
                        if _dot3(fn, ref_fn) > 0.0:
                            grp.append(fi)
                            placed = True
                            break
                    if not placed:
                        vi_groups[vi].append([fi])

        # Create fv entries and map (fi, corner) -> fvi_index
        fv_entries: List[Dict[str, Any]] = []
        corner_to_fvi: Dict[Tuple[int,int], int] = {}

        for vi in sorted(vi_groups.keys()):
            for gi, face_list in enumerate(vi_groups[vi]):
                fvi_idx = len(fv_entries)
                unique_faces = sorted(set(face_list))
                fv_entries.append({"vi": vi, "smooth": smoothing_group, "faces": unique_faces})
                for fi in unique_faces:
                    for c in range(3):
                        if int(indices[fi*3 + c]) == vi:
                            corner_to_fvi[(fi, c)] = fvi_idx

        # Build face_fvi_refs and patch into faces_blob
        # Each face record in faces_blob: 3i(indices) + 9f(colors) + 3f(normal) + 3f(center) + f(radius) + i(mat) + i(sg) + 3i(fvi)
        # Total per-face bytes: 12 + 36 + 12 + 12 + 4 + 4 + 4 + 12 = 96 bytes
        face_record_size = 96
        fvi_offset_in_face = 84  # offset of fvi0 within a face record
        for fi in range(num_faces):
            fvi0 = corner_to_fvi.get((fi, 0), 0)
            fvi1 = corner_to_fvi.get((fi, 1), 0)
            fvi2 = corner_to_fvi.get((fi, 2), 0)
            off = fi * face_record_size + fvi_offset_in_face
            struct.pack_into("<iii", faces_blob, off, fvi0, fvi1, fvi2)

        # finalize face_vertices totals for header counts
        num_face_vertices = len(fv_entries)
        total_face_vertices += num_face_vertices
        adj_sum = 0
        for _fv in fv_entries:
            adj_sum += len(_fv.get("faces") or [])
        total_adjacent += int(adj_sum)

        # Bounding sphere
        bcenter_bl, brad = _bounds_center_radius(pos_bl)
        bcenter_rf = blender_to_rf(bcenter_bl)

        # Mesh body
        body = bytearray()
        body += _pack_strz(name)
        body += _pack_strz(parent_name)
        body += struct.pack("<b", int(save_parent_val))  # save_parent
        body += struct.pack("<i", int(num_vertices))
        body += struct.pack("<i", int(num_faces))
        body += faces_blob
        body += struct.pack("<i", int(fps))
        body += struct.pack("<f", float(start_time))
        body += struct.pack("<f", float(end_time))
        body += struct.pack("<i", int(num_frames))

        body += struct.pack("<i", 1)  # num_materials in mesh
        body += struct.pack("<i", int(mo["material_index"]))  # global mat index

        body += _pack_vec3_rf(bcenter_rf)
        body += struct.pack("<f", float(brad))
        body += struct.pack("<I", int(flags_raw))

        num_face_vertices = len(fv_entries)
        body += struct.pack("<i", int(num_face_vertices))
        # mesh_face_vertex table: (smoothing_group, vertex_index, u,v legacy, n_adj, adj_faces[])
        for fv in fv_entries:
            vi = int(fv.get("vi", 0))
            adj = sorted(set(int(x) for x in (fv.get("faces") or [])))
            body += struct.pack("<i", int(fv.get("smooth", 1)))
            body += struct.pack("<i", vi)
            body += struct.pack("<f", 0.0)  # u legacy
            body += struct.pack("<f", 0.0)  # v legacy
            body += struct.pack("<i", int(len(adj)))
            for afi in adj:
                body += struct.pack("<i", int(afi))
        rf_ex = mo.get("rf_vfx") or {}
        rf_block_b64 = rf_ex.get("keyframed_block_b64") if isinstance(rf_ex, dict) else None
        rf_block = None
        if isinstance(rf_block_b64, str) and rf_block_b64:
            try:
                rf_block = base64.b64decode(rf_block_b64.encode("ascii"))
            except Exception:
                rf_block = None

        is_keyframed = (not is_morph) and (not bool(mo.get("parent_is_mesh", False)))
        # Only use rf_block for keyframed path; morph meshes are never keyframed
        if not is_morph and rf_block is not None and _rf46_keyframed_block_is_sane(rf_block):
            is_keyframed = True

        if is_keyframed:
            total_keyframe_lists += 1
            if rf_block is not None and _rf46_keyframed_block_is_sane(rf_block):
                nt, nr, ns = _rf46_keyframed_block_counts(rf_block)
                total_translation_keys += nt
                total_rotation_keys += nr
                total_scale_keys += ns
            else:
                total_translation_keys += 1
                total_rotation_keys += 1
                total_scale_keys += 1

        body += struct.pack("<B", 1 if is_keyframed else 0)  # is_keyframed

        # frames
        if is_morph:
            # determine frame absolute positions
            base = pos_bl
            targets = targets_bl
            frame_positions: List[List[Tuple[float,float,float]]] = []

            # Prefer baked per-frame data from sidecar (written by Blender add-on)
            baked_mesh = baked_frames_data.get(name) if baked_frames_data else None
            if baked_mesh and "frames" in baked_mesh:
                baked_verts = baked_mesh["frames"]
                if debug:
                    print(f"  [BAKED] Using sidecar data: {len(baked_verts)} frames, {len(baked_verts[0])} verts")
                for bfi in range(len(baked_verts)):
                    # Baked data is in Blender local space — apply gltf_scale
                    bv = baked_verts[bfi]
                    frame_pts = [(float(v[0]) * gltf_scale, float(v[1]) * gltf_scale, float(v[2]) * gltf_scale) for v in bv]
                    frame_positions.append(frame_pts)
                # Adjust num_frames/end_frame to match baked data
                num_frames = len(frame_positions)
                # Pad or trim
                while len(frame_positions) < num_frames:
                    frame_positions.append(list(frame_positions[-1]))
                frame_positions = frame_positions[:num_frames]
            elif weights_info and weights_info.get("times") and weights_info.get("weights") is not None:
                times = weights_info["times"]
                wflat = weights_info["weights"]
                nt = len(targets)
                for fi in range(len(times)):
                    w = wflat[fi*nt:(fi+1)*nt] if nt>0 else []
                    # Blend all targets using their weights (not winner-takes-all).
                    # pos = base + sum(w[ti] * target[ti]) for all targets
                    frame_pts: List[Tuple[float,float,float]] = []
                    for vi in range(num_vertices):
                        bx, by, bz = base[vi]
                        dx, dy, dz = 0.0, 0.0, 0.0
                        for ti in range(min(nt, len(w))):
                            wt = float(w[ti])
                            if abs(wt) < 1e-7:
                                continue
                            dx += wt * targets[ti][vi][0]
                            dy += wt * targets[ti][vi][1]
                            dz += wt * targets[ti][vi][2]
                        frame_pts.append((bx + dx, by + dy, bz + dz))
                    frame_positions.append(frame_pts)
            else:
                # simple: frame0 base, then each target
                frame_positions.append(list(base))
                for tp in targets:
                    frame_positions.append([(base[i][0]+tp[i][0], base[i][1]+tp[i][1], base[i][2]+tp[i][2]) for i in range(num_vertices)])
                # Pad to num_frames by repeating last frame (static mesh = repeat base)
                while len(frame_positions) < num_frames:
                    frame_positions.append(list(base))
                frame_positions = frame_positions[:num_frames]

            for fi in range(num_frames):
                pts_bl = frame_positions[fi]
                pts_rf = [blender_to_rf(p) for p in pts_bl]
                center_rf, mult_rf, s16s = _quantize_positions_rf(pts_rf)
                body += _pack_vec3_rf(center_rf)
                body += _pack_vec3_rf(mult_rf)
                for (ix,iy,iz) in s16s:
                    body += struct.pack("<hhh", int(ix), int(iy), int(iz))
                # Facing mesh billboard data (2 floats per frame)
                if flag_facing or flag_facing_rod:
                    fp = _get_facing_pair(rf_ex, fi, facing_width)
                    body += struct.pack("<ff", float(fp[0]), float(fp[1]))
                # Facing-rod up_vector (frame 0 only, v >= 0x40001)
                if flag_facing_rod and fi == 0:
                    up = _get_facing_rod_up(rf_ex)
                    body += _pack_vec3_rf(up)
                # UVs: frame 0 always, every frame if dump_uvs
                if fi == 0 or flag_dump_uvs:
                    if uv_bl is None:
                        for _ in range(3*num_faces):
                            body += _pack_uv((0.0,0.0))
                    else:
                        for uv in face_uvs:
                            body += _pack_uv(uv)
        else:
            # non-morph: positions only on frame0 (frames >0 usually contain only transforms if not keyframed)
            pts_rf0 = [blender_to_rf(p) for p in pos_bl]
            center_rf0, mult_rf0, s16s0 = _quantize_positions_rf(pts_rf0)

            t_bl = mo["t"]
            r_bl = mo["r"]
            s_bl = mo["s"]

            for fi in range(num_frames):
                if fi == 0:
                    # Vertex positions only written for frame 0 (non-morph)
                    body += _pack_vec3_rf(center_rf0)
                    body += _pack_vec3_rf(mult_rf0)
                    for (ix,iy,iz) in s16s0:
                        body += struct.pack("<hhh", int(ix), int(iy), int(iz))
                    # Facing mesh billboard data (frame 0 for non-morph)
                    if flag_facing or flag_facing_rod:
                        fp = _get_facing_pair(rf_ex, 0, facing_width)
                        body += struct.pack("<ff", float(fp[0]), float(fp[1]))
                    if flag_facing_rod:
                        up = _get_facing_rod_up(rf_ex)
                        body += _pack_vec3_rf(up)
                    # UVs frame 0 (always present for v>=0x3000D)
                    if uv_bl is None:
                        for _ in range(3*num_faces):
                            body += _pack_uv((0.0,0.0))
                    else:
                        for uv in face_uvs:
                            body += _pack_uv(uv)

                if not is_keyframed:
                    # Non-keyframed meshes store TRS on every frame
                    body += _pack_vec3_rf(blender_to_rf(t_bl))
                    body += _pack_quat_raw(quat_blender_to_rf(r_bl))
                    body += _pack_vec3_rf((float(s_bl[0]), float(s_bl[2]), float(s_bl[1])))

            if is_keyframed:
                # For true export, only reuse the round-trip keyframed block if its
                # rotation and scale are identity-ish. If the user changed transforms
                # in Blender, the old rf_block would double-apply TRS.
                if rf_block is not None and _rf46_keyframed_block_is_sane(rf_block) and _rf46_keyframed_block_has_safe_trs(rf_block):
                    body += rf_block
                else:
                    # Keyframed meshes store pivot + keyframe list (v4.6).
                    # For authored meshes, vertices are already in RF space with transforms applied,
                    # so TRS keyframes must be IDENTITY to avoid double-application.
                    # Use exact byte patterns from known-working RF files (including -0.0 signs).
                    _NEG0 = struct.pack("<f", -0.0)       # 0x80000000
                    _POS0 = struct.pack("<f",  0.0)       # 0x00000000
                    _ONE  = struct.pack("<f",  1.0)       # 0x3F800000
                    _ZERO_I = struct.pack("<i",  0)
                    _ONE_I  = struct.pack("<i",  1)

                    # pivot_translation: (0, 0, 0) — use -0.0 for X,Z to match RF
                    body += _NEG0 + _POS0 + _NEG0
                    # pivot_rotation: (0, -0, 0, 1) — RF identity quat
                    body += _POS0 + _NEG0 + _POS0 + _ONE
                    # pivot_scale: (1, 1, 1)
                    body += _ONE + _ONE + _ONE

                    # Translation key: 1 key at time=0, value=(0,0,0), tangents=(-0,0,-0)
                    body += _ONE_I          # count
                    body += _ZERO_I         # time
                    body += _NEG0 + _POS0 + _NEG0     # value
                    body += _NEG0 + _POS0 + _NEG0     # in_tangent
                    body += _NEG0 + _POS0 + _NEG0     # out_tangent

                    # Rotation key: 1 key at time=0, value=(0,-0,0,1), TCB=(0,0,0,0,0)
                    body += _ONE_I          # count
                    body += _ZERO_I         # time
                    body += _POS0 + _NEG0 + _POS0 + _ONE   # value
                    body += struct.pack("<fffff", 0.0,0.0,0.0,0.0,0.0)  # TCB params

                    # Scale key: 1 key at time=0, value=(1,1,1), tangents=(0,0,0)
                    body += _ONE_I          # count
                    body += _ZERO_I         # time
                    body += _ONE + _ONE + _ONE           # value
                    body += _POS0 + _POS0 + _POS0        # in_tangent
                    body += _POS0 + _POS0 + _POS0        # out_tangent
        sec_bytes += _section(SEC_SFXO, bytes(body))

    # Dummy sections
    for do in dummy_objs:
        body = bytearray()
        body += _pack_strz(do["name"])
        body += _pack_strz(do["parent_name"])
        body += struct.pack("<B", 0)  # save_parent (match RF samples)
        pos_rf = blender_to_rf(do["t"])
        q_rf = quat_blender_to_rf(do["r"])
        body += _pack_vec3_rf(pos_rf)
        body += _pack_quat_raw(q_rf)
        body += struct.pack("<i", int(end_frame + 1))
        for _ in range(end_frame + 1):
            body += _pack_vec3_rf(pos_rf)
            body += _pack_quat_raw(q_rf)
        sec_bytes += _section(SEC_DMMY, bytes(body))

    # Particle system sections
    total_part_sys_frames = 0
    particle_textures: List[Dict[str, Any]] = []   # unique textures from authored particles
    for po in particle_objs:
        ps = po["ps_data"]
        raw_b64 = ps.get("raw_body_b64", "")
        if raw_b64:
            # Round-trip: byte-exact preserved PART body
            raw_body = base64.b64decode(raw_b64)
            sec_bytes += _section(SEC_PART, raw_body)
            # num_part_sys_frames = sum of entry counts across all PARTs
            total_part_sys_frames += int(ps.get("num_particles", ps.get("num_frames", 0)))
        else:
            # Authored: build PART from emitter parameters + position
            t_bl = po.get("t", (0.0, 0.0, 0.0))
            r_bl = po.get("r", (0.0, 0.0, 0.0, 1.0))
            pos_rf = blender_to_rf(t_bl)
            quat_rf = quat_blender_to_rf(r_bl)
            # Static emitter: same position/orientation every frame
            num_entries = end_frame + 1
            positions_rf = [pos_rf] * num_entries
            orientations_rf = [quat_rf] * num_entries
            # Sanitize parent name: 'Scene Root' is a glTF/Blender concept,
            # not a valid RF object. Use empty string (no parent) unless
            # the parent is an actual mesh or dummy in the VFX.
            part_parent = po["parent_name"]
            valid_parents = set(mo["name"] for mo in mesh_objs) | set(do["name"] for do in dummy_objs)
            if part_parent not in valid_parents:
                part_parent = ""
            part_body = _build_part_section(
                name=po["name"],
                parent_name=part_parent,
                save_parent=int(ps.get("save_parent", 0)),
                positions_rf=positions_rf,
                orientations_rf=orientations_rf,
                flags=int(ps.get("flags", 0x10)),
                lifetime=int(ps.get("lifetime", 30)),
                fps=int(ps.get("fps", 15)),
                particle_size=float(ps.get("particle_size", 0.5)),
                speed=float(ps.get("speed", 0.4)),
                shrink_ratio=float(ps.get("shrink_ratio", 0.2)),
                birth_rate=int(ps.get("birth_rate", 10)),
                apply_gravity=int(ps.get("apply_gravity", 0)),
                fade_at_death=float(ps.get("fade_at_death", 0.6)),
                size_variation=float(ps.get("size_variation", 0.0)),
                speed_variation=float(ps.get("speed_variation", 0.0)),
                gravity_strength=float(ps.get("gravity_strength", 1.0)),
                emitter_radius=float(ps.get("emitter_radius", 0.0)),
                random_direction=float(ps.get("random_direction", 30.0)),
            )
            sec_bytes += _section(SEC_PART, part_body)
            total_part_sys_frames += num_entries

            # Collect unique particle textures for MATL generation
            tex = str(ps.get("texture_name", "")).strip()
            add = int(ps.get("additive", 1))
            if tex:
                # Avoid duplicates
                existing = [t for t in particle_textures if t["tex0"] == tex]
                if not existing:
                    particle_textures.append({
                        "tex0": tex,
                        "additive": add,
                        "self_illum": 1.0,
                        "self_illum_list": None,
                        "opacity": None,
                    })

    # Material sections (at end, like sample)
    # Mesh materials first, then particle materials
    all_mats = list(mats) + particle_textures
    for mat_info in all_mats:
        sec_bytes += _section(SEC_MATL, _make_matl_section(
            mat_info["tex0"],
            additive=int(mat_info.get("additive", 0)),
            self_illum=float(mat_info.get("self_illum", 1.0)),
            self_illum_list=mat_info.get("self_illum_list"),
            opacity_frames=mat_info.get("opacity"),
        ))

    # --- header counts ---
    counts = {
        "num_meshes": len(mesh_objs),
        "num_lights": 0,
        "num_dummies": len(dummy_objs),
        "num_particle_systems": len(particle_objs),
        "num_spacewarps": 0,
        "num_cameras": 0,
        "num_selsets": 0,
        "num_materials": len(all_mats),
        "num_mix_frames": 0,
        "num_self_illumination_frames": sum(len(m.get("self_illum_list") or [1.0]) for m in all_mats),
        "num_opacity_frames": sum(len(m.get("opacity") or [1.0]) for m in all_mats),
        "num_faces": total_faces,
        "num_mesh_material_indices": total_mesh_material_indices,
        "num_vertex_normals": total_face_vertices,
        "num_adjacent_faces": total_adjacent,
        "num_mesh_frames": total_mesh_frames,
        "num_uv_frames": total_uv_frames,
        "num_mesh_transform_frames": 0,
        "num_mesh_transform_keyframe_lists": total_keyframe_lists,
        "num_mesh_translation_keys": total_translation_keys,
        "num_mesh_rotation_keys": total_rotation_keys,
        "num_mesh_scale_keys": total_scale_keys,
        "num_light_frames": 0,
        "num_dummy_frames": total_dummy_frames,
        "num_part_sys_frames": total_part_sys_frames,
        "num_spacewarp_frames": 0,
        "num_camera_frames": 0,
        "num_selset_objects": 0,
    }

    out_version = int(template_hdr.version) if template_hdr is not None else 0x00040006
    out_flags = int(template_hdr.flags) if (template_hdr is not None and template_hdr.flags is not None) else 0
    header = _write_vfx_header(out_version, out_flags, end_frame, counts)
    os.makedirs(os.path.dirname(os.path.abspath(out_vfx)) or ".", exist_ok=True)
    with open(out_vfx, "wb") as f:
        f.write(header)
        f.write(sec_bytes)
    print("Wrote:", out_vfx)

def main(argv: List[str]) -> int:
    # Minimal CLI to match your workflow
    args = list(argv)
    debug_frames = False
    out_mode_gltf = False
    scale = 1.0
    trs_scale: Optional[float] = None

    mesh_offsets: Dict[str, Tuple[float,float,float]] = {}
    def _parse_mesh_offset(spec: str) -> Tuple[str, Tuple[float,float,float]]:
        # spec: Name=x,y,z  (floats)
        if "=" not in spec:
            raise ValueError("Expected Name=x,y,z")
        name, rhs = spec.split("=", 1)
        name = name.strip()
        parts = [p.strip() for p in rhs.split(",")]
        if len(parts) != 3:
            raise ValueError("Expected three comma-separated numbers: x,y,z")
        x = float(parts[0]); y = float(parts[1]); z = float(parts[2])
        return name.lower(), (x,y,z)

    # New modes
    roundtrip_only = False
    verify_roundtrip = False
    roundtrip_out: Optional[str] = None

    vfx_out: Optional[str] = None
    patch_vfx_only = False

    gltf_in: Optional[str] = None
    gltf_scale = 1.0
    only_meshes: Optional[List[str]] = None
    dump_sections = False

    new_from_gltf: Optional[str] = None


    anchor_name: Optional[str] = None
    template_vfx: Optional[str] = None
    baked_frames_path: Optional[str] = None
    i = 0
    files: List[str] = []
    while i < len(args):
        a = args[i]
        if a == "--debug-frames":
            debug_frames = True; i += 1; continue
        if a == "--gltf":
            out_mode_gltf = True; i += 1; continue
        if a == "--scale":
            scale = float(args[i+1]); i += 2; continue
        if a == "--trs-scale":
            trs_scale = float(args[i+1]); i += 2; continue
        if a == "--mesh-offset":
            name,(x,y,z) = _parse_mesh_offset(args[i+1])
            mesh_offsets[name] = (x,y,z)
            i += 2; continue

        # Roundtrip writer
        if a == "--roundtrip-only":
            roundtrip_only = True; i += 1; continue
        if a == "--verify-roundtrip":
            verify_roundtrip = True; i += 1; continue
        if a == "--roundtrip-out":
            roundtrip_out = args[i+1]; i += 2; continue

        # VFX output (patch-in-place, size-preserving)
        if a == "--vfx-out":
            vfx_out = args[i+1]; i += 2; continue
        if a == "--patch-vfx-only":
            patch_vfx_only = True; i += 1; continue

        # Patch from edited glTF into template VFX
        if a == "--gltf-in":
            gltf_in = args[i+1]; i += 2; continue
        if a == "--gltf-scale":
            gltf_scale = float(args[i+1]); i += 2; continue
        if a == "--new-vfx-from-gltf":
            new_from_gltf = args[i+1]; i += 2; continue
        if a == "--anchor":
            anchor_name = args[i+1]; i += 2; continue
        if a == "--template-vfx":
            template_vfx = args[i+1]; i += 2; continue
        if a == "--baked-frames":
            baked_frames_path = args[i+1]; i += 2; continue

        if a == "--only-mesh":
            only_meshes = [s.strip() for s in args[i+1].split(",") if s.strip()]; i += 2; continue

        if a == "--dump-sections":
            dump_sections = True; i += 1; continue

        if a == "--help" or a == "-h":
            print("Usage:")
            print("  Export glTF: vfx2obj.py --gltf [--debug-frames] [--scale N] [--trs-scale N] [--mesh-offset Name=x,y,z] file.vfx")
            print("  Roundtrip:   vfx2obj.py --roundtrip-only [--verify-roundtrip] [--roundtrip-out PATH] file.vfx")
            print("  Patch VFX (offset): vfx2obj.py --vfx-out PATH --patch-vfx-only --mesh-offset Name=x,y,z file.vfx")
            print("  Patch VFX from glTF: vfx2obj.py --vfx-out PATH --patch-vfx-only --gltf-in edited.gltf [--gltf-scale N] [--only-mesh a,b] template.vfx")
            print("  True export: vfx2obj.py --new-vfx-from-gltf scene.gltf --vfx-out PATH [--gltf-scale N] [--anchor NODE] [--template-vfx template.vfx]")
            print("  New VFX from glTF: vfx2obj.py --new-vfx-from-gltf scene.gltf --vfx-out PATH [--gltf-scale N] [--template-vfx template.vfx]")
            return 0

        files.append(a)
        i += 1

    # True export: glTF -> new VFX (no template)
    if new_from_gltf is not None:
        if vfx_out is None:
            base = os.path.splitext(os.path.basename(new_from_gltf))[0]
            out_dir = os.path.dirname(os.path.abspath(new_from_gltf)) or "."
            vfx_out = os.path.join(out_dir, base + ".vfx")
        export_new_vfx_from_gltf(new_from_gltf, vfx_out, gltf_scale=gltf_scale, anchor=anchor_name, template_vfx=template_vfx, debug=debug_frames, baked_frames_path=baked_frames_path)
        return 0

    if not files:
        print("No input files. Provide a .vfx path.")
        return 2

    if trs_scale is None:
        trs_scale = scale

    for in_path in files:
        if not os.path.isfile(in_path):
            print(f"[SKIP] Not a file: {in_path}")
            continue

        base = os.path.splitext(os.path.basename(in_path))[0]
        out_dir = os.path.dirname(os.path.abspath(in_path)) or "."

        if dump_sections:
            # simple dump of section types/lengths
            data = open(in_path, "rb").read()
            b = Bin(data)
            _ = parse_header(b)
            print(f"=== Sections: {os.path.basename(in_path)} ===")
            while b.tell() < len(data):
                sec_type = b.u32()
                sec_len = b.u32()
                body_len = sec_len - 4
                section_end = b.tell() + body_len
                print(f"  type=0x{sec_type:08X} len={sec_len} body={body_len} at=0x{(b.tell()-8):X}")
                b.seek(section_end)

        if roundtrip_only:
            outp = roundtrip_out
            if outp is None:
                outp = os.path.join(out_dir, "out", base + ".roundtrip.vfx")
            roundtrip_copy(in_path, outp, verify=verify_roundtrip)
            continue

        # Patch VFX output modes
        if vfx_out is not None and patch_vfx_only:
            if gltf_in is not None:
                patch_vfx_meshes_from_gltf(in_path, gltf_in, vfx_out, gltf_scale=gltf_scale, only_meshes=only_meshes, debug=debug_frames)
            elif mesh_offsets:
                patch_vfx_apply_mesh_offsets(in_path, vfx_out, mesh_offsets=mesh_offsets, scale=scale, debug=debug_frames)
            else:
                raise RuntimeError("--patch-vfx-only requires either --gltf-in or --mesh-offset")
            continue

        # Default: export glTF
        out_gltf = os.path.join(out_dir, base + ".gltf")
        export_gltf(in_path, out_gltf, debug_frames=debug_frames, scale=scale, trs_scale=trs_scale, mesh_offsets=mesh_offsets)

    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))