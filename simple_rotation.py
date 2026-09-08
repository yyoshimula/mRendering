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
  - **姿勢クォータニオンの向き: q は inertial → body**（航空宇宙標準）。
    yoshimulib の `q2dcm(q, scalar=4)` はそのまま慣性→機体の姿勢行列 A(q)
    を返す（数値検証済み: q2dcm(+90°z) @ [1,0,0] = [0,-1,0]）。
    Mitsuba の `to_world` に必要なのは body → world なので、
    シーン構築では常に **A(q).T** を使う（dcm_to_mitsuba_transform 参照）。
  - 角度は全てラジアン、角速度は rad/s、慣性モーメントは kg·m²

verb との関係:
  - mrender.py の rotation サブコマンドから呼ばれる（軌道なし、単機タンブリング）
  - relative_motion.py とは姿勢動力学・シーン構成要素を共有する
    （唯一の実装は scene_common.py、本ファイルは再エクスポートするだけ）

モジュール分割:
  - scene_common.py   : propagate_attitude / create_axes / create_satellite /
                        split_obj_by_parts / load_model_with_parts（Mitsuba 依存）
  - verb_parsers.py   : build_parser（Mitsuba 非依存、GUI が既定値の導出に使う）
  - config_loader.py  : load_yaml_config（1 段フラット化、Mitsuba 非依存）

yoshimulib 使用関数:
  - q_kine   : クォータニオン微分（scene_common で角速度と連立積分）
  - q2dcm     : クォータニオン → 姿勢行列 A(q)（= inertial→body。
                body→inertial が要るときは転置する）

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
import numpy as np

# プロジェクトルートをパスに追加（yoshimulibのため）
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mitsuba as mi

from yoshimulib.attitude.quaternion import q2dcm

from mi_variant import init_variant
init_variant(mi)  # MRENDER_VARIANT 環境変数でバリアント切替（mi_variant.py 参照）

# 姿勢動力学・シーン構成要素は relative_motion.py と共有（scene_common.py が
# 唯一の実装）。ここでは後方互換のため同名で再エクスポートしている
# （live_worker の `SR.propagate_attitude` などの参照経路を壊さないため）。
from config_loader import load_flat_yaml_config as load_yaml_config  # noqa: E402
from scene_common import (  # noqa: E402
    SCALAR,
    create_axes,
    create_satellite,
    inertia_matrices,
    load_model_with_parts,
    propagate_attitude,
    propagate_trajectory,
    split_obj_by_parts,
)
from verb_parsers import build_rotation_parser as build_parser  # noqa: E402


def dcm_to_mitsuba_transform(C: np.ndarray) -> mi.ScalarTransform4f:
    """DCM (3x3) → Mitsuba ScalarTransform4f (4x4 同次変換)

    Mitsuba の Transform4f は 4x4 同次行列で、上 3x3 が回転、最右列が並進。
    列ベクトル右掛け規約: world = M · local。

    Args:
        C: **body → inertial(world)** の回転行列。
           姿勢クォータニオン q は inertial → body 規約なので、
           `q2dcm(q, scalar=SCALAR)` が返すのは A(q) = inertial → body。
           呼び出し側で **A(q).T** を渡すこと（転置し忘れると描画姿勢が
           真値の逆回転になる）。

    並進は 0（原点に置く）。
    """
    m = np.eye(4)
    m[:3, :3] = C
    return mi.ScalarTransform4f(m)


def quat_to_body_transform(q: np.ndarray) -> mi.ScalarTransform4f:
    """姿勢クォータニオン (inertial→body) → Mitsuba to_world (body→inertial)。

    転置の一段（A(q)ᵀ）をここに閉じ込める。バッチ (_run) と GUI ライブ
    プレビュー (live_worker.build_rotation_frame) の両方がこれを呼ぶことで、
    規約の実装が 1 箇所になり両者が乖離しない。
    """
    return dcm_to_mitsuba_transform(q2dcm(q, scalar=SCALAR).T)


# create_axes / create_satellite / split_obj_by_parts / load_model_with_parts /
# load_yaml_config / build_parser は共有モジュールへ移動済み
# （scene_common.py / config_loader.py / verb_parsers.py、冒頭で再エクスポート）。


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
            # Mitsuba 既定の near_clip=0.01 は、小さめのモデル（model_scale が
            # 小さい場合や近接カメラ）で手前側を切り落とす。十分小さい値に広げる。
            'near_clip': 1e-3,
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
      ① クォータニオン (inertial→body) から姿勢行列 A(q) を生成 (q2dcm) し、
         body→inertial の A(q)ᵀ に転置する
      ② DCM から Mitsuba Transform4f を作成
      ③ シーン辞書を再構築 → mi.render() → PNG 保存
      ④ 回転エネルギー T と角運動量 |L| をログ出力（保存量チェック）
      ⑤ propagate_attitude で次フレームへ姿勢を進める
    """
    output_dir = Path(args.output_dir or 'output/simple')
    output_dir.mkdir(parents=True, exist_ok=True)

    # 慣性テンソル（主軸座標系のため対角）
    I_body, I_inv = inertia_matrices(args.Ix, args.Iy, args.Iz)

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
        # q (inertial→body) → to_world (body→inertial = A(q)ᵀ)。
        # 転置の実装は quat_to_body_transform に一元化（live_worker と共有）。
        body_transform = quat_to_body_transform(q)

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
