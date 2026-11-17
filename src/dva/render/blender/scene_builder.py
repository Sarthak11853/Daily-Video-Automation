"""Blender scene builder — runs INSIDE Blender (or with the `bpy` module).

    blender -b --factory-startup --python scene_builder.py -- --spec scene.json
    python scene_builder.py --spec scene.json          # with `pip install bpy`

Builds one scene of the video entirely from procedural, license-free assets:
an environment, 1-3 hero props, the channel mascot ("Gyaanu" the robot),
an extruded 3D keyword, ambient particles and an animated camera, then
renders PNG frames. Everything is seeded so re-renders are identical.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys

import bpy  # type: ignore

D2R = math.pi / 180.0

# ---------------------------------------------------------------- palette
PAL = {
    "white": (0.95, 0.95, 0.97), "ink": (0.03, 0.04, 0.09), "red": (0.95, 0.12, 0.18),
    "orange": (1.0, 0.45, 0.08), "yellow": (1.0, 0.80, 0.10), "green": (0.20, 0.75, 0.25),
    "lime": (0.55, 0.92, 0.20), "teal": (0.05, 0.75, 0.75), "blue": (0.10, 0.40, 0.95),
    "sky": (0.35, 0.70, 1.0), "purple": (0.55, 0.25, 0.95), "pink": (1.0, 0.40, 0.65),
    "brown": (0.40, 0.22, 0.10), "sand": (0.95, 0.78, 0.50), "grey": (0.55, 0.57, 0.62),
    "gold": (1.0, 0.70, 0.15), "ice": (0.70, 0.92, 1.0), "navy": (0.05, 0.08, 0.25),
}

ENV = {  # world gradient bottom/mid/top, ground colour (None = floating), world strength
    "space":   ((0.01, 0.01, 0.05), (0.06, 0.02, 0.18), (0.10, 0.05, 0.35), None, 0.6),
    "ocean":   ((0.00, 0.12, 0.30), (0.00, 0.35, 0.60), (0.20, 0.75, 0.90), (0.85, 0.72, 0.45), 0.75),
    "lab":     ((0.55, 0.70, 0.95), (0.60, 0.75, 1.00), (0.75, 0.85, 1.00), (0.42, 0.50, 0.75), 0.5),
    "city":    ((1.00, 0.55, 0.35), (1.00, 0.70, 0.50), (0.45, 0.55, 0.95), (0.35, 0.40, 0.48), 0.6),
    "ancient": ((1.00, 0.70, 0.35), (1.00, 0.82, 0.55), (0.45, 0.70, 1.00), (0.92, 0.72, 0.42), 0.5),
    "jungle":  ((0.55, 0.85, 0.55), (0.65, 0.90, 0.85), (0.40, 0.75, 1.00), (0.25, 0.60, 0.20), 0.6),
    "arctic":  ((0.80, 0.90, 1.00), (0.65, 0.82, 1.00), (0.35, 0.55, 0.95), (0.92, 0.96, 1.00), 0.45),
    "mind":    ((0.15, 0.03, 0.30), (0.40, 0.10, 0.60), (0.85, 0.40, 0.85), None, 0.8),
    "earth":   ((0.55, 0.80, 1.00), (0.40, 0.65, 1.00), (0.15, 0.35, 0.90), None, 0.65),
    "mystery": ((0.03, 0.03, 0.10), (0.08, 0.06, 0.22), (0.18, 0.12, 0.40), (0.10, 0.08, 0.20), 0.55),
    "desert":  ((1.00, 0.75, 0.40), (1.00, 0.85, 0.60), (0.50, 0.75, 1.00), (0.98, 0.75, 0.40), 0.5),
    "studio":  ((0.10, 0.20, 0.55), (0.20, 0.45, 0.90), (0.50, 0.30, 0.95), (0.15, 0.25, 0.60), 0.8),
}

STATE: dict = {"mats": {}, "font": None, "fps": 24, "frames": 120, "rng": random.Random(0), "style": {}}


# ================================================================ helpers
def coll():
    return bpy.context.scene.collection


def mat(name, color, rough=0.42, metal=0.0, emit=0.0, coat=0.35, alpha=1.0):
    key = (name, tuple(round(c, 3) for c in color), rough, metal, emit, alpha)
    if key in STATE["mats"]:
        return STATE["mats"][key]
    m = bpy.data.materials.new(name)
    try:
        m.use_nodes = True
    except Exception:  # noqa: BLE001 — removed in future Blender
        pass
    p = m.node_tree.nodes.get("Principled BSDF")
    rgba = (*color, 1.0)
    p.inputs["Base Color"].default_value = rgba
    p.inputs["Roughness"].default_value = rough
    p.inputs["Metallic"].default_value = metal
    if "Coat Weight" in p.inputs:
        p.inputs["Coat Weight"].default_value = coat
    if emit > 0:
        p.inputs["Emission Color"].default_value = rgba
        p.inputs["Emission Strength"].default_value = emit
    if alpha < 1.0:
        p.inputs["Alpha"].default_value = alpha
        try:
            m.surface_render_method = "BLENDED"
        except Exception:  # noqa: BLE001
            pass
    m.diffuse_color = rgba
    STATE["mats"][key] = m
    return m


def _finish(obj, m, smooth=True, parent=None, bevel=0.0):
    if m is not None:
        obj.data.materials.clear()
        obj.data.materials.append(m)
    if smooth and hasattr(obj.data, "polygons"):
        for poly in obj.data.polygons:
            poly.use_smooth = True
    if bevel > 0:
        mod = obj.modifiers.new("bevel", "BEVEL")
        mod.width = bevel
        mod.segments = 3
        mod.limit_method = "ANGLE"
    if parent is not None:
        obj.parent = parent
    return obj


def _bake_scale(o, scale):
    """Bake scale into the mesh so children are not distorted by the parent."""
    from mathutils import Matrix  # type: ignore
    sx, sy, sz = scale
    o.data.transform(Matrix.Diagonal((sx, sy, sz, 1.0)))
    o.scale = (1.0, 1.0, 1.0)


def sphere(loc, scale, m, parent=None, segs=32, rings=16, bake=True):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=segs, ring_count=rings, radius=1.0, location=loc)
    o = bpy.context.active_object
    sc = tuple(scale) if isinstance(scale, (tuple, list)) else (scale, scale, scale)
    if bake:
        _bake_scale(o, sc)
    else:
        o.scale = sc
    return _finish(o, m, parent=parent)


def ico(loc, r, m, parent=None, sub=1):
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=sub, radius=r, location=loc)
    return _finish(bpy.context.active_object, m, smooth=sub > 1, parent=parent)


def cube(loc, scale, m, parent=None, bevel=0.06, rot=(0, 0, 0), bake=True):
    bpy.ops.mesh.primitive_cube_add(size=2.0, location=loc, rotation=rot)
    o = bpy.context.active_object
    if bake:
        _bake_scale(o, tuple(scale))
        bevel = min(bevel, 0.45 * min(scale))
    else:
        o.scale = scale
    return _finish(o, m, smooth=False, parent=parent, bevel=bevel)


def cyl(loc, r, depth, m, parent=None, rot=(0, 0, 0), verts=32, bevel=0.0):
    bpy.ops.mesh.primitive_cylinder_add(vertices=verts, radius=r, depth=depth, location=loc, rotation=rot)
    return _finish(bpy.context.active_object, m, parent=parent, bevel=bevel)


def cone(loc, r1, r2, depth, m, parent=None, rot=(0, 0, 0), verts=32, smooth=True):
    bpy.ops.mesh.primitive_cone_add(vertices=verts, radius1=r1, radius2=r2, depth=depth, location=loc, rotation=rot)
    return _finish(bpy.context.active_object, m, smooth=smooth, parent=parent)


def torus(loc, R, r, m, parent=None, rot=(0, 0, 0)):
    bpy.ops.mesh.primitive_torus_add(major_radius=R, minor_radius=r, location=loc, rotation=rot,
                                     major_segments=48, minor_segments=12)
    return _finish(bpy.context.active_object, m, parent=parent)


def empty(name, loc=(0, 0, 0), parent=None):
    o = bpy.data.objects.new(name, None)
    o.location = loc
    coll().objects.link(o)
    if parent is not None:
        o.parent = parent
    return o


def poly_prism(points2d, depth, m, loc=(0, 0, 0), parent=None, rot=(90 * D2R, 0, 0)):
    """Extrude a 2D outline (x,z plane facing camera) into a prism."""
    import bmesh  # type: ignore
    me = bpy.data.meshes.new("prism")
    bm = bmesh.new()
    front = [bm.verts.new((x, y, -depth / 2)) for x, y in points2d]
    back = [bm.verts.new((x, y, depth / 2)) for x, y in points2d]
    bm.faces.new(front)
    bm.faces.new(list(reversed(back)))
    n = len(points2d)
    for i in range(n):
        j = (i + 1) % n
        bm.faces.new((front[i], front[j], back[j], back[i]))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(me)
    bm.free()
    o = bpy.data.objects.new("prism", me)
    coll().objects.link(o)
    o.location = loc
    o.rotation_euler = rot
    return _finish(o, m, smooth=False, parent=parent, bevel=0.02)


def text_obj(body, size, m, loc=(0, 0, 0), parent=None, extrude=0.08, outline_mat=None):
    cu = bpy.data.curves.new("txt", "FONT")
    cu.body = body
    cu.align_x, cu.align_y = "CENTER", "CENTER"
    cu.size = size
    cu.extrude = extrude
    cu.bevel_depth = 0.015
    if STATE["font"]:
        cu.font = STATE["font"]
    o = bpy.data.objects.new("txt", cu)
    coll().objects.link(o)
    o.location = loc
    o.rotation_euler = (90 * D2R, 0, 0)
    o.data.materials.append(m)
    if parent is not None:
        o.parent = parent
    if outline_mat is not None:
        cu2 = cu.copy()
        cu2.offset = 0.045
        cu2.extrude = extrude * 0.6
        o2 = bpy.data.objects.new("txt_outline", cu2)
        coll().objects.link(o2)
        o2.location = (0, extrude * 1.2, 0)
        o2.rotation_euler = (0, 0, 0)
        o2.parent = o
        # parent space of a text rotated 90° on X: +Y local points away from camera
        o2.location = (0, 0, -extrude * 1.4)
        o2.data.materials.clear()
        o2.data.materials.append(outline_mat)
    return o


# ------------------------------------------------------------- animation
def key(obj, path, frame, value, interp="BEZIER"):
    prefs = bpy.context.preferences.edit
    old = prefs.keyframe_new_interpolation_type
    prefs.keyframe_new_interpolation_type = interp
    setattr(obj, path, value)
    obj.keyframe_insert(data_path=path, frame=frame)
    prefs.keyframe_new_interpolation_type = old


def sec(t):
    return max(1, int(round(t * STATE["fps"])) + 1)


def pop_in(obj, start, final_scale=None, dur=0.45):
    s = tuple(final_scale or obj.scale)
    key(obj, "scale", sec(start), (0.001, 0.001, 0.001), "BACK")
    key(obj, "scale", sec(start + dur), s)


def bob(obj, amp=0.08, period=2.0, phase=0.0, axis=2):
    base = list(obj.location)
    t, end = 0.0, STATE["frames"] / STATE["fps"] + period
    while t <= end:
        loc = list(base)
        loc[axis] = base[axis] + amp * math.sin(2 * math.pi * (t / period) + phase)
        key(obj, "location", sec(t), tuple(loc), "SINE")
        t += period / 4


def spin(obj, degrees_per_sec=20.0, axis=2):
    rot0 = list(obj.rotation_euler)
    dur = STATE["frames"] / STATE["fps"]
    key(obj, "rotation_euler", 1, tuple(rot0), "LINEAR")
    rot1 = list(rot0)
    rot1[axis] += degrees_per_sec * dur * D2R
    key(obj, "rotation_euler", STATE["frames"] + 1, tuple(rot1), "LINEAR")


# ================================================================ props
def p_planet(r):
    c = r.choice(["orange", "purple", "teal", "pink", "sky"])
    sphere((0, 0, 0), 0.85, mat("planet", PAL[c]), parent=None)
    root = bpy.context.active_object
    ring = torus((0, 0, 0), 1.35, 0.06, mat("ring", PAL["yellow"], emit=0.3), parent=root, rot=(70 * D2R, 0, 20 * D2R))
    ring.scale = (1.0, 1.0, 0.25)
    for i in range(3):
        a = r.uniform(0, 6.28)
        sphere((0.55 * math.cos(a), -0.6, 0.55 * math.sin(a)), 0.16, mat("spot", tuple(x * 0.8 for x in PAL[c])), parent=root)
    return root


def p_moon(r):
    root = sphere((0, 0, 0), 0.8, mat("moon", (0.78, 0.78, 0.82), rough=0.8))
    for _ in range(6):
        a, b = r.uniform(-1.2, 1.2), r.uniform(-1.0, 1.0)
        sphere((0.65 * math.sin(a), -0.72 * math.cos(a) * math.cos(b), 0.65 * math.sin(b)), r.uniform(0.1, 0.2),
               mat("crater", (0.55, 0.55, 0.6), rough=0.9), parent=root)
    return root


def p_sun(r):
    root = sphere((0, 0, 0), 0.8, mat("sun", PAL["orange"], emit=4.0))
    for i in range(12):
        a = i * 30 * D2R
        cone((1.05 * math.cos(a), 0, 1.05 * math.sin(a)), 0.15, 0.0, 0.4, mat("ray", PAL["yellow"], emit=3.0),
             parent=root, rot=(0, -a + 90 * D2R, 0))
    return root


def p_star(r):
    pts = []
    for i in range(10):
        rad = 0.95 if i % 2 == 0 else 0.42
        a = math.pi / 2 + i * math.pi / 5
        pts.append((rad * math.cos(a), rad * math.sin(a)))
    return poly_prism(pts, 0.3, mat("star", PAL["yellow"], emit=1.5))


def p_rocket(r):
    root = cyl((0, 0, 0), 0.35, 1.4, mat("rocket", PAL["white"]))
    cone((0, 0, 1.05), 0.35, 0.0, 0.7, mat("nose", PAL["red"]), parent=root)
    for i in range(3):
        a = i * 120 * D2R
        cube((0.42 * math.cos(a), 0.42 * math.sin(a), -0.55), (0.18, 0.04, 0.25), mat("fin", PAL["red"]), parent=root,
             rot=(0, 0, a))
    torus((0, -0.33, 0.25), 0.15, 0.04, mat("metal", PAL["grey"], metal=0.8, rough=0.3), parent=root, rot=(90 * D2R, 0, 0))
    sphere((0, -0.32, 0.25), (0.13, 0.05, 0.13), mat("glass", PAL["sky"], emit=0.6), parent=root)
    flame = cone((0, 0, -0.95), 0.25, 0.0, 0.6, mat("flame", PAL["orange"], emit=6.0), parent=root, rot=(180 * D2R, 0, 0))
    key(flame, "scale", 1, (1, 1, 1))
    for i in range(1, 40):
        key(flame, "scale", sec(i * 0.12), (1, 1, 0.8 + 0.4 * (i % 2)), "SINE")
    root.rotation_euler = (0, 20 * D2R, 0)
    return root


def p_satellite(r):
    root = cube((0, 0, 0), (0.3, 0.3, 0.35), mat("gold", PAL["gold"], metal=0.9, rough=0.25))
    for s in (-1, 1):
        cube((s * 0.95, 0, 0), (0.55, 0.02, 0.28), mat("panel", PAL["blue"], metal=0.5, rough=0.2), parent=root)
    cone((0, 0, 0.55), 0.3, 0.05, 0.25, mat("dish", PAL["white"]), parent=root, rot=(180 * D2R, 0, 0))
    return root


def p_telescope(r):
    root = empty("telescope")
    cyl((0, 0, 0.5), 0.18, 1.6, mat("tube", PAL["navy"], metal=0.3), parent=root, rot=(0, 55 * D2R, 0))
    cyl((0.62, 0, 0.95), 0.24, 0.12, mat("rim", PAL["gold"], metal=0.8), parent=root, rot=(0, 55 * D2R, 0))
    for i in range(3):
        a = i * 120 * D2R
        cyl((0.3 * math.cos(a), 0.3 * math.sin(a), -0.3), 0.04, 1.1, mat("leg", PAL["grey"]), parent=root,
            rot=(0.3 * math.sin(a), -0.3 * math.cos(a), 0))
    return root


def p_atom(r):
    root = empty("atom")
    for i in range(7):
        sphere((r.uniform(-.18, .18), r.uniform(-.18, .18), r.uniform(-.18, .18)), 0.2,
               mat("nuc", PAL["red"] if i % 2 else PAL["blue"]), parent=root)
    for k, rot in enumerate([(90 * D2R, 0, 0), (90 * D2R, 60 * D2R, 0), (90 * D2R, -60 * D2R, 0)]):
        orb = empty("orbit", parent=root)
        orb.rotation_euler = rot
        torus((0, 0, 0), 1.0, 0.02, mat("orbitline", PAL["sky"], emit=2.0), parent=orb)
        e = sphere((1.0, 0, 0), 0.09, mat("electron", PAL["yellow"], emit=4.0), parent=orb)
        piv = empty("piv", parent=orb)
        e.parent = piv
        spin(piv, 160 + 40 * k)
    return root


def p_dna(r):
    root = empty("dna")
    for i in range(16):
        z = -1.0 + i * 0.13
        a = i * 0.6
        x, y = 0.45 * math.cos(a), 0.45 * math.sin(a)
        sphere((x, y, z), 0.09, mat("dnaA", PAL["pink"]), parent=root)
        sphere((-x, -y, z), 0.09, mat("dnaB", PAL["teal"]), parent=root)
        c = cyl((0, 0, z), 0.025, 0.9, mat("bond", PAL["yellow"]), parent=root, rot=(0, 90 * D2R, a))
    spin(root, 40)
    return root


def p_brain(r):
    root = empty("brain")
    m = mat("brain", (1.0, 0.55, 0.70), rough=0.5)
    for s in (-1, 1):
        for _ in range(9):
            sphere((s * r.uniform(0.15, 0.55), r.uniform(-0.35, 0.35), r.uniform(-0.25, 0.35)),
                   r.uniform(0.25, 0.36), m, parent=root)
    sphere((0, 0.1, -0.4), (0.15, 0.15, 0.3), mat("stem", (0.95, 0.45, 0.6)), parent=root)
    return root


def p_heart(r):
    root = empty("heart")
    m = mat("heart", PAL["red"], rough=0.25, coat=0.8)
    sphere((-0.34, 0, 0.25), 0.5, m, parent=root)
    sphere((0.34, 0, 0.25), 0.5, m, parent=root)
    cone((0, 0, -0.35), 0.78, 0.0, 1.05, m, parent=root, rot=(180 * D2R, 0, 0))
    key(root, "scale", 1, (1, 1, 1))
    t = 0.0
    while t < STATE["frames"] / STATE["fps"]:
        key(root, "scale", sec(t + 0.1), (1.12, 1.12, 1.12), "SINE")
        key(root, "scale", sec(t + 0.3), (1, 1, 1), "SINE")
        t += 0.9
    return root


def p_globe(r):
    root = sphere((0, 0, 0), 0.9, mat("ocean", PAL["blue"], rough=0.3))
    for _ in range(9):
        a, b = r.uniform(0, 6.28), r.uniform(-1.1, 1.1)
        sphere((0.82 * math.cos(a) * math.cos(b), 0.82 * math.sin(a) * math.cos(b), 0.82 * math.sin(b)),
               (r.uniform(.2, .35), r.uniform(.2, .35), r.uniform(.12, .25)), mat("land", PAL["green"]), parent=root)
    spin(root, 25)
    return root


def p_pyramid(r):
    root = cone((0, 0, 0), 1.1, 0.0, 1.4, mat("pyr", PAL["sand"], rough=0.8), verts=4, smooth=False)
    root.rotation_euler = (0, 0, 45 * D2R)
    return root


def p_column(r):
    root = cyl((0, 0, 0), 0.3, 2.0, mat("marble", (0.92, 0.9, 0.85), rough=0.6), verts=16)
    cube((0, 0, 1.05), (0.42, 0.42, 0.1), mat("marble", (0.92, 0.9, 0.85), rough=0.6), parent=root)
    cube((0, 0, -1.05), (0.42, 0.42, 0.1), mat("marble", (0.92, 0.9, 0.85), rough=0.6), parent=root)
    return root


def p_tree(r):
    root = cyl((0, 0, -0.5), 0.15, 1.2, mat("bark", PAL["brown"], rough=0.8))
    g = mat("leaf", PAL["green"] if r.random() < 0.6 else PAL["lime"], rough=0.6)
    for x, z, s in ((0, 0.9, 0.65), (-0.4, 0.55, 0.45), (0.4, 0.6, 0.48)):
        sphere((x, 0, z), s, g, parent=root)
    return root


def p_mountain(r):
    root = cone((0, 0, 0), 1.2, 0.0, 1.8, mat("rock", PAL["grey"], rough=0.9), verts=6, smooth=False)
    cone((0, 0, 0.6), 0.42, 0.0, 0.62, mat("snow", PAL["white"], rough=0.7), parent=root, verts=6, smooth=False)
    return root


def p_volcano(r):
    root = cone((0, 0, 0), 1.2, 0.35, 1.3, mat("vrock", (0.35, 0.22, 0.15), rough=0.9), verts=12)
    cyl((0, 0, 0.66), 0.33, 0.04, mat("lava", PAL["orange"], emit=6.0), parent=root)
    for i in range(5):
        b = sphere((r.uniform(-.2, .2), r.uniform(-.2, .2), 0.8), 0.1, mat("lava", PAL["orange"], emit=6.0), parent=root)
        start = r.uniform(0, 1.5)
        key(b, "location", sec(start), (b.location.x, b.location.y, 0.7))
        key(b, "location", sec(start + 1.2), (b.location.x * 4, b.location.y * 4, 1.6), "QUAD")
    return root


def p_fish(r):
    c = PAL[r.choice(["orange", "yellow", "pink", "teal"])]
    root = sphere((0, 0, 0), (0.75, 0.32, 0.48), mat("fish", c))
    cone((-0.9, 0, 0), 0.4, 0.0, 0.5, mat("fin", PAL["orange"]), parent=root, rot=(0, 90 * D2R, 0), verts=3)
    sphere((0.45, -0.24, 0.12), 0.1, mat("eyew", PAL["white"]), parent=root)
    sphere((0.48, -0.31, 0.12), 0.05, mat("pupil", PAL["ink"]), parent=root)
    bob(root, 0.12, 1.6)
    return root


def p_octopus(r):
    root = empty("octopus")
    m = mat("octo", (0.70, 0.25, 0.85), rough=0.35, coat=0.6)
    sphere((0, 0, 0.45), (0.62, 0.58, 0.72), m, parent=root)
    for s in (-1, 1):
        sphere((s * 0.24, -0.5, 0.45), 0.15, mat("eyew", PAL["white"]), parent=root)
        sphere((s * 0.24, -0.63, 0.45), 0.07, mat("pupil", PAL["ink"]), parent=root)
    for i in range(8):
        a = i * 45 * D2R + 22 * D2R
        arm = empty("arm", (0.35 * math.cos(a), 0.35 * math.sin(a), 0.0), parent=root)
        arm.rotation_euler = (0, 0, a)
        for k in range(6):
            rr = 0.15 * (1 - k / 7)
            sphere((0.12 + k * 0.17, 0, -0.08 - 0.06 * k + 0.012 * k * k), rr, m, parent=arm, segs=16, rings=8)
        key(arm, "rotation_euler", 1, (0, 0, a), "SINE")
        t = 0.0
        while t < STATE["frames"] / STATE["fps"]:
            key(arm, "rotation_euler", sec(t + 0.6), (0.18 * math.sin(i), 0.15, a), "SINE")
            key(arm, "rotation_euler", sec(t + 1.2), (0, -0.1, a), "SINE")
            t += 1.2
    return root


def p_bird(r):
    c = PAL[r.choice(["sky", "red", "yellow", "lime"])]
    root = sphere((0, 0, 0), (0.55, 0.45, 0.45), mat("bird", c))
    sphere((0.45, 0, 0.35), 0.32, mat("bird", c), parent=root)
    cone((0.82, 0, 0.32), 0.1, 0.0, 0.3, mat("beak", PAL["orange"]), parent=root, rot=(0, 90 * D2R, 0))
    sphere((0.55, -0.27, 0.45), 0.07, mat("pupil", PAL["ink"]), parent=root)
    for s in (-1, 1):
        w = sphere((0, s * 0.45, 0.15), (0.4, 0.08, 0.22), mat("wing", tuple(x * 0.8 for x in c)), parent=root)
        for i in range(30):
            key(w, "rotation_euler", sec(i * 0.15), (s * (0.5 if i % 2 else -0.3), 0, 0), "SINE")
    bob(root, 0.15, 1.2)
    return root


def p_creature(r):
    c = PAL[r.choice(["orange", "teal", "pink", "lime", "sand"])]
    root = sphere((0, 0, 0), (0.7, 0.6, 0.62), mat("fur", c, rough=0.7, coat=0.0))
    for s in (-1, 1):
        sphere((s * 0.42, 0, 0.55), (0.18, 0.12, 0.28), mat("fur", c, rough=0.7, coat=0.0), parent=root)
        sphere((s * 0.25, -0.5, 0.15), 0.15, mat("eyew", PAL["white"]), parent=root)
        sphere((s * 0.25, -0.62, 0.15), 0.08, mat("pupil", PAL["ink"]), parent=root)
    sphere((0, -0.6, -0.05), 0.08, mat("nose", PAL["ink"]), parent=root)
    return root


def p_book(r):
    root = empty("book")
    cover = mat("cover", PAL[r.choice(["red", "blue", "purple"])])
    for s in (-1, 1):
        cube((s * 0.5, 0, 0), (0.5, 0.06, 0.7), cover, parent=root, rot=(0, 0, s * 12 * D2R))
        cube((s * 0.47, -0.05, 0.01), (0.46, 0.05, 0.66), mat("page", PAL["white"], rough=0.9), parent=root,
             rot=(0, 0, s * 12 * D2R))
    root.rotation_euler = (-25 * D2R, 0, 0)
    return root


def p_lightbulb(r):
    root = sphere((0, 0, 0.25), 0.62, mat("bulb", PAL["yellow"], emit=5.0, rough=0.1))
    cyl((0, 0, -0.5), 0.28, 0.45, mat("base", PAL["grey"], metal=0.8, rough=0.3), parent=root)
    for i in range(3):
        torus((0, 0, -0.38 - i * 0.12), 0.29, 0.03, mat("base", PAL["grey"], metal=0.8, rough=0.3), parent=root)
    return root


def p_text_symbol(sym, color):
    def f(r):
        o = text_obj(sym, 2.4, mat(f"sym{sym}", color, emit=1.2), extrude=0.25, outline_mat=mat("outline", PAL["ink"]))
        return o
    return f


def p_magnifier(r):
    root = torus((0, 0, 0.25), 0.55, 0.09, mat("rim", PAL["gold"], metal=0.8, rough=0.25), rot=(90 * D2R, 0, 0))
    cyl((0, 0, 0), 0.5, 0.04, mat("lens", PAL["ice"], emit=0.4, alpha=0.6), parent=root)
    cyl((0.55, 0, -0.9), 0.08, 0.9, mat("handle", PAL["brown"]), parent=root, rot=(90 * D2R, 0, 0))
    root.rotation_euler = (90 * D2R, -35 * D2R, 0)
    return root


def p_clock(r):
    root = cyl((0, 0, 0), 0.85, 0.15, mat("face", PAL["white"]), rot=(90 * D2R, 0, 0))
    torus((0, 0, 0), 0.88, 0.07, mat("rimc", PAL["red"]), parent=root)
    for i in range(12):
        a = i * 30 * D2R
        cube((0.7 * math.cos(a), 0.7 * math.sin(a), 0.09), (0.03, 0.06, 0.02), mat("tick", PAL["ink"]), parent=root,
             rot=(0, 0, a))
    for length, speed, w in ((0.55, 360, 0.03), (0.38, 30, 0.05)):
        piv = empty("hand", (0, 0, 0.1), parent=root)
        cube((0, length / 2, 0), (w, length / 2, 0.02), mat("tick", PAL["ink"]), parent=piv)
        spin(piv, -speed)
    return root


def p_crystal(r):
    root = empty("crystal")
    for i, (x, h) in enumerate(((0, 1.4), (-0.45, 0.9), (0.42, 1.0))):
        m = mat("cryst", PAL["teal"] if i != 1 else PAL["purple"], emit=1.5, rough=0.1)
        c = cone((x, 0, -0.3 + h / 2), 0.25, 0.0, h, m, parent=root, verts=6, smooth=False)
        c.rotation_euler = (0, x * 0.5, 0)
    return root


def p_beaker(r):
    root = cyl((0, 0, 0), 0.5, 1.2, mat("glass", PAL["ice"], rough=0.05, alpha=0.4))
    liq = cyl((0, 0, -0.2), 0.46, 0.75, mat("liquid", PAL[r.choice(["lime", "pink", "teal"])], emit=2.0), parent=root)
    torus((0, 0, 0.6), 0.5, 0.04, mat("glass", PAL["ice"], rough=0.05, alpha=0.4), parent=root)
    for i in range(4):
        b = sphere((r.uniform(-.25, .25), r.uniform(-.25, .25), 0.0), 0.07, mat("bub", PAL["white"], emit=1.0), parent=root)
        st = r.uniform(0, 1)
        for k in range(12):
            key(b, "location", sec(st + k * 1.0), (b.location.x, b.location.y, -0.5), "LINEAR")
            key(b, "location", sec(st + k * 1.0 + 0.95), (b.location.x, b.location.y, 0.2), "LINEAR")
    return root


def p_coin(r):
    root = cyl((0, 0, 0), 0.8, 0.14, mat("coin", PAL["gold"], metal=1.0, rough=0.2), rot=(90 * D2R, 0, 0))
    torus((0, 0, 0), 0.68, 0.04, mat("coin", PAL["gold"], metal=1.0, rough=0.2), parent=root)
    spin(root, 90)
    return root


def p_skull(r):
    root = sphere((0, 0, 0.2), (0.7, 0.65, 0.65), mat("skull", (0.95, 0.93, 0.85), rough=0.6))
    cube((0, -0.1, -0.45), (0.42, 0.4, 0.2), mat("skull", (0.95, 0.93, 0.85), rough=0.6), parent=root, bevel=0.1)
    for s in (-1, 1):
        sphere((s * 0.26, -0.55, 0.15), 0.17, mat("socket", PAL["ink"]), parent=root)
    return root


def p_bone(r):
    root = cyl((0, 0, 0), 0.14, 1.5, mat("bone", (0.95, 0.92, 0.8), rough=0.6), rot=(0, 90 * D2R, 0))
    for x in (-0.75, 0.75):
        for y in (-0.13, 0.13):
            sphere((y, 0, x), 0.22, mat("bone", (0.95, 0.92, 0.8), rough=0.6), parent=root)
    root.rotation_euler = (0, 70 * D2R, 0)
    return root


def p_chart(r):
    root = empty("chart")
    cols = ["teal", "yellow", "orange", "pink"]
    for i in range(4):
        h = 0.4 + i * 0.4
        bar = cube((-0.75 + i * 0.5, 0, -0.8), (0.18, 0.18, 1.0), mat(f"bar{i}", PAL[cols[i]]), parent=root,
                   bake=False)
        bar.scale = (0.18, 0.18, 0.001)
        key(bar, "scale", sec(0.3 + i * 0.25), (0.18, 0.18, 0.001), "BACK")
        key(bar, "scale", sec(0.9 + i * 0.25), (0.18, 0.18, h / 2))
        key(bar, "location", sec(0.3 + i * 0.25), (-0.75 + i * 0.5, 0, -0.8), "BACK")
        key(bar, "location", sec(0.9 + i * 0.25), (-0.75 + i * 0.5, 0, -0.8 + h / 2))
    return root


def p_robot(r):
    root = cube((0, 0, 0), (0.45, 0.4, 0.5), mat("rob", PAL["grey"], metal=0.6, rough=0.3), bevel=0.1)
    for s in (-1, 1):
        sphere((s * 0.18, -0.4, 0.15), 0.1, mat("roeye", PAL["red"], emit=4), parent=root)
    cyl((0, 0, 0.65), 0.03, 0.3, mat("rob", PAL["grey"], metal=0.6, rough=0.3), parent=root)
    sphere((0, 0, 0.82), 0.07, mat("roeye", PAL["red"], emit=4), parent=root)
    return root


def p_ice(r):
    return cube((0, 0, 0), (0.7, 0.7, 0.7), mat("iceb", PAL["ice"], rough=0.1, emit=0.25), bevel=0.12,
                rot=(0.2, 0.3, 0.4))


def p_drop(r):
    root = sphere((0, 0, -0.2), 0.6, mat("water", PAL["sky"], rough=0.05, coat=1.0))
    cone((0, 0, 0.45), 0.52, 0.0, 0.9, mat("water", PAL["sky"], rough=0.05, coat=1.0), parent=root)
    return root


def p_lightning(r):
    pts = [(-0.2, 1.0), (0.45, 1.0), (0.1, 0.25), (0.5, 0.25), (-0.35, -1.0), (-0.05, -0.1), (-0.45, -0.1)]
    return poly_prism(pts, 0.25, mat("bolt", PAL["yellow"], emit=3.0))


def p_eye(r):
    root = sphere((0, 0, 0), 0.8, mat("eyeball", PAL["white"], rough=0.2))
    sphere((0, -0.62, 0), (0.42, 0.25, 0.42), mat("iris", PAL[r.choice(["blue", "green", "brown"])]), parent=root)
    sphere((0, -0.75, 0), (0.2, 0.15, 0.2), mat("pupil", PAL["ink"]), parent=root)
    return root


def p_map(r):
    root = empty("map")
    for i in range(3):
        cube((-0.6 + i * 0.6, 0, 0), (0.3, 0.02, 0.6), mat("paper", PAL["sand"], rough=0.9), parent=root,
             rot=(0, 0, (15 if i % 2 else -15) * D2R))
        sphere((-0.6 + i * 0.6, -0.04, 0.1), (0.15, 0.02, 0.2), mat("land", PAL["green"]), parent=root)
    cone((0.1, -0.12, 0.2), 0.08, 0.0, 0.25, mat("pin", PAL["red"]), parent=root, rot=(180 * D2R, 0, 0))
    return root


def p_chip(r):
    root = cube((0, 0, 0), (0.7, 0.7, 0.08), mat("pcb", (0.05, 0.35, 0.2)), rot=(90 * D2R, 0, 0))
    cube((0, 0, -0.1), (0.35, 0.35, 0.05), mat("die", PAL["ink"], metal=0.4), parent=root)
    for i in range(6):
        for s in (-1, 1):
            cube((-0.5 + i * 0.2, s * 0.78, 0), (0.04, 0.1, 0.03), mat("pin", PAL["gold"], metal=1.0), parent=root)
    return root


def p_phone(r):
    root = cube((0, 0, 0), (0.5, 0.06, 0.95), mat("phone", PAL["ink"], rough=0.2), bevel=0.12)
    cube((0, -0.05, 0), (0.44, 0.02, 0.86), mat("screen", PAL["sky"], emit=0.7), parent=root, bevel=0.06)
    return root


PROP_BUILDERS = {
    "planet": p_planet, "moon": p_moon, "sun": p_sun, "star": p_star, "rocket": p_rocket, "satellite": p_satellite,
    "telescope": p_telescope, "atom": p_atom, "dna": p_dna, "brain": p_brain, "heart": p_heart, "globe": p_globe,
    "pyramid": p_pyramid, "column": p_column, "tree": p_tree, "mountain": p_mountain, "volcano": p_volcano,
    "fish": p_fish, "octopus": p_octopus, "bird": p_bird, "creature": p_creature, "book": p_book,
    "lightbulb": p_lightbulb, "question": p_text_symbol("?", PAL["yellow"]),
    "exclamation": p_text_symbol("!", PAL["orange"]), "magnifier": p_magnifier, "clock": p_clock,
    "crystal": p_crystal, "beaker": p_beaker, "coin": p_coin, "skull": p_skull, "bone": p_bone, "chart": p_chart,
    "robot": p_robot, "ice": p_ice, "drop": p_drop, "lightning": p_lightning, "eye": p_eye, "map": p_map,
    "chip": p_chip, "phone": p_phone,
}


# ================================================================ mascot
def build_mascot(action, loc, style):
    """Gyaanu: the channel's recurring robot host (identical in every video)."""
    body_c = tuple(style.get("mascot_color", PAL["teal"]))
    accent = tuple(style.get("accent_color", PAL["yellow"]))
    root = empty("mascot", loc)
    rig = empty("mascot_rig", (0, 0, 0), parent=root)
    body = sphere((0, 0, 0.95), (0.62, 0.55, 0.72), mat("m_body", body_c, rough=0.3, coat=0.8), parent=rig)
    sphere((0, -0.38, 1.05), (0.46, 0.2, 0.36), mat("m_face", PAL["navy"], rough=0.15, coat=1.0), parent=rig)
    eyes = []
    for s in (-1, 1):
        e = sphere((s * 0.17, -0.56, 1.1), (0.1, 0.05, 0.14), mat("m_eye", (0.7, 1.0, 1.0), emit=5.0), parent=rig,
                   bake=False)
        eyes.append(e)
    smile = torus((0, -0.56, 0.93), 0.11, 0.022, mat("m_eye", (0.7, 1.0, 1.0), emit=5.0), parent=rig,
                  rot=(90 * D2R, 0, 0))
    smile.scale = (1.0, 0.6, 1.0)
    sphere((0, -0.5, 0.55), (0.2, 0.08, 0.13), mat("m_badge", accent, emit=0.5), parent=rig)
    cyl((0, 0, 1.75), 0.025, 0.25, mat("m_metal", PAL["grey"], metal=0.8, rough=0.3), parent=rig)
    bulb = sphere((0, 0, 1.92), 0.09, mat("m_bulb", accent, emit=6.0), parent=rig, bake=False)
    arms = []
    for s in (-1, 1):
        piv = empty("arm_piv", (s * 0.6, 0, 1.0), parent=rig)
        sphere((s * 0.12, 0, -0.18), (0.11, 0.11, 0.24), mat("m_body", body_c, rough=0.3, coat=0.8), parent=piv)
        piv.rotation_euler = (0, s * -15 * D2R, 0)
        arms.append(piv)
    for s in (-1, 1):
        sphere((s * 0.25, -0.05, 0.12), (0.17, 0.22, 0.1), mat("m_feet", PAL["navy"]), parent=rig)

    fps, frames = STATE["fps"], STATE["frames"]
    dur = frames / fps
    # blink every ~2.7s
    t = 0.8
    while t < dur:
        for e in eyes:
            key(e, "scale", sec(t), (0.1, 0.05, 0.14))
            key(e, "scale", sec(t + 0.06), (0.1, 0.05, 0.015))
            key(e, "scale", sec(t + 0.13), (0.1, 0.05, 0.14))
        t += 2.7
    # antenna glow pulse
    for i in range(int(dur / 0.5) + 1):
        k = 0.09 if i % 2 else 0.12
        key(bulb, "scale", sec(i * 0.5), (k, k, k), "SINE")
    pop_in(root, 0.05, (0.85, 0.85, 0.85), 0.4)
    L, R = arms[0], arms[1]

    if action == "wave":
        bob(rig, 0.04, 1.4)
        for i in range(int(dur / 0.3)):
            key(R, "rotation_euler", sec(i * 0.3), (0, (-150 if i % 2 else -120) * D2R, 0), "SINE")
    elif action == "surprised":
        key(rig, "location", sec(0.3), (0, 0, 0))
        key(rig, "location", sec(0.55), (0, 0.25, 0.45), "QUAD")
        key(rig, "location", sec(0.85), (0, 0.2, 0), "BOUNCE")
        for e in eyes:
            key(e, "scale", sec(0.5), (0.14, 0.05, 0.2))
        for s, a in ((1, L), (-1, R)):
            key(a, "rotation_euler", sec(0.4), (0, s * 140 * D2R, 0), "BACK")
        bob(rig, 0.03, 0.4, axis=0)
    elif action == "point":
        key(R, "rotation_euler", sec(0.2), (0, -15 * D2R, 0))
        key(R, "rotation_euler", sec(0.6), (0, -95 * D2R, 0), "BACK")
        key(rig, "rotation_euler", sec(0.2), (0, 0, 0))
        key(rig, "rotation_euler", sec(0.6), (0, 0, 20 * D2R))
        bob(rig, 0.04, 1.6)
    elif action == "think":
        key(rig, "rotation_euler", sec(0.2), (0, 0, 0))
        key(rig, "rotation_euler", sec(0.9), (0, 12 * D2R, -10 * D2R), "SINE")
        key(L, "rotation_euler", sec(0.3), (0, 130 * D2R, 0), "BACK")
        bob(rig, 0.03, 2.2)
    elif action == "celebrate":
        t = 0.2
        while t < dur:
            key(rig, "location", sec(t), (0, 0, 0), "QUAD")
            key(rig, "location", sec(t + 0.3), (0, 0, 0.5), "QUAD")
            t += 0.6
        spin(rig, 220)
        for s, a in ((1, L), (-1, R)):
            key(a, "rotation_euler", sec(0.2), (0, s * 150 * D2R, 0), "BACK")
    elif action == "scared":
        bob(rig, 0.03, 0.15, axis=0)
        for e in eyes:
            key(e, "scale", sec(0.3), (0.13, 0.05, 0.18))
    else:  # idle / explain
        bob(rig, 0.05, 1.5)
        if action == "explain":
            for i in range(int(dur / 0.7)):
                key(R if i % 2 else L, "rotation_euler",
                    sec(i * 0.7), (0, (1 if i % 2 == 0 else -1) * (50 + 25 * (i % 3)) * D2R, 0), "SINE")
    return root


