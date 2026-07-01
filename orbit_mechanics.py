"""
軌道計算・座標変換・幾何計算モジュール

yoshimulibの薄ラッパー群。ケプラー軌道要素、姿勢行列、太陽位置、
相対軌道（football）、LVLH座標系、地表ターゲット位置、地球遮蔽判定を提供。
数値軌道伝播（J2摂動・大気抵抗）は scipy solve_ivp を使用。

物理モデル:
  - 二体問題: r̈ = -μ·r/|r|³  （地球中心慣性系 ECI）
  - J2 摂動 (地球扁平): U_J2 = (μ/r)·J2·(R_E/r)²·(3sin²φ - 1)/2 → 加速度に展開
  - 大気抵抗: a_drag = -(1/2)·ρ·C_D·(A/m)·|v|·v
  - LVLH(RTN): R = r/|r| (radial out), W = (r×v)/|r×v| (orbit normal), S = W×R (along-track)

座標系の規約:
  - ECI : Earth-Centered Inertial（位置 [km]、速度 [km/s]）
  - RTN(LVLH) : 衛星位置を基準とした Radial-Tangential-Normal の右手系
  - Body : 衛星機体固定座標（NADIR モードでは x=W, y=S, z=-R に整列）
  - 角度は全てラジアン、クォータニオンはスカラーラスト [qx, qy, qz, qw]

呼び出し元:
  - satellite_orbit.py（render / lightcurve / preview）からインポートされる中核ユーティリティ
"""

import numpy as np
from dataclasses import dataclass
from typing import Tuple, Optional

from scipy.integrate import solve_ivp

from yoshimulib.orbit.orbital_elements import oe2rv, rv2oe, MU_EARTH_KM, R_EARTH_KM, AU_KM
from yoshimulib.orbit.transforms import dcm_i2rtn
from yoshimulib.attitude.quaternion import q2dcm

# 物理定数 (yoshimulib)
EARTH_RADIUS_KM = R_EARTH_KM     # 地球赤道半径 [km] (WGS84: 6378.137)
EARTH_MU = MU_EARTH_KM           # 地球重力定数 [km³/s²]
AU = AU_KM                       # 天文単位 [km]
SCALE_FACTOR = 1.0 / EARTH_RADIUS_KM

# J2摂動定数
J2 = 1.08263e-3                  # 地球J2帯状調和係数

# 大気抵抗用定数
DRAG_CD = 2.2                    # 抗力係数（典型値）
DRAG_A_M = 0.01                  # 面積質量比 [m²/kg]（典型的な衛星）


@dataclass
class OrbitalElements:
    """ケプラー軌道要素 (osculating, ECI 基準)

    6 パラメータでケプラー楕円軌道を一意に決める標準集合。
    `mean_anomaly_0` は初期エポック (t=0) における平均近点角。
    """
    semi_major_axis: float      # 軌道長半径 a [km]
    eccentricity: float         # 離心率 e [-]
    inclination: float          # 軌道傾斜角 i [rad]
    raan: float                 # 昇交点赤経 Ω [rad]
    arg_periapsis: float        # 近地点引数 ω [rad]
    mean_anomaly_0: float       # 初期平均近点角 M₀ [rad]

    @property
    def orbital_period(self) -> float:
        """軌道周期 T [s]  (ケプラー第3法則: T = 2π√(a³/μ))"""
        return 2 * np.pi * np.sqrt(self.semi_major_axis**3 / EARTH_MU)


def compute_orbital_position(elements: OrbitalElements, time: float) -> Tuple[np.ndarray, np.ndarray]:
    """
    ケプラー軌道要素から時刻tにおける位置と速度を計算 (yoshimulib oe2rv使用)

    平均運動 n = √(μ/a³) を用いて M(t) = M₀ + n·t を更新し、
    yoshimulib の oe2rv (flag=0: 平均近点角入力) で内部的にケプラー方程式
    M = E - e·sin(E) を解いて (r, v) を返す。

    Args:
        elements: ケプラー軌道要素 (a [km], e, i [rad], Ω [rad], ω [rad], M₀ [rad])
        time: 初期エポックからの経過時間 [s]

    Returns:
        position: 位置ベクトル [km] (ECI 座標系)
        velocity: 速度ベクトル [km/s] (ECI 座標系)
    """
    # 平均運動 n = √(μ/a³)、平均近点角の時間更新 M(t) = M₀ + n·t
    n = np.sqrt(EARTH_MU / elements.semi_major_axis**3)
    M = elements.mean_anomaly_0 + n * time

    # yoshimulib は (N,6) 形式を期待するので 1 行にまとめる
    oe_array = np.array([[
        elements.semi_major_axis,
        elements.eccentricity,
        elements.inclination,
        elements.raan,
        elements.arg_periapsis,
        M,
    ]])
    # flag=0: 6 列目を平均近点角 M として扱う（flag=1 だと真近点角 f）
    r, v = oe2rv(oe_array, flag=0, mu=EARTH_MU)
    return r.flatten(), v.flatten()


