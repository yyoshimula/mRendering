#!/usr/bin/env python3
"""
Mitsuba 3 基礎学習: オイラーの回転運動方程式に基づく衛星タンブリング

物理モデル:
  - オイラーの回転運動方程式 (主軸座標系):
        I·ω̇ = -ω × (I·ω) + τ            (τ=0: トルクフリー)
  - 姿勢キネマティクス (クォータニオン):
        q̇ = (1/2) Ω(ω) q                 ※スカラーラスト規約
  - 非対称慣性テンソル (Ix ≠ Iy ≠ Iz) により、中間軸まわりの不安定回転
    （Dzhanibekov 効果）など実装どおりの挙動を再現

座標系の規約:
  - Inertial : シーン固定の慣性座標系（カメラ・光源を配置）
  - Body     : 機体固定座標系。q により Inertial から回される
  - クォータニオン: スカラーラスト [qx, qy, qz, qw] (yoshimulib の SCALAR=4)
  - 角度は全てラジアン、角速度は rad/s、慣性モーメントは kg·m²

verb との関係:
  - mrender.py の rotation サブコマンドから呼ばれる（軌道なし、単機タンブリング）
  - relative_motion.py からも姿勢動力学を流用（同じ propagate_attitude）

yoshimulib 使用関数:
  - q_prop_mat: クォータニオン離散伝播行列 Φ(ω, dt)
  - q2dcm     : クォータニオン → 方向余弦行列 (DCM = body→inertial の回転行列)

使い方:
  # mrender 経由（推奨）
  python mrender.py rotation --config presets/akatsuki.yaml

  # スタンドアロン: YAMLプリセット
  python simple_rotation.py --config presets/akatsuki.yaml

  # プリセット + CLIオーバーライド（CLI引数 > YAML > デフォルト）
  python simple_rotation.py --config presets/akatsuki.yaml --frames 10 --samples 4

  # 複数YAML（後のファイルが優先）
  python simple_rotation.py --config presets/base.yaml presets/override.yaml

  # デフォルトプロシージャルモデル（config無し）
  python simple_rotation.py --frames 30

  # 外部OBJモデル + CLI指定
  python simple_rotation.py --model-path models/akatsuki.obj --model-scale 0.1 --frames 10
"""

import argparse
from pathlib import Path
import sys
from typing import List
import numpy as np
from scipy.integrate import solve_ivp
import yaml

# プロジェクトルートをパスに追加（yoshimulibのため）
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mitsuba as mi

from yoshimulib.attitude.kinematics import q_prop_mat
from yoshimulib.attitude.quaternion import q2dcm

mi.set_variant('llvm_ad_rgb')

# スカラー部の位置: 4 = scalar-last [qx, qy, qz, qw]
# (yoshimulib の規約: scalar=4 → 4 番目要素が w、scalar=1 → 先頭が w)
SCALAR = 4


def propagate_attitude(q: np.ndarray, w: np.ndarray, I_body: np.ndarray,
                       I_inv: np.ndarray, dt: float,
                       substeps: int = 10) -> tuple:
    """
    姿勢と角速度を dt 分だけ伝播（剛体姿勢動力学 + キネマティクス）

    動力学:
        I·ω̇ = -ω × (I·ω)              （オイラーの回転運動方程式、トルクフリー）
        → ω̇ = I⁻¹ · (-ω × (I·ω))     を scipy solve_ivp (RK45) で積分
    キネマティクス:
        q(t+h) = Φ(ω, h) · q(t)        （離散伝播行列 yoshimulib.q_prop_mat）
        |q| = 1 を毎ステップ再正規化（数値誤差の蓄積防止）

    Args:
        q: 現在のクォータニオン [qx,qy,qz,qw] (4,)  Body→Inertial 回転を表す
        w: 現在の角速度 [rad/s] (3,)               Body 座標系成分
        I_body: 慣性テンソル [kg·m²] (3,3)         Body 座標系（主軸なら対角）
        I_inv : 逆慣性テンソル [1/(kg·m²)] (3,3)
        dt: 全体の時間刻み [s] (通常 1/fps)
        substeps: クォータニオン伝播のサブステップ数（角速度の刻み）

    Returns:
        (q_next, w_next): 更新後の (クォータニオン, 角速度) ※Body 系
    """
    h = dt / substeps
    # solve_ivp に各サブステップ末尾の角速度を出力させる
    t_eval = [h * i for i in range(1, substeps + 1)]

    def euler_dynamics(t, w_vec):
        # オイラー方程式: ω̇ = -I⁻¹ · (ω × I·ω)  （トルクフリー）
        return I_inv @ (-np.cross(w_vec, I_body @ w_vec))

    sol = solve_ivp(euler_dynamics, [0, dt], w, method='RK45',
                    t_eval=t_eval, rtol=1e-10, atol=1e-12)

    # 各サブステップでクォータニオン伝播（yoshimulib の離散伝播行列）
    # ω が一定な微小区間 h で q(t+h) = Φ(ω,h)·q(t) を解析的に適用する
    for i in range(substeps):
        w_i = sol.y[:, i]
        Phi = q_prop_mat(SCALAR, h, w_i.reshape(1, 3))
        q = Phi @ q
        # 正規化（数値誤差の蓄積防止: 単位クォータニオン |q|=1 を維持）
        q = q / np.linalg.norm(q)

    return q, sol.y[:, -1]


