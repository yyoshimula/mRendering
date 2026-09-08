#!/usr/bin/env python3
"""
大気散乱シミュレーションモジュール

地球大気中での光の散乱・減衰を物理ベースで近似計算するモジュール。
扱う物理現象:
  - レイリー散乱: 分子サイズ << 波長 のとき支配的。散乱断面積が λ^(-4) に比例し、
    青空・赤い夕焼けの主因。位相関数 P(θ) = (3/4) * (1 + cos²θ)。
  - ミー散乱: エアロゾル等の粒子サイズ ~ 波長 のとき支配的。前方散乱が強く、
    Henyey-Greenstein 位相関数 P_HG(cosθ; g) で近似する。
  - 指数大気密度モデル: ρ(h) = ρ₀ * exp(-h / H)、スケールハイト H は
    レイリー成分 ~8 km、ミー成分 ~1.2 km。
  - 光学的厚さ τ の経路積分と Beer-Lambert 則 I = I0 * exp(-τ)。
  - 地球アルベド（地球照）の立体角ベース簡易計算。

使用側: scene_earth.py / scene_builder.py 等で大気カラーや地球照を組み合わせる際に
参照される。Mitsuba 自体には大気散乱 BSDF はないため、ここで前計算した量を
背景色や追加光源として渡す想定。
"""

import numpy as np
from typing import Tuple, Optional
from dataclasses import dataclass


@dataclass
class AtmosphereParameters:
    """大気パラメータ

    地球大気の散乱・密度モデルを保持するデータクラス。
    スケールハイト H と海面密度の散乱係数 β₀ から、任意高度の散乱係数は
    β(h) = β₀ * exp(-h / H) で与えられる（指数大気近似）。
    """
    # 地球大気のスケールハイト [km]
    # 物理的には H = k_B T / (m g)（等温大気）。実大気は近似的にこの値。
    rayleigh_scale_height: float = 8.0      # レイリー散乱のスケールハイト（分子大気）
    mie_scale_height: float = 1.2           # ミー散乱のスケールハイト（エアロゾル）

    # 散乱係数 [1/km] at sea level
    # 波長 λ=550nm (緑) での標準値。波長依存は wavelength_dependent_rayleigh で扱う。
    rayleigh_coeff: float = 0.0058          # レイリー散乱係数 β_R (550nm)
    mie_coeff: float = 0.0021               # ミー散乱係数 β_M (波長依存はここでは無視)

    # ミー散乱の異方性パラメータ（Henyey-Greenstein位相関数）
    # g は <cosθ> の値に相当する。海洋雲・霞は ~0.76 程度の強い前方散乱。
    mie_g: float = 0.76                     # g = 0: 等方散乱, g → 1: 前方散乱

    # 地球半径 [km]（球対称大気の基準）
    earth_radius: float = 6371.0

    # 大気の上限高度 [km]（積分の打ち切り高度。100 km ~ Karman line）
    atmosphere_top: float = 100.0


def rayleigh_phase(cos_theta: float) -> float:
    """
    レイリー散乱の位相関数

    分子（双極子振動子）からの散乱の角度分布を表す:
        P(θ) = (3 / 4) * (1 + cos²θ)
    前後対称で、横方向 (θ=90°) より前方・後方が約 2 倍強い。

    Args:
        cos_theta: 散乱角のコサイン (入射方向と散乱方向の内積)

    Returns:
        位相関数の値（4π 積分で正規化される定義に準拠）
    """
    # レイリー散乱: (3/4) * (1 + cos²θ)
    return 0.75 * (1.0 + cos_theta * cos_theta)


def henyey_greenstein_phase(cos_theta: float, g: float) -> float:
    """
    Henyey-Greenstein位相関数（ミー散乱の経験的近似）

    厳密な Mie 理論は計算コストが高いため、雲・エアロゾル等の前方散乱を
    1 パラメータ g で近似する標準的な位相関数:
        P_HG(θ; g) = (1 - g²) / [4π * (1 + g² - 2g*cosθ)^(3/2)]
    g は平均コサイン <cosθ> に等しい。

    Args:
        cos_theta: 散乱角のコサイン
        g: 異方性パラメータ (-1 ≤ g ≤ 1)。+: 前方散乱, 0: 等方, -: 後方散乱

    Returns:
        位相関数の値（4π 積分で 1 になる正規化）
    """
    g2 = g * g
    denom = 1.0 + g2 - 2.0 * g * cos_theta
    return (1.0 - g2) / (4.0 * np.pi * denom * np.sqrt(denom))