# ============================================================ environment
def world_gradient(bottom, mid, top, strength):
    w = bpy.data.worlds.new("World")
    bpy.context.scene.world = w
    try:
        w.use_nodes = True
    except Exception:  # noqa: BLE001
        pass
    nt = w.node_tree
    nt.nodes.clear()
    tc = nt.nodes.new("ShaderNodeTexCoord")
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    mr = nt.nodes.new("ShaderNodeMapRange")
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    bg = nt.nodes.new("ShaderNodeBackground")
    out = nt.nodes.new("ShaderNodeOutputWorld")
    nt.links.new(tc.outputs["Generated"], sep.inputs[0])
    nt.links.new(sep.outputs["Z"], mr.inputs["Value"])
    mr.inputs["From Min"].default_value = -0.35
    mr.inputs["From Max"].default_value = 0.65
    nt.links.new(mr.outputs["Result"], ramp.inputs["Fac"])
    el = ramp.color_ramp.elements
    el[0].color = (*bottom, 1)
    el[1].color = (*top, 1)
    e_mid = el.new(0.45)
    e_mid.color = (*mid, 1)
    nt.links.new(ramp.outputs["Color"], bg.inputs["Color"])
    bg.inputs["Strength"].default_value = strength
    nt.links.new(bg.outputs[0], out.inputs[0])


