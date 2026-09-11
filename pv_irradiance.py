"""太陽電池パネル入射照度・発電量（relative verb の PV 計測パス）。

衛星の指定面に入射する放射照度 [W/m²] をフレームごとに計測し、
  E_direct : 太陽直達（食 ν・入射角 cos・機体自身による遮蔽込み）
  E_earth  : 地球反射（地球照。パストレで地球テクスチャ/雲/一様アルベドを積分）
  E_other  : その他の間接光（機体パーツ間の相互反射 + envmap/定数環境光）
  E_total  : 上 3 つの和
  P        : 発電量 = η · A · E_total [W]
を CSV に書き出す。

計測の仕組み（Mitsuba の制約と対策）:
  - 間接光は Mitsuba の `irradiancemeter` センサ（シェイプに入れ子）で
    ∫ L cosθ dω を面積平均する。1×1 の film に E がそのまま出る。
  - **directional 太陽（デルタ光源）は irradiancemeter では 0 になる**
    （センサ位置は path integrator の「表面相互作用」ではなく NEE が走らない。
    実測で確認済み）。そこで直達は解析的に扱う:
      E_direct = S0 · ν · ⟨ max(0, n·ŝ) · vis ⟩
    ⟨⟩ はシェイプ上の一様サンプル点平均、vis は太陽方向へのレイキャスト
    （`scene.ray_test`）による機体の自己遮蔽。
  - 地球照の分離は「地球を反射率 0 の黒体にした同一シーン」をもう 1 つ持ち、
    差分で出す（E_earth = E(with earth) − E(black earth)）。地球の遮蔽は両方に
    同じに効くので、差分は純粋に地球が反射した光（地球→機体→パネルの
    多重反射込み）になる。地球を「外す」と、地球に隠れていたはずの環境光が
    下半球から入って地球照を過小評価する（実測 −6%）ので採らない。
  - 既定では環境光（envmap / 定数フィル光）を PV 計測シーンから外す。
    relative の既定環境光は可視化用の淡いフィルで物理量ではないため
    （0.02 レンダ単位 ≈ 18 W/m²、直達の 1.3%）。`--pv-include-env` で含める。

単位換算:
  レンダは `sun_irradiance`（レンダ単位、既定 5.0）× 黒体色で組まれる。
  E は太陽との比で無次元化してから物理値に戻す:
      E_phys [W/m²] = S0_wm2 · lum(E_rgb) / lum(sun_rgb_base)
  lum は Rec.709 輝度（groundobs の絶対測光と同じ規約）。つまり
  「RGB 3 帯域を V バンド近似で束ねた広帯域アルベド」であり、NIR 域の
  植生高反射は含まれない（Si/3 接合セルの応答域に対する既知の限界。
  spectral バリアント対応は将来課題）。

パネル指定（YAML `pv_panels:` リスト or 単一パネルの CLI フラグ、
`pv_parts:` の OBJ パーツ名）:
  pv_panels:
    - name: array_plus_y
      host: deputy           # deputy | chief
      center: [0, 4.0, 0]    # 機体系 [m]
      normal: [0, 1, 0]      # 機体系。受光面の法線（片面）
      up: [0, 0, 1]          # 面内の向き（任意、見た目のみ）
      size: [2.5, 6.0]       # [m] 幅×高さ
      efficiency: 0.30
  pv_parts: [hbltel_wfc_1]   # OBJ パーツ全体を受光面とみなす（両面平均）

  仮想パネル（rectangle）は片面（+法線側）だけ受光する。OBJ パーツは
  閉じたメッシュなら表裏の全フェイスで平均されるので、薄板パネルは
  「表面積 2 倍・裏面は暗い」ぶん半分程度に出ることに注意。
"""

from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

import mitsuba as mi

# Rec.709 輝度係数（ground_observation.LUM_WEIGHTS と同一）
LUM_WEIGHTS = np.array([0.2126, 0.7152, 0.0722])

PV_SENSOR_PREFIX = 'pv_sensor_'

CSV_COLUMNS = [
    'frame', 't_s', 'panel', 'host', 'area_m2', 'nu', 'cos_sun', 'sun_visible',
    'e_direct_wm2', 'e_earth_wm2', 'e_other_wm2', 'e_total_wm2', 'p_w',
    'e_earth_r_wm2', 'e_earth_g_wm2', 'e_earth_b_wm2',
]


