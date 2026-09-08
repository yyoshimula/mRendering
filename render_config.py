"""レンダリング設定のデータ型と定数。

このモジュールは satellite_orbit.py / mrender.py から参照され、
CLI と YAML プリセットの両方が最終的にここで定義された dataclass に集約される。

責務:
    - 軌道、地球、照明、アニメーション、カメラ、物体仕様の dataclass を提供
    - レンダリングに必要な「単位 / 座標系 / デフォルト値」をひとまとまりに保持

各 dataclass のフィールドにはコメントで「単位」「座標系」「初期値の根拠」を併記している。
ここを変更すると config_loader.py 側の build_render_config() / resolve_*_object() の
組み立て規則も整合させる必要があるので注意。
"""

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import numpy as np

from orbit_mechanics import OrbitalElements, NumericalPropagator, CsvEphemeris


# 軌道要素のデフォルト値 (ISS 相当の円軌道を想定)
#   altitude     : 高度 [km] (地表からの距離。a = R_earth + altitude)
#   eccentricity : 離心率 [-]
#   inclination  : 軌道傾斜角 [deg]
#   raan         : 昇交点赤経 RAAN [deg]
#   arg_periapsis: 近点引数 [deg]
#   mean_anomaly : 平均近点角 (t=0 における初期位相) [deg]
DEFAULT_ORBIT_SETTINGS = {
    'altitude': 408.0,       # ISS の代表高度 [km]
    'eccentricity': 0.0,     # 円軌道
    'inclination': 51.6,     # ISS の軌道傾斜角 [deg]
    'raan': 0.0,
    'arg_periapsis': 0.0,
    'mean_anomaly': 0.0,
}

# YAML セクション名のエイリアス。空でも load_yaml_config() は動く。
# 例: SECTION_ALIASES = {'cam': 'camera'} のように後付け可能。
SECTION_ALIASES = {}


@dataclass
class CameraOverrides:
    """YAML / CLI で `camera.origin` などを直接指定したとき用の上書き値。

    すべて None の場合は view_mode に応じた自動カメラ計算が行われる。

    Attributes:
        origin: カメラ位置 (シーン単位、地球半径=1.0)
        target: 注視点 (シーン単位)
        fov:    視野角 [deg]
        up:     上方向ベクトル (シーン単位)
    """
    origin: Optional[Sequence[float]] = None
    target: Optional[Sequence[float]] = None
    fov: Optional[float] = None
    up: Optional[Sequence[float]] = None


@dataclass
class CameraConfig:
    """カメラ配置とビュー切り替えの設定。

    座標系:
        外部ビューや lat/lon は ECI (Earth-Centered Inertial) を基準に計算され、
        最終的にシーン単位 (地球半径=1.0) に正規化される。

    Attributes:
        view_mode:          'inertial' / 'chase' / 'satellite' / 'earth' のビュー名
        target_lat:         注視点緯度 [deg]、None で未指定
        target_lon:         注視点経度 [deg]、None で未指定
        target_alt_km:      注視点の高度 [km]
        chase_back:         チェースカメラの後方オフセット (シーン単位)
        chase_out:          チェースカメラの法線方向オフセット (シーン単位)
        inertial_fixed:     慣性系俯瞰ビュー (inertial) を固定するか (True なら回転しない)
        inertial_distance:  慣性系俯瞰ビュー (inertial) の地心距離 (シーン単位、地球半径=1.0)
        overrides:          ユーザ手指定があればこちらを優先
        view_camera_name:   多物体時にカメラ位置として使う物体名
        view_target_name:   多物体時に注視対象として使う物体名
    """
    view_mode: str
    target_lat: Optional[float]
    target_lon: Optional[float]
    target_alt_km: float
    chase_back: float
    chase_out: float
    inertial_fixed: bool
    inertial_distance: float
    overrides: CameraOverrides
    view_camera_name: Optional[str] = None
    view_target_name: Optional[str] = None


@dataclass
class EarthConfig:
    """地球モデル (本体・雲・夜間光・大気) の構成パラメータ。

    座標系/単位:
        - シーン上の地球半径は 1.0 に正規化される (scene_earth.create_earth)
        - cloud_opacity, atmosphere_density は無次元 [0,1] 付近のスカラー
        - earth_rotation_period_hours は恒星時 (sidereal day) ベースで 23.934 h を想定

    Attributes:
        day_texture:                昼側テクスチャ画像パス (equirectangular 推奨)
        night_texture:              夜間光テクスチャ画像パス (例: BlackMarble)
        cloud_texture:              雲テクスチャ画像パス
        use_night_lights:           夜間光レイヤーを描画するか
        use_clouds:                 雲レイヤーを描画するか
        cloud_opacity:              雲の不透明度 [0..1]
        night_mask_res:             夜マスクの解像度 [px] (高いほど terminator が滑らか)
        night_terminator_softness:  昼夜境界のソフトネス (smoothstep の幅)
        use_atmosphere:             大気シェルを追加するか
        atmosphere_density:         大気の散乱係数スケール
        atmosphere_radius_scale:    大気層の半径 (1.0 が地表、>1.0 で外側)
        earth_rotation:             地球自転を有効にするか
        earth_rotation_period_hours:1 自転にかかる時間 [h] (デフォルト恒星日)
        earth_rotation_speed:       自転速度の倍率 (デバッグ用)
        earth_only:                 地球のみ (衛星等を描画しない) モード
    """
    day_texture: Optional[str]
    night_texture: Optional[str]
    cloud_texture: Optional[str]
    use_night_lights: bool
    use_clouds: bool
    cloud_opacity: float
    night_mask_res: int
    night_terminator_softness: float
    use_atmosphere: bool
    atmosphere_density: float
    atmosphere_radius_scale: float
    earth_rotation: bool
    earth_rotation_period_hours: float
    earth_rotation_speed: float
    earth_only: bool