def scatter(builder, n, r, xr, yr, zr, scale=(0.6, 1.4)):
    objs = []
    for _ in range(n):
        o = builder(r)
        o.location = (r.uniform(*xr), r.uniform(*yr), r.uniform(*zr))
        s = r.uniform(*scale)
        o.scale = (o.scale[0] * s, o.scale[1] * s, o.scale[2] * s)
        objs.append(o)
    return objs


def stars(r, n=160, emit=6.0):
    m = mat("starpt", (1, 1, 0.95), emit=emit)
    base = ico((0, 30, 0), 0.06, m)
    for _ in range(n):
        o = base.copy()
        o.location = (r.uniform(-14, 14), r.uniform(18, 35), r.uniform(-12, 22))
        s = r.uniform(0.5, 2.2)
        o.scale = (s, s, s)
        coll().objects.link(o)


def floaters(r, kind, n):
    """Ambient particles: bubbles, snow, sparkles, dust."""
    spec = {"bubbles": (PAL["white"], 0.6, 0.05, 1), "snow": (PAL["white"], 0.4, 0.05, -1),
            "sparkles": (PAL["yellow"], 6.0, 0.03, 1), "dust": ((1, 0.9, 0.7), 1.0, 0.025, 1)}[kind]
    col, em, rad, direction = spec
    m = mat(f"fl_{kind}", col, emit=em, alpha=0.8 if kind == "bubbles" else 1.0)
    dur = STATE["frames"] / STATE["fps"]
    for _ in range(n):
        x, y = r.uniform(-3.5, 3.5), r.uniform(-3.0, 4.0)
        z0 = r.uniform(-2.5, 6.0)
        o = ico((x, y, z0), rad * r.uniform(0.6, 1.8), m, sub=2)
        speed = r.uniform(0.25, 0.7) * direction
        key(o, "location", 1, (x, y, z0), "LINEAR")
        key(o, "location", STATE["frames"] + 1, (x + r.uniform(-0.4, 0.4), y, z0 + speed * dur), "LINEAR")


