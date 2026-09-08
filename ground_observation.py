#!/usr/bin/env python3
"""
地上望遠鏡からの宇宙物体光学観測シミュレーション (groundobs verb)。

地上局 (緯度/経度/標高) から軌道上物体を光学望遠鏡で観測したときの
  1. 明るさ  — 見かけ等級（大気圏外 mag_app / 減光込み mag_obs）
  2. 見え方  — 望遠鏡センサ面の画像（点像〜分解像を同一パイプラインで扱う）
を物理ベースで計算する。

render/lightcurve 系との違い（このモジュールが新規に持つ物理）:
  - 地球自転: 観測地は WGS-84 楕円体上の ECEF 点で、GMST で ECI へ回転する
    （Vallado eq. 3-7 の site 幾何。SatCap satcap/tle/frames.py から移植）
  - 実時刻: UTC エポック (ISO 8601) → ユリウス日。太陽位置は yoshimulib の
    VSOP87 暦、GMST は `orbit.sidereal.gmst`
  - 軌道入力: ケプラー要素 / CSV ephemeris / **TLE (SGP4 伝播)** の 3 系統。
    TLE は sgp4 パッケージで伝播し、TEME ≈ ECI と近似する（両分点差 ~20 分角。
    ポインティング・測光には無視でき、恒星背景の位置ずれも視覚上問題ない）
  - 本影/半影: yoshimulib `shadow()`（Montenbruck & Gill 円錐影）の照射率
    ν∈[0,1] をそのまま太陽放射照度に掛ける（半影で滑らかに減光）
  - 絶対測光: 太陽 directional emitter の放射照度を物理値 S0 [W/m²] で与え、
    レンダ画像（リニア放射輝度）を立体角積分して開口面照度 E [W/m²] を得る。
      m_app = m_sun − 2.5·log10(E / S0)      （m_sun = −26.74, V バンド近似）
    RGB→輝度は Rec.709 係数で重み付けし、太陽 RGB も同じ輝度が S0 になる
    よう正規化してあるので、等級はスペクトル形状の取り方に依存しない
  - 大気: 減光 k·X（X = sec z 平面平行 airmass）とシーイング（ガウス PSF、
    FWHM 指定 [arcsec]）、大気屈折（Sæmundsson 式で幾何仰角→視仰角。
    airmass・可視判定は視仰角で評価）
  - 恒星背景: Hipparcos カタログ (SatCap 由来の npz、固有運動込み) を
    視野内へ投影する。追尾モードに応じて露光中の恒星トレイルも描く
  - 追尾モード: target（物体追尾、既定）= 物体は静止・恒星が流れる /
    sidereal（恒星時追尾）= 恒星は点・物体が露光中にストリークになる
  - センサ: ピクセルスケール [arcsec/px] のセンサ格子に flux 保存で再標本化。
    オプションで CCD ノイズモデル（ショット + 読み出し、SatCap
    satcap/sim/camera.py のノイズ式を物理単位で再実装）

シーン構成（float32 精度対策が本質）:
  - ターゲットを原点に置き、軸は ECI に平行、単位は km
  - カメラは真のレンジ d ではなく代理距離 d_r = min(d, 1000·R_bound) に置く
    （真距離だと GEO で「レイ原点 4e4 km ↔ 物体 1e-3 km」の交差判定が
    float32 の桁落ちで壊れる。太陽は directional なので放射輝度 L は
    距離に不変で、画像・測光とも d_r の取り方に依存しない）
  - レンダ窓の「真の角スケール」は観測者レンジ d で換算し、ピクセルごとの
    放射照度 E_ij = L_ij·ΔΩ_ij をセンサ格子に積み上げる。物体がセンサ
    ピクセルより小さければ全 flux が 1 点に落ち、PSF 畳み込みで自然に
    点像（シーイング円盤）になる。大きければ形が分解される
  - 画像座標系: Mitsuba look_at の実測（列+ = f×up 方向、行+ = −up 方向）に
    合わせて恒星・ストリークを同一基底で投影する（camera_basis 参照）

出力（runs/<ts>_groundobs_<name>/）:
  - frames/frame_%04d.png : センサ像（ストレッチ済み。非可視フレームは暗黒）
  - observation.csv       : frame, time_s, jd_utc, az/el（幾何・視位置）,
                            range, phase angle, airmass, sun_el, ν, visible,
                            視直径, E [W/m²], mag_app, mag_obs
  - --save-linear 指定時は frames/frame_%04d_linear.npy（センサ面照度 W/m²/px）

使い方:
  python mrender.py groundobs --config presets/groundobs_hubble.yaml
  python ground_observation.py --altitude-km 500 --start-overhead --frames 30
  python ground_observation.py --tle input/iss_sample.tle --frames 10

GUI ライブプレビュー: live_worker.build_groundobs_frame が build_context /
render_observation_frame を再利用する（レンダ済み画像を FrameBuild.image01 で
返す特殊経路）。この 2 関数のシグネチャを変えるときは live_worker も追随させること。
"""

import argparse
import csv as _csv
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import numpy as np
from scipy.ndimage import gaussian_filter, zoom as ndi_zoom
from scipy.signal import fftconvolve

# プロジェクトルートを sys.path に追加（yoshimulib のため）
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mitsuba as mi

from mi_variant import init_variant
init_variant(mi)

from yoshimulib.conversion.calendar import gc2jd                      # noqa: E402
from yoshimulib.orbit.sidereal import gmst                            # noqa: E402
from yoshimulib.orbit.transforms import shadow as earth_shadow        # noqa: E402

from orbit_mechanics import (                                         # noqa: E402
    EARTH_RADIUS_KM, EARTH_MU,
    OrbitalElements, CsvEphemeris, oe2rv,
    compute_orbital_position, compute_attitude_matrix,
)
from scene_common import (                                            # noqa: E402
    SCALAR, inertia_matrices, load_model_with_parts, propagate_trajectory,
)
from config_loader import load_flat_yaml_config as load_yaml_config   # noqa: E402
from verb_parsers import build_groundobs_parser as build_parser       # noqa: E402


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
# WGS-84 楕円体（SatCap satcap/tle/frames.py と同値。Vallado 4th ed.）
WGS84_A_KM = 6378.137
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)

SUN_RADIUS_KM = 695700.0        # 太陽半径（食判定用、satellite_orbit.py と同値）
SUN_APPARENT_MAG = -26.74       # 太陽の見かけ等級（V バンド）
ARCSEC_TO_RAD = math.pi / (180.0 * 3600.0)
MAS_TO_RAD = ARCSEC_TO_RAD / 1000.0
FWHM_TO_SIGMA = 1.0 / (2.0 * math.sqrt(2.0 * math.log(2.0)))  # ≈ 1/2.3548

# Rec.709 輝度係数（scene_builder.compute_image_flux と同じ重み）
LUM_WEIGHTS = np.array([0.2126, 0.7152, 0.0722])

# 物理定数（光子エネルギー計算用）
PLANCK_H = 6.62607015e-34       # [J·s]
LIGHT_C = 2.99792458e8          # [m/s]
LAMBDA_EFF_M = 550e-9           # V バンド実効波長 [m]

# 代理カメラ距離の上限倍率: d_r = min(range, R_bound × この値)。
# 球レイ交差の判別式は (R/d)² が float32 eps (1.2e-7) を割ると桁落ちで壊れる
# ため、d/R ≤ 1000（(R/d)² = 1e-6）に制限する。directional 光源のみの
# シーンなので放射輝度は距離不変、結果は d_r の取り方に依存しない。
SURROGATE_RANGE_FACTOR = 1000.0
FOV_MARGIN = 1.5                # レンダ窓 = バウンディング半径 × この係数

# Hipparcos カタログの元期 [ユリウス年]（SatCap catalog.py と同値）
STAR_CATALOG_EPOCH_JYEAR = 1991.25
# トレイル描画の分割数上限（1 分割 = flux/K の点置き）
TRAIL_MAX_STEPS = 1024
# sidereal ストリークの shift-and-add 分割数上限
STREAK_MAX_STEPS = 192


def _luminance(rgb: np.ndarray) -> np.ndarray:
    """RGB (…,3) → Rec.709 輝度。"""
    return rgb @ LUM_WEIGHTS


# ---------------------------------------------------------------------------
# 時刻・太陽
# ---------------------------------------------------------------------------
def parse_epoch_jd(epoch_utc: str) -> float:
    """ISO 8601 UTC 文字列 → ユリウス日。naive は UTC とみなす。"""
    text = epoch_utc.strip().replace('Z', '+00:00')
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    sec = dt.second + dt.microsecond * 1e-6
    return float(gc2jd(dt.year, dt.month, dt.day, dt.hour, dt.minute, sec))


_VSOP_CACHE = None