def _atmospheric_density(alt_km: float) -> float:
    """簡易指数大気モデル: 高度から大気密度 [kg/m³] を返す。

    参考: US Standard Atmosphere 1976 の簡易近似。
    200-1000 km の LEO 領域で妥当な値を返す。
    """
    if alt_km < 200:
        h0, rho0, H = 150.0, 2.07e-9, 22.52
    elif alt_km < 400:
        h0, rho0, H = 250.0, 6.97e-11, 45.55
    elif alt_km < 600:
        h0, rho0, H = 450.0, 1.59e-12, 63.82
    else:
        h0, rho0, H = 700.0, 1.17e-14, 88.67
    return rho0 * np.exp(-(alt_km - h0) / H)


def _eom_perturbed(t: float, state: np.ndarray,
                   mu: float, use_j2: bool, use_drag: bool,
                   cd: float, a_over_m: float, re: float) -> np.ndarray:
    """摂動付き運動方程式（solve_ivp 用の右辺関数）。

    支配方程式:
      r̈ = -μ·r/|r|³ + a_J2 + a_drag
    state = [x, y, z, vx, vy, vz]  (km, km/s, ECI)
    戻り値 = d(state)/dt = [vx, vy, vz, ax, ay, az]  (km/s, km/s², ECI)

    含む摂動:
      - J2 帯状調和（地球扁平、最大の長期摂動）
      - 大気抵抗（簡易指数大気、< 1000 km で有効）
    """
    r_vec = state[:3]
    v_vec = state[3:]
    r = np.linalg.norm(r_vec)
    r2 = r * r

    # 二体加速度  a = -μ·r/|r|³  （ニュートン重力）
    a_two_body = -mu / (r2 * r) * r_vec

    a_pert = np.zeros(3)

    # J2 摂動加速度（地球扁平により赤道面で対称、極で非対称）
    # a_J2 = (3/2)·J2·μ·R_E²/r⁵ · [x(5z²/r² - 1), y(5z²/r² - 1), z(5z²/r² - 3)]
    if use_j2:
        z2_r2 = (r_vec[2] / r) ** 2
        coeff = 1.5 * J2 * mu * re**2 / (r2 * r2 * r)
        a_pert[0] += coeff * r_vec[0] * (5 * z2_r2 - 1)
        a_pert[1] += coeff * r_vec[1] * (5 * z2_r2 - 1)
        a_pert[2] += coeff * r_vec[2] * (5 * z2_r2 - 3)

    # 大気抵抗加速度  a_drag = -(1/2)·ρ·C_D·(A/m)·|v|·v
    # （v は対大気相対速度。ここでは地球大気の共回転を無視し v_rel ≈ v_ECI とする簡易扱い）
    if use_drag:
        alt_km = r - re
        if alt_km < 1000:
            rho = _atmospheric_density(alt_km)
            v_rel = v_vec  # 大気共回転は無視（簡易）
            v_mag = np.linalg.norm(v_rel)
            if v_mag > 1e-12:
                # ρ [kg/m³] と v [m/s] の単位系で力を出してから km/s² に戻す
                v_mag_ms = v_mag * 1000.0
                a_drag_mag = 0.5 * rho * cd * a_over_m * v_mag_ms**2 / 1000.0  # km/s²
                a_pert -= a_drag_mag * (v_rel / v_mag)

    # state 微分は [v ; a]
    return np.concatenate([v_vec, a_two_body + a_pert])


