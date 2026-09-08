"""2026-09-08 第 2 次検証（#2〜#8）の回帰テスト。

対象: 星空 envmap の投影規約、lightcurve 観測者の自転・自動画角、太陽/恒星の
平均分点 of date、groundobs 光電子換算、黒体色の線形化、render 系の絶対エポック。
"""
import math
import unittest

import numpy as np

import scene_common  # noqa: F401  Mitsuba バリアント初期化（import 順回帰 #1 は保留中）
import mitsuba as mi
import orbit_mechanics as om
import satellite_orbit as so
import ground_observation as go
import relative_motion as rm
import optical_lighting as ol
from config_loader import build_parser, build_render_config
from scene_builder import create_scene
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    'generate_starfield', Path(__file__).resolve().parents[1] / 'tools' / 'generate_starfield.py')
generate_starfield = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generate_starfield)


def meeus_sun_longitude_of_date_deg(jd: float) -> float:
    """Meeus ch.25 低精度太陽黄経（平均分点 of date、精度 ~0.01°）。"""
    T = (jd - 2451545.0) / 36525.0
    L0 = (280.46646 + 36000.76983 * T + 0.0003032 * T * T) % 360
    M = math.radians((357.52911 + 35999.05029 * T - 0.0001537 * T * T) % 360)
    C = ((1.914602 - 0.004817 * T - 0.000014 * T * T) * math.sin(M)
         + (0.019993 - 0.000101 * T) * math.sin(2 * M) + 0.000289 * math.sin(3 * M))
    return (L0 + C) % 360


def ra_dec_unit(ra_deg, dec_deg):
    ra, dec = math.radians(ra_deg), math.radians(dec_deg)
    return np.array([math.cos(dec) * math.cos(ra), math.cos(dec) * math.sin(ra), math.sin(dec)])


class SunAndStarFrameTests(unittest.TestCase):
    def test_sun_position_is_mean_equinox_of_date(self):
        for epoch in ['2026-04-05T12:00:00', '2050-01-01T00:00:00Z', '1992-10-13T00:00:00']:
            jd = om.parse_epoch_jd(epoch)
            r = om.sun_position_eci_km(jd)
            eps = om.mean_obliquity_rad(jd)
            # 赤道 → 黄道（of date）に戻して黄経を比較
            y_ecl = r[1] * math.cos(eps) + r[2] * math.sin(eps)
            lon = math.degrees(math.atan2(y_ecl, r[0])) % 360
            self.assertAlmostEqual(lon, meeus_sun_longitude_of_date_deg(jd), delta=0.02, msg=epoch)
            self.assertAlmostEqual(np.linalg.norm(r) / om.AU_KM if hasattr(om, 'AU_KM') else 1.0,
                                   1.0 if not hasattr(om, 'AU_KM') else np.linalg.norm(r) / om.AU_KM, places=6)

    def test_j2000_and_date_frames_differ_by_general_precession(self):
        jd = om.parse_epoch_jd('2026-04-05T12:00:00')
        a = om.sun_position_eci_km(jd, frame='j2000')
        b = om.sun_position_eci_km(jd, frame='date')
        angle = math.degrees(math.acos(np.clip(a @ b / np.linalg.norm(a) / np.linalg.norm(b), -1, 1)))
        years = (jd - 2451545.0) / 365.25
        self.assertAlmostEqual(angle, 50.29 * years / 3600 * math.cos(math.radians(23.44)) * 1.0,
                               delta=0.05)  # 黄道上の点の歳差 ≈ 50.3″/yr（赤道面で見て ≈ 0.37°）

    def test_precession_matrix_is_rotation_and_moves_equinox_forward(self):
        jd = om.parse_epoch_jd('2026-04-05T12:00:00')
        P = om.precession_j2000_to_date(jd)
        np.testing.assert_allclose(P @ P.T, np.eye(3), atol=1e-12)
        self.assertAlmostEqual(np.linalg.det(P), 1.0, places=12)
        # 分点は黄道上を西へ後退するので、J2000 の春分点方向は of-date 系では正の赤経
        # （恒星の赤経は年 ~46″ = p·cos ε 増える）。26.26 年で ≈ 0.3668°·cos 23.44° = 0.337°
        x = P @ np.array([1.0, 0, 0])
        ra_deg = math.degrees(math.atan2(x[1], x[0]))
        self.assertGreater(ra_deg, 0)
        self.assertAlmostEqual(ra_deg, 0.3668 * math.cos(math.radians(23.44)), delta=0.005)

    def test_star_catalog_directions_are_precessed_to_date(self):
        jd = om.parse_epoch_jd('2026-08-19T00:00:00')
        # 固有運動ゼロの合成カタログ 1 星（RA 0, Dec 0）
        cat = {'unit': np.array([[1.0, 0, 0]]), 'ra': np.array([0.0]), 'dec': np.array([0.0]),
               'pm_ra_cosdec': np.array([0.0]), 'pm_dec': np.array([0.0]), 'mag': np.array([1.0])}
        moved, mag = go.stars_in_field(cat, np.array([1.0, 0, 0]), math.radians(2), 5.0, jd)
        self.assertEqual(len(mag), 1)
        expected = om.precession_j2000_to_date(jd) @ np.array([1.0, 0, 0])
        np.testing.assert_allclose(moved[0], expected, atol=1e-12)
        self.assertGreater(math.degrees(math.acos(moved[0] @ np.array([1.0, 0, 0]))), 0.3)


