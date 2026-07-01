#!/usr/bin/env python3
"""
照明・環境光モジュール

宇宙シーンに必要な天体照明の物理ベースモデルと、Mitsuba 3 の光源辞書生成を行う。
扱う物理現象:
  - 黒体放射: 温度 T の理想黒体が放つスペクトルを Planck 則
        B_λ(T) = (2hc²/λ⁵) / (exp(hc/(λk_BT)) - 1)
    で計算し、可視波長帯の積分から CIE XYZ → sRGB に変換するのが厳密だが、
    本モジュールは Tanner Helland の経験式で近似する。
  - 太陽は実効温度 5778 K の黒体に近い（光球の有効温度）。
  - 視等級 m: m₁ - m₂ = -2.5 log₁₀(I₁ / I₂)（Pogson の式）。
    太陽 m_⊙ = -26.74、満月 m_moon ≈ -12.6 等を基準に強度比を導く。
  - 星空・地球照は環境光（envmap / constant）として近似する。
  - 太陽・月の位置は J2000.0 ユリウス日基準の簡易ケプラー近似。

使用側: scene_builder.py / satellite_orbit.py からシーン辞書に渡される。
Mitsuba の `directional` `constant` `envmap` プラグインに合わせた dict を返す。
"""

import numpy as np
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass


@dataclass
class SunParameters:
    """太陽光パラメータ

    太陽光球の有効温度と地球軌道（1 AU）における放射照度（太陽定数）を保持する。
    """
    # 太陽の色温度 [K]
    # 5778 K は太陽光球の有効温度。Planck 則のピークは λ_max ≈ 502 nm（緑）。
    temperature: float = 5778.0

    # 太陽光の強度（地球軌道での太陽定数） [W/m^2]
    # 実際の値: 1361 W/m^2（Total Solar Irradiance, TSI）
    solar_constant: float = 1361.0

    # レンダリング用のスケーリング係数（実 SI 値そのままだとトーンマッピング外になる）
    intensity_scale: float = 5.0


@dataclass
class MoonParameters:
    """月光パラメータ

    月は太陽光の反射体だが、レンダリング上は視等級ベースで強度を逆算する。
    """
    # 満月の視等級（地球から見た見かけの明るさ。負ほど明るい）
    magnitude: float = -12.6

    # 月の色（月面レゴリスの反射スペクトル。太陽光よりやや黄色寄り）
    color_rgb: Tuple[float, float, float] = (1.0, 0.98, 0.95)

    # 月の位相（0.0=新月, 0.5=半月, 1.0=満月）
    # 厳密には位相積分（Hapke モデル等）が必要だが、ここでは線形比例で近似
    phase: float = 1.0


def blackbody_to_rgb(temperature_kelvin: float) -> np.ndarray:
    """
    黒体放射の色温度から sRGB 色を計算

    厳密には Planck 則 B_λ(T) を可視波長域で CIE 等色関数と畳み込み、
    XYZ → sRGB 変換するのが正攻法。本関数は Tanner Helland の経験式で
    その結果を区分多項式・対数で近似する（実装が単純で十分な視覚的精度）。

    Args:
        temperature_kelvin: 色温度 [K]（例: 太陽 5778, 白熱電球 2700, 青空 ~10000）

    Returns:
        RGB値（正規化済み、各成分 0-1。sRGB 想定）
    """
    # 簡易的な色温度→RGB変換（経験式）
    # 出典: Tanner Helland's algorithm

    temp = temperature_kelvin / 100.0

    # Red
    if temp <= 66:
        red = 1.0
    else:
        red = temp - 60
        red = 329.698727446 * (red ** -0.1332047592)
        red = np.clip(red / 255.0, 0, 1)

    # Green
    if temp <= 66:
        green = temp
        green = 99.4708025861 * np.log(green) - 161.1195681661
        green = np.clip(green / 255.0, 0, 1)
    else:
        green = temp - 60
        green = 288.1221695283 * (green ** -0.0755148492)
        green = np.clip(green / 255.0, 0, 1)

    # Blue
    if temp >= 66:
        blue = 1.0
    elif temp <= 19:
        blue = 0.0
    else:
        blue = temp - 10
        blue = 138.5177312231 * np.log(blue) - 305.0447927307
        blue = np.clip(blue / 255.0, 0, 1)

    return np.array([red, green, blue])


