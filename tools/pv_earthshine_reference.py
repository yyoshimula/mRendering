#!/usr/bin/env python3
"""地球反射（earthshine）が衛星の面に与える放射照度の解析参照実装。

Mitsuba を使わない numpy だけの数値積分で、`relative` verb の PV 計測
（pv_irradiance.py: irradiancemeter + レイキャスト）を検証する物差し。
地球を **単一 BRDF の球**（テクスチャ・雲・大気なし）として、衛星から
見える昼側キャップをパネル格子に離散化し

    E = S0 · Σ_panels f_r(ŝ, n̂_p, v̂) · (n̂_p·ŝ) · (n̂_p·v̂) · (−n̂·v̂) · dA / d²

を足し上げる（f_r: 地球面 BRDF、n̂_p: パネル法線、v̂: パネル→衛星、
n̂: 受光面法線、d: 距離）。

出典・帰属:
  格子の取り方（角度 off-plane/on-plane による接平面座標とそのヤコビアン）と
  Lambert/Phong BRDF は Fankhauser, Tyson & Askari (2023)
  "Satellite Optical Brightness", AJ 166, 59 (arXiv:2305.11123) の公開コード
  **Lumos** (`lumos-sat` 1.0.9, MIT License, Copyright (c) 2023 Forrest
  Fankhauser) の `lumos.calculator.get_earthshine_panels` /
  `lumos.brdf.library` を基に、放射照度計算用に書き直したもの。
  下記 EARTH_BRDF_FITS の数値も同論文リポジトリ
  (github.com/Forrest-Fankhauser/satellite-optical-brightness,
  lab_brdf_fits.ipynb) の Phong フィット結果。

使い方:
  python tools/pv_earthshine_reference.py --altitude-km 550 --sun-zenith-deg 0
  python tools/pv_earthshine_reference.py --altitude-km 550 --sun-zenith-deg 60 \\
      --brdf phong --kd 0.53 --ks 0.28 --n 7.31

座標: 「明るさ座標系」 z = 地心→衛星、太陽は y-z 平面（y ≥ 0 側）。
  sun_zenith_deg = 衛星直下点における太陽天頂角（0 = 太陽直下、90 = 明暗境界）。
  受光面法線 normal は同じ座標系で与える（既定 [0,0,-1] = 天底向き）。
"""

from __future__ import annotations

import argparse
import math
from typing import Callable, Sequence

import numpy as np

R_EARTH_KM = 6378.137
S0_WM2 = 1361.0

BRDF = Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray]


# ---------------------------------------------------------------------------
# BRDF（Lumos lumos.brdf.library より。引数は (入射方向[光源へ], 法線, 出射方向)
# の (3,N) 配列、戻り値 (N,) [1/sr]）
# ---------------------------------------------------------------------------
def lambertian(albedo: float) -> BRDF:
    def f(wi, n, wo):
        cos_i = (wi * n).sum(0)
        cos_o = (wo * n).sum(0)
        return np.where((cos_i > 0) & (cos_o > 0), albedo / math.pi, 0.0)
    return f


def phong(kd: float, ks: float, n_exp: float) -> BRDF:
    """f = Kd/π + Ks·(n+2)/(2π)·(r̂·ŵo)^n（r̂ = 鏡面反射方向）。"""
    def f(wi, n, wo):
        dot_i = (wi * n).sum(0)
        r = 2.0 * dot_i * n - wi
        dot_r = np.clip((r * wo).sum(0), 0.0, 1.0)
        val = kd / math.pi + ks * (n_exp + 2.0) / (2.0 * math.pi) * dot_r ** n_exp
        cos_o = (wo * n).sum(0)
        return np.where((dot_i > 0) & (cos_o > 0), val, 0.0)
    return f


# Fankhauser+2023 の航空機 CARS 実測（479 nm）に対する Phong フィット。
# 注意: 単一波長・大気込みの値で、Kd=0.53 は植生の可視アルベドとしては
# 非物理的に大きい。角度分布（後方散乱の強さ・海面のグリント幅）の参考用で、
# 広帯域アルベドとして使うなら Kd+Ks を CERES/MODIS 系の値に再正規化すること。
EARTH_BRDF_FITS = {
    'vegetation_cars_479nm': dict(kd=0.53, ks=0.28, n=7.31),
}