def ground(color, size=90):
    bpy.ops.mesh.primitive_plane_add(size=size, location=(0, 20, 0))
    g = bpy.context.active_object
    return _finish(g, mat("ground", color, rough=0.85, coat=0.0), smooth=False)


def platform(loc, color):
    p = cyl(loc, 0.75, 0.3, mat("platform", color, rough=0.5), bevel=0.08)
    bob(p, 0.05, 2.4)
    return p


def build_environment(name, r):
    bottom, mid, top, gcol, strength = ENV.get(name, ENV["studio"])
    world_gradient(bottom, mid, top, strength)
    has_ground = gcol is not None
    if has_ground:
        ground(gcol)
    if name == "space":
        stars(r)
        for x, z, c, s in ((-4.5, 7, "purple", 1.6), (5, 2, "orange", 1.0)):
            pl = sphere((x, 22, z), s, mat("far_planet", PAL[c], rough=0.6))
            spin(pl, 5)
        floaters(r, "sparkles", 25)
    elif name == "ocean":
        for i in range(14):
            x = r.uniform(-7, 7)
            y = r.uniform(3, 14)
            h = r.uniform(1.5, 3.5)
            sw = cyl((x, y, h / 2), 0.08, h, mat("weed", PAL["green"]), verts=8)
            key(sw, "rotation_euler", 1, (0, -0.08, 0), "SINE")
            for k in range(1, 20):
                key(sw, "rotation_euler", sec(k * 1.3), (0, 0.08 if k % 2 else -0.08, 0), "SINE")
        for _ in range(10):
            sphere((r.uniform(-6, 6), r.uniform(2, 10), 0.2), r.uniform(0.3, 0.7),
                   mat("coral", PAL[r.choice(["pink", "orange", "purple"])], rough=0.7))
        floaters(r, "bubbles", 40)
    elif name == "lab":
        cube((0, 7, 4), (9, 0.2, 4), mat("wall", (0.45, 0.62, 0.92), rough=0.9), bevel=0)
        for z in (2.2, 3.6):
            cube((0, 6.6, z), (4.0, 0.35, 0.06), mat("shelf", (0.95, 0.85, 0.7)), bevel=0.02)
            for i in range(7):
                b = p_beaker(r)
                b.location = (-3.3 + i * 1.1, 6.5, z + 0.38)
                b.scale = (0.28, 0.28, 0.28)
        cube((0, 3, 0.45), (3.5, 1.0, 0.45), mat("table", (0.95, 0.95, 1.0)), bevel=0.05)
        floaters(r, "sparkles", 15)
    elif name == "city":
        cols = ["pink", "sky", "yellow", "teal", "orange", "purple"]
        for i in range(16):
            w, d, h = r.uniform(0.7, 1.4), r.uniform(0.7, 1.4), r.uniform(2.0, 7.0)
            x = r.choice([-1, 1]) * r.uniform(2.0, 8.0)
            y = r.uniform(4, 16)
            cube((x, y, h / 2), (w, d, h / 2), mat("bld", PAL[r.choice(cols)], rough=0.6), bevel=0.08)
            for k in range(int(h)):
                cube((x, y - d - 0.01, 0.6 + k), (w * 0.7, 0.02, 0.15), mat("win", (1, 0.95, 0.6), emit=1.5), bevel=0)
        cube((0, 6, 0.01), (1.4, 14, 0.01), mat("road", (0.2, 0.2, 0.25)), bevel=0)
        floaters(r, "dust", 15)
    elif name == "ancient":
        for x, y, s in ((-5, 14, 3.2), (3.5, 18, 4.2), (9, 22, 2.5)):
            p = p_pyramid(r)
            p.location, p.scale = (x, y, s * 0.7), (s, s, s)
        for x in (-3.2, 3.2):
            c = p_column(r)
            c.location = (x, 4, 1.0)
        sun = p_sun(r)
        sun.location, sun.scale = (4, 30, 10), (2.5, 2.5, 2.5)
        floaters(r, "dust", 20)
    elif name in ("jungle", "desert"):
        if name == "jungle":
            for _ in range(14):
                t = p_tree(r)
                x = r.choice([-1, 1]) * r.uniform(2.2, 8)
                t.location = (x, r.uniform(2, 14), 1.1)
                s = r.uniform(1.2, 2.4)
                t.scale = (s, s, s)
            for x, y, s in ((-8, 30, 7), (6, 34, 9)):
                m = p_mountain(r)
                m.location, m.scale = (x, y, s * 0.85), (s, s, s)
            floaters(r, "sparkles", 15)
        else:
            for _ in range(8):
                sphere((r.uniform(-12, 12), r.uniform(8, 25), -0.5), (r.uniform(3, 6), r.uniform(2, 4), 1.4),
                       mat("dune", (0.98, 0.72, 0.38), rough=0.9))
            for _ in range(6):
                x = r.choice([-1, 1]) * r.uniform(2, 6)
                c = cyl((x, r.uniform(2, 8), 0.8), 0.25, 1.6, mat("cactus", PAL["green"]), verts=12)
                sphere((0, 0, 0.8), 0.25, mat("cactus", PAL["green"]), parent=c)
            sun = p_sun(r)
            sun.location, sun.scale = (-4, 30, 9), (2.5, 2.5, 2.5)
            floaters(r, "dust", 20)
    elif name == "arctic":
        for _ in range(10):
            b = p_ice(r)
            b.location = (r.choice([-1, 1]) * r.uniform(2, 8), r.uniform(2, 12), 0.4)
        for x, y, s in ((-7, 25, 6), (5, 30, 8), (12, 28, 5)):
            m = cone((x, y, s * 0.6), s, 0, s * 1.4, mat("snowmt", (0.9, 0.95, 1.0), rough=0.7), verts=6, smooth=False)
        floaters(r, "snow", 70)
    elif name == "mind":
        neurons = []
        for _ in range(22):
            p = (r.uniform(-6, 6), r.uniform(3, 12), r.uniform(-2, 8))
            neurons.append(p)
            n = sphere(p, r.uniform(0.12, 0.3), mat("neuron", PAL[r.choice(["pink", "sky", "yellow"])], emit=4.0))
            bob(n, 0.2, r.uniform(2, 4), r.uniform(0, 6))
        for i in range(18):
            a, b = r.sample(neurons, 2)
            mid_pt = tuple((a[k] + b[k]) / 2 for k in range(3))
            dx, dy, dz = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
            length = math.sqrt(dx * dx + dy * dy + dz * dz)
            if length > 6:
                continue
            c = cyl(mid_pt, 0.02, length, mat("synapse", (0.9, 0.7, 1.0), emit=2.0), verts=6)
            c.rotation_euler = (0, math.acos(dz / length), math.atan2(dy, dx))
        floaters(r, "sparkles", 30)
    elif name == "earth":
        for _ in range(9):
            cx, cy, cz = r.uniform(-7, 7), r.uniform(5, 18), r.uniform(-1, 8)
            cl = empty("cloud", (cx, cy, cz))
            for k in range(5):
                sphere((k * 0.45 - 0.9, 0, r.uniform(-0.1, 0.25)), r.uniform(0.4, 0.7), mat("cloud", PAL["white"], rough=0.9),
                       parent=cl)
            key(cl, "location", 1, (cx, cy, cz), "LINEAR")
            key(cl, "location", STATE["frames"] + 1, (cx + 1.5, cy, cz), "LINEAR")
        floaters(r, "sparkles", 10)
    elif name == "mystery":
        moon = sphere((-3, 25, 10), 2.2, mat("moonlight", (0.95, 0.95, 0.8), emit=3.0))
        for _ in range(8):
            q = text_obj("?", r.uniform(0.6, 1.2), mat("qglow", PAL["purple"], emit=3.0),
                         loc=(r.uniform(-5, 5), r.uniform(4, 12), r.uniform(1, 7)))
            bob(q, 0.25, r.uniform(2, 4), r.uniform(0, 6))
        for _ in range(6):
            x = r.choice([-1, 1]) * r.uniform(2.5, 7)
            t = cyl((x, r.uniform(3, 10), 1.5), 0.12, 3.0, mat("deadtree", (0.15, 0.1, 0.15)), verts=8)
            cyl((0.4, 0, 0.8), 0.05, 1.2, mat("deadtree", (0.15, 0.1, 0.15)), parent=t, rot=(0, 50 * D2R, 0), verts=6)
        floaters(r, "dust", 25)
    else:  # studio
        cube((0, 9, 5), (12, 0.2, 7), mat("backdrop", (0.25, 0.35, 0.8), rough=0.8), bevel=0)
        floaters(r, "sparkles", 30)
    return has_ground


