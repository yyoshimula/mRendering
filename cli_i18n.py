"""CLI（argparse ヘルプ・usage）の表示言語切替。標準ライブラリのみ・Mitsuba 非依存。

方針は GUI（gui/i18n.js）と同じ: ソースのヘルプ文言は日本語のまま書き、
環境の言語が日本語以外なら辞書「日本語原文 → 英語」で表示時に置換する。
辞書に無い文言は日本語のまま出る（落ちない）。

言語の決定（`cli_lang()`）:
    MRENDER_LANG=ja|en > LC_ALL > LC_MESSAGES > LANG > locale.getlocale()。
    値が ja* なら日本語、それ以外（C / POSIX / 未設定を含む）は英語。

使い方:
    parser = argparse.ArgumentParser(description='…日本語…')
    ...
    return localize_parser(parser)     # build_*_parser() の末尾で 1 回

`localize_parser` は description / epilog / 引数グループ名 / 各引数の help・metavar を
置換する（サブパーサも再帰）。`%(default)s` 等の書式は英訳側にも同じまま残すこと。
ユーザー向け文言を増やしたら `DICT` にも英訳を足す（tests/test_cli_i18n.py が
カバレッジを検査する）。
"""
from __future__ import annotations

import argparse
import locale
import os
from typing import Any, Dict, Optional

