"""
Mitsubaシーン辞書の組み立てモジュール

カメラ設定、照明システム統合、地球・衛星オブジェクトの統合、
画像処理ユーティリティ（トーンマップ、輝度計算）を提供。

責務:
    - mi.load_dict() に渡せる Mitsuba シーン辞書 (Python dict) を組み立てる
    - 地球 (scene_earth) と照明 (optical_lighting) と衛星 (scene_objects)
      を 1 つの辞書にマージするハブとして機能する
    - レンダリング後の HDR 画像のトーンマップ・輝度集計などの後処理ヘルパも提供

呼び出し元: satellite_orbit.py / simple_rotation.py / relative_motion.py のメインループ。

Mitsuba メモ (初見の人向け):
    - Mitsuba 3 はシーンを Python dict で記述する (`{'type': '...', ...}`)
      ノード型を `'type'` キーで指定し、その他のキーがプロパティになる
    - `mi.ScalarTransform4f` は 4x4 同次変換行列。`look_at()` / `translate()`
      / `rotate(axis, deg)` / `scale()` をチェーンして組み立てる
    - emitter は光源、bsdf は材質、shape (sphere/cube/cylinder/obj/ply) は形状
    - 'envmap' は環境マップ (球面 HDRI)、'directional' は平行光源、'area' は面光源
"""

import numpy as np
from pathlib import Path
from typing import Optional, Any

import mitsuba as mi

from scene_earth import (
    create_earth, create_clouds, create_night_lights, create_atmosphere,
)
from optical_lighting import (
    starfield_to_world_matrix,
    create_space_lighting_scene, SunParameters,
)


def compute_camera_up(camera_position: np.ndarray, camera_target: np.ndarray) -> np.ndarray:
    """視線に対して安定したupベクトルを計算

    視線方向と +Z (世界北) がほぼ平行 (内積 > 0.95) のときは、look_at の
    特異点を避けるため up を +Y に切り替える。
    """
    view_dir = camera_target - camera_position
    view_dir = view_dir / np.linalg.norm(view_dir)
    up = np.array([0.0, 0.0, 1.0])
    if abs(np.dot(view_dir, up)) > 0.95:
        up = np.array([0.0, 1.0, 0.0])
    return up


def compute_image_flux(image: Any) -> tuple:
    """レンダリング画像から総輝度と平均輝度を計算

    Rec.709 (sRGB) の輝度係数 (0.2126, 0.7152, 0.0722) を使用。
    光度曲線 (light curve) 解析時に使う。

    Returns:
        (total_luminance, mean_luminance)
    """
    img = np.array(image, dtype=np.float32)
    if img.ndim == 2:
        luminance = img
    else:
        luminance = 0.2126 * img[..., 0] + 0.7152 * img[..., 1] + 0.0722 * img[..., 2]
    total = float(np.sum(luminance))
    mean = float(np.mean(luminance))
    return total, mean


def apply_tonemap(image: Any, exposure: float = 0.0, gamma: float = 2.2) -> np.ndarray:
    """簡易トーンマップ（露光＋ガンマ）

    HDR (linear, 0..∞) → LDR (sRGB 近似, 0..1) 変換:
        out = clip( (img * 2^exposure)^(1/gamma), 0, 1 )

    Args:
        exposure: 露光補正 [stops] (+1 で 2 倍明るく)
        gamma:    ガンマ値 (sRGB 近似で 2.2)
    """
    img = np.array(image, dtype=np.float32)
    img = img * (2.0 ** exposure)
    img = np.clip(img, 0.0, None)
    img = np.power(img, 1.0 / gamma)
    img = np.clip(img, 0.0, 1.0)
    return img


