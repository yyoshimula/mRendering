import unittest
from argparse import Namespace
import numpy as np
from relative_camera import chief_origin, resolve_camera


class RelativeCameraTests(unittest.TestCase):
    def camera(self, mode, chief=None, deputy=None, **kw):
        c = np.eye(4) if chief is None else chief
        d = np.eye(4) if deputy is None else deputy
        if deputy is None:
            d[:3, 3] = [2, 3, 4]
        return resolve_camera(mode, c, d, origin=[7, 8, 9], target=[1, 1, 1], up=[0, 0, 1], **kw)

    def test_tracking_both_directions(self):
        a = self.camera('chief_to_deputy')
        b = self.camera('deputy_to_chief')
        np.testing.assert_allclose(a['origin'], [0, 0, 0])
        np.testing.assert_allclose(a['target'], [2, 3, 4])
        np.testing.assert_allclose(b['origin'], [2, 3, 4])
        np.testing.assert_allclose(b['target'], [0, 0, 0])

    def test_fixed_camera_and_offset_follow_host_attitude(self):
        transform = np.eye(4)
        transform[:3, :3] = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
        a = self.camera('chief_fixed', chief=transform, offset=[.01, 0, 0])
        np.testing.assert_allclose(a['origin'], [0, .01, 0])
        np.testing.assert_allclose(a['target'], [0, 1.01, 0])
        transform[:3, 3] = [2, 3, 4]
        b = self.camera('deputy_fixed', deputy=transform)
        np.testing.assert_allclose(b['target'], [2, 4, 4])

    def test_singular_up_has_a_finite_basis(self):
        d = np.eye(4); d[2, 3] = 1
        a = self.camera('chief_to_deputy', deputy=d)
        self.assertTrue(np.isfinite(a['up']).all())
        self.assertAlmostEqual(np.dot(a['up'], [0, 0, 1]), 0)

    def test_origin_is_enforced_and_coincident_camera_is_rejected(self):
        with self.assertRaises(ValueError): chief_origin([1, 0, 0])
        with self.assertRaises(ValueError): self.camera('chief_to_deputy', deputy=np.eye(4))

    def test_hill_position_does_not_rotate_with_chief_body(self):
        from relative_motion import scene_deputy_state
        p, _ = scene_deputy_state(Namespace(mode='static', relative_frame='hill'),
                                 [0, 0, np.sin(np.pi/4), np.cos(np.pi/4)],
                                 [1, 2, 3], [0, 0, 0, 1])
        np.testing.assert_allclose(p, [1, 2, 3])

    def test_live_scene_hides_only_the_camera_host(self):
        from live_worker import build_relative_frame, camera_from_fields
        for mode in ['chief_to_deputy', 'chief_fixed', 'deputy_to_chief', 'deputy_fixed']:
            with self.subTest(mode=mode):
                fields = {'camera_mode': mode, 'hide_chief': True, 'mode': 'static',
                          'rel_position': [1.5, 0, 0], 'chief_scale': .001, 'deputy_scale': .001,
                          'show_body_axes': False, 'show_inertial_axes': False}
                frame = build_relative_frame(fields, 0, 32, 32, 1)
                names = frame.scene_dict.keys()
                self.assertEqual(any(k.startswith('chief_') for k in names), mode.startswith('deputy_'))
                self.assertEqual(any(k.startswith('deputy_') for k in names), mode.startswith('chief_'))
                self.assertIsNone(camera_from_fields('relative', fields))


if __name__ == '__main__': unittest.main()