def sun_position_eci_km(jd: float) -> np.ndarray:
    """VSOP87 による太陽の地心位置 [km]（J2000 ≈ ECI）。係数はモジュール内キャッシュ。

    yoshimulib の `sun_moon.ephemeris.sun()` は内部の `au2km(sun_au, const)`
    呼び出しが定数オブジェクトを AU 値の位置に渡してしまう既知の TypeError を
    持つため、その手前の `sun_lon_lat_r()`（VSOP87 本体、黄経/黄緯/距離）から
    黄道→赤道変換（x 軸まわり +EPS0 回転）を自前で行う。
    """
    global _VSOP_CACHE
    if _VSOP_CACHE is None:
        from yoshimulib.orbit.constants import OrbitalConstants, vsop_const
        from yoshimulib.sun_moon.ephemeris import sun_lon_lat_r
        _VSOP_CACHE = (OrbitalConstants(), vsop_const(), sun_lon_lat_r)
    const, earth_vsop, _sun_lon_lat_r = _VSOP_CACHE
    lon, lat, r_au = _sun_lon_lat_r(np.array([jd]), const, earth_vsop)
    lon = float(np.atleast_1d(lon)[0])
    lat = float(np.atleast_1d(lat)[0])
    r_km = float(np.atleast_1d(r_au)[0]) * const.AU
    # 黄道直交座標
    x_ecl = r_km * math.cos(lat) * math.cos(lon)
    y_ecl = r_km * math.cos(lat) * math.sin(lon)
    z_ecl = r_km * math.sin(lat)
    # 黄道→赤道（J2000 平均黄道傾斜 EPS0、x 軸まわり回転）
    eps = float(const.EPS0)
    return np.array([
        x_ecl,
        y_ecl * math.cos(eps) - z_ecl * math.sin(eps),
        y_ecl * math.sin(eps) + z_ecl * math.cos(eps),
    ])


# ---------------------------------------------------------------------------
# 地上局幾何（WGS-84 / GMST / ENU。SatCap satcap/tle/frames.py 移植）
# ---------------------------------------------------------------------------
def site_ecef_km(lat_deg: float, lon_deg: float, alt_m: float) -> np.ndarray:
    """測地緯度/経度/楕円体高 → WGS-84 ECEF 位置 [km]（Vallado eq. 3-7）。"""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    h_km = alt_m / 1000.0
    sin_lat = math.sin(lat)
    c = WGS84_A_KM / math.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    s = c * (1.0 - WGS84_E2)
    return np.array([
        (c + h_km) * math.cos(lat) * math.cos(lon),
        (c + h_km) * math.cos(lat) * math.sin(lon),
        (s + h_km) * sin_lat,
    ])


def rot_z(theta: float) -> np.ndarray:
    """+z 軸まわりの能動回転行列。r_eci = rot_z(GMST) @ r_ecef。"""
    c, s = math.cos(theta), math.sin(theta)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def enu_matrix(lat_deg: float, lon_deg: float) -> np.ndarray:
    """ECEF ベクトル → 観測地 ENU 成分への変換行列（行 = E, N, U）。"""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    sin_lon, cos_lon = math.sin(lon), math.cos(lon)
    return np.array([
        [-sin_lon, cos_lon, 0.0],
        [-sin_lat * cos_lon, -sin_lat * sin_lon, cos_lat],
        [cos_lat * cos_lon, cos_lat * sin_lon, sin_lat],
    ])


def azel_from_enu(enu: np.ndarray) -> Tuple[float, float]:
    """ENU ベクトル → (方位角 [deg, 北=0 東回り], 仰角 [deg])。"""
    e, n, u = float(enu[0]), float(enu[1]), float(enu[2])
    az = math.degrees(math.atan2(e, n)) % 360.0
    el = math.degrees(math.atan2(u, math.hypot(e, n)))
    return az, el


def airmass_plane_parallel(el_deg: float) -> float:
    """平面平行 airmass X = sec z = 1/sin(el)。地平線下は nan。

    誤差は 60° で 0.1%、20° で 1%（SatCap lightcurve.py と同式）。
    """
    if not math.isfinite(el_deg) or el_deg <= 0.0:
        return math.nan
    return 1.0 / math.sin(math.radians(el_deg))


def apparent_elevation(el_deg: float) -> float:
    """幾何仰角 → 大気屈折込みの視仰角 [deg]（Sæmundsson 式）。

        R [arcmin] = 1.02 / tan(h + 10.3/(h + 5.11))    (h [deg])

    地平線で ~0.48°、仰角 45° で ~0.017° 持ち上がる。h < -1° では屈折を
    適用しない（式の適用域外。どのみち非可視）。
    """
    if not math.isfinite(el_deg) or el_deg < -1.0:
        return el_deg
    r_arcmin = 1.02 / math.tan(math.radians(el_deg + 10.3 / (el_deg + 5.11)))
    return el_deg + r_arcmin / 60.0


# ---------------------------------------------------------------------------
# 恒星カタログ（SatCap satcap/astro/catalog.py 移植、npz は同プロジェクト由来）
# ---------------------------------------------------------------------------
_STAR_CATALOGS: Dict[str, Dict[str, np.ndarray]] = {}


def load_star_catalog(path: str) -> Optional[Dict[str, np.ndarray]]:
    """npz カタログ（ra_deg/dec_deg/pm_ra_cosdec/pm_dec/mag）をロードして
    単位ベクトル込みでキャッシュする。見つからなければ None（警告 1 回）。"""
    key = str(path)
    if key in _STAR_CATALOGS:
        return _STAR_CATALOGS[key] or None
    if not Path(path).exists():
        print(f'⚠ groundobs: 恒星カタログが見つかりません: {path}（恒星背景なしで続行）',
              file=sys.stderr)
        _STAR_CATALOGS[key] = {}
        return None
    data = np.load(path, allow_pickle=False)
    ra = np.radians(np.asarray(data['ra_deg'], dtype=np.float64))
    dec = np.radians(np.asarray(data['dec_deg'], dtype=np.float64))
    cat = {
        'unit': np.column_stack([
            np.cos(dec) * np.cos(ra),
            np.cos(dec) * np.sin(ra),
            np.sin(dec),
        ]),
        'ra': ra,
        'dec': dec,
        'pm_ra_cosdec': np.asarray(data['pm_ra_cosdec'], dtype=np.float64),  # mas/yr
        'pm_dec': np.asarray(data['pm_dec'], dtype=np.float64),              # mas/yr
        'mag': np.asarray(data['mag'], dtype=np.float64),
    }
    _STAR_CATALOGS[key] = cat
    return cat


def stars_in_field(cat: Dict[str, np.ndarray], boresight: np.ndarray,
                   radius_rad: float, mag_limit: float,
                   jd: float) -> Tuple[np.ndarray, np.ndarray]:
    """視野コーン内の恒星の (単位ベクトル (N,3), 等級 (N,)) を返す。

    固有運動は単位球上で伝播する（SatCap catalog.propagate_proper_motion と
    同式。pm_ra_cosdec は cosδ 込みの mas/yr）。座標は ICRS ≈ J2000 ≈ ECI
    とみなす（フレームバイアス 23 mas は無視）。
    """
    mask = cat['mag'] <= mag_limit
    unit = cat['unit'][mask]
    # コーン粗選別（固有運動マージンは高々数 arcsec なので radius に 10" 足す）
    sel = unit @ boresight > math.cos(radius_rad + 10.0 * ARCSEC_TO_RAD)
    if not np.any(sel):
        return np.zeros((0, 3)), np.zeros(0)
    unit = unit[sel]
    ra = cat['ra'][mask][sel]
    dec = cat['dec'][mask][sel]
    pm_a = cat['pm_ra_cosdec'][mask][sel]
    pm_d = cat['pm_dec'][mask][sel]
    mag = cat['mag'][mask][sel]

    years = (jd - 2451545.0) / 365.25 + 2000.0 - STAR_CATALOG_EPOCH_JYEAR
    e_alpha = np.column_stack([-np.sin(ra), np.cos(ra), np.zeros_like(ra)])
    e_delta = np.column_stack([-np.sin(dec) * np.cos(ra),
                               -np.sin(dec) * np.sin(ra),
                               np.cos(dec)])
    shift = (pm_a[:, None] * e_alpha + pm_d[:, None] * e_delta) * (years * MAS_TO_RAD)
    moved = unit + shift
    moved /= np.linalg.norm(moved, axis=1, keepdims=True)
    return moved, mag


# ---------------------------------------------------------------------------
# ターゲットモデル
# ---------------------------------------------------------------------------
def obj_bounding_radius(model_path: str, scale: float) -> float:
    """OBJ の頂点から原点基準のバウンディング半径 [km] を求める（scale 適用後）。"""
    r_max_sq = 0.0
    with open(model_path) as fh:
        for line in fh:
            if line.startswith('v '):
                parts = line.split()
                x, y, z = float(parts[1]), float(parts[2]), float(parts[3])
                r_max_sq = max(r_max_sq, x * x + y * y + z * z)
    return math.sqrt(r_max_sq) * scale


