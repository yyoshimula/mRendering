#!/usr/bin/env python3
"""
相対運動レンダリング: 軌道力学なし。相対位置と相対姿勢のみを与える。

simple_rotation.py の兄弟スクリプト。
- simple_rotation: 単機の姿勢運動だけを扱う
- relative_motion: 2機（chief / deputy）の相対配置を扱う

軌道伝播・地球は一切描画しない。chief は原点に固定し、deputy を
ユーザー指定の相対位置 r_rel と相対姿勢クォータニオン q_rel で配置する。

座標系・量の規約:
  - シーン座標 = chief 中心の Hill / RTN(LVLH) frame として扱う想定
    （chief を原点・恒等姿勢に置けば、rel_position はそのまま RTN 成分）
  - 位置単位 [km]（Mitsuba シーン単位とみなして直接配置。スケール感は
    モデル側の scale で調整する）
  - クォータニオン: スカラーラスト [qx, qy, qz, qw] (yoshimulib SCALAR=4)
  - 角度はラジアン、角速度 rad/s、慣性モーメント kg·m²
  - rel_quat の意味: chief Body → deputy Body の相対姿勢
    （chief が恒等のとき、そのまま deputy の慣性姿勢になる）

入力モード:
  static : 単一の (r_rel, q_rel) を全フレームへ適用（既定）
  csv    : CSV (time_s, x, y, z, qx, qy, qz, qw) を時系列で読み込む
           列の意味は CLAUDE.md「CSV 入力スキーマ」を参照
           - 位置 (x,y,z) は chief の RTN 基底（HCW frame）で表した deputy の相対位置
           - 姿勢 (qx,qy,qz,qw) は chief Body → deputy Body の相対クォータニオン
           例: input/rel_state_hcw.csv（matlabSample/mainHCW.m が出力）
  tumble : chief 静止 + deputy をオイラー回転（位置は固定、姿勢のみ動的）

verb との関係:
  - mrender.py の relative サブコマンドから呼ばれる

使い方:
  # mrender 経由（推奨、runs/ に自動配置）
  python mrender.py relative --config presets/relative_static.yaml

  # スタンドアロン: 静止配置（既定）
  python relative_motion.py --frames 30 \\
      --rel-position 1.5 0 0 --rel-quat 0 0 0 1

  # CSV駆動
  python relative_motion.py --mode csv --rel-csv input/rel_state_sample.csv --frames 60

  # deputy をタンブリング（位置固定）
  python relative_motion.py --mode tumble \\
      --rel-position 1.5 0 0 --wx 0.3 --wy 0.1 --wz 1.0 --frames 60

  # YAMLプリセット
  python relative_motion.py --config presets/relative_static.yaml
"""

import argparse
import csv as _csv
from pathlib import Path
import sys
from typing import List, Optional, Tuple
import numpy as np
from scipy.integrate import solve_ivp
import yaml

# プロジェクトルートを sys.path に追加（yoshimulib のため）
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mitsuba as mi

from yoshimulib.attitude.kinematics import q_prop_mat
from yoshimulib.attitude.quaternion import q2dcm

mi.set_variant('llvm_ad_rgb')

# スカラー部の位置: 4 = scalar-last [qx, qy, qz, qw]
# (yoshimulib の規約: scalar=4 → 4 番目要素が w)
SCALAR = 4


# ---------------------------------------------------------------------------
# 姿勢動力学（simple_rotation.py と同一実装）
# ---------------------------------------------------------------------------
def propagate_attitude(q: np.ndarray, w: np.ndarray, I_body: np.ndarray,
                       I_inv: np.ndarray, dt: float, substeps: int = 10) -> tuple:
    """オイラー回転運動方程式 + クォータニオンキネマティクスで dt 分伝播。

    動力学: I·ω̇ = -ω × (I·ω)        （トルクフリー、Body 系）
    キネマ : q(t+h) = Φ(ω,h)·q(t)     （yoshimulib q_prop_mat、scalar-last）

    Args:
        q : クォータニオン [qx,qy,qz,qw] (Body→Inertial 回転)
        w : 角速度 [rad/s] (Body 系)
        I_body / I_inv : 慣性テンソルとその逆 [kg·m²]
        dt : 時間刻み [s]
        substeps : 内部サブステップ数

    Returns:
        (q_next, w_next): 更新後のクォータニオンと角速度
    """
    h = dt / substeps
    t_eval = [h * i for i in range(1, substeps + 1)]

    def euler_dynamics(t, w_vec):
        # ω̇ = -I⁻¹ · (ω × I·ω)
        return I_inv @ (-np.cross(w_vec, I_body @ w_vec))

    sol = solve_ivp(euler_dynamics, [0, dt], w, method='RK45',
                    t_eval=t_eval, rtol=1e-10, atol=1e-12)

    for i in range(substeps):
        w_i = sol.y[:, i]
        # ω が一定な微小区間 h に対する解析的な離散伝播行列
        Phi = q_prop_mat(SCALAR, h, w_i.reshape(1, 3))
        q = Phi @ q
        q = q / np.linalg.norm(q)  # 単位ノルムを再保証

    return q, sol.y[:, -1]


