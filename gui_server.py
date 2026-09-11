#!/usr/bin/env python3
"""mrender GUI サーバー（標準ライブラリのみ、Flask 不要）。

ブラウザからクリック操作で YAML プリセット相当の設定を組み立て、
`mrender.py <verb> --config <生成 YAML>` をサブプロセスとして起動する。
レンダリングの進捗（frames/ 内の PNG 数）とログ、最新フレームの
ライブプレビュー、過去ラン（runs/）の一覧表示までを提供する。

起動:
    python mrender.py gui              # http://127.0.0.1:8600
    python gui_server.py --port 8765

設計メモ:
    - Mitsuba は import しない（軽量起動）。フォームのデフォルト値は
      config_loader._ARG_DEFAULTS（共通 verb）と verb_parsers の argparse
      定義（relative / rotation verb）から供給する。後者はハードコードせず
      `parser_defaults(build_*_parser())` で自動導出するので、verb 側の
      argparse を変えれば GUI も自動追従する。
    - フォーム状態は「argparse dest 名 → 値」のフラット辞書。YAML 出力時、
      dict 値（model_parts 等）は `advanced:` セクションに包む
      （relative/rotation の 1 段フラット化ローダが dict をトップレベルに
      置けないため。config_loader 側も同じ規則で読める）。
    - ジョブは output_dir を明示指定して起動するため、実行前から
      ランディレクトリの場所が分かる（進捗 = frames/*.png のカウント）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import secrets
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from config_loader import _ARG_DEFAULTS, load_flat_yaml_config
from verb_parsers import (
    build_groundobs_parser,
    build_relative_parser,
    build_rotation_parser,
    parser_defaults,
)

ROOT = Path(__file__).resolve().parent
GUI_DIR = ROOT / 'gui'
RUNS_DIR = ROOT / 'runs'
GUI_CONFIG_DIR = RUNS_DIR / '_gui_configs'

_SLUG_RE = re.compile(r'[^A-Za-z0-9._-]+')

# ---------------------------------------------------------------------------
# フォームスキーマ
# ---------------------------------------------------------------------------
# field 型: int / float / str / bool / vec3 / vec4 / select / file / yaml
# file の kind: model / texture / csv / envmap / any
MATERIAL_NAMES = ['', 'aluminum_brushed', 'aluminum_polished', 'gold', 'solar_panel',
                  'thermal_blanket', 'radiator', 'carbon_composite', 'white_paint',
                  'kapton_mli']

# relative / rotation verb のデフォルトは argparse 定義から自動導出する
# （verb_parsers は Mitsuba 非依存なので GUI から安全に import できる）。
# 以前はここに辞書をハードコードしていたが、argparse 側と乖離していた
# （rotation の camera_origin が [6,4,4] vs 実際の [3,2,2] 等）ので廃止した。
RELATIVE_DEFAULTS: Dict[str, Any] = parser_defaults(build_relative_parser())
ROTATION_DEFAULTS: Dict[str, Any] = parser_defaults(build_rotation_parser())
GROUNDOBS_DEFAULTS: Dict[str, Any] = parser_defaults(build_groundobs_parser())


def F(key: str, label: str, ftype: str, **kw) -> Dict[str, Any]:
    """フィールド定義の短縮コンストラクタ。"""
    d = {'key': key, 'label': label, 'type': ftype}
    d.update(kw)
    return d


COMMON_SECTIONS: List[Dict[str, Any]] = [
    {'title': 'レンダリング', 'fields': [
        F('frames', 'フレーム数', 'int'),
        F('samples', 'サンプル数 (spp)', 'int', help='高いほど綺麗・遅い。プレビュー 16–32、本番 128–512'),
        F('width', '幅 [px]', 'int'),
        F('height', '高さ [px]', 'int'),
        F('duration_sec', 'シミュレーション時間 [s]', 'float', optional=True,
          help='未指定時は orbit_speed 由来の時間軸'),
        F('start_time', '開始時刻 [s]', 'float'),
        F('epoch_utc', 'エポック UTC (ISO 8601)', 'str', optional=True,
          help='t=0 の実時刻。指定すると太陽=VSOP87・自転角=GMST（平均分点 of date）。空欄=簡易モデル（t=0 で太陽=+x・グリニッジ=+x）'),
        F('start_frame', '開始フレーム', 'int', help='preview ではこのフレームだけ描画'),
        F('make_video', '完了後に mp4 作成', 'bool'),
        F('video_fps', '動画再生 fps', 'float', optional=True,
          help='mp4 の再生速度のみ（物理・時間軸には無関係）'),
    ]},
    {'title': '主物体（軌道・モデル）', 'fields': [
        F('primary_name', '名前', 'str'),
        F('satellite_model', '3Dモデル', 'file', kind='model', optional=True,
          help='未指定ならプロシージャル衛星'),
        F('satellite_model_material', '材質プリセット', 'select', options=MATERIAL_NAMES),
        F('satellite_model_keep_materials', 'モデル内蔵材質を使う', 'bool'),
        F('satellite_scale', 'スケール（地球半径=1）', 'float', step=0.0001),
        F('attitude_mode', '姿勢モード', 'select',
          options=['nadir', 'sun_tracking', 'velocity_aligned']),
        F('altitude', '高度 [km]', 'float'),
        F('eccentricity', '離心率 e', 'float', step=0.001, optional=True),
        F('inclination', '軌道傾斜角 i [deg]', 'float', optional=True),
        F('raan', '昇交点赤経 Ω [deg]', 'float', optional=True),
        F('arg_periapsis', '近地点引数 ω [deg]', 'float', optional=True),
        F('mean_anomaly', '平均近点角 M₀ [deg]', 'float', optional=True),
        F('orbit_speed', '軌道再生速度倍率', 'float'),
        F('csv_path', '軌道 CSV (ephemeris)', 'file', kind='csv', optional=True,
          help='指定すると軌道要素より優先'),
        F('propagator', '伝播器', 'select', options=['kepler', 'numerical']),
        F('use_j2', 'J2 摂動', 'bool'),
        F('use_drag', '大気抵抗', 'bool'),
    ]},
    {'title': '追加物体（objects: リスト）', 'fields': [
        F('objects', 'objects (YAML リスト)', 'yaml', rows=8,
          placeholder='- name: chief\n  csv_path: input/abs_chief_hcw.csv\n  scale: 0.0001\n- name: deputy\n  csv_path: input/abs_deputy_hcw.csv\n  scale: 0.0001',
          help='YAML のリスト形式。name / csv_path / path / material / scale / altitude 等'),
    ]},
    {'title': 'カメラ', 'fields': [
        F('view_mode', '視点モード', 'select',
          options=['inertial', 'chase', 'satellite', 'earth'],
          help='satellite = 機載カメラ（view_camera_name → view_target_name）'),
        F('view_camera_name', 'カメラ搭載物体名', 'str', optional=True),
        F('view_target_name', '注視物体名', 'str', optional=True),
        F('camera_fov', 'FOV [deg]', 'float', optional=True),
        F('camera_origin', 'カメラ位置（上書き）', 'vec3', optional=True),
        F('camera_target', '注視点（上書き）', 'vec3', optional=True),
        F('camera_up', 'up ベクトル', 'vec3', optional=True),
        F('inertial_distance', 'inertial 視点距離', 'float'),
        F('chase_back', 'chase 後方距離', 'float'),
        F('chase_out', 'chase 横距離', 'float'),
    ]},
    {'title': '地球', 'fields': [
        F('earth_texture', '昼テクスチャ', 'file', kind='texture'),
        F('earth_night_texture', '夜テクスチャ', 'file', kind='texture', optional=True),
        F('use_night_lights', '夜の街明かり', 'bool'),
        F('earth_cloud_texture', '雲テクスチャ', 'file', kind='texture', optional=True),
        F('use_clouds', '雲レイヤ', 'bool'),
        F('cloud_opacity', '雲の不透明度', 'float', step=0.05),
        F('use_atmosphere', '大気散乱', 'bool'),
        F('atmosphere_density', '大気密度', 'float', step=0.005),
        F('earth_rotation', '地球自転', 'bool'),
        F('earth_only', '地球のみ描画', 'bool'),
    ]},
    {'title': 'ライティング・トーン', 'fields': [
        F('advanced_optics', '高度な光学（推奨）', 'bool',
          help='黒体放射太陽色・星空等の物理ベースライティング'),
        F('sun_temperature', '太陽色温度 [K]', 'float'),
        F('sun_angle', '太陽黄経 [deg]', 'float', optional=True,
          help='黄道上の太陽位置（0=春分点方向 +x、90=夏至）。空欄=自動（epoch_utc 指定なら VSOP87）'),
        F('sun_rotate', '太陽を回す', 'bool'),
        F('starfield_brightness', '星空の明るさ', 'float', step=0.001),
        F('hdri_path', 'HDRI 環境マップ', 'file', kind='envmap', optional=True),
        F('show_orbit', '軌道の軌跡線を描画', 'bool'),
        F('exposure', '露出 [stop]', 'float', step=0.1),
        F('gamma', 'ガンマ', 'float', step=0.1),
    ]},
]

LIGHTCURVE_EXTRA = {'title': 'ライトカーブ / 観測者', 'fields': [
    F('light_curve_fov', '観測 FOV [deg]', 'float', optional=True,
      help='空欄=物体の見かけサイズから自動（クリップ防止）'),
    F('light_curve_samples', '観測サンプル数', 'int'),
    F('observer_lat', '観測者緯度 [deg]', 'float'),
    F('observer_lon', '観測者経度 [deg]', 'float'),
    F('observer_alt', '観測者高度 [km]', 'float'),
]}

def pv_section(hosts: List[str], extra_fields: List[Dict[str, Any]]) -> Dict[str, Any]:
    """太陽電池パネル（PV 入射照度・発電量）のフォームセクション。

    relative / groundobs で共用（verb_parsers.add_pv_arguments に対応）。
    extra_fields は verb 固有の地球モデル欄など。
    """
    host_txt = hosts[0]
    return {'title': '太陽電池パネル（PV 入射照度・発電量）', 'fields': [
        F('pv_panel_size', '仮想パネル 幅×高さ [m]', 'vec2', optional=True,
          help='指定すると 1 枚追加し、フレームごとに直達/地球照/その他 [W/m²] と'
               '発電量 [W] を pv_irradiance.csv に出力'),
        F('pv_panel_center', 'パネル中心 [m・機体系]', 'vec3'),
        F('pv_panel_normal', '受光面法線 [機体系]', 'vec3', help='片面受光'),
        F('pv_panel_up', '面内の上方向 [機体系]', 'vec3'),
        F('pv_host', '取付機体', 'select', options=list(hosts)),
        F('pv_parts', '受光面にする OBJ パーツ (YAML リスト)', 'yaml', rows=2, optional=True,
          placeholder='- hbltel_wfc_1',
          help='閉じたメッシュは表裏全フェイスで平均される'),
        F('pv_panels', '複数パネル定義 (YAML)', 'yaml', rows=6, optional=True,
          placeholder=f'- name: array\n  host: {host_txt}\n  center: [0, 4, 0]\n'
                      '  normal: [0, 1, 0]\n  size: [2.5, 6]\n  efficiency: 0.3'),
        F('pv_efficiency', '発電効率 η', 'float', step=0.01),
        F('pv_sun_irradiance_wm2', '太陽定数 [W/m²]', 'float'),
        F('pv_samples', '間接光パス数/フレーム', 'int'),
        F('pv_direct_samples', '直達 遮蔽サンプル点数', 'int'),
        F('earth_albedo_gibs', '地球アルベドマップを実データで自動生成', 'bool',
          help='MODIS 雲分率・雲光学的厚さ（NASA GIBS）+ 地表アルベドの 2 層モデル。'
               '日付ごとに runs/_gibs_cache/albedo/ にキャッシュ（要ネットワーク）'),
        F('earth_albedo_map', '既製アルベドマップ PNG', 'file', kind='texture', optional=True,
          help='earth_albedo_map.py の出力。指定時は一様アルベド/テクスチャより優先'),
        F('earth_albedo_date', 'アルベドマップの日付', 'str', optional=True,
          help='YYYY-MM-DD。空欄=フレームの UTC 日付'),
        F('earth_albedo_level', 'マップ解像度 (GIBS level)', 'int',
          help='2: 4096×2048 (~10 km/px), 3: 8192×4096'),
    ] + list(extra_fields)}


RELATIVE_SECTIONS: List[Dict[str, Any]] = [
    {'title': 'レンダリング', 'fields': [
        F('frames', 'フレーム数', 'int'),
        F('samples', 'サンプル数 (spp)', 'int'),
        F('width', '幅 [px]', 'int'),
        F('height', '高さ [px]', 'int'),
        F('fps', '時間刻み fps', 'float',
          help='dt = 1/fps。時間 [s] 欄を指定中は無視される（dt = duration/frames）'),
        F('duration_sec', 'シミュレーション時間 [s]', 'float', optional=True,
          help='指定時 dt = duration/frames'),
        F('start_time', '開始時刻 [s]', 'float'),
        F('make_video', '完了後に mp4 作成', 'bool'),
        F('video_fps', '動画再生 fps', 'float', optional=True,
          help='mp4 の再生速度のみ（物理・時間軸には無関係）'),
        F('jobs', '並列プロセス数', 'int',
          help='フレーム範囲を N 分割して並列レンダ。GPU 実行 × 多フレームで'
               'ほぼ線形にスケール（少フレームではプロセス起動が勝って逆効果）'),
    ]},
    {'title': 'モード・相対状態', 'fields': [
        F('mode', 'モード', 'select',
          options=['static', 'csv', 'tumble', 'absolute'],
          help='static=固定 / csv=時系列再生 / tumble=deputy タンブリング / '
               'absolute=絶対軌道 2 本から相対状態+環境を導出'),
        F('rel_position', 'deputy 相対位置 [km]', 'vec3'),
        F('relative_frame', '相対位置の座標系', 'select', options=['hill', 'chief'],
          help='hill=Hill/RTN（chief原点）、chief=旧形式のchief機体系'),
        F('rel_quat', '相対姿勢 [qx qy qz qw]', 'vec4',
          help='absolute+tumble では t=0 の RTN 相対初期姿勢'),
        F('rel_csv', '相対状態 CSV', 'file', kind='csv', optional=True,
          help='mode=csv 時必須（time_s,x,y,z,qx,qy,qz,qw）'),
    ]},
    {'title': '絶対軌道（mode=absolute）', 'fields': [
        F('epoch_utc', 'エポック UTC', 'str',
          help='ISO 8601。太陽位置 (VSOP87)・GMST (地球姿勢) の t=0 基準'),
        F('chief_oe', 'chief 軌道要素', 'vec6',
          help='[a_km, e, i°, Ω°, ω°, M0°]'),
        F('deputy_oe', 'deputy 軌道要素', 'vec6', optional=True,
          help='[a_km, e, i°, Ω°, ω°, M0°]（CSV 未指定なら必須）'),
        F('chief_orbit_csv', 'chief 軌道 CSV', 'file', kind='csv', optional=True,
          help='CsvEphemeris スキーマ（rad）。指定時は軌道要素より優先'),
        F('deputy_orbit_csv', 'deputy 軌道 CSV', 'file', kind='csv', optional=True),
        F('deputy_attitude', 'deputy 姿勢', 'select',
          options=['tumble', 'lvlh', 'csv'],
          help='tumble=ECI 系トルクフリー伝播 / lvlh=deputy RTN+rel_quat 固定 / '
               'csv=軌道 CSV の q1..q4。太陽方向・地心方向・高度・地球回転は'
               '軌道から自動計算（宇宙環境の該当欄は無視）'),
    ]},
    {'title': 'タンブル動力学（mode=tumble）', 'fields': [
        F('Ix', 'Ix [kg·m²]', 'float'),
        F('Iy', 'Iy [kg·m²]', 'float'),
        F('Iz', 'Iz [kg·m²]', 'float'),
        F('wx', 'ωx [rad/s]', 'float', step=0.001),
        F('wy', 'ωy [rad/s]', 'float', step=0.001),
        F('wz', 'ωz [rad/s]', 'float', step=0.001),
    ]},
    {'title': 'chief（サービサ側）', 'fields': [
        F('hide_chief', 'chief を描画しない（カメラ=機載視点）', 'bool'),
        F('chief_model', 'chief モデル', 'file', kind='model', optional=True),
        F('chief_scale', 'chief スケール', 'float', step=0.0001),
        F('chief_position', 'chief 位置 [km]', 'vec3', readonly=True, help='Hill座標系の原点 [0, 0, 0] に固定'),
        F('chief_parts', 'パーツ別 BSDF (YAML)', 'yaml', rows=6, optional=True),
        F('chief_bsdf', 'デフォルト BSDF (YAML)', 'yaml', rows=4, optional=True),
        F('chief_quat', 'chief 姿勢 [qx qy qz qw]', 'vec4'),
    ]},
    {'title': 'deputy（ターゲット側）', 'fields': [
        F('deputy_model', 'deputy モデル', 'file', kind='model', optional=True),
        F('deputy_scale', 'deputy スケール', 'float', step=0.0001,
          help='モデル単位が m なら 0.001（シーンは km 単位）'),
        F('deputy_parts', 'パーツ別 BSDF (YAML)', 'yaml', rows=8,
          placeholder='hbltel_3:\n  type: principled\n  base_color: {type: bitmap, filename: models/hubble_textures/hbltel_3.png}\n  metallic: 0.85\n  roughness: 0.28',
          help="OBJ の 'o' パーツ名 → Mitsuba BSDF 辞書"),
        F('deputy_bsdf', 'デフォルト BSDF (YAML)', 'yaml', rows=4, optional=True),
    ]},
    pv_section(['deputy', 'chief'], [
        F('pv_include_env', '環境光も計測に含める', 'bool',
          help='既定は除外（既定の淡い環境光は可視化用で物理量ではない）'),
        F('earth_uniform_albedo', '地球を一様アルベド球にする', 'float', step=0.01, optional=True,
          help='空欄=テクスチャ地球。0.3 で雲込み平均アルベドのランバート球（解析検証用）'),
    ]),
    {'title': 'カメラ', 'fields': [
        F('camera_mode', 'カメラモード', 'select', options=['manual', 'chief_to_deputy', 'chief_fixed', 'deputy_to_chief', 'deputy_fixed']),
        F('camera_offset', '取付位置 [km・機体系]', 'vec3'),
        F('camera_direction', '固定視線方向・機体系', 'vec3', help='fixed のときのみ使用'),
        F('camera_body_up', '上方向・機体系', 'vec3'),
        F('camera_origin', 'カメラ位置 [km]', 'vec3'),
        F('camera_target', '注視点 [km]', 'vec3'),
        F('camera_fov', 'FOV [deg]', 'float'),
        F('camera_up', 'up ベクトル', 'vec3'),
    ]},
    {'title': '宇宙環境（フォトリアル）', 'fields': [
        F('show_earth', '地球を背景に描画', 'bool'),
        F('earth_direction', '地心方向（シーン座標）', 'vec3'),
        F('earth_altitude_km', '高度 [km]', 'float'),
        F('earth_texture', '地球テクスチャ', 'file', kind='texture'),
        F('earth_rotation_deg', '地球テクスチャ回転 [deg]', 'float'),
        F('earth_gibs', 'GIBS 実写画像（雲込み・要ネット）', 'bool',
          help='NASA GIBS から可視域だけオンデマンド取得（MODIS 250 m/px、'
               '実写日次。取得失敗時はローカルテクスチャへ自動フォールバック）'),
        F('earth_gibs_date', 'GIBS 撮像日', 'str', optional=True,
          help='YYYY-MM-DD（未指定は昨日 UTC。Terra は 2000-02-24〜今日）'),
        F('earth_gibs_layer', 'GIBS レイヤ', 'str', optional=True,
          help='例: VIIRS_SNPP_CorrectedReflectance_TrueColor'),
        F('sun_direction', '太陽光の進行方向', 'vec3'),
        F('sun_irradiance', '太陽強度', 'float', step=0.1),
        F('sun_temperature', '太陽色温度 [K]', 'float', optional=True,
          help='例 5778。未指定は従来色'),
        F('starfield', '星空 envmap (EXR)', 'file', kind='envmap', optional=True),
        F('env_brightness', '環境光/星空の明るさ', 'float', step=0.005, optional=True),
        F('max_depth', '最大バウンス数', 'int'),
    ]},
    {'title': '可視化補助', 'fields': [
        F('show_inertial_axes', '慣性軸を描画', 'bool'),
        F('show_body_axes', '機体軸を描画', 'bool'),
    ]},
]

ROTATION_SECTIONS: List[Dict[str, Any]] = [
    {'title': 'レンダリング', 'fields': [
        F('frames', 'フレーム数', 'int'),
        F('samples', 'サンプル数 (spp)', 'int'),
        F('width', '幅 [px]', 'int'),
        F('height', '高さ [px]', 'int'),
        F('fps', '時間刻み fps', 'float', help='dt = 1/fps'),
    ]},
    {'title': 'モデル', 'fields': [
        F('model_path', '3Dモデル', 'file', kind='model', optional=True),
        F('model_scale', 'スケール', 'float', step=0.01),
        F('model_parts', 'パーツ別 BSDF (YAML)', 'yaml', rows=8, optional=True),
        F('model_bsdf', 'デフォルト BSDF (YAML)', 'yaml', rows=4, optional=True),
    ]},
    {'title': '回転動力学', 'fields': [
        F('Ix', 'Ix [kg·m²]', 'float'),
        F('Iy', 'Iy [kg·m²]', 'float'),
        F('Iz', 'Iz [kg·m²]', 'float'),
        F('wx', 'ωx [rad/s]', 'float', step=0.01),
        F('wy', 'ωy [rad/s]', 'float', step=0.01),
        F('wz', 'ωz [rad/s]', 'float', step=0.01),
    ]},
    {'title': 'カメラ', 'fields': [
        F('camera_origin', 'カメラ位置', 'vec3'),
        F('camera_target', '注視点', 'vec3'),
        F('camera_fov', 'FOV [deg]', 'float'),
    ]},
]


GROUNDOBS_SECTIONS: List[Dict[str, Any]] = [
    {'title': '時間軸（UTC エポック）', 'fields': [
        F('epoch_utc', 'エポック UTC (ISO 8601)', 'str',
          help='t=0 の UTC 時刻。GMST・太陽位置の基準'),
        F('frames', 'フレーム数', 'int'),
        F('fps', '時間刻み fps', 'float',
          help='dt = 1/fps。時間 [s] 欄を指定中は無視される（dt = duration/frames）'),
        F('duration_sec', '総観測時間 [s]', 'float', optional=True,
          help='指定時 dt = duration/frames'),
        F('start_time', '開始オフセット [s]', 'float'),
    ]},
    {'title': '観測地（地上局）', 'fields': [
        F('site_lat', '緯度 [deg]（北+）', 'float', step=0.0001),
        F('site_lon', '経度 [deg]（東+）', 'float', step=0.0001),
        F('site_alt_m', '標高 [m]', 'float'),
        F('min_elevation_deg', '最低仰角 [deg]', 'float'),
    ]},
    {'title': '軌道', 'fields': [
        F('tle', 'TLE ファイル', 'str', optional=True,
          help='SGP4 伝播（TEME≈ECI 近似）。CSV・ケプラー要素より優先。'
               'epoch 未指定時は TLE エポックを t=0 に採用'),
        F('tle_name', 'TLE 衛星名（部分一致）', 'str', optional=True),
        F('orbit_csv', '軌道 CSV (ephemeris)', 'file', kind='csv', optional=True,
          help='指定時はケプラー要素より優先'),
        F('altitude_km', '円軌道高度 [km]', 'float'),
        F('semi_major_axis_km', '軌道長半径 a [km]', 'float', optional=True),
        F('eccentricity', '離心率 e', 'float', step=0.001),
        F('inclination_deg', '軌道傾斜角 i [deg]', 'float'),
        F('raan_deg', 'RAAN Ω [deg]', 'float'),
        F('arg_periapsis_deg', '近地点引数 ω [deg]', 'float'),
        F('mean_anomaly_deg', '平均近点角 M₀ [deg]', 'float'),
        F('start_overhead', 't=0 で天頂パスに整列', 'bool',
          help='Ω と M₀ を自動調整（デモ・可視パス作成用）'),
    ]},
    {'title': '姿勢', 'fields': [
        F('attitude_mode', '姿勢モード', 'select',
          options=['nadir', 'sun_tracking', 'velocity_aligned', 'tumble']),
        F('Ix', 'Ix [kg·m²]', 'float'),
        F('Iy', 'Iy [kg·m²]', 'float'),
        F('Iz', 'Iz [kg·m²]', 'float'),
        F('wx', 'ωx [rad/s]', 'float', step=0.001),
        F('wy', 'ωy [rad/s]', 'float', step=0.001),
        F('wz', 'ωz [rad/s]', 'float', step=0.001),
    ]},
    {'title': 'ターゲット', 'fields': [
        F('model_path', '3Dモデル', 'file', kind='model', optional=True,
          help='未指定なら拡散球ターゲット'),
        F('model_scale', 'モデル単位→km 倍率', 'float', step=0.0001,
          help='m 単位モデルなら 0.001'),
        F('model_parts', 'パーツ別 BSDF (YAML)', 'yaml', rows=8, optional=True),
        F('model_bsdf', 'デフォルト BSDF (YAML)', 'yaml', rows=4, optional=True),
        F('sphere_radius_m', '球半径 [m]', 'float', step=0.1),
        F('sphere_albedo', '球アルベド', 'float', step=0.01),
    ]},
    {'title': '観測モード・恒星背景', 'fields': [
        F('tracking_mode', '追尾モード', 'select', options=['target', 'sidereal'],
          help='target=物体追尾（恒星が流れる）/ sidereal=恒星時追尾（物体がストリーク）'),
        F('show_stars', '恒星背景を描画', 'bool'),
        F('star_catalog', '恒星カタログ (npz)', 'str'),
        F('star_mag_limit', '恒星の限界等級', 'float', step=0.5),
        F('refraction', '大気屈折を適用', 'bool'),
    ]},
    {'title': '望遠鏡・大気', 'fields': [
        F('pixel_scale_arcsec', 'ピクセルスケール ["/px]', 'float', step=0.01),
        F('sensor_px', 'センサ一辺 [px]', 'int'),
        F('supersample', '内部スーパーサンプル', 'int'),
        F('seeing_arcsec', 'シーイング FWHM ["]', 'float', step=0.1),
        F('extinction_k', '減光係数 k [mag/airmass]', 'float', step=0.01),
        F('samples', 'サンプル数 (spp)', 'int'),
        F('max_depth', '最大バウンス数', 'int'),
        F('sun_temperature', '太陽色温度 [K]', 'float'),
        F('sun_irradiance_wm2', '太陽定数 [W/m²]', 'float'),
    ]},
    {'title': 'センサノイズ (CCD)', 'fields': [
        F('sensor_noise', 'CCD ノイズモデルを適用', 'bool'),
        F('aperture_m', '口径 [m]', 'float', step=0.01),
        F('throughput', 'スループット', 'float', step=0.01),
        F('quantum_efficiency', 'QE', 'float', step=0.01),
        F('exposure_s', '露光時間 [s]', 'float', step=0.001),
        F('read_noise_e', '読み出しノイズ [e⁻]', 'float', step=0.1),
        F('gain_e_per_adu', 'ゲイン [e⁻/ADU]', 'float', step=0.1),
        F('full_well_e', 'フルウェル [e⁻]', 'float'),
        F('sky_mag_arcsec2', '夜空輝度 [mag/arcsec²]', 'float', step=0.1),
        F('noise_seed', 'ノイズシード', 'int'),
    ]},
    pv_section(['target'], [
        F('earth_uniform_albedo', '地球アルベド（一様ランバート球）', 'float', step=0.01,
          help='雲込み全球平均 0.29〜0.30（CERES）。earth_texture 指定時は無視'),
        F('earth_texture', '地球テクスチャ', 'file', kind='texture', optional=True,
          help='指定時は可視域クロップ + GMST 姿勢のテクスチャ地球（BMNG は雲なしで暗く、地球照は下限値）'),
        F('earthshine', '観測像にも地球照を含める', 'bool',
          help='物体への照り返しを測光・像に乗せる（既定 OFF = 太陽のみ）'),
    ]),
    {'title': '出力', 'fields': [
        F('stretch', 'ストレッチ', 'select', options=['asinh', 'linear', 'log']),
        F('stretch_percentile', 'ストレッチ percentile', 'float', step=0.1),
        F('save_linear', 'リニア画像 (.npy) も保存', 'bool'),
        F('make_video', '完了後に mp4 作成', 'bool'),
        F('video_fps', '動画再生 fps', 'float', optional=True,
          help='mp4 の再生速度のみ（物理・時間軸には無関係）'),
    ]},
]


def missing_default_keys(sections: List[Dict[str, Any]],
                         defaults: Dict[str, Any]) -> List[str]:
    """フォームスキーマの必須フィールドのうち defaults に無いキーを列挙する。

    defaults は argparse 定義から自動導出しているので、ここが空でなければ
    「フォームに出しているのに verb 側に dest が無い」設定ミスを意味する。

    検査から外すもの:
      - `type: yaml` のフィールド（model_parts / deputy_parts 等の dict ブロック。
        argparse には dest が無く YAML からのみ渡る仕様）
      - `optional=True` のフィールド（未指定＝None が正常な値）
    """
    return [field['key']
            for section in sections
            for field in section['fields']
            if field['key'] not in defaults
            and field.get('type') != 'yaml'
            and not field.get('optional')]


# 既定値を argparse から自動導出している verb。ここだけカバレッジを検査する
# （render 系は config_loader._ARG_DEFAULTS 由来で、軌道要素など「未設定が
#  既定」の項目を意図的に持たない）。
_AUTO_DEFAULT_VERBS = ('relative', 'rotation', 'groundobs')


def _warn_schema_gaps() -> None:
    """起動時に defaults のカバレッジを検査し、欠けがあれば警告する。"""
    schema = build_verb_schema()
    for verb in _AUTO_DEFAULT_VERBS:
        missing = missing_default_keys(schema[verb]['sections'],
                                       schema[verb]['defaults'])
        if missing:
            print(f'⚠ gui_server: verb={verb} のフォーム項目に既定値がありません: '
                  f'{", ".join(missing)}（verb_parsers の argparse 定義を確認）',
                  file=sys.stderr)


def build_verb_schema() -> Dict[str, Any]:
    """verb → {label, sections, defaults} の GUI スキーマを構築する。"""
    common_defaults = dict(_ARG_DEFAULTS)
    lightcurve_sections = COMMON_SECTIONS[:1] + [LIGHTCURVE_EXTRA] + COMMON_SECTIONS[1:]
    return {
        'render': {
            'label': 'render（軌道レンダ）',
            'desc': 'ケプラー/数値伝播軌道 + 姿勢でフレーム連番を出力',
            'sections': COMMON_SECTIONS, 'defaults': common_defaults,
        },
        'preview': {
            'label': 'preview（1フレーム）',
            'desc': '開始フレームだけ即レンダ（材質・構図調整用）',
            'sections': COMMON_SECTIONS, 'defaults': common_defaults,
        },
        'onboard': {
            'label': 'onboard（機載カメラ）',
            'desc': '別物体を機載カメラで注視（view_mode=satellite 既定）',
            'sections': COMMON_SECTIONS,
            'defaults': {**common_defaults, 'view_mode': 'satellite'},
        },
        'lightcurve': {
            'label': 'lightcurve（光度曲線）',
            'desc': '地上観測者から見た明るさの時系列を CSV 出力',
            'sections': lightcurve_sections, 'defaults': common_defaults,
        },
        'relative': {
            'label': 'relative（2機の相対配置）',
            'desc': '相対状態または2機の絶対軌道から近接撮像（OOS）',
            'sections': RELATIVE_SECTIONS, 'defaults': RELATIVE_DEFAULTS,
        },
        'modelview': {
            'label': 'モデル確認',
            'desc': 'ブラウザ内でモデルを直接表示（レンダリング待ちなし）',
            'defaults': {'display_mode': 'solid', 'keep_materials': True},
            'sections': [{'title': '表示', 'fields': [
                F('display_mode', '描画方法', 'select', options=['solid', 'overlay', 'wire']),
                F('keep_materials', 'モデルの材質を使用', 'bool'),
            ]}],
        },
        'rotation': {
            'label': 'rotation（単機タンブリング）',
            'desc': '軌道なし。オイラー回転運動方程式で単機を回す',
            'sections': ROTATION_SECTIONS, 'defaults': ROTATION_DEFAULTS,
        },
        'groundobs': {
            'label': 'groundobs（地上望遠鏡観測）',
            'desc': '地上局からの見かけ等級ライトカーブ + 望遠鏡センサ像（点像〜分解像）',
            'sections': GROUNDOBS_SECTIONS, 'defaults': GROUNDOBS_DEFAULTS,
        },
    }


# ---------------------------------------------------------------------------
# ファイル一覧・プリセット読み込み
# ---------------------------------------------------------------------------
def list_files() -> Dict[str, Any]:
    """ドロップダウン用のファイル候補一覧を返す（プロジェクト相対パス）。"""
    def rel(paths):
        return sorted(str(p.relative_to(ROOT)) for p in paths)
    models = [p for ext in ('*.obj', '*.ply', '*.glb', '*.gltf')
              for p in (ROOT / 'models').glob(ext)]
    models += [p for ext in ('*.obj', '*.ply', '*.glb', '*.gltf')
               for p in (ROOT / 'models/library').rglob(ext)]
    # 配布に含めないモデル（gitignore 対象、無ければ空）
    if (ROOT / 'internal' / 'models').is_dir():
        models += [p for ext in ('*.obj', '*.ply', '*.glb', '*.gltf')
                   for p in (ROOT / 'internal' / 'models').glob(ext)]
    from model_materials import library_metadata
    model_labels = {}
    for path in models:
        meta = library_metadata(path)
        if meta and meta.get('label'):
            model_labels[str(path.relative_to(ROOT))] = meta['label']
    textures = [p for pat in ('*.jpg', '*.jpeg', '*.png')
                for p in ROOT.glob(pat)]
    textures += [p for pat in ('*.jpg', '*.png', '*.exr')
                 for d in ('models/cloud_textures', 'assets', 'assets/textures')
                 for p in (ROOT / d).glob(pat) if (ROOT / d).exists()]
    csvs = list((ROOT / 'input').glob('*.csv'))
    envmaps = [p for d in ('assets',) for p in (ROOT / d).glob('*.exr')]
    presets = preset_paths()
    return {
        'model': rel(models), 'model_labels': model_labels, 'texture': rel(textures), 'csv': rel(csvs),
        'envmap': rel(envmaps), 'preset': rel(presets),
    }


# モデルの置き場。/model-assets/ と /api/model-info はこの配下だけを配信する
MODEL_ROOTS = ('models', 'internal/models')


def model_root_of(path: Path) -> Optional[Path]:
    """path が MODEL_ROOTS のいずれかの配下ならそのルート（resolve 済み）を返す。"""
    for rel in MODEL_ROOTS:
        root = (ROOT / rel).resolve()
        if path.is_relative_to(root):
            return root
    return None


def model_asset_path(rel: str) -> Optional[Path]:
    """/model-assets/<rel> の rel を実ファイルへ解決する。rel はリポジトリ相対
    （models/x.obj, internal/models/x.obj）と、従来の models/ 相対（x.obj、
    hubble_textures/x.png）の両方を受け付ける。置き場の外なら None。"""
    for base in (ROOT, ROOT / 'models'):
        path = (base / rel).resolve()
        if model_root_of(path) is not None and path.is_file():
            return path
    return None


def model_asset_url(path: Path) -> str:
    return '/model-assets/' + urllib.parse.quote(str(path.resolve().relative_to(ROOT.resolve())))


def preset_paths() -> List[Path]:
    """プリセット YAML の探索先。`internal/presets/`（非公開・gitignore 対象）は
    存在すれば併せて列挙する（配布版には無いので、その場合は `presets/` のみ）。"""
    paths = list((ROOT / 'presets').glob('*.yaml'))
    internal = ROOT / 'internal' / 'presets'
    if internal.is_dir():
        paths += list(internal.glob('*.yaml'))
    return paths


def flatten_simple_yaml(paths: List[Path]) -> Dict[str, Any]:
    """relative/rotation 系の 1 段フラット化。

    実装は `config_loader.load_flat_yaml_config`（relative_motion /
    simple_rotation の `load_yaml_config` と同一実体）。ここは後方互換のための
    薄い別名。
    """
    return load_flat_yaml_config(paths)


GUI_META_PREFIX = '# mrender-gui: '


def preset_ui_metadata() -> Dict[str, list]:
    """GUI 保存プリセットの分類。YAML コメントなのでレンダラ設定に混入しない。"""
    result = {}
    for path in preset_paths():
        try:
            with path.open() as fh:
                first = fh.readline()
            if first.startswith(GUI_META_PREFIX):
                meta = json.loads(first[len(GUI_META_PREFIX):])
                if valid_preset_ui_metadata(meta):
                    result[str(path.relative_to(ROOT))] = meta
        except (OSError, UnicodeError, ValueError):
            continue
    return result


def valid_preset_ui_metadata(meta) -> bool:
    return (isinstance(meta, list) and len(meta) == 4
            and all(isinstance(v, str) for v in meta)
            and meta[0] in ('absolute', 'relative', 'ground', 'rotation')
            and meta[3] in ('render', 'onboard', 'preview', 'lightcurve',
                            'relative', 'groundobs', 'rotation'))


def load_preset_flat(path: Path, verb: str) -> Dict[str, Any]:
    """プリセット YAML を GUI フォーム用のフラット辞書へ変換する。"""
    if verb in ('relative', 'rotation', 'groundobs'):
        return flatten_simple_yaml([path])
    from config_loader import load_yaml_config
    return load_yaml_config([str(path)])


# ---------------------------------------------------------------------------
# 設定 → YAML 出力
# ---------------------------------------------------------------------------
def compose_config(fields: Dict[str, Any], yaml_blocks: Dict[str, str]) -> Dict[str, Any]:
    """フォーム値と YAML テキストブロックから最終 config 辞書を作る。

    dict 値のキー（model_parts 等）は `advanced:` セクションへ包む。
    どちらのローダ（config_loader / relative_motion）でも 1 段フラット化で
    元のキー名に戻る。
    """
    doc: Dict[str, Any] = {}
    advanced: Dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, dict):
            advanced[key] = value
        else:
            doc[key] = value
    for key, text in (yaml_blocks or {}).items():
        text = (text or '').strip()
        if not text:
            continue
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f'{key} の YAML が不正です: {exc}')
        if parsed is None:
            continue
        if key == 'objects':
            if not isinstance(parsed, list):
                raise ValueError('objects は YAML リストで指定してください')
            doc['objects'] = parsed
        elif isinstance(parsed, dict):
            advanced[key] = parsed
        else:
            doc[key] = parsed
    if advanced:
        doc['advanced'] = advanced
    return doc


# ---------------------------------------------------------------------------
# ジョブ管理
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# リモート実行マシン（gui_hosts.json）
# ---------------------------------------------------------------------------
# ジョブ・ライブプレビューをリモートホスト（DGX Spark 等）で実行するための
# 定義。ROOT/gui_hosts.json の形式:
#   {"dgx": {"ssh": "dgx", "dir": "mRendering",
#                "variant": "cuda_ad_rgb,llvm_ad_rgb"}}
# ssh は ~/.ssh/config のホスト名（鍵認証・パスフレーズなしで繋がること）、
# dir はリモートのリポジトリルート（$HOME 相対）、variant は MRENDER_VARIANT。
# リモート側には tools/dgx/sync_to_dgx.sh 等でコードが同期済みであること。
# ジョブの runs/ はローカルへ rsync で自動ミラーされるので、進捗・最新
# フレーム・成果物リンクはローカル実行と同じ UI で見える。
REMOTE_HOSTS: Dict[str, Dict[str, str]] = {}


def load_remote_hosts() -> None:
    """gui_hosts.json を読み込む。自ホスト名と同名のエントリは除外する
    （DGX 上で GUI を動かしたときに dgx→dgx の自己 SSH を出さない）。"""
    REMOTE_HOSTS.clear()
    path = ROOT / 'gui_hosts.json'
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f'gui_hosts.json 読込エラー（リモート実行は無効）: {exc}', file=sys.stderr)
        return
    myhost = socket.gethostname().split('.')[0].lower()
    for name, spec in (data or {}).items():
        if not isinstance(spec, dict) or not spec.get('ssh'):
            continue
        if name.lower() == myhost:
            continue
        REMOTE_HOSTS[name] = {
            'ssh': str(spec['ssh']),
            'dir': str(spec.get('dir', 'mRendering')),
            'variant': str(spec.get('variant', 'cuda_ad_rgb,llvm_ad_rgb')),
        }


def resolve_machine(machine: str) -> Optional[Dict[str, str]]:
    """マシン名 → リモート定義（local なら None）。未知の名前は ValueError。"""
    if machine in ('', 'local', None):
        return None
    remote = REMOTE_HOSTS.get(machine)
    if remote is None:
        raise ValueError(f'未知の実行マシン: {machine}')
    return remote


JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()
_JOB_SEQ = [0]


def start_job(verb: str, name: str, config: Dict[str, Any],
              machine: str = 'local') -> Dict[str, Any]:
    """設定 YAML を書き出し、mrender サブプロセスを起動する。

    machine がリモート（gui_hosts.json のエントリ）の場合は、YAML を scp で
    送って `ssh -tt` 経由でリモート実行し、runs/<run> をローカルへ rsync で
    ミラーするスレッドを回す。以降の進捗・フレーム表示はローカル実行と共通。
    """
    remote = resolve_machine(machine)
    ts = time.strftime('%Y%m%d_%H%M%S')
    slug = _SLUG_RE.sub('_', (name or verb)).strip('_') or verb
    run_name = f'{ts}_{verb}_{slug}'
    run_dir = RUNS_DIR / run_name
    config = dict(config)
    # リモートではリポジトリルート相対で書かせる（絶対パスは Mac 側のもの）
    config['output_dir'] = f'runs/{run_name}' if remote else str(run_dir)

    GUI_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = GUI_CONFIG_DIR / f'{run_name}.yaml'
    with cfg_path.open('w') as fh:
        yaml.safe_dump(config, fh, sort_keys=True, allow_unicode=True)

    log_path = run_dir / 'gui_job.log'
    log_fh = log_path.open('w')
    if remote is None:
        cmd = [sys.executable, str(ROOT / 'mrender.py'), verb,
               '--config', str(cfg_path)]
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log_fh,
                                stderr=subprocess.STDOUT)
    else:
        rdir = remote['dir']
        cfg_rel = f'runs/_gui_configs/{cfg_path.name}'
        # 送信先ディレクトリを確保してから YAML を転送
        subprocess.run(
            ['ssh', remote['ssh'],
             f'mkdir -p {rdir}/runs/_gui_configs {rdir}/runs/{run_name}'],
            check=True, timeout=20, capture_output=True)
        subprocess.run(
            ['scp', '-q', str(cfg_path), f"{remote['ssh']}:{rdir}/{cfg_rel}"],
            check=True, timeout=30, capture_output=True)
        # -tt: 強制 pty 割り当て。ローカルの ssh が死ぬとリモート側に HUP が
        # 届き、レンダプロセスが孤児として残らない
        rcmd = (f"cd {rdir} && MRENDER_VARIANT={remote['variant']} "
                f"exec venv/bin/python mrender.py {verb} --config {cfg_rel}")
        cmd = ['ssh', '-tt', remote['ssh'], rcmd]
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log_fh,
                                stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)

    with _JOBS_LOCK:
        _JOB_SEQ[0] += 1
        job_id = f'job{_JOB_SEQ[0]}'
        JOBS[job_id] = {
            'id': job_id, 'verb': verb, 'name': slug,
            'run_dir': str(run_dir), 'run_name': run_name,
            'config_path': str(cfg_path),
            'log_path': str(log_path), 'proc': proc, 'log_fh': log_fh,
            'total_frames': _expected_frames(verb, config),
            'started_at': ts, 'cmd': ' '.join(cmd),
            'machine': machine if remote else 'local', 'remote': remote,
            'synced': remote is None,
        }
    if remote is not None:
        threading.Thread(target=_mirror_remote_run, args=(job_id,),
                         daemon=True).start()
    return job_status(job_id)


def _mirror_remote_run(job_id: str) -> None:
    """リモートジョブの runs/<run> をローカルへ定期 rsync する。

    プロセス生存中は ~3 秒間隔、終了後に最終ミラー（mp4 / manifest 含む）を
    行ってから job['synced'] を立てる（それまで status は 'syncing'）。"""
    with _JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        return
    proc: subprocess.Popen = job['proc']
    remote = job['remote']
    src = f"{remote['ssh']}:{remote['dir']}/runs/{job['run_name']}/"
    dst = job['run_dir'] + '/'

    def sync_once() -> None:
        subprocess.run(['rsync', '-az', '--timeout=20', src, dst],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    while proc.poll() is None:
        sync_once()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            pass
    sync_once()
    job['synced'] = True


def _expected_frames(verb: str, config: Dict[str, Any]) -> Optional[int]:
    """進捗率計算用の総フレーム数を見積もる。"""
    if verb == 'preview':
        return 1
    if verb == 'lightcurve':
        return None
    frames = config.get('frames')
    start = int(config.get('start_frame') or 0)
    end = config.get('end_frame')
    if end is not None:
        return int(end) - start
    if frames is not None:
        return int(frames) - (start if verb in ('render', 'onboard') else 0)
    return None


def job_status(job_id: str) -> Optional[Dict[str, Any]]:
    """ジョブの状態スナップショットを返す（進捗・ログ末尾・最新フレーム）。"""
    with _JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        return None
    proc: subprocess.Popen = job['proc']
    code = proc.poll()
    if code is None:
        status = 'running'
    elif job.get('cancelled'):
        # ユーザー停止（ローカル: SIGTERM = -15 / リモート: ssh 死亡 = 255）を
        # エラーと区別する
        status = 'cancelled'
    elif not job.get('synced', True):
        # リモートジョブ: プロセスは終わったが最終 rsync がまだ
        status = 'syncing'
    elif code == 0:
        status = 'done'
    else:
        status = f'error (exit {code})'

    run_dir = Path(job['run_dir'])
    frames_dir = run_dir / 'frames'
    pngs = sorted(frames_dir.glob('frame_*.png')) if frames_dir.exists() else []
    latest = str(pngs[-1].relative_to(ROOT)) if pngs else None
    total = job['total_frames']
    progress = (len(pngs) / total) if total else None

    log_tail = ''
    try:
        text = Path(job['log_path']).read_text(errors='replace')
        log_tail = '\n'.join(text.splitlines()[-30:])
    except OSError:
        pass

    video = run_dir / str('output.mp4')
    outputs = []
    for p in sorted(run_dir.glob('*.mp4')) + sorted(run_dir.glob('*.csv')):
        outputs.append(str(p.relative_to(ROOT)))
    return {
        'id': job_id, 'verb': job['verb'], 'name': job['name'],
        'status': status, 'progress': progress,
        'frames_done': len(pngs), 'total_frames': total,
        'run_dir': str(run_dir.relative_to(ROOT)),
        'latest_frame': latest, 'outputs': outputs,
        'log_tail': log_tail, 'started_at': job['started_at'],
        'cmd': job['cmd'], 'machine': job.get('machine', 'local'),
    }


def cancel_job(job_id: str) -> bool:
    with _JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        return False
    if job['proc'].poll() is None:
        job['cancelled'] = True
    remote = job.get('remote')
    if remote is not None and job['proc'].poll() is None:
        # ssh -tt の HUP 伝搬だけに頼らず、設定 YAML 名（タイムスタンプ入りで
        # 一意）で明示 pkill する。先頭文字を [ ] で囲み、リモートの bash 自身の
        # コマンドラインにパターンがマッチして自殺するのを防ぐ
        base = Path(job['config_path']).name
        pattern = f'[{base[0]}]{base[1:]}'
        try:
            subprocess.run(['ssh', remote['ssh'], f"pkill -f '{pattern}'"],
                           timeout=15, capture_output=True)
        except (subprocess.SubprocessError, OSError):
            pass
    if job['proc'].poll() is None:
        job['proc'].terminate()
    return True


def list_runs(limit: int = 40) -> List[Dict[str, Any]]:
    """runs/ 配下の過去ランを新しい順に一覧する。"""
    entries = []
    if not RUNS_DIR.exists():
        return entries
    for d in sorted(RUNS_DIR.iterdir(), reverse=True):
        if not d.is_dir() or d.name.startswith('_'):
            continue
        manifest = {}
        mpath = d / 'manifest.json'
        if mpath.exists():
            try:
                manifest = json.loads(mpath.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        frames = sorted((d / 'frames').glob('frame_*.png')) if (d / 'frames').exists() else []
        videos = sorted(d.glob('*.mp4'))
        entries.append({
            'name': d.name,
            'verb': manifest.get('verb'),
            'status': manifest.get('status'),
            'duration_s': manifest.get('duration_s'),
            'frame_count': len(frames),
            'thumb': str(frames[-1].relative_to(ROOT)) if frames else None,
            'video': str(videos[0].relative_to(ROOT)) if videos else None,
            'path': str(d.relative_to(ROOT)),
        })
        if len(entries) >= limit:
            break
    return entries


# ---------------------------------------------------------------------------
# ライブプレビュー ワーカー（live_worker.py）の管理とプロキシ
# ---------------------------------------------------------------------------
# Mitsuba を常駐 import した別プロセス（live_worker.py）を遅延起動し、
# /api/live/* を単純に中継する。gui_server 自身は Mitsuba を import しない。
LIVE: Dict[str, Any] = {
    'port': None,       # main() で決定（既定 gui port + 1）
    'host': '127.0.0.1',
    'proc': None,
    'log_fh': None,
    'machine': 'local',  # 'local' か gui_hosts.json のエントリ名
}
_LIVE_LOCK = threading.RLock()
LIVE_START_TIMEOUT_S = 25.0        # Mitsuba の import があるので長め
LIVE_START_TIMEOUT_REMOTE_S = 60.0  # + ssh 接続とリモート Mitsuba import


def live_base_url() -> str:
    return f"http://{LIVE['host']}:{LIVE['port']}"


def _live_get(path: str, timeout: float = 15.0):
    """ワーカーへ GET し、(status, headers, body) を返す。"""
    req = urllib.request.Request(live_base_url() + path, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def _live_post(path: str, payload: Any, timeout: float = 15.0):
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(live_base_url() + path, data=body, method='POST',
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def live_worker_alive() -> bool:
    proc = LIVE.get('proc')
    return proc is not None and proc.poll() is None


def available_live_port(host: str, preferred: int) -> int:
    """Use the preferred port if free, otherwise avoid an orphan worker/tunnel."""
    for requested in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, requested))
                port = sock.getsockname()[1]
                if port <= 65435:  # remote listener uses port + 100
                    return port
            except OSError:
                if requested == 0:
                    raise
    raise RuntimeError('ライブワーカー用の空きポートを確保できません')


def ensure_live_worker() -> None:
    """Serialize startup through verified readiness, including remote tunnels."""
    with _LIVE_LOCK:
        if live_worker_alive() and LIVE.get('ready'):
            return
        old = LIVE.get('proc')
        if old is not None and old.poll() is None:
            old.terminate()
            old.wait(timeout=3)
        old_fh = LIVE.get('log_fh')
        if old_fh is not None:
            old_fh.close()
        GUI_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        machine = LIVE.get('machine', 'local')
        remote = REMOTE_HOSTS.get(machine)
        LIVE['port'] = available_live_port(LIVE['host'], LIVE['port'])
        token = secrets.token_hex(16)
        LIVE['ready'] = False
        log_fh = (GUI_CONFIG_DIR / 'live_worker.log').open('a')
        log_fh.write(f'\n=== live_worker start {time.strftime("%Y-%m-%d %H:%M:%S")} '
                     f'port={LIVE["port"]} machine={machine} ===\n')
        log_fh.flush()
        if remote is None:
            cmd = [sys.executable, str(ROOT / 'live_worker.py'), '--port', str(LIVE['port']),
                   '--host', str(LIVE['host']), '--instance-token', token]
            stdin = None
        else:
            port = LIVE['port']
            rport = port + 100
            rcmd = (f"cd {shlex.quote(remote['dir'])} && MRENDER_VARIANT={shlex.quote(remote['variant'])} "
                    f"exec venv/bin/python live_worker.py --port {rport} --host 127.0.0.1 "
                    f"--instance-token {token}")
            cmd = ['ssh', '-tt', '-o', 'ExitOnForwardFailure=yes',
                   '-L', f'{LIVE["host"]}:{port}:127.0.0.1:{rport}', remote['ssh'], rcmd]
            stdin = subprocess.DEVNULL
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log_fh,
                                stderr=subprocess.STDOUT, stdin=stdin)
        LIVE['proc'], LIVE['log_fh'] = proc, log_fh
        timeout = LIVE_START_TIMEOUT_REMOTE_S if remote is not None else LIVE_START_TIMEOUT_S
        deadline = time.monotonic() + timeout
        last_exc = None
        try:
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(f'live_worker が起動直後に終了しました (exit {proc.returncode})。'
                                       'ログ: runs/_gui_configs/live_worker.log')
                try:
                    status, _h, body = _live_get('/health', timeout=2.0)
                    health = json.loads(body.decode('utf-8'))
                    if (status == 200 and health.get('ok')
                            and health.get('instance_token') == token and proc.poll() is None):
                        LIVE['ready'] = True
                        return
                except Exception as exc:
                    last_exc = exc
                time.sleep(.25)
            raise RuntimeError(f'live_worker の起動待ちがタイムアウトしました: {last_exc}')
        except Exception:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            log_fh.close()
            LIVE['proc'] = LIVE['log_fh'] = None
            raise


def stop_live_worker() -> bool:
    """ワーカーへ /shutdown を投げ、保険で terminate する。"""
    stopped = False
    with _LIVE_LOCK:
        proc = LIVE.get('proc')
        if proc is None:
            return False
        if proc.poll() is None:
            try:
                _live_post('/shutdown', {}, timeout=3.0)
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
            stopped = True
        log_fh = LIVE.get('log_fh')
        if log_fh is not None:
            try:
                log_fh.close()
            except OSError:
                pass
        LIVE['proc'] = None
        LIVE['log_fh'] = None
        LIVE['ready'] = False
    return stopped


# ---------------------------------------------------------------------------
# AI アシスタント（headless claude / grok(cursor-agent) を SSE で中継）
# ---------------------------------------------------------------------------
# GUI 右側の drawer から「使い方の質問」と「presets/ の YAML 編集」を頼むための
# バックエンド。CLI を stream-json モードで起動し、その行をそのまま SSE で
# ブラウザへ流す。gui_server 自身は Mitsuba も LLM SDK も import しない
# （subprocess だけ）。
#
# 安全側の設計:
#   - claude は --permission-mode acceptEdits（Read/Edit/Write は自動承認、
#     それ以外の承認が要る操作は headless では実行されない）。
#     --dangerously-skip-permissions は使わない。
#   - 127.0.0.1 / localhost 以外にバインドされている場合は /api/ai 系を 404。
AI_BIN_DIR = Path.home() / '.local' / 'bin'
CLAUDE_BIN = AI_BIN_DIR / 'claude'
AGENT_BIN = AI_BIN_DIR / 'agent'
CURSOR_AGENT_BIN = AI_BIN_DIR / 'cursor-agent'
CLAUDE_EFFORTS = {'low', 'medium', 'high', 'xhigh', 'max'}
GROK_EFFORTS = {'low', 'medium', 'high', 'xhigh'}  # Grok 4.6 に max は無い
AI_TIMEOUT_S = 15 * 60
AI_MAX_JOBS = 3
AI_LOCAL_HOSTS = {'127.0.0.1', 'localhost', '::1', '::ffff:127.0.0.1'}
# tool_use の中でファイルを書き換えるツール（done の edited_files 収集用）
AI_EDIT_TOOLS = {'Edit', 'Write', 'NotebookEdit', 'MultiEdit'}

AI: Dict[str, Any] = {'enabled': True}   # main() でバインド先を見て決める
_AI_RUNS: Dict[str, Dict[str, Any]] = {}  # run_id -> {proc, session_id}
_AI_RUNS_LOCK = threading.Lock()

AI_SYSTEM_PROMPT = """\
あなたは mRender（Mitsuba 3 を使った人工衛星レンダリングシステム）の GUI 内に
組み込まれたアシスタントです。役割は次の 3 つです:
(1) 使い方の質問に答える（リポジトリの CLAUDE.md / README.md を参照すること）
(2) presets/ 以下の YAML プリセットの作成・編集
(3) レンダリング設定値の推奨