def build_target_objects(args: argparse.Namespace,
                         body_to_world: mi.ScalarTransform4f) -> dict:
    """ターゲット形状の Mitsuba 辞書を返す。

    - model_path 指定時: OBJ をパーツ分割して読み込み（relative verb と同じ
      load_model_with_parts）。YAML の model_parts / model_bsdf 辞書に対応
    - 未指定時: 拡散球（半径 sphere_radius_m、アルベド sphere_albedo）。
      解析解 E = (2/3)·ρ·(r/d)²·S0·位相関数 と比較できる測光検証用の既定
    """
    if args.model_path:
        parts = getattr(args, 'model_parts', None)
        default_bsdf = getattr(args, 'model_bsdf', None)
        return load_model_with_parts(
            str(args.model_path), parts or {}, default_bsdf or {},
            body_to_world, float(args.model_scale), prefix='target',
        )
    radius_km = float(args.sphere_radius_m) / 1000.0
    albedo = float(args.sphere_albedo)
    return {
        'target_sphere': {
            'type': 'sphere',
            'to_world': body_to_world @ mi.ScalarTransform4f.scale([radius_km] * 3),
            'bsdf': {
                'type': 'diffuse',
                'reflectance': {'type': 'rgb', 'value': [albedo] * 3},
            },
        },
    }


# ---------------------------------------------------------------------------
# カメラ基底と画像座標系
# ---------------------------------------------------------------------------
class CameraBasis(NamedTuple):
    """ボアサイトまわりの画像座標基底（すべて ECI 単位ベクトル）。

    Mitsuba look_at の実測（本ファイル冒頭コメント）に一致させてある:
      画像の列+  ↔ e_x = normalize(f × up_param)
      画像の行+  ↔ −u_cam（u_cam = e_x × f、画像は y 下向き）
    """
    forward: np.ndarray   # ボアサイト（観測者→物体）
    e_x: np.ndarray       # 画像 x+ 方向
    u_cam: np.ndarray     # 画像 y− 方向（カメラの up）
    up_param: np.ndarray  # look_at に渡した up


def camera_basis(boresight: np.ndarray) -> CameraBasis:
    """ボアサイト方向から画像座標基底を作る（up は +Z、縮退時 +Y）。"""
    f = boresight / np.linalg.norm(boresight)
    up = np.array([0.0, 0.0, 1.0])
    if abs(float(np.dot(f, up))) > 0.95:
        up = np.array([0.0, 1.0, 0.0])
    e_x = np.cross(f, up)
    e_x = e_x / np.linalg.norm(e_x)
    u_cam = np.cross(e_x, f)
    return CameraBasis(f, e_x, u_cam, up)


def project_to_grid(direction: np.ndarray, basis: CameraBasis,
                    n_pristine: int, pristine_rad: float) -> Optional[Tuple[float, float]]:
    """ECI 方向単位ベクトル → スーパーサンプルセンサ格子の (row, col) 浮動小数。

    ボアサイト後方（dot ≤ 0）は None。タンジェント平面座標
    θx = (d·e_x)/(d·f)、θy = (d·u_cam)/(d·f) を使う（perspective カメラの
    ピクセルは tan 等間隔だが、arcmin 級の視野では角度等間隔との差は
    サブミリ秒角で無視できる）。
    """
    denom = float(direction @ basis.forward)
    if denom <= 1e-9:
        return None
    tx = float(direction @ basis.e_x) / denom
    ty = float(direction @ basis.u_cam) / denom
    col = n_pristine / 2.0 + tx / pristine_rad
    row = n_pristine / 2.0 - ty / pristine_rad
    return row, col


# ---------------------------------------------------------------------------
# レンダリング（代理距離 + flux 保存の再標本化）
# ---------------------------------------------------------------------------
def render_target_irradiance(args: argparse.Namespace,
                             basis: CameraBasis,
                             range_km: float,
                             sun_travel_dir: np.ndarray,
                             sun_rgb: np.ndarray,
                             body_to_world: mi.ScalarTransform4f,
                             bound_radius_km: float,
                             pristine_rad: float,
                             samples: int) -> Tuple[np.ndarray, int, float]:
    """ターゲットをレンダし、ピクセルごとの開口面照度 E_ij [W/m²] を返す。

    Returns:
        (E_img (N,N,3) [W/m²], N, theta_span_rad):
        E_img は各レンダピクセルが開口面に運ぶ放射照度、theta_span_rad は
        レンダ窓の真の角スケール（観測者から見た全幅）
    """
    # 代理カメラ距離（float32 のレイ交差桁落ち対策。モジュール冒頭コメント参照）
    d_r = min(range_km, max(bound_radius_km * SURROGATE_RANGE_FACTOR, 1e-3))
    half_extent = bound_radius_km * FOV_MARGIN
    fov_deg = 2.0 * math.degrees(math.atan(half_extent / d_r))

    # 真の角スケール（観測者レンジ換算）とレンダ解像度。
    # 内部センサ格子（pixel_scale/supersample）より細かく刻む。
    theta_span = 2.0 * math.atan(half_extent / range_km)
    n_render = int(np.clip(math.ceil(theta_span / pristine_rad) * 2, 48, 1024))

    cam_pos = (-basis.forward * d_r).tolist()

    scene_dict = {
        'type': 'scene',
        'integrator': {'type': 'path', 'max_depth': int(args.max_depth)},
        'camera': {
            'type': 'perspective',
            'fov': fov_deg,
            'near_clip': d_r * 0.01,
            'far_clip': d_r * 100.0,
            'to_world': mi.ScalarTransform4f.look_at(
                origin=cam_pos, target=[0.0, 0.0, 0.0],
                up=basis.up_param.tolist()),
            'film': {
                'type': 'hdrfilm',
                'width': n_render,
                'height': n_render,
                # box フィルタ: ピクセル間で flux を再配分しない（測光保存）
                'rfilter': {'type': 'box'},
            },
            'sampler': {'type': 'independent', 'sample_count': int(samples)},
        },
        'sun': {
            'type': 'directional',
            'direction': sun_travel_dir.tolist(),
            'irradiance': {'type': 'rgb', 'value': sun_rgb.tolist()},
        },
        # envmap なし: 深宇宙背景は 0（測光のゼロ点を汚さない）
    }
    scene_dict.update(build_target_objects(args, body_to_world))

    from asset_cache import preload_bitmaps, share_bsdf_textures
    scene = mi.load_dict(preload_bitmaps(share_bsdf_textures(scene_dict)))
    image = np.array(mi.render(scene), dtype=np.float64)[:, :, :3]

    # 放射輝度 L → 開口面照度: E_ij = L_ij · ΔΩ_ij（ΔΩ は真の角スケールで計算）
    d_omega = (theta_span / n_render) ** 2
    return image * d_omega, n_render, theta_span


def accumulate_on_sensor_grid(e_img: np.ndarray, theta_span: float,
                              n_pristine: int, pristine_rad: float) -> np.ndarray:
    """レンダピクセルの照度をスーパーサンプルセンサ格子へ積み上げる（flux 保存）。

    レンダ窓とセンサ格子は同一中心・同一向き（同じカメラ up）なので、
    角座標の 1 次元マッピングで足し込むだけでよい。センサ外に出た分は捨てる
    （視野からあふれた flux は実際に検出器に届かない）。
    """
    n_render = e_img.shape[0]
    delta = theta_span / n_render
    coords = (np.arange(n_render) + 0.5) * delta - theta_span / 2.0
    idx = np.floor(coords / pristine_rad + n_pristine / 2.0).astype(int)
    valid = (idx >= 0) & (idx < n_pristine)

    acc = np.zeros((n_pristine, n_pristine, 3), dtype=np.float64)
    if not np.any(valid):
        return acc
    iy = idx[valid]
    ix = idx[valid]
    sub = e_img[np.ix_(valid, valid)]
    np.add.at(acc, (iy[:, None], ix[None, :]), sub)
    return acc


def deposit_flux_segment(acc: np.ndarray, p0: Tuple[float, float],
                         p1: Tuple[float, float], flux_rgb: np.ndarray) -> None:
    """点源の flux を (row,col) p0→p1 の線分に沿って積む（恒星トレイル用）。

    露光中に光源が画像上を動いた分だけ flux が引き伸ばされる。線分を視野
    矩形でクリップし、視野内に残った割合だけを K 分割して点置きする
    （PSF 畳み込み前の格子なので点置きで十分。σ_PSF ≫ 1 subpixel）。
    """
    n = acc.shape[0]
    r0, c0 = p0
    r1, c1 = p1
    length = math.hypot(r1 - r0, c1 - c0)
    if length < 0.5:
        ir, ic = int(math.floor(r0)), int(math.floor(c0))
        if 0 <= ir < n and 0 <= ic < n:
            acc[ir, ic] += flux_rgb
        return

    # Liang–Barsky で [0, n) 矩形へクリップ
    t_min, t_max = 0.0, 1.0
    dr, dc = r1 - r0, c1 - c0
    for p, q in ((-dr, r0), (dr, n - 1e-9 - r0), (-dc, c0), (dc, n - 1e-9 - c0)):
        if abs(p) < 1e-12:
            if q < 0:
                return  # 線分全体が視野外
            continue
        t = q / p
        if p < 0:
            t_min = max(t_min, t)
        else:
            t_max = min(t_max, t)
        if t_min > t_max:
            return
    fraction = t_max - t_min
    clipped_len = length * fraction
    k = int(np.clip(math.ceil(clipped_len) + 1, 2, TRAIL_MAX_STEPS))
    ts = np.linspace(t_min, t_max, k)
    rows = np.floor(r0 + ts * dr).astype(int)
    cols = np.floor(c0 + ts * dc).astype(int)
    ok = (rows >= 0) & (rows < n) & (cols >= 0) & (cols < n)
    if not np.any(ok):
        return
    np.add.at(acc, (rows[ok], cols[ok]), flux_rgb * (fraction / k))


