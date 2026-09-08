"""Static, normalized model inspection scene for the GUI live viewport.

No attitude propagation, environment map, Earth, or floor. The coordinate arrows
are centered on the model bounds and retain the source model's axis directions.
"""
from functools import lru_cache
from pathlib import Path

import numpy as np
import mitsuba as mi

from scene_common import create_axes, load_model_with_parts
from scene_objects import load_external_model


from model_materials import registered_materials as _registered_materials


def _geometry(path, keep_materials):
    if path:
        if not Path(path).is_file():
            raise ValueError(f'モデルが見つかりません: {path}')
        materials = _registered_materials(path) if keep_materials else None
        if materials:
            parts, default = materials
            return load_model_with_parts(path, parts, default, mi.ScalarTransform4f(),
                                         scale=1.0, prefix='model')
        return load_external_model(path, np.zeros(3), np.eye(3), scale=1.0,
                                   keep_materials=keep_materials, prefix='model')
    return load_external_model(str(Path(__file__).parent / 'models/test_cube.obj'),
                               np.zeros(3), np.eye(3), scale=1.0, prefix='model')


@lru_cache(maxsize=32)
def _bounds(path, mtime):
    # Materials do not affect bounds; cache by source modification time.
    scene = mi.load_dict({'type': 'scene', **_geometry(path, False)})
    bbox = scene.bbox()
    lo, hi = np.array(bbox.min), np.array(bbox.max)
    center = (lo + hi) / 2
    radius = float(np.linalg.norm(hi - lo) / 2)
    if not np.all(np.isfinite(center)) or not np.isfinite(radius) or radius <= 0:
        raise ValueError('モデルの大きさを取得できません')
    return center, radius


def _arrow_tip(axis, color, length=1.45):
    """Closed cone mesh (Mitsuba does not provide an analytic cone shape)."""
    n = 24
    direction = np.eye(3)[axis]
    u, v = np.eye(3)[(axis + 1) % 3], np.eye(3)[(axis + 2) % 3]
    theta = np.arange(n) * (2 * np.pi / n)
    ring = length * direction + .065 * (np.cos(theta)[:, None] * u + np.sin(theta)[:, None] * v)
    vertices = np.vstack([ring, (length + .20) * direction, length * direction])
    faces = []
    for i in range(n):
        j = (i + 1) % n
        faces.extend([(i, j, n), (j, i, n + 1)])
    props = mi.Properties()
    props['bsdf'] = mi.load_dict({'type': 'diffuse', 'reflectance': {'type': 'rgb', 'value': color}})
    mesh = mi.Mesh(f'axis_{axis}_arrow', len(vertices), len(faces), props)
    params = mi.traverse(mesh)
    params['vertex_positions'] = vertices.astype(np.float32).ravel()
    params['faces'] = np.asarray(faces, dtype=np.uint32).ravel()
    params.update()
    return mesh


@lru_cache(maxsize=2)
def _wire_mesh(path, mtime, line_radius):
    """Combine unique triangle edges into one mesh, avoiding thousands of shapes."""
    center, radius = _bounds(path, mtime)
    raw = mi.load_dict({'type': 'scene', **_geometry(path, False)})
    segments = []
    for shape in raw.shapes():
        params = mi.traverse(shape)
        if 'vertex_positions' not in params or 'faces' not in params:
            continue
        vertices = (np.array(params['vertex_positions']).reshape(-1, 3) - center) / radius
        triangles = np.array(params['faces']).reshape(-1, 3)
        # Weld OBJ normal/UV seam duplicates for edge display only.
        vertices, remap = np.unique(np.round(vertices, 7), axis=0, return_inverse=True)
        triangles = remap[triangles]
        edges = np.concatenate([triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]]])
        edges = np.unique(np.sort(edges, axis=1), axis=0)
        segments.append(vertices[edges])
    if not segments:
        raise ValueError('三角形メッシュの辺を取得できません')
    segments = np.concatenate(segments)
    a, b = segments[:, 0], segments[:, 1]
    delta = b - a
    lengths = np.linalg.norm(delta, axis=1)
    valid = lengths > 1e-8
    a, b, delta, lengths = a[valid], b[valid], delta[valid], lengths[valid]
    d = delta / lengths[:, None]
    basis = np.eye(3)[np.argmin(np.abs(d), axis=1)]
    u = np.cross(d, basis)
    u /= np.linalg.norm(u, axis=1)[:, None]
    v = np.cross(d, u)
    sides = 6
    angle = np.arange(sides) * (2 * np.pi / sides)
    ring = line_radius * (np.cos(angle)[None, :, None] * u[:, None, :]
                          + np.sin(angle)[None, :, None] * v[:, None, :])
    vertices = np.concatenate([a[:, None, :] + ring, b[:, None, :] + ring], axis=1).reshape(-1, 3)
    local_faces = []
    for i in range(sides):
        j = (i + 1) % sides
        local_faces.extend([(i, j, sides + i), (j, sides + j, sides + i)])
    faces = (np.asarray(local_faces)[None, :, :] + np.arange(len(a))[:, None, None] * sides * 2).reshape(-1, 3)
    props = mi.Properties()
    props['bsdf'] = mi.load_dict({'type': 'diffuse', 'reflectance': {'type': 'rgb', 'value': [.12, .65, .9]}})
    mesh = mi.Mesh('model_wire_edges', len(vertices), len(faces), props)
    params = mi.traverse(mesh)
    params['vertex_positions'] = vertices.astype(np.float32).ravel()
    params['faces'] = faces.astype(np.uint32).ravel()
    params.update()
    return mesh