# ================================================================== camera
def build_camera(move, target_loc, dur):
    tgt = empty("cam_target", target_loc)
    rig = empty("cam_rig", target_loc)
    cam_data = bpy.data.cameras.new("cam")
    cam_data.lens = 23
    cam_data.sensor_fit = "VERTICAL"
    cam_data.sensor_height = 24
    cam = bpy.data.objects.new("cam", cam_data)
    coll().objects.link(cam)
    cam.parent = rig
    bpy.context.scene.camera = cam
    c = cam.constraints.new("TRACK_TO")
    c.target = tgt
    c.track_axis = "TRACK_NEGATIVE_Z"
    c.up_axis = "UP_Y"
    if STATE.get("dof"):
        cam_data.dof.use_dof = True
        cam_data.dof.focus_object = tgt
        cam_data.dof.aperture_fstop = 4.0
    end = STATE["frames"] + 1
    base = (0.0, -10.0, 1.2)
    moves = {
        "push_in": (base, (0.0, -9.0, 1.1), (0, 0, 0), (0, 0, 0)),
        "pull_out": ((0.0, -9.0, 1.1), (0.0, -10.6, 1.4), (0, 0, 0), (0, 0, 0)),
        "orbit_left": (base, base, (0, 0, 10 * D2R), (0, 0, -12 * D2R)),
        "orbit_right": (base, base, (0, 0, -10 * D2R), (0, 0, 12 * D2R)),
        "pan_up": ((0.0, -9.6, 0.2), (0.0, -9.6, 2.0), (0, 0, 0), (0, 0, 0)),
        "pan_down": ((0.0, -9.6, 2.3), (0.0, -9.6, 0.6), (0, 0, 0), (0, 0, 0)),
        "dolly_side": ((-1.2, -9.6, 1.2), (1.2, -9.6, 1.2), (0, 0, 0), (0, 0, 0)),
        "crane_down": ((0.0, -9.0, 5.0), (0.0, -9.6, 1.2), (0, 0, 0), (0, 0, 0)),
        "static": (base, (0.0, -9.7, 1.25), (0, 0, -1.5 * D2R), (0, 0, 1.5 * D2R)),
    }
    p0, p1, r0, r1 = moves.get(move, moves["push_in"])
    key(cam, "location", 1, p0)
    key(cam, "location", end, p1)
    key(rig, "rotation_euler", 1, r0)
    key(rig, "rotation_euler", end, r1)
    return cam