プリセット編集時の注意:
- YAML のキー名は argparse の dest 名と一致させること。未知のキーは黙って無視
  されるため、効かない設定があればまずキー名を疑う。
- 地球テクスチャの正しいキーは earth_day_texture / earth_night_texture /
  earth_cloud_texture（day_texture 等は無効）。
- 近接撮像シーンは relative verb を使う（render/onboard は地球半径=1 のシーン
  単位なので数十 m の分離距離が float32 精度で崩れる）。

その他:
- ユーザーが明示的に依頼しない限り、ソースコード（*.py）は変更しないこと。
- 編集したプリセットは GUI が自動で再読み込みしてライブプレビューに反映する
  ので、レンダリングの再実行を指示する必要はない。
- 回答は幅の狭いサイドパネルに表示される。簡潔に。ファイル全文の貼り付けや
  巨大な表は避ける。"""


def ai_local_only(host: str) -> bool:
    """gui_server がローカルループバックにバインドされているか。"""
    return str(host or '').strip().lower() in AI_LOCAL_HOSTS


def claude_settings_defaults() -> Dict[str, str]:
    """--model/--effort 未指定時に CLI が使う値（~/.claude/settings.json）。"""
    try:
        s = json.loads((Path.home() / '.claude' / 'settings.json').read_text(encoding='utf-8'))
        return {'model': str(s.get('model') or ''), 'effort': str(s.get('effortLevel') or '')}
    except Exception:  # noqa: BLE001 - 無ければ CLI 側のデフォルトに任せる
        return {'model': '', 'effort': ''}


def is_grok_model(model: str) -> bool:
    """`grok-4.6` 等は Cursor CLI 側へ振り分ける。"""
    return str(model or '').strip().lower().startswith('grok')


def resolve_claude_bin() -> Optional[str]:
    if CLAUDE_BIN.is_file() and os.access(CLAUDE_BIN, os.X_OK):
        return str(CLAUDE_BIN)
    return shutil.which('claude')


def resolve_agent_bin() -> Optional[str]:
    """新しい Cursor CLI (`agent`) を優先し、無ければ `cursor-agent`。"""
    for p in (AGENT_BIN, CURSOR_AGENT_BIN):
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return shutil.which('agent') or shutil.which('cursor-agent')


_agent_help_lock = threading.Lock()
_agent_help_cache: Dict[str, Any] = {'bin': None, 'text': None}


def agent_help_text(bin_path: str) -> str:
    """`agent --help`（キャッシュ）。インストール済み CLI にある flag だけ渡すため。"""
    with _agent_help_lock:
        if _agent_help_cache['bin'] == bin_path and _agent_help_cache['text'] is not None:
            return _agent_help_cache['text']
    try:
        r = subprocess.run([bin_path, '--help'], capture_output=True, text=True, timeout=12)
        text = (r.stdout or '') + '\n' + (r.stderr or '')
    except Exception:  # noqa: BLE001
        text = ''
    with _agent_help_lock:
        _agent_help_cache['bin'] = bin_path
        _agent_help_cache['text'] = text
    return text


def agent_has_flag(bin_path: str, flag: str) -> bool:
    return flag in agent_help_text(bin_path)


# Cursor CLI の stream-json は tool_call.{read,write,…}ToolCall 形式。
# フロントは Claude 形式の assistant/tool_use ブロックを解釈するので、
# started イベントだけを Claude 形式へ翻訳する。
_AGENT_TOOL_MAP = {
    'readToolCall': ('Read', lambda a: {'file_path': a.get('path') or a.get('file_path')}),
    'writeToolCall': ('Write', lambda a: {'file_path': a.get('path') or a.get('file_path')}),
    'editToolCall': ('Edit', lambda a: {'file_path': a.get('path') or a.get('file_path')}),
    'grepToolCall': ('Grep', lambda a: {'pattern': a.get('pattern') or a.get('query')}),
    'globToolCall': ('Glob', lambda a: {'pattern': a.get('glob_pattern') or a.get('pattern')}),
    'shellToolCall': ('Bash', lambda a: {'command': a.get('command') or a.get('cmd')}),
    'bashToolCall': ('Bash', lambda a: {'command': a.get('command') or a.get('cmd')}),
    'webSearchToolCall': ('WebSearch', lambda a: {'query': a.get('query') or a.get('search_term')}),
    'webFetchToolCall': ('WebFetch', lambda a: {'url': a.get('url')}),
}


def _agent_tool_block(tc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(tc, dict) or not tc:
        return None
    fn = tc.get('function')
    if isinstance(fn, dict) and fn.get('name'):
        args = fn.get('arguments')
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:  # noqa: BLE001
                args = {'command': args}
        if not isinstance(args, dict):
            args = {}
        return {'type': 'tool_use', 'name': str(fn.get('name') or 'tool'), 'input': args}
    for key, val in tc.items():
        args = (val or {}).get('args') if isinstance(val, dict) else {}
        if not isinstance(args, dict):
            args = {}
        mapped = _AGENT_TOOL_MAP.get(key)
        if mapped:
            name, conv = mapped
            return {'type': 'tool_use', 'name': name, 'input': conv(args)}
        name = re.sub(r'ToolCall$', '', str(key)) or 'tool'
        return {'type': 'tool_use', 'name': name, 'input': args}
    return None


def normalize_agent_sse_line(line: str) -> Optional[str]:
    """Cursor CLI の NDJSON を Claude 形式イベントへ正規化する。

    tool_call の completed（UI 無し）と user エコーは None を返して捨てる。
    """
    try:
        ev = json.loads(line)
    except Exception:  # noqa: BLE001
        return line
    if not isinstance(ev, dict):
        return line
    typ = ev.get('type')
    if typ == 'user':
        return None
    if typ == 'tool_call':
        if ev.get('subtype') != 'started':
            return None
        blk = _agent_tool_block(ev.get('tool_call') or {})
        if not blk:
            return None
        return json.dumps({
            'type': 'assistant',
            'message': {'content': [blk]},
            'session_id': ev.get('session_id') or '',
        }, ensure_ascii=False)
    return line


def grok_prompt_preamble(sysprompt: str) -> str:
    """Cursor CLI には --append-system-prompt が無いのでプロンプトに畳み込む。"""
    return '[システム指示]\n' + sysprompt + '\n\n'


def cursor_model_id(model: str, effort: str) -> str:
    """UI のモデル名 + effort を Cursor CLI の実モデル ID へ変換する。

    現行 cursor-agent (2025.09+) のモデル ID は effort 埋め込みの
    `cursor-grok-4.6-<effort>[-fast]` 形式（--effort フラグは無い）。
    UI 側は monet 同様 `grok-4.6` / `grok-4.6-fast` + effort 別指定で扱い、
    ここで合成する。effort 未指定は high。
    """
    m = str(model or '').strip().lower()
    fast = m.endswith('-fast')
    base = m[:-5] if fast else m          # grok-4.6
    eff = effort or 'high'
    return f'cursor-{base}-{eff}' + ('-fast' if fast else '')


def build_agent_cmd(agent_bin: str, prompt: str, session_id: str,
                    model: str, effort: str) -> List[str]:
    """Cursor CLI のコマンドライン。

    --force/--approve-mcps は付けないが、--trust（このワークスペース = ROOT の
    信頼）は必須: 無いと headless 実行が Workspace Trust プロンプトで止まる。
    信頼の範囲はこのリポジトリのみ（monet と同挙動）。
    """
    cmd = [agent_bin, '-p', prompt, '--output-format', 'stream-json']
    if agent_has_flag(agent_bin, '--trust'):
        cmd.append('--trust')
    if agent_has_flag(agent_bin, '--workspace'):
        cmd += ['--workspace', str(ROOT)]
    if session_id:
        cmd += ['--resume', session_id]
    if model:
        cmd += ['--model', cursor_model_id(model, effort)]
    return cmd


def gui_context_block(ctx: Any) -> str:
    """リクエストの gui_context を、プロンプト先頭に置くテキストへ変換する。"""
    if not isinstance(ctx, dict):
        return ''
    verb = str(ctx.get('verb') or '').strip()
    text = str(ctx.get('yaml') or '').strip()
    if not verb and not text:
        return ''
    parts = [f'現在の GUI 状態: verb={verb or "(未選択)"}']
    if text:
        parts.append('現在のフォーム設定（YAML）:\n```yaml\n' + text + '\n```')
    return '\n'.join(parts) + '\n\n'


def _kill_ai(proc: subprocess.Popen) -> None:
    """プロセスグループごと SIGTERM（CLI は子プロセスを産む）。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:  # noqa: BLE001
        pass


