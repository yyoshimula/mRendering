"""
地球描画関連モジュール

地球本体、雲レイヤー、夜間光、大気シェルの生成。
テクスチャ合成（昼夜マスク、雲不透明度）のユーティリティを含む。

責務:
    - Mitsuba シーン辞書の地球関連サブツリーを返す
      (create_earth / create_clouds / create_night_lights / create_atmosphere)
    - 昼夜境界 (terminator) マスクの生成と、夜間光テクスチャへの適用
    - 雲テクスチャの不透明度プレ乗算

呼び出し元: scene_builder.create_scene()

シーン単位とテクスチャ規約:
    - 地球半径は 1.0 (シーン単位)。実距離は orbit_mechanics.SCALE_FACTOR で換算
    - レイヤーは半径を僅かに変えて重ねる:
        地表本体  : 1.000
        夜間光    : 1.002 (本体の僅か外側、Z-fighting 回避)
        雲        : 1.008
        大気      : 1.030 程度 (atmosphere_radius_scale)
    - テクスチャは equirectangular (横が経度、縦が緯度) を想定
      画像座標 (u, v) → 経度 lon = (u-0.5)*2π, 緯度 lat = (0.5-v)*π
    - 自転は +Z 軸まわりに rotation_deg [deg] で表現

Mitsuba メモ:
    - 'sphere' は中心 + 半径で定義する組み込み形状
    - 'mask' BSDF は opacity テクスチャで透明度を切り替えるラッパ
    - 'area' emitter を持たせると面光源になる (夜間光に使用)
    - 'homogeneous' medium は participating media (体積散乱) に使う
"""

import numpy as np
from pathlib import Path
from typing import Dict, Any

import mitsuba as mi

# 夜間光テクスチャ等の bitmap キャッシュ。
# load_bitmap_rgb_float() で同じパスを何度も読まないようにする。
NIGHT_TEXTURE_CACHE: Dict[str, np.ndarray] = {}


def create_earth(radius=1.0, texture_path=None, rotation_deg: float = 0.0):
    """
    地球を表す球体を作成

    昼テクスチャ (BlueMarble など、equirectangular) を diffuse BSDF の
    reflectance に貼る。テクスチャが無ければ深い青で塗りつぶす。

    Args:
        radius: 地球の半径（基準値: 1.0、シーン単位）
        texture_path: テクスチャ画像へのパス（オプション）
        rotation_deg: +Z 軸まわりの自転角 [deg]

    Returns:
        Mitsubaシェイプオブジェクト (dict)
    """
    bsdf = {'type': 'diffuse'}

    if texture_path and Path(texture_path).exists():
        bsdf['reflectance'] = {
            'type': 'bitmap',
            'filename': str(texture_path),
        }
    else:
        # テクスチャがない場合：地球らしい色
        bsdf['reflectance'] = {
            'type': 'rgb',
            'value': [0.15, 0.35, 0.65]  # 深い青
        }

    earth = {
        'type': 'sphere',
        'center': [0, 0, 0],
        'radius': radius,
        'bsdf': bsdf
    }
    if abs(rotation_deg) > 1e-8:
        # 自転を適用 (Mitsuba の to_world は世界座標への変換行列)
        earth['to_world'] = mi.ScalarTransform4f.rotate([0, 0, 1], rotation_deg)
    return earth


def rotate_vector_z(vector: np.ndarray, rotation_deg: float) -> np.ndarray:
    """Z軸回転を適用 (3D ベクトルを地球自転と同じ向きに回す)。"""
    if abs(rotation_deg) < 1e-8:
        return vector
    angle = np.radians(rotation_deg)
    c = np.cos(angle)
    s = np.sin(angle)
    rot = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    return rot @ vector