class NumericalPropagator:
    """数値軌道伝播器。初期軌道要素から solve_ivp で伝播し、任意時刻の状態を補間で返す。

    用途: J2 や大気抵抗を含めたい場合に compute_orbital_position の代替として使用する。
    内部では DOP853 で 1 周期分以上を一度に積分し、`dense_output` で任意 t をサンプル。
    入出力は全て ECI、単位は km / km/s / s。
    """

    def __init__(self, elements: 'OrbitalElements',
                 use_j2: bool = True, use_drag: bool = False,
                 drag_cd: float = DRAG_CD, drag_a_over_m: float = DRAG_A_M):
        self.elements = elements
        self.use_j2 = use_j2
        self.use_drag = use_drag
        self.drag_cd = drag_cd
        self.drag_a_over_m = drag_a_over_m
        self._sol = None
        self._t_max = 0.0

    def _initial_state(self) -> np.ndarray:
        """軌道要素から初期状態ベクトル [x,y,z,vx,vy,vz] を生成。"""
        r, v = compute_orbital_position(self.elements, 0.0)
        return np.concatenate([r, v])

    def _ensure_propagated(self, t: float) -> None:
        """必要な時刻まで伝播済みか確認し、不足なら再伝播する。"""
        if self._sol is not None and t <= self._t_max:
            return

        # 要求時刻 + 余裕（周期の10%）まで伝播
        margin = self.elements.orbital_period * 0.1
        t_end = max(t + margin, self.elements.orbital_period)

        y0 = self._initial_state()
        sol = solve_ivp(
            _eom_perturbed,
            [0, t_end],
            y0,
            method='DOP853',
            args=(EARTH_MU, self.use_j2, self.use_drag,
                  self.drag_cd, self.drag_a_over_m, EARTH_RADIUS_KM),
            rtol=1e-12,
            atol=1e-14,
            dense_output=True,
        )
        if not sol.success:
            raise RuntimeError(f"軌道伝播に失敗: {sol.message}")

        self._sol = sol.sol
        self._t_max = t_end

    def state_at(self, t: float) -> Tuple[np.ndarray, np.ndarray]:
        """時刻 t [s] における位置 [km] と速度 [km/s] を返す (ECI)。

        必要に応じて再伝播する遅延評価。dense_output による高次補間を内部使用。
        """
        self._ensure_propagated(t)
        state = self._sol(t)
        return state[:3], state[3:]


