"""Run with: venv/bin/python -m unittest discover -s tests -p 'test_model_viewer.py'"""
import unittest

import numpy as np
import mitsuba as mi

mi.set_variant('llvm_ad_rgb')

from model_viewer import build_scene  # noqa: E402
from live_worker import build_frame  # noqa: E402
from pathlib import Path  # noqa: E402


class ModelViewerTests(unittest.TestCase):
    def test_display_modes_render_without_background(self):
        for mode in ('solid', 'overlay', 'wire'):
            with self.subTest(mode=mode):
                spec, camera = build_scene({'display_mode': mode}, 160, 120, 8)
                self.assertNotIn('envmap', spec)
                self.assertEqual(len([k for k in spec if k.startswith('model_arrow_')]), 3)
                self.assertEqual('model_wire_edges' in spec, mode != 'solid')
                if mode == 'wire':
                    self.assertNotIn('model_mesh', spec)
                pixels = np.array(mi.render(mi.load_dict(spec), spp=8, seed=1))
                self.assertTrue(np.isfinite(pixels).all())
                self.assertGreater(pixels.max(), 0)
                np.testing.assert_array_equal(pixels[0, 0], [0, 0, 0])

    def test_time_and_angular_velocity_do_not_rotate_model(self):
        # Even old rotation settings must have no effect on this separate mode.
        fields = {'wx': 9, 'wy': 7, 'wz': 5, 'frames': 1000}
        first = build_frame('modelview', fields, 0, 160, 120, 4, Path('/tmp'))
        later = build_frame('modelview', fields, 500, 160, 120, 4, Path('/tmp'))
        self.assertEqual(first.traj_frames, 1)
        self.assertEqual(later.time_s, 0)
        self.assertEqual(first.camera, later.camera)
        a = np.array(mi.render(mi.load_dict(first.scene_dict), spp=4, seed=3))
        b = np.array(mi.render(mi.load_dict(later.scene_dict), spp=4, seed=3))
        np.testing.assert_array_equal(a, b)

    def test_akatsuki_materials_come_from_its_preset(self):
        spec, _ = build_scene({'model_path': 'models/akatsuki.obj', 'keep_materials': True}, 160, 120, 4)
        self.assertEqual(spec['model_part_PLANET-C_v24']['bsdf']['material'], 'Au')
        self.assertEqual(spec['model_part_sap1']['bsdf']['reflectance']['value'], [.05, .05, .3])
        gray, _ = build_scene({'model_path': 'models/akatsuki.obj', 'keep_materials': False}, 160, 120, 4)
        self.assertEqual(gray['model_mesh']['bsdf']['reflectance']['value'], [.55, .55, .55])

    def test_camera_override_keeps_model_geometry_fixed(self):
        spec, _ = build_scene({}, 160, 120, 4)
        moved, camera = build_scene({'camera_origin': [0, 0, 7], 'camera_target': [0, 0, 0]}, 160, 120, 4)
        np.testing.assert_array_equal(np.array(spec['model_mesh']['to_world'].matrix),
                                      np.array(moved['model_mesh']['to_world'].matrix))
        self.assertEqual(camera['origin'], [0, 0, 7])


if __name__ == '__main__':
    unittest.main()