def dcm_to_mitsuba_transform(C: np.ndarray) -> mi.ScalarTransform4f:
    """DCM (3x3) → Mitsuba ScalarTransform4f (4x4 同次変換)

    Mitsuba の Transform4f は 4x4 同次行列で、上 3x3 が回転、最右列が並進。
    列ベクトル右掛け規約: world = M · local  なので、本体ローカル座標を
    Inertial へ持ち上げる回転行列 (DCM) をそのまま左上 3x3 に入れる。
    並進は 0（原点に置く）。
    """
    m = np.eye(4)
    m[:3, :3] = C
    return mi.ScalarTransform4f(m)


def create_axes(length: float = 1.5, radius: float = 0.015,
                prefix: str = 'inertial',
                transform: mi.ScalarTransform4f = None,
                colors=None, glow: float = 0.5) -> dict:
    """座標軸を描画（cylinder + 先端 sphere）

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
    """
    原点に衛星モデルを作成（DCMベースのTransformで姿勢を適用）

    各形状の to_world は body_transform @ (ローカル変換) の合成。
    Mitsuba の `@` は左から world ← local の順に積まれる
    （world = body_transform · local_scale · local_pos）。

    Args:
        body_transform: 機体座標系 → 慣性座標系の Transform4f（4x4）
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


def load_yaml_config(paths: List[str]) -> dict:
    """YAMLプリセットファイルを読み込んでフラット辞書に変換

    ネストされたセクション（model:, dynamics: 等）を 1 段だけ展開し、
    argparse の dest 名（アンダースコア区切り）に統一する。
    複数ファイル指定時は後のファイルが優先（dict の上書きマージ）。

    優先順位: CLI 引数 > YAML > argparse デフォルト
    （parse_args の中で set_defaults を経由するため、CLI で明示した値が常に勝つ）
    """
    merged = {}
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


def split_obj_by_parts(obj_path: str) -> dict:
    """OBJ ファイルを 'o' ディレクティブでパーツ分割し、一時ファイルに書き出す

    Mitsuba は OBJ メッシュ単位で BSDF を割り当てるため、パーツ別に異なる材質
    を当てたい場合は事前にパーツごとの一時 OBJ を作る必要がある。

    Args:
        obj_path: OBJファイルパス

    Returns:
        {'part_name': '/tmp/xxx_part_name.obj', ...}
        'o' ディレクティブが無い OBJ は {'default': obj_path} を返す
    """
    import tempfile

    with open(obj_path) as f:
        lines = f.readlines()

    # グローバルヘッダ（v, vn, vt, mtllib等）とパーツ別フェイス行を分離
    header_lines = []  # v, vn, vt, mtllib 等
    parts = {}  # {name: [face_lines]}
    current_part = None

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
                if 'default' not in parts:
                    parts['default'] = []
                parts['default'].append(line)

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