class CsvEphemeris:
    """CSV ファイルから読み込んだ軌道・姿勢データを補間して返す。

    スキーマ:
      必須: time_s [s], a_km [km], e, inc_rad, raan_rad, ome_rad, f_rad（osculating Keplerian, ECI 想定）
      任意: q1, q2, q3, q4 — スカラーラスト クォータニオン（無ければ attitude_at() は None）
    生成元: matlabSample/mainHCW.m（chief: GEO circular equatorial）など。
    例: input/attitudeOrbit.csv, input/abs_chief_hcw.csv, input/abs_deputy_hcw.csv
    範囲外 t は端点クランプ＋初回のみ stderr 警告。`#` コメント行はスキップ。
    """

    _REQUIRED_COLS = ['time_s', 'a_km', 'e', 'inc_rad', 'raan_rad', 'ome_rad', 'f_rad']
    _ATTITUDE_COLS = ['q1', 'q2', 'q3', 'q4']

    def __init__(self, csv_path: str) -> None:
        import csv as _csv

        with open(csv_path, newline='') as fh:
            # コメント行（先頭 #）と空行をスキップしてから CSV を読む。
            cleaned = [
                line for line in fh
                if line.strip() and not line.lstrip().startswith('#')
            ]
            reader = _csv.reader(cleaned)
            header = [h.strip() for h in next(reader)]
            rows = [[float(v) for v in row] for row in reader if row]

        col_idx = {name: header.index(name) for name in self._REQUIRED_COLS}
        data = np.array(rows)

        self._times = data[:, col_idx['time_s']]
        self._oe = np.column_stack([
            data[:, col_idx['a_km']],
            data[:, col_idx['e']],
            data[:, col_idx['inc_rad']],
            data[:, col_idx['raan_rad']],
            data[:, col_idx['ome_rad']],
            data[:, col_idx['f_rad']],
        ])

        if all(c in header for c in self._ATTITUDE_COLS):
            q_idx = [header.index(c) for c in self._ATTITUDE_COLS]
            self._quat = data[:, q_idx]  # (N, 4) [q1,q2,q3,q4] scalar-last
        else:
            self._quat = None

        self.t_min = float(self._times[0])
        self.t_max = float(self._times[-1])
        self._csv_path = csv_path
        self._out_of_range_warned = False

    # ------------------------------------------------------------------
    def _warn_if_out_of_range(self, t: float) -> None:
        if self._out_of_range_warned:
            return
        if t < self.t_min - 1e-9 or t > self.t_max + 1e-9:
            import sys
            print(
                f"⚠ CsvEphemeris: 要求時刻 t={t:.3f}s が CSV 範囲 "
                f"[{self.t_min:.3f}, {self.t_max:.3f}]s を超えています。"
                f" 端点にクランプして補間します ({self._csv_path})。",
                file=sys.stderr,
            )
            self._out_of_range_warned = True

    @property
    def has_attitude(self) -> bool:
        return self._quat is not None

    @property
    def duration(self) -> float:
        return self.t_max - self.t_min

    # ------------------------------------------------------------------
    def _interp_oe(self, t: float) -> np.ndarray:
        """時刻 t における軌道要素 6 成分を線形補間で返す。"""
        self._warn_if_out_of_range(t)
        t_clamped = np.clip(t, self.t_min, self.t_max)
        return np.array([np.interp(t_clamped, self._times, self._oe[:, i]) for i in range(6)])

    def state_at(self, t: float) -> Tuple[np.ndarray, np.ndarray]:
        """時刻 t [s] における位置 [km] と速度 [km/s] を返す (ECI)。

        CSV の 6 列目は真近点角 f (`f_rad`) なので flag=1 で oe2rv を呼ぶ
        （compute_orbital_position は flag=0 = 平均近点角入力なのと対比）。
        """
        oe = self._interp_oe(t)
        oe_2d = oe.reshape(1, 6)
        r, v = oe2rv(oe_2d, flag=1, mu=EARTH_MU)
        return r.flatten(), v.flatten()

    def attitude_at(self, t: float) -> Optional[np.ndarray]:
        """時刻 t [s] における姿勢行列 (3x3 DCM, Body→ECI) を返す。

        CSV に q1..q4 列が無ければ None。クォータニオンは線形補間後に再正規化。
        規約は MATLAB/yoshimulib と同じスカラーラスト [qx, qy, qz, qw]。
        """
        if self._quat is None:
            return None
        t_clamped = np.clip(t, self.t_min, self.t_max)
        q = np.array([np.interp(t_clamped, self._times, self._quat[:, i]) for i in range(4)])
        q = q / np.linalg.norm(q)
        return self._quat_to_dcm(q)

    @staticmethod
    def _quat_to_dcm(q: np.ndarray) -> np.ndarray:
        """クォータニオン [q1,q2,q3,q4] (スカラーラスト) → Body→ECI DCM。

        MATLAB 側の `q2dcm(4, q)` は Rbi (ECI→Body) として使われている。
        Mitsuba の `to_world` には Body→ECI が必要なので転置して返す。
        """
        return q2dcm(q, scalar=4).T


def compute_attitude_matrix(position: np.ndarray, velocity: np.ndarray,
                            mode: str = 'nadir', sun_direction: Optional[np.ndarray] = None) -> np.ndarray:
    """
    衛星の姿勢行列を計算 (yoshimulib rv2oe/dcm_i2rtn使用)

    モード別の指向則:
      - nadir            : 機体 -z 軸を地球中心 (-R 方向) へ向ける（地球指向）
                           x=W (orbit normal), y=S (along-track), z=-R (nadir)
      - sun_tracking     : 機体 +z 軸を太陽方向へ向ける（ソーラーパネル正対）
                           x は速度方向と直交させて取り、y = z × x で右手系を完成
      - velocity_aligned : 機体 +x 軸を速度方向へ向ける（ラム指向）
                           z は概ね nadir（-R を x と直交化）

    Args:
        position: 衛星位置ベクトル [km] (ECI)
        velocity: 衛星速度ベクトル [km/s] (ECI)
        mode: 姿勢制御モード ('nadir', 'sun_tracking', 'velocity_aligned')
        sun_direction: 太陽方向単位ベクトル (ECI、sun_tracking モードで使用)

    Returns:
        attitude_matrix: 姿勢行列 (3x3) - 各列が機体座標系の軸を ECI で表現したもの
                         つまり C = [x_b | y_b | z_b]、列ベクトル右掛けで body→ECI
    """
    r_norm = position / np.linalg.norm(position)
    v_norm = velocity / np.linalg.norm(velocity)

    if mode == 'nadir':
        # 位置・速度から軌道要素を逆算 → RTN 基底を ECI で取得
        oe = rv2oe(position.reshape(1, 3), velocity.reshape(1, 3), EARTH_MU)
        # dcm_i2rtn の引数順: (Ω, i, ω, f) ※ rv2oe の列順 [a,e,i,Ω,ω,f] に対応
        C = dcm_i2rtn(oe[0, 3], oe[0, 2], oe[0, 4], oe[0, 5])
        R_vec = C[0]  # radial out (地球→衛星)
        S_vec = C[1]  # along-track (≈ 速度方向、円軌道で完全一致)
        W_vec = C[2]  # orbit normal (R × S)
        # NADIR本体座標系: x=W, y=S, z=-R （z 軸が地心方向）
        x_body, y_body, z_body = W_vec, S_vec, -R_vec

    elif mode == 'sun_tracking' and sun_direction is not None:
        # 機体 z 軸を太陽方向へ。速度と平行になる縮退時は radial に切替
        sun_norm = sun_direction / np.linalg.norm(sun_direction)
        z_body = sun_norm
        x_body = np.cross(v_norm, z_body)
        if np.linalg.norm(x_body) < 1e-6:
            x_body = np.cross(r_norm, z_body)
        x_body = x_body / np.linalg.norm(x_body)
        y_body = np.cross(z_body, x_body)

    elif mode == 'velocity_aligned':
        # 機体 x 軸 = 進行方向、z 軸はおおよそ地心方向 (-R を x と直交化)
        x_body = v_norm
        z_body = -r_norm
        z_body = z_body - np.dot(z_body, x_body) * x_body  # Gram-Schmidt
        z_body = z_body / np.linalg.norm(z_body)
        y_body = np.cross(z_body, x_body)
    else:
        return compute_attitude_matrix(position, velocity, mode='nadir')

    # 列ベクトルとして body 軸を並べる (Mitsuba は列ベクトル右掛け規約)
    return np.column_stack([x_body, y_body, z_body])