def luminance(rgb) -> float:
    return float(np.asarray(rgb, dtype=float).reshape(3) @ LUM_WEIGHTS)


# ---------------------------------------------------------------------------
# パネル仕様
# ---------------------------------------------------------------------------
@dataclass
class PvPanel:
    """受光面 1 枚の仕様。geometry は機体系 [m]。"""
    name: str
    host: str = 'deputy'            # 'deputy' | 'chief'
    kind: str = 'rect'              # 'rect' (仮想矩形) | 'part' (OBJ パーツ)
    center: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    normal: Tuple[float, float, float] = (0.0, 0.0, 1.0)
    up: Tuple[float, float, float] = (0.0, 1.0, 0.0)
    size: Tuple[float, float] = (1.0, 1.0)  # [m] 幅, 高さ
    part: Optional[str] = None
    efficiency: float = 0.30
    bsdf: Optional[dict] = None
    area_m2: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self.host = str(self.host)
        if self.kind == 'rect':
            self.area_m2 = float(self.size[0]) * float(self.size[1])

    @property
    def scene_key(self) -> str:
        """シーン辞書上のシェイプキー（= Mitsuba shape id）。"""
        if self.kind == 'part':
            return f'{self.host}_part_{self.part}'
        return f'{self.host}_pv_{self.name}'

    @property
    def sensor_id(self) -> str:
        return PV_SENSOR_PREFIX + self.name


def _as_vec(v, n: int, label: str) -> tuple:
    arr = [float(x) for x in np.asarray(v, dtype=float).reshape(-1)]
    if len(arr) != n:
        raise ValueError(f'{label}: 長さ {n} が必要です（{arr}）')
    return tuple(arr)


def panels_from_args(args: argparse.Namespace,
                     hosts: Sequence[str] = ('deputy', 'chief')) -> List[PvPanel]:
    """argparse Namespace（YAML マージ後）から PvPanel 一覧を作る。

    優先順: YAML `pv_panels`（list[dict] / {name: dict}）+ `pv_parts` +
    単一パネル CLI フラグ（`--pv-panel-size` 指定時のみ有効）。
    何も無ければ空リスト（= PV 計測しない）。hosts は verb が許す取付機体名
    （relative: deputy/chief、groundobs: target）。
    """
    panels: List[PvPanel] = []
    host_default = str(getattr(args, 'pv_host', None) or hosts[0])
    eff_default = float(getattr(args, 'pv_efficiency', 0.30) or 0.30)

    raw = getattr(args, 'pv_panels', None)
    items: List[Tuple[str, dict]] = []
    if isinstance(raw, dict):
        items = [(str(k), dict(v or {})) for k, v in raw.items()]
    elif isinstance(raw, (list, tuple)):
        for i, v in enumerate(raw):
            if not isinstance(v, dict):
                raise ValueError(f'pv_panels[{i}] は辞書で指定してください')
            items.append((str(v.get('name', f'panel{i}')), dict(v)))
    for name, spec in items:
        if spec.get('part'):
            panels.append(PvPanel(
                name=name, host=str(spec.get('host', host_default)), kind='part',
                part=str(spec['part']),
                efficiency=float(spec.get('efficiency', eff_default))))
            continue
        panels.append(PvPanel(
            name=name, host=str(spec.get('host', host_default)), kind='rect',
            center=_as_vec(spec.get('center', [0, 0, 0]), 3, f'pv_panels[{name}].center'),
            normal=_as_vec(spec.get('normal', [0, 0, 1]), 3, f'pv_panels[{name}].normal'),
            up=_as_vec(spec.get('up', [0, 1, 0]), 3, f'pv_panels[{name}].up'),
            size=_as_vec(spec.get('size', [1, 1]), 2, f'pv_panels[{name}].size'),
            efficiency=float(spec.get('efficiency', eff_default)),
            bsdf=spec.get('bsdf')))

    for part in (getattr(args, 'pv_parts', None) or []):
        panels.append(PvPanel(name=str(part), host=host_default, kind='part',
                              part=str(part), efficiency=eff_default))

    size = getattr(args, 'pv_panel_size', None)
    if size:
        panels.append(PvPanel(
            name=str(getattr(args, 'pv_panel_name', None) or 'panel'),
            host=host_default, kind='rect',
            center=_as_vec(getattr(args, 'pv_panel_center', None) or [0, 0, 0], 3, 'pv_panel_center'),
            normal=_as_vec(getattr(args, 'pv_panel_normal', None) or [0, 0, 1], 3, 'pv_panel_normal'),
            up=_as_vec(getattr(args, 'pv_panel_up', None) or [0, 1, 0], 3, 'pv_panel_up'),
            size=_as_vec(size, 2, 'pv_panel_size'), efficiency=eff_default))

    names = [p.name for p in panels]
    if len(set(names)) != len(names):
        raise ValueError(f'pv パネル名が重複しています: {names}')
    for p in panels:
        if p.host not in hosts:
            raise ValueError(f'pv panel {p.name!r}: host は {"|".join(hosts)}（{p.host!r}）')
    return panels