# ---------------------------------------------------------------------------
# Transform ヘルパー
# ---------------------------------------------------------------------------
def make_body_transform(position: np.ndarray, q: np.ndarray) -> mi.ScalarTransform4f:
    """位置 + クォータニオン姿勢から Mitsuba Transform4f を組む。

    Mitsuba の 4x4 同次行列の並びは
        [ R(3x3)  t(3,) ]
        [ 0 0 0    1    ]
    で、列ベクトル右掛け規約 world = M·local。回転行列は body→inertial の
    DCM そのものを上 3x3 に入れ、最右列に並進 (慣性座標での衛星位置) を入れる。

    Args:
        position : 慣性座標系での衛星中心位置（シーン単位）
        q        : クォータニオン [qx,qy,qz,qw]（body→inertial）

    Returns:
        Mitsuba ScalarTransform4f（4x4 同次変換）
    """
    C = q2dcm(q, scalar=SCALAR)
    m = np.eye(4)
    m[:3, :3] = C
    m[:3, 3] = np.asarray(position, dtype=float)
    return mi.ScalarTransform4f(m)


# ---------------------------------------------------------------------------
# CSV 入力
# ---------------------------------------------------------------------------
def load_csv_relative_state(path: str) -> List[Tuple[float, np.ndarray, np.ndarray]]:
    """CSV を [(time_s, position(3,), quat(4,)), ...] に読み込む。

    スキーマ (CLAUDE.md「CSV 入力スキーマ」と整合):
        time_s [s]                       … サンプリング時刻
        x, y, z [km, シーン単位扱い]     … chief の RTN 基底（HCW frame）で
                                            表した deputy の相対位置
        qx, qy, qz, qw                   … chief Body → deputy Body の相対姿勢
                                            （スカラーラスト規約、内部で正規化）

    生成元の例: matlabSample/mainHCW.m → input/rel_state_hcw.csv
    パース規則:
      - `#` から始まる行はコメントとしてスキップ
      - 1 行目のヘッダは float 化に失敗するので自動スキップ
      - 8 列未満の行はスキップ
      - クォータニオンノルムが 0 なら例外
      - 時刻順にソートして返す
    時間範囲外の問い合わせは interp_csv_state() で端点クランプ＋ 1 度警告。
    """
    states: List[Tuple[float, np.ndarray, np.ndarray]] = []
    with open(path) as handle:
        reader = _csv.reader(handle)
        for row in reader:
            if not row or row[0].strip().startswith('#'):
                continue
            try:
                vals = [float(v) for v in row[:8]]
            except ValueError:
                # ヘッダ行などは飛ばす
                continue
            if len(vals) < 8:
                continue
            t = vals[0]
            pos = np.array(vals[1:4])
            q = np.array(vals[4:8])
            n = np.linalg.norm(q)
            if n < 1e-12:
                raise ValueError(f"CSV のクォータニオンがゼロです: time={t}")
            q = q / n
            states.append((t, pos, q))
    if not states:
        raise ValueError(f"有効な行が見つかりませんでした: {path}")
    states.sort(key=lambda s: s[0])
    return states


_REL_OUT_OF_RANGE_WARNED: dict = {}