def compute_sun_position(time: float) -> np.ndarray:
    """
    簡易的な太陽位置計算（ECI座標系）

    地球公転の単純な円運動近似。歳差・章動・離心率は無視。
    黄道面 (xy 平面を基準) に対し z 方向に黄道傾斜角 23.5 度を与える。

    Args:
        time: 元期からの経過時間 [s] (元期 t=0 で太陽は +x 方向)

    Returns:
        sun_direction: 地球→太陽方向の単位ベクトル (ECI 座標系、無次元)
    """
    # 簡易モデル: 地球-太陽の幾何角度を年周運動 (1 年 = 365.25 日) で進める
    days = time / 86400.0  # 秒→日
    sun_angle = 2 * np.pi * days / 365.25  # ω_E·t (1 年で 1 周)

    # 黄道傾斜角 ε ≈ 23.5°（地軸の傾き）
    obliquity = np.radians(23.5)

    # 黄道面で円運動 → 黄道傾斜だけ z 成分を持つ
    sun_direction = np.array([
        np.cos(sun_angle),
        np.sin(sun_angle) * np.cos(obliquity),
        np.sin(sun_angle) * np.sin(obliquity)
    ])

    return sun_direction


def compute_football_debris_elements(
    chaser_elements: OrbitalElements,
    distance_km: float = 1.0,
    phase_deg: float = 0.0,
    cross_track_km: float = 0.0,
    orientation_deg: float = 0.0,
) -> OrbitalElements:
    """
    Chaser軌道要素からfootball相対運動するtarget(debris)の軌道要素を自動計算

    Football(2:1 楕円) は Clohessy-Wiltshire (HCW) 方程式の同一周期解の一つ。
    chaser の RTN フレームから見ると、deputy は radial 方向に半振幅 ρ、
    along-track 方向に半振幅 2ρ の楕円を描く。半長軸を一致 (δa=0) させて
    secular drift を抑え、δe で振幅、δM₀ で位相、δi で面外を作る。

      - along-track 振幅 = 2 · radial 振幅 (CW の固有比)
      - distance_km = along-track 半振幅 = 2·δe·a  →  δe = distance / (2·a)
      - δM₀ で初期位相（楕円上のスタート位置）を制御
      - δi で cross-track 方向の振幅 (≈ a·δi) を制御
      - δω で軌道面内の楕円向きを制御

    Args:
        chaser_elements: chaser (deputy) のケプラー軌道要素
        distance_km: along-track 方向の半振幅 [km]（特性距離）
        phase_deg: 初期位相 [deg]（0=along-track 先行, 90=radial 外側）
        cross_track_km: cross-track 方向の振幅 [km]
        orientation_deg: 軌道面内回転角 [deg]（δω で実現）

    Returns:
        target (debris) のケプラー軌道要素
    """
    a = chaser_elements.semi_major_axis

    # 離心率の差分: radial振幅 = δe * a, along-track振幅 = 2 * δe * a
    # distance_km = along-track半振幅 = 2 * δe * a  →  δe = distance / (2*a)
    delta_e = distance_km / (2.0 * a)

    # 位相制御: phase → δM₀
    # football楕円の位相はM₀の差で制御
    phase_rad = np.radians(phase_deg)

    # cross-track: δi = cross_track_km / a
    delta_i = cross_track_km / a

    # 軌道面内回転角
    delta_omega = np.radians(orientation_deg)

    return OrbitalElements(
        semi_major_axis=a,  # 同一半長軸 → secular drift なし
        eccentricity=chaser_elements.eccentricity + delta_e,
        inclination=chaser_elements.inclination + delta_i,
        raan=chaser_elements.raan,
        arg_periapsis=chaser_elements.arg_periapsis + delta_omega,
        mean_anomaly_0=chaser_elements.mean_anomaly_0 + phase_rad,
    )