def create_scene(camera_position=None, camera_target=None, camera_up=None,
                satellite_objects=None, sun_direction=None, earth_texture=None,
                width=1920, height=1080, fov=45, use_advanced_lighting=True,
                sun_temperature=5778.0, starfield_brightness=0.01,
                hdri_path=None,
                include_earth: bool = True, include_envmap: bool = True,
                sample_count: int = 128, earth_rotation_deg: float = 0.0,
                earth_night_texture: Optional[str] = None,
                night_emission_texture: Optional[str] = None,
                earth_cloud_texture: Optional[str] = None,
                use_night_lights: bool = False,
                use_clouds: bool = False,
                cloud_opacity: float = 0.5,
                use_atmosphere: bool = False,
                atmosphere_density: float = 0.02,
                atmosphere_radius_scale: float = 1.03):
    """
    Mitsubaシーンを作成

    返り値の dict は `mi.load_dict(scene_dict)` でシーンインスタンスに変換できる。
    トップレベルキー:
        'type': 'scene'         (固定)
        'integrator':           積分器 (パストレーサ等)
        'camera':               センサ (perspective + hdrfilm + sampler)
        'earth' / 'clouds' / 'night_lights' / 'atmosphere':
                                地球関連シェイプ (オプション)
        'sun' / 'envmap' / その他: 照明
        その他衛星オブジェクト (satellite_objects から展開)

    積分器・センサの選択意図:
        - 'path' は単方向パストレーサ (max_depth=12 でガラス越し等の多重反射に対応)
        - 'perspective' + 'hdrfilm' で線形 HDR を出力 → apply_tonemap で sRGB 化
        - 'gaussian' rfilter は高品質再構成フィルタ (デフォルト)
        - 'independent' sampler は単純な乱数サンプラ (spp = sample_count)

    Args:
        camera_position: カメラの位置 (シーン単位)
        camera_target: カメラの注視点
        camera_up: カメラの上方向
        satellite_objects: 衛星オブジェクトの辞書 (scene_objects.create_satellite 等の出力)
        sun_direction: 太陽方向ベクトル (3 要素、シーン座標、自動正規化される)
        earth_texture: 地球のテクスチャパス（昼）
        earth_night_texture: 夜間光テクスチャパス（合成用）
        night_emission_texture: 夜間光（夜側マスク適用済み）テクスチャパス
        earth_cloud_texture: 雲テクスチャパス
        width: 画像の幅 [px]
        height: 画像の高さ [px]
        fov: 視野角 [度]
        use_advanced_lighting: 高度な照明を使用 (黒体放射太陽 + 星空 envmap)
        sun_temperature: 太陽の色温度 [K]
        starfield_brightness: 星空の明るさ倍率

    Returns:
        Mitsubaシーン辞書 (mi.load_dict() に渡す)
    """
    # 既定値の補完 (ECI 原点を見るカメラ・天頂up・+X からの太陽光)
    if camera_target is None:
        camera_target = [0, 0, 0]
    if camera_up is None:
        camera_up = [0, 0, 1]
    if sun_direction is None:
        sun_direction = [1, 0, 0]

    # 太陽方向を正規化
    sun_dir_normalized = sun_direction / np.linalg.norm(sun_direction)

    # シーン辞書の骨格。ここに地球・照明・衛星を順次追加する。
    scene_dict = {
        'type': 'scene',

        # 積分器（レンダリング手法）
        # 大気（null BSDF + homogeneous 媒質のシェル）を含むシーンでは 'path' が
        # 媒質透過の影レイ（NEE）を扱えず、太陽の直接光が全遮断されて地球が
        # 真っ暗になる。媒質があるときだけ 'volpath' に切り替える。
        'integrator': {
            'type': 'volpath' if use_atmosphere else 'path',
            'max_depth': 12,
        },

        # カメラ
        'camera': {
            'type': 'perspective',
            'fov': fov,
            'near_clip': 1e-6,
            # look_at: カメラ姿勢を origin/target/up から決定 (Mitsuba 標準ヘルパ)
            'to_world': mi.ScalarTransform4f.look_at(
                origin=camera_position,
                target=camera_target,
                up=camera_up
            ),
            'film': {
                # hdrfilm は線形 RGB の HDR バッファを出力 (後段でトーンマップする)
                'type': 'hdrfilm',
                'width': width,
                'height': height,
                'rfilter': {
                    'type': 'gaussian',
                },
            },
            'sampler': {
                'type': 'independent',
                'sample_count': 128,  # 高品質 (この値は最後に sample_count で上書きされる)
            },
        },

    }

    # 地球
    if include_earth:
        # 地球本体: 半径 1.0 のシーン単位で生成 (実距離は SCALE_FACTOR で別途換算)
        scene_dict['earth'] = create_earth(
            radius=1.0,
            texture_path=earth_texture,
            rotation_deg=earth_rotation_deg
        )

        # 雲レイヤー: 地表より少し外 (1.008) のシェルとして mask BSDF で重ねる
        if use_clouds and earth_cloud_texture and Path(earth_cloud_texture).exists():
            scene_dict['clouds'] = create_clouds(
                radius=1.008,
                texture_path=earth_cloud_texture,
                opacity=cloud_opacity,
                rotation_deg=earth_rotation_deg
            )

        # 夜間光 (BlackMarble 等): 地表より僅かに外 (1.002) で発光体として配置
        if use_night_lights and night_emission_texture:
            if Path(night_emission_texture).exists():
                scene_dict['night_lights'] = create_night_lights(
                    radius=1.002,
                    texture_path=night_emission_texture,
                    rotation_deg=earth_rotation_deg
                )

        # 大気シェル: homogeneous medium を含む球で簡易レイリー散乱を表現
        if use_atmosphere:
            scene_dict['atmosphere'] = create_atmosphere(
                radius=atmosphere_radius_scale,
                density=atmosphere_density
            )

    # 照明システム
    if use_advanced_lighting:
        # 高度な照明システムを使用
        # 黒体放射ベースの太陽 + (HDRI or 星空) を一括生成
        sun_params = SunParameters(temperature=sun_temperature)
        lighting = create_space_lighting_scene(
            sun_direction=sun_dir_normalized,
            enable_moon=False,
            starfield_brightness=starfield_brightness,
            sun_params=sun_params,
            hdri_path=hdri_path,
        )

        # 照明を追加
        # 'starfield' のみ envmap 扱いで include_envmap フラグに従う
        for light_name, light_dict in lighting.items():
            if light_name == 'starfield':
                if include_envmap:
                    if light_dict.get('type') == 'envmap':
                        # 星図規約 → ECI（シーン = ECI をスケールしたもの）の固定回転
                        light_dict = dict(light_dict)
                        light_dict['to_world'] = mi.ScalarTransform4f(
                            starfield_to_world_matrix().tolist())
                    scene_dict['envmap'] = light_dict
            else:
                scene_dict[light_name] = light_dict
    else:
        # 従来の照明システム (advanced_optics=False の互換パス)
        # 'directional' は無限遠の平行光源、irradiance は線形 RGB [W/m^2 相当]
        # `direction` は光が進む向き → 地球→太陽の sun_dir_normalized を反転して渡す
        scene_dict['sun'] = {
            'type': 'directional',
            'direction': (-sun_dir_normalized).tolist(),
            'irradiance': {
                'type': 'rgb',
                'value': [5.0, 5.0, 4.8],
            }
        }
        if include_envmap:
            if hdri_path:
                # HDRI 環境マップ (球面ラップ)
                scene_dict['envmap'] = {
                    'type': 'envmap',
                    'filename': hdri_path,
                    'scale': starfield_brightness,
                    'to_world': mi.ScalarTransform4f(starfield_to_world_matrix().tolist()),
                }
            else:
                # 暗い一様な背景 (深宇宙の代用)。色比は青寄りのまま、
                # 全体倍率は starfield_brightness にスケールさせる。
                scene_dict['envmap'] = {
                    'type': 'constant',
                    'radiance': {
                        'type': 'rgb',
                        'value': [
                            0.5 * starfield_brightness,
                            0.5 * starfield_brightness,
                            1.0 * starfield_brightness,
                        ],
                    }
                }

    # 衛星オブジェクトを追加
    # satellite_objects は {key: shape_dict, ...} の形 (scene_objects 側で組み立て済み)
    if satellite_objects:
        for obj_name, obj_dict in satellite_objects.items():
            scene_dict[obj_name] = obj_dict

    # サンプル数
    # 上の sampler 初期値 128 をユーザ指定で上書き (CLI/YAML の --samples)
    scene_dict['camera']['sampler']['sample_count'] = int(sample_count)

    return scene_dict