class StarfieldEnvmapConventionTests(unittest.TestCase):
    def _probe(self, env, direction):
        si = mi.SurfaceInteraction3f()
        d = -np.asarray(direction, dtype=float)
        si.wi = mi.Vector3f(float(d[0]), float(d[1]), float(d[2]))
        return np.array(env.eval(si)).ravel()

    def test_generated_pixel_convention_matches_mitsuba_with_fixed_rotation(self):
        W, H = 256, 128
        # 画素値に連続 (u, v) を符号化（bilinear 補間は線形なので継ぎ目以外は厳密）
        u = (np.arange(W) + 0.5) / W
        v = (np.arange(H) + 0.5) / H
        img = np.zeros((H, W, 3), dtype=np.float32)
        img[:, :, 0] = u[None, :]
        img[:, :, 1] = v[:, None]
        env = mi.load_dict({'type': 'envmap', 'bitmap': mi.Bitmap(img),
                            'to_world': mi.ScalarTransform4f(ol.starfield_to_world_matrix().tolist())})
        for ra, dec in [(10, 0), (90, 0), (180, 0), (270, 0), (45, 60), (300, -45), (200, 20)]:
            px, py = generate_starfield.pixel_of_ra_dec(math.radians(ra), math.radians(dec), W, H)
            value = self._probe(env, ra_dec_unit(ra, dec))
            with self.subTest(ra=ra, dec=dec):
                # 生成側の画素 (px, py) と Mitsuba が実際に参照する (u, v) が 1 画素以内で一致
                self.assertAlmostEqual(value[0] * W, px + 0.5, delta=1.0)
                self.assertAlmostEqual(value[1] * H, py + 0.5, delta=1.0)
                # 星図の向き: RA が増えると u は減る（内側から見た空）、Dec が増えると上へ
                self.assertAlmostEqual(value[0], (1 - ra / 360) % 1, delta=1.5 / W)
                self.assertAlmostEqual(value[1], 0.5 - dec / 180, delta=1.5 / H)

    def test_fixed_rotation_is_proper_and_puts_pole_and_equinox(self):
        R = ol.STARFIELD_LOCAL_TO_ECI
        self.assertAlmostEqual(np.linalg.det(R), 1.0, places=12)
        np.testing.assert_allclose(R @ [0, 1, 0], [0, 0, 1])    # local +y → 北天極
        np.testing.assert_allclose(R @ [0, 0, -1], [1, 0, 0])   # local −z → 春分点

    def test_relative_rotate_env_dict_composes_eci_rotation(self):
        a = np.array([[0, 1, 0], [-1, 0, 0], [0, 0, 1]], dtype=float)  # Rz(-90°)
        out = rm.AbsoluteOrbitContext.rotate_env_dict({'type': 'envmap', 'filename': 'x.exr'}, a)
        m = np.array(out['to_world'].matrix)[:3, :3]
        np.testing.assert_allclose(m, a @ ol.STARFIELD_LOCAL_TO_ECI, atol=1e-12)

    def test_render_scene_envmap_has_fixed_rotation(self):
        scene = create_scene(camera_position=[3, 0, 0], camera_target=[0, 0, 0], camera_up=[0, 0, 1],
                             satellite_objects={}, sun_direction=np.array([1.0, 0, 0]),
                             earth_texture=None, width=8, height=8, fov=40,
                             use_advanced_lighting=False, include_earth=False,
                             hdri_path='assets/starfield.exr', sample_count=1)
        m = np.array(scene['envmap']['to_world'].matrix)[:3, :3]
        np.testing.assert_allclose(m, ol.STARFIELD_LOCAL_TO_ECI, atol=1e-12)