def compute_lvlh_frame(position_km: np.ndarray, velocity_km: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    位置・速度ベクトルから LVLH(RSW = RTN) 座標系の基底を計算

    定義 (Hill / RTN frame):
      R = r / |r|             radial 外向き
      W = (r × v) / |r × v|   軌道面法線
      S = W × R               along-track（円軌道で速度方向と一致）

    内部では yoshimulib の rv2oe → dcm_i2rtn 経由で同じ基底を得る。
    HCW 方程式で扱う相対運動の基準フレーム。

    Args:
        position_km: ECI 位置 [km]
        velocity_km: ECI 速度 [km/s]

    Returns:
        R, S, W: それぞれ ECI 座標で表した RTN 単位基底ベクトル
    """
    oe = rv2oe(position_km.reshape(1, 3), velocity_km.reshape(1, 3), EARTH_MU)
    # dcm_i2rtn(Ω, i, ω, f) の各行が R, S, W
    C = dcm_i2rtn(oe[0, 3], oe[0, 2], oe[0, 4], oe[0, 5])
    return C[0].copy(), C[1].copy(), C[2].copy()


def compute_target_position_km(lat_deg: float, lon_deg: float, alt_km: float) -> np.ndarray:
    """地球固定座標系 (ECEF) でのターゲット位置 [km]（簡易、地球自転は無視）

    球体地球を仮定した経緯度→直交変換。WGS84 楕円体補正は省略。

    Args:
        lat_deg: 緯度 [deg]
        lon_deg: 経度 [deg]
        alt_km : 海抜高度 [km]

    Returns:
        位置ベクトル [km]（地心から見た直交座標）
    """
    lat = np.radians(lat_deg)
    lon = np.radians(lon_deg)
    r = EARTH_RADIUS_KM + alt_km
    return np.array([
        r * np.cos(lat) * np.cos(lon),
        r * np.cos(lat) * np.sin(lon),
        r * np.sin(lat)
    ])


def compute_observer_position_km(lat_deg: float, lon_deg: float, alt_km: float) -> np.ndarray:
    """地上観測者位置 [km]（簡易、地球自転は無視。compute_target_position_km と同じ式）"""
    return compute_target_position_km(lat_deg, lon_deg, alt_km)


def is_earth_occluded(observer_km: np.ndarray, target_km: np.ndarray,
                      earth_radius_km: float = EARTH_RADIUS_KM) -> bool:
    """観測線が地球に遮蔽されるか判定

    線分 observer→target と原点中心半径 R の球の交差判定。
    線分上で原点に最も近い点を求め、その距離が R 未満なら遮蔽されているとみなす。

    Args:
        observer_km: 観測者位置 [km]（地心慣性系）
        target_km  : ターゲット位置 [km]（同上）
        earth_radius_km: 地球半径 [km]

    Returns:
        True: 視線が地球に隠される / False: 直接視線あり
    """
    d = target_km - observer_km
    d_norm_sq = np.dot(d, d)
    if d_norm_sq < 1e-12:
        return False

    # 線分パラメータ t∈[0,1] で原点に最近接する点を探す
    t = -np.dot(observer_km, d) / d_norm_sq
    t = np.clip(t, 0.0, 1.0)
    closest = observer_km + t * d
    return np.dot(closest, closest) < earth_radius_km ** 2