def _collect_edited(line: str, out: List[str]) -> None:
    """assistant イベントの tool_use から編集対象ファイルを拾う（重複排除）。"""
    if '"tool_use"' not in line:
        return
    try:
        ev = json.loads(line)
    except Exception:  # noqa: BLE001
        return
    if not isinstance(ev, dict) or ev.get('type') != 'assistant':
        return
    blocks = ((ev.get('message') or {}).get('content')
              if isinstance(ev.get('message'), dict) else None)
    if not isinstance(blocks, list):
        return
    for blk in blocks:
        if not isinstance(blk, dict) or blk.get('type') != 'tool_use':
            continue
        if blk.get('name') not in AI_EDIT_TOOLS:
            continue
        inp = blk.get('input') if isinstance(blk.get('input'), dict) else {}
        fp = inp.get('file_path') or inp.get('notebook_path')
        if not fp:
            continue
        try:
            rel = str(Path(str(fp)).resolve().relative_to(ROOT))
        except ValueError:
            continue  # リポジトリ外の編集は報告しない
        if rel not in out:
            out.append(rel)


# ---------------------------------------------------------------------------
# HTTP ハンドラ
# ---------------------------------------------------------------------------
_CONTENT_TYPES = {
    '.html': 'text/html; charset=utf-8', '.js': 'text/javascript',
    '.css': 'text/css', '.png': 'image/png', '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg', '.mp4': 'video/mp4', '.json': 'application/json',
    '.yaml': 'text/plain; charset=utf-8', '.csv': 'text/plain; charset=utf-8',
    '.log': 'text/plain; charset=utf-8', '.exr': 'application/octet-stream',
    '.wasm': 'application/wasm', '.webp': 'image/webp',
}