def smoothstep(edge0: float, edge1: float, x: np.ndarray) -> np.ndarray:
    """滑らかな段差関数 (GLSL の smoothstep と同一)。

    edge0..edge1 を 3t^2 - 2t^3 で補間し、両端で C^1 連続。
    昼夜境界 (terminator) のソフトネスに使う。
    """
    t = np.clip((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def generate_night_mask(sun_dir_body: np.ndarray, width: int, height: int,
                        softness: float = 0.02) -> np.ndarray:
    """夜側マスクを生成（1=夜, 0=昼）

    地表上の各点 (lat, lon) の法線と太陽方向の内積から terminator を計算する。
    内積 > 0 なら昼、< 0 なら夜。softness で滑らかに繋ぐ。

    Args:
        sun_dir_body: 太陽方向 (地球固定座標、自動正規化)
        width, height: マスク画像サイズ [px]
        softness: terminator 幅 (内積空間)

    Returns:
        shape=(height, width) の float マスク [0..1]
    """
    sun_dir = sun_dir_body / (np.linalg.norm(sun_dir_body) + 1e-12)

    # 画素中心の (u, v) → 経度 lon, 緯度 lat (equirectangular)
    u = (np.arange(width) + 0.5) / width
    v = (np.arange(height) + 0.5) / height
    lon = (u - 0.5) * 2.0 * np.pi
    lat = (0.5 - v) * np.pi

    lon_grid, lat_grid = np.meshgrid(lon, lat)
    cos_lat = np.cos(lat_grid)

    # 各画素に対応する地表法線 (球面)
    normals = np.stack([
        cos_lat * np.cos(lon_grid),
        cos_lat * np.sin(lon_grid),
        np.sin(lat_grid)
    ], axis=-1)

    # 太陽との内積。terminator は dot=0 付近、softness 幅で滑らかに切り替え
    dot = normals @ sun_dir
    terminator = smoothstep(-softness, softness, dot)
    night_mask = 1.0 - terminator
    return np.clip(night_mask, 0.0, 1.0)


def resize_image_nearest(image: np.ndarray, width: int, height: int) -> np.ndarray:
    """最近傍で画像リサイズ (テクスチャと夜マスクの解像度合わせ用)。"""
    src_h, src_w = image.shape[:2]
    if src_w == width and src_h == height:
        return image
    xs = np.clip((np.linspace(0, src_w - 1, width)).astype(int), 0, src_w - 1)
    ys = np.clip((np.linspace(0, src_h - 1, height)).astype(int), 0, src_h - 1)
    return image[ys][:, xs]


def load_bitmap_rgb_float(path: str) -> np.ndarray:
    """bitmapをfloat RGB配列で読み込み（キャッシュ対応）

    Mitsuba の `mi.Bitmap` で読んで Float32 RGB に変換する。
    モノクロは 3 チャネルに複製される。
    NIGHT_TEXTURE_CACHE にキャッシュされ、2 回目以降は I/O を省略する。
    """
    cached = NIGHT_TEXTURE_CACHE.get(path)
    if cached is not None:
        return cached
    bitmap = mi.Bitmap(path).convert(mi.Bitmap.PixelFormat.RGB, mi.Struct.Type.Float32)
    array = np.array(bitmap, copy=False)
    if array.ndim == 2:
        array = np.stack([array, array, array], axis=-1)
    NIGHT_TEXTURE_CACHE[path] = array
    return array


def write_combined_night_texture(night_texture_path: str, night_mask: np.ndarray,
                                 output_path: Path) -> None:
    """夜光テクスチャに夜側マスクを適用して出力

    `night_texture * night_mask` を計算して PNG/EXR に書き出す。
    出力は scene_builder で `create_night_lights(texture_path=...)` の入力になる。
    """
    night_texture = load_bitmap_rgb_float(night_texture_path)
    target_h, target_w = night_mask.shape
    night_texture = resize_image_nearest(night_texture, target_w, target_h)
    combined = np.clip(night_texture * night_mask[..., None], 0.0, 1.0).astype(np.float32)
    mi.util.write_bitmap(str(output_path), combined)


def write_cloud_opacity_texture(cloud_texture_path: str, opacity: float,
                                output_path: Path) -> None:
    """雲テクスチャに不透明度を適用して出力 (`cloud * opacity` をプレ乗算)。"""
    cloud_texture = load_bitmap_rgb_float(cloud_texture_path)
    combined = np.clip(cloud_texture * opacity, 0.0, 1.0).astype(np.float32)
    mi.util.write_bitmap(str(output_path), combined)


def create_clouds(radius: float, texture_path: str, opacity: float = 0.5,
                  rotation_deg: float = 0.0) -> Dict[str, Any]:
    """雲レイヤーを生成

    地表より僅か外側 (radius=1.008 程度) に置く透過球。
    'mask' BSDF の opacity に雲テクスチャを与え、雲のあるピクセルだけ
    内側の diffuse BSDF を有効にする (= 雲の隙間から地表が透けて見える)。
    """
    bsdf = {
        'type': 'mask',
        'opacity': {
            'type': 'bitmap',
            'filename': str(texture_path),
        },
        'bsdf': {
            'type': 'diffuse',
            'reflectance': {'type': 'rgb', 'value': [1.0, 1.0, 1.0]}
        }
    }
    clouds = {
        'type': 'sphere',
        'center': [0, 0, 0],
        'radius': radius,
        'bsdf': bsdf
    }
    if abs(rotation_deg) > 1e-8:
        clouds['to_world'] = mi.ScalarTransform4f.rotate([0, 0, 1], rotation_deg)
    return clouds


def create_night_lights(radius: float, texture_path: str,
                        rotation_deg: float = 0.0) -> Dict[str, Any]:
    """夜間光を発光として追加

    BSDF を 'null' (反射しない) にし、'area' emitter で発光させる。
    radiance テクスチャは write_combined_night_texture で夜マスク済みのものを使う想定。
    """
    night_lights = {
        'type': 'sphere',
        'center': [0, 0, 0],
        'radius': radius,
        'bsdf': {'type': 'null'},
        'emitter': {
            'type': 'area',
            'radiance': {
                'type': 'bitmap',
                'filename': str(texture_path)
            }
        }
    }
    if abs(rotation_deg) > 1e-8:
        night_lights['to_world'] = mi.ScalarTransform4f.rotate([0, 0, 1], rotation_deg)
    return night_lights


def create_atmosphere(radius: float, density: float = 0.02) -> Dict[str, Any]:
    """簡易大気シェル（薄い散乱媒体）

    地表を包む透明な球を作り、その内部に homogeneous な散乱媒体を入れる。
    sigma_t を波長別に変える (R<G<B) ことで青空寄りの散乱を再現する簡易モデル。

    Args:
        radius:  大気層の外半径 (シーン単位、1.03 程度)
        density: 全体の散乱・吸収係数スケール
    """
    density = max(density, 1e-6)
    return {
        'type': 'sphere',
        'center': [0, 0, 0],
        'radius': radius,
        'bsdf': {'type': 'null'},
        'interior': {
            'type': 'homogeneous',
            # sigma_t: 消散係数 (R<G<B でレイリー散乱の青寄りを近似)
            'sigma_t': {'type': 'rgb', 'value': [density * 0.6, density * 0.8, density]},
            # albedo ≒ 1 で吸収を抑え、ほぼ純散乱に近い挙動
            'albedo': {'type': 'rgb', 'value': [0.9, 0.95, 1.0]},
        }
    }