def interp_csv_state(states: List[Tuple[float, np.ndarray, np.ndarray]],
                     t: float, *, csv_label: str = 'rel_csv') -> Tuple[np.ndarray, np.ndarray]:
    """時刻 t における (位置, クォータニオン) を補間する。

    位置は線形補間。クォータニオンは符号を揃えてから線形補間 + 正規化
    （nlerp; SLERP の高速近似）。範囲外 t は端点クランプし、初回のみ警告。

    Args:
        states : load_csv_relative_state の戻り値（時刻昇順）
        t      : 評価時刻 [s]
        csv_label : 警告メッセージ用ラベル

    Returns:
        (position(3,) [km/シーン単位], quaternion(4,) [qx,qy,qz,qw])
    """
    t_min = states[0][0]
    t_max = states[-1][0]
    if (t < t_min - 1e-9 or t > t_max + 1e-9) and not _REL_OUT_OF_RANGE_WARNED.get(csv_label):
        import sys
        print(
            f"⚠ relative CSV: 要求時刻 t={t:.3f}s が範囲 "
            f"[{t_min:.3f}, {t_max:.3f}]s を超えています。端点にクランプします ({csv_label})。",
            file=sys.stderr,
        )
        _REL_OUT_OF_RANGE_WARNED[csv_label] = True
    if t <= t_min:
        return states[0][1].copy(), states[0][2].copy()
    if t >= t_max:
        return states[-1][1].copy(), states[-1][2].copy()
    for i in range(len(states) - 1):
        t0, p0, q0 = states[i]
        t1, p1, q1 = states[i + 1]
        if t0 <= t <= t1:
            alpha = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            pos = (1.0 - alpha) * p0 + alpha * p1
            # クォータニオンは ±q が同じ姿勢を表すので、内積 < 0 なら符号反転して
            # 短い方の弧で補間する（SLERP の代用となる簡易 nlerp）
            if np.dot(q0, q1) < 0.0:
                q1 = -q1
            q = (1.0 - alpha) * q0 + alpha * q1
            q = q / np.linalg.norm(q)
            return pos, q
    return states[-1][1].copy(), states[-1][2].copy()


# ---------------------------------------------------------------------------
# シーン構成要素（simple_rotation.py と同一スタイル）
# ---------------------------------------------------------------------------
def create_axes(length: float = 1.5, radius: float = 0.015,
                prefix: str = 'inertial',
                transform: mi.ScalarTransform4f = None,
                colors=None, glow: float = 0.5) -> dict:
    """座標軸を描画（cylinder + 先端 sphere）。"""
    if transform is None:
        transform = mi.ScalarTransform4f()
    if colors is None:
        colors = [
            [1.0, 0.2, 0.2],
            [0.2, 1.0, 0.2],
            [0.2, 0.2, 1.0],
        ]

    objects = {}
    directions = [('x', [1, 0, 0]), ('y', [0, 1, 0]), ('z', [0, 0, 1])]

    for (name, direction), color in zip(directions, colors):
        d = np.array(direction, dtype=float)
        z = np.array([0, 0, 1.0])
        if np.allclose(d, z):
            axis_rot = mi.ScalarTransform4f()
        elif np.allclose(d, -z):
            axis_rot = mi.ScalarTransform4f.rotate([1, 0, 0], 180)
        else:
            rot_axis = np.cross(z, d)
            rot_angle = np.degrees(np.arccos(np.clip(np.dot(z, d), -1, 1)))
            axis_rot = mi.ScalarTransform4f.rotate(rot_axis.tolist(), rot_angle)

        shaft = {
            'type': 'cylinder',
            'to_world': transform @ axis_rot @ mi.ScalarTransform4f.scale([radius, radius, length]),
            'bsdf': {'type': 'diffuse', 'reflectance': {'type': 'rgb', 'value': color}},
        }
        if glow > 0:
            shaft['emitter'] = {
                'type': 'area',
                'radiance': {'type': 'rgb', 'value': [c * glow for c in color]},
            }
        objects[f'{prefix}_axis_{name}'] = shaft

        tip_local = d * length
        tip_xform = transform @ mi.ScalarTransform4f.translate(tip_local.tolist())
        tip = {
            'type': 'sphere',
            'to_world': tip_xform @ mi.ScalarTransform4f.scale([radius * 2.5] * 3),
            'bsdf': {'type': 'diffuse', 'reflectance': {'type': 'rgb', 'value': color}},
        }
        if glow > 0:
            tip['emitter'] = {
                'type': 'area',
                'radiance': {'type': 'rgb', 'value': [c * glow for c in color]},
            }
        objects[f'{prefix}_axis_{name}_tip'] = tip

    return objects


