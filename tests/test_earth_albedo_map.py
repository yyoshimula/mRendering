"""地球アルベドマップ（地表 + 雲の 2 層、GIBS 実データ）の検証。

ネットワークは使わず、GIBS の WMTS タイルとカラーマップをモックした fetcher で
取得・逆変換・合成・キャッシュを検証する。Mitsuba 側は「一様値のマップ」が
一様ランバート球と同じ地球照を返すことで raw 読み込み経路を確認する。
"""
import io
import json
import math
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

import earth_albedo_map as eam

COLORMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ColorMaps>
 <ColorMap title="{title}" units="{units}">
  <Entries>
   <ColorMapEntry rgb="0,0,0" transparent="true" sourceValue="[-9999]" value="[-9999]" ref="1"/>
   {entries}
  </Entries>
 </ColorMap>
</ColorMaps>
"""


def _cm(entries, title='t', units='u'):
    body = '\n'.join(f'<ColorMapEntry rgb="{r},{g},{b}" transparent="false" '
                     f'sourceValue="{v}" value="{v}" ref="2"/>' for (r, g, b, v) in entries)
    return COLORMAP_XML.format(title=title, units=units, entries=body)


def _tile_png(rgb):
    img = Image.new('RGB', (eam.GIBS_TILE, eam.GIBS_TILE), tuple(rgb))
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


class ColormapTests(unittest.TestCase):
    def test_parse_and_apply(self):
        lut = eam.parse_colormap(_cm([(10, 20, 30, '[0.2,0.4)'), (40, 50, 60, '[1.0]')]))
        self.assertEqual(set(lut), {(10, 20, 30), (40, 50, 60)})   # no-data は除外
        self.assertAlmostEqual(lut[(10, 20, 30)], 0.3)
        self.assertAlmostEqual(lut[(40, 50, 60)], 1.0)
        rgb = np.array([[[10, 20, 30], [40, 50, 60], [0, 0, 0]]], dtype=np.uint8)
        vals = eam.apply_colormap(rgb, lut)
        np.testing.assert_allclose(vals[0, :2], [0.3, 1.0])
        self.assertTrue(np.isnan(vals[0, 2]))

    def test_physics_helpers(self):
        self.assertAlmostEqual(float(eam.cloud_albedo_from_tau(np.array(10.0))), 1.5 / 3.5)
        self.assertAlmostEqual(float(eam.cloud_albedo_from_tau(np.array(0.0))), 0.0)
        a = eam.composite_albedo(np.array(0.1), np.array(0.5), np.array(0.6), atm_albedo=0.07)
        self.assertAlmostEqual(float(a), 0.5 * 0.6 + 0.5 * (0.07 + 0.93 ** 2 * 0.1))
        # 面積重み平均: 上下対称の一様マップは値そのもの
        self.assertAlmostEqual(eam.area_weighted_mean(np.full((8, 16), 0.3)), 0.3)
        self.assertEqual(eam.jd_to_date(2451545.0), '2000-01-01')      # J2000.0 = 正午
        self.assertEqual(eam.jd_to_date(2461090.5), '2026-02-19')      # .5 = 0h UTC
        # BMNG 較正: sRGB 0.5 グレー → Y=0.214 → 0.06 + 1.2·(0.214−0.01) ≈ 0.305
        g = np.full((1, 1, 3), 128, dtype=np.uint8)
        self.assertAlmostEqual(float(eam.surface_albedo_from_bmng(g)[0, 0]), 0.3052, delta=0.002)


class BuildMapTests(unittest.TestCase):
    def test_build_with_mock_gibs_and_cache(self):
        calls = []
        cloud_rgb = {0: (0, 0, 255), 1: (200, 200, 200)}   # 西半球 曇り / 東半球 晴れ

        def fetch(url):
            calls.append(url)
            if url.endswith('Cloud_Fraction_Day.xml'):
                return _cm([(0, 0, 255, '[0.8,0.9)'), (200, 200, 200, '[0.0,0.1)')]).encode()
            if url.endswith('Optical_Thickness.xml'):
                return _cm([(255, 0, 0, '[20,21)')]).encode()
            if 'Cloud_Fraction_Day/default/' in url:
                col = int(url[:-len('.png')].rsplit('/', 1)[-1])
                return _tile_png(cloud_rgb[col])
            if 'Optical_Thickness/default/' in url:
                return _tile_png((255, 0, 0))
            return None

        with tempfile.TemporaryDirectory() as tmp:
            bmng = Path(tmp) / 'bmng.png'
            Image.new('RGB', (64, 32), (128, 128, 128)).save(bmng)
            cfg = eam.AlbedoMapConfig(
                date='2026-03-20', level=0, cloud_layer='X_Cloud_Fraction_Day',
                tau_layer='X_Optical_Thickness', surface_source='bmng',
                bmng_path=str(bmng), cache_dir=Path(tmp) / 'cache')
            path, stats = eam.build_albedo_map(cfg, fetch=fetch)
            self.assertTrue(path.exists())
            arr = np.asarray(Image.open(path)).astype(np.float64) / 65535.0
            self.assertEqual(arr.shape, (512, 1024))
            a_s = 0.06 + 1.2 * (eam.srgb_to_linear(np.array(128 / 255.0)) - 0.01)
            clear = 0.07 + 0.93 ** 2 * a_s
            ac = 20.5 * 0.15 / (2 + 20.5 * 0.15)
            west = 0.85 * ac + 0.15 * clear
            east = 0.05 * ac + 0.95 * clear
            self.assertAlmostEqual(float(arr[256, 100]), west, delta=2e-4)
            self.assertAlmostEqual(float(arr[256, 900]), east, delta=2e-4)
            self.assertEqual(stats['cloud_source'], 'X_Cloud_Fraction_Day+X_Optical_Thickness')
            self.assertEqual(stats['surface_source'], 'bmng')
            self.assertAlmostEqual(stats['global_mean_cloud_fraction'], 0.45, delta=1e-6)
            self.assertAlmostEqual(stats['global_mean_albedo'], 0.5 * (west + east), delta=1e-3)
            n_first = len(calls)
            self.assertGreater(n_first, 0)
            # 2 回目: PNG + JSON があるので取得なし
            path2, stats2 = eam.build_albedo_map(cfg, fetch=fetch)
            self.assertEqual(path2, path)
            self.assertEqual(len(calls), n_first)
            # force 再生成でもタイル/カラーマップはディスクキャッシュから（取得なし）
            eam.build_albedo_map(cfg, fetch=fetch, force=True)
            self.assertEqual(len(calls), n_first)
            self.assertTrue(json.loads(path.with_suffix('.json').read_text())['global_mean_albedo'] > 0)

    def test_missing_cloud_layer_falls_back_to_clear_sky(self):
        with tempfile.TemporaryDirectory() as tmp:
            bmng = Path(tmp) / 'bmng.png'
            Image.new('RGB', (64, 32), (20, 40, 90)).save(bmng)   # 海っぽい暗色
            cfg = eam.AlbedoMapConfig(date='2026-03-20', level=0, cloud_layer='nope',
                                      tau_layer=None, bmng_path=str(bmng),
                                      cache_dir=Path(tmp) / 'cache')
            path, stats = eam.build_albedo_map(cfg, fetch=lambda url: None)
            self.assertEqual(stats['cloud_source'], 'none')
            arr = np.asarray(Image.open(path)).astype(np.float64) / 65535.0
            # 雲なし: A = 0.07 + 0.93²·α_s、α_s は BMNG 較正（暗い海色 → ≈0.077）
            a_s = float(eam.surface_albedo_from_bmng(
                np.array([[[20, 40, 90]]], dtype=np.uint8))[0, 0])
            self.assertLess(a_s, 0.1)
            self.assertAlmostEqual(float(arr.mean()), 0.07 + 0.93 ** 2 * a_s, delta=2e-3)

    def test_resolve_prefers_explicit_path(self):
        import argparse
        args = argparse.Namespace(earth_albedo_map='/x/y.png', earth_albedo_gibs=True)
        self.assertEqual(eam.resolve_albedo_map(args, 2451545.0), '/x/y.png')
        self.assertIsNone(eam.resolve_albedo_map(argparse.Namespace(), None))


class MitsubaAlbedoMapTests(unittest.TestCase):
    def test_uniform_map_matches_uniform_sphere(self):
        import mitsuba as mi
        import pv_irradiance as pv
        import relative_motion as rm
        from tools import pv_earthshine_reference as ref
        H, sz = 550.0, 60.0
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / 'albedo.png'
            Image.fromarray(np.full((32, 64), round(0.3 * 65535), dtype=np.uint16)).save(png)
            sun_dir = np.array([0.0, math.sin(math.radians(sz)), math.cos(math.radians(sz))])
            results = {}
            for label, kw in (('map', {'albedo_map': str(png)}), ('uniform', {'uniform_albedo': 0.3})):
                d = {'type': 'scene', 'integrator': {'type': 'path', 'max_depth': 8},
                     'sun': {'type': 'directional', 'direction': (-sun_dir).tolist(),
                             'irradiance': {'type': 'rgb', 'value': [5.0, 5.0, 4.8]}}}
                d.update(rm.create_earth_backdrop([0, 0, 0], [0, 0, -1], H, 'unused.jpg', **kw))
                panel = pv.PvPanel(name='n', kind='rect', center=(0, 0, 0), normal=(0, 0, -1),
                                   up=(0, 1, 0), size=(1.0, 1.0))
                pv.add_pv_sensors(d, [panel], {'deputy': mi.ScalarTransform4f.translate([0, 0, 0])},
                                  spp=8192)
                full, black, direct = pv.pv_scene_dicts(d)
                from asset_cache import preload_bitmaps, share_bsdf_textures
                load = lambda x: mi.load_dict(preload_bitmaps(share_bsdf_textures(x)))  # noqa: E731
                m = pv.measure_panels(load(full), load(black), load(direct), [panel],
                                      sun_dir_scene=sun_dir, sun_rgb_frame=[5.0, 5.0, 4.8],
                                      sun_rgb_base=[5.0, 5.0, 4.8], s0_wm2=1361.0, nu=1.0,
                                      spp=8192, direct_samples=256)[0]
                results[label] = m.e_earth
            self.assertAlmostEqual(results['map'] / results['uniform'], 1.0, delta=0.015)
            expect = ref.earthshine_irradiance(H, sz, (0, 0, -1), ref.lambertian(0.3))
            self.assertAlmostEqual(results['map'] / expect, 1.0, delta=0.03)


if __name__ == '__main__':
    unittest.main()