def lights(env):
    warm = (1.0, 0.92, 0.82)
    night = env in ("space", "mystery", "mind")
    bpy.ops.object.light_add(type="SUN", location=(4, -6, 9))
    sun = bpy.context.active_object
    sun.data.energy = 1.6 if night else 2.0
    sun.data.color = (0.85, 0.85, 1.0) if night else warm
    sun.data.angle = 12 * D2R
    sun.rotation_euler = (50 * D2R, 10 * D2R, 35 * D2R)
    bpy.ops.object.light_add(type="AREA", location=(-5, -6, 4))
    fill = bpy.context.active_object
    fill.data.energy = 220
    fill.data.size = 6
    fill.data.color = (0.75, 0.85, 1.0)
    fill.rotation_euler = (70 * D2R, 0, -40 * D2R)
    bpy.ops.object.light_add(type="AREA", location=(3, 5, 5))
    rim = bpy.context.active_object
    rim.data.energy = 450
    rim.data.size = 4
    rim.data.color = (1.0, 0.55, 0.9) if night else (1.0, 0.85, 0.6)
    c = rim.constraints.new("TRACK_TO")
    c.target = bpy.data.objects.get("cam_target")
    c.track_axis = "TRACK_NEGATIVE_Z"
    c.up_axis = "UP_Y"