DICT: Dict[str, str] = {
    # ---- mrender.py ----
    '  例: python mrender.py render --config presets/iss.yaml\n':
        '  examples: python mrender.py render --config presets/iss.yaml\n',
    '  # 後勝ちで差分 YAML を重ねる\n': '  # later YAML files override earlier ones\n',
    '未対応のサブコマンド': 'unknown subcommand',

    # ---- gui（gui_server.main） ----
    'mrender GUI サーバー': 'mrender GUI server',
    'HTTP ポート': 'HTTP port',
    'バインドするアドレス': 'address to bind',
    '起動時にブラウザを開かない': 'do not open a browser on startup',
    'ライブプレビュー ワーカーのポート（既定: --port + 1）': 'port of the live preview worker (default: --port + 1)',

    # ---- 共通 ----
    'YAMLプリセットファイル（複数指定可、後のファイルが優先）': 'YAML preset file(s); later files take precedence',
    'YAMLプリセット（複数指定可、後のファイルが優先）': 'YAML preset(s); later files take precedence',
    'フレーム数': 'number of frames',
    'フレームレート [fps]': 'frame rate [fps]',
    'サンプル数/ピクセル': 'samples per pixel',
    '画像幅': 'image width',
    '画像高さ': 'image height',
    '離心率 e': 'eccentricity e',
    '軌道傾斜角 i [deg]': 'inclination i [deg]',
    '昇交点赤経 Ω [deg]': 'RAAN Ω [deg]',
    '近地点引数 ω [deg]': 'argument of periapsis ω [deg]',
    '慣性モーメント Ix [kg·m²]': 'moment of inertia Ix [kg·m²]',
    '慣性モーメント Iy [kg·m²]': 'moment of inertia Iy [kg·m²]',
    '慣性モーメント Iz [kg·m²]': 'moment of inertia Iz [kg·m²]',
    '初期角速度 ωx [rad/s]': 'initial angular velocity ωx [rad/s]',
    '初期角速度 ωy [rad/s]': 'initial angular velocity ωy [rad/s]',
    '初期角速度 ωz [rad/s]': 'initial angular velocity ωz [rad/s]',
    '視野角 [deg]': 'field of view [deg]',
    'カメラ位置 [x y z]': 'camera position [x y z]',
    'カメラ注視点 [x y z]': 'camera look-at point [x y z]',
    'カメラの up ベクトル [x y z]': 'camera up vector [x y z]',
    'レンダ後に ffmpeg で動画化する': 'encode a video with ffmpeg after rendering',
    '動画 fps（未指定時はレンダリング fps を使用）': 'video fps (defaults to the rendering fps)',
    '動画ファイル名（run ディレクトリ直下に出力）': 'video file name (written in the run directory)',

    # ---- render / lightcurve / preview / onboard（config_loader.build_parser） ----
    '地球周回物体の絶対軌道と姿勢運動をレンダリングする': 'Render the absolute orbit and attitude motion of an Earth-orbiting object',
    '\n例:\n  python satellite_orbit.py --config presets/iss.yaml\n  python satellite_orbit.py --config presets/multi_object.yaml\n        ':
        '\nexamples:\n  python satellite_orbit.py --config presets/iss.yaml\n  python satellite_orbit.py --config presets/multi_object.yaml\n        ',

    # ---- relative ----
    '相対位置・相対姿勢を直接与えて 2 機をレンダリング（軌道力学なし）':
        'Render two spacecraft from a given relative position and attitude (no orbital dynamics)',
    '\n例:\n  # mrender 経由（推奨、runs/ に自動配置）\n  python mrender.py relative --config presets/relative_static.yaml\n\n  # 静止配置\n  python relative_motion.py --frames 30 --rel-position 1.5 0 0\n\n  # CSV駆動（time_s, x, y, z, qx, qy, qz, qw）\n  python relative_motion.py --mode csv --rel-csv input/rel_state_sample.csv\n\n  # deputy をタンブリング\n  python relative_motion.py --mode tumble --rel-position 1.5 0 0 \\\n      --wx 0.3 --wy 0.1 --wz 1.0\n        ':
        '\nexamples:\n  # via mrender (recommended; output goes under runs/)\n  python mrender.py relative --config presets/relative_static.yaml\n\n  # static placement\n  python relative_motion.py --frames 30 --rel-position 1.5 0 0\n\n  # CSV-driven (time_s, x, y, z, qx, qy, qz, qw)\n  python relative_motion.py --mode csv --rel-csv input/rel_state_sample.csv\n\n  # tumbling deputy\n  python relative_motion.py --mode tumble --rel-position 1.5 0 0 \\\n      --wx 0.3 --wy 0.1 --wz 1.0\n        ',
    '出力ディレクトリ（未指定時は output/relative）': 'output directory (default: output/relative)',
    'シミュレートする総時間 [s]。指定時は dt = duration_sec / frames で frames を均等配分する。未指定時は dt = 1/fps（実時間 1:1）。':
        'total simulated time [s]. When set, frames are spread evenly with dt = duration_sec / frames; otherwise dt = 1/fps (real time 1:1).',
    'シミュレーション開始時刻 [s]（CSV 駆動時の参照開始点）': 'simulation start time [s] (reference start when CSV-driven)',
    'フレーム範囲を N 分割してサブプロセス並列レンダ。GPU バリアントではフレーム準備（CPU）が支配的なのでほぼ線形にスケールする（多フレーム × GPU 向け。少フレームでは各プロセスの起動 ~10s が勝って逆効果）。出力は直列実行と ±1LSB（Mitsuba 固有の揺らぎ）以内で一致':
        'split the frame range into N chunks rendered in parallel subprocesses. Scales almost linearly on GPU variants, where per-frame preparation (CPU) dominates (for many frames × GPU; with few frames the ~10 s process startup wins and it is counterproductive). Output matches serial execution within ±1 LSB (Mitsuba noise)',
    '永続シーン（mi.load_dict を毎フレームやり直さず差分パラメータ更新でレンダ）を無効化して従来動作へ。永続シーンは作り直し比 ±数 LSB の経路差が衛星の鏡面部に出うる（統計的品質は同一・検証済み）':
        'disable the persistent scene (which updates parameters in place instead of re-running mi.load_dict every frame) and fall back to the legacy behavior. The persistent scene can differ by a few LSB on specular parts of the satellite versus rebuilding (statistically equivalent, verified)',
    'このフレーム番号からレンダ（--jobs の内部用。時刻・番号付けはグローバル基準のまま部分レンダする）':
        'render from this frame index (internal use by --jobs; time and numbering stay global)',
    'このフレーム番号の手前までレンダ（--jobs の内部用）': 'render up to (excluding) this frame index (internal use by --jobs)',
    '相対状態の与え方（absolute = chief/deputy の絶対軌道を伝播して相対状態と環境（太陽方向・食・地球姿勢）を導出）':
        'how the relative state is given (absolute = propagate the chief/deputy absolute orbits and derive the relative state and environment: Sun direction, eclipse, Earth attitude)',
    'deputy の chief に対する相対位置 [x y z]（シーン単位）': 'deputy position relative to chief [x y z] (scene units)',
    'deputy の chief に対する相対クォータニオン [qx qy qz qw]': 'deputy attitude relative to chief as a quaternion [qx qy qz qw]',
    '相対位置の成分基準: hill=Hill/RTN、chief=従来のchief機体系': 'frame of the relative position components: hill = Hill/RTN, chief = legacy chief body frame',
    'chief はHill原点に固定。[0 0 0] のみ指定可': 'chief is fixed at the Hill origin; only [0 0 0] is accepted',
    'chief クォータニオン [qx qy qz qw]': 'chief quaternion [qx qy qz qw]',
    '相対状態 CSV: time_s,x,y,z,qx,qy,qz,qw': 'relative state CSV: time_s,x,y,z,qx,qy,qz,qw',
    'absolute モードの t=0 の UTC エポック（ISO 8601）。太陽位置（VSOP87）と GMST（地球姿勢）の基準':
        'UTC epoch of t=0 in absolute mode (ISO 8601). Reference for the Sun position (VSOP87) and GMST (Earth attitude)',
    'chief のケプラー要素 [a_km, e, i_deg, Ω_deg, ω_deg, M0_deg]': 'chief Keplerian elements [a_km, e, i_deg, Ω_deg, ω_deg, M0_deg]',
    'deputy のケプラー要素（absolute モードで CSV 未指定なら必須）': 'deputy Keplerian elements (required in absolute mode unless a CSV is given)',
    'chief の絶対軌道 CSV（CsvEphemeris スキーマ: time_s, a_km, e, inc_rad, raan_rad, ome_rad, f_rad [, q1..q4]）。指定時 --chief-oe より優先':
        'chief absolute-orbit CSV (CsvEphemeris schema: time_s, a_km, e, inc_rad, raan_rad, ome_rad, f_rad [, q1..q4]). Takes precedence over --chief-oe',
    'deputy の絶対軌道 CSV（同スキーマ）。指定時 --deputy-oe より優先': 'deputy absolute-orbit CSV (same schema). Takes precedence over --deputy-oe',
    'absolute モードの deputy 姿勢: tumble=慣性系トルクフリー伝播（rel_quat は t=0 の RTN 相対初期姿勢、ω は wx/wy/wz）/ lvlh=deputy 自身の RTN に rel_quat を固定オフセット / csv=--deputy-orbit-csv の q1..q4 列（ECI→Body）':
        'deputy attitude in absolute mode: tumble = torque-free propagation in the inertial frame (rel_quat is the initial RTN-relative attitude at t=0, ω from wx/wy/wz) / lvlh = rel_quat as a fixed offset from the deputy RTN frame / csv = q1..q4 columns of --deputy-orbit-csv (ECI→Body)',
    'chief OBJ モデルパス': 'chief OBJ model path',
    'deputy OBJ モデルパス': 'deputy OBJ model path',
    'カメラ搭載機の機体系での取付位置 [km]': 'camera mount position in the host body frame [km]',
    'fixed モードの機体系での視線方向': 'line-of-sight direction in the body frame for fixed modes',
    'カメラ搭載機の機体系での上方向': 'camera up direction in the host body frame',
    'chief 機体を描画しない（カメラ＝サービサ機体の機載カメラ視点にする場合）': 'do not render the chief body (for an onboard-camera view from the servicer)',
    'パストレーシングの最大バウンス数': 'maximum number of path-tracing bounces',
    '背景に地球（テクスチャ付き解析球）を描画する': 'render Earth (textured analytic sphere) in the background',
    'chief から見た地心方向の単位ベクトル（シーン座標）': 'unit vector from chief toward the Earth center (scene coordinates)',
    'chief の高度 [km]（地球球体の距離を決める）': 'chief altitude [km] (sets the distance to the Earth sphere)',
    '地球テクスチャ画像のパス': 'Earth texture image path',
    '地球テクスチャの自転角オフセット [deg]': 'Earth texture rotation offset [deg]',
    '可視域クロップに使う高解像度の地球テクスチャ。単一 equirect 画像か、tools/prepare_bmng.py が生成するタイル分割ディレクトリ（既定: BMNG 500 m/px 相当の 86400×43200 タイル群）。無ければ assets/textures/earth_day.jpg → earth_texture の順にフォールバック':
        'high-resolution Earth texture for the visible-area crop: a single equirectangular image or a tile directory produced by tools/prepare_bmng.py (default: BMNG 500 m/px, 86400×43200 tiles). Falls back to assets/textures/earth_day.jpg, then earth_texture',
    'クロップ画像の最大辺 [px]（超えたら縮小。GPU/メモリに余裕があれば 12000 で 500 m/px を最大活用）':
        'maximum side of the crop image [px] (downscaled beyond this; 12000 uses the full 500 m/px if GPU/memory allow)',
    '地球テクスチャを NASA GIBS (WMTS) から可視域だけオンデマンド取得する（実写日次画像・雲込み、250 m/px。要ネットワーク。runs/_gibs_cache/ にタイルをキャッシュし、失敗時はローカルソースへフォールバック）':
        'fetch only the visible area of the Earth texture on demand from NASA GIBS (WMTS): daily real imagery with clouds, 250 m/px. Needs network; tiles are cached in runs/_gibs_cache/ and local sources are used on failure',
    'GIBS の撮像日 (YYYY-MM-DD)。未指定は昨日 (UTC)': 'GIBS imaging date (YYYY-MM-DD). Default: yesterday (UTC)',
    'GIBS レイヤ名（例: MODIS_Aqua_CorrectedReflectance_TrueColor, VIIRS_SNPP_CorrectedReflectance_TrueColor）':
        'GIBS layer name (e.g. MODIS_Aqua_CorrectedReflectance_TrueColor, VIIRS_SNPP_CorrectedReflectance_TrueColor)',
    'mode=absolute の地球を従来の解析 sphere で描く（既定は UV 球メッシュ。メッシュは毎フレームの地球姿勢更新がカーネル再コンパイルを誘発しないため大幅に速い）':
        'draw the Earth in mode=absolute as the legacy analytic sphere (default is a UV-sphere mesh, which is much faster because per-frame attitude updates do not trigger kernel recompilation)',
    '可視域の高解像度クロップを無効化し、earth_texture を全球に貼る従来動作へ戻す':
        'disable the high-resolution visible-area crop and map earth_texture over the whole globe (legacy behavior)',
    '可視キャップ（地平線）に足すクロップ余白の中心角 [deg]': 'extra crop margin beyond the visible cap (horizon), as a central angle [deg]',
    '太陽光の進行方向ベクトル（正規化不要）': 'direction of travel of sunlight (need not be normalized)',
    '太陽光の放射照度スケール（レンダリング単位）': 'sunlight irradiance scale (rendering units)',
    '指定時は黒体放射色 [K] で太陽色を決める（例: 5778）': 'if set, the Sun color follows a blackbody at this temperature [K] (e.g. 5778)',
    '環境光の明るさ。starfield 指定時はそのスケールに使う（未指定時は従来の淡い定数環境光）':
        'ambient light brightness; used as the starfield scale when --starfield is set (default: the legacy faint constant ambient)',
    '星空 envmap (EXR) のパス（例: assets/starfield.exr）': 'starfield envmap (EXR) path (e.g. assets/starfield.exr)',
    '指定時は地球をテクスチャ無しの一様ランバート球（反射率 = この値）にする。PV 計測の解析検証・雲なし平均アルベド (~0.3) の概算用。クロップ/GIBS は無効になる':
        'if set, Earth becomes an untextured uniform Lambertian sphere with this albedo. For analytic validation of PV measurements and rough cloud-free mean albedo (~0.3) estimates. Disables crop/GIBS',
    '既製のアルベドマップ PNG（earth_albedo_map.py の出力）を地球に貼る': 'map a prebuilt albedo map PNG (output of earth_albedo_map.py) onto Earth',
    'MODIS 雲分率・雲光学的厚さ（NASA GIBS）+ 地表アルベドから日付ごとのアルベドマップを自動生成して使う（要ネットワーク、runs/_gibs_cache/albedo/ にキャッシュ）':
        'auto-generate and use a per-date albedo map from MODIS cloud fraction and optical thickness (NASA GIBS) plus surface albedo (needs network; cached in runs/_gibs_cache/albedo/)',
    '自動生成の日付 YYYY-MM-DD（未指定はフレームの UTC 日付 / epoch）': 'date for the auto-generated map, YYYY-MM-DD (default: the frame UTC date / epoch)',
    'マップ解像度の GIBS level（2: 4096×2048 ≈10 km/px、3: 8192×4096）': 'GIBS level of the map resolution (2: 4096×2048 ≈10 km/px, 3: 8192×4096)',
    '自動生成でキャッシュ済みタイルのみ使う（ネットワーク不使用）': 'use only cached tiles for auto-generation (no network)',
    '受光面とみなす OBJ パーツ名（pv_host のモデル）。閉じたメッシュは表裏全フェイスで平均される':
        'OBJ part names treated as receiving faces (on the pv_host model). Closed meshes are averaged over all faces, front and back',
    'パネルを取り付ける機体（CLI 単一パネル / pv_parts 用）': 'spacecraft the panel is mounted on (for the single CLI panel / pv_parts)',
    '仮想矩形パネルの幅・高さ [m]。指定すると 1 枚追加する': 'width and height of a virtual rectangular panel [m]; adds one panel when given',
    '仮想パネル中心（機体系 [m]）': 'virtual panel center (body frame [m])',
    '仮想パネル受光面の法線（機体系、片面受光）': 'virtual panel receiving-face normal (body frame, single-sided)',
    '仮想パネル面内の上方向（機体系、見た目のみ）': 'virtual panel in-plane up direction (body frame, appearance only)',
    '仮想パネルの名前（CSV の panel 列）': 'virtual panel name (panel column of the CSV)',
    '発電効率 η（P = η·A·E_total）。3 接合 GaAs ≈ 0.30、Si ≈ 0.2': 'conversion efficiency η (P = η·A·E_total). Triple-junction GaAs ≈ 0.30, Si ≈ 0.2',
    'irradiancemeter のパス数/フレーム（間接光の積分精度）': 'irradiancemeter paths per frame (integration accuracy of indirect light)',
    '直達光の遮蔽判定に使うパネル上のサンプル点数': 'number of sample points on the panel for direct-light shadowing',
    '物理換算に使う太陽定数 [W/m²]（レンダ単位 sun_irradiance とは独立）': 'solar constant for physical conversion [W/m²] (independent of the rendering-unit sun_irradiance)',
    '出力 CSV 名（run ディレクトリ直下）': 'output CSV name (in the run directory)',
    '環境光（envmap / 定数フィル光）も PV 計測に含める。既定は除外（relative の既定環境光は可視化用で物理量でないため）':
        'include ambient light (envmap / constant fill) in the PV measurement. Excluded by default (the default ambient in relative is for visualization, not a physical quantity)',
    '慣性軸を描画する（既定: 有効）': 'draw the inertial axes (default: on)',
    '機体軸を描画する（既定: 有効）': 'draw the body axes (default: on)',

    # ---- rotation ----
    'オイラー回転運動方程式による衛星タンブリング': 'Satellite tumbling from Euler\'s rotation equations',
    '\n例:\n  # mrender 経由（推奨、runs/ に自動配置）\n  python mrender.py rotation --config presets/rotation_hubble.yaml\n\n  # スタンドアロン: YAMLプリセット\n  python simple_rotation.py --config presets/rotation_hubble.yaml\n\n  # プリセット + 個別オーバーライド\n  python simple_rotation.py --config presets/rotation_hubble.yaml --frames 10 --samples 4\n\n  # デフォルトプロシージャルモデル\n  python simple_rotation.py --frames 30\n        ':
        '\nexamples:\n  # via mrender (recommended; output goes under runs/)\n  python mrender.py rotation --config presets/rotation_hubble.yaml\n\n  # standalone: YAML preset\n  python simple_rotation.py --config presets/rotation_hubble.yaml\n\n  # preset + individual overrides\n  python simple_rotation.py --config presets/rotation_hubble.yaml --frames 10 --samples 4\n\n  # default procedural model\n  python simple_rotation.py --frames 30\n        ',
    'フレーム数（デフォルト: 30）': 'number of frames (default: 30)',
    'フレームレート [fps]（デフォルト: 30）': 'frame rate [fps] (default: 30)',
    'サンプル数/ピクセル（デフォルト: 256）': 'samples per pixel (default: 256)',
    '画像幅（デフォルト: 640）': 'image width (default: 640)',
    '画像高さ（デフォルト: 480）': 'image height (default: 480)',
    '慣性モーメント Ix [kg·m²]（デフォルト: 4.0）': 'moment of inertia Ix [kg·m²] (default: 4.0)',
    '慣性モーメント Iy [kg·m²]（デフォルト: 2.0）': 'moment of inertia Iy [kg·m²] (default: 2.0)',
    '慣性モーメント Iz [kg·m²]（デフォルト: 1.0）': 'moment of inertia Iz [kg·m²] (default: 1.0)',
    '初期角速度 ωx [rad/s]（デフォルト: 0.3）': 'initial angular velocity ωx [rad/s] (default: 0.3)',
    '初期角速度 ωy [rad/s]（デフォルト: 0.1）': 'initial angular velocity ωy [rad/s] (default: 0.1)',
    '初期角速度 ωz [rad/s]（デフォルト: 2.0）': 'initial angular velocity ωz [rad/s] (default: 2.0)',
    'OBJモデルファイルパス': 'OBJ model file path',
    'モデルスケール（デフォルト: 1.0）': 'model scale (default: 1.0)',
    '視野角 [deg]（デフォルト: 40）': 'field of view [deg] (default: 40)',
    '出力ディレクトリ（未指定時は output/simple）': 'output directory (default: output/simple)',

    # ---- groundobs ----
    '地上望遠鏡からの宇宙物体光学観測（見かけ等級 + 望遠鏡像）': 'Optical observation of a space object from a ground telescope (apparent magnitude + telescope image)',
    '\n例:\n  # mrender 経由（推奨、runs/ に自動配置）\n  python mrender.py groundobs --config presets/groundobs_hubble.yaml\n\n  # スタンドアロン: LEO 物体の天頂パスを観測\n  python ground_observation.py --altitude-km 500 --start-overhead --frames 30\n\n  # GEO の小物体（点像 + センサノイズ）\n  python ground_observation.py --altitude-km 35786 --start-overhead \\\n      --sphere-radius-m 1.0 --sensor-noise --frames 10\n        ':
        '\nexamples:\n  # via mrender (recommended; output goes under runs/)\n  python mrender.py groundobs --config presets/groundobs_hubble.yaml\n\n  # standalone: observe a zenith pass of a LEO object\n  python ground_observation.py --altitude-km 500 --start-overhead --frames 30\n\n  # small GEO object (point source + sensor noise)\n  python ground_observation.py --altitude-km 35786 --start-overhead \\\n      --sphere-radius-m 1.0 --sensor-noise --frames 10\n        ',
    'シミュレーション t=0 の UTC エポック（ISO 8601）。GMST・太陽位置の計算基準になる': 'UTC epoch of simulation t=0 (ISO 8601). Reference for GMST and the Sun position',
    'フレームレート [fps]（dt = 1/fps。地上観測は 1 fps 既定）': 'frame rate [fps] (dt = 1/fps; ground observation defaults to 1 fps)',
    '総観測時間 [s]。指定時は dt = duration_sec / frames': 'total observation time [s]. When set, dt = duration_sec / frames',
    'エポックからの開始オフセット [s]': 'start offset from the epoch [s]',
    '観測地 緯度 [deg]（北+）': 'site latitude [deg] (north positive)',
    '観測地 経度 [deg]（東+）': 'site longitude [deg] (east positive)',
    '観測地 標高 [m]': 'site elevation [m]',
    'この仰角未満は地平線下として非可視扱い [deg]': 'below this elevation the object is treated as not visible [deg]',
    'TLE ファイルパス（SGP4 伝播、TEME≈ECI 近似）。epoch-utc 未指定時は TLE エポックを t=0 に採用する':
        'TLE file path (SGP4 propagation, TEME≈ECI). Without --epoch-utc the TLE epoch becomes t=0',
    'TLE ファイル内の衛星名（部分一致。未指定は先頭エントリ）': 'satellite name within the TLE file (substring match; default: first entry)',
    '絶対軌道 CSV（time_s,a_km,e,inc_rad,raan_rad,ome_rad,f_rad[,q1..q4]）': 'absolute-orbit CSV (time_s,a_km,e,inc_rad,raan_rad,ome_rad,f_rad[,q1..q4])',
    '円軌道高度 [km]（semi-major-axis-km 未指定時に a = RE + altitude）': 'circular orbit altitude [km] (a = RE + altitude unless --semi-major-axis-km is given)',
    '軌道長半径 a [km]（指定時は altitude-km より優先）': 'semi-major axis a [km] (takes precedence over --altitude-km)',
    '初期平均近点角 M₀ [deg]': 'initial mean anomaly M₀ [deg]',
    't=start_time で物体が観測地の天頂に最も近づくよう Ω(RAAN) と M₀ を自動調整（ケプラー軌道のみ。デモ・可視パス作成用）':
        'auto-adjust Ω (RAAN) and M₀ so the object passes closest to the site zenith at t=start_time (Keplerian orbits only; for demos / creating a visible pass)',
    '姿勢モード（orbit-csv に q1..q4 があればそれが優先）': 'attitude mode (q1..q4 in --orbit-csv take precedence if present)',
    '慣性モーメント Ix [kg·m²]（tumble）': 'moment of inertia Ix [kg·m²] (tumble)',
    '慣性モーメント Iy [kg·m²]（tumble）': 'moment of inertia Iy [kg·m²] (tumble)',
    '慣性モーメント Iz [kg·m²]（tumble）': 'moment of inertia Iz [kg·m²] (tumble)',
    '初期角速度 ωx [rad/s]（tumble）': 'initial angular velocity ωx [rad/s] (tumble)',
    '初期角速度 ωy [rad/s]（tumble）': 'initial angular velocity ωy [rad/s] (tumble)',
    '初期角速度 ωz [rad/s]（tumble）': 'initial angular velocity ωz [rad/s] (tumble)',
    'OBJ モデルパス（未指定時は拡散球ターゲット）': 'OBJ model path (default: diffuse sphere target)',
    'モデル単位→km の倍率（メートル単位モデルなら 0.001）': 'model units → km factor (0.001 for a model in metres)',
    '解析球ターゲットの半径 [m]（model-path 未指定時）': 'radius of the analytic sphere target [m] (when --model-path is not given)',
    '解析球ターゲットの拡散アルベド': 'diffuse albedo of the analytic sphere target',
    'target=物体追尾（恒星が露光中に流れる）/ sidereal=恒星時追尾（恒星は点像、物体がストリークになる）':
        'target = track the object (stars trail during the exposure) / sidereal = sidereal tracking (stars are points, the object streaks)',
    'Hipparcos カタログの恒星背景を描画する': 'draw the Hipparcos star background',
    '恒星カタログ npz（ra_deg/dec_deg/pm_ra_cosdec/pm_dec/mag）': 'star catalog npz (ra_deg/dec_deg/pm_ra_cosdec/pm_dec/mag)',
    '描画する恒星の限界等級（Hipparcos は V≈9 まで収録）': 'limiting magnitude of drawn stars (Hipparcos goes down to V≈9)',
    '大気屈折（Sæmundsson 式）を仰角に適用する（既定: 有効）': 'apply atmospheric refraction (Sæmundsson formula) to the elevation (default: on)',
    '検出器のピクセルスケール [arcsec/px]': 'detector pixel scale [arcsec/px]',
    '検出器の一辺ピクセル数（正方形センサ）': 'detector side length in pixels (square sensor)',
    'PSF 畳み込み用の内部スーパーサンプル倍率': 'internal supersampling factor for the PSF convolution',
    '大気シーイング FWHM [arcsec]（ガウス PSF）': 'atmospheric seeing FWHM [arcsec] (Gaussian PSF)',
    '大気揺らぎの瞬時 PSF を Kolmogorov 位相スクリーンで生成（スペックル + 像揺れ + 露光平均）。ガウスシーイングの代替で、r0 は seeing-arcsec から換算。短露光（exposure-s ≲ 0.1 s）の分解像形状推定向け':
        'generate the instantaneous turbulence PSF from Kolmogorov phase screens (speckle + image motion + exposure averaging). Replaces Gaussian seeing; r0 is derived from --seeing-arcsec. For resolved-shape estimation at short exposures (--exposure-s ≲ 0.1 s)',
    '大気コヒーレンス時間 τ0 [ms]。露光平均のサブ露光数 = exposure_s / τ0（turbulence-max-screens で上限）':
        'atmospheric coherence time τ0 [ms]. Number of sub-exposures averaged = exposure_s / τ0 (capped by --turbulence-max-screens)',
    '露光平均に使う独立スクリーン数の上限。長露光では残留スペックルが実際より強め（~1/√N）に残る点に注意':
        'maximum number of independent screens averaged per exposure. Note that for long exposures residual speckle stays stronger than reality (~1/√N)',
    'von Kármán 外部スケール L0 [m]（tip-tilt 揺れ幅の上限を決める）': 'von Kármán outer scale L0 [m] (caps the tip-tilt excursion)',
    '位相スクリーン乱数シード（フレーム番号と合成、再現性あり）': 'phase-screen random seed (combined with the frame index; reproducible)',
    '大気減光係数 k [mag/airmass]（V バンド典型 0.1-0.3）': 'atmospheric extinction coefficient k [mag/airmass] (typical V band 0.1-0.3)',
    '太陽の黒体放射色温度 [K]': 'blackbody color temperature of the Sun [K]',
    '大気圏外太陽放射照度（太陽定数）[W/m²]。測光の基準にもなる': 'exo-atmospheric solar irradiance (solar constant) [W/m²]. Also the photometric reference',
    'センサ像をモノクロ（Rec.709 輝度）で出力する。sensor-noise ON 時は常にモノクロなので不要':
        'write the sensor image in monochrome (Rec.709 luminance). Not needed with --sensor-noise, which is always monochrome',
    'CCD ノイズモデルを適用（ショット + 読み出しノイズ、モノクロ出力）': 'apply the CCD noise model (shot + read noise, monochrome output)',
    '望遠鏡口径 [m]': 'telescope aperture [m]',
    '光学系スループット': 'optical throughput',
    '検出器 QE': 'detector quantum efficiency',
    '露光時間 [s]（ノイズの光子数に加え、恒星トレイル / sidereal ストリークの長さもこの値で決まる）':
        'exposure time [s] (sets the photon count for noise and the length of star trails / sidereal streaks)',
    '読み出しノイズ [e⁻ rms]': 'read noise [e⁻ rms]',
    'ゲイン [e⁻/ADU]': 'gain [e⁻/ADU]',
    'フルウェル容量 [e⁻]': 'full-well capacity [e⁻]',
    '夜空背景輝度 [mag/arcsec²]（暗い空 21-22、市街地 17-19）': 'night-sky background brightness [mag/arcsec²] (dark sky 21-22, urban 17-19)',
    'ノイズ乱数シード': 'noise random seed',
    '地球を一様ランバート球（反射率 = この値）にする。雲込み全球平均は 0.29〜0.30（CERES）。--earth-texture 指定時は無視':
        'make Earth a uniform Lambertian sphere with this albedo. Cloud-inclusive global mean is 0.29–0.30 (CERES). Ignored when --earth-texture is set',
    '指定時はテクスチャ地球（可視域クロップ + GMST 姿勢、relative と同じ経路）。BMNG は雲なし・海が暗いので地球照は下限値になる':
        'if set, a textured Earth (visible-area crop + GMST attitude, same path as relative). BMNG is cloud-free with dark oceans, so earthshine is a lower bound',
    '観測像のレンダにも地球を入れ、物体への地球照（照り返し）を測光に含める。既定 OFF（従来どおり太陽のみ。ランバート球の解析検証はこの既定で成立する）':
        'include Earth in the observation render so earthshine on the object enters the photometry. Default off (Sun only, as before; the Lambertian-sphere analytic validation holds with this default)',
    '出力ディレクトリ（未指定時は output/groundobs）': 'output directory (default: output/groundobs)',
    'PNG 表示用のストレッチ方式': 'stretch method for the display PNG',
    'ストレッチ正規化に使う輝度パーセンタイル': 'brightness percentile used for stretch normalization',
    'リニアなセンサ面照度画像 (.npy, W/m²/px) も保存する': 'also save the linear sensor-plane irradiance image (.npy, W/m²/px)',
}