def build_scene(fields, width, height, samples):
    path = fields.get('model_path') or ''
    mtime = Path(path).stat().st_mtime_ns if path else 0
    center, radius = _bounds(path, mtime)
    transform = mi.ScalarTransform4f.scale(1 / radius) @ mi.ScalarTransform4f.translate(-center)
    shapes = _geometry(path, bool(fields.get('keep_materials', False)))
    for shape in shapes.values():
        if not fields.get('keep_materials', False):
            shape['bsdf'] = {'type': 'diffuse', 'reflectance': {'type': 'rgb', 'value': [.55, .55, .55]}}
        shape['to_world'] = transform @ shape.get('to_world', mi.ScalarTransform4f())
    mode = fields.get('display_mode', 'solid')
    if mode in ('overlay', 'wire'):
        wire = _wire_mesh(path, mtime, .004)
        if mode == 'wire':
            shapes = {}
        shapes['model_wire_edges'] = wire
    fov = float(fields.get('camera_fov') or 40)
    # Fit both the unit-radius model and the 1.65-long arrows, including portrait films.
    half_angle = np.arctan(np.tan(np.deg2rad(fov / 2)) * min(1., height / width))
    distance = 1.8 / np.sin(half_angle)
    default_origin = np.array([3., 2., 3.]) / np.sqrt(22) * distance
    camera = {'origin': fields.get('camera_origin') or default_origin.tolist(),
              'target': fields.get('camera_target') or [0., 0., 0.],
              'up': fields.get('camera_up') or [0., 1., 0.], 'fov': fov}
    scene = {
        'type': 'scene',
        'integrator': {'type': 'path', 'max_depth': 6},
        'camera': {'type': 'perspective', 'fov': fov, 'near_clip': .001,
                   'to_world': mi.ScalarTransform4f.look_at(**{k: camera[k] for k in ('origin', 'target', 'up')}),
                   'film': {'type': 'hdrfilm', 'width': width, 'height': height,
                            'rfilter': {'type': 'gaussian'}},
                   'sampler': {'type': 'independent', 'sample_count': samples}},
        **shapes,
    }
    # Opposed studio lights illuminate both sides without a visible background.
    for i, direction in enumerate(([1, -1, -1], [-1, .5, 1], [0, 1, -.5])):
        scene[f'light_{i}'] = {'type': 'directional', 'direction': direction,
                               'irradiance': {'type': 'rgb', 'value': [2., 2., 2.]}}
    colors = [[1., .12, .12], [.12, 1., .12], [.15, .35, 1.]]
    axes = create_axes(length=1.45, radius=.018, colors=colors, prefix='model_axes', glow=0)
    for i, name in enumerate(('X', 'Y', 'Z')):
        axes.pop(f'model_axes_axis_{name.lower()}_tip', None)
        axes.pop(f'model_axes_axis_{name}_tip', None)
        axes[f'model_arrow_{name}'] = _arrow_tip(i, colors[i])
    scene.update(axes)
    return scene, camera