# =================================================================== render
def configure_render(spec):
    sc = bpy.context.scene
    r = sc.render
    r.resolution_x = int(spec["width"])
    r.resolution_y = int(spec["height"])
    r.resolution_percentage = int(spec.get("resolution_percentage", 100))
    r.fps = STATE["fps"]
    sc.frame_start = 1
    sc.frame_end = STATE["frames"]
    r.film_transparent = False
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGB"
    r.image_settings.compression = 15
    r.filepath = os.path.join(spec["output_dir"], "frame_")
    try:
        # "Standard" keeps cartoon colours saturated (AgX desaturates them)
        sc.view_settings.view_transform = spec.get("view_transform", "Standard")
        for look in spec.get("looks", ["Medium High Contrast", "AgX - Punchy", "Punchy"]):
            try:
                sc.view_settings.look = look
                break
            except TypeError:
                continue
    except TypeError:
        pass
    engine = spec.get("engine", "BLENDER_EEVEE")
    r.engine = engine
    if engine == "CYCLES":
        cy = sc.cycles
        cy.samples = int(spec.get("samples", 24))
        cy.use_denoising = True
        cy.max_bounces = 4
        cy.diffuse_bounces = 2
        cy.glossy_bounces = 2
        cy.transmission_bounces = 4
        cy.use_adaptive_sampling = True
        device = spec.get("cycles_device", "CPU")
        if device != "CPU":
            try:
                prefs = bpy.context.preferences.addons["cycles"].preferences
                prefs.compute_device_type = device
                prefs.get_devices()
                for d in prefs.devices:
                    d.use = True
                cy.device = "GPU"
            except Exception:  # noqa: BLE001
                cy.device = "CPU"
    else:
        ee = sc.eevee
        ee.taa_render_samples = int(spec.get("samples", 32))
        for attr, val in (("use_shadows", True), ("use_raytracing", False), ("use_gtao", True)):
            if hasattr(ee, attr):
                try:
                    setattr(ee, attr, val)
                except Exception:  # noqa: BLE001
                    pass


