#!/usr/bin/env python3
"""地球の実効アルベドマップ（2 層モデル: 地表 + 雲）を実データから生成する。

PV 計測（pv_irradiance.py）と groundobs の地球照の光源として、「陸・海・雲で
反射率が違う」空間分布を持つ地球を作るためのツール。出力は equirect
（u = 経度 −180°→180°、v = 北極→南極）の **リニア 16-bit グレースケール PNG**
（値 = アルベド × 65535）で、`create_earth_backdrop(albedo_map=...)` が
Mitsuba の bitmap（raw=True、sRGB 変換なし）として地球球体に貼る。

モデル（画素ごと、[Donohoe & Battisti 2011] の分解に倣う）:
    A = f_c · α_c(τ)  +  (1 − f_c) · [ α_atm + (1 − α_atm)² · α_s ]
      f_c   : 雲分率（MODIS MOD06、GIBS の Cloud Fraction レイヤ）
      α_c   : 雲アルベド。雲光学的厚さ τ から二流近似 α_c = τ(1−g)/(2 + τ(1−g))
              （g = 0.85 の非対称因子。τ=10 → 0.43、τ=30 → 0.69）。
              τ レイヤが無ければ定数 cloud_albedo（既定 0.55）
      α_atm : 晴天大気（レイリー + エアロゾル）の反射（既定 0.07）。(1−α_atm)² は
              往復の透過近似
      α_s   : 地表アルベド。MODIS MCD43 白空アルベド（GIBS レイヤ、要 id 確認）
              または BMNG 昼テクスチャの輝度からの較正推定（後述）
    全球平均が CERES の ~0.29〜0.30 になるかを stats で確認できる。

データ源（NASA GIBS WMTS、認証不要。帰属表記: "NASA GIBS / Worldview"）:
    タイル: https://gibs.earthdata.nasa.gov/wmts/epsg4326/best/{layer}/default/
            {date}/{tms}/{z}/{row}/{col}.png
    科学レイヤはパレット PNG なので、同サービスのカラーマップ XML
    (https://gibs.earthdata.nasa.gov/colormaps/v1.3/{layer}.xml) で
    RGB → 物理値に逆変換する（ColorMapEntry の rgb / value 属性）。
    レイヤ id・TileMatrixSet 名は GetCapabilities で確認できる:
        python earth_albedo_map.py --list-layers Cloud_Fraction Albedo
    既定 id は MODIS Terra の Cloud Fraction (Day) / Cloud Optical Thickness。
    地表アルベドの MODIS レイヤは環境から確認できなかったため既定は BMNG 推定
    （`--surface-source gibs --surface-layer <id>` で切替）。

BMNG からの地表アルベド推定（surface_source=bmng）:
    昼テクスチャの sRGB → リニア輝度 Y をとり α_s = clip(a0 + gain·(Y − y0))。
    既定 a0=0.06（海）、y0=0.01（海の Y）、gain=1.2（サハラ Y≈0.25 → 0.35）。
    表示用合成画像の較正なので ±0.05 程度の粗さがある。MODIS 白空アルベドが
    使えるならそちらを優先すること。

使い方:
    # 2026-03-20 の雲込みアルベドマップを生成（runs/_gibs_cache/albedo/ にキャッシュ）
    python earth_albedo_map.py --date 2026-03-20
    # relative / groundobs で使う（epoch の日付で自動生成するなら --earth-albedo-gibs）
    python mrender.py relative --config presets/pv_hubble_orbit.yaml --earth-albedo-gibs
    python mrender.py groundobs --config presets/groundobs_hubble.yaml \\
        --earth-albedo-map runs/_gibs_cache/albedo/albedo_2026-03-20_L2.png
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parent
GIBS_WMTS = 'https://gibs.earthdata.nasa.gov/wmts/epsg4326/best'
GIBS_COLORMAP = 'https://gibs.earthdata.nasa.gov/colormaps/v1.3/{layer}.xml'
GIBS_CAPABILITIES = f'{GIBS_WMTS}/1.0.0/WMTSCapabilities.xml'
GIBS_TILE = 512
CACHE_DIR = ROOT / 'runs' / '_gibs_cache'

Fetcher = Callable[[str], Optional[bytes]]


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------
def http_fetch(url: str, timeout: float = 30.0, retries: int = 2) -> Optional[bytes]:
    """URL を取得して bytes を返す。失敗時 None（呼び出し側で欠損扱い）。"""
    import urllib.request
    req = urllib.request.Request(url, headers={'User-Agent': 'mrender/1.0'})
    for _ in range(max(1, retries)):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception:  # noqa: BLE001 - リトライ後は欠損扱い
            continue
    return None


def gibs_grid_size(level: int) -> Tuple[int, int]:
    """EPSG:4326 タイル行列 level の全球画素数 (W, H)。level 0 = 1024×512。"""
    return GIBS_TILE * 2 ** (level + 1), GIBS_TILE * 2 ** level


def fetch_gibs_layer(layer: str, date: str, level: int, tms: str,
                     fetch: Fetcher = http_fetch, cache_dir: Path = CACHE_DIR,
                     offline: bool = False, workers: int = 12) -> Optional[np.ndarray]:
    """パレット PNG レイヤの全球タイルを取得し RGB (H, W, 3) uint8 を返す。

    タイルは cache_dir/<layer>/<date>/z<z>_r<row>_c<col>.png に永続キャッシュ。
    1 枚も取れなければ None。欠損タイルは (0,0,0)（カラーマップで no-data 扱い）。
    """
    W, H = gibs_grid_size(level)
    n_rows, n_cols = H // GIBS_TILE, W // GIBS_TILE
    cache = cache_dir / layer / date
    cache.mkdir(parents=True, exist_ok=True)
    tiles = [(r, c) for r in range(n_rows) for c in range(n_cols)]

    def get(t):
        r, c = t
        path = cache / f'z{level}_r{r}_c{c}.png'
        if path.exists() and path.stat().st_size > 0:
            return t, path
        if offline:
            return t, None
        url = (f'{GIBS_WMTS}/{layer}/default/{date}/{tms}/{level}/{r}/{c}.png')
        data = fetch(url)
        if not data:
            return t, None
        tmp = path.with_suffix('.part')
        tmp.write_bytes(data)
        tmp.rename(path)
        return t, path

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = dict(pool.map(get, tiles))
    ok = [p for p in results.values() if p is not None]
    if not ok:
        return None
    if len(ok) < len(tiles):
        print(f'  gibs: {layer} {date}: {len(tiles) - len(ok)}/{len(tiles)} タイル欠損',
              file=sys.stderr)
    from PIL import Image
    out = np.zeros((H, W, 3), dtype=np.uint8)
    for (r, c), path in results.items():
        if path is None:
            continue
        ta = np.asarray(Image.open(path).convert('RGB'))
        out[r * GIBS_TILE:(r + 1) * GIBS_TILE, c * GIBS_TILE:(c + 1) * GIBS_TILE] = \
            ta[:GIBS_TILE, :GIBS_TILE]
    return out


# ---------------------------------------------------------------------------
# カラーマップ（RGB → 物理値）
# ---------------------------------------------------------------------------
_NUM = re.compile(r'[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?')


def parse_colormap(xml_text: str) -> Dict[Tuple[int, int, int], float]:
    """GIBS カラーマップ XML → {(r,g,b): 値} を返す。

    `<ColorMapEntry rgb="r,g,b" value="[a,b)" ...>` の値区間は中点、単一値は
    そのまま。transparent="true" や no-data 項目（数値なし）は除外する。
    XML の名前空間・バージョン差に依存しないよう属性名だけで走査する。
    """
    lut: Dict[Tuple[int, int, int], float] = {}
    root = ET.fromstring(xml_text)
    for el in root.iter():
        rgb = el.attrib.get('rgb')
        if not rgb:
            continue
        if str(el.attrib.get('transparent', 'false')).lower() == 'true':
            continue
        vtxt = el.attrib.get('value') or el.attrib.get('sourceValue') or ''
        nums = [float(x) for x in _NUM.findall(vtxt)]
        if not nums:
            continue
        val = nums[0] if len(nums) == 1 else 0.5 * (nums[0] + nums[1])
        try:
            r, g, b = (int(x) for x in rgb.split(','))
        except ValueError:
            continue
        lut[(r, g, b)] = val
    return lut


def apply_colormap(rgb: np.ndarray, lut: Dict[Tuple[int, int, int], float]) -> np.ndarray:
    """RGB 画像 (H,W,3) を LUT で物理値 (H,W) に変換。未登録色は NaN。"""
    key = (rgb[..., 0].astype(np.int64) << 16) | (rgb[..., 1].astype(np.int64) << 8) \
        | rgb[..., 2].astype(np.int64)
    keys = np.array([(r << 16) | (g << 8) | b for (r, g, b) in lut], dtype=np.int64)
    vals = np.array(list(lut.values()), dtype=np.float64)
    order = np.argsort(keys)
    keys, vals = keys[order], vals[order]
    idx = np.searchsorted(keys, key)
    idx = np.clip(idx, 0, len(keys) - 1)
    hit = keys[idx] == key
    out = np.full(key.shape, np.nan, dtype=np.float64)
    out[hit] = vals[idx[hit]]
    return out


def load_gibs_values(layer: str, date: str, level: int, tms: str,
                     fetch: Fetcher = http_fetch, cache_dir: Path = CACHE_DIR,
                     offline: bool = False) -> Optional[np.ndarray]:
    """GIBS 科学レイヤの物理値マップ (H, W)（NaN = 欠損）。カラーマップもキャッシュ。"""
    cm_path = cache_dir / layer / 'colormap.xml'
    if cm_path.exists():
        xml_text = cm_path.read_text()
    else:
        if offline:
            return None
        data = fetch(GIBS_COLORMAP.format(layer=layer))
        if not data:
            print(f'  gibs: {layer} のカラーマップを取得できません', file=sys.stderr)
            return None
        cm_path.parent.mkdir(parents=True, exist_ok=True)
        cm_path.write_bytes(data)
        xml_text = data.decode('utf-8', errors='replace')
    lut = parse_colormap(xml_text)
    if not lut:
        print(f'  gibs: {layer} のカラーマップに数値項目がありません', file=sys.stderr)
        return None
    rgb = fetch_gibs_layer(layer, date, level, tms, fetch, cache_dir, offline)
    if rgb is None:
        return None
    return apply_colormap(rgb, lut)


def list_layers(keywords, fetch: Fetcher = http_fetch) -> list:
    """GetCapabilities からレイヤ id と TileMatrixSet を列挙（キーワード部分一致）。"""
    data = fetch(GIBS_CAPABILITIES)
    if not data:
        raise RuntimeError('GIBS GetCapabilities を取得できません')
    root = ET.fromstring(data)
    out = []
    for layer in root.iter():
        if not layer.tag.endswith('}Layer') and layer.tag != 'Layer':
            continue
        ident = None
        tms = []
        for el in layer.iter():
            if el.tag.endswith('Identifier') and ident is None and el.text:
                ident = el.text.strip()
            if el.tag.endswith('}TileMatrixSet') or el.tag == 'TileMatrixSet':
                if el.text:
                    tms.append(el.text.strip())
        if ident and (not keywords or any(k.lower() in ident.lower() for k in keywords)):
            out.append((ident, sorted(set(tms))))
    return out


# ---------------------------------------------------------------------------
# 物理モデル
# ---------------------------------------------------------------------------
def cloud_albedo_from_tau(tau: np.ndarray, g: float = 0.85) -> np.ndarray:
    """二流近似の雲アルベド α_c = τ(1−g) / (2 + τ(1−g))（保存散乱、直上入射相当）。"""
    t = np.maximum(np.asarray(tau, dtype=np.float64), 0.0) * (1.0 - g)
    return t / (2.0 + t)


def srgb_to_linear(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def surface_albedo_from_bmng(rgb: np.ndarray, a0: float = 0.06, y0: float = 0.01,
                             gain: float = 1.2, lo: float = 0.03, hi: float = 0.9
                             ) -> np.ndarray:
    """BMNG 昼テクスチャ (H,W,3) uint8 → 地表アルベドの較正推定 (H,W)。"""
    lin = srgb_to_linear(rgb.astype(np.float64) / 255.0)
    y = lin @ np.array([0.2126, 0.7152, 0.0722])
    return np.clip(a0 + gain * (y - y0), lo, hi)


def composite_albedo(surface: np.ndarray, cloud_fraction: np.ndarray,
                     cloud_albedo: np.ndarray, atm_albedo: float = 0.07) -> np.ndarray:
    """A = f·α_c + (1−f)·[α_atm + (1−α_atm)²·α_s]。NaN の入力は保守的に埋める。"""
    f = np.nan_to_num(np.asarray(cloud_fraction, dtype=np.float64), nan=0.0)
    f = np.clip(f, 0.0, 1.0)
    ac = np.asarray(cloud_albedo, dtype=np.float64)
    ac = np.where(np.isfinite(ac), ac, 0.55)
    s = np.asarray(surface, dtype=np.float64)
    s = np.where(np.isfinite(s), s, 0.06)
    clear = atm_albedo + (1.0 - atm_albedo) ** 2 * s
    return np.clip(f * ac + (1.0 - f) * clear, 0.0, 1.0)


def area_weighted_mean(a: np.ndarray) -> float:
    """equirect マップの緯度 cos 重み付き全球平均。"""
    h = a.shape[0]
    lat = (0.5 - (np.arange(h) + 0.5) / h) * math.pi
    w = np.cos(lat)[:, None] * np.ones_like(a)
    return float(np.nansum(a * w) / np.sum(w))


def resize_nearest(a: np.ndarray, shape: Tuple[int, int]) -> np.ndarray:
    """(H,W[,3]) → shape に最近傍リサンプル（マスク/分率の混色を避ける）。"""
    H, W = a.shape[:2]
    rows = (np.arange(shape[0]) * H / shape[0]).astype(int)
    cols = (np.arange(shape[1]) * W / shape[1]).astype(int)
    return a[rows][:, cols]


def resize_box(a: np.ndarray, shape: Tuple[int, int]) -> np.ndarray:
    """(H,W) → 縮小は面積平均（BOX）、拡大は最近傍。"""
    H, W = a.shape[:2]
    if shape[0] >= H or shape[1] >= W:
        return resize_nearest(a, shape)
    from PIL import Image
    img = Image.fromarray(np.asarray(a, dtype=np.float32), mode='F')
    return np.asarray(img.resize((shape[1], shape[0]), Image.BOX), dtype=np.float64)


# ---------------------------------------------------------------------------
# マップ生成
# ---------------------------------------------------------------------------
@dataclass
class AlbedoMapConfig:
    date: str
    level: int = 2                       # GIBS level: 2 = 4096×2048 (~10 km/px), 3 = 8192×4096
    cloud_layer: str = 'MODIS_Terra_Cloud_Fraction_Day'
    cloud_tms: str = '2km'
    tau_layer: Optional[str] = 'MODIS_Terra_Cloud_Optical_Thickness'
    tau_tms: str = '2km'
    surface_source: str = 'bmng'         # 'bmng' | 'gibs'
    surface_layer: Optional[str] = None  # surface_source='gibs' のレイヤ id（要確認）
    surface_tms: str = '500m'
    bmng_path: str = 'assets/textures/earth_day.jpg'
    cloud_albedo: float = 0.55           # τ レイヤ無し時の定数
    asymmetry_g: float = 0.85
    atm_albedo: float = 0.07
    ocean_albedo: float = 0.06
    bmng_gain: float = 1.2
    offline: bool = False
    cache_dir: Path = field(default_factory=lambda: CACHE_DIR)


def default_map_path(cfg: AlbedoMapConfig) -> Path:
    return cfg.cache_dir / 'albedo' / f'albedo_{cfg.date}_L{cfg.level}.png'


def build_albedo_map(cfg: AlbedoMapConfig, out_path: Optional[Path] = None,
                     fetch: Fetcher = http_fetch, force: bool = False
                     ) -> Tuple[Path, dict]:
    """アルベドマップ PNG と stats JSON を生成して (png_path, stats) を返す。

    既存の PNG があれば（force でなければ）再生成せず sidecar の stats を返す。
    雲データが取れない場合は例外ではなく雲なし（f_c = 0）で作り、stats に
    'cloud_source': 'none' を記す（呼び出し側は警告して続行できる）。
    """
    out_path = Path(out_path) if out_path else default_map_path(cfg)
    stats_path = out_path.with_suffix('.json')
    if out_path.exists() and stats_path.exists() and not force:
        return out_path, json.loads(stats_path.read_text())

    W, H = gibs_grid_size(cfg.level)
    shape = (H, W)

    # --- 地表アルベド ---
    surface_src = cfg.surface_source
    surface = None
    if cfg.surface_source == 'gibs' and cfg.surface_layer:
        vals = load_gibs_values(cfg.surface_layer, cfg.date, cfg.level, cfg.surface_tms,
                                fetch, cfg.cache_dir, cfg.offline)
        if vals is not None:
            # MODIS アルベドは陸のみ（海は欠損）→ 海は定数
            surface = np.where(np.isfinite(vals), vals, cfg.ocean_albedo)
        else:
            print(f'  albedo: 地表レイヤ {cfg.surface_layer} を取得できず BMNG 推定へ',
                  file=sys.stderr)
    if surface is None:
        surface_src = 'bmng'
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        p = Path(cfg.bmng_path)
        if not p.is_absolute():
            p = ROOT / p
        if not p.exists():
            p = ROOT / 'earth_texture.jpg'
        img = Image.open(p).convert('RGB')
        img = img.resize((W, H), Image.BOX)
        surface = surface_albedo_from_bmng(np.asarray(img), a0=cfg.ocean_albedo,
                                           gain=cfg.bmng_gain)

    # --- 雲 ---
    cloud_src = 'none'
    frac = np.zeros(shape)
    ac = np.full(shape, cfg.cloud_albedo)
    fvals = load_gibs_values(cfg.cloud_layer, cfg.date, cfg.level, cfg.cloud_tms,
                             fetch, cfg.cache_dir, cfg.offline)
    if fvals is not None:
        cloud_src = cfg.cloud_layer
        if np.nanmax(fvals) > 1.5:      # 百分率で来るレイヤに備える
            fvals = fvals / 100.0
        frac = np.clip(np.nan_to_num(resize_box(fvals, shape), nan=0.0), 0.0, 1.0)
        if cfg.tau_layer:
            tvals = load_gibs_values(cfg.tau_layer, cfg.date, cfg.level, cfg.tau_tms,
                                     fetch, cfg.cache_dir, cfg.offline)
            if tvals is not None:
                t = resize_box(np.nan_to_num(tvals, nan=np.nan), shape)
                ac_tau = cloud_albedo_from_tau(t, cfg.asymmetry_g)
                ac = np.where(np.isfinite(t) & (t > 0), ac_tau, cfg.cloud_albedo)
                cloud_src += f'+{cfg.tau_layer}'
    else:
        print(f'  albedo: 雲レイヤ {cfg.cloud_layer} {cfg.date} を取得できず雲なしで生成',
              file=sys.stderr)

    A = composite_albedo(surface, frac, ac, cfg.atm_albedo)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    Image.fromarray(np.round(A * 65535.0).astype(np.uint16)).save(str(out_path))
    stats = {
        'date': cfg.date, 'level': cfg.level, 'width': W, 'height': H,
        'surface_source': surface_src, 'cloud_source': cloud_src,
        'model': 'A = f*alpha_c + (1-f)*(alpha_atm + (1-alpha_atm)^2*alpha_s)',
        'atm_albedo': cfg.atm_albedo, 'ocean_albedo': cfg.ocean_albedo,
        'global_mean_albedo': area_weighted_mean(A),
        'global_mean_surface_albedo': area_weighted_mean(surface),
        'global_mean_cloud_fraction': area_weighted_mean(frac),
        'global_mean_cloud_albedo': area_weighted_mean(np.where(frac > 0, ac, np.nan)),
        'attribution': 'NASA GIBS / Worldview (MODIS); NASA Blue Marble Next Generation',
    }
    stats_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False))
    return out_path, stats


def albedo_map_for_date(date: str, **kw) -> Tuple[Path, dict]:
    """verb から呼ぶ便利関数: 日付のマップをキャッシュ付きで返す。"""
    cfg = AlbedoMapConfig(date=str(date), **kw)
    return build_albedo_map(cfg)


def jd_to_date(jd: float) -> str:
    """ユリウス日 → UTC 日付 'YYYY-MM-DD'。"""
    from datetime import datetime, timedelta, timezone
    t = datetime(2000, 1, 1, 12, tzinfo=timezone.utc) + timedelta(days=float(jd) - 2451545.0)
    return t.date().isoformat()


_REPORTED: set = set()


def resolve_albedo_map(args, jd: Optional[float] = None) -> Optional[str]:
    """verb の args から地球アルベドマップのパスを決める（無ければ None）。

    優先順: `earth_albedo_map`（既製 PNG）> `earth_albedo_gibs`（日付で自動生成、
    日付は `earth_albedo_date` > jd > `epoch_utc`）。自動生成は日付ごとに
    キャッシュされ、stats は日付ごとに 1 回だけ表示する。
    """
    path = getattr(args, 'earth_albedo_map', None)
    if path:
        return str(path)
    if not getattr(args, 'earth_albedo_gibs', False):
        return None
    date = getattr(args, 'earth_albedo_date', None)
    if not date:
        if jd is not None:
            date = jd_to_date(jd)
        else:
            date = str(getattr(args, 'epoch_utc', '') or '')[:10]
    if not date:
        from datetime import datetime, timedelta, timezone
        date = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    level = int(getattr(args, 'earth_albedo_level', 2) or 2)
    p, stats = albedo_map_for_date(date, level=level,
                                   offline=bool(getattr(args, 'earth_albedo_offline', False)))
    if str(p) not in _REPORTED:
        _REPORTED.add(str(p))
        print(f"  地球アルベドマップ: {p} (地表={stats['surface_source']}, "
              f"雲={stats['cloud_source']}, 全球平均 {stats['global_mean_albedo']:.3f}, "
              f"雲分率 {stats['global_mean_cloud_fraction']:.2f})")
    return str(p)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description='地球アルベドマップ（地表 + 雲の 2 層）を実データから生成')
    ap.add_argument('--date', type=str, default=None, help='YYYY-MM-DD（既定: 昨日 UTC）')
    ap.add_argument('--out', type=str, default=None, help='出力 PNG（既定: runs/_gibs_cache/albedo/）')
    ap.add_argument('--level', type=int, default=2, help='GIBS level（2: 4096×2048, 3: 8192×4096）')
    ap.add_argument('--cloud-layer', type=str, default='MODIS_Terra_Cloud_Fraction_Day')
    ap.add_argument('--cloud-tms', type=str, default='2km')
    ap.add_argument('--tau-layer', type=str, default='MODIS_Terra_Cloud_Optical_Thickness',
                    help="雲光学的厚さレイヤ（'none' で定数 cloud_albedo）")
    ap.add_argument('--tau-tms', type=str, default='2km')
    ap.add_argument('--surface-source', choices=['bmng', 'gibs'], default='bmng')
    ap.add_argument('--surface-layer', type=str, default=None,
                    help='surface_source=gibs の MODIS アルベドレイヤ id（--list-layers Albedo で確認）')
    ap.add_argument('--surface-tms', type=str, default='500m')
    ap.add_argument('--cloud-albedo', type=float, default=0.55)
    ap.add_argument('--atm-albedo', type=float, default=0.07)
    ap.add_argument('--ocean-albedo', type=float, default=0.06)
    ap.add_argument('--offline', action='store_true', help='キャッシュのみ使用（ネットワーク不使用）')
    ap.add_argument('--force', action='store_true', help='既存マップを再生成')
    ap.add_argument('--list-layers', nargs='*', default=None, metavar='KEYWORD',
                    help='GetCapabilities からレイヤ id と TileMatrixSet を列挙して終了')
    a = ap.parse_args(argv)

    if a.list_layers is not None:
        for ident, tms in list_layers(a.list_layers):
            print(f'{ident}\t{",".join(tms)}')
        return

    date = a.date
    if not date:
        from datetime import datetime, timedelta, timezone
        date = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    cfg = AlbedoMapConfig(
        date=date, level=a.level, cloud_layer=a.cloud_layer, cloud_tms=a.cloud_tms,
        tau_layer=None if a.tau_layer in (None, '', 'none') else a.tau_layer,
        tau_tms=a.tau_tms, surface_source=a.surface_source, surface_layer=a.surface_layer,
        surface_tms=a.surface_tms, cloud_albedo=a.cloud_albedo, atm_albedo=a.atm_albedo,
        ocean_albedo=a.ocean_albedo, offline=a.offline)
    path, stats = build_albedo_map(cfg, a.out, force=a.force)
    print(f'アルベドマップ: {path}')
    for k in ('surface_source', 'cloud_source', 'global_mean_albedo',
              'global_mean_surface_albedo', 'global_mean_cloud_fraction',
              'global_mean_cloud_albedo'):
        v = stats[k]
        print(f'  {k}: {v:.4f}' if isinstance(v, float) else f'  {k}: {v}')


if __name__ == '__main__':
    main()
