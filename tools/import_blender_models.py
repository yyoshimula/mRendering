"""Run in Blender: --background --disable-autoexec --python this.py -- SOURCE_ROOT.

Import four spacecraft assets as metre-scale, Z-up OBJ meshes grouped by material.
Source .blend files are read only; cameras/lights and reference renders are excluded.
"""
import bpy
import json
import re
import sys
from pathlib import Path
from mathutils import Vector

source = Path(sys.argv[sys.argv.index('--') + 1])
root = Path(__file__).resolve().parents[1]
models = [('acs3', 'ACS3.blend', 'ACS3'), ('bluewalker_3', 'BlueWalker-3.blend', 'BlueWalker-3'),
          ('ekran', 'EKRAN.blend', 'EKRAN'), ('h2a_adrasj', 'H2A_ADRASJ/H2A_ADRASJ.blend', 'H-IIA上段（ADRAS-J）')]
for slug, filename, label in models:
    bpy.ops.wm.open_mainfile(filepath=str(source / filename))
    out = root / 'models/library' / slug
    out.mkdir(parents=True, exist_ok=True)
    scale = bpy.context.scene.unit_settings.scale_length
    depsgraph = bpy.context.evaluated_depsgraph_get()
    groups, bsdfs, saved_images = {}, {}, {}
    count = 0
    for obj in bpy.context.scene.objects:
        if obj.type not in {'MESH', 'CURVE', 'SURFACE', 'FONT'} or obj.hide_render:
            continue
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        if not mesh:
            continue
        mesh.calc_loop_triangles()
        if not mesh.loop_triangles:
            evaluated.to_mesh_clear()
            continue
        count += 1
        coordinates = [tuple((evaluated.matrix_world @ v.co) * scale) for v in mesh.vertices]
        uv = mesh.uv_layers.active
        triangles = {}
        for tri in mesh.loop_triangles:
            triangles.setdefault(tri.material_index, []).append(tri)
        for index, tris in triangles.items():
            mat = mesh.materials[index] if index < len(mesh.materials) else None
            name = mat.name if mat else 'default'
            if name not in groups:
                key = 'mat_' + str(len(groups)) + '_' + re.sub(r'[^A-Za-z0-9_-]', '_', name)
                groups[name] = {'key': key, 'vertices': [], 'uv': [], 'faces': []}
                base, metallic, roughness = [.55, .55, .55], 0., .6
                texture = None
                if mat:
                    base = list(mat.diffuse_color[:3])
                    nodes = list(mat.node_tree.nodes) if mat.use_nodes else []
                    principled = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
                    if principled:
                        base = list(principled.inputs['Base Color'].default_value[:3])
                        metallic = float(principled.inputs['Metallic'].default_value)
                        roughness = float(principled.inputs['Roughness'].default_value)
                        # Direct image-linked base colors are portable. Procedural nodes are not baked.
                        links = principled.inputs['Base Color'].links
                        image_node = links[0].from_node if links else None
                        if image_node and image_node.type == 'TEX_IMAGE' and image_node.image:
                            image = image_node.image
                            if image.name not in saved_images:
                                directory = out / 'textures'; directory.mkdir(exist_ok=True)
                                dest = directory / (re.sub(r'[^A-Za-z0-9_-]', '_', image.name) + '.png')
                                image.filepath_raw = str(dest); image.file_format = 'PNG'; image.save()
                                saved_images[image.name] = str(dest.relative_to(root))
                            texture = saved_images[image.name]
                bsdfs[key] = {'type': 'principled', 'base_color': {'type': 'bitmap', 'filename': texture} if texture else {'type': 'rgb', 'value': base},
                              'metallic': metallic, 'roughness': max(.04, roughness)}
            group = groups[name]
            offset = len(group['vertices'])
            group['vertices'].extend(coordinates)
            for tri in tris:
                vt = []
                if uv:
                    for loop in tri.loops:
                        group['uv'].append(tuple(uv.data[loop].uv)); vt.append(len(group['uv']))
                group['faces'].append(tuple((offset + v + 1, vt[k] if vt else None) for k, v in enumerate(tri.vertices)))
        evaluated.to_mesh_clear()
    obj_path = out / (slug + '.obj')
    vertex_offset = uv_offset = 0
    lo, hi = [float('inf')]*3, [-float('inf')]*3
    with obj_path.open('w') as fh:
        fh.write(f'# {label}; metres; Blender source XYZ axes preserved (Z up)\n')
        for group in groups.values():
            for v in group['vertices']:
                fh.write('v %.8g %.8g %.8g\n' % v)
                for i in range(3): lo[i] = min(lo[i], v[i]); hi[i] = max(hi[i], v[i])
            for v in group['uv']: fh.write('vt %.8g %.8g\n' % v)
            fh.write('o ' + group['key'] + '\n')
            for face in group['faces']:
                refs = [str(v + vertex_offset) + ('/' + str(t + uv_offset) if t is not None else '') for v, t in face]
                fh.write('f ' + ' '.join(refs) + '\n')
            vertex_offset += len(group['vertices']); uv_offset += len(group['uv'])
    metadata = {'model': obj_path.name, 'label': label, 'source': filename, 'units': 'm', 'up_axis': 'Z',
                'parts': bsdfs, 'default': {}, 'source_objects': count,
                'triangles': sum(len(g['faces']) for g in groups.values()), 'bounds': [lo, hi],
                'notes': 'Materials approximate Blender Principled BSDF. Procedural shader nodes are not baked.'}
    (out/'materials.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2)+'\n')
    print('IMPORTED', label, count, 'objects;', len(groups), 'materials;', metadata['triangles'], 'triangles', flush=True)