def load_model_with_parts(obj_path: str, part_bsdfs: dict,
                          default_bsdf: dict,
                          body_transform: mi.ScalarTransform4f,
                          scale: float = 1.0) -> dict:
    """OBJ ファイルをパーツ分割して読み込み、各パーツに BSDF を適用

    YAML では `model_parts` に {part_name: bsdf_dict} を書いておけば
    部位ごとに材質を変えられる（例: panel→solar_panel, body→aluminum_brushed）。

    Args:
        obj_path: OBJ ファイルパス
        part_bsdfs: {'part_name': bsdf_dict, ...} パーツ別 BSDF
        default_bsdf: 未指定パーツに適用するデフォルト BSDF
        body_transform: 姿勢変換 (機体→慣性、Mitsuba ScalarTransform4f)
        scale: 全体スケール（Mitsuba シーン単位で表示するための倍率）
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
        objects[f'part_{part_name}'] = {
            'type': 'obj',
            'filename': part_path,
            'to_world': transform,
            'bsdf': bsdf,
        }

    return objects


def build_scene(body_transform: mi.ScalarTransform4f,
                width: int, height: int, samples: int,
                model_path: str = None, part_bsdfs: dict = None,
                default_bsdf: dict = None, model_scale: float = 1.0,
                camera_origin=None, camera_target=None, camera_fov: float = 40) -> dict:
    """Mitsuba シーン辞書を組み立てる

    構成: integrator (path tracer) + perspective camera + directional sun +
          constant envmap (微弱な背景) + 慣性軸 + 機体軸 + 衛星本体。
    body_transform を含む全ての形状はこの 1 関数でフレームごとに再構築される。
    """
    if camera_origin is None:
        camera_origin = [3, 2, 2]
    if camera_target is None:
        camera_target = [0, 0, 0]
    scene_dict = {
        'type': 'scene',
        'integrator': {
            'type': 'path',
            'max_depth': 8,
        },
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
            'sampler': {
                'type': 'independent',
                'sample_count': samples,
            },
        },
        'sun': {
            'type': 'directional',
            'direction': [1, -1, -0.5],
            'irradiance': {
                'type': 'rgb',
                'value': [5, 5, 4.8],
            },
        },
        'envmap': {
            'type': 'constant',
            'radiance': {
                'type': 'rgb',
                'value': [0.02, 0.02, 0.03],
            },
        },
    }

    # 慣性座標系（固定）
    scene_dict.update(create_axes(prefix='inertial', glow=0.5))

    # 機体固定座標系（姿勢に追従）
    scene_dict.update(create_axes(
        length=0.8, radius=0.02, prefix='body',
        transform=body_transform,
        colors=[
            [1.0, 0.6, 0.0],  # x_b: オレンジ
            [1.0, 1.0, 0.0],  # y_b: 黄
            [0.8, 0.0, 1.0],  # z_b: 紫
        ],
        glow=0.8,
    ))

    # 衛星
    if model_path:
        scene_dict.update(load_model_with_parts(
            model_path, part_bsdfs or {}, default_bsdf or {},
            body_transform, model_scale))
    else:
        scene_dict.update(create_satellite(body_transform))

    return scene_dict


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='オイラー回転運動方程式による衛星タンブリング',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
例:
  # mrender 経由（推奨、runs/ に自動配置）
  python mrender.py rotation --config presets/akatsuki.yaml

  # スタンドアロン: YAMLプリセット
  python simple_rotation.py --config presets/akatsuki.yaml

  # プリセット + 個別オーバーライド
  python simple_rotation.py --config presets/akatsuki.yaml --frames 10 --samples 4

  # デフォルトプロシージャルモデル
  python simple_rotation.py --frames 30
        """
    )
    parser.add_argument('--config', nargs='+', default=[],
                        help='YAMLプリセットファイル（複数指定可、後のファイルが優先）')
    # レンダリング
    parser.add_argument('--frames', type=int, default=30,
                        help='フレーム数（デフォルト: 30）')
    parser.add_argument('--fps', type=float, default=30.0,
                        help='フレームレート [fps]（デフォルト: 30）')
    parser.add_argument('--samples', type=int, default=256,
                        help='サンプル数/ピクセル（デフォルト: 256）')
    parser.add_argument('--width', type=int, default=640,
                        help='画像幅（デフォルト: 640）')
    parser.add_argument('--height', type=int, default=480,
                        help='画像高さ（デフォルト: 480）')
    # 慣性テンソル主軸値 [kg·m²]（非対称→タンブリング）
    parser.add_argument('--Ix', type=float, default=4.0,
                        help='慣性モーメント Ix [kg·m²]（デフォルト: 4.0）')
    parser.add_argument('--Iy', type=float, default=2.0,
                        help='慣性モーメント Iy [kg·m²]（デフォルト: 2.0）')
    parser.add_argument('--Iz', type=float, default=1.0,
                        help='慣性モーメント Iz [kg·m²]（デフォルト: 1.0）')
    # 初期角速度 [rad/s]
    parser.add_argument('--wx', type=float, default=0.3,
                        help='初期角速度 ωx [rad/s]（デフォルト: 0.3）')
    parser.add_argument('--wy', type=float, default=0.1,
                        help='初期角速度 ωy [rad/s]（デフォルト: 0.1）')
    parser.add_argument('--wz', type=float, default=2.0,
                        help='初期角速度 ωz [rad/s]（デフォルト: 2.0）')
    # モデル
    parser.add_argument('--model-path', type=str, default=None,
                        help='OBJモデルファイルパス')
    parser.add_argument('--model-scale', type=float, default=1.0,
                        help='モデルスケール（デフォルト: 1.0）')
    # カメラ
    parser.add_argument('--camera-origin', nargs=3, type=float,
                        default=[3, 2, 2], help='カメラ位置 [x y z]')
    parser.add_argument('--camera-target', nargs=3, type=float,
                        default=[0, 0, 0], help='カメラ注視点 [x y z]')
    parser.add_argument('--camera-fov', type=float, default=40,
                        help='視野角 [deg]（デフォルト: 40）')
    parser.add_argument('--output-dir', type=str, default=None,
                        help='出力ディレクトリ（未指定時は output/simple）')

    return parser


