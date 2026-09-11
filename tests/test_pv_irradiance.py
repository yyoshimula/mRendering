"""PV 計測（pv_irradiance）の検証: 直達の cos/遮蔽、地球照の解析参照一致、CSV 出力。"""
import argparse
import csv
import math
import tempfile
import unittest
from pathlib import Path

import numpy as np

import mitsuba as mi
import pv_irradiance as pv
import relative_motion as rm
from tools import pv_earthshine_reference as ref

S0 = 1361.0
R = ref.R_EARTH_KM
H = 550.0


def _lambert_earth_scene(sun_dir_scene, albedo=0.3, env=None, extra=None):
    """一様ランバート地球（高度 H、地心 −z）+ 天底向きパネル 6 m² の PV シーン。"""
    sun_dir_scene = np.asarray(sun_dir_scene, dtype=float)
    d = {'type': 'scene', 'integrator': {'type': 'path', 'max_depth': 8},
         'sun': {'type': 'directional', 'direction': (-sun_dir_scene).tolist(),
                 'irradiance': {'type': 'rgb', 'value': [5.0, 5.0, 4.8]}}}
    if env is not None:
        d['envmap'] = env
    d.update(rm.create_earth_backdrop([0, 0, 0], [0, 0, -1], H, 'unused.jpg',
                                      uniform_albedo=albedo))
    if extra:
        d.update(extra)
    panel = pv.PvPanel(name='nadir', kind='rect', center=(0, 0, 0), normal=(0, 0, -1),
                       up=(0, 1, 0), size=(2.0, 3.0))
    pv.add_pv_sensors(d, [panel], {'deputy': mi.ScalarTransform4f.translate([0, 0, 0])},
                      spp=16384)
    return d, panel


def _measure(d, panel, sun_dir_scene, include_env=False):
    full, black, direct = pv.pv_scene_dicts(d, include_env)
    return pv.measure_panels(
        mi.load_dict(full), mi.load_dict(black), mi.load_dict(direct), [panel],
        sun_dir_scene=sun_dir_scene, sun_rgb_frame=[5.0, 5.0, 4.8],
        sun_rgb_base=[5.0, 5.0, 4.8], s0_wm2=S0, nu=1.0, spp=16384,
        direct_samples=2048)[0]