# ---------------------------------------------------------------------------
# 地球パネル格子（Lumos get_earthshine_panels の写し + 下限クランプ）
# ---------------------------------------------------------------------------
def earth_panels(sat_z_km: float, angle_past_terminator: float, density: int):
    """衛星から見え太陽に照らされる地球面のパネル格子を返す。

    Lumos 流の接平面角度座標 (ψ = off-plane, ω = on-plane) で格子を切り、
    面積はヤコビアン行列式から出す。角度は衛星直下点を原点とし、on-plane は
    太陽方向 (+y)。angle_past_terminator は衛星の明暗境界からの角度（正 = 夜側、
    −90° = 太陽直下）。

    Returns:
        (p (3,N) [km], n (3,N), areas (N,) [km²])
    """
    R = R_EARTH_KM
    max_angle = math.acos(R / sat_z_km)
    lo = max(float(angle_past_terminator), -max_angle)  # 原版は下限を α のまま使う
    off = np.linspace(-max_angle, max_angle, density)
    on = np.linspace(lo, max_angle, density)
    d_phi = abs(off[1] - off[0])
    d_theta = abs(on[1] - on[0])
    on, off = np.meshgrid(on, off)
    on, off = on.ravel(), off.ravel()

    nz = 1.0 / np.sqrt(1.0 + np.tan(on) ** 2 + np.tan(off) ** 2)
    nx = np.tan(off) * nz
    ny = np.tan(on) * nz
    visible = np.arccos(np.clip(nz, -1, 1)) < max_angle
    off, on, nx, ny, nz = off[visible], on[visible], nx[visible], ny[visible], nz[visible]

    x, y, z = nx * R, ny * R, nz * R
    phi, theta = off, on
    dx_dr = nx / nz * z / R
    dx_dphi = z ** 3 / (R ** 2 * np.cos(phi) ** 2 * np.cos(theta) ** 2)
    dx_dtheta = -(ny / nz * nx / nz * z ** 3) / (R ** 2 * np.cos(theta) ** 2)
    dy_dr = np.tan(theta) * z / R
    dy_dphi = -(ny / nz * nx / nz * z ** 3) / (R ** 2 * np.cos(phi) ** 2)
    dy_dtheta = dx_dphi
    dz_dr = z / R
    dz_dphi = -(nx / nz * z ** 3) / (R ** 2 * np.cos(phi) ** 2)
    dz_dtheta = -(ny / nz * z ** 3) / (R ** 2 * np.cos(theta) ** 2)
    det = (dx_dr * (dy_dphi * dz_dtheta - dy_dtheta * dz_dphi)
           - dy_dr * (dx_dphi * dz_dtheta - dx_dtheta * dz_dphi)
           + dz_dr * (dx_dphi * dy_dtheta - dx_dtheta * dy_dphi))
    areas = det * d_phi * d_theta
    return np.vstack([x, y, z]), np.vstack([nx, ny, nz]), areas


# ---------------------------------------------------------------------------
# 受光面の地球反射照度
# ---------------------------------------------------------------------------
def earthshine_irradiance(altitude_km: float, sun_zenith_deg: float,
                          normal: Sequence[float] = (0.0, 0.0, -1.0),
                          brdf: BRDF | None = None, density: int = 251,
                          s0_wm2: float = S0_WM2) -> float:
    """明るさ座標系で受光面 1 点（衛星位置）の地球反射照度 [W/m²] を返す。

    面は衛星位置に置いた無限小の平板（片面、法線 normal）。地球パネルから
    面への入射は −v̂（v̂ = パネル→衛星）で、面側の cos は max(0, n̂·(−v̂))。
    """
    if brdf is None:
        brdf = lambertian(0.3)
    sat_z = R_EARTH_KM + float(altitude_km)
    alpha = math.radians(float(sun_zenith_deg)) - math.pi / 2.0  # 太陽直下 → −90°
    sun = np.array([0.0, math.cos(alpha), -math.sin(alpha)])
    p, n, areas = earth_panels(sat_z, alpha, density)
    to_sat = np.vstack([-p[0], -p[1], sat_z - p[2]])
    d = np.linalg.norm(to_sat, axis=0)
    v = to_sat / d
    cos_sun = np.clip((n * sun[:, None]).sum(0), 0.0, None)
    cos_out = np.clip((n * v).sum(0), 0.0, None)
    nn = np.asarray(normal, dtype=float)
    nn = nn / np.linalg.norm(nn)
    cos_in = np.clip(-(nn[:, None] * v).sum(0), 0.0, None)
    f = brdf(np.repeat(sun[:, None], p.shape[1], axis=1), n, v)
    return float(s0_wm2 * np.sum(f * cos_sun * cos_out * cos_in * areas / d ** 2))


def uniform_disk_estimate(altitude_km: float, albedo: float,
                          s0_wm2: float = S0_WM2) -> float:
    """よく使われる簡易式 a·S0·(R/(R+h))²（天底平板・太陽直下・一様輝度近似）。

    実際のランバート球は縁ほど太陽天頂角が大きく暗いので、厳密値はこれより
    数 % 小さい（550 km で ~1%）。
    """
    return float(albedo * s0_wm2 * (R_EARTH_KM / (R_EARTH_KM + altitude_km)) ** 2)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--altitude-km', type=float, default=550.0)
    ap.add_argument('--sun-zenith-deg', type=float, nargs='+', default=[0.0, 30.0, 60.0, 90.0],
                    help='衛星直下点の太陽天頂角（複数可）')
    ap.add_argument('--normal', type=float, nargs=3, default=[0.0, 0.0, -1.0],
                    help='受光面法線（明るさ座標系: z=天頂、y=太陽側）')
    ap.add_argument('--brdf', choices=['lambert', 'phong'], default='lambert')
    ap.add_argument('--albedo', type=float, default=0.3)
    ap.add_argument('--kd', type=float, default=0.53)
    ap.add_argument('--ks', type=float, default=0.28)
    ap.add_argument('--n', type=float, default=7.31)
    ap.add_argument('--density', type=int, default=251)
    ap.add_argument('--s0', type=float, default=S0_WM2)
    a = ap.parse_args(argv)
    brdf = lambertian(a.albedo) if a.brdf == 'lambert' else phong(a.kd, a.ks, a.n)
    print(f'高度 {a.altitude_km:.0f} km, 法線 {a.normal}, BRDF={a.brdf}, S0={a.s0:.0f} W/m²')
    for sz in a.sun_zenith_deg:
        e = earthshine_irradiance(a.altitude_km, sz, a.normal, brdf, a.density, a.s0)
        print(f'  太陽天頂角 {sz:5.1f}°: E_earth = {e:8.2f} W/m²')
    if a.brdf == 'lambert':
        print(f'  簡易式 a·S0·(R/(R+h))² = {uniform_disk_estimate(a.altitude_km, a.albedo, a.s0):.2f} W/m²')


if __name__ == '__main__':
    main()