def build(spec):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    STATE["fps"] = int(spec.get("fps", 24))
    STATE["frames"] = max(2, int(round(float(spec["duration"]) * STATE["fps"])))
    STATE["rng"] = r = random.Random(int(spec.get("seed", 0)))
    STATE["mats"] = {}
    STATE["dof"] = bool(spec.get("dof", True))
    style = spec.get("style", {})
    font_path = spec.get("font_path")
    if font_path and os.path.exists(font_path):
        STATE["font"] = bpy.data.fonts.load(font_path)
    else:
        STATE["font"] = None
    scene = spec["scene"]
    env = scene.get("environment", "studio")
    has_ground = build_environment(env, r)

    hero_center = (0.45, 1.2, 2.45)
    props = scene.get("props") or ["question"]
    slots = {1: [(0.0, 0.0, 0.0)], 2: [(-0.7, 0.3, 0.75), (0.85, -0.2, -0.65)],
             3: [(0.0, 0.3, 1.0), (-1.0, -0.1, -0.55), (1.05, 0.0, -0.6)]}[min(3, len(props))]
    sizes = {1: 1.75, 2: 1.2, 3: 0.95}[min(3, len(props))]
    hero_root = empty("hero", hero_center)
    for i, (name, off) in enumerate(zip(props[:3], slots)):
        builder = PROP_BUILDERS.get(name, PROP_BUILDERS["question"])
        holder = empty(f"slot_{i}", off, parent=hero_root)
        obj = builder(r)
        obj.parent = holder
        obj.location = (0, 0, 0)
        holder.scale = (sizes, sizes, sizes)
        pop_in(holder, 0.1 + i * 0.22, (sizes, sizes, sizes), 0.5)
        bob(obj, 0.07, 2.4 + i * 0.3, phase=i)
    key(hero_root, "rotation_euler", 1, (0, 0, -8 * D2R), "SINE")
    key(hero_root, "rotation_euler", STATE["frames"] + 1, (0, 0, 8 * D2R), "SINE")

    action = scene.get("mascot_action", "explain")
    mascot_loc = (-1.15, -1.2, 0.0 if has_ground else 0.2)
    if action != "none":
        build_mascot(action, mascot_loc, style)
    if not has_ground and action != "none":
        platform((mascot_loc[0], mascot_loc[1], mascot_loc[2] - 0.05), (0.35, 0.3, 0.6))

    words = (scene.get("on_screen_text") or "").strip()
    if words:
        tcol = tuple(style.get("text_color", PAL["yellow"]))
        txt = text_obj(words, 0.75, mat("kw", tcol, emit=0.6, rough=0.3), loc=(0, -1.0, 4.55),
                       extrude=0.12, outline_mat=mat("outline", PAL["ink"], rough=0.6))
        bpy.context.view_layer.update()
        max_w = 3.6
        if txt.dimensions.x > max_w:
            f = max_w / txt.dimensions.x
            txt.scale = (f, f, f)
        final = tuple(txt.scale)
        pop_in(txt, 0.25, final, 0.45)
        key(txt, "rotation_euler", sec(0.7), (90 * D2R, 0, -3 * D2R), "SINE")
        key(txt, "rotation_euler", sec(2.0), (90 * D2R, 0, 3 * D2R), "SINE")
        key(txt, "rotation_euler", STATE["frames"] + 1, (90 * D2R, 0, -2 * D2R), "SINE")

    build_camera(scene.get("camera", "push_in"), (0.0, 0.0, 1.9), spec["duration"])
    lights(env)
    configure_render(spec)


def main(argv=None):
    argv = argv if argv is not None else (sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:])
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--frames", default="", help="optional 'start:end' subset (for previews)")
    ap.add_argument("--still", type=int, default=0, help="render only this frame number")
    a = ap.parse_args(argv)
    with open(a.spec, encoding="utf-8") as fh:
        spec = json.load(fh)
    os.makedirs(spec["output_dir"], exist_ok=True)
    build(spec)
    sc = bpy.context.scene
    if a.still:
        sc.frame_set(a.still)
        sc.render.filepath = os.path.join(spec["output_dir"], f"still_{a.still:04d}.png")
        bpy.ops.render.render(write_still=True)
    else:
        if a.frames:
            s, e = a.frames.split(":")
            sc.frame_start, sc.frame_end = int(s), int(e)
        bpy.ops.render.render(animation=True)
    if spec.get("save_blend"):
        bpy.ops.wm.save_as_mainfile(filepath=os.path.join(spec["output_dir"], "scene.blend"))
    print("DVA_RENDER_OK", spec["output_dir"])


if __name__ == "__main__":
    main()