def shift_add(dst: np.ndarray, src: np.ndarray, dy: int, dx: int,
              weight: float) -> None:
    """src を (dy, dx) だけずらして dst に weight 倍で加算する（境界クリップ）。"""
    n = dst.shape[0]
    y0d, y1d = max(0, dy), min(n, n + dy)
    x0d, x1d = max(0, dx), min(n, n + dx)
    if y0d >= y1d or x0d >= x1d:
        return
    dst[y0d:y1d, x0d:x1d] += weight * src[y0d - dy:y1d - dy, x0d - dx:x1d - dx]


def apply_seeing_psf(acc: np.ndarray, seeing_arcsec: float,
                     pristine_scale_arcsec: float) -> np.ndarray:
    """大気シーイングをガウス PSF（FWHM = seeing_arcsec）として畳み込む。

    gaussian_filter は正規化カーネルなので総 flux は保存される
    （境界は mode='constant' で外へ漏れた分だけ減る = 視野外へ逃げた光）。
    """
    if seeing_arcsec <= 0.0:
        return acc
    sigma_px = seeing_arcsec * FWHM_TO_SIGMA / pristine_scale_arcsec
    return gaussian_filter(acc, sigma=(sigma_px, sigma_px, 0.0), mode='constant')


# ---------------------------------------------------------------------------
# 大気揺らぎ（Kolmogorov 位相スクリーン → 瞬時スペックル PSF）
# ---------------------------------------------------------------------------
def kolmogorov_phase_screen(n: int, dx: float, r0: float, l0_outer: float,
                            rng: np.random.Generator) -> np.ndarray:
    """von Kármán 乱流の位相スクリーン [rad] を FFT 法 + サブハーモニクスで生成。

    PSD: Φ(f) = 0.023 r0^{-5/3} (f² + 1/L0²)^{-11/6} [rad²·m²]
    FFT 法はグリッド幅 L = n·dx より低い空間周波数（= tip-tilt の主成分）を
    落とすため、Lane/Johansson-Gavel 式の 3 レベル・サブハーモニクスで
    低周波を補う（像揺れの再現に必須）。
    構造関数 D(r) = 6.88(r/r0)^{5/3} との一致は
    internal/groundobs_validation/turbulence_validation.py で検証。
    """
    df = 1.0 / (n * dx)
    fx = np.fft.fftfreq(n, d=dx)
    fxx, fyy = np.meshgrid(fx, fx, indexing='ij')
    f0_sq = 1.0 / (l0_outer * l0_outer)
    psd = 0.023 * r0 ** (-5.0 / 3.0) * (fxx ** 2 + fyy ** 2 + f0_sq) ** (-11.0 / 6.0)
    psd[0, 0] = 0.0
    cn = ((rng.standard_normal((n, n)) + 1j * rng.standard_normal((n, n)))
          * np.sqrt(psd) * df)
    # ifft2 は 1/n² を含むので n² を掛けて Σ c(f)·e^{2πif·x} に戻す。
    # 一様ランダム位相の和の実部 → Var = Σ PSD·df²（= PSD の離散積分）
    screen = np.real(np.fft.ifft2(cn)) * n * n

    # サブハーモニクス: グリッド最低周波数 df を 3 分割ずつ 3 レベル掘り下げる
    coords = (np.arange(n) - n / 2.0) * dx
    xx, yy = np.meshgrid(coords, coords, indexing='ij')
    for p in range(1, 4):
        dfp = df / 3.0 ** p
        for m in (-1, 0, 1):
            for l in (-1, 0, 1):
                if m == 0 and l == 0:
                    continue
                f_x, f_y = m * dfp, l * dfp
                psd_p = (0.023 * r0 ** (-5.0 / 3.0)
                         * (f_x ** 2 + f_y ** 2 + f0_sq) ** (-11.0 / 6.0))
                a = ((rng.standard_normal() + 1j * rng.standard_normal())
                     * math.sqrt(psd_p) * dfp)
                screen += np.real(a * np.exp(2j * np.pi * (f_x * xx + f_y * yy)))
    return screen


def r0_from_seeing(seeing_arcsec: float, wavelength_m: float = LAMBDA_EFF_M) -> float:
    """シーイング FWHM [arcsec] → Fried パラメータ r0 [m]（FWHM ≈ 0.98 λ/r0）。"""
    return 0.98 * wavelength_m / (seeing_arcsec * ARCSEC_TO_RAD)


# turbulence PSF の瞳サンプリング密度 [px/r0]。turbulence_psf_kernel 参照
TURB_PUPIL_SAMPLES_PER_R0 = 4.0


def _crop_kernel_energy(kernel: np.ndarray, keep: float = 0.9999) -> np.ndarray:
    """PSF カーネルを、エネルギー keep を含む中心対称の最小箱に切り詰める。"""
    n = kernel.shape[0]
    c = n // 2
    total = float(kernel.sum())
    if total <= 0.0:
        return kernel
    # 中心からの Chebyshev 距離ごとの累積エネルギーで半幅を決める
    idx = np.arange(n)
    cheb = np.maximum(np.abs(idx[:, None] - c), np.abs(idx[None, :] - c))
    order = np.argsort(cheb, axis=None)
    cum = np.cumsum(kernel.flat[order]) / total
    k = int(np.searchsorted(cum, keep)) if cum[-1] >= keep else len(cum) - 1
    half = int(cheb.flat[order[min(k, len(order) - 1)]]) + 1
    lo, hi = max(0, c - half), min(n, c + half + 1)
    return kernel[lo:hi, lo:hi]


def turbulence_psf_kernel(args: argparse.Namespace, pristine_scale_arcsec: float,
                          frame: int) -> np.ndarray:
    """1 フレームぶんの露光平均された大気揺らぎ PSF カーネル（総和 1）。

    瞬時 PSF = |FFT{P·exp(iφ)}|²（P=円形開口、φ=位相スクリーン）を
    サブ露光数 N = clip(exposure/τ0, 1, max_screens) 枚の独立スクリーンで
    平均する。N が小さい（短露光）ほどスペックル・像揺れが残り、
    大きいほど長露光ガウス（シーイング円盤）へ収束する。
    フレームごとに独立なスクリーン → フレーム間で像が揺れ動く。
    カーネルは総和 1 に正規化されるので絶対測光は不変。
    """
    d_ap = float(args.aperture_m)
    r0 = r0_from_seeing(float(args.seeing_arcsec))
    tau0 = max(float(args.turbulence_tau0_ms), 1e-3) * 1e-3
    n_screens = int(np.clip(round(float(args.exposure_s) / tau0), 1,
                            int(args.turbulence_max_screens)))

    # 瞳サンプリング: r0 あたり ≥TURB_PUPIL_SAMPLES_PER_R0 px（最低 64 px/口径）、
    # FFT パディング ≥2×。PSF 配列の全角幅は λ/dx で決まるため、係数を上げると
    # カーネルの裾のサポートが広がる（高ダイナミックレンジ画像で裾の打ち切りが
    # 見える場合に引き上げる。既定 4.0 は従来とビット同一）
    n_ap = int(np.clip(math.ceil(TURB_PUPIL_SAMPLES_PER_R0 * d_ap / r0), 64, 512))
    n_fft = 1 << (2 * n_ap - 1).bit_length()
    dx = d_ap / n_ap
    coords = (np.arange(n_fft) - n_fft / 2.0) * dx
    xx, yy = np.meshgrid(coords, coords, indexing='ij')
    pupil = (xx ** 2 + yy ** 2) <= (d_ap / 2.0) ** 2

    rng = np.random.default_rng(int(args.turbulence_seed) * 100003 + frame)
    psf = np.zeros((n_fft, n_fft), dtype=np.float64)
    for _ in range(n_screens):
        phi = kolmogorov_phase_screen(n_fft, dx, r0,
                                      float(args.turbulence_outer_scale_m), rng)
        field = np.where(pupil, np.exp(1j * phi), 0.0)
        psf += np.abs(np.fft.fftshift(np.fft.fft2(field))) ** 2
    # 自然サンプリング λ/(n_fft·dx) → 検出器の pristine スケールへ再標本化
    theta_nat_arcsec = LAMBDA_EFF_M / (n_fft * dx) / ARCSEC_TO_RAD
    factor = theta_nat_arcsec / pristine_scale_arcsec
    kernel = ndi_zoom(psf, factor, order=1, prefilter=False)
    kernel = np.clip(kernel, 0.0, None)
    kernel = _crop_kernel_energy(kernel)
    s = float(kernel.sum())
    return kernel / s if s > 0.0 else kernel


def apply_turbulence_psf(acc: np.ndarray, args: argparse.Namespace,
                         pristine_scale_arcsec: float, frame: int) -> np.ndarray:
    """大気揺らぎの瞬時 PSF を畳み込む（apply_seeing_psf の代替経路）。

    カーネル総和 1・mode='same' なので総 flux は保存される
    （境界からはみ出た分だけ減るのはガウス経路と同じ扱い）。
    """
    if float(args.seeing_arcsec) <= 0.0:
        return acc
    kernel = turbulence_psf_kernel(args, pristine_scale_arcsec, frame)
    return fftconvolve(acc, kernel[:, :, None], mode='same', axes=(0, 1))