def csv_output_path(args: argparse.Namespace, output_dir, frame_start: Optional[int] = None):
    """PV 計測 CSV の置き場。

    mrender 経由（output_dir = runs/<run>/frames）ならラン直下、
    スタンドアロンなら output_dir 直下。--jobs のチャンクは
    `<name>.part<frame_start>.csv` に書き、親が結合する。
    """
    from pathlib import Path
    output_dir = Path(output_dir)
    name = str(getattr(args, 'pv_csv_name', None) or 'pv_irradiance.csv')
    base = output_dir.parent if output_dir.name == 'frames' else output_dir
    if frame_start is not None:
        stem, dot, ext = name.rpartition('.')
        name = f'{stem or ext}.part{int(frame_start):04d}.{ext if stem else "csv"}'
    return base / name


# ---------------------------------------------------------------------------
# シーン辞書への組み込み
# ---------------------------------------------------------------------------
def _sensor_dict(sensor_id: str, spp: int, seed: int) -> dict:
    # 注意: Mitsuba の辞書パーサは内容が同一のノードを 1 つのオブジェクトに
    # 畳むため（id 違いだけでは区別されない）、パネルごとに sampler の seed を
    # 変えて内容を一意にする。でないと 2 枚目で
    # "An endpoint can be only be attached to a single shape" になる（実測）。
    return {
        'type': 'irradiancemeter',
        'id': sensor_id,
        'sampler': {'type': 'independent', 'sample_count': int(spp),
                    'seed': int(seed)},
        'film': {'type': 'hdrfilm', 'width': 1, 'height': 1,
                 'rfilter': {'type': 'box'}, 'pixel_format': 'rgb'},
    }


def _basis_from_normal(normal, up) -> np.ndarray:
    """法線 n を +z、up を +y に寄せた 3×3 回転（列 = 新基底）。"""
    n = np.asarray(normal, dtype=float)
    n = n / np.linalg.norm(n)
    u = np.asarray(up, dtype=float)
    u = u - n * float(u @ n)
    if np.linalg.norm(u) < 1e-9:
        # up が法線と平行 → 適当な直交ベクトルを選ぶ
        u = np.cross(n, [1.0, 0.0, 0.0])
        if np.linalg.norm(u) < 1e-9:
            u = np.cross(n, [0.0, 1.0, 0.0])
    u = u / np.linalg.norm(u)
    x = np.cross(u, n)
    return np.column_stack([x, u, n])


def rect_to_world(panel: PvPanel, host_xform: mi.ScalarTransform4f) -> mi.ScalarTransform4f:
    """仮想矩形パネルの to_world（機体系 [m] → シーン [km]）。"""
    rot4 = np.eye(4)
    rot4[:3, :3] = _basis_from_normal(panel.normal, panel.up)
    c_km = [float(v) / 1000.0 for v in panel.center]
    half = [float(panel.size[0]) / 2000.0, float(panel.size[1]) / 2000.0, 1.0]
    return (host_xform
            @ mi.ScalarTransform4f.translate(c_km)
            @ mi.ScalarTransform4f(rot4.tolist())
            @ mi.ScalarTransform4f.scale(half))


