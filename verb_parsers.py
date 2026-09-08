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

    # 高速化
    parser.add_argument('--jobs', type=int, default=1,
                        help='フレーム範囲を N 分割してサブプロセス並列レンダ。'
                             'GPU バリアントではフレーム準備（CPU）が支配的なので'
                             'ほぼ線形にスケールする（多フレーム × GPU 向け。'
                             '少フレームでは各プロセスの起動 ~10s が勝って逆効果）。'
                             '出力は直列実行と ±1LSB（Mitsuba 固有の揺らぎ）以内で一致')
    parser.add_argument('--no-persistent-scene', dest='persistent_scene',
                        action='store_false', default=True,
                        help='永続シーン（mi.load_dict を毎フレームやり直さず差分'
                             'パラメータ更新でレンダ）を無効化して従来動作へ。'
                             '永続シーンは作り直し比 ±数 LSB の経路差が衛星の'
                             '鏡面部に出うる（統計的品質は同一・検証済み）')
    parser.add_argument('--frame-start', type=int, default=None,
                        help='このフレーム番号からレンダ（--jobs の内部用。'
                             '時刻・番号付けはグローバル基準のまま部分レンダする）')
    parser.add_argument('--frame-end', type=int, default=None,
                        help='このフレーム番号の手前までレンダ（--jobs の内部用）')

    # モード
    parser.add_argument('--mode', choices=['static', 'csv', 'tumble', 'absolute'],
                        default='static',
                        help='相対状態の与え方（absolute = chief/deputy の絶対軌道を'
                             '伝播して相対状態と環境（太陽方向・食・地球姿勢）を導出）')

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

    # absolute モード（絶対軌道 2 本 → 相対状態 + 時変環境）
    # シーン座標 = chief 中心の RTN(LVLH) frame [km]（x=R, y=T, z=N）。
    # 軌道要素の角度は YAML で書きやすいよう deg（CSV スキーマは従来どおり rad）。
    parser.add_argument('--epoch-utc', type=str, default='2026-01-01T00:00:00',
                        help='absolute モードの t=0 の UTC エポック（ISO 8601）。'
                             '太陽位置（VSOP87）と GMST（地球姿勢）の基準')
    parser.add_argument('--chief-oe', nargs=6, type=float,
                        default=[6778.137, 0.001, 51.6, 0.0, 0.0, 0.0],
                        metavar=('A_KM', 'E', 'INC_DEG', 'RAAN_DEG', 'ARGP_DEG', 'M0_DEG'),
                        help='chief のケプラー要素 [a_km, e, i_deg, Ω_deg, ω_deg, M0_deg]')
    parser.add_argument('--deputy-oe', nargs=6, type=float, default=None,
                        metavar=('A_KM', 'E', 'INC_DEG', 'RAAN_DEG', 'ARGP_DEG', 'M0_DEG'),
                        help='deputy のケプラー要素（absolute モードで CSV 未指定なら必須）')
    parser.add_argument('--chief-orbit-csv', type=str, default=None,
                        help='chief の絶対軌道 CSV（CsvEphemeris スキーマ: time_s, a_km, e, '
                             'inc_rad, raan_rad, ome_rad, f_rad [, q1..q4]）。指定時 --chief-oe より優先')
    parser.add_argument('--deputy-orbit-csv', type=str, default=None,
                        help='deputy の絶対軌道 CSV（同スキーマ）。指定時 --deputy-oe より優先')
    parser.add_argument('--deputy-attitude', choices=['tumble', 'lvlh', 'csv'],
                        default='tumble',
                        help='absolute モードの deputy 姿勢: tumble=慣性系トルクフリー伝播'
                             '（rel_quat は t=0 の RTN 相対初期姿勢、ω は wx/wy/wz）/ '
                             'lvlh=deputy 自身の RTN に rel_quat を固定オフセット / '
                             'csv=--deputy-orbit-csv の q1..q4 列（ECI→Body）')

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
    parser.add_argument('--earth-highres-texture', type=str,
                        default='assets/textures/earth_day_500m',
                        help='可視域クロップに使う高解像度の地球テクスチャ。'
                             '単一 equirect 画像か、tools/prepare_bmng.py が生成する'
                             'タイル分割ディレクトリ（既定: BMNG 500 m/px 相当の'
                             ' 86400×43200 タイル群）。無ければ'
                             ' assets/textures/earth_day.jpg → earth_texture の順に'
                             'フォールバック')
    parser.add_argument('--earth-crop-max-dim', type=int, default=8192,
                        help='クロップ画像の最大辺 [px]（超えたら縮小。'
                             'GPU/メモリに余裕があれば 12000 で 500 m/px を最大活用）')
    parser.add_argument('--earth-gibs', action='store_true', default=False,
                        help='地球テクスチャを NASA GIBS (WMTS) から可視域だけ'
                             'オンデマンド取得する（実写日次画像・雲込み、250 m/px。'
                             '要ネットワーク。runs/_gibs_cache/ にタイルをキャッシュし、'
                             '失敗時はローカルソースへフォールバック）')
    parser.add_argument('--earth-gibs-date', type=str, default=None,
                        help='GIBS の撮像日 (YYYY-MM-DD)。未指定は昨日 (UTC)')
    parser.add_argument('--earth-gibs-layer', type=str,
                        default='MODIS_Terra_CorrectedReflectance_TrueColor',
                        help='GIBS レイヤ名（例: MODIS_Aqua_CorrectedReflectance_TrueColor, '
                             'VIIRS_SNPP_CorrectedReflectance_TrueColor）')
    parser.add_argument('--earth-analytic-sphere', action='store_true', default=False,
                        help='mode=absolute の地球を従来の解析 sphere で描く'
                             '（既定は UV 球メッシュ。メッシュは毎フレームの地球姿勢'
                             '更新がカーネル再コンパイルを誘発しないため大幅に速い）')
    parser.add_argument('--no-earth-crop', dest='earth_crop', action='store_false',
                        default=True,
                        help='可視域の高解像度クロップを無効化し、earth_texture を'
                             '全球に貼る従来動作へ戻す')
    parser.add_argument('--earth-crop-margin-deg', type=float, default=5.0,
                        help='可視キャップ（地平線）に足すクロップ余白の中心角 [deg]')
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


