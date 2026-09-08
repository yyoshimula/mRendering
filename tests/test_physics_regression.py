"""Boundary and integration regressions for the 2026-09-08 physics fixes."""
import math
import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

import orbit_mechanics as om
import scene_common as sc
import scene_earth as se
import satellite_orbit as so
import relative_motion as rm
import live_worker
import mitsuba as mi
from config_loader import build_parser, build_render_config, resolve_primary_object
from tools.physics_audit import reference_orbit, attitude_metrics
from yoshimulib.attitude.quaternion import q2dcm


class PhysicsRegressionTests(unittest.TestCase):
    def test_orbit_phase_across_circular_and_equatorial_limits(self):
        for e in [0., 1e-12, 1e-10, 2e-10, .1]:
            for inc in [0., 1e-12, 2e-10, .7, math.pi-1e-12, math.pi]:
                with self.subTest(e=e, inc=inc):
                    el = om.OrbitalElements(10000, e, inc, .8, 1.3, .4)
                    for t in [-500., 0., 1000.]:
                        actual = om.compute_orbital_position(el, t)
                        expected = reference_orbit(el, t)
                        np.testing.assert_allclose(actual[0], expected[0], atol=1e-7, rtol=0)
                        np.testing.assert_allclose(actual[1], expected[1], atol=1e-10, rtol=0)

    def test_batched_true_anomaly_preserves_input_angles(self):
        # e=0 gives M=f, including retrograde equatorial cases. This is also the
        # vectorized conversion used by groundobs' overhead alignment search.
        rows = np.array([[7000, 0, inc, .8, 1.1, f]
                         for inc in [0., .7, math.pi] for f in [-1., 0., 1.]])
        original = rows.copy()
        r, v = om.oe2rv(rows, flag=1, mu=om.EARTH_MU)
        for i, row in enumerate(rows):
            rr, vv = reference_orbit(om.OrbitalElements(*row), 0)
            np.testing.assert_allclose(r[i], rr, atol=1e-7, rtol=0)
            np.testing.assert_allclose(v[i], vv, atol=1e-10, rtol=0)
        np.testing.assert_array_equal(rows, original)

    def test_numerical_cache_handles_both_directions_and_extension(self):
        el = om.OrbitalElements(10000, .1, .7, .8, 1.1, .3)
        propagator = om.NumericalPropagator(el, use_j2=False)
        for t in [0., -10., 100., -100., 12000., -12000., 2., -2.]:
            with self.subTest(t=t):
                r, v = propagator.state_at(t)
                rr, vv = reference_orbit(el, t)
                np.testing.assert_allclose(r, rr, atol=1e-5, rtol=0)
                np.testing.assert_allclose(v, vv, atol=1e-8, rtol=0)
        for t in [np.nan, np.inf, -np.inf]:
            with self.assertRaises(ValueError):
                propagator.state_at(t)

    def test_coupled_attitude_conserves_inertial_momentum(self):
        m = attitude_metrics([2500, 8000, 7000], [.02, .005, .03], 5553.6/180, 179)
        self.assertLess(m['error_deg'], 1e-5)
        self.assertLess(m['angular_momentum_vector_relative_error'], 1e-8)

    def test_attitude_zero_interval_and_reverse_integration(self):
        I, Ii = sc.inertia_matrices(4, 2, 1)
        q = Rotation.from_rotvec([.2, .3, .4]).as_quat()
        w = np.array([.3, .1, 2.])
        q0, w0 = sc.propagate_attitude(q, w, I, Ii, 0.)
        np.testing.assert_allclose(q0, q, atol=1e-14)
        np.testing.assert_array_equal(w0, w)
        q1, w1 = sc.propagate_attitude(q, w, I, Ii, 10.)
        q2, w2 = sc.propagate_attitude(q1, w1, I, Ii, -10.)
        np.testing.assert_allclose(q2dcm(q2, scalar=4), q2dcm(q, scalar=4), atol=1e-8)
        np.testing.assert_allclose(w2, w, atol=1e-8)

    def test_rotated_day_cloud_and_night_layers_share_geographic_uv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/'white.exr')
            mi.util.write_bitmap(path, np.ones((16, 32, 3), dtype=np.float32), write_async=False)
            for angle in [0., 37., 90., 180., 359.]:
                # Geographic longitude 0 at the equator must always sample u=.5.
                expected_direction = se.rotate_vector_z(np.array([1., 0, 0]), angle)
                factories = [se.create_earth, se.create_clouds, se.create_night_lights]
                for factory in factories:
                    with self.subTest(angle=angle, layer=factory.__name__):
                        shape = factory(radius=1., texture_path=path, rotation_deg=angle)
                        scene = mi.load_dict({'type': 'scene', 'surface': shape})
                        hit = scene.ray_intersect(mi.Ray3f(mi.Point3f(2*expected_direction),
                                                          mi.Vector3f(-expected_direction)))
                        uv = np.array(hit.uv).ravel()
                        np.testing.assert_allclose(uv, [.5, .5], atol=2e-6)
                        sun_body = se.rotate_vector_z(expected_direction, -angle)
                        mask = se.generate_night_mask(sun_body, 360, 180)
                        self.assertLess(mask[int(uv[1]*180), int(uv[0]*360)], .01)

    def test_horizon_contacts_and_actual_occultations(self):
        R = om.EARTH_RADIUS_KM
        for lat, lon in [(35, 125), (-86, -180), (0, 0), (90, 0)]:
            observer = om.compute_observer_position_km(lat, lon, 0.)
            self.assertFalse(om.is_earth_occluded(observer, observer*(1+400/R)))
            self.assertTrue(om.is_earth_occluded(observer, -observer*(1+400/R)))
        observer = np.array([R, 0., 0.])
        self.assertFalse(om.is_earth_occluded(observer, observer+[0, 400, 0]))
        for elevation, expected in [(1e-6, False), (-1e-6, True)]:
            target = observer+400*np.array([math.sin(elevation), math.cos(elevation), 0])
            self.assertEqual(om.is_earth_occluded(observer, target), expected)

    def test_csv_without_attitude_uses_each_pointing_law(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'orbit.csv'
            p.write_text('time_s,a_km,e,inc_rad,raan_rad,ome_rad,f_rad\n'
                         '0,7000,0,0.7,0.8,1.1,0.3\n10,7000,0,0.7,0.8,1.1,0.4\n')
            args = build_parser().parse_args([])
            args.csv_path = str(p)
            sun = np.array([1., 0, 0])
            for mode in ['nadir', 'sun_tracking', 'velocity_aligned']:
                args.attitude_mode = mode
                state = so.compute_object_state(resolve_primary_object(args), 5, sun)
                if mode == 'nadir':
                    actual = state.attitude_matrix[:, 2]
                    expected = -state.position_km/np.linalg.norm(state.position_km)
                elif mode == 'sun_tracking':
                    actual, expected = state.attitude_matrix[:, 2], sun
                else:
                    actual = state.attitude_matrix[:, 0]
                    expected = state.velocity_km/np.linalg.norm(state.velocity_km)
                np.testing.assert_allclose(actual, expected, atol=1e-12)

    def test_absolute_csv_attitude_transforms_into_scene_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'orbit.csv'
            q = Rotation.from_rotvec([.3, .4, .5]).as_quat()
            p.write_text('time_s,a_km,e,inc_rad,raan_rad,ome_rad,f_rad,q1,q2,q3,q4\n'
                         '0,7000,0.1,0.7,0.8,1.1,0.3,'+','.join(map(str, q))+'\n')
            args = rm.parse_args(['--mode', 'absolute', '--chief-oe', '7000', '.1', '40', '20', '30', '0',
                                  '--deputy-orbit-csv', str(p), '--deputy-attitude', 'csv'])
            ctx = rm.AbsoluteOrbitContext(args)
            _, i2rtn, _, _ = ctx.relative_state(0)
            i2body = ctx.deputy_attitude_dcm(0, i2rtn, np.array([0., 0, 0, 1]), None)
            scene_q = ctx.rel_quat_scene(i2body, i2rtn)
            body_to_scene = np.array(rm.make_body_transform(np.zeros(3), scene_q).matrix)[:3, :3]
            expected = i2rtn @ Rotation.from_quat(q).as_matrix()
            np.testing.assert_allclose(body_to_scene, expected, atol=1e-6)

    def test_camera_and_live_earth_use_same_rotation(self):
        args = build_parser().parse_args([])
        cfg = build_render_config(args, None)
        cfg.camera.view_mode = 'satellite'
        cfg.camera.target_lat = 0.; cfg.camera.target_lon = 0.
        cfg.earth.use_clouds = cfg.earth.use_night_lights = False
        spec = resolve_primary_object(args)
        with tempfile.TemporaryDirectory() as tmp:
            for enabled in [False, True]:
                cfg.earth.earth_rotation = enabled
                for t in [-10000., 0., 10000.]:
                    state = so.compute_object_state(spec, t, np.array([1., 0, 0]))
                    _, target, _, _ = so.resolve_camera([state], cfg)
                    batch, _, _ = so.build_earth_textures(Path(tmp), 0, t, np.array([1., 0, 0]), cfg)
                    live, _, _ = live_worker.orbit_earth_textures(Path(tmp), t, np.array([1., 0, 0]), cfg)
                    self.assertEqual(batch, live)
                    expected = se.rotate_vector_z(np.array([1., 0, 0]), batch)
                    np.testing.assert_allclose(target, expected, atol=1e-12)


if __name__ == '__main__':
    unittest.main()