def cli_lang() -> str:
    """'ja' か 'en'。MRENDER_LANG > LC_ALL > LC_MESSAGES > LANG > locale.getlocale()。"""
    forced = os.environ.get('MRENDER_LANG', '').strip().lower()
    if forced in ('ja', 'en'):
        return forced
    for key in ('LC_ALL', 'LC_MESSAGES', 'LANG'):
        val = os.environ.get(key, '').strip()
        if val and val not in ('C', 'POSIX', 'C.UTF-8', 'C.utf8'):
            return 'ja' if val.lower().startswith('ja') else 'en'
    try:
        loc = locale.getlocale()[0]
    except Exception:  # noqa: BLE001 - ロケール取得失敗は英語扱い
        loc = None
    if loc:
        return 'ja' if loc.lower().startswith('ja') else 'en'
    return 'en'


_LANG: Optional[str] = None


def current_lang() -> str:
    global _LANG
    if _LANG is None:
        _LANG = cli_lang()
    return _LANG


def set_lang(lang: Optional[str]) -> None:
    """テスト用: 言語を固定する（None で環境から再判定）。"""
    global _LANG
    _LANG = lang


def tr(text: Any, **vars: Any) -> Any:
    """原文 → 表示文言。文字列以外はそのまま。vars で {name} を置換する。"""
    if not isinstance(text, str):
        return text
    out = DICT.get(text, text) if current_lang() != 'ja' else text
    if vars:
        for k, v in vars.items():
            out = out.replace('{' + k + '}', str(v))
    return out


def localize_parser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """parser の description / epilog / グループ名 / help / metavar を表示言語に置換して返す（サブパーサも再帰）。"""
    if current_lang() == 'ja':
        return parser
    parser.description = tr(parser.description)
    parser.epilog = tr(parser.epilog)
    for group in parser._action_groups:  # noqa: SLF001 - argparse に公開 API が無い
        group.title = tr(group.title)
        group.description = tr(group.description)
    for action in parser._actions:  # noqa: SLF001
        if action.help and action.help is not argparse.SUPPRESS:
            action.help = tr(action.help)
        if isinstance(action.metavar, str):
            action.metavar = tr(action.metavar)
        choices = getattr(action, 'choices', None)
        if isinstance(choices, dict):  # サブパーサ
            for sub in choices.values():
                if isinstance(sub, argparse.ArgumentParser):
                    localize_parser(sub)
    return parser