class PvIrradianceTests(unittest.TestCase):
    def test_reference_grid_vs_disk_formula(self):
        # ランバート球の厳密値は縁の減光で簡易式より僅かに小さい（550 km で ~1%）
        e = ref.earthshine_irradiance(H, 0.0, (0, 0, -1), ref.lambertian(0.3))
        approx = ref.uniform_disk_estimate(H, 0.3)
        self.assertLess(e, approx)
        self.assertAlmostEqual(e / approx, 1.0, delta=0.015)
        # 明暗境界上ではほぼゼロ、太陽が地平線下なら完全にゼロ
        self.assertLess(ref.earthshine_irradiance(H, 90.0), 0.05 * e)
        self.assertEqual(ref.earthshine_irradiance(H, 135.0), 0.0)

    def test_direct_cosine_and_self_shadowing(self):
        # 太陽と 30° 傾いた 1 m² 板 + 半分を覆う遮蔽板 → E = S0·cos30°·0.5
        blocker = {'blocker': {
            'type': 'rectangle', 'id': 'blocker',
            'to_world': (mi.ScalarTransform4f.translate([0.00025, 0, 0.0005])
                         @ mi.ScalarTransform4f.scale([0.00025, 0.01, 1])),
            'bsdf': {'type': 'diffuse'}}}
        d = {'type': 'scene', 'integrator': {'type': 'path', 'max_depth': 4},
             'sun': {'type': 'directional', 'direction': [0, 0, -1],
                     'irradiance': {'type': 'rgb', 'value': [5.0, 5.0, 4.8]}}}
        d.update(blocker)
        panel = pv.PvPanel(name='p', kind='rect', center=(0, 0, 0), normal=(0, 0, 1),
                           up=(0, 1, 0), size=(1.0, 1.0))
        host = mi.ScalarTransform4f.rotate([1, 0, 0], 30.0)
        pv.add_pv_sensors(d, [panel], {'deputy': host}, spp=256)
        m = _measure(d, panel, [0, 0, 1])
        self.assertAlmostEqual(m.cos_sun, math.cos(math.radians(30)), delta=1e-3)
        self.assertAlmostEqual(m.sun_visible, 0.5, delta=0.03)
        self.assertAlmostEqual(m.e_direct, S0 * math.cos(math.radians(30)) * 0.5,
                               delta=0.03 * S0)
        self.assertAlmostEqual(m.e_earth, 0.0, delta=1e-6)
        self.assertAlmostEqual(m.area_m2, 1.0)

    def test_lambert_earth_matches_analytic_reference(self):
        for sz_deg in (0.0, 60.0):
            with self.subTest(sun_zenith_deg=sz_deg):
                sz = math.radians(sz_deg)
                sun_dir = [0.0, math.sin(sz), math.cos(sz)]  # 太陽の方向（+z = 天頂）
                d, panel = _lambert_earth_scene(sun_dir)
                m = _measure(d, panel, sun_dir)
                expect = ref.earthshine_irradiance(H, sz_deg, (0, 0, -1), ref.lambertian(0.3))
                self.assertAlmostEqual(m.e_earth / expect, 1.0, delta=0.02)
                self.assertAlmostEqual(m.e_direct, 0.0)      # 天底向き板に直達なし
                self.assertAlmostEqual(m.e_other, 0.0, delta=1e-6)
                self.assertAlmostEqual(m.power_w, 0.3 * 6.0 * m.e_total, delta=1e-6)

    def test_env_light_is_excluded_by_default_and_not_attributed_to_earth(self):
        env = {'type': 'constant', 'radiance': {'type': 'rgb', 'value': [0.02, 0.02, 0.02]}}
        sun_dir = [0.0, 0.0, 1.0]
        d, panel = _lambert_earth_scene(sun_dir, env=env)
        m_off = _measure(d, panel, sun_dir, include_env=False)
        m_on = _measure(d, panel, sun_dir, include_env=True)
        self.assertAlmostEqual(m_off.e_other, 0.0, delta=1e-6)
        # 環境光を含めても地球照はほぼ不変（地球黒体差分なので地球の遮蔽分は
        # 混ざらない。地球が環境光を反射した分 ~1% だけ増えるのは物理的に正しい）
        self.assertGreater(m_on.e_earth, m_off.e_earth)
        self.assertAlmostEqual(m_on.e_earth / m_off.e_earth, 1.0, delta=0.03)
        # 天底板の上半球は地球に遮られるので、環境光は π·L·S0/lum(sun) より小さい
        full_hemisphere = math.pi * 0.02 * S0 / pv.luminance([5.0, 5.0, 4.8])
        self.assertGreater(m_on.e_other, 0.0)
        self.assertLess(m_on.e_other, 0.5 * full_hemisphere)

    def test_panels_from_args_parsing(self):
        args = argparse.Namespace(
            pv_panels=[{'name': 'a', 'center': [0, 1, 0], 'normal': [0, 1, 0], 'size': [2, 3]},
                       {'name': 'b', 'part': 'hbltel_wfc_1', 'host': 'chief'}],
            pv_parts=['hbltel_1'], pv_host='deputy', pv_efficiency=0.2,
            pv_panel_size=[1, 1], pv_panel_center=[0, 0, 0], pv_panel_normal=[0, 0, 1],
            pv_panel_up=[0, 1, 0], pv_panel_name='cli')
        panels = pv.panels_from_args(args)
        self.assertEqual([p.name for p in panels], ['a', 'b', 'hbltel_1', 'cli'])
        self.assertEqual(panels[0].area_m2, 6.0)
        self.assertEqual(panels[1].scene_key, 'chief_part_hbltel_wfc_1')
        self.assertEqual(panels[2].scene_key, 'deputy_part_hbltel_1')
        self.assertEqual(panels[3].efficiency, 0.2)
        self.assertEqual(pv.panels_from_args(argparse.Namespace()), [])
        with self.assertRaises(ValueError):   # groundobs は host=target のみ
            pv.panels_from_args(args, hosts=('target',))
        args.pv_panel_name = 'a'
        with self.assertRaises(ValueError):
            pv.panels_from_args(args)

    def test_sun_override_keeps_earthshine_during_eclipse(self):
        # absolute モード相当: 画像用の太陽に ν=0 が掛かっていても、PV シーンでは
        # ν 無しの太陽に戻すので地球の昼側からの地球照が残る
        sun_dir = [0.0, math.sin(math.radians(60)), math.cos(math.radians(60))]
        d, panel = _lambert_earth_scene(sun_dir)
        d['sun']['irradiance']['value'] = [0.0, 0.0, 0.0]        # ν = 0
        full, black, direct = pv.pv_scene_dicts(d, sun_rgb=[5.0, 5.0, 4.8])
        m = pv.measure_panels(
            mi.load_dict(full), mi.load_dict(black), mi.load_dict(direct), [panel],
            sun_dir_scene=sun_dir, sun_rgb_frame=[0.0, 0.0, 0.0],
            sun_rgb_base=[5.0, 5.0, 4.8], s0_wm2=S0, nu=0.0, spp=8192,
            direct_samples=512)[0]
        expect = ref.earthshine_irradiance(H, 60.0, (0, 0, -1), ref.lambertian(0.3))
        self.assertAlmostEqual(m.e_earth / expect, 1.0, delta=0.03)
        self.assertEqual(m.e_direct, 0.0)
        self.assertEqual(m.nu, 0.0)

    def test_groundobs_earthshine_flag_brightens_lightcurve(self):
        # 物体が昼側の地球の上にいる幾何（観測地も昼）では、--earthshine で観測像の
        # 開口面照度が増える = observation.csv のライトカーブに地球照が乗る
        import ground_observation as go
        base = ['--frames', '1', '--altitude-km', str(H), '--attitude-mode', 'nadir',
                '--sphere-radius-m', '1.0', '--samples', '64', '--sensor-px', '16',
                '--start-overhead', '--seeing-arcsec', '2.0']
        pick = None
        for hh in range(24):
            ep = f'2026-03-20T{hh:02d}:00:00'
            args = go.parse_args(base + ['--epoch-utc', ep])
            g = go.geometry_at(go.build_context(args), 0.0)
            if g.nu > 0.99 and g.sun_el > 30.0:
                pick = ep
                break
        self.assertIsNotNone(pick)
        flux = {}
        for es in (False, True):
            args = go.parse_args(base + ['--epoch-utc', pick] + (['--earthshine'] if es else []))
            res = go.render_observation_frame(args, go.build_context(args), 0)
            flux[es] = float(res.row['irradiance_wm2'])
        # 観測地が昼だと観測者は物体の天底側（太陽と反対側、位相角 ~120°）を見る
        # ので直達の寄与は満月相当の ~7% しかなく、天底半球全体を照らす地球照
        # （~0.26·S0 で相当 ~17%）の方が大きい → 2〜3 倍に増える（実測 2.7 倍）
        self.assertGreater(flux[True], flux[False] * 1.5)
        self.assertLess(flux[True], flux[False] * 8.0)

    def test_groundobs_pv_matches_reference(self):
        import ground_observation as go
        with tempfile.TemporaryDirectory() as tmp:
            args = go.parse_args([
                '--frames', '1', '--altitude-km', str(H), '--attitude-mode', 'nadir',
                '--sphere-radius-m', '1.0', '--samples', '2', '--sensor-px', '16',
                '--epoch-utc', '2026-03-20T12:00:00',
                '--pv-panel-size', '1', '1', '--pv-panel-normal', '0', '0', '1',
                '--pv-panel-center', '0', '0', '2',   # 球（半径 1 m）の外、機体 +z = 天底
                '--pv-samples', '8192', '--pv-direct-samples', '512',
                '--output-dir', tmp])
            ctx = go.build_context(args)
            g = go.geometry_at(ctx, ctx.t_start)
            panels = pv.panels_from_args(args, hosts=('target',))
            m = go.measure_pv_frame(args, ctx, 0, panels)[0]
            # 参照: 物体直下点の太陽天頂角と、天底向き板
            r_hat = g.r_obj / np.linalg.norm(g.r_obj)
            s_hat = g.sun_pos / np.linalg.norm(g.sun_pos)
            sz_deg = math.degrees(math.acos(float(np.clip(r_hat @ s_hat, -1, 1))))
            alt = float(np.linalg.norm(g.r_obj)) - R
            expect = ref.earthshine_irradiance(alt, sz_deg, (0, 0, -1), ref.lambertian(0.3))
            self.assertGreater(expect, 50.0)   # 昼側で意味のある値になる時刻を選んである
            self.assertAlmostEqual(m.e_earth / expect, 1.0, delta=0.03)
            # 天底向き板への直達 = S0·ν·max(0, −r̂·ŝ)
            self.assertAlmostEqual(m.e_direct, S0 * g.nu * max(0.0, float(-r_hat @ s_hat)),
                                   delta=1.0)
            self.assertEqual(m.host, 'target')
            # CSV 出力（_run 経由）
            go._run(args)
            rows = list(csv.DictReader(open(Path(tmp) / 'pv_irradiance.csv', newline='')))
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(float(rows[0]['e_earth_wm2']) / expect, 1.0, delta=0.03)

    def test_run_writes_csv_with_direct_and_earthshine(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'frames'
            args = rm.parse_args([
                '--frames', '2', '--samples', '2', '--width', '32', '--height', '24',
                '--no-inertial-axes', '--no-body-axes', '--hide-chief',
                '--show-earth', '--earth-uniform-albedo', '0.3',
                '--earth-altitude-km', str(H), '--earth-direction', '0', '0', '-1',
                '--sun-direction', '0', '0', '-1',            # 太陽は天頂
                '--pv-panel-size', '2', '3', '--pv-panel-normal', '0', '0', '1',
                '--pv-panel-center', '0', '0', '1500',        # 手続きモデルの外
                '--rel-position', '0.02', '0', '0', '--pv-samples', '4096',
                '--pv-direct-samples', '512', '--output-dir', str(out)])
            rm._run(args)
            csv_path = Path(tmp) / 'pv_irradiance.csv'
            self.assertTrue(csv_path.exists())
            rows = list(csv.DictReader(open(csv_path, newline='')))
            self.assertEqual(len(rows), 2)
            self.assertEqual(list(rows[0].keys()), pv.CSV_COLUMNS)
            r = rows[0]
            self.assertAlmostEqual(float(r['e_direct_wm2']), S0, delta=0.01 * S0)
            self.assertAlmostEqual(float(r['e_earth_wm2']), 0.0, delta=0.5)  # 天頂向きに地球照なし
            self.assertAlmostEqual(float(r['p_w']), 0.3 * 6.0 * float(r['e_total_wm2']), delta=0.01)


if __name__ == '__main__':
    unittest.main()