def wavelength_dependent_rayleigh(wavelength_nm: float, reference_wavelength_nm: float = 550.0,
                                   reference_coeff: float = 0.0058) -> float:
    """
    波長依存のレイリー散乱係数

    レイリー散乱断面積は σ ∝ λ^(-4)（双極子放射の周波数 4 乗則）。
    短波長（青 ~440 nm）が長波長（赤 ~680 nm）より約 (680/440)^4 ≈ 5.7 倍強く
    散乱されるため、空が青く・夕焼けが赤く見える。

    式: β(λ) = β(λ_ref) * (λ_ref / λ)^4

    Args:
        wavelength_nm: 波長 [nm]
        reference_wavelength_nm: 基準波長 [nm]（既定: 550 nm = 緑）
        reference_coeff: 基準波長での散乱係数 [1/km]

    Returns:
        指定波長での散乱係数 [1/km]
    """
    ratio = reference_wavelength_nm / wavelength_nm
    return reference_coeff * (ratio ** 4)


def compute_atmospheric_density(altitude_km: float, scale_height: float) -> float:
    """
    高度における大気密度（海面レベルに対する比）

    等温大気の静水圧平衡から導かれる指数モデル:
        ρ(h) = ρ₀ * exp(-h / H)
    H はスケールハイトで、レイリー成分（分子大気）は ~8 km、
    ミー成分（エアロゾル）は ~1.2 km と短く、地表近傍に集中する。

    Args:
        altitude_km: 高度 [km]（地表より下は 0 にクランプ）
        scale_height: スケールハイト [km]

    Returns:
        大気密度比 (0.0 ~ 1.0、海面で 1.0)
    """
    if altitude_km < 0:
        altitude_km = 0
    return np.exp(-altitude_km / scale_height)


def ray_sphere_intersection(ray_origin: np.ndarray, ray_direction: np.ndarray,
                            sphere_center: np.ndarray, sphere_radius: float) -> Tuple[float, float]:
    """
    光線と球の交点を計算（解析解）

    レイ P(t) = O + t*D を球 |P - C|² = r² に代入し、t の二次方程式を解く:
        a t² + b t + c = 0
        a = |D|², b = 2 D·(O-C), c = |O-C|² - r²
    判別式 D < 0 なら交点なし。地球（または大気上限球）と視線の交差判定に使う。

    Args:
        ray_origin: 光線の原点
        ray_direction: 光線の方向（正規化済み）
        sphere_center: 球の中心
        sphere_radius: 球の半径

    Returns:
        (t_near, t_far): 交点までの距離（交点がない場合は両方 -1.0）
    """
    oc = ray_origin - sphere_center
    a = np.dot(ray_direction, ray_direction)
    b = 2.0 * np.dot(oc, ray_direction)
    c = np.dot(oc, oc) - sphere_radius * sphere_radius

    discriminant = b * b - 4 * a * c

    if discriminant < 0:
        return -1.0, -1.0

    sqrt_discriminant = np.sqrt(discriminant)
    t_near = (-b - sqrt_discriminant) / (2.0 * a)
    t_far = (-b + sqrt_discriminant) / (2.0 * a)

    return t_near, t_far