def solar_irradiance_to_render_scale(solar_constant: float, intensity_scale: float = 5.0) -> float:
    """
    実際の太陽放射照度をレンダリング用にスケーリング

    実物理値の 1361 W/m² をそのまま使うと Mitsuba 内部の浮動小数点と
    トーンマップで扱いづらいため、ユーザ指定の intensity_scale を返すだけの
    「正規化レイヤ」として機能する。引数 solar_constant は将来の拡張用。

    Args:
        solar_constant: 太陽定数 [W/m^2]（現状は未使用）
        intensity_scale: レンダリング用スケール係数

    Returns:
        レンダリング用の強度値
    """
    # Mitsubaのレンダリングスケールに合わせて調整
    # 実際の物理値は非常に大きいため、適切にスケーリング
    return intensity_scale


def create_sun_light(
    sun_direction: np.ndarray,
    params: Optional[SunParameters] = None
) -> Dict[str, Any]:
    """
    太陽光源（平行光源）を作成

    地球軌道スケールでは太陽の角直径は約 0.5° と小さく、平行光近似で十分。
    Mitsuba の `directional` プラグインを使う:
      - type: 'directional' = 単一方向の平行光
      - direction: 光が進む向き（光源 → シーン）
      - irradiance: その方向に直交する面で受ける放射照度（RGB）

    Args:
        sun_direction: 太陽方向ベクトル（正規化済み、シーンに向かう向き）
        params: 太陽パラメータ

    Returns:
        Mitsuba光源辞書
    """
    if params is None:
        params = SunParameters()

    # 色温度からRGB色を計算（5778 K → ほぼ白）
    sun_color = blackbody_to_rgb(params.temperature)

    # 強度をスケーリング
    intensity = solar_irradiance_to_render_scale(params.solar_constant, params.intensity_scale)

    # Mitsuba の `directional.direction` は「光が進む向き」。呼び出し側は
    # 地球→太陽（天空での太陽方向）を渡してくる規約なので、ここで符号反転して
    # 太陽→シーンに直す。これを忘れると照明が裏面から当たり背景以外真っ黒になる。
    light_direction = (-sun_direction).tolist()

    return {
        'type': 'directional',
        'direction': light_direction,
        'irradiance': {
            'type': 'rgb',
            'value': (sun_color * intensity).tolist()
        }
    }


def magnitude_to_intensity(magnitude: float) -> float:
    """
    天体の視等級を相対強度に変換（Pogson の式）

    視等級の定義（19 世紀の Pogson による標準化）:
        m₁ - m₂ = -2.5 * log₁₀(I₁ / I₂)
    すなわち 5 等級差で輝度比 100 倍。本関数は太陽 (m=-26.74) を 1.0 とした
    相対強度を返す。

    Args:
        magnitude: 視等級（負ほど明るい）

    Returns:
        相対強度（太陽を基準=1.0とした場合。例: 満月 ~3e-6）
    """
    # 太陽の視等級: -26.74
    sun_magnitude = -26.74

    # 強度比を計算: I/I_sun = 10^(-Δm/2.5)
    delta_mag = magnitude - sun_magnitude
    intensity_ratio = 10 ** (-delta_mag / 2.5)

    return intensity_ratio


def create_moon_light(
    moon_direction: np.ndarray,
    params: Optional[MoonParameters] = None
) -> Dict[str, Any]:
    """
    月光源（平行光源）を作成

    月の角直径も約 0.5° で平行光近似。視等級ベースで太陽強度に対する比を求め、
    位相 (新月→満月) で線形に補正する。

    Args:
        moon_direction: 月方向ベクトル（正規化済み）
        params: 月パラメータ

    Returns:
        Mitsuba光源辞書
    """
    if params is None:
        params = MoonParameters()

    # 位相による明るさ補正（0=新月で消灯、1=満月でフル）
    phase_factor = params.phase

    # 等級 → 太陽比の強度。1000 倍の係数は表示上の見栄えを優先した経験的スケール
    intensity = magnitude_to_intensity(params.magnitude) * phase_factor * 1000.0  # スケール調整

    moon_color = np.array(params.color_rgb)

    return {
        'type': 'directional',
        'direction': moon_direction.tolist(),
        'irradiance': {
            'type': 'rgb',
            'value': (moon_color * intensity).tolist()
        }
    }


