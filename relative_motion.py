#!/usr/bin/env python3
"""
相対運動レンダリング: 軌道力学なし。相対位置と相対姿勢のみを与える。

simple_rotation.py の兄弟スクリプト。
- simple_rotation: 単機の姿勢運動だけを扱う
- relative_motion: 2機（chief / deputy）の相対配置を扱う

共有モジュール（両者で実装を 1 本化しているもの）:
- scene_common.py  : propagate_attitude / create_axes / create_satellite /
                     split_obj_by_parts / load_model_with_parts（Mitsuba 依存）
- verb_parsers.py  : build_parser（Mitsuba 非依存、GUI が既定値の導出に使う）
- config_loader.py : load_yaml_config（1 段フラット化、Mitsuba 非依存）

軌道伝播・地球は一切描画しない。chief は原点に固定し、deputy を
ユーザー指定の相対位置 r_rel と相対姿勢クォータニオン q_rel で配置する。

座標系・量の規約:
  - シーン座標 = chief 中心の Hill / RTN(LVLH) frame として扱う想定
    （chief を原点・恒等姿勢に置けば、rel_position はそのまま RTN 成分）
  - 位置単位 [km]（Mitsuba シーン単位とみなして直接配置。スケール感は
    モデル側の scale で調整する）
  - クォータニオン: スカラーラスト [qx, qy, qz, qw] (yoshimulib SCALAR=4)
  - **姿勢クォータニオンの向き: q は inertial → body**（航空宇宙標準）。
    yoshimulib の `q2dcm(q, scalar=4)` はそのまま慣性→機体の姿勢行列
    A(q) を返す（数値検証済み: q2dcm(+90°z) @ [1,0,0] = [0,-1,0]）。
    Mitsuba の `to_world` に必要なのは body → world なので、
    シーン構築では常に **A(q).T** を使う（make_body_transform を参照）。
  - 角度はラジアン、角速度 rad/s、慣性モーメント kg·m²
  - rel_quat の意味: chief Body → deputy Body の相対姿勢
    （A_dep = A_rel · A_chief。chief が恒等のとき、そのまま deputy の慣性姿勢）

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

# プロジェクトルートを sys.path に追加（yoshimulib のため）
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mitsuba as mi

from yoshimulib.attitude.quaternion import q2dcm

from mi_variant import init_variant
init_variant(mi)  # MRENDER_VARIANT 環境変数でバリアント切替（mi_variant.py 参照）

# 姿勢動力学・シーン構成要素は simple_rotation.py と共有（scene_common.py が
# 唯一の実装）。ここでは後方互換のため同名で再エクスポートしている
# （live_worker の `RM.propagate_attitude` などの参照経路を壊さないため）。
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
from verb_parsers import build_relative_parser as build_parser  # noqa: E402


# ---------------------------------------------------------------------------
# Transform ヘルパー
# ---------------------------------------------------------------------------
def make_body_transform(position: np.ndarray, q: np.ndarray) -> mi.ScalarTransform4f:
    """位置 + クォータニオン姿勢から Mitsuba Transform4f を組む。

    Mitsuba の 4x4 同次行列の並びは
        [ R(3x3)  t(3,) ]
        [ 0 0 0    1    ]
    で、列ベクトル右掛け規約 world = M·local。よって上 3x3 には
    **body → inertial(world)** の回転行列を入れる必要がある。

    q は inertial → body の姿勢クォータニオンで、yoshimulib の
    `q2dcm(q, scalar=4)` はその姿勢行列 A(q)（= inertial → body）を返す。
    したがって body → inertial は **A(q).T**。ここを転置せずに入れると
    描画姿勢が真値の逆回転（鏡映的な誤り）になる。

    Args:
        position : 慣性座標系での衛星中心位置（シーン単位）
        q        : クォータニオン [qx,qy,qz,qw]（inertial → body の姿勢）

    Returns:
        Mitsuba ScalarTransform4f（4x4 同次変換、body → world）
    """
    m = np.eye(4)
    m[:3, :3] = q2dcm(q, scalar=SCALAR).T  # A(q)ᵀ = body → inertial
    m[:3, 3] = np.asarray(position, dtype=float)
    return mi.ScalarTransform4f(m)


def compose_deputy_state(chief_pos: np.ndarray, chief_q: np.ndarray,
                         rel_pos: np.ndarray, rel_q: np.ndarray
                         ) -> Tuple[np.ndarray, np.ndarray]:
    """chief の慣性状態 + 相対状態 → deputy の慣性位置・姿勢クォータニオン。

    バッチ (_run) と GUI ライブプレビュー (live_worker.build_scene_dict) の
    両方がこれを呼ぶ。合成規約の実装を 1 箇所に閉じ込め、両者が乖離しない
    ようにするための共有ヘルパー。

    位置: r_dep^I = r_chief^I + A(q_chief)ᵀ · r_rel
        rel_pos は chief の Body / RTN 系成分なので、慣性へ持ち上げるには
        body → inertial 行列 = A(q_chief)ᵀ を掛ける（q は inertial → body）。

    姿勢合成: rel_q は chief Body → deputy Body なので
        A_dep = A_rel · A_chief
    を満たす必要がある。yoshimulib の q_mult は
        q2dcm(q_mult(q_a, q_b)) == q2dcm(q_a) @ q2dcm(q_b)
    という順序なので、引数は (rel, chief) が正しい。
    数値検証（chief=+90°z, rel=+90°x, scalar=4）:
        A_rel @ A_chief と q2dcm(q_mult(rel, chief)) の差 = 4.9e-32
        q_mult(chief, rel) だと差 = 2.45（不一致）
    また q_mult の戻り値は (1,4) の 2D なので .ravel() で 1D 化する
    （しないと下流の q2dcm(q[1]...) が IndexError になる）。

    Args:
        chief_pos : chief 慣性位置 (3,)（シーン単位）
        chief_q   : chief 姿勢 [qx,qy,qz,qw]（inertial → chief Body、正規化済み前提）
        rel_pos   : deputy 相対位置 (3,)（chief Body/RTN 系成分）
        rel_q     : 相対姿勢 [qx,qy,qz,qw]（chief Body → deputy Body）

    Returns:
        (deputy_pos_inertial (3,), deputy_q_inertial (4,) [inertial → deputy Body])
    """
    chief_pos = np.asarray(chief_pos, dtype=float)
    rel_pos = np.asarray(rel_pos, dtype=float)
    C_chief_b2i = q2dcm(chief_q, scalar=SCALAR).T
    deputy_pos_inertial = chief_pos + C_chief_b2i @ rel_pos

    deputy_q_inertial = np.asarray(rel_q, dtype=float)
    if not np.allclose(chief_q, [0.0, 0.0, 0.0, 1.0]):
        from yoshimulib.attitude.quaternion import q_mult
        deputy_q_inertial = np.asarray(
            q_mult(rel_q, chief_q, scalar=SCALAR), dtype=float
        ).ravel()
        deputy_q_inertial = deputy_q_inertial / np.linalg.norm(deputy_q_inertial)
    return deputy_pos_inertial, deputy_q_inertial


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
# シーン構成要素
# ---------------------------------------------------------------------------
# create_axes / create_satellite / split_obj_by_parts / load_model_with_parts は
# scene_common.py に一本化済み（モジュール冒頭で再エクスポート）。


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
def create_earth_backdrop(chief_pos, direction, altitude_km: float,
                          texture: str, rotation_deg: float = 0.0) -> dict:
    """chief 近傍シーン（km 単位）に地球球体を背景として置く。

    シーン座標は chief 中心の RTN(Hill) frame 想定なので、`direction` に
    [0,0,-1]（-N 方向 = 地心方向）などを与えると chief の高度 `altitude_km`
    に対応する位置へ地球（半径 R_EARTH_KM の解析球）が置かれる。
    パストレーシングにより地球アルベド（earthshine）の照り返しも自動で乗る。
    """
    from yoshimulib.orbit.orbital_elements import R_EARTH_KM
    d = np.asarray(direction, dtype=float)
    d = d / np.linalg.norm(d)
    center = np.asarray(chief_pos, dtype=float) + d * (R_EARTH_KM + altitude_km)
    to_world = (mi.ScalarTransform4f.translate(center.tolist())
                @ mi.ScalarTransform4f.rotate([0, 0, 1], rotation_deg)
                @ mi.ScalarTransform4f.scale([R_EARTH_KM] * 3))
    return {
        'earth': {
            'type': 'sphere',
            'to_world': to_world,
            'bsdf': {
                'type': 'diffuse',
                'reflectance': {'type': 'bitmap', 'filename': texture},
            },
        },
    }


def build_scene(chief_xform: mi.ScalarTransform4f,
                deputy_xform: mi.ScalarTransform4f,
                *, width: int, height: int, samples: int,
                chief_model: Optional[str], chief_parts: Optional[dict],
                chief_default_bsdf: Optional[dict], chief_scale: float,
                deputy_model: Optional[str], deputy_parts: Optional[dict],
                deputy_default_bsdf: Optional[dict], deputy_scale: float,
                camera_origin, camera_target, camera_fov: float,
                show_inertial_axes: bool, show_body_axes: bool,
                camera_up=(0.0, 1.0, 0.0), max_depth: int = 8,
                sun_direction=(1.0, -1.0, -0.5),
                sun_rgb=(5.0, 5.0, 4.8),
                env_dict: Optional[dict] = None,
                earth_dict: Optional[dict] = None,
                hide_chief: bool = False) -> dict:
    """Mitsuba シーン辞書を組み立てる。"""
    if env_dict is None:
        env_dict = {
            'type': 'constant',
            'radiance': {'type': 'rgb', 'value': [0.02, 0.02, 0.03]},
        }
    scene_dict = {
        'type': 'scene',
        'integrator': {'type': 'path', 'max_depth': max_depth},
        'camera': {
            'type': 'perspective',
            'fov': camera_fov,
            # シーン単位は km。Mitsuba 既定の near_clip=0.01 は 10 m 相当で、
            # 近接撮像（数 m〜数十 m）のターゲットを丸ごと切り落としてしまう。
            # near を 1e-5 km (= 1 cm)、far を 1e6 km（地球背景を十分含む）に広げる。
            'near_clip': 1e-5,
            'far_clip': 1e6,
            'to_world': mi.ScalarTransform4f.look_at(
                origin=camera_origin,
                target=camera_target,
                up=list(camera_up),
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
            'direction': list(sun_direction),
            'irradiance': {'type': 'rgb', 'value': list(sun_rgb)},
        },
        'envmap': env_dict,
    }

    if earth_dict:
        scene_dict.update(earth_dict)

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

    if not hide_chief:
        add_object(scene_dict, chief_xform,
                   model_path=chief_model, part_bsdfs=chief_parts,
                   default_bsdf=chief_default_bsdf, scale=chief_scale, prefix='chief')
    add_object(scene_dict, deputy_xform,
               model_path=deputy_model, part_bsdfs=deputy_parts,
               default_bsdf=deputy_default_bsdf, scale=deputy_scale, prefix='deputy')

    return scene_dict


# ---------------------------------------------------------------------------
# YAML 読み込み・パーサ（mrender verb / GUI から再利用するため分離）
# ---------------------------------------------------------------------------
# load_yaml_config = config_loader.load_flat_yaml_config（1 段フラット化）
# build_parser     = verb_parsers.build_relative_parser（Mitsuba 非依存）
# どちらもモジュール冒頭で import 済み。ここでは公開名を保つためだけの節。


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


def build_environment(args: argparse.Namespace, chief_pos: np.ndarray
                      ) -> Tuple[list, Optional[dict], Optional[dict]]:
    """太陽色・環境光・地球背景を組み立てて (sun_rgb, env_dict, earth_dict) を返す。

    これらはフレーム間で不変なので、バッチ (`_run`) は 1 度だけ呼ぶ。
    GUI ライブプレビュー (live_worker.build_scene_dict) も同じ関数を呼ぶことで、
    式の複製を無くしバッチとライブの画が乖離しないようにしている。

    規則:
      - sun_temperature 指定時は黒体放射色を最大成分で正規化し irradiance 倍。
        未指定なら従来互換の淡い暖色 [1, 1, 0.96] × irradiance。
      - starfield 指定時は envmap（scale = env_brightness、既定 1.0）。
        starfield 無しで env_brightness 指定なら定数環境光 [b, b, 1.2b]。
        どちらも無ければ None を返し、build_scene 側の既定（淡い暗色）に任せる。
      - show_earth 指定時のみ地球背景の解析球を作る。

    Args:
        args      : relative verb の argparse Namespace
        chief_pos : chief の慣性位置（地球球体の配置基準、シーン単位 km）

    Returns:
        (sun_rgb, env_dict, earth_dict)
    """
    sun_irr = float(args.sun_irradiance)
    sun_temp = args.sun_temperature
    if sun_temp:
        from optical_lighting import blackbody_to_rgb
        rgb = blackbody_to_rgb(float(sun_temp))
        rgb = rgb / max(float(np.max(rgb)), 1e-9)
        sun_rgb = (rgb * sun_irr).tolist()
    else:
        # 従来互換: 淡い暖色 [5,5,4.8] を irradiance でスケール
        sun_rgb = [sun_irr, sun_irr, sun_irr * 0.96]

    env_brightness = args.env_brightness
    starfield = args.starfield
    if starfield:
        env_dict = {'type': 'envmap', 'filename': str(starfield),
                    'scale': float(env_brightness if env_brightness is not None else 1.0)}
    elif env_brightness is not None:
        b = float(env_brightness)
        env_dict = {'type': 'constant', 'radiance': {'type': 'rgb', 'value': [b, b, b * 1.2]}}
    else:
        env_dict = None  # build_scene 側の従来デフォルト

    earth_dict = None
    if args.show_earth:
        earth_dict = create_earth_backdrop(
            chief_pos,
            args.earth_direction,
            float(args.earth_altitude_km),
            str(args.earth_texture),
            float(args.earth_rotation_deg),
        )
    return sun_rgb, env_dict, earth_dict


def _run(args: argparse.Namespace) -> None:
    """argparse Namespace を受け取り、シーンを連番でレンダする。

    フレームごとの処理:
      ① モードに応じて (rel_pos, rel_q) を決定
         - static: 初期値固定
         - csv   : 時刻 t を CSV 補間
         - tumble: rel_pos 固定、rel_q は前フレームから姿勢動力学で進める
      ② chief 姿勢で rel_pos を慣性座標に持ち上げる:
            r_dep^I = r_chief^I + A(q_chief)ᵀ · r_rel
         （q は inertial → body なので body → inertial は転置）
         q_dep^I は chief 恒等なら rel_q そのまま、そうでなければ
         A_dep = A_rel · A_chief となるよう q_mult(rel_q, chief_q) で合成
      ③ Mitsuba シーン辞書を組み立てて mi.render() → PNG 保存
      ④ tumble の場合のみ q_dep, w_dep を propagate_attitude で次フレームへ
    """
    output_dir = Path(args.output_dir or 'output/relative')
    output_dir.mkdir(parents=True, exist_ok=True)

    # dict 系（chief_parts / *_bsdf）は argparse に dest が無く YAML からしか
    # 来ないので getattr で拾う。それ以外は登録済み dest なので直接参照する。
    chief_parts = getattr(args, 'chief_parts', None)
    chief_default_bsdf = getattr(args, 'chief_bsdf', None)
    deputy_parts = getattr(args, 'deputy_parts', None)
    deputy_default_bsdf = getattr(args, 'deputy_bsdf', None)

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
    start_t = float(args.start_time or 0.0)

    # tumble モード用の初期状態
    q_dep = rel_q_init.copy()
    w_dep = np.array([args.wx, args.wy, args.wz], dtype=float)
    I_body, I_inv = inertia_matrices(args.Ix, args.Iy, args.Iz)

    # --- 宇宙環境（太陽色・環境光・地球背景）はフレーム間で不変なので先に構築 ---
    sun_rgb, env_dict, earth_dict = build_environment(args, chief_pos)

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

        # deputy の慣性位置・姿勢を合成（規約・数値検証は compose_deputy_state
        # の docstring 参照。live_worker と共有し実装を 1 箇所に閉じ込める）。
        deputy_pos_inertial, deputy_q_inertial = compose_deputy_state(
            chief_pos, chief_q, rel_pos, rel_q)

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
            camera_up=args.camera_up,
            max_depth=int(args.max_depth),
            sun_direction=args.sun_direction,
            sun_rgb=sun_rgb,
            env_dict=env_dict,
            earth_dict=earth_dict,
            hide_chief=bool(args.hide_chief),
        )
        # テクスチャ/星空 EXR の毎フレーム再デコードを避ける
        # （BSDF テクスチャは実体共有、envmap 等は Bitmap キャッシュ。
        #  live_worker のリファインパスと同一経路になり、ライブ⇔バッチの
        #  ピクセル一致が構成上保証される）
        from asset_cache import preload_bitmaps, share_bsdf_textures
        scene = mi.load_dict(preload_bitmaps(share_bsdf_textures(scene_dict)))
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