# ---------------------------------------------------------------------------
# センサモデル（CCD 方程式。SatCap satcap/sim/camera.py のノイズ式を物理単位で）
# ---------------------------------------------------------------------------
def sensor_electrons(e_px_lum: np.ndarray, args: argparse.Namespace,
                     rng: np.random.Generator) -> np.ndarray:
    """ピクセル照度 [W/m²] → ADU 画像（ショット + 読み出しノイズ、飽和クリップ）。

    光電子数: N_e = E·A·τ·t_exp / (hc/λ) · QE
    ショットノイズ: Poisson(N_e)（λ が大きい画素はガウス近似）
    読み出し: N(0, read_noise_e)、その後フルウェルでクリップし gain で ADU 化。
    """
    area = math.pi * (float(args.aperture_m) / 2.0) ** 2
    photon_energy = PLANCK_H * LIGHT_C / LAMBDA_EFF_M
    scale = (area * float(args.throughput) * float(args.exposure_s)
             * float(args.quantum_efficiency) / photon_energy)
    electrons = np.clip(e_px_lum, 0.0, None) * scale

    # ショットノイズ（λ > 1e6 はガウス近似。np.random.poisson の λ 上限対策）
    small = electrons < 1e6
    noisy = np.empty_like(electrons)
    noisy[small] = rng.poisson(electrons[small]).astype(np.float64)
    big = ~small
    if np.any(big):
        noisy[big] = electrons[big] + rng.normal(0.0, np.sqrt(electrons[big]))

    noisy = np.clip(noisy, 0.0, float(args.full_well_e))
    noisy = noisy + rng.normal(0.0, float(args.read_noise_e), size=noisy.shape)
    adu = np.clip(noisy, 0.0, None) / float(args.gain_e_per_adu)
    return adu


def sky_irradiance_per_px(args: argparse.Namespace) -> float:
    """夜空背景輝度 [mag/arcsec²] → センサ 1 ピクセルあたりの照度 [W/m²]。

    等級→照度は物体と同じ V バンド近似スケール:
        E(m) = S0 · 10^(−0.4·(m − m_sun))
    を 1 arcsec² あたりに適用し、ピクセル立体角 (pixel_scale)² を掛ける。
    """
    s0 = float(args.sun_irradiance_wm2)
    m_sky = float(args.sky_mag_arcsec2)
    e_per_arcsec2 = s0 * 10.0 ** (-0.4 * (m_sky - SUN_APPARENT_MAG))
    return e_per_arcsec2 * float(args.pixel_scale_arcsec) ** 2


# ---------------------------------------------------------------------------
# 表示用ストレッチ・保存
# ---------------------------------------------------------------------------
def stretch_image(img: np.ndarray, mode: str, percentile: float,
                  noise_floor: float = 0.0) -> np.ndarray:
    """リニア画像 → 表示用 [0,1]。faint 側を持ち上げる天体画像風ストレッチ。

    noise_floor > 0 のとき正規化スケールの下限にする。ノイズ支配の画像で
    パーセンタイル値がノイズ裾に落ち、ノイズ画素が白に張り付いて実源が
    埋もれるのを防ぐ（σ の数倍を渡す。zscale の簡易版）。
    """
    positive = img[img > 0]
    if positive.size == 0:
        return np.zeros_like(img)
    vmax = max(float(np.percentile(positive, percentile)), float(noise_floor))
    if vmax <= 0.0:
        return np.zeros_like(img)
    x = np.clip(img / vmax, 0.0, None)
    if mode == 'linear':
        y = x
    elif mode == 'log':
        y = np.log1p(99.0 * x) / math.log(100.0)
    else:  # asinh
        y = np.arcsinh(10.0 * x) / math.asinh(10.0)
    return np.clip(y, 0.0, 1.0)


def save_png(path: Path, img01: np.ndarray) -> None:
    """[0,1] 画像を 8bit PNG で保存（ストレッチ済みなので追加ガンマなし）。"""
    if img01.ndim == 2:
        img01 = np.repeat(img01[:, :, None], 3, axis=2)
    bmp = mi.Bitmap(np.ascontiguousarray(img01.astype(np.float32)))
    bmp.convert(mi.Bitmap.PixelFormat.RGB, mi.Struct.Type.UInt8,
                srgb_gamma=False).write(str(path))


# ---------------------------------------------------------------------------
# 軌道（ケプラー / CSV / TLE）
# ---------------------------------------------------------------------------
class _KeplerOrbit:
    def __init__(self, elements: OrbitalElements):
        self.elements = elements

    def state_at(self, t: float) -> Tuple[np.ndarray, np.ndarray]:
        return compute_orbital_position(self.elements, t)


class _TleOrbit:
    """TLE + SGP4 伝播。位置は TEME だが ECI(J2000) と近似して扱う
    （両分点差 ~20 分角。モジュール冒頭コメント参照）。"""

    def __init__(self, line1: str, line2: str, jd0: float, name: str = ''):
        from sgp4.api import Satrec
        self.sat = Satrec.twoline2rv(line1, line2)
        self.jd0 = jd0
        self.name = name

    @property
    def tle_epoch_jd(self) -> float:
        return float(self.sat.jdsatepoch) + float(self.sat.jdsatepochF)

    def state_at(self, t: float) -> Tuple[np.ndarray, np.ndarray]:
        jd = self.jd0 + t / 86400.0
        jd_base = math.floor(jd - 0.5) + 0.5
        err, r, v = self.sat.sgp4(jd_base, jd - jd_base)
        if err != 0:
            from sgp4.api import SGP4_ERRORS
            raise RuntimeError(f'SGP4 伝播エラー ({err}): {SGP4_ERRORS.get(err, "?")}')
        return np.asarray(r, dtype=float), np.asarray(v, dtype=float)


def read_tle_file(path: str, name_filter: Optional[str] = None
                  ) -> Tuple[str, str, str]:
    """TLE ファイルから (name, line1, line2) を読む。

    3 行形式（名前行あり）と 2 行形式の両方に対応。name_filter 指定時は
    名前行に部分一致する最初のエントリを返す。
    """
    lines = [ln.rstrip() for ln in open(path) if ln.strip()]
    entries: List[Tuple[str, str, str]] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith('1 ') and i + 1 < len(lines) and lines[i + 1].startswith('2 '):
            name = lines[i - 1].strip() if i > 0 and not lines[i - 1].startswith(('1 ', '2 ')) else ''
            entries.append((name, lines[i], lines[i + 1]))
            i += 2
        else:
            i += 1
    if not entries:
        raise ValueError(f'TLE が見つかりません: {path}')
    if name_filter:
        for name, l1, l2 in entries:
            if name_filter.lower() in name.lower():
                return name, l1, l2
        raise ValueError(f'TLE 名 "{name_filter}" が {path} に見つかりません')
    return entries[0]


def build_orbit(args: argparse.Namespace, jd0: float):
    """(state_at(t) を持つオブジェクト, CsvEphemeris or None) を返す。

    優先度: --tle > --orbit-csv > ケプラー要素。
    """
    if getattr(args, 'tle', None):
        name, l1, l2 = read_tle_file(str(args.tle), getattr(args, 'tle_name', None))
        return _TleOrbit(l1, l2, jd0, name=name), None
    if args.orbit_csv:
        eph = CsvEphemeris(str(args.orbit_csv))
        return eph, eph
    a = float(args.semi_major_axis_km) if args.semi_major_axis_km \
        else EARTH_RADIUS_KM + float(args.altitude_km)
    elements = OrbitalElements(
        semi_major_axis=a,
        eccentricity=float(args.eccentricity),
        inclination=math.radians(float(args.inclination_deg)),
        raan=math.radians(float(args.raan_deg)),
        arg_periapsis=math.radians(float(args.arg_periapsis_deg)),
        mean_anomaly_0=math.radians(float(args.mean_anomaly_deg)),
    )
    return _KeplerOrbit(elements), None