@dataclass
class LightingConfig:
    """太陽・環境光・サンプリング・トーンマップ設定。

    Attributes:
        use_advanced_optics:    advanced 光学 (黒体放射太陽 + starfield) を使う
        sun_temperature:        太陽の有効温度 [K] (5778 K が物理値)
        sun_angle:              太陽方向の手動指定 [deg]、None で自動
        sun_rotate:             時間経過で太陽方向を回転させるか
        sun_rotate_period_s:    太陽回転の周期 [s]
        sun_rotate_speed:       回転速度倍率
        starfield_brightness:   星空 envmap の倍率
        hdri_path:              HDRI 環境マップ画像パス (None なら星空 or 暗黒)
        show_orbit:             軌道線を描画するか
        orbit_line_radius:      軌跡シリンダーの半径 (シーン単位、地球半径=1.0)
        orbit_line_color:       軌跡 RGB 色 (各成分 [0..1])
        orbit_line_glow:        軌跡の自発光倍率 (0 で発光なし、>0 で area emitter 付与)
        sample_count:           パストレーシングの spp (samples per pixel)
        exposure:               露光補正 [stops] (2^exposure 倍)
        gamma:                  ガンマ補正値 (sRGB 近似で 2.2)
    """
    use_advanced_optics: bool
    sun_temperature: float
    sun_angle: Optional[float]
    sun_rotate: bool
    sun_rotate_period_s: float
    sun_rotate_speed: float
    starfield_brightness: float
    hdri_path: Optional[str]
    show_orbit: bool
    orbit_line_radius: float
    orbit_line_color: Tuple[float, float, float]
    orbit_line_glow: float
    sample_count: int
    exposure: float
    gamma: float


@dataclass
class AnimationConfig:
    """フレーム時間軸の設定。

    Attributes:
        orbit_speed:   軌道進行のスケール (1.0 で実時間相当)
        duration_sec:  シミュレーション時間 [s]、None なら frames から逆算
        fps:           出力動画の FPS、None なら frames/duration から逆算
        start_time:    シミュレーション開始時刻 [s] (epoch オフセット)
        epoch_jd:      t=0 に対応する UTC のユリウス日。None なら簡易モデル
                       （t=0 で太陽=+x・グリニッジ=+x という暗黙のエポック ≈ 春分の
                       GMST=0 時刻）。指定時は太陽=VSOP87、自転角=GMST の実時刻
    """
    orbit_speed: float
    duration_sec: Optional[float]
    fps: Optional[float]
    start_time: float = 0.0
    epoch_jd: Optional[float] = None


@dataclass
class RenderConfig:
    """1 回のレンダリングジョブを記述するトップレベル設定。

    Attributes:
        width / height: 出力画像サイズ [px]
        camera:    カメラ設定
        earth:     地球モデル設定
        lighting:  照明・トーンマップ設定
        animation: 時間軸設定
    """
    width: int
    height: int
    camera: CameraConfig
    earth: EarthConfig
    lighting: LightingConfig
    animation: AnimationConfig


@dataclass
class OrbitSettings:
    """ユーザ入力レベルの軌道要素 (角度は deg、a の代わりに altitude [km])。

    `build_orbital_elements()` で `OrbitalElements` (角度 rad、a [km]) に変換される。
    """
    altitude: float        # 高度 [km] (a - R_earth)
    eccentricity: float    # 離心率 [-]
    inclination: float     # 軌道傾斜角 [deg]
    raan: float            # 昇交点赤経 RAAN [deg]
    arg_periapsis: float   # 近点引数 [deg]
    mean_anomaly: float    # 平均近点角 [deg] (t=0 の初期位相)


@dataclass
class ObjectSpec:
    """軌道周回物体 1 機の静的な仕様 (時間に依らないパラメータ)。

    時々刻々の状態は `ObjectState` 側に保持する。

    Attributes:
        name:                  物体識別名 (シーン辞書のキー prefix にも使う)
        orbital_elements:      ラジアン/km 単位の軌道要素
        orbit:                 入力レベルの設定 (deg/km、再構成や表示用に保持)
        attitude_mode:         'nadir' / 'sun_tracking' / 'velocity_aligned' など
        scale:                 シーン単位のモデルスケール (地球半径=1.0)
        model_path:            外部モデル (.obj/.ply/.glb) パス、None で procedural
        model_material:        BSDF 名 (resolve_material_by_name の引数)
        model_keep_materials:  True で OBJ の MTL をそのまま使う
        propagator:            数値伝播器 (J2/drag を使うとき)
        ephemeris:             CSV エフェメリス (CsvEphemeris、外部軌道データ供給時)
    """
    name: str
    orbital_elements: OrbitalElements
    orbit: OrbitSettings
    attitude_mode: str
    scale: float
    model_path: Optional[str]
    model_material: Optional[str]
    model_keep_materials: bool
    propagator: Optional[NumericalPropagator] = None
    ephemeris: Optional[CsvEphemeris] = None


@dataclass
class ObjectState:
    """ある時刻 t における物体の動的状態。

    Attributes:
        spec:             この物体の静的仕様 (ObjectSpec) への参照
        time_s:           シミュレーション時刻 [s]
        position_km:      ECI 位置 [km]
        velocity_km:      ECI 速度 [km/s] (要素名は km だが単位は km/s)
        attitude_matrix:  Body→ECI の 3x3 回転行列
    """
    spec: ObjectSpec
    time_s: float
    position_km: np.ndarray
    velocity_km: np.ndarray
    attitude_matrix: np.ndarray