def compute_atmospheric_scattering(
    view_pos: np.ndarray,
    view_dir: np.ndarray,
    sun_dir: np.ndarray,
    params: Optional[AtmosphereParameters] = None,
    num_samples: int = 16,
    wavelengths: Tuple[float, float, float] = (680.0, 550.0, 440.0)  # RGB wavelengths [nm]
) -> Tuple[np.ndarray, np.ndarray]:
    """
    大気散乱による色の計算（シングルスキャタリング近似）

    視線に沿って各サンプル点 P_i で
        L(P_i) = (太陽から P_i への透過率) * 散乱係数 * 位相関数 * 大気密度
    を積分し、視点までの透過率も同時に蓄積する。
    （マルチスキャタリングは無視。実装は短経路の単純化版）

    Args:
        view_pos: 視点位置 [km]（地球中心からのベクトル）
        view_dir: 視線方向（正規化済み）
        sun_dir: 太陽方向（正規化済み）
        params: 大気パラメータ
        num_samples: 視線方向のサンプリング点数
        wavelengths: RGB に対応する波長 [nm] (赤=680, 緑=550, 青=440)

    Returns:
        (in_scatter_rgb, transmittance_rgb):
            - in_scatter_rgb: 大気内散乱光（背景に加算する空の色）[0-1]
            - transmittance_rgb: 視線方向の透過率 [0-1]
    """
    if params is None:
        params = AtmosphereParameters()

    # 大気上限球（半径 R_earth + h_top）と視線の交点
    atmosphere_radius = params.earth_radius + params.atmosphere_top
    t_atm_near, t_atm_far = ray_sphere_intersection(
        view_pos, view_dir, np.array([0, 0, 0]), atmosphere_radius
    )

    if t_atm_far < 0:
        # 大気と交差しない（視線が宇宙空間のみを通る）
        return np.array([0.0, 0.0, 0.0]), np.array([1.0, 1.0, 1.0])

    # 地球表面との交点も計算（地面に遮られる場合はそこで積分を終える）
    t_ground_near, t_ground_far = ray_sphere_intersection(
        view_pos, view_dir, np.array([0, 0, 0]), params.earth_radius
    )

    # 積分区間 [t_start, t_end] を決定
    t_start = max(0, t_atm_near)
    if t_ground_near > 0:
        t_end = t_ground_near  # 地面に当たる: そこで積分終了
    else:
        t_end = t_atm_far  # 大気を抜ける: 大気上限まで積分

    if t_end <= t_start:
        return np.array([0.0, 0.0, 0.0]), np.array([1.0, 1.0, 1.0])

    # RGB 各波長での散乱係数（λ^-4 則を適用）
    # 結果として β_R(青) > β_R(緑) > β_R(赤) となり、空が青く見える
    rayleigh_coeffs = np.array([
        wavelength_dependent_rayleigh(wl, reference_coeff=params.rayleigh_coeff)
        for wl in wavelengths
    ])

    # 視線サンプリング刻み
    step_size = (t_end - t_start) / num_samples

    rayleigh_sum = np.zeros(3)
    mie_sum = np.zeros(3)

    # 視点〜サンプル点間の累積光学的厚さ（透過率計算用）
    view_optical_depth_rayleigh = np.zeros(3)
    view_optical_depth_mie = 0.0

    # 散乱角は太陽方向と視線方向のなす角（cosθ = view·sun）
    cos_theta = np.dot(view_dir, sun_dir)
    rayleigh_phase_value = rayleigh_phase(cos_theta)
    mie_phase_value = henyey_greenstein_phase(cos_theta, params.mie_g)

    for i in range(num_samples):
        t = t_start + step_size * (i + 0.5)
        sample_pos = view_pos + view_dir * t
        altitude = np.linalg.norm(sample_pos) - params.earth_radius

        if altitude < 0 or altitude > params.atmosphere_top:
            continue

        # サンプル点における大気密度（成分ごとに別スケールハイト）
        rayleigh_density = compute_atmospheric_density(altitude, params.rayleigh_scale_height)
        mie_density = compute_atmospheric_density(altitude, params.mie_scale_height)

        # サンプル点から太陽への光学的厚さ
        # 本来は太陽方向にも経路積分が必要だが、ここでは
        #     τ_sun ≈ ρ(h) * β * H
        # の単点近似（指数大気の鉛直積分の閉じた形）で済ませている
        sun_optical_depth_rayleigh = rayleigh_density * rayleigh_coeffs * params.rayleigh_scale_height
        sun_optical_depth_mie = mie_density * params.mie_coeff * params.mie_scale_height

        # 各波長での透過率（太陽→サンプル点→視点の合計光学的厚さ）
        # I/I0 = exp(-τ_total)
        attenuation = np.exp(
            -(sun_optical_depth_rayleigh + view_optical_depth_rayleigh +
              sun_optical_depth_mie + view_optical_depth_mie)
        )

        # 散乱光の蓄積: dL = ρ * β * T * P(θ) * ds
        rayleigh_sum += rayleigh_density * rayleigh_coeffs * attenuation * rayleigh_phase_value * step_size
        mie_sum += mie_density * params.mie_coeff * attenuation * mie_phase_value * step_size

        # 視線方向の光学的厚さを次サンプルに向けて蓄積
        view_optical_depth_rayleigh += rayleigh_density * rayleigh_coeffs * step_size
        view_optical_depth_mie += mie_density * params.mie_coeff * step_size

    # 散乱光の合計（レイリー＋ミー）
    in_scatter = rayleigh_sum + mie_sum

    # 視線方向の総透過率（背景物体の色を減衰させる係数）
    # optical_depth は既に ∫ρβ ds（無次元）。βを再度掛けない。
    transmittance = np.exp(-(view_optical_depth_rayleigh + view_optical_depth_mie))

    # 表示用に [0, 1] にクランプ（HDR を扱いたい場合は呼び出し側で外す）
    in_scatter = np.clip(in_scatter, 0, 1)
    transmittance = np.clip(transmittance, 0, 1)

    return in_scatter, transmittance