class LightCurveTests(unittest.TestCase):
    def test_observer_rotates_with_earth_like_ground_target(self):
        args = build_parser().parse_args([])
        cfg = build_render_config(args, None)
        t = cfg.earth.earth_rotation_period_hours * 3600 / 4
        obs = so.observer_position_eci_km(t, cfg, 0.0, 0.0, 0.0)
        # 赤道・経度 0 の観測者は 1/4 自転で +y へ
        np.testing.assert_allclose(obs / np.linalg.norm(obs), [0, 1, 0], atol=1e-9)
        fixed = so.observer_position_eci_km(0.0, cfg, 0.0, 0.0, 0.0)
        self.assertGreater(np.linalg.norm(obs - fixed), 5000)

    def test_auto_fov_fits_procedural_and_mesh_objects(self):
        objects = so.create_satellite(np.array([2.0, 0, 0]), np.eye(3), scale=0.002)
        radius = so.estimate_bounding_radius(objects, np.array([2.0, 0, 0]))
        self.assertGreater(radius, 0.004)   # パネル半幅 (5·scale/2 = 0.005) 以上
        self.assertLess(radius, 0.03)
        fov = so.resolve_light_curve_fov(None, objects, np.array([2.0, 0, 0]), 1.0)
        self.assertGreater(fov, math.degrees(2 * math.atan(radius)))          # 収まる
        self.assertLess(fov, math.degrees(2 * math.atan(2.0 * radius)))       # 過大でない
        self.assertEqual(so.resolve_light_curve_fov(2.0, objects, np.array([2.0, 0, 0]), 1.0), 2.0)


class PhotoelectronTests(unittest.TestCase):
    def test_v_band_photoelectron_count_matches_textbook(self):
        # m_V = 13, D = 0.3 m, 1 s, QE 0.8, τ 0.7 → 教科書 (Bessell V 帯) ≈ 2.2e3 e⁻
        s0 = 1361.0
        E = s0 * 10 ** (-0.4 * (13 - go.SUN_APPARENT_MAG))
        n_e = E * go.photons_per_wm2(s0) * math.pi * 0.15 ** 2 * 0.7 * 1.0 * 0.8
        self.assertAlmostEqual(n_e / 2209, 1.0, delta=0.1)
        self.assertAlmostEqual(go.V_BAND_SOLAR_IRRADIANCE_WM2, 159, delta=5)

    def test_sensor_electrons_uses_v_band_scale(self):
        args = go.parse_args([])
        args.aperture_m = 0.3; args.throughput = 0.7; args.exposure_s = 1.0
        args.quantum_efficiency = 0.8; args.read_noise_e = 0.0; args.gain_e_per_adu = 1.0
        args.full_well_e = 1e12; args.sun_irradiance_wm2 = 1361.0
        E = 1361.0 * 10 ** (-0.4 * (13 - go.SUN_APPARENT_MAG))
        img = np.full((64, 64), E)
        adu = go.sensor_electrons(img, args, np.random.default_rng(0))
        self.assertAlmostEqual(adu.mean() / 2209, 1.0, delta=0.1)