DEFAULT_PANEL_BSDF = {
    # 太陽電池セル風（暗い青、弱い鏡面）
    'type': 'principled',
    'base_color': {'type': 'rgb', 'value': [0.04, 0.06, 0.16]},
    'metallic': 0.2,
    'roughness': 0.25,
}


def add_pv_sensors(scene_dict: dict, panels: Sequence[PvPanel],
                   host_xforms: Dict[str, mi.ScalarTransform4f],
                   spp: int) -> dict:
    """scene_dict にパネル（矩形 or 既存パーツ）と irradiancemeter を組み込む。

    in-place で書き換え、同じ dict を返す。矩形パネルは host の機体系に
    固定（毎フレームの host_xform に追従）。パーツ指定は既存キー
    `{host}_part_{name}` に sensor を入れ子にし、id を明示する
    （Mitsuba は辞書キーを shape id にしないため、後で
    `scene.shapes()` から引けるように必要）。
    """
    for i, p in enumerate(panels):
        key = p.scene_key
        if p.kind == 'part':
            if key not in scene_dict:
                avail = sorted(k for k in scene_dict if k.startswith(f'{p.host}_part_'))
                raise ValueError(f'pv パーツ {p.part!r} が {p.host} に見つかりません'
                                 f'（利用可能: {avail}）')
            shape = scene_dict[key]
        else:
            xf = host_xforms.get(p.host)
            if xf is None:
                raise ValueError(f'pv panel {p.name!r}: host {p.host!r} の姿勢がありません')
            shape = {
                'type': 'rectangle',
                'to_world': rect_to_world(p, xf),
                'bsdf': dict(p.bsdf or DEFAULT_PANEL_BSDF),
            }
            scene_dict[key] = shape
        shape['id'] = key
        shape['sensor'] = _sensor_dict(p.sensor_id, spp, seed=i + 1)
    return scene_dict


BLACK_ENV = {'type': 'constant', 'radiance': {'type': 'rgb', 'value': [0.0, 0.0, 0.0]}}
BLACK_BSDF = {'type': 'diffuse', 'reflectance': {'type': 'rgb', 'value': [0.0, 0.0, 0.0]}}


def pv_scene_dicts(scene_dict: dict, include_env: bool = False,
                   sun_rgb=None) -> Tuple[dict, dict, dict]:
    """(地球込み, 地球黒体, 地球なし) の PV 計測用シーン辞書 3 つを返す。

    いずれも画像用 scene_dict の浅いコピーで、既定では envmap を黒にする。
      - 地球込み − 地球黒体 = 地球照（地球の遮蔽は両方に同じに効く）
      - 地球黒体            = その他（機体相互反射 + 環境光）
      - 地球なし            = 直達の自己遮蔽レイキャスト用（地球による食は
                              解析の ν で扱うので、ここでは地球に遮らせない）
    sun_rgb を与えると太陽 irradiance を差し替える。absolute モードのように
    画像用シーンの太陽に食 ν が掛かっている場合は **ν を外した値** を渡すこと:
    衛星が地球の影にいても地球の昼側は太陽に照らされているので、地球照の
    計算で太陽を暗くしてはいけない（機体自身への直達・機体相互反射は
    シーン内の地球球体が幾何学的に遮る。半影は硬い影で近似）。
    """
    full = dict(scene_dict)
    if not include_env and 'envmap' in full:
        full['envmap'] = dict(BLACK_ENV)
    if sun_rgb is not None and 'sun' in full:
        sun = dict(full['sun'])
        sun['irradiance'] = {'type': 'rgb', 'value': [float(v) for v in sun_rgb]}
        full['sun'] = sun
    black = dict(full)
    if 'earth' in black:
        earth = dict(black['earth'])
        earth['bsdf'] = dict(BLACK_BSDF)
        black['earth'] = earth
    direct = {k: v for k, v in black.items() if k != 'earth'}
    return full, black, direct


# ---------------------------------------------------------------------------
# 計測
# ---------------------------------------------------------------------------
def _sensor_index(scene, sensor_id: str) -> int:
    for i, s in enumerate(scene.sensors()):
        if s.id() == sensor_id:
            return i
    raise KeyError(f'sensor {sensor_id!r} がシーンにありません: '
                   f'{[s.id() for s in scene.sensors()]}')