def create_satellite(body_transform: mi.ScalarTransform4f) -> dict:
    """簡易プロシージャル衛星モデル（本体・パネル・アンテナ）。"""
    objects = {}

    objects['body'] = {
        'type': 'cube',
        'to_world': body_transform @ mi.ScalarTransform4f.scale([0.3, 0.3, 0.4]),
        'bsdf': {
            'type': 'roughplastic',
            'diffuse_reflectance': {'type': 'rgb', 'value': [0.7, 0.7, 0.75]},
            'alpha': 0.15,
            'int_ior': 1.5,
        }
    }

    panel_bsdf = {
        'type': 'diffuse',
        'reflectance': {'type': 'rgb', 'value': [0.05, 0.05, 0.2]},
    }
    objects['panel_left'] = {
        'type': 'cube',
        'to_world': body_transform
            @ mi.ScalarTransform4f.translate([-0.7, 0, 0])
            @ mi.ScalarTransform4f.scale([0.5, 0.02, 0.3]),
        'bsdf': panel_bsdf,
    }
    objects['panel_right'] = {
        'type': 'cube',
        'to_world': body_transform
            @ mi.ScalarTransform4f.translate([0.7, 0, 0])
            @ mi.ScalarTransform4f.scale([0.5, 0.02, 0.3]),
        'bsdf': panel_bsdf,
    }

    objects['antenna'] = {
        'type': 'cylinder',
        'to_world': body_transform
            @ mi.ScalarTransform4f.translate([0, 0, 0.5])
            @ mi.ScalarTransform4f.scale([0.05, 0.05, 0.3]),
        'bsdf': {'type': 'conductor', 'material': 'Au'},
    }

    return objects


def split_obj_by_parts(obj_path: str) -> dict:
    """OBJ を 'o' ディレクティブでパーツ分割し、一時ファイルに書き出す。"""
    import tempfile

    with open(obj_path) as f:
        lines = f.readlines()

    header_lines: List[str] = []
    parts: dict = {}
    current_part: Optional[str] = None

    for line in lines:
        stripped = line.strip()
        if stripped.startswith('o '):
            current_part = stripped[2:].strip()
            parts[current_part] = []
        elif stripped.startswith(('v ', 'vn ', 'vt ', 'mtllib ')):
            header_lines.append(line)
        elif stripped.startswith(('f ', 'usemtl ', 's ')):
            if current_part is not None:
                parts[current_part].append(line)
            else:
                parts.setdefault('default', []).append(line)

    if not parts:
        return {'default': obj_path}

    tmp_dir = tempfile.mkdtemp(prefix='mrender_relparts_')
    result = {}
    for part_name, face_lines in parts.items():
        if not face_lines:
            continue
        tmp_path = Path(tmp_dir) / f'{part_name}.obj'
        with open(tmp_path, 'w') as f:
            f.writelines(header_lines)
            f.write(f'o {part_name}\n')
            f.writelines(face_lines)
        result[part_name] = str(tmp_path)
    return result


def load_model_with_parts(obj_path: str, part_bsdfs: dict, default_bsdf: dict,
                          body_transform: mi.ScalarTransform4f, scale: float = 1.0,
                          prefix: str = 'model') -> dict:
    """OBJ をパーツ分割して読み込み、各パーツに BSDF を適用する。"""
    fallback_bsdf = {
        'type': 'principled',
        'base_color': {'type': 'rgb', 'value': [0.7, 0.7, 0.7]},
        'metallic': 0.3,
        'roughness': 0.4,
    }
    transform = body_transform @ mi.ScalarTransform4f.scale([scale] * 3)
    part_files = split_obj_by_parts(obj_path)

    objects = {}
    for part_name, part_path in part_files.items():
        bsdf = part_bsdfs.get(part_name, default_bsdf) or fallback_bsdf
        objects[f'{prefix}_part_{part_name}'] = {
            'type': 'obj',
            'filename': part_path,
            'to_world': transform,
            'bsdf': bsdf,
        }
    return objects


def add_object(objects: dict, body_transform: mi.ScalarTransform4f,
               *, model_path: Optional[str], part_bsdfs: Optional[dict],
               default_bsdf: Optional[dict], scale: float, prefix: str) -> None:
    """1 物体を objects 辞書へ追加する。OBJ 指定時はパーツ別 BSDF も適用。"""
    if model_path:
        objects.update(load_model_with_parts(
            model_path, part_bsdfs or {}, default_bsdf or {},
            body_transform, scale, prefix=prefix,
        ))
    else:
        # プロシージャル衛星はキー名にプレフィックスを付けて両機の衝突を防ぐ
        for key, val in create_satellite(body_transform).items():
            objects[f'{prefix}_{key}'] = val


