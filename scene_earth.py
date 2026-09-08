"""
地球描画関連モジュール

地球本体、雲レイヤー、夜間光、大気シェルの生成。
テクスチャ合成（昼夜マスク、雲不透明度）のユーティリティを含む。

責務:
    - Mitsuba シーン辞書の地球関連サブツリーを返す
      (create_earth / create_clouds / create_night_lights / create_atmosphere)
    - 昼夜境界 (terminator) マスクの生成と、夜間光テクスチャへの適用
    - 雲テクスチャの不透明度プレ乗算
    - 上記 2 つの生成物 (PNG/EXR) のメモ化。太陽方向・自転角・パラメータが
      前フレームと同じなら再生成せず、既存ファイルのパスを返す
      (resolve_night_emission_texture / resolve_cloud_opacity_texture)

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

# 戻り値注釈 `-> mi.ScalarTransform4f` を import 時に評価させない（バリアント未設定の
# 段階で satellite_orbit → scene_builder → ここが import される。2026-09-08 回帰の修正）。
from __future__ import annotations

import os
import numpy as np
from pathlib import Path
from typing import Any, Dict, Tuple

import mitsuba as mi

# 夜間光テクスチャ等の float RGB 配列キャッシュ。
# キーは (path, mtime, target_w, target_h) で、値は **リサイズ済み** 配列。
# 原寸をキャッシュすると earth_night 8K で ~1.1 GB が常駐してしまうため、
# 実際に使うマスク解像度（既定 1024x512）まで落としたものだけを保持する。
NIGHT_TEXTURE_CACHE: Dict[Tuple[str, float, int, int], np.ndarray] = {}

# これを超える画素数の配列はキャッシュしない（雲テクスチャ 8192x4096 等）。
# 8 Mpx = float32 RGB で 96 MB。
RGB_CACHE_MAX_PIXELS = 8_000_000

# 生成済みテクスチャ PNG/EXR の再利用メモ。
# キーは「出力が一意に決まるパラメータ一式」で、値は生成済みファイルパス。
# 太陽・地球自転が止まっているフレーム列では夜光テクスチャの中身が変わらない
# ので、前フレームの PNG をそのまま使い回して再生成をスキップする。
_GENERATED_TEXTURE_CACHE: Dict[Tuple[Any, ...], str] = {}

# 太陽方向をキー化するときの丸め桁数。
# 出力 PNG は 8bit 量子化されるので、この桁の差は画に現れない。
_SUN_DIR_KEY_DECIMALS = 12


def earth_surface_transform(rotation_deg: float = 0.0) -> mi.ScalarTransform4f:
    """経度0°が+Xに来るよう、球体UVの原点を地理画像の中央へ揃える。

    Mitsuba sphere は local +X が u=0。地理画像は u=.5 が経度0°なので
    球を180°回したうえで地球自転を適用する。昼・雲・夜光に共通。
    この180°はUV規約の補正で、ECEF→ECIの自転角には含めない。
    """
    return mi.ScalarTransform4f.rotate([0, 0, 1], 180.0 + rotation_deg)


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
        'bsdf': bsdf,
        'to_world': earth_surface_transform(rotation_deg),
    }
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


def _mtime(path: str) -> float:
    """ファイルの更新時刻を返す（取得できなければ 0.0）。キャッシュキー用。"""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def load_bitmap_rgb_float(path: str, target_w: int = 0, target_h: int = 0) -> np.ndarray:
    """bitmap を float RGB 配列で読み込み、指定サイズへ最近傍リサイズする。

    Mitsuba の `mi.Bitmap` で読んで Float32 RGB に変換する。
    モノクロは 3 チャネルに複製される。

    `target_w`/`target_h` が 0 なら原寸のまま返す。戻り値は
    NIGHT_TEXTURE_CACHE に (path, mtime, target_w, target_h) キーで
    キャッシュされるが、RGB_CACHE_MAX_PIXELS を超える巨大配列は
    常駐メモリを圧迫するのでキャッシュしない。

    Returns:
        shape=(h, w, 3) の float32 配列。**呼び出し側で書き換えないこと**
        （キャッシュ実体を共有するため）。
    """
    key = (str(path), _mtime(path), int(target_w), int(target_h))
    cached = NIGHT_TEXTURE_CACHE.get(key)
    if cached is not None:
        return cached

    bitmap = mi.Bitmap(path).convert(mi.Bitmap.PixelFormat.RGB, mi.Struct.Type.Float32)
    array = np.array(bitmap, copy=False)
    if array.ndim == 2:
        array = np.stack([array, array, array], axis=-1)
    if target_w and target_h:
        # 縮小時は fancy indexing で新しい配列になるので、巨大な元 Bitmap の
        # バッファは参照されず即座に解放される。
        array = resize_image_nearest(array, target_w, target_h)

    if array.shape[0] * array.shape[1] <= RGB_CACHE_MAX_PIXELS:
        NIGHT_TEXTURE_CACHE[key] = array
    return array


def write_combined_night_texture(night_texture_path: str, night_mask: np.ndarray,
                                 output_path: Path) -> None:
    """夜光テクスチャに夜側マスクを適用して出力

    `night_texture * night_mask` を計算して PNG/EXR に書き出す。
    出力は scene_builder で `create_night_lights(texture_path=...)` の入力になる。
    """
    target_h, target_w = night_mask.shape
    night_texture = load_bitmap_rgb_float(night_texture_path, target_w, target_h)
    combined = np.clip(night_texture * night_mask[..., None], 0.0, 1.0).astype(np.float32)
    # write_async=False: 直後に同一フレームの mi.load_dict がこのファイルを読む。
    # 既定の非同期書き込みだと空ファイルを読む競合が起きる（シーン構築が
    # テクスチャ共有で高速化された結果、顕在化した）。
    mi.util.write_bitmap(str(output_path), combined, write_async=False)


def resolve_night_emission_texture(night_texture_path: str, sun_dir_body: np.ndarray,
                                   mask_w: int, mask_h: int, softness: float,
                                   output_path: Path) -> Path:
    """夜側マスク適用済みの夜光テクスチャを用意し、そのパスを返す。

    出力内容は (夜光テクスチャ, 本体固定系での太陽方向, マスク解像度, softness)
    だけで決まる。フレームループでこれらが変わらない場合（太陽固定・地球自転
    オフなど）は、既に書き出した前フレームの PNG をそのまま返して
    マスク生成と書き出しをまるごとスキップする。

    Args:
        night_texture_path: 元の夜間光テクスチャ（BlackMarble 等）。
        sun_dir_body: 地球本体固定系での太陽方向（自動正規化される）。
        mask_w, mask_h: 昼夜マスクの解像度 [px]。
        softness: terminator の滑らかさ（内積空間の幅）。
        output_path: このフレームで書き出す先のパス。

    Returns:
        利用すべきテクスチャのパス（キャッシュヒット時は前フレームのもの）。
    """
    sun_key = tuple(np.round(np.asarray(sun_dir_body, dtype=float),
                             _SUN_DIR_KEY_DECIMALS).tolist())
    key = ('night', str(night_texture_path), _mtime(str(night_texture_path)),
           sun_key, int(mask_w), int(mask_h), float(softness))
    cached = _GENERATED_TEXTURE_CACHE.get(key)
    if cached is not None and os.path.exists(cached):
        return Path(cached)

    night_mask = generate_night_mask(sun_dir_body, width=mask_w, height=mask_h,
                                     softness=softness)
    write_combined_night_texture(night_texture_path, night_mask, output_path)
    _GENERATED_TEXTURE_CACHE[key] = str(output_path)
    return output_path


def write_cloud_opacity_texture(cloud_texture_path: str, opacity: float,
                                output_path: Path) -> None:
    """雲テクスチャに不透明度を適用して出力 (`cloud * opacity` をプレ乗算)。"""
    cloud_texture = load_bitmap_rgb_float(cloud_texture_path)
    combined = np.clip(cloud_texture * opacity, 0.0, 1.0).astype(np.float32)
    mi.util.write_bitmap(str(output_path), combined, write_async=False)


def resolve_cloud_opacity_texture(cloud_texture_path: str, opacity: float,
                                  output_path: Path) -> Path:
    """不透明度をプレ乗算した雲テクスチャを用意し、そのパスを返す。

    出力内容は (雲テクスチャ, opacity) だけで決まりフレームに依存しないので、
    ラン中は最初の 1 フレームだけ生成して以降は使い回す。
    """
    key = ('cloud', str(cloud_texture_path), _mtime(str(cloud_texture_path)),
           float(opacity), str(output_path))
    cached = _GENERATED_TEXTURE_CACHE.get(key)
    if cached is not None and os.path.exists(cached):
        return Path(cached)

    write_cloud_opacity_texture(cloud_texture_path, opacity, output_path)
    _GENERATED_TEXTURE_CACHE[key] = str(output_path)
    return output_path


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
        'bsdf': bsdf,
        'to_world': earth_surface_transform(rotation_deg),
    }
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
        'to_world': earth_surface_transform(rotation_deg),
        'bsdf': {'type': 'null'},
        'emitter': {
            'type': 'area',
            'radiance': {
                'type': 'bitmap',
                'filename': str(texture_path)
            }
        }
    }
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
