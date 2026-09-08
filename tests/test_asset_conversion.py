import tempfile
import unittest
from pathlib import Path
import numpy as np
import trimesh
from asset_cache import _obj_has_extent, _uses_draco, glb_to_obj_cached


class AssetConversionTests(unittest.TestCase):
    def test_collapsed_obj_is_not_a_valid_cache(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'collapsed.obj'
            path.write_text('v 0 0 0\nv 0 0 0\nf 1 2 1\n')
            self.assertFalse(_obj_has_extent(path))

    def test_aqua_is_decoded_and_loadable(self):
        import mitsuba as mi
        mi.set_variant('llvm_ad_rgb')
        self.assertTrue(_uses_draco(Path('models/aqua.glb')))
        self.assertTrue(_obj_has_extent(Path('models/aqua.obj')))
        mesh = mi.load_dict({'type': 'obj', 'filename': 'models/aqua.obj'})
        self.assertGreater(float(np.linalg.norm(np.array(mesh.bbox().max) - np.array(mesh.bbox().min))), 1)

    def test_scene_node_transform_is_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'translated.glb'
            scene = trimesh.Scene()
            transform = np.eye(4)
            transform[:3, 3] = [10, 20, 30]
            scene.add_geometry(trimesh.creation.box(), transform=transform)
            scene.export(str(path))
            mesh = trimesh.load(str(glb_to_obj_cached(path)), force='mesh')
            np.testing.assert_allclose(mesh.bounds.mean(axis=0), [10, 20, 30])


if __name__ == '__main__':
    unittest.main()
