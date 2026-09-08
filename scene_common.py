#!/usr/bin/env python3
"""relative / rotation verb が共有する姿勢動力学とシーン構成要素。

`relative_motion.py` と `simple_rotation.py` は「軌道力学を使わない」姉妹
スクリプトで、姿勢伝播・座標軸・プロシージャル衛星・OBJ パーツ分割の実装が
逐語コピーで並存していた。このモジュールがその唯一の実装で、両者は
再エクスポート（`from scene_common import ...`）するだけになっている。

座標系・量の規約（両 verb 共通）:
  - クォータニオン: スカラーラスト [qx, qy, qz, qw]（yoshimulib の SCALAR=4）
  - **姿勢クォータニオン q は inertial → body**（航空宇宙標準）。
    `q2dcm(q, scalar=4)` はそのまま慣性→機体の姿勢行列 A(q) を返すので、
    Mitsuba の `to_world`（body → world）には常に **A(q).T** を使う。
  - 角度はラジアン、角速度 rad/s、慣性モーメント kg·m²

Mitsuba に依存する（`import mitsuba` する）ので、GUI サーバーからは import
しないこと。argparse 定義だけが要るなら `verb_parsers.py`、YAML の 1 段
フラット化だけが要るなら `config_loader.load_flat_yaml_config` を使う。
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.integrate import solve_ivp

import mitsuba as mi

from yoshimulib.attitude.kinematics import q_prop_mat

# 単体 import されたときのために変換バリアントを保証する
# （relative_motion / simple_rotation 経由なら既に設定済みなので no-op）。
from mi_variant import init_variant
init_variant(mi)

# スカラー部の位置: 4 = scalar-last [qx, qy, qz, qw]
# (yoshimulib の規約: scalar=4 → 4 番目要素が w、scalar=1 → 先頭が w)
SCALAR = 4


# ---------------------------------------------------------------------------
# 姿勢動力学
# ---------------------------------------------------------------------------
def inertia_matrices(Ix: float, Iy: float, Iz: float) -> Tuple[np.ndarray, np.ndarray]:
    """主軸慣性モーメントから (I, I⁻¹) を作る。

    逆行列は `np.linalg.inv` ではなく成分ごとの逆数の対角行列として作る。
    数値的に等価だが最下位ビットが揺れうるため、バッチ / ライブ両経路で
    同じ式を使い、レンダ結果のビット一致を壊さないようにしている。
    """
    I_body = np.diag([Ix, Iy, Iz])
    I_inv = np.diag([1.0 / Ix, 1.0 / Iy, 1.0 / Iz])
    return I_body, I_inv


def propagate_attitude(q: np.ndarray, w: np.ndarray, I_body: np.ndarray,
                       I_inv: np.ndarray, dt: float,
                       substeps: int = 10) -> tuple:
    """姿勢と角速度を dt 分だけ伝播（剛体姿勢動力学 + キネマティクス）。

    動力学:
        I·ω̇ = -ω × (I·ω)              （オイラーの回転運動方程式、トルクフリー）
        → ω̇ = I⁻¹ · (-ω × (I·ω))     を scipy solve_ivp (RK45) で積分
    キネマティクス:
        q(t+h) = Φ(ω, h) · q(t)        （離散伝播行列 yoshimulib.q_prop_mat）
        |q| = 1 を毎ステップ再正規化（数値誤差の蓄積防止）

    Args:
        q: 現在のクォータニオン [qx,qy,qz,qw] (4,)  inertial → body の姿勢
        w: 現在の角速度 [rad/s] (3,)               Body 座標系成分
        I_body: 慣性テンソル [kg·m²] (3,3)         Body 座標系（主軸なら対角）
        I_inv : 逆慣性テンソル [1/(kg·m²)] (3,3)
        dt: 全体の時間刻み [s]（通常 1/fps）
        substeps: クォータニオン伝播のサブステップ数（角速度の刻み）

    Returns:
        (q_next, w_next): 更新後の (クォータニオン, 角速度) ※Body 系
    """
    h = dt / substeps
    # solve_ivp に各サブステップ末尾の角速度を出力させる。
    # 最終点は h*substeps ではなく dt そのものにする（浮動小数で
    # (dt/substeps)*substeps が dt を 1 ULP 超えると solve_ivp の
    # 「t_eval が t_span 外」エラーになる。dt=5553.6/180 等で実際に発現）
    t_eval = [h * i for i in range(1, substeps)] + [dt]

    def euler_dynamics(t, w_vec):
        # オイラー方程式: ω̇ = -I⁻¹ · (ω × I·ω)  （トルクフリー）
        return I_inv @ (-np.cross(w_vec, I_body @ w_vec))

    sol = solve_ivp(euler_dynamics, [0, dt], w, method='RK45',
                    t_eval=t_eval, rtol=1e-10, atol=1e-12)

    # 各サブステップでクォータニオン伝播（yoshimulib の離散伝播行列）
    # ω が一定な微小区間 h で q(t+h) = Φ(ω,h)·q(t) を解析的に適用する
    for i in range(substeps):
        w_i = sol.y[:, i]
        # ω ≈ 0 のサブステップは回転なし。q_prop_mat は ψ = (ω/|ω|)·sin(|ω|h/2)
        # で回転軸を正規化するため |ω| = 0 だと 0/0 → NaN が q に伝播する。
        if np.linalg.norm(w_i) < 1e-12:
            continue
        Phi = q_prop_mat(SCALAR, h, w_i.reshape(1, 3))
        q = Phi @ q
        # 正規化（数値誤差の蓄積防止: 単位クォータニオン |q|=1 を維持）
        q = q / np.linalg.norm(q)

    return q, sol.y[:, -1]


def propagate_trajectory(q0: np.ndarray, w0: np.ndarray, I_body: np.ndarray,
                         I_inv: np.ndarray, dt: float,
                         n_frames: int) -> List[np.ndarray]:
    """フレーム 0..n-1 の姿勢クォータニオン列を一括で作る。

    `_run()` のフレームループが「描画 → propagate_attitude」を繰り返すのと
    同じ順序・同じ刻みなので、traj[k] はバッチのフレーム k の姿勢と一致する。
    ライブプレビュー（live_worker）が任意フレームへ O(1) で飛ぶために使う。

    Returns:
        長さ n_frames のリスト（先頭は q0 のコピー）。
    """
    q = np.asarray(q0, dtype=float).copy()
    w = np.asarray(w0, dtype=float).copy()
    traj = [q.copy()]
    for _ in range(max(0, n_frames - 1)):
        q, w = propagate_attitude(q, w, I_body, I_inv, dt)
        traj.append(q.copy())
    return traj


# ---------------------------------------------------------------------------
# シーン構成要素
# ---------------------------------------------------------------------------
def create_axes(length: float = 1.5, radius: float = 0.015,
                prefix: str = 'inertial',
                transform: mi.ScalarTransform4f = None,
                colors=None, glow: float = 0.5) -> dict:
    """座標軸を描画（cylinder + 先端 sphere）。

    transform=None なら Inertial 軸（原点固定）。transform に body_transform
    を渡すと機体軸（姿勢に追従）として描画される。可視性向上のため軸自体に
    弱い area emitter を付けている (glow > 0)。
    """
    if transform is None:
        transform = mi.ScalarTransform4f()
    if colors is None:
        colors = [
            [1.0, 0.2, 0.2],  # X軸: 赤
            [0.2, 1.0, 0.2],  # Y軸: 緑
            [0.2, 0.2, 1.0],  # Z軸: 青
        ]

    objects = {}
    directions = [
        ('x', [1, 0, 0]),
        ('y', [0, 1, 0]),
        ('z', [0, 0, 1]),
    ]

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

        shaft_dict = {
            'type': 'cylinder',
            'to_world': transform @ axis_rot @ mi.ScalarTransform4f.scale([radius, radius, length]),
            'bsdf': {
                'type': 'diffuse',
                'reflectance': {'type': 'rgb', 'value': color},
            },
        }
        if glow > 0:
            shaft_dict['emitter'] = {
                'type': 'area',
                'radiance': {'type': 'rgb', 'value': [c * glow for c in color]},
            }
        objects[f'{prefix}_axis_{name}'] = shaft_dict

        tip_local = d * length
        tip_transform = transform @ mi.ScalarTransform4f.translate(tip_local.tolist())
        tip_dict = {
            'type': 'sphere',
            'to_world': tip_transform @ mi.ScalarTransform4f.scale([radius * 2.5] * 3),
            'bsdf': {
                'type': 'diffuse',
                'reflectance': {'type': 'rgb', 'value': color},
            },
        }
        if glow > 0:
            tip_dict['emitter'] = {
                'type': 'area',
                'radiance': {'type': 'rgb', 'value': [c * glow for c in color]},
            }
        objects[f'{prefix}_axis_{name}_tip'] = tip_dict

    return objects


def create_satellite(body_transform: mi.ScalarTransform4f) -> dict:
    """簡易プロシージャル衛星モデル（本体・パネル・アンテナ）。

    各形状の to_world は body_transform @ (ローカル変換) の合成。
    Mitsuba の `@` は左から world ← local の順に積まれる
    （world = body_transform · local_pos · local_scale）。

    Args:
        body_transform: 機体座標系 → 慣性座標系の Transform4f（4x4）

    Returns:
        キーにプレフィックスは付かない（`body` / `panel_left` / ...）。
        2 機を同一シーンに置く relative 側は呼び出し元で付け替える。
    """
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
        'bsdf': {
            'type': 'conductor',
            'material': 'Au',
        }
    }

    return objects


# ---------------------------------------------------------------------------
# OBJ パーツ分割（(path, mtime) メモ化つき）
# ---------------------------------------------------------------------------
# 分割結果は入力 OBJ だけで決まるので、同じファイル・同じ mtime なら一時
# ディレクトリを作り直す必要がない。キャッシュが無かった頃はフレームごとに
# `mrender_parts_*` の tmpdir が増え続けていた（120 フレームで 120 個）。
_SPLIT_CACHE: Dict[Tuple[str, float], Dict[str, str]] = {}
_SPLIT_LOCK = threading.Lock()


def _split_obj_by_parts_uncached(obj_path: str) -> dict:
    """OBJ を 'o' ディレクティブで分割し、パーツごとの一時 OBJ を書き出す。"""
    with open(obj_path) as f:
        lines = f.readlines()

    # グローバルヘッダ（v, vn, vt, mtllib等）とパーツ別フェイス行を分離
    header_lines: List[str] = []      # v, vn, vt, mtllib 等
    parts: Dict[str, List[str]] = {}  # {name: [face_lines]}
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
                # o ディレクティブ前のフェイス → 'default' パーツ
                parts.setdefault('default', []).append(line)

    if not parts:
        # o ディレクティブなし → 全体を1パーツとして扱う
        return {'default': obj_path}

    # 各パーツを一時OBJファイルに書き出し
    tmp_dir = tempfile.mkdtemp(prefix='mrender_parts_')
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


def split_obj_by_parts(obj_path: str) -> dict:
    """OBJ ファイルを 'o' ディレクティブでパーツ分割し、一時ファイルへ書き出す。

    Mitsuba は OBJ メッシュ単位で BSDF を割り当てるため、パーツ別に異なる材質
    を当てたい場合は事前にパーツごとの一時 OBJ を作る必要がある。

    結果は (パス, mtime) でメモ化する。キャッシュヒット時は一時ファイルを
    作り直さないので、フレームループでも tmpdir は 1 モデルにつき 1 個しか
    増えない。生成物が外部から消されていた場合だけ作り直す。

    Args:
        obj_path: OBJファイルパス

    Returns:
        {'part_name': '/tmp/xxx/part_name.obj', ...}
        'o' ディレクティブが無い OBJ は {'default': obj_path} を返す
    """
    try:
        mtime = os.path.getmtime(obj_path)
    except OSError:
        mtime = 0.0
    key = (str(obj_path), mtime)
    with _SPLIT_LOCK:
        hit = _SPLIT_CACHE.get(key)
    if hit is not None and all(os.path.exists(p) for p in hit.values()):
        return hit

    result = _split_obj_by_parts_uncached(obj_path)
    with _SPLIT_LOCK:
        _SPLIT_CACHE[key] = result
    return result


def load_model_with_parts(obj_path: str, part_bsdfs: dict, default_bsdf: dict,
                          body_transform: mi.ScalarTransform4f, scale: float = 1.0,
                          prefix: str = '') -> dict:
    """OBJ ファイルをパーツ分割して読み込み、各パーツに BSDF を適用する。

    YAML では `model_parts` / `deputy_parts` 等に {part_name: bsdf_dict} を
    書いておけば部位ごとに材質を変えられる
    （例: panel→solar_panel, body→aluminum_brushed）。

    Args:
        obj_path: OBJ ファイルパス
        part_bsdfs: {'part_name': bsdf_dict, ...} パーツ別 BSDF
        default_bsdf: 未指定パーツに適用するデフォルト BSDF
        body_transform: 姿勢変換（機体→慣性、Mitsuba ScalarTransform4f）
        scale: 全体スケール（Mitsuba シーン単位で表示するための倍率）
        prefix: シーン辞書キーの接頭辞。空文字なら `part_{name}`（rotation の
            従来キー名）、指定時は `{prefix}_part_{name}`（relative は 2 機を
            同一シーンに置くのでキー衝突を避けるため必須）。
    """
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
        key = f'{prefix}_part_{part_name}' if prefix else f'part_{part_name}'
        objects[key] = {
            'type': 'obj',
            'filename': part_path,
            'to_world': transform,
            'bsdf': bsdf,
        }

    return objects
