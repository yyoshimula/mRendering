"""Blender background helper for Draco-compressed glTF geometry conversion."""
import sys
from pathlib import Path
import bpy

source, output = sys.argv[sys.argv.index('--') + 1:]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=str(Path(source).resolve()))
if not any(obj.type == 'MESH' for obj in bpy.context.scene.objects):
    raise RuntimeError('glTF contains no mesh objects')
bpy.ops.wm.obj_export(filepath=str(Path(output).resolve()), export_materials=False, export_normals=False,
                      forward_axis='NEGATIVE_Z', up_axis='Y', export_triangulated_mesh=True)
