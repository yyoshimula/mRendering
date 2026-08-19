#!/usr/bin/env python3
"""relative / rotation verb の argparse 定義（Mitsuba 非依存）。

`relative_motion.py` / `simple_rotation.py` はレンダラ本体なので import すると
Mitsuba が立ち上がる。GUI サーバー（`gui_server.py`）はフォームの初期値を
知りたいだけなので、パーサ定義だけをこの軽量モジュールに分離してある。

  - `relative_motion.build_parser` = `build_relative_parser`（再エクスポート）
  - `simple_rotation.build_parser` = `build_rotation_parser`（再エクスポート）
  - GUI の初期値は `parser_defaults(build_*_parser())` で自動導出する
    （ハードコードした辞書との乖離を構造的に無くすため）

このモジュールに import してよいのは標準ライブラリだけ。
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, Iterable

# GUI フォームには出さない dest。
#   config     : プリセット読込は GUI の別 UI が担当する
#   output_dir : ラン開始時に gui_server が決める
GUI_EXCLUDED_DESTS = ('config', 'output_dir')


def parser_defaults(parser: argparse.ArgumentParser,
                    exclude: Iterable[str] = GUI_EXCLUDED_DESTS) -> Dict[str, Any]:
    """パーサの既定値を dest → 値の辞書として取り出す。

    `parse_args([])` を通すので「引数を何も渡さずに verb を実行したときの値」と
    厳密に一致する。GUI のフォーム初期値はこれを唯一の真実として使う。
    """
    excluded = set(exclude or ())
    return {dest: value for dest, value in vars(parser.parse_args([])).items()
            if dest not in excluded}


def build_relative_parser() -> argparse.ArgumentParser:
    """relative verb（2 機の相対配置、軌道力学なし）のパーサ。"""
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
    parser.add_argument('--camera-up', nargs=3, type=float, default=[0.0, 1.0, 0.0],
                        help='カメラの up ベクトル [x y z]')

    # フォトリアリスティック宇宙環境（on-orbit servicing 撮像などで使用）
    parser.add_argument('--hide-chief', action='store_true', default=False,
                        help='chief 機体を描画しない（カメラ＝サービサ機体の機載カメラ視点にする場合）')
    parser.add_argument('--max-depth', type=int, default=8,
                        help='パストレーシングの最大バウンス数')
    parser.add_argument('--show-earth', action='store_true', default=False,
                        help='背景に地球（テクスチャ付き解析球）を描画する')
    parser.add_argument('--earth-direction', nargs=3, type=float, default=[0.0, 0.0, -1.0],
                        help='chief から見た地心方向の単位ベクトル（シーン座標）')
    parser.add_argument('--earth-altitude-km', type=float, default=400.0,
                        help='chief の高度 [km]（地球球体の距離を決める）')
    parser.add_argument('--earth-texture', type=str, default='earth_texture.jpg',
                        help='地球テクスチャ画像のパス')
    parser.add_argument('--earth-rotation-deg', type=float, default=0.0,
                        help='地球テクスチャの自転角オフセット [deg]')
    parser.add_argument('--sun-direction', nargs=3, type=float, default=[1.0, -1.0, -0.5],
                        help='太陽光の進行方向ベクトル（正規化不要）')
    parser.add_argument('--sun-irradiance', type=float, default=5.0,
                        help='太陽光の放射照度スケール（レンダリング単位）')
    parser.add_argument('--sun-temperature', type=float, default=None,
                        help='指定時は黒体放射色 [K] で太陽色を決める（例: 5778）')
    parser.add_argument('--env-brightness', type=float, default=None,
                        help='環境光の明るさ。starfield 指定時はそのスケールに使う'
                             '（未指定時は従来の淡い定数環境光）')
    parser.add_argument('--starfield', type=str, default=None,
                        help='星空 envmap (EXR) のパス（例: assets/starfield.exr）')

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


def build_rotation_parser() -> argparse.ArgumentParser:
    """rotation verb（単機タンブリング、軌道なし）のパーサ。"""
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