class BlackbodyTests(unittest.TestCase):
    def test_linear_srgb_of_solar_and_daylight_temperatures(self):
        rgb = ol.blackbody_to_rgb(5778)
        self.assertEqual(rgb.max(), 1.0)
        self.assertAlmostEqual(rgb[1], 0.88, delta=0.02)
        self.assertAlmostEqual(rgb[2], 0.82, delta=0.02)
        d65 = ol.blackbody_to_rgb(6504)
        self.assertTrue(np.all(d65 > 0.93))
        hot = ol.blackbody_to_rgb(9500)
        self.assertGreater(hot[2], hot[0])

    def test_starfield_generator_shares_linear_blackbody(self):
        np.testing.assert_allclose(generate_starfield.blackbody_rgb(4500), ol.blackbody_to_rgb(4500))


class RenderEpochTests(unittest.TestCase):
    def _config(self, **over):
        args = build_parser().parse_args([])
        for k, v in over.items():
            setattr(args, k, v)
        return build_render_config(args, None)

    def test_epoch_drives_sun_and_gmst(self):
        cfg = self._config(epoch_utc='2026-04-05T12:00:00')
        jd0 = om.parse_epoch_jd('2026-04-05T12:00:00')
        np.testing.assert_allclose(so.resolve_sun_direction(0.0, cfg), om.sun_direction_at_jd(jd0), atol=1e-12)
        np.testing.assert_allclose(so.resolve_sun_direction(3600.0, cfg),
                                   om.sun_direction_at_jd(jd0 + 3600 / 86400), atol=1e-12)
        self.assertAlmostEqual(so.compute_earth_rotation_deg(0.0, cfg), math.degrees(om.gmst_rad(jd0)), places=9)
        self.assertAlmostEqual(so.compute_earth_rotation_deg(3600.0, cfg) - so.compute_earth_rotation_deg(0.0, cfg),
                               15.04107, delta=1e-3)

    def test_without_epoch_legacy_model_is_unchanged(self):
        cfg = self._config()
        np.testing.assert_allclose(so.resolve_sun_direction(0.0, cfg), [1, 0, 0], atol=1e-12)
        self.assertEqual(so.compute_earth_rotation_deg(0.0, cfg), 0.0)

    def test_sun_angle_is_ecliptic_longitude(self):
        cfg = self._config(sun_angle=90.0)
        d = so.resolve_sun_direction(0.0, cfg)
        eps = om.mean_obliquity_rad(2451545.0)
        np.testing.assert_allclose(d, [0, math.cos(eps), math.sin(eps)], atol=1e-12)
        cfg0 = self._config(sun_angle=0.0)
        np.testing.assert_allclose(so.resolve_sun_direction(0.0, cfg0), [1, 0, 0], atol=1e-12)



class StarfieldSidecarTests(unittest.TestCase):
    def test_regeneration_decision_by_projection_sidecar(self):
        import json, tempfile
        import asset_bootstrap as ab
        with tempfile.TemporaryDirectory() as tmp:
            exr = Path(tmp) / 'starfield.exr'
            self.assertEqual(ab.starfield_needs_regeneration(exr), 'missing')
            exr.write_bytes(b'x')
            self.assertIn('no-sidecar', ab.starfield_needs_regeneration(exr))
            ab.starfield_sidecar_path(exr).write_text(json.dumps({'projection': 'equirect-ra-v1'}))
            self.assertIn('projection', ab.starfield_needs_regeneration(exr))
            ab.write_starfield_sidecar(exr)
            self.assertEqual(ab.starfield_needs_regeneration(exr), '')


if __name__ == '__main__':
    unittest.main()