def _shape_by_id(scene, shape_id: str):
    for sh in scene.shapes():
        if sh.id() == shape_id:
            return sh
    raise KeyError(f'shape {shape_id!r} がシーンにありません')


def render_irradiance_rgb(scene, sensor_id: str, spp: int) -> np.ndarray:
    """irradiancemeter を 1 回レンダして E_rgb（レンダ単位）を返す。"""
    idx = _sensor_index(scene, sensor_id)
    img = mi.render(scene, sensor=idx, spp=int(spp))
    return np.asarray(img, dtype=float).reshape(-1)[:3]


def direct_sun_factor(scene, shape_id: str, sun_dir_scene, n_samples: int,
                      seed: int = 0) -> Tuple[float, float, float]:
    """(⟨max(0,n·ŝ)·vis⟩, ⟨n·ŝ⟩, ⟨vis | n·ŝ>0⟩) を返す。

    シェイプ上を一様サンプルし、各点から太陽方向へレイを飛ばして
    機体等による遮蔽を判定する。面積 [シーン単位²] は shape.surface_area()。
    """
    import drjit as dr
    sh = _shape_by_id(scene, shape_id)
    n = max(16, int(n_samples))
    sampler = mi.load_dict({'type': 'independent'})
    sampler.seed(int(seed), n)
    ps = sh.sample_position(0.0, sampler.next_2d())
    s = np.asarray(sun_dir_scene, dtype=float)
    s = s / np.linalg.norm(s)
    sun = mi.Vector3f(float(s[0]), float(s[1]), float(s[2]))
    # 面から僅かに浮かせて自己交差を避ける（シーン単位 km: 1e-7 km = 0.1 mm）
    eps = 1e-7
    occluded = scene.ray_test(mi.Ray3f(ps.p + ps.n * eps, sun))
    # 注意: 平面シェイプ（rectangle）は法線が一定なので ps.n が長さ 1 で返る。
    # numpy 側で明示的にサンプル数へブロードキャストしてから集計する
    cos = np.broadcast_to(np.asarray(dr.dot(ps.n, sun), dtype=float).reshape(-1), (n,))
    vis = np.where(np.broadcast_to(np.asarray(occluded, dtype=bool).reshape(-1), (n,)),
                   0.0, 1.0)
    factor = float(np.mean(np.maximum(cos, 0.0) * vis))
    mean_cos = float(np.mean(cos))
    lit = cos > 0.0
    vis_lit = float(vis[lit].mean()) if lit.any() else 0.0
    return factor, mean_cos, vis_lit


def surface_area_m2(scene, shape_id: str) -> float:
    """シーン単位 km² → m²。"""
    sh = _shape_by_id(scene, shape_id)
    a = sh.surface_area()
    try:
        a = float(np.asarray(a).reshape(-1)[0])
    except Exception:
        a = float(a)
    return a * 1.0e6


@dataclass
class PvMeasurement:
    panel: str
    host: str
    area_m2: float
    nu: float
    cos_sun: float
    sun_visible: float
    e_direct: float
    e_earth: float
    e_other: float
    e_earth_rgb: Tuple[float, float, float]
    efficiency: float

    @property
    def e_total(self) -> float:
        return self.e_direct + self.e_earth + self.e_other

    @property
    def power_w(self) -> float:
        return self.efficiency * self.area_m2 * self.e_total

    def row(self, frame: int, t_s: float) -> dict:
        return {
            'frame': frame, 't_s': f'{t_s:.6f}', 'panel': self.panel, 'host': self.host,
            'area_m2': f'{self.area_m2:.6f}', 'nu': f'{self.nu:.6f}',
            'cos_sun': f'{self.cos_sun:.6f}', 'sun_visible': f'{self.sun_visible:.6f}',
            'e_direct_wm2': f'{self.e_direct:.4f}', 'e_earth_wm2': f'{self.e_earth:.4f}',
            'e_other_wm2': f'{self.e_other:.4f}', 'e_total_wm2': f'{self.e_total:.4f}',
            'p_w': f'{self.power_w:.4f}',
            'e_earth_r_wm2': f'{self.e_earth_rgb[0]:.4f}',
            'e_earth_g_wm2': f'{self.e_earth_rgb[1]:.4f}',
            'e_earth_b_wm2': f'{self.e_earth_rgb[2]:.4f}',
        }


