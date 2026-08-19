"""Blender headless: NASA Hubble (A) GLB -> OBJ + PNG textures for Mitsuba.

- Draco 圧縮 glTF を Blender の native importer で読み込む
- マテリアル毎にオブジェクトを分割し、マテリアル名でリネーム ('o' 名になる)
- 実寸 (全長 13.2 m) に正規化し、バウンディングボックス中心を原点へ
- WebP テクスチャを PNG に変換して models/hubble_textures/ に保存
- OBJ を models/hubble.obj にエクスポート (UV/法線付き)
"""
import bpy
import os
import sys
from mathutils import Vector

ROOT = '<repo root>'
SRC = os.path.join(ROOT, 'models', 'hubble_a.glb')
OBJ_OUT = os.path.join(ROOT, 'models', 'hubble.obj')
TEX_DIR = os.path.join(ROOT, 'models', 'hubble_textures')
HUBBLE_LENGTH_M = 13.2  # 実機全長

os.makedirs(TEX_DIR, exist_ok=True)

# --- クリーンなシーン ---
bpy.ops.wm.read_factory_settings(use_empty=True)

# --- インポート ---
bpy.ops.import_scene.gltf(filepath=SRC)
meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
print('imported mesh objects:', [o.name for o in meshes])

# --- 全メッシュを結合してから material で分割し直す ---
for o in bpy.context.scene.objects:
    o.select_set(o.type == 'MESH')
bpy.context.view_layer.objects.active = meshes[0]
if len(meshes) > 1:
    bpy.ops.object.join()
obj = bpy.context.view_layer.objects.active
# 親の変換を焼き込み
obj.select_set(True)
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

# マテリアル毎に分割
bpy.ops.mesh.separate(type='MATERIAL')
parts = [o for o in bpy.context.scene.objects if o.type == 'MESH']
for p in parts:
    if p.data.materials and p.data.materials[0]:
        p.name = p.data.materials[0].name.replace('.', '_').replace(' ', '_')
print('parts:', [p.name for p in parts])

# --- 実寸へ正規化 + 原点をバウンディングボックス中心へ ---
mins = Vector((1e30, 1e30, 1e30))
maxs = Vector((-1e30, -1e30, -1e30))
for p in parts:
    for corner in p.bound_box:
        wc = p.matrix_world @ Vector(corner)
        mins = Vector(map(min, mins, wc))
        maxs = Vector(map(max, maxs, wc))
size = maxs - mins
center = (maxs + mins) / 2
longest = max(size)
scale = HUBBLE_LENGTH_M / longest
print(f'raw size={tuple(size)}, center={tuple(center)}, scale={scale}')

for p in parts:
    p.location = (p.location.x - center.x, p.location.y - center.y, p.location.z - center.z)
for p in parts:
    p.select_set(True)
bpy.context.view_layer.objects.active = parts[0]
bpy.ops.transform.resize(value=(scale, scale, scale), center_override=(0, 0, 0))
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

# --- テクスチャを PNG で書き出し ---
seen = {}
for img in bpy.data.images:
    if img.name in ('Render Result', 'Viewer Node') or img.size[0] == 0:
        continue
    base = img.name.replace('.', '_').replace(' ', '_')
    if base in seen:
        continue
    seen[base] = True
    out = os.path.join(TEX_DIR, base + '.png')
    img.file_format = 'PNG'
    img.filepath_raw = out
    img.save()
    print('texture ->', out, img.size[:])

# マテリアル→テクスチャ対応を記録
with open(os.path.join(TEX_DIR, 'material_map.txt'), 'w') as fh:
    for p in parts:
        mat = p.data.materials[0] if p.data.materials else None
        tex = None
        if mat and mat.use_nodes:
            for node in mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    tex = node.image.name.replace('.', '_').replace(' ', '_') + '.png'
                    break
        fh.write(f'{p.name}\t{tex}\n')
        print('part', p.name, '-> tex', tex)

# --- OBJ エクスポート ---
for p in parts:
    p.select_set(True)
bpy.ops.wm.obj_export(
    filepath=OBJ_OUT,
    export_selected_objects=True,
    export_materials=True,
    export_normals=True,
    export_uv=True,
    path_mode='RELATIVE',
    forward_axis='Y', up_axis='Z',
)
print('exported:', OBJ_OUT)