def build_groundobs_parser() -> argparse.ArgumentParser:
    """groundobs verb（地上望遠鏡からの光学観測シミュレーション）のパーサ。

    地上局 (lat/lon/alt) から軌道上物体を光学望遠鏡で観測したときの
    「明るさ（見かけ等級）」と「見え方（点像〜分解像）」を計算する。
    地球自転 (GMST)・実太陽暦 (VSOP87)・本影/半影・大気減光・シーイング
    PSF・センサノイズを含む。座標系は ECI、UTC エポック基準。
    """
    parser = argparse.ArgumentParser(
        description='地上望遠鏡からの宇宙物体光学観測（見かけ等級 + 望遠鏡像）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
例:
  # mrender 経由（推奨、runs/ に自動配置）
  python mrender.py groundobs --config presets/groundobs_hubble.yaml

  # スタンドアロン: LEO 物体の天頂パスを観測
  python ground_observation.py --altitude-km 500 --start-overhead --frames 30

  # GEO の小物体（点像 + センサノイズ）
  python ground_observation.py --altitude-km 35786 --start-overhead \\
      --sphere-radius-m 1.0 --sensor-noise --frames 10
        """,
    )
    parser.add_argument('--config', nargs='+', default=[],
                        help='YAMLプリセットファイル（複数指定可、後のファイルが優先）')

    # 時間軸（UTC エポック基準）
    parser.add_argument('--epoch-utc', type=str, default='2026-01-01T00:00:00',
                        help='シミュレーション t=0 の UTC エポック（ISO 8601）。'
                             'GMST・太陽位置の計算基準になる')
    parser.add_argument('--frames', type=int, default=30, help='フレーム数')
    parser.add_argument('--fps', type=float, default=1.0,
                        help='フレームレート [fps]（dt = 1/fps。地上観測は 1 fps 既定）')
    parser.add_argument('--duration-sec', type=float, default=None,
                        help='総観測時間 [s]。指定時は dt = duration_sec / frames')
    parser.add_argument('--start-time', type=float, default=0.0,
                        help='エポックからの開始オフセット [s]')

    # 観測地（地上局）
    parser.add_argument('--site-lat', type=float, default=35.0, help='観測地 緯度 [deg]（北+）')
    parser.add_argument('--site-lon', type=float, default=135.0, help='観測地 経度 [deg]（東+）')
    parser.add_argument('--site-alt-m', type=float, default=0.0, help='観測地 標高 [m]')
    parser.add_argument('--min-elevation-deg', type=float, default=0.0,
                        help='この仰角未満は地平線下として非可視扱い [deg]')

    # 軌道（TLE / CSV ephemeris / ケプラー要素。優先度もこの順）
    parser.add_argument('--tle', type=str, default=None,
                        help='TLE ファイルパス（SGP4 伝播、TEME≈ECI 近似）。'
                             'epoch-utc 未指定時は TLE エポックを t=0 に採用する')
    parser.add_argument('--tle-name', type=str, default=None,
                        help='TLE ファイル内の衛星名（部分一致。未指定は先頭エントリ）')
    parser.add_argument('--orbit-csv', type=str, default=None,
                        help='絶対軌道 CSV（time_s,a_km,e,inc_rad,raan_rad,ome_rad,f_rad[,q1..q4]）')
    parser.add_argument('--altitude-km', type=float, default=500.0,
                        help='円軌道高度 [km]（semi-major-axis-km 未指定時に a = RE + altitude）')
    parser.add_argument('--semi-major-axis-km', type=float, default=None,
                        help='軌道長半径 a [km]（指定時は altitude-km より優先）')
    parser.add_argument('--eccentricity', type=float, default=0.0, help='離心率 e')
    parser.add_argument('--inclination-deg', type=float, default=51.6, help='軌道傾斜角 i [deg]')
    parser.add_argument('--raan-deg', type=float, default=0.0, help='昇交点赤経 Ω [deg]')
    parser.add_argument('--arg-periapsis-deg', type=float, default=0.0, help='近地点引数 ω [deg]')
    parser.add_argument('--mean-anomaly-deg', type=float, default=0.0, help='初期平均近点角 M₀ [deg]')
    parser.add_argument('--start-overhead', action='store_true', default=False,
                        help='t=start_time で物体が観測地の天頂に最も近づくよう Ω(RAAN) と '
                             'M₀ を自動調整（ケプラー軌道のみ。デモ・可視パス作成用）')

    # 姿勢
    parser.add_argument('--attitude-mode',
                        choices=['nadir', 'sun_tracking', 'velocity_aligned', 'tumble'],
                        default='nadir', help='姿勢モード（orbit-csv に q1..q4 があればそれが優先）')
    parser.add_argument('--Ix', type=float, default=4.0, help='慣性モーメント Ix [kg·m²]（tumble）')
    parser.add_argument('--Iy', type=float, default=2.0, help='慣性モーメント Iy [kg·m²]（tumble）')
    parser.add_argument('--Iz', type=float, default=1.0, help='慣性モーメント Iz [kg·m²]（tumble）')
    parser.add_argument('--wx', type=float, default=0.3, help='初期角速度 ωx [rad/s]（tumble）')
    parser.add_argument('--wy', type=float, default=0.1, help='初期角速度 ωy [rad/s]（tumble）')
    parser.add_argument('--wz', type=float, default=1.0, help='初期角速度 ωz [rad/s]（tumble）')

    # ターゲットモデル（OBJ または解析球）
    parser.add_argument('--model-path', type=str, default=None,
                        help='OBJ モデルパス（未指定時は拡散球ターゲット）')
    parser.add_argument('--model-scale', type=float, default=0.001,
                        help='モデル単位→km の倍率（メートル単位モデルなら 0.001）')
    parser.add_argument('--sphere-radius-m', type=float, default=1.0,
                        help='解析球ターゲットの半径 [m]（model-path 未指定時）')
    parser.add_argument('--sphere-albedo', type=float, default=0.3,
                        help='解析球ターゲットの拡散アルベド')

    # 追尾モード・恒星背景・屈折
    parser.add_argument('--tracking-mode', choices=['target', 'sidereal'],
                        default='target',
                        help='target=物体追尾（恒星が露光中に流れる）/ '
                             'sidereal=恒星時追尾（恒星は点像、物体がストリークになる）')
    parser.add_argument('--show-stars', action='store_true', default=False,
                        help='Hipparcos カタログの恒星背景を描画する')
    parser.add_argument('--star-catalog', type=str, default='assets/hipparcos.npz',
                        help='恒星カタログ npz（ra_deg/dec_deg/pm_ra_cosdec/pm_dec/mag）')
    parser.add_argument('--star-mag-limit', type=float, default=9.0,
                        help='描画する恒星の限界等級（Hipparcos は V≈9 まで収録）')
    parser.add_argument('--refraction', action='store_true', default=True,
                        help='大気屈折（Sæmundsson 式）を仰角に適用する（既定: 有効）')
    parser.add_argument('--no-refraction', dest='refraction', action='store_false')

    # 望遠鏡・検出器
    parser.add_argument('--pixel-scale-arcsec', type=float, default=0.5,
                        help='検出器のピクセルスケール [arcsec/px]')
    parser.add_argument('--sensor-px', type=int, default=256,
                        help='検出器の一辺ピクセル数（正方形センサ）')
    parser.add_argument('--supersample', type=int, default=3,
                        help='PSF 畳み込み用の内部スーパーサンプル倍率')
    parser.add_argument('--seeing-arcsec', type=float, default=2.0,
                        help='大気シーイング FWHM [arcsec]（ガウス PSF）')
    parser.add_argument('--turbulence', action='store_true', default=False,
                        help='大気揺らぎの瞬時 PSF を Kolmogorov 位相スクリーンで生成'
                             '（スペックル + 像揺れ + 露光平均）。ガウスシーイングの'
                             '代替で、r0 は seeing-arcsec から換算。短露光'
                             '（exposure-s ≲ 0.1 s）の分解像形状推定向け')
    parser.add_argument('--turbulence-tau0-ms', type=float, default=5.0,
                        help='大気コヒーレンス時間 τ0 [ms]。露光平均のサブ露光数 = '
                             'exposure_s / τ0（turbulence-max-screens で上限）')
    parser.add_argument('--turbulence-max-screens', type=int, default=32,
                        help='露光平均に使う独立スクリーン数の上限。長露光では'
                             '残留スペックルが実際より強め（~1/√N）に残る点に注意')
    parser.add_argument('--turbulence-outer-scale-m', type=float, default=25.0,
                        help='von Kármán 外部スケール L0 [m]（tip-tilt 揺れ幅の上限を決める）')
    parser.add_argument('--turbulence-seed', type=int, default=0,
                        help='位相スクリーン乱数シード（フレーム番号と合成、再現性あり）')
    parser.add_argument('--extinction-k', type=float, default=0.20,
                        help='大気減光係数 k [mag/airmass]（V バンド典型 0.1-0.3）')
    parser.add_argument('--samples', type=int, default=128, help='サンプル数/ピクセル')
    parser.add_argument('--max-depth', type=int, default=6,
                        help='パストレーシングの最大バウンス数')

    # 太陽（物理単位）
    parser.add_argument('--sun-temperature', type=float, default=5778.0,
                        help='太陽の黒体放射色温度 [K]')
    parser.add_argument('--sun-irradiance-wm2', type=float, default=1361.0,
                        help='大気圏外太陽放射照度（太陽定数）[W/m²]。測光の基準にもなる')

    parser.add_argument('--monochrome', action='store_true', default=False,
                        help='センサ像をモノクロ（Rec.709 輝度）で出力する。'
                             'sensor-noise ON 時は常にモノクロなので不要')

    # センサノイズモデル（CCD 方程式、SatCap 移植）
    parser.add_argument('--sensor-noise', action='store_true', default=False,
                        help='CCD ノイズモデルを適用（ショット + 読み出しノイズ、モノクロ出力）')
    parser.add_argument('--aperture-m', type=float, default=0.35, help='望遠鏡口径 [m]')
    parser.add_argument('--throughput', type=float, default=0.7, help='光学系スループット')
    parser.add_argument('--quantum-efficiency', type=float, default=0.8, help='検出器 QE')
    parser.add_argument('--exposure-s', type=float, default=0.05,
                        help='露光時間 [s]（ノイズの光子数に加え、恒星トレイル / '
                             'sidereal ストリークの長さもこの値で決まる）')
    parser.add_argument('--read-noise-e', type=float, default=3.0, help='読み出しノイズ [e⁻ rms]')
    parser.add_argument('--gain-e-per-adu', type=float, default=1.0, help='ゲイン [e⁻/ADU]')
    parser.add_argument('--full-well-e', type=float, default=60000.0, help='フルウェル容量 [e⁻]')
    parser.add_argument('--sky-mag-arcsec2', type=float, default=21.0,
                        help='夜空背景輝度 [mag/arcsec²]（暗い空 21-22、市街地 17-19）')
    parser.add_argument('--noise-seed', type=int, default=0, help='ノイズ乱数シード')

    # 出力
    parser.add_argument('--output-dir', type=str, default=None,
                        help='出力ディレクトリ（未指定時は output/groundobs）')
    parser.add_argument('--stretch', choices=['asinh', 'linear', 'log'], default='asinh',
                        help='PNG 表示用のストレッチ方式')
    parser.add_argument('--stretch-percentile', type=float, default=99.9,
                        help='ストレッチ正規化に使う輝度パーセンタイル')
    parser.add_argument('--save-linear', action='store_true', default=False,
                        help='リニアなセンサ面照度画像 (.npy, W/m²/px) も保存する')
    parser.add_argument('--make-video', action='store_true', default=False,
                        help='レンダ後に ffmpeg で動画化する')
    parser.add_argument('--video-fps', type=float, default=None,
                        help='動画 fps（未指定時はレンダリング fps を使用）')
    parser.add_argument('--video-name', type=str, default='output.mp4',
                        help='動画ファイル名（run ディレクトリ直下に出力）')

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