def measure_panels(scene_full, scene_black, scene_direct, panels: Sequence[PvPanel], *,
                   sun_dir_scene, sun_rgb_frame, sun_rgb_base, s0_wm2: float,
                   nu: float, spp: int, direct_samples: int,
                   seed: int = 0) -> List[PvMeasurement]:
    """全パネルの 1 フレーム分の計測。

    Args:
        scene_full     : 地球込みの mi.Scene（PV センサ入り、姿勢同期済み）
        scene_black    : 地球を黒体にした mi.Scene（None なら地球照 = 0 扱い）
        scene_direct   : 地球なしの mi.Scene（直達の自己遮蔽レイキャスト用。
                         None なら scene_full を使う）
        sun_dir_scene  : シーン座標での「太陽の方向」単位ベクトル
        sun_rgb_frame  : このフレームの太陽 RGB（ν 込み、レンダ単位）
        sun_rgb_base   : ν 無しの太陽 RGB（物理換算の基準）
        s0_wm2         : 太陽定数 [W/m²]
    """
    if scene_direct is None:
        scene_direct = scene_full
    base = np.asarray(sun_rgb_base, dtype=float).reshape(3)
    frame_rgb = np.asarray(sun_rgb_frame, dtype=float).reshape(3)
    lum_base = max(luminance(base), 1e-12)
    scale = float(s0_wm2) / lum_base                   # レンダ単位 → W/m²
    scale_rgb = float(s0_wm2) / np.maximum(base, 1e-12)  # チャネル別
    out: List[PvMeasurement] = []
    for i, p in enumerate(panels):
        e_full = render_irradiance_rgb(scene_full, p.sensor_id, spp)
        if scene_black is not None:
            e_no = render_irradiance_rgb(scene_black, p.sensor_id, spp)
        else:
            e_no = e_full.copy()
        e_earth_rgb = np.maximum(e_full - e_no, 0.0)
        factor, mean_cos, vis_lit = direct_sun_factor(
            scene_direct, p.scene_key, sun_dir_scene, direct_samples, seed=seed * 131 + i)
        e_direct = luminance(frame_rgb) * factor * scale
        e_earth = luminance(e_earth_rgb) * scale
        e_other = luminance(e_no) * scale
        area = p.area_m2 if p.kind == 'rect' else surface_area_m2(scene_full, p.scene_key)
        out.append(PvMeasurement(
            panel=p.name, host=p.host, area_m2=area, nu=float(nu),
            cos_sun=mean_cos, sun_visible=vis_lit,
            e_direct=e_direct, e_earth=e_earth, e_other=e_other,
            e_earth_rgb=tuple(float(v) for v in (e_earth_rgb * scale_rgb)),
            efficiency=float(p.efficiency)))
    return out


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
class PvCsvWriter:
    def __init__(self, path) -> None:
        self.path = str(path)
        self._f = open(self.path, 'w', newline='')
        self._w = csv.DictWriter(self._f, fieldnames=CSV_COLUMNS)
        self._w.writeheader()

    def write(self, frame: int, t_s: float, ms: Sequence[PvMeasurement]) -> None:
        for m in ms:
            self._w.writerow(m.row(frame, t_s))
        self._f.flush()

    def close(self) -> None:
        self._f.close()


def merge_csv_parts(parts: Sequence[str], out_path: str) -> None:
    """--jobs のチャンク CSV をフレーム順に結合する。"""
    rows: List[dict] = []
    for p in parts:
        with open(p, newline='') as f:
            rows.extend(csv.DictReader(f))
    rows.sort(key=lambda r: (int(r['frame']), r['panel']))
    with open(out_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def format_summary(ms: Sequence[PvMeasurement]) -> str:
    parts = []
    for m in ms:
        parts.append(f"{m.panel}: dir {m.e_direct:6.1f} + earth {m.e_earth:5.1f} "
                     f"+ other {m.e_other:4.1f} = {m.e_total:6.1f} W/m²  P={m.power_w:.1f} W")
    return '  '.join(parts)