# ---------------------------------------------------------------------------
# シーン構築
# ---------------------------------------------------------------------------
def build_scene(chief_xform: mi.ScalarTransform4f,
                deputy_xform: mi.ScalarTransform4f,
                *, width: int, height: int, samples: int,
                chief_model: Optional[str], chief_parts: Optional[dict],
                chief_default_bsdf: Optional[dict], chief_scale: float,
                deputy_model: Optional[str], deputy_parts: Optional[dict],
                deputy_default_bsdf: Optional[dict], deputy_scale: float,
                camera_origin, camera_target, camera_fov: float,
                show_inertial_axes: bool, show_body_axes: bool) -> dict:
    """Mitsuba シーン辞書を組み立てる。"""
    scene_dict = {
        'type': 'scene',
        'integrator': {'type': 'path', 'max_depth': 8},
        'camera': {
            'type': 'perspective',
            'fov': camera_fov,
            'to_world': mi.ScalarTransform4f.look_at(
                origin=camera_origin,
                target=camera_target,
                up=[0, 1, 0],
            ),
            'film': {
                'type': 'hdrfilm',
                'width': width,
                'height': height,
                'rfilter': {'type': 'gaussian'},
            },
            'sampler': {'type': 'independent', 'sample_count': samples},
        },
        'sun': {
            'type': 'directional',
            'direction': [1, -1, -0.5],
            'irradiance': {'type': 'rgb', 'value': [5, 5, 4.8]},
        },
        'envmap': {
            'type': 'constant',
            'radiance': {'type': 'rgb', 'value': [0.02, 0.02, 0.03]},
        },
    }

    if show_inertial_axes:
        scene_dict.update(create_axes(prefix='inertial', glow=0.5))

    if show_body_axes:
        # chief 機体軸（オレンジ・黄・紫）
        scene_dict.update(create_axes(
            length=0.8, radius=0.02, prefix='chief_body',
            transform=chief_xform,
            colors=[[1.0, 0.6, 0.0], [1.0, 1.0, 0.0], [0.8, 0.0, 1.0]],
            glow=0.8,
        ))
        # deputy 機体軸（シアン・マゼンタ・ライム）
        scene_dict.update(create_axes(
            length=0.8, radius=0.02, prefix='deputy_body',
            transform=deputy_xform,
            colors=[[0.0, 0.8, 1.0], [1.0, 0.2, 0.8], [0.6, 1.0, 0.2]],
            glow=0.8,
        ))

    add_object(scene_dict, chief_xform,
               model_path=chief_model, part_bsdfs=chief_parts,
               default_bsdf=chief_default_bsdf, scale=chief_scale, prefix='chief')
    add_object(scene_dict, deputy_xform,
               model_path=deputy_model, part_bsdfs=deputy_parts,
               default_bsdf=deputy_default_bsdf, scale=deputy_scale, prefix='deputy')

    return scene_dict


# ---------------------------------------------------------------------------
# YAML 読み込み（simple_rotation.py と同じ 1 段フラット化）
# ---------------------------------------------------------------------------
def load_yaml_config(paths: List[str]) -> dict:
    merged: dict = {}
    for path in paths:
        with open(path) as f:
            data = yaml.safe_load(f)
        if data is None:
            continue
        for key, val in data.items():
            if isinstance(val, dict):
                for k, v in val.items():
                    merged[k.replace('-', '_')] = v
            else:
                merged[key.replace('-', '_')] = val
    return merged