def create_starfield_envmap(
    brightness: float = 0.01,
    color_tint: Tuple[float, float, float] = (1.0, 1.0, 1.05),
    hdri_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    星空環境マップを作成

    HDRI 画像（equirectangular 投影の EXR 等）が与えられれば `envmap` プラグインで
    全天画像をシーン背景・無限遠光源として使う。なければ無方向の `constant` 光源で
    代替（暗い均一な宇宙背景）。

    Args:
        brightness: 星空の明るさ（envmap の場合は scale 倍率）
        color_tint: 色調（デフォルトはやや青みがかる）
        hdri_path: HDRI環境マップのパス（tools/generate_starfield.py が生成）

    Returns:
        Mitsuba環境光源辞書
    """
    if hdri_path:
        # HDRI画像を使用（equirectangular EXR 等）
        return {
            'type': 'envmap',
            'filename': hdri_path,
            'scale': brightness,
        }
    else:
        # 単色の暗い宇宙背景（全方向から一様な放射）
        color = np.array(color_tint) * brightness
        return {
            'type': 'constant',
            'radiance': {
                'type': 'rgb',
                'value': color.tolist()
            }
        }


def create_earth_shine(
    intensity: float = 0.3,
    color: Tuple[float, float, float] = (0.8, 0.9, 1.0)
) -> Dict[str, Any]:
    """
    地球照（アルベド光）を環境光として近似

    実際の地球照は地球の方向（半球）から強く受ける指向性のある照明だが、
    ここでは簡易に全方向均一の `constant` 光源として近似する。
    青みは地球（海洋・大気散乱）の典型的な反射色を反映。

    注意: 実際の地球照は方向性があるため、この関数は簡易版です。
    より正確なシミュレーションにはoptical_atmosphere.pyのcompute_earth_albedoを使用してください。

    Args:
        intensity: 地球照の強度
        color: 地球照の色（やや青みがかる）

    Returns:
        Mitsuba環境光源辞書
    """
    earth_color = np.array(color) * intensity

    return {
        'type': 'constant',
        'radiance': {
            'type': 'rgb',
            'value': earth_color.tolist()
        }
    }


def compute_sun_position_detailed(
    julian_date: float,
    simplified: bool = True
) -> np.ndarray:
    """
    ユリウス日から太陽位置を計算

    簡易版: 地球公転を円軌道とみなし、黄経が時間に比例して進むと近似。
    黄道座標 → ECI（赤道座標）への変換は黄道傾斜角 ε ≈ 23.44° の
    x 軸まわり回転で行う:
        x_ECI = cos λ
        y_ECI = sin λ * cos ε
        z_ECI = sin λ * sin ε

    Args:
        julian_date: ユリウス日
        simplified: 簡易モデルを使用（True）か、より正確なモデルを使用（False）

    Returns:
        太陽方向ベクトル（ECI座標系、正規化済み）
    """
    if simplified:
        # 簡易モデル: 年周運動のみ
        days_since_j2000 = julian_date - 2451545.0  # J2000.0からの経過日数
        sun_angle = 2 * np.pi * days_since_j2000 / 365.25

        # 黄道傾斜角（地球自転軸と公転面の傾き）
        obliquity = np.radians(23.44)

        sun_direction = np.array([
            np.cos(sun_angle),
            np.sin(sun_angle) * np.cos(obliquity),
            np.sin(sun_angle) * np.sin(obliquity)
        ])
    else:
        # より詳細なモデル（将来の拡張用）
        # TODO: より正確な太陽位置計算（黄経、離心率等を考慮、VSOP87 など）
        sun_direction = compute_sun_position_detailed(julian_date, simplified=True)

    return sun_direction / np.linalg.norm(sun_direction)


def compute_moon_position(
    julian_date: float,
    simplified: bool = True
) -> Tuple[np.ndarray, float]:
    """
    ユリウス日から月の位置と位相を計算

    周期:
      - 月の公転周期 (恒星月) = 27.3 日: 月が天球上で 1 周
      - 朔望月 = 29.5 日: 新月→満月→新月の周期（地球の公転を加味）
    位相は phase = (1 + cos(2π t / 29.5)) / 2 で 0..1 に正規化。

    Args:
        julian_date: ユリウス日
        simplified: 簡易モデルを使用

    Returns:
        (moon_direction, phase):
            - moon_direction: 月方向ベクトル（正規化済み）
            - phase: 月の位相（0.0=新月, 1.0=満月）
    """
    if simplified:
        # 簡易モデル
        days_since_j2000 = julian_date - 2451545.0
        moon_angle = 2 * np.pi * days_since_j2000 / 27.3  # 月の公転周期: 27.3日

        # 月の軌道傾斜角（簡易版、白道と黄道のなす角）
        inclination = np.radians(5.14)

        moon_direction = np.array([
            np.cos(moon_angle),
            np.sin(moon_angle) * np.cos(inclination),
            np.sin(moon_angle) * np.sin(inclination)
        ])

        # 位相計算（新月→満月の周期: 29.5日）
        phase_angle = 2 * np.pi * days_since_j2000 / 29.5
        phase = (1 + np.cos(phase_angle)) / 2.0  # 0.0=新月, 1.0=満月
    else:
        # 詳細モデル（将来の拡張用）
        moon_direction, phase = compute_moon_position(julian_date, simplified=True)

    return moon_direction / np.linalg.norm(moon_direction), phase


def create_space_lighting_scene(
    sun_direction: np.ndarray,
    enable_moon: bool = False,
    moon_direction: Optional[np.ndarray] = None,
    moon_phase: float = 1.0,
    starfield_brightness: float = 0.01,
    sun_params: Optional[SunParameters] = None,
    hdri_path: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    宇宙環境の完全な照明シーンを作成

    必須の太陽光に加え、オプションで月光・星空 HDRI を組み合わせて
    Mitsuba シーン辞書に登録できる形で返す。
    返り値の各キー（'sun', 'moon', 'starfield'）はそのまま
    Mitsuba scene dict にトップレベル追加できる。

    Args:
        sun_direction: 太陽方向ベクトル
        enable_moon: 月光を有効化
        moon_direction: 月方向ベクトル
        moon_phase: 月の位相（0.0-1.0）
        starfield_brightness: 星空の明るさ
        sun_params: 太陽パラメータ

    Returns:
        照明オブジェクトの辞書（Mitsubaシーンに統合可能）
    """
    lighting = {}

    # 太陽光
    lighting['sun'] = create_sun_light(sun_direction, sun_params)

    # 月光（オプション）
    if enable_moon and moon_direction is not None:
        moon_params = MoonParameters(phase=moon_phase)
        lighting['moon'] = create_moon_light(moon_direction, moon_params)

    # 星空環境光
    lighting['starfield'] = create_starfield_envmap(brightness=starfield_brightness, hdri_path=hdri_path)

    return lighting


class LightingPresets:
    """照明プリセット

    シーン用途別に SunParameters の典型値をまとめたファクトリ。
    """

    @staticmethod
    def daylight() -> SunParameters:
        """昼間の太陽光（標準: 5778 K）"""
        return SunParameters(temperature=5778.0, intensity_scale=5.0)

    @staticmethod
    def sunset() -> SunParameters:
        """夕方の太陽光

        大気中の長い経路でレイリー散乱が短波長を奪い、見かけの色温度が下がる。
        3500 K は夕焼け〜白熱電球程度の暖色。
        """
        return SunParameters(temperature=3500.0, intensity_scale=3.0)

    @staticmethod
    def eclipse() -> SunParameters:
        """日食時の太陽光（皆既日食前後の薄明、強度を大幅に減衰）"""
        return SunParameters(temperature=5778.0, intensity_scale=0.5)


if __name__ == "__main__":
    # テスト
    print("照明モジュールのテスト\n")

    print("=== 色温度テスト ===")
    temps = [2000, 3500, 5778, 6500, 10000]
    for temp in temps:
        rgb = blackbody_to_rgb(temp)
        print(f"{temp}K: RGB = ({rgb[0]:.3f}, {rgb[1]:.3f}, {rgb[2]:.3f})")

    print("\n=== 太陽光源 ===")
    sun_dir = np.array([1, 0, 0])
    sun_light = create_sun_light(sun_dir)
    print(f"Type: {sun_light['type']}")
    print(f"Direction: {sun_light['direction']}")

    print("\n=== 月光源 ===")
    moon_dir = np.array([0, 1, 0])
    moon_light = create_moon_light(moon_dir)
    print(f"Type: {moon_light['type']}")

    print("\n=== 宇宙照明シーン ===")
    lighting_scene = create_space_lighting_scene(
        sun_direction=sun_dir,
        enable_moon=True,
        moon_direction=moon_dir,
        moon_phase=0.8
    )
    print(f"照明オブジェクト数: {len(lighting_scene)}")
    print(f"含まれる光源: {list(lighting_scene.keys())}")