class GuiHandler(BaseHTTPRequestHandler):
    """GUI 用のシンプルな JSON API + 静的ファイル配信。"""

    server_version = 'mrenderGUI/1.0'

    # --- ヘルパ ---
    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message: str, status: int = 400) -> None:
        self._send_json({'error': message}, status=status)

    def _read_json(self) -> Any:
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b'{}'
        return json.loads(raw.decode('utf-8'))

    def _send_file(self, path: Path) -> None:
        try:
            resolved = path.resolve()
            resolved.relative_to(ROOT)  # パストラバーサル防止
        except ValueError:
            self._send_error_json('forbidden', 403)
            return
        if not resolved.is_file():
            self._send_error_json('not found', 404)
            return
        ctype = _CONTENT_TYPES.get(resolved.suffix.lower(), 'application/octet-stream')
        data = resolved.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # noqa: D102 - 静かなログ
        pass

    # --- GET ---
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        route = parsed.path

        if route in ('/', '/index.html'):
            self._send_file(GUI_DIR / 'index.html')
        elif route.startswith('/gui/'):
            path = (GUI_DIR / urllib.parse.unquote(route[len('/gui/'):])).resolve()
            if not path.is_relative_to(GUI_DIR.resolve()):
                self._send_error_json('forbidden', 403)
            else:
                self._send_file(path)
        elif route.startswith('/model-assets/'):
            path = model_asset_path(urllib.parse.unquote(route[len('/model-assets/'):]))
            if path is None:
                self._send_error_json('forbidden', 403)
            else:
                self._send_file(path)
        elif route == '/api/model-info':
            from model_materials import registered_materials
            path = (ROOT / ((qs.get('path') or ['models/test_cube.obj'])[0] or 'models/test_cube.obj')).resolve()
            if model_root_of(path) is None or not path.is_file():
                self._send_error_json('モデルが見つかりません', 404)
                return
            if path.suffix.lower() not in ('.obj', '.ply', '.glb', '.gltf'):
                self._send_error_json('未対応のモデル形式です')
                return
            materials = registered_materials(str(path))
            self._send_json({
                'url': model_asset_url(path),
                'parts': materials[0] if materials else {},
                'default_material': materials[1] if materials else {},
            })
        elif route == '/api/state':
            self._send_json({
                'verbs': build_verb_schema(),
                'files': list_files(),
                'preset_meta': preset_ui_metadata(),
            })
        elif route == '/api/preset':
            path = (qs.get('path') or [''])[0]
            verb = (qs.get('verb') or ['render'])[0]
            p = (ROOT / path)
            if not p.is_file():
                self._send_error_json(f'プリセットが見つかりません: {path}', 404)
                return
            try:
                flat = load_preset_flat(p, verb)
            except Exception as exc:  # noqa: BLE001
                self._send_error_json(f'プリセット読込エラー: {exc}', 500)
                return
            self._send_json({'flat': flat, 'raw': p.read_text()})
        elif route == '/api/jobs':
            with _JOBS_LOCK:
                ids = list(JOBS.keys())
            self._send_json([job_status(i) for i in reversed(ids)])
        elif route == '/api/job':
            job_id = (qs.get('id') or [''])[0]
            st = job_status(job_id)
            if st is None:
                self._send_error_json('unknown job', 404)
            else:
                self._send_json(st)
        elif route == '/api/machines':
            self._send_json({
                'machines': ['local'] + sorted(REMOTE_HOSTS.keys()),
                'live_machine': LIVE.get('machine', 'local'),
            })
        elif route == '/api/runs':
            self._send_json(list_runs())
        elif route == '/api/file':
            path = (qs.get('path') or [''])[0]
            self._send_file(ROOT / path)
        elif route == '/api/live/frame':
            self._proxy_live_frame(parsed.query)
        elif route == '/api/live/status':
            self._proxy_live_status()
        elif route == '/api/ai/defaults':
            if not AI['enabled']:
                self._send_error_json('not found', 404)
                return
            d = claude_settings_defaults()
            self._send_json({
                'model': d['model'], 'effort': d['effort'],
                'claude_available': resolve_claude_bin() is not None,
                'grok_available': resolve_agent_bin() is not None,
            })
        else:
            self._send_error_json('not found', 404)

    # --- ライブプレビュー中継 ---
    _LIVE_HEADERS = ('X-Live-Gen', 'X-Live-Refined', 'X-Live-Simplified',
                     'X-Live-Status',
                     'X-Live-Ms', 'X-Live-Error')

    def _proxy_live_frame(self, query: str) -> None:
        """ワーカーの /frame をバイナリ + X-Live-* ヘッダごと透過する。"""
        if not live_worker_alive():
            self._send_error_json('live worker not running', 503)
            return
        path = '/frame' + (f'?{query}' if query else '')
        try:
            status, headers, body = _live_get(path, timeout=20.0)
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(f'live worker 通信エラー: {exc}', 502)
            return
        self.send_response(status)
        for name in self._LIVE_HEADERS:
            if headers.get(name) is not None:
                self.send_header(name, headers[name])
        self.send_header('Cache-Control', 'no-store')
        if status == 304:
            self.end_headers()
            return
        ctype = headers.get('Content-Type', 'application/octet-stream')
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy_live_status(self) -> None:
        if not live_worker_alive():
            self._send_json({'running': False,
                             'error': 'live worker not running'})
            return
        try:
            status, _headers, body = _live_get('/status', timeout=10.0)
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(f'live worker 通信エラー: {exc}', 502)
            return
        try:
            obj = json.loads(body.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_error_json('live worker が不正な応答を返しました', 502)
            return
        if isinstance(obj, dict):
            obj = {'running': True, **obj}
        self._send_json(obj, status=status)

    # --- POST ---
    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        try:
            payload = self._read_json()
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_error_json(f'JSON パースエラー: {exc}')
            return

        if route == '/api/compose':
            # フォーム → YAML テキスト（プレビュー用、実行はしない）
            try:
                config = compose_config(payload.get('fields') or {},
                                        payload.get('yaml_blocks') or {})
            except ValueError as exc:
                self._send_error_json(str(exc))
                return
            text = yaml.safe_dump(config, sort_keys=True, allow_unicode=True)
            verb = payload.get('verb', 'render')
            self._send_json({
                'yaml': text,
                'cli': f'python mrender.py {verb} --config <保存した.yaml>',
            })
        elif route == '/api/render':
            verb = payload.get('verb')
            if verb not in ('render', 'preview', 'onboard', 'lightcurve',
                            'relative', 'rotation', 'groundobs'):
                self._send_error_json(f'未対応 verb: {verb}')
                return
            try:
                config = compose_config(payload.get('fields') or {},
                                        payload.get('yaml_blocks') or {})
            except ValueError as exc:
                self._send_error_json(str(exc))
                return
            try:
                st = start_job(verb, payload.get('name') or verb, config,
                               machine=payload.get('machine') or 'local')
            except ValueError as exc:
                self._send_error_json(str(exc))
                return
            except (subprocess.SubprocessError, OSError) as exc:
                self._send_error_json(f'リモート起動に失敗: {exc}', 502)
                return
            self._send_json(st)
        elif route == '/api/cancel':
            ok = cancel_job(payload.get('id', ''))
            self._send_json({'ok': ok})
        elif route == '/api/save_preset':
            name = _SLUG_RE.sub('_', payload.get('name') or '').strip('_')
            if not name:
                self._send_error_json('プリセット名を指定してください')
                return
            try:
                config = compose_config(payload.get('fields') or {},
                                        payload.get('yaml_blocks') or {})
            except ValueError as exc:
                self._send_error_json(str(exc))
                return
            path = ROOT / 'presets' / f'{name}.yaml'
            with path.open('w') as fh:
                meta = payload.get('gui_meta')
                if valid_preset_ui_metadata(meta):
                    fh.write(GUI_META_PREFIX + json.dumps(meta, ensure_ascii=False) + '\n')
                yaml.safe_dump(config, fh, sort_keys=True, allow_unicode=True)
            self._send_json({'saved': str(path.relative_to(ROOT))})
        elif route == '/api/live/update':
            # 初回リクエストでワーカーを遅延起動（プロセス死亡時は自動再起動）
            try:
                ensure_live_worker()
            except RuntimeError as exc:
                self._send_error_json(str(exc), 503)
                return
            try:
                status, _headers, body = _live_post('/update', payload, timeout=20.0)
            except Exception as exc:  # noqa: BLE001
                self._send_error_json(f'live worker 通信エラー: {exc}', 502)
                return
            try:
                obj = json.loads(body.decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_error_json('live worker が不正な応答を返しました', 502)
                return
            self._send_json(obj, status=status)
        elif route == '/api/live/stop':
            self._send_json({'ok': stop_live_worker()})
        elif route == '/api/live/machine':
            machine = payload.get('machine') or 'local'
            try:
                resolve_machine(machine)
            except ValueError as exc:
                self._send_error_json(str(exc))
                return
            with _LIVE_LOCK:
                if machine != LIVE.get('machine', 'local'):
                    stop_live_worker()
                    LIVE['machine'] = machine
            self._send_json({'ok': True, 'machine': machine})
        elif route == '/api/ai':
            if not AI['enabled']:
                self._send_error_json('not found', 404)
                return
            self._ai_run(payload)
        elif route == '/api/ai/stop':
            if not AI['enabled']:
                self._send_error_json('not found', 404)
                return
            self._ai_stop(payload)
        else:
            self._send_error_json('not found', 404)

    # --- AI アシスタント ---
    def _ai_run(self, req: Dict[str, Any]) -> None:
        """POST /api/ai — CLI を起動し stream-json を SSE で中継する。"""
        prompt = str(req.get('prompt') or '').strip()
        if not prompt:
            self._send_error_json('empty prompt')
            return
        ask = prompt  # ログ用（prompt には以下で前置きが付く）
        ctx = gui_context_block(req.get('gui_context'))
        if ctx:
            prompt = ctx + prompt

        session_id = str(req.get('session_id') or '').strip()
        model = str(req.get('model') or '').strip()
        effort = str(req.get('effort') or '').strip()
        grok = is_grok_model(model)
        if grok and effort == 'max':
            effort = 'xhigh'          # Grok 4.6 に max は無い
        if effort and effort not in (GROK_EFFORTS if grok else CLAUDE_EFFORTS):
            self._send_error_json(f'bad effort: {effort}')
            return
        if model and not re.fullmatch(r'[A-Za-z0-9.\[\]_-]{1,64}', model):
            self._send_error_json('bad model')
            return

        engine = 'grok' if grok else 'claude'
        if grok:
            runner_bin = resolve_agent_bin()
            if not runner_bin:
                self._send_error_json(
                    'Cursor CLI (agent) が見つかりません。'
                    'curl https://cursor.com/install -fsS | bash のあと agent login してください',
                    500)
                return
        else:
            runner_bin = resolve_claude_bin()
            if not runner_bin:
                self._send_error_json('claude CLI not found', 500)
                return

        # 実行スロットの確保（セッション間は並列、同一セッション内は直列）
        run_id = f'r{os.urandom(6).hex()}'
        with _AI_RUNS_LOCK:
            if len(_AI_RUNS) >= AI_MAX_JOBS:
                self._send_json({'error': 'busy', 'active': len(_AI_RUNS)}, status=409)
                return
            if session_id and any(r['session_id'] == session_id for r in _AI_RUNS.values()):
                self._send_json({'error': 'session_busy'}, status=409)
                return
            _AI_RUNS[run_id] = {'proc': None, 'session_id': session_id}
        try:
            if grok:
                cmd = build_agent_cmd(runner_bin,
                                      grok_prompt_preamble(AI_SYSTEM_PROMPT) + prompt,
                                      session_id, model, effort)
            else:
                cmd = [runner_bin, '-p', prompt,
                       '--output-format', 'stream-json', '--verbose',
                       '--permission-mode', 'acceptEdits',
                       '--append-system-prompt', AI_SYSTEM_PROMPT]
                if session_id:
                    cmd += ['--resume', session_id]
                if model:
                    cmd += ['--model', model]
                if effort:
                    cmd += ['--effort', effort]
            env = dict(os.environ)
            env['PATH'] = f'{AI_BIN_DIR}:{env.get("PATH", "")}'
            try:
                proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, start_new_session=True)
            except Exception as exc:  # noqa: BLE001
                self._send_error_json(str(exc), 500)
                return
            with _AI_RUNS_LOCK:
                _AI_RUNS[run_id]['proc'] = proc
            dflt = claude_settings_defaults()
            eff_model = model or ('' if grok else dflt['model'])
            eff_effort = effort or ('' if grok else dflt['effort'])
            print(f'▶ {engine}[{run_id[:7]}]: {ask[:80]!r}'
                  + (f'  (resume {session_id[:8]})' if session_id else '')
                  + f'  [{eff_model or "?"} / {eff_effort or "?"}]'
                  + f'  [{len(_AI_RUNS)} active]')

            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            # SSE は長さ不定で Content-Length を送れない。keep-alive のままだと
            # ブラウザ側のストリームが終端しないので、接続を閉じて終わる。
            self.close_connection = True

            killer = threading.Timer(AI_TIMEOUT_S, _kill_ai, args=(proc,))
            killer.daemon = True
            killer.start()
            stderr_buf: List[str] = []
            t_err = threading.Thread(target=lambda: stderr_buf.append(proc.stderr.read()),
                                     daemon=True)
            t_err.start()

            def emit(payload: str) -> bool:
                try:
                    self.wfile.write(f'data: {payload}\n\n'.encode('utf-8'))
                    self.wfile.flush()
                    return True
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return False

            client_gone = not emit(json.dumps({
                'type': 'run_started', 'run_id': run_id, 'engine': engine,
                'model': eff_model, 'effort': eff_effort}, ensure_ascii=False))

            # ブラウザが途中で切断しても CLI は完走させる（編集が中途半端な
            # 状態で止まるのを避ける）。書き込みだけをやめる。
            edited: List[str] = []
            for line in proc.stdout:
                line = line.rstrip('\n')
                if not line:
                    continue
                if grok:
                    line = normalize_agent_sse_line(line)
                    if line is None:
                        continue
                _collect_edited(line, edited)
                # 新規会話の session_id は init イベントから拾う（同一セッションの
                # 同時 resume を 409 にするため記録しておく）
                if not session_id and '"init"' in line:
                    try:
                        ev = json.loads(line)
                        if ev.get('type') == 'system' and ev.get('subtype') == 'init':
                            session_id = str(ev.get('session_id') or '')
                            with _AI_RUNS_LOCK:
                                _AI_RUNS[run_id]['session_id'] = session_id
                    except Exception:  # noqa: BLE001
                        pass
                if not client_gone:
                    client_gone = not emit(line)
            code = proc.wait()
            killer.cancel()
            t_err.join(timeout=5)
            err = (stderr_buf[0] if stderr_buf else '').strip()
            print(('✓' if code == 0 else '✗') + f' {engine}[{run_id[:7]}] exit={code}'
                  + (f'  edited: {", ".join(edited)}' if edited else ''))
            if not client_gone:
                if code != 0:
                    emit(json.dumps({'type': 'error', 'exit': code, 'stderr': err[-800:]},
                                    ensure_ascii=False))
                emit(json.dumps({'type': 'done', 'exit': code, 'edited_files': edited},
                                ensure_ascii=False))
        finally:
            with _AI_RUNS_LOCK:
                _AI_RUNS.pop(run_id, None)

    def _ai_stop(self, req: Dict[str, Any]) -> None:
        """POST /api/ai/stop — run_id 指定（無ければ全部）で CLI を停止する。"""
        run_id = str(req.get('run_id') or '').strip()
        with _AI_RUNS_LOCK:
            if run_id:
                targets = {k: v for k, v in _AI_RUNS.items() if k == run_id}
            else:
                targets = dict(_AI_RUNS)   # id 無し → 全停止
        stopped = []
        for rid, r in targets.items():
            proc = r.get('proc')
            if proc is not None and proc.poll() is None:
                _kill_ai(proc)
                stopped.append(rid)
        if stopped:
            print(f'⏹ ai: stop requested ({", ".join(s[:7] for s in stopped)})')
        self._send_json({'ok': True, 'stopped': stopped})


class _ReuseAddrServer(ThreadingHTTPServer):
    """TIME_WAIT 状態のソケットを掴んだままでも再起動できるようにする。

    Ctrl-C 直後の再起動で `Address already in use` を避ける。
    ただし別プロセスが実際に LISTEN 中の場合は依然 bind に失敗する
    （その場合は main 側で分かりやすいメッセージを出す）。
    """
    allow_reuse_address = True


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description='mrender GUI サーバー')
    parser.add_argument('--port', type=int, default=8600)
    parser.add_argument('--host', type=str, default='127.0.0.1')
    parser.add_argument('--no-browser', action='store_true', default=False)
    parser.add_argument('--live-port', type=int, default=None,
                        help='ライブプレビュー ワーカーのポート（既定: --port + 1）')
    args = parser.parse_args(argv)

    LIVE['host'] = args.host
    LIVE['port'] = args.live_port or (args.port + 1)
    load_remote_hosts()
    # AI drawer はローカル専用（CLI をリポジトリ上で実行するため、
    # LAN 等に公開されたインスタンスでは /api/ai 系を 404 にする）
    AI['enabled'] = ai_local_only(args.host)

    # フォーム項目に対する既定値の欠けを起動ログで知らせる
    # （defaults は argparse から自動導出しているので、欠け = 設定ミス）
    _warn_schema_gaps()

    # starfield.exr は Git 管理外（100 MB 超）。clone 直後は無いので、
    # envmap ドロップダウンに最初から載るようここで生成しておく。
    try:
        from asset_bootstrap import ensure_starfield_envmap
        ensure_starfield_envmap(ROOT / 'assets' / 'starfield.exr')
    except Exception as exc:  # noqa: BLE001
        print(f'  starfield.exr の生成に失敗しました（envmap 無しで続行）: {exc}',
              file=sys.stderr)

    try:
        server = _ReuseAddrServer((args.host, args.port), GuiHandler)
    except OSError as exc:
        # ポート衝突（Errno 48/98 = Address already in use）は、
        # トレースバックではなく対処法を添えて終了する。
        print(
            f'✗ ポート {args.port} は既に使用中です（{exc}）。\n'
            f'  既存の GUI が動いている可能性があります → http://{args.host}:{args.port}/\n'
            f'  別ポートで起動するには: python mrender.py gui --port 8601\n'
            f'  使用中プロセスを確認するには: lsof -iTCP:{args.port} -sTCP:LISTEN -P',
            file=sys.stderr,
        )
        raise SystemExit(1)

    url = f'http://{args.host}:{args.port}/'
    print(f'mrender GUI: {url}  (Ctrl-C で終了)')
    print(f'  ライブプレビュー ワーカー: 優先ポート {LIVE["port"]}（使用中なら空きポート、初回更新で起動）')
    if REMOTE_HOSTS:
        print(f'  リモート実行マシン: {", ".join(sorted(REMOTE_HOSTS))}（gui_hosts.json）')
    if AI['enabled']:
        print('  AI アシスタント: 有効'
              + ('' if resolve_claude_bin() else '（claude CLI が見つかりません）'))
    else:
        print(f'  AI アシスタント: 無効（{args.host} は loopback ではありません）')
    if not args.no_browser:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    def on_terminate(_signum, _frame):
        raise KeyboardInterrupt

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, on_terminate)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n終了します。')
    finally:
        stop_live_worker()
        server.server_close()


if __name__ == '__main__':
    main()