# ---------------------------------------------------------------------------
# パーサ・実行ロジックの分離（mrender verb から再利用するため）
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='相対位置・相対姿勢を直接与えて 2 機をレンダリング（軌道力学なし）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
例:
  # mrender 経由（推奨、runs/ に自動配置）
  python mrender.py relative --config presets/relative_static.yaml

  # 静止配置
  python relative_motion.py --frames 30 --rel-position 1.5 0 0

  # CSV駆動（time_s, x, y, z, qx, qy, qz, qw）
  python relative_motion.py --mode csv --rel-csv input/rel_state_sample.csv

  # deputy をタンブリング
  python relative_motion.py --mode tumble --rel-position 1.5 0 0 \\
      --wx 0.3 --wy 0.1 --wz 1.0
        """,
    )
    parser.add_argument('--config', nargs='+', default=[],
                        help='YAMLプリセットファイル（複数指定可、後のファイルが優先）')

    # レンダリング
    parser.add_argument('--frames', type=int, default=30, help='フレーム数')
    parser.add_argument('--fps', type=float, default=30.0, help='フレームレート [fps]')
    parser.add_argument('--samples', type=int, default=128, help='サンプル数/ピクセル')
    parser.add_argument('--width', type=int, default=640, help='画像幅')
    parser.add_argument('--height', type=int, default=480, help='画像高さ')
    parser.add_argument('--output-dir', type=str, default=None,
                        help='出力ディレクトリ（未指定時は output/relative）')
    parser.add_argument('--duration-sec', type=float, default=None,
                        help='シミュレートする総時間 [s]。指定時は dt = duration_sec / frames で frames を均等配分する。'
                             '未指定時は dt = 1/fps（実時間 1:1）。')
    parser.add_argument('--start-time', type=float, default=0.0,
                        help='シミュレーション開始時刻 [s]（CSV 駆動時の参照開始点）')

    # モード
    parser.add_argument('--mode', choices=['static', 'csv', 'tumble'], default='static',
                        help='相対状態の与え方')

    # 相対位置・相対姿勢（static / tumble の初期値）
    parser.add_argument('--rel-position', nargs=3, type=float, default=[1.5, 0.0, 0.0],
                        help='deputy の chief に対する相対位置 [x y z]（シーン単位）')
    parser.add_argument('--rel-quat', nargs=4, type=float, default=[0.0, 0.0, 0.0, 1.0],
                        help='deputy の chief に対する相対クォータニオン [qx qy qz qw]')

    # chief 配置（通常は原点・恒等）
    parser.add_argument('--chief-position', nargs=3, type=float, default=[0.0, 0.0, 0.0],
                        help='chief 位置 [x y z]')
    parser.add_argument('--chief-quat', nargs=4, type=float, default=[0.0, 0.0, 0.0, 1.0],
                        help='chief クォータニオン [qx qy qz qw]')

    # CSV モード
    parser.add_argument('--rel-csv', type=str, default=None,
                        help='相対状態 CSV: time_s,x,y,z,qx,qy,qz,qw')

    # tumble モード（オイラー回転動力学パラメータ）
    parser.add_argument('--Ix', type=float, default=4.0, help='慣性モーメント Ix [kg·m²]')
    parser.add_argument('--Iy', type=float, default=2.0, help='慣性モーメント Iy [kg·m²]')
    parser.add_argument('--Iz', type=float, default=1.0, help='慣性モーメント Iz [kg·m²]')
    parser.add_argument('--wx', type=float, default=0.3, help='初期角速度 ωx [rad/s]')
    parser.add_argument('--wy', type=float, default=0.1, help='初期角速度 ωy [rad/s]')
    parser.add_argument('--wz', type=float, default=1.0, help='初期角速度 ωz [rad/s]')

    # モデル（chief / deputy 共通の OBJ + パーツ別 BSDF）
    parser.add_argument('--chief-model', type=str, default=None,
                        help='chief OBJ モデルパス')
    parser.add_argument('--chief-scale', type=float, default=1.0)
    parser.add_argument('--deputy-model', type=str, default=None,
                        help='deputy OBJ モデルパス')
    parser.add_argument('--deputy-scale', type=float, default=1.0)

    # カメラ
    parser.add_argument('--camera-origin', nargs=3, type=float, default=[3.5, 2.5, 2.0],
                        help='カメラ位置 [x y z]')
    parser.add_argument('--camera-target', nargs=3, type=float, default=[0.5, 0.0, 0.0],
                        help='カメラ注視点 [x y z]')
    parser.add_argument('--camera-fov', type=float, default=45.0, help='視野角 [deg]')

    # 動画化
    parser.add_argument('--make-video', action='store_true', default=False,
                        help='レンダ後に ffmpeg で動画化する')
    parser.add_argument('--video-fps', type=float, default=None,
                        help='動画 fps（未指定時はレンダリング fps を使用）')
    parser.add_argument('--video-name', type=str, default='output.mp4',
                        help='動画ファイル名（run ディレクトリ直下に出力）')

    # 軸の表示
    parser.add_argument('--show-inertial-axes', action='store_true', default=True,
                        help='慣性軸を描画する（既定: 有効）')
    parser.add_argument('--no-inertial-axes', dest='show_inertial_axes',
                        action='store_false')
    parser.add_argument('--show-body-axes', action='store_true', default=True,
                        help='機体軸を描画する（既定: 有効）')
    parser.add_argument('--no-body-axes', dest='show_body_axes', action='store_false')

    return parser


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """CLI と YAML をマージ（CLI > YAML > argparseデフォルト）。

    手順:
      ① 一度 parse_known_args で --config だけ取り出す
      ② YAML を読み込み set_defaults でデフォルト値を上書き
      ③ もう一度 parse_args で CLI 値を最優先にマージ
    """
    parser = build_parser()
    args_pre, _ = parser.parse_known_args(argv)
    if args_pre.config:
        parser.set_defaults(**load_yaml_config(args_pre.config))
    return parser.parse_args(argv)


def _run(args: argparse.Namespace) -> None:
    """argparse Namespace を受け取り、シーンを連番でレンダする。

    フレームごとの処理:
      ① モードに応じて (rel_pos, rel_q) を決定
         - static: 初期値固定
         - csv   : 時刻 t を CSV 補間
         - tumble: rel_pos 固定、rel_q は前フレームから姿勢動力学で進める
      ② chief 姿勢で rel_pos を慣性座標に持ち上げる:
            r_dep^I = r_chief^I + R(q_chief) · r_rel
         q_dep^I は chief 恒等なら rel_q そのまま、そうでなければ
         q_chief * q_rel をクォータニオン積で合成
      ③ Mitsuba シーン辞書を組み立てて mi.render() → PNG 保存
      ④ tumble の場合のみ q_dep, w_dep を propagate_attitude で次フレームへ
    """
    output_dir = Path(args.output_dir or 'output/relative')
    output_dir.mkdir(parents=True, exist_ok=True)

    # YAML 経由で渡る dict 系（chief_parts 等）は getattr で拾う
    chief_parts = getattr(args, 'chief_parts', None) or getattr(args, 'chief_model_parts', None)
    chief_default_bsdf = getattr(args, 'chief_bsdf', None) or getattr(args, 'chief_model_bsdf', None)
    deputy_parts = getattr(args, 'deputy_parts', None) or getattr(args, 'deputy_model_parts', None)
    deputy_default_bsdf = getattr(args, 'deputy_bsdf', None) or getattr(args, 'deputy_model_bsdf', None)

    # chief 姿勢（全フレームで固定）
    chief_pos = np.asarray(args.chief_position, dtype=float)
    chief_q = np.asarray(args.chief_quat, dtype=float)
    chief_q = chief_q / np.linalg.norm(chief_q)
    chief_xform = make_body_transform(chief_pos, chief_q)

    # モード別の deputy 状態シーケンス生成
    csv_states = None
    if args.mode == 'csv':
        if not args.rel_csv:
            raise ValueError('--mode csv のときは --rel-csv が必須です')
        csv_states = load_csv_relative_state(args.rel_csv)

    rel_pos_init = np.asarray(args.rel_position, dtype=float)
    rel_q_init = np.asarray(args.rel_quat, dtype=float)
    rel_q_init = rel_q_init / np.linalg.norm(rel_q_init)

    if args.duration_sec is not None and args.duration_sec > 0 and args.frames > 0:
        dt = args.duration_sec / args.frames
    else:
        dt = 1.0 / args.fps if args.fps > 0 else 1.0
    start_t = float(getattr(args, 'start_time', 0.0) or 0.0)

    # tumble モード用の初期状態
    q_dep = rel_q_init.copy()
    w_dep = np.array([args.wx, args.wy, args.wz], dtype=float)
    I_body = np.diag([args.Ix, args.Iy, args.Iz])
    I_inv = np.diag([1.0 / args.Ix, 1.0 / args.Iy, 1.0 / args.Iz])

    print('相対運動レンダリング')
    print(f"  モード: {args.mode}")
    print(f"  chief 位置: {chief_pos.tolist()}, クォータニオン: {chief_q.tolist()}")
    if args.mode == 'static':
        print(f"  rel_position: {rel_pos_init.tolist()}")
        print(f"  rel_quat:     {rel_q_init.tolist()}")
    elif args.mode == 'csv':
        print(f"  CSV: {args.rel_csv}  ({len(csv_states)} 行)")
    else:  # tumble
        print(f"  rel_position (固定): {rel_pos_init.tolist()}")
        print(f"  慣性テンソル: diag({args.Ix}, {args.Iy}, {args.Iz}) kg·m²")
        print(f"  初期角速度:   ({args.wx}, {args.wy}, {args.wz}) rad/s")
    span_s = dt * args.frames
    if args.duration_sec is not None:
        print(f"  時間軸: duration={args.duration_sec:.3f}s, dt={dt:.4f}s, start_t={start_t:.3f}s")
    else:
        print(f"  時間軸: fps={args.fps}, dt={dt:.4f}s, span={span_s:.3f}s, start_t={start_t:.3f}s")
    print(f"  フレーム数: {args.frames}")
    print(f"  サンプル数: {args.samples} spp")
    print(f"  出力: {output_dir}/")
    print()

    for frame in range(args.frames):
        t = start_t + frame * dt

        if args.mode == 'static':
            rel_pos = rel_pos_init
            rel_q = rel_q_init
        elif args.mode == 'csv':
            rel_pos, rel_q = interp_csv_state(csv_states, t, csv_label=args.rel_csv)
        else:  # tumble
            rel_pos = rel_pos_init
            rel_q = q_dep

        # deputy の慣性座標は: r_dep^I = r_chief^I + R(q_chief) · r_rel
        # （rel_pos は chief の Body / RTN 系成分、R で慣性へ持ち上げる）
        C_chief = q2dcm(chief_q, scalar=SCALAR)
        deputy_pos_inertial = chief_pos + C_chief @ rel_pos
        # 姿勢合成: q_dep^I = q_chief ⊗ q_rel
        # chief が恒等回転なら rel_q がそのまま deputy の慣性姿勢になる
        # （ショートカット: 厳密合成が必要なら yoshimulib.q_mult を使う）
        deputy_q_inertial = rel_q
        if not np.allclose(chief_q, [0.0, 0.0, 0.0, 1.0]):
            from yoshimulib.attitude.quaternion import q_mult
            deputy_q_inertial = q_mult(chief_q, rel_q, scalar=SCALAR)
            deputy_q_inertial = deputy_q_inertial / np.linalg.norm(deputy_q_inertial)

        deputy_xform = make_body_transform(deputy_pos_inertial, deputy_q_inertial)

        scene_dict = build_scene(
            chief_xform, deputy_xform,
            width=args.width, height=args.height, samples=args.samples,
            chief_model=args.chief_model, chief_parts=chief_parts,
            chief_default_bsdf=chief_default_bsdf, chief_scale=args.chief_scale,
            deputy_model=args.deputy_model, deputy_parts=deputy_parts,
            deputy_default_bsdf=deputy_default_bsdf, deputy_scale=args.deputy_scale,
            camera_origin=args.camera_origin, camera_target=args.camera_target,
            camera_fov=args.camera_fov,
            show_inertial_axes=args.show_inertial_axes,
            show_body_axes=args.show_body_axes,
        )
        scene = mi.load_dict(scene_dict)
        image = mi.render(scene)

        output_path = output_dir / f'frame_{frame:04d}.png'
        mi.util.write_bitmap(str(output_path), image)

        rel_dist = float(np.linalg.norm(rel_pos))
        print(f"  [{frame+1:3d}/{args.frames}] t={t:.3f}s  "
              f"|r_rel|={rel_dist:.3f}  "
              f"q_rel=[{rel_q[0]:.3f},{rel_q[1]:.3f},{rel_q[2]:.3f},{rel_q[3]:.3f}]  "
              f"→ {output_path}")

        # tumble モードのみ次フレームへ姿勢伝播
        if args.mode == 'tumble':
            q_dep, w_dep = propagate_attitude(q_dep, w_dep, I_body, I_inv, dt)

    print(f"\n完了！ 動画にするには:")
    print(f"  ffmpeg -framerate {int(args.fps)} -i {output_dir}/frame_%04d.png "
          f"-c:v libx264 -pix_fmt yuv420p output/relative_motion.mp4")


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    _run(args)


if __name__ == '__main__':
    main()