def parse_args(argv=None) -> argparse.Namespace:
    """CLI と YAML をマージ（CLI > YAML > argparseデフォルト）。

    手順: ① まず CLI を parse_known_args して --config の有無だけ拾う
          ② --config があれば YAML を読んで set_defaults でデフォルト上書き
          ③ もう一度 parse_args してマージ結果を確定させる
    """
    parser = build_parser()
    args_pre, _ = parser.parse_known_args(argv)
    if args_pre.config:
        parser.set_defaults(**load_yaml_config(args_pre.config))
    return parser.parse_args(argv)


def _run(args: argparse.Namespace) -> None:
    """argparse Namespace を受け取り、シーンを連番でレンダする。

    各フレームで:
      ① クォータニオンから DCM を生成 (q2dcm)
      ② DCM から Mitsuba Transform4f を作成
      ③ シーン辞書を再構築 → mi.render() → PNG 保存
      ④ 回転エネルギー T と角運動量 |L| をログ出力（保存量チェック）
      ⑤ propagate_attitude で次フレームへ姿勢を進める
    """
    output_dir = Path(args.output_dir or 'output/simple')
    output_dir.mkdir(parents=True, exist_ok=True)

    # 慣性テンソル（主軸座標系のため対角）
    I_body = np.diag([args.Ix, args.Iy, args.Iz])
    I_inv = np.diag([1.0 / args.Ix, 1.0 / args.Iy, 1.0 / args.Iz])

    # 初期状態
    q = np.array([0.0, 0.0, 0.0, 1.0])  # 単位クォータニオン（恒等回転、scalar-last 規約）
    w = np.array([args.wx, args.wy, args.wz])  # 初期角速度 [rad/s]（Body 系）

    dt = 1.0 / args.fps  # フレーム間の物理時間 [s]（実時間 1:1 でアニメ化）

    # 保存量（トルクフリーなら厳密に保存）
    # 回転運動エネルギー T = (1/2) ω·I·ω
    # 角運動量ノルム  |L| = |I·ω|（Body 系で測ると剛体回転で常に一定）
    T0 = 0.5 * w @ I_body @ w
    L0 = np.linalg.norm(I_body @ w)

    print("オイラー回転運動方程式シミュレーション")
    print(f"  慣性テンソル: diag({args.Ix}, {args.Iy}, {args.Iz}) kg·m²")
    print(f"  初期角速度: ({args.wx}, {args.wy}, {args.wz}) rad/s")
    print(f"  回転エネルギー: {T0:.4f} J")
    print(f"  角運動量ノルム: {L0:.4f} kg·m²/s")
    print(f"  フレーム数: {args.frames}, dt={dt:.4f}s")
    print(f"  サンプル数: {args.samples} spp")
    print(f"  出力: {output_dir}/")
    print()

    for frame in range(args.frames):
        # クォータニオン → DCM (3x3, body→inertial 回転)（yoshimulib）
        C = q2dcm(q, scalar=SCALAR)  # 3x3
        # DCM を Mitsuba 4x4 同次行列に詰める（並進ゼロ、原点に固定）
        body_transform = dcm_to_mitsuba_transform(C)

        # シーン構築・レンダリング
        scene_dict = build_scene(body_transform, args.width, args.height, args.samples,
                                 model_path=args.model_path,
                                 part_bsdfs=getattr(args, 'model_parts', None),
                                 default_bsdf=getattr(args, 'model_bsdf', None),
                                 model_scale=args.model_scale,
                                 camera_origin=args.camera_origin,
                                 camera_target=args.camera_target,
                                 camera_fov=args.camera_fov)
        scene = mi.load_dict(scene_dict)
        image = mi.render(scene)

        output_path = output_dir / f'frame_{frame:04d}.png'
        mi.util.write_bitmap(str(output_path), image)

        # 保存量チェック
        T = 0.5 * w @ I_body @ w
        L = np.linalg.norm(I_body @ w)
        print(f"  [{frame+1:3d}/{args.frames}] "
              f"|ω|={np.linalg.norm(w):.3f} rad/s  "
              f"T={T:.4f} J (err={abs(T-T0)/T0:.1e})  "
              f"|L|={L:.4f} (err={abs(L-L0)/L0:.1e})  "
              f"→ {output_path}")

        # 姿勢伝播（次フレームへ）
        q, w = propagate_attitude(q, w, I_body, I_inv, dt)

    print(f"\n完了！ 動画にするには:")
    print(f"  ffmpeg -framerate {int(args.fps)} -i {output_dir}/frame_%04d.png "
          f"-c:v libx264 -pix_fmt yuv420p output/simple_rotation.mp4")


def main(argv=None) -> None:
    args = parse_args(argv)
    _run(args)


if __name__ == '__main__':
    main()