def align_overhead(elements: OrbitalElements, site_dir_eci: np.ndarray,
                   t_start: float) -> OrbitalElements:
    """t=t_start で物体が観測地天頂に最も近づくよう Ω(RAAN) と M₀ を選び直す。

    M₀ だけの調整では軌道面が観測地の上を通らない場合に天頂へ届かない
    （面外角がそのまま残る）ため、軌道面の向き Ω も一緒に振る。
    i ≥ |観測地の地心緯度| なら天頂をほぼ通る面が必ず存在する。
    (Ω, M) の 2 次元グリッドを oe2rv の 1 回のベクトル呼び出しで評価し、
    site 方向との内積が最大の組を採る（グリッド 0.5° 刻み ≒ 仰角誤差 <1°、
    デモ用途には十分）。a / e / i / ω は変更しない。
    """
    n_mean = math.sqrt(EARTH_MU / elements.semi_major_axis ** 3)
    raan_cand = np.linspace(0.0, 2.0 * math.pi, 360, endpoint=False)
    m_cand = np.linspace(0.0, 2.0 * math.pi, 720, endpoint=False)
    raan_grid, m_grid = np.meshgrid(raan_cand, m_cand, indexing='ij')
    raan_flat = raan_grid.ravel()
    m_flat = m_grid.ravel()
    oe = np.column_stack([
        np.full_like(m_flat, elements.semi_major_axis),
        np.full_like(m_flat, elements.eccentricity),
        np.full_like(m_flat, elements.inclination),
        raan_flat,
        np.full_like(m_flat, elements.arg_periapsis),
        m_flat + n_mean * t_start,
    ])
    r, _ = oe2rv(oe, flag=0, mu=EARTH_MU)
    r_unit = r / np.linalg.norm(r, axis=1, keepdims=True)
    best = int(np.argmax(r_unit @ site_dir_eci))
    return OrbitalElements(
        semi_major_axis=elements.semi_major_axis,
        eccentricity=elements.eccentricity,
        inclination=elements.inclination,
        raan=float(raan_flat[best]),
        arg_periapsis=elements.arg_periapsis,
        mean_anomaly_0=float(m_flat[best]),
    )


# ---------------------------------------------------------------------------
# 観測コンテキスト（フレーム間で不変な前計算。バッチ / ライブプレビュー共用）
# ---------------------------------------------------------------------------
@dataclass
class ObservationContext:
    jd0: float
    r_site_ecef: np.ndarray
    enu: np.ndarray
    dt: float
    t_start: float
    orbit: Any
    eph: Optional[CsvEphemeris]
    sun_rgb_base: np.ndarray
    bound_radius_km: float
    sensor_px: int
    supersample: int
    pixel_scale: float
    k_ext: float
    e_sky_px: float
    tracking_mode: str
    refraction: bool
    exposure_s: float
    tumble_traj: Optional[List[np.ndarray]] = None
    star_catalog: Optional[Dict[str, np.ndarray]] = None
    star_mag_limit: float = 9.0
    min_el: float = 0.0
    s0: float = 1361.0
    site_lat: float = 0.0
    site_lon: float = 0.0


class FrameGeometry(NamedTuple):
    """時刻 t の観測幾何一式。"""
    jd: float
    theta: float             # GMST [rad]
    r_site_eci: np.ndarray
    r_obj: np.ndarray
    v_obj: np.ndarray
    sun_pos: np.ndarray
    los: np.ndarray          # 観測者→物体 [km]
    range_km: float
    az: float
    el: float                # 幾何仰角 [deg]
    el_app: float            # 屈折込み視仰角 [deg]
    x_air: float
    sun_el: float
    nu: float
    phase_deg: float


class FrameResult(NamedTuple):
    png01: np.ndarray        # 表示用 [0,1] 画像（H,W,3）
    linear: np.ndarray       # リニアなセンサ面照度 or ADU
    row: Dict[str, Any]      # observation.csv の 1 行
    time_s: float
    visible: bool


_EPOCH_DEFAULT: Optional[str] = None


def _epoch_default() -> str:
    """argparse の epoch_utc 既定値（TLE エポック自動採用の判定に使う）。"""
    global _EPOCH_DEFAULT
    if _EPOCH_DEFAULT is None:
        _EPOCH_DEFAULT = build_parser().get_default('epoch_utc')
    return _EPOCH_DEFAULT


def build_context(args: argparse.Namespace) -> ObservationContext:
    """フレーム間で不変な前計算をまとめる。live_worker もこれを共用する。"""
    jd0 = parse_epoch_jd(str(args.epoch_utc))
    site_lat = float(args.site_lat)
    site_lon = float(args.site_lon)
    r_site_ecef = site_ecef_km(site_lat, site_lon, float(args.site_alt_m))
    enu = enu_matrix(site_lat, site_lon)

    if args.duration_sec is not None and args.duration_sec > 0 and args.frames > 0:
        dt = float(args.duration_sec) / args.frames
    else:
        dt = 1.0 / args.fps if args.fps > 0 else 1.0
    t_start = float(args.start_time or 0.0)

    orbit, eph = build_orbit(args, jd0)

    # TLE 指定時、エポック未指定（argparse 既定のまま）なら TLE エポックを使う
    if isinstance(orbit, _TleOrbit) and str(args.epoch_utc) == _epoch_default():
        jd0 = orbit.tle_epoch_jd
        orbit.jd0 = jd0
        print(f"  TLE エポックを t=0 に採用: JD {jd0:.5f}")

    if args.start_overhead:
        if isinstance(orbit, _KeplerOrbit):
            theta0 = float(np.atleast_1d(gmst(jd0 + t_start / 86400.0))[0])
            site_dir = rot_z(theta0) @ r_site_ecef
            site_dir = site_dir / np.linalg.norm(site_dir)
            orbit.elements = align_overhead(orbit.elements, site_dir, t_start)
            print(f"  --start-overhead: Ω = {math.degrees(orbit.elements.raan):.2f}°, "
                  f"M₀ = {math.degrees(orbit.elements.mean_anomaly_0):.2f}° に調整")
        else:
            print('⚠ --start-overhead はケプラー軌道のみ対応（CSV/TLE では無視）',
                  file=sys.stderr)

    # 太陽 RGB: 黒体色を「Rec.709 輝度 = S0」となるよう正規化する。
    # 測光式 m = m_sun − 2.5·log10(E_lum / S0) がスペクトル形状に依存しない
    # ための規約（放射照度の輝度重み付き値が基準 S0 に一致する）。
    s0 = float(args.sun_irradiance_wm2)
    from optical_lighting import blackbody_to_rgb
    rgb = np.asarray(blackbody_to_rgb(float(args.sun_temperature)), dtype=float)
    sun_rgb_base = rgb * (s0 / max(float(_luminance(rgb)), 1e-12))

    if args.model_path:
        bound_radius_km = obj_bounding_radius(str(args.model_path),
                                              float(args.model_scale))
    else:
        bound_radius_km = float(args.sphere_radius_m) / 1000.0

    # tumble 姿勢はフレーム列を一括前計算する（ライブプレビューの O(1)
    # スクラブと、バッチの逐次伝播が同一結果になる。propagate_trajectory 参照）
    tumble_traj = None
    if str(args.attitude_mode) == 'tumble':
        I_body, I_inv = inertia_matrices(float(args.Ix), float(args.Iy), float(args.Iz))
        tumble_traj = propagate_trajectory(
            np.array([0.0, 0.0, 0.0, 1.0]),
            np.array([float(args.wx), float(args.wy), float(args.wz)]),
            I_body, I_inv, dt, max(1, int(args.frames)))

    star_catalog = None
    if getattr(args, 'show_stars', False):
        star_catalog = load_star_catalog(str(args.star_catalog))

    return ObservationContext(
        jd0=jd0, r_site_ecef=r_site_ecef, enu=enu, dt=dt, t_start=t_start,
        orbit=orbit, eph=eph, sun_rgb_base=sun_rgb_base,
        bound_radius_km=bound_radius_km,
        sensor_px=int(args.sensor_px), supersample=max(1, int(args.supersample)),
        pixel_scale=float(args.pixel_scale_arcsec),
        k_ext=float(args.extinction_k),
        e_sky_px=sky_irradiance_per_px(args) if args.sensor_noise else 0.0,
        tracking_mode=str(getattr(args, 'tracking_mode', 'target')),
        refraction=bool(getattr(args, 'refraction', True)),
        exposure_s=float(args.exposure_s),
        tumble_traj=tumble_traj,
        star_catalog=star_catalog,
        star_mag_limit=float(getattr(args, 'star_mag_limit', 9.0)),
        min_el=float(args.min_elevation_deg), s0=s0,
        site_lat=site_lat, site_lon=site_lon,
    )


def los_at(ctx: ObservationContext, t: float) -> Tuple[np.ndarray, np.ndarray]:
    """時刻 t の (観測者 ECI 位置, 観測者→物体ベクトル [km])。軽量版。"""
    jd = ctx.jd0 + t / 86400.0
    theta = float(np.atleast_1d(gmst(jd))[0])
    r_site = rot_z(theta) @ ctx.r_site_ecef
    r_obj, _ = ctx.orbit.state_at(t)
    return r_site, r_obj - r_site


def geometry_at(ctx: ObservationContext, t: float) -> FrameGeometry:
    """時刻 t の観測幾何一式を計算する。"""
    jd = ctx.jd0 + t / 86400.0
    theta = float(np.atleast_1d(gmst(jd))[0])
    r_site_eci = rot_z(theta) @ ctx.r_site_ecef
    r_obj, v_obj = ctx.orbit.state_at(t)
    sun_pos = sun_position_eci_km(jd)

    los = r_obj - r_site_eci
    range_km = float(np.linalg.norm(los))
    los_ecef = rot_z(-theta) @ los
    az, el = azel_from_enu(ctx.enu @ los_ecef)
    el_app = apparent_elevation(el) if ctx.refraction else el
    sun_ecef = rot_z(-theta) @ sun_pos
    _, sun_el = azel_from_enu(ctx.enu @ sun_ecef)

    nu = float(np.atleast_1d(earth_shadow(
        r_obj.reshape(1, 3), sun_pos.reshape(1, 3),
        SUN_RADIUS_KM, EARTH_RADIUS_KM))[0])

    to_obs = r_site_eci - r_obj
    to_sun = sun_pos - r_obj
    phase_deg = math.degrees(math.acos(np.clip(
        float(np.dot(to_obs, to_sun))
        / (np.linalg.norm(to_obs) * np.linalg.norm(to_sun)), -1.0, 1.0)))
    return FrameGeometry(
        jd=jd, theta=theta, r_site_eci=r_site_eci, r_obj=r_obj, v_obj=v_obj,
        sun_pos=sun_pos, los=los, range_km=range_km, az=az, el=el,
        el_app=el_app, x_air=airmass_plane_parallel(el_app), sun_el=sun_el,
        nu=nu, phase_deg=phase_deg)