def compute_earth_albedo(
    point_on_satellite: np.ndarray,
    normal_at_point: np.ndarray,
    sun_dir: np.ndarray,
    earth_radius: float = 6371.0,
    earth_albedo: float = 0.3
) -> np.ndarray:
    """
    地球アルベド効果（地球照、Earthshine）を計算

    地球を Lambert ディスク（拡散反射体）として近似:
        E_earth = α * E_sun * cos(θ_sun) * cos(θ_normal) * Ω / π
    ここで:
        α       = 地球の平均アルベド（~0.3）
        Ω       = 衛星から見た地球の立体角 = 2π(1 - cos(α_app))、α_app は見かけの半角
        cos(θ_sun)    = 地球面の太陽入射角の余弦（昼夜判定）
        cos(θ_normal) = 衛星表面法線と地球方向の余弦（受光面の傾き）

    Args:
        point_on_satellite: 衛星上の点の位置 [km]（地球中心原点）
        normal_at_point: その点での法線ベクトル（正規化済み）
        sun_dir: 太陽方向（正規化済み）
        earth_radius: 地球半径 [km]
        earth_albedo: 地球の平均アルベド（反射率、~0.3）

    Returns:
        albedo_contribution: 地球照による照度（RGB相対値）
    """
    # 衛星位置から地球中心への方向（衛星 → 地球）
    to_earth_center = -point_on_satellite
    distance_to_earth = np.linalg.norm(to_earth_center)
    to_earth_center_norm = to_earth_center / distance_to_earth

    # 地球が衛星から見える立体角（球冠の立体角）
    # sin(α_app) = R_earth / d  → Ω = 2π(1 - cos(α_app))
    sin_half_angle = earth_radius / distance_to_earth
    if sin_half_angle > 1.0:
        sin_half_angle = 1.0

    solid_angle = 2 * np.pi * (1 - np.sqrt(1 - sin_half_angle**2))

    # 地球表面が太陽を向いているか（昼側のみ反射に寄与）
    # 可視地球面の外向き法線は「地球→衛星」。衛星→地球とは逆向き。
    cos_earth_sun = np.dot(-to_earth_center_norm, sun_dir)
    if cos_earth_sun < 0:
        cos_earth_sun = 0  # 夜側は寄与なし

    # 衛星表面法線が地球を向いているか（受光面の傾き）
    cos_normal_earth = np.dot(normal_at_point, to_earth_center_norm)
    if cos_normal_earth < 0:
        cos_normal_earth = 0  # 地球と反対向きは寄与なし

    # Lambert 反射モデルでの放射照度
    albedo_intensity = earth_albedo * cos_earth_sun * cos_normal_earth * solid_angle / np.pi

    # 地球照の色（海洋＋雲のスペクトル合成。実観測でやや暖色寄り）
    albedo_color = np.array([1.0, 0.95, 0.9]) * albedo_intensity

    return albedo_color


if __name__ == "__main__":
    # テスト
    print("大気散乱モジュールのテスト")

    params = AtmosphereParameters()

    # 衛星位置（高度400km）
    altitude_km = 400
    view_pos = np.array([0, 0, params.earth_radius + altitude_km])

    # 地平線方向を見る
    view_dir = np.array([1, 0, -0.1])
    view_dir = view_dir / np.linalg.norm(view_dir)

    # 太陽は横から
    sun_dir = np.array([0, 1, 0])

    in_scatter, transmittance = compute_atmospheric_scattering(
        view_pos, view_dir, sun_dir, params
    )

    print(f"散乱光 (RGB): {in_scatter}")
    print(f"透過率 (RGB): {transmittance}")

    # アルベドテスト
    albedo = compute_earth_albedo(
        view_pos,
        np.array([0, 0, -1]),  # 下向き法線
        sun_dir
    )
    print(f"地球アルベド (RGB): {albedo}")