def attitude_at(ctx: ObservationContext, args: argparse.Namespace, frame: int,
                t: float, g: FrameGeometry) -> np.ndarray:
    """フレームの機体→ECI 回転行列。優先度: CSV 姿勢 > tumble 軌跡 > 指向則。

    tumble はフレーム先頭時刻の姿勢を使う（sidereal モードの露光中心との
    ずれは高々 exposure/2 で、近似として無視する）。
    """
    if ctx.eph is not None and ctx.eph.has_attitude:
        dcm = ctx.eph.attitude_at(t)
        if dcm is not None:
            return dcm
    if ctx.tumble_traj is not None:
        from yoshimulib.attitude.quaternion import q2dcm
        idx = int(np.clip(frame, 0, len(ctx.tumble_traj) - 1))
        # q は inertial→body（scene_common 規約）なので body→ECI は転置
        return q2dcm(ctx.tumble_traj[idx], scalar=SCALAR).T
    sun_dir = g.sun_pos / np.linalg.norm(g.sun_pos)
    return compute_attitude_matrix(g.r_obj, g.v_obj,
                                   mode=str(args.attitude_mode),
                                   sun_direction=sun_dir)


# ---------------------------------------------------------------------------
# フレーム生成（バッチ / ライブプレビュー共用）
# ---------------------------------------------------------------------------
def _deposit_stars(acc: np.ndarray, ctx: ObservationContext,
                   basis: CameraBasis, g: FrameGeometry, t: float,
                   n_pristine: int, pristine_rad: float) -> int:
    """視野内の恒星を積む。追尾モードに応じて露光中のトレイルも描く。

    target モード: 望遠鏡は物体を追うので、恒星は露光中に視野を流れる。
      露光開始・終了それぞれのボアサイト基底へ恒星を投影し、線分に flux を
      引き伸ばす（トレイル）。
    sidereal モード: 望遠鏡は恒星を追うので恒星は点像。

    Returns: 視野に積んだ恒星の数
    """
    cat = ctx.star_catalog
    if cat is None:
        return 0
    half_diag = (n_pristine / 2.0) * pristine_rad * math.sqrt(2.0)
    stars, mags = stars_in_field(cat, basis.forward, half_diag * 1.05,
                                 ctx.star_mag_limit, g.jd)
    if len(stars) == 0:
        return 0

    basis_end: Optional[CameraBasis] = None
    if ctx.tracking_mode == 'target' and ctx.exposure_s > 0.0:
        # 露光終了時のボアサイト（物体を追い続けた望遠鏡の向き）
        _, los_end = los_at(ctx, t + ctx.exposure_s)
        basis_end = camera_basis(los_end / np.linalg.norm(los_end))

    count = 0
    for s_vec, mag in zip(stars, mags):
        e_star = ctx.s0 * 10.0 ** (-0.4 * (float(mag) - SUN_APPARENT_MAG))
        flux_rgb = np.full(3, e_star)  # 白色: [E,E,E] の Rec.709 輝度 = E（係数和 = 1）
        p0 = project_to_grid(s_vec, basis, n_pristine, pristine_rad)
        if p0 is None:
            continue
        if basis_end is not None:
            p1 = project_to_grid(s_vec, basis_end, n_pristine, pristine_rad)
            if p1 is None:
                p1 = p0
        else:
            p1 = p0
        deposit_flux_segment(acc, p0, p1, flux_rgb)
        count += 1
    return count


def _streak_offsets(ctx: ObservationContext, basis: CameraBasis,
                    t0: float, t1: float, n_pristine: int,
                    pristine_rad: float) -> List[Tuple[int, int]]:
    """sidereal モードの物体ストリーク: 露光 [t0, t1] 中の物体位置を
    固定ボアサイト基底へ投影し、中心からの整数オフセット列を返す。"""
    # 端点間隔からステップ数を見積もる（パスはほぼ直線）
    ends = []
    for tt in (t0, t1):
        _, los = los_at(ctx, tt)
        p = project_to_grid(los / np.linalg.norm(los), basis,
                            n_pristine, pristine_rad)
        ends.append(p)
    if ends[0] is None or ends[1] is None:
        return [(0, 0)]
    est_len = math.hypot(ends[1][0] - ends[0][0], ends[1][1] - ends[0][1])
    k = int(np.clip(math.ceil(est_len) + 1, 2, STREAK_MAX_STEPS))
    center = n_pristine / 2.0
    offsets: List[Tuple[int, int]] = []
    for tt in np.linspace(t0, t1, k):
        _, los = los_at(ctx, float(tt))
        p = project_to_grid(los / np.linalg.norm(los), basis,
                            n_pristine, pristine_rad)
        if p is None:
            continue
        offsets.append((int(round(p[0] - center)), int(round(p[1] - center))))
    return offsets or [(0, 0)]


def render_observation_frame(args: argparse.Namespace, ctx: ObservationContext,
                             frame: int, *, samples: Optional[int] = None,
                             supersample: Optional[int] = None) -> FrameResult:
    """1 フレーム分の観測像と CSV 行を作る（バッチ / ライブプレビュー共用）。

    samples / supersample はライブプレビュー用の品質オーバーライド
    （None ならフォーム/CLI の値）。
    """
    t = ctx.t_start + frame * ctx.dt
    sidereal = ctx.tracking_mode == 'sidereal'
    # sidereal は露光中心でカメラを固定（ストリークが対称に伸びる）
    t_cam = t + 0.5 * ctx.exposure_s if sidereal else t
    g = geometry_at(ctx, t_cam)

    spp = int(samples if samples is not None else args.samples)
    ss = max(1, int(supersample if supersample is not None else ctx.supersample))
    n_pristine = ctx.sensor_px * ss
    pristine_scale = ctx.pixel_scale / ss
    pristine_rad = pristine_scale * ARCSEC_TO_RAD

    visible = g.el_app > ctx.min_el
    ang_diam_arcsec = 2.0 * math.atan(ctx.bound_radius_km / g.range_km) / ARCSEC_TO_RAD

    e_total_lum = 0.0
    mag_app = None
    mag_obs = None
    n_stars = 0
    acc = np.zeros((n_pristine, n_pristine, 3), dtype=np.float64)

    if visible:
        basis = camera_basis(g.los / np.linalg.norm(g.los))

        if g.nu > 0.0:
            r_b2i = attitude_at(ctx, args, frame, t_cam, g)
            m4 = np.eye(4)
            m4[:3, :3] = r_b2i
            body_xform = mi.ScalarTransform4f(m4)
            sun_dir = g.sun_pos / np.linalg.norm(g.sun_pos)
            e_img, n_render, theta_span = render_target_irradiance(
                args, basis, g.range_km, (-sun_dir).copy(),
                ctx.sun_rgb_base * g.nu, body_xform, ctx.bound_radius_km,
                pristine_rad, spp)

            # 測光: PSF・視野切り出しの前に全 flux を積分（大気圏外の値）
            e_total_lum = float(_luminance(e_img.sum(axis=(0, 1))))
            if e_total_lum > 0.0:
                mag_app = SUN_APPARENT_MAG - 2.5 * math.log10(e_total_lum / ctx.s0)
                if math.isfinite(g.x_air):
                    mag_obs = mag_app + ctx.k_ext * g.x_air

            obj_acc = accumulate_on_sensor_grid(e_img, theta_span,
                                                n_pristine, pristine_rad)
            if sidereal and ctx.exposure_s > 0.0:
                # 恒星時追尾: 露光中に物体が流れる → shift-and-add でストリーク化
                offsets = _streak_offsets(ctx, basis, t, t + ctx.exposure_s,
                                          n_pristine, pristine_rad)
                w = 1.0 / len(offsets)
                streaked = np.zeros_like(obj_acc)
                for dy, dx in offsets:
                    shift_add(streaked, obj_acc, dy, dx, w)
                acc += streaked
            else:
                acc += obj_acc

        # 恒星背景は物体が本影中でも見える（望遠鏡は向いている）
        n_stars = _deposit_stars(acc, ctx, basis, g, t, n_pristine, pristine_rad)

        # 大気減光 → シーイング → センサ格子へビニング
        if math.isfinite(g.x_air):
            acc *= 10.0 ** (-0.4 * ctx.k_ext * g.x_air)
        if getattr(args, 'turbulence', False):
            acc = apply_turbulence_psf(acc, args, pristine_scale, frame)
        else:
            acc = apply_seeing_psf(acc, float(args.seeing_arcsec), pristine_scale)

    sensor_e = acc.reshape(ctx.sensor_px, ss, ctx.sensor_px, ss, 3).sum(axis=(1, 3))

    # センサ出力（ノイズモデル ON ならモノクロ ADU、OFF ならリニア RGB）
    if args.sensor_noise:
        rng = np.random.default_rng(int(args.noise_seed) + frame)
        e_px_lum = _luminance(sensor_e) + ctx.e_sky_px
        adu = sensor_electrons(e_px_lum, args, rng)
        # 表示は背景（空）を中央値で差し引いてからストレッチする。
        # 空が支配的な画像を素のままストレッチすると全面が白に張り付く。
        # 正規化の下限は 8σ（1.4826·MAD のロバスト推定）にして、ノイズ画素が
        # 白点化して実源が埋もれるのを防ぐ。linear（npy 側）は生 ADU のまま。
        med = float(np.median(adu))
        sigma = 1.4826 * float(np.median(np.abs(adu - med)))
        display = np.clip(adu - med, 0.0, None)
        png01 = stretch_image(display, str(args.stretch),
                              float(args.stretch_percentile),
                              noise_floor=8.0 * sigma)
        linear_out: np.ndarray = adu
    else:
        if getattr(args, 'monochrome', False):
            # モノクロ検出器: Rec.709 輝度 [W/m²/px]（測光と同じ重み）
            sensor_out: np.ndarray = _luminance(sensor_e)
        else:
            sensor_out = sensor_e
        png01 = stretch_image(sensor_out, str(args.stretch),
                              float(args.stretch_percentile))
        linear_out = sensor_out
    if png01.ndim == 2:
        # ノイズモデル経路はモノクロ → 表示用に 3ch 化して返す
        #（save_png / live_worker の image01 経路の双方が RGB を前提にできる）
        png01 = np.repeat(png01[:, :, None], 3, axis=2)

    row = {
        'frame': frame,
        'time_s': f'{t:.3f}',
        'jd_utc': f'{g.jd:.8f}',
        'az_deg': f'{g.az:.4f}',
        'el_deg': f'{g.el:.4f}',
        'el_app_deg': f'{g.el_app:.4f}',
        'range_km': f'{g.range_km:.3f}',
        'phase_angle_deg': f'{g.phase_deg:.3f}',
        'airmass': f'{g.x_air:.4f}' if math.isfinite(g.x_air) else '',
        'sun_el_deg': f'{g.sun_el:.3f}',
        'illumination_nu': f'{g.nu:.4f}',
        'visible': int(bool(visible)),
        'n_stars': n_stars,
        'ang_diameter_arcsec': f'{ang_diam_arcsec:.4f}',
        'irradiance_wm2': f'{e_total_lum:.6e}' if e_total_lum > 0 else '',
        'mag_app': f'{mag_app:.3f}' if mag_app is not None else '',
        'mag_obs': f'{mag_obs:.3f}' if mag_obs is not None else '',
    }
    return FrameResult(png01=png01, linear=linear_out, row=row,
                       time_s=t, visible=visible)


# ---------------------------------------------------------------------------
# メインループ
# ---------------------------------------------------------------------------
def _run(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir or 'output/groundobs')
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = Path(getattr(args, 'csv_path', None) or (output_dir / 'observation.csv'))

    ctx = build_context(args)

    print('地上望遠鏡観測シミュレーション (groundobs)')
    print(f"  エポック: JD {ctx.jd0:.5f} ({args.epoch_utc})")
    print(f"  観測地: lat={ctx.site_lat:.4f}° lon={ctx.site_lon:.4f}° "
          f"alt={float(args.site_alt_m):.0f} m")
    if isinstance(ctx.orbit, _TleOrbit):
        print(f"  軌道: TLE {args.tle}"
              + (f" ({ctx.orbit.name})" if ctx.orbit.name else '')
              + "（SGP4、TEME≈ECI 近似）")
    elif ctx.eph is not None:
        print(f"  軌道: CSV ephemeris {args.orbit_csv}")
    else:
        el0 = ctx.orbit.elements
        print(f"  軌道: a={el0.semi_major_axis:.1f} km e={el0.eccentricity:.4f} "
              f"i={math.degrees(el0.inclination):.2f}° (周期 {el0.orbital_period:.0f} s)")
    print(f"  ターゲット: "
          + (f"{args.model_path} (scale {args.model_scale})" if args.model_path
             else f"拡散球 r={args.sphere_radius_m} m, albedo={args.sphere_albedo}")
          + f", バウンディング半径 {ctx.bound_radius_km * 1000:.2f} m")
    field_arcsec = ctx.sensor_px * ctx.pixel_scale
    print(f"  望遠鏡: {ctx.pixel_scale}\"/px × {ctx.sensor_px}px = 視野 "
          f"{field_arcsec:.1f}\" (シーイング {args.seeing_arcsec}\", 減光 k={ctx.k_ext}, "
          f"追尾 {ctx.tracking_mode}, 屈折 {'ON' if ctx.refraction else 'OFF'})")
    if getattr(args, 'turbulence', False):
        r0_cm = r0_from_seeing(float(args.seeing_arcsec)) * 100.0
        n_scr = int(np.clip(round(float(args.exposure_s)
                                  / (float(args.turbulence_tau0_ms) * 1e-3)),
                            1, int(args.turbulence_max_screens)))
        print(f"  大気揺らぎ: 瞬時 PSF（r0={r0_cm:.1f} cm, D/r0="
              f"{float(args.aperture_m) * 100.0 / r0_cm:.1f}, "
              f"τ0={args.turbulence_tau0_ms} ms → {n_scr} スクリーン/露光, "
              f"L0={args.turbulence_outer_scale_m} m）")
    if ctx.star_catalog is not None:
        print(f"  恒星背景: {args.star_catalog} (mag ≤ {ctx.star_mag_limit})")
    if args.sensor_noise:
        print(f"  センサ: D={args.aperture_m} m, t={args.exposure_s} s, "
              f"QE={args.quantum_efficiency}, 空 {args.sky_mag_arcsec2} mag/arcsec²")
    print(f"  時間軸: dt={ctx.dt:.3f} s × {args.frames} フレーム "
          f"(start={ctx.t_start:.1f} s, 露光 {ctx.exposure_s} s)")
    print(f"  出力: {output_dir}/")
    print()

    rows: List[Dict[str, Any]] = []
    for frame in range(int(args.frames)):
        res = render_observation_frame(args, ctx, frame)
        frame_path = output_dir / f'frame_{frame:04d}.png'
        save_png(frame_path, res.png01)
        if args.save_linear:
            np.save(output_dir / f'frame_{frame:04d}_linear.npy',
                    res.linear.astype(np.float32))
        rows.append(res.row)

        r = res.row
        nu = float(r['illumination_nu'])
        status = ('本影' if nu <= 0.0 else ('半影' if nu < 1.0 else '日照')) \
            if res.visible else '地平線下'
        mag_txt = f"mag={r['mag_obs']}" if r['mag_obs'] else f'({status})'
        star_txt = f" ★{r['n_stars']}" if int(r['n_stars']) else ''
        print(f"  [{frame + 1:3d}/{args.frames}] t={res.time_s:8.1f}s "
              f"az={float(r['az_deg']):6.1f}° el={float(r['el_deg']):5.1f}° "
              f"d={float(r['range_km']):8.1f}km "
              f"θ={float(r['ang_diameter_arcsec']):7.2f}\" {mag_txt}{star_txt}")

    fieldnames = list(rows[0].keys()) if rows else []
    with open(csv_path, 'w', newline='') as fh:
        fh.write("# groundobs observation log\n")
        fh.write(f"# epoch_utc={args.epoch_utc} jd0={ctx.jd0:.8f}\n")
        fh.write(f"# site lat={ctx.site_lat} lon={ctx.site_lon} "
                 f"alt_m={args.site_alt_m}\n")
        fh.write(f"# tracking={ctx.tracking_mode} refraction={ctx.refraction}\n")
        fh.write("# el_deg: geometric elevation / el_app_deg: with refraction "
                 "(Saemundsson). airmass and visibility use el_app.\n")
        fh.write(f"# mag_app: above-atmosphere apparent magnitude "
                 f"(V-band approx, m_sun={SUN_APPARENT_MAG})\n")
        fh.write(f"# mag_obs = mag_app + k*X, k={ctx.k_ext}\n")
        writer = _csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    n_vis = sum(r['visible'] for r in rows)
    mags = [float(r['mag_obs']) for r in rows if r['mag_obs']]
    print(f"\n完了: 可視 {n_vis}/{len(rows)} フレーム"
          + (f", 最大光度 mag {min(mags):.2f}" if mags else ''))
    print(f"  ライトカーブ: {csv_path}")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """CLI と YAML をマージ（CLI > YAML > argparse デフォルト。relative と同方式）。"""
    parser = build_parser()
    args_pre, _ = parser.parse_known_args(argv)
    if args_pre.config:
        parser.set_defaults(**load_yaml_config(args_pre.config))
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    _run(args)


if __name__ == '__main__':
    main()
